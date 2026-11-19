"""Account-bound platform client and a loopback adapter for Harness's public factory.

Harness retains all generic protocol/model behavior. Its existing base_url,
headers and session_header point at this sidecar's private relay. Each SDK call
gets fresh cloud authorization and a new logical request ID; SDK retries are
turned off, so reconnecting never silently sends a second paid request.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.contracts import IdentityView
from octop.infra.models.contracts import (
    BudgetView,
    ModelContext,
    ModelRequest,
    ModelView,
    Purpose,
    RequestView,
    UsageSummary,
)
from octop.infra.models.protocols import PATHS
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from octop.infra.applications.local import LocalApplication


class PlatformFactory(ChatModelFactory):
    def get_chat_model(self, model_ref: str, *, max_retries: int | None = None) -> BaseChatModel:
        # Application billing policy, using Harness's public retry parameter.
        return super().get_chat_model(model_ref, max_retries=0)


class LocalModelResponse:
    def __init__(self, response: httpx.Response, calls: set[asyncio.Task[Any]]) -> None:
        self.response, self.calls = response, calls

    async def chunks(self) -> AsyncIterator[bytes]:
        current = asyncio.current_task()
        if current:
            self.calls.add(current)
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        finally:
            if current:
                self.calls.discard(current)
            await self.close()

    async def close(self) -> None:
        await self.response.aclose()


class LocalModels:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        cloud_url: str,
        local_origin: str,
        identity: IdentityView,
        access: Callable[..., Awaitable[str]],
    ) -> None:
        self.client, self.cloud_url, self.local_origin = client, cloud_url, local_origin
        self.identity, self.access = identity, access
        self.api_token = secrets.token_urlsafe(32)
        self.runtime: LocalApplication | None = None
        self._factories: list[PlatformFactory] = []
        self._calls: set[asyncio.Task[Any]] = set()
        self.closed = False

    async def catalog(self) -> list[ModelView]:
        token = await self.access(expected_identity=self.identity)
        try:
            response = await self.client.get(
                self.cloud_url + "/api/models", headers={"Authorization": "Bearer " + token}
            )
            response.raise_for_status()
            return [ModelView.model_validate(r) for r in response.json()]
        except (httpx.HTTPError, ValueError) as exc:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE) from exc

    async def _usage(self, path: str, params: dict[str, int]) -> Any:
        token = await self.access(expected_identity=self.identity)
        try:
            response = await self.client.get(
                self.cloud_url + "/api/usage/" + path,
                headers={"Authorization": "Bearer " + token},
                params=params,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OctopError.localized(ErrorCode.CLOUD_UNAVAILABLE) from exc

    async def budgets(self) -> list[BudgetView]:
        return [BudgetView.model_validate(r) for r in await self._usage("budgets", {})]

    async def requests(self, limit: int) -> list[RequestView]:
        return [
            RequestView.model_validate(r) for r in await self._usage("requests", {"limit": limit})
        ]

    async def summary(self, since: int, until: int) -> list[UsageSummary]:
        return [
            UsageSummary.model_validate(r)
            for r in await self._usage("summary", {"since": since, "until": until})
        ]

    async def validate_model(self, model_id: str) -> None:
        if not any(
            m.model_id == model_id and m.protocol != "embeddings" for m in await self.catalog()
        ):
            raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)

    async def factory(self, agent_id: str) -> PlatformFactory:
        catalog = [m for m in await self.catalog() if m.protocol != "embeddings"]
        providers = []
        # Each model has its own provider entry internally, but model refs stay
        # platform/<logical-name>. A single account can use mixed protocols.
        # Harness refs are provider/model. Distinct protocol factories would
        # change the public ID, so use one factory whose public lookup delegates
        # to an independent standard Harness factory for each logical model.
        for m in catalog:
            providers.append(
                ProviderConfig(
                    id="platform",
                    base_url=self.local_origin + "/api/platform",
                    api_key=self.api_token,
                    protocol="anthropic" if m.protocol == "messages" else "openai",
                    headers={"X-Octop-Agent-Id": agent_id},
                    session_header="X-Octop-Thread-Id",
                    models=[
                        ModelConfig(
                            id=m.model_id.split("/", 1)[1],
                            name=m.name,
                            input=list(m.input_modalities),
                            max_input_tokens=m.input_token_ceiling,
                            max_output_tokens=m.output_token_ceiling,
                            api_mode="responses"
                            if m.protocol == "responses"
                            else "chat_completions",
                        )
                    ],
                )
            )
        factory = CatalogFactory(providers)
        self._factories.append(factory)
        return factory

    async def open(
        self, *, path: str, payload: dict[str, Any], context: ModelContext, purpose: Purpose
    ) -> LocalModelResponse:
        if self.closed:
            raise OctopError.localized(ErrorCode.AUTH_FAILED)
        if self.runtime:
            row = await asyncio.get_running_loop().run_in_executor(
                None, self.runtime._storage().agents.get, context.agent_id
            )
            if row and row.resource_id:
                if context.resource_id and context.resource_id != row.resource_id:
                    raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)
                context = context.model_copy(update={"resource_id": row.resource_id})
        token = await self.access(expected_identity=self.identity)
        model_id = payload.get("model", "")
        model_id = model_id if model_id.startswith("platform/") else "platform/" + model_id
        model = next((m for m in await self.catalog() if m.model_id == model_id), None)
        if model is None or path != PATHS[model.protocol]:
            raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)
        headers = {"Authorization": "Bearer " + token}
        try:
            registered = await self.client.post(
                self.cloud_url + "/api/model-contexts", headers=headers, json=context.model_dump()
            )
            registered.raise_for_status()
            body = ModelRequest(
                **context.model_dump(),
                request_id=new_ulid(),
                model_id=model_id,
                purpose=purpose,
                payload={**payload, "model": model_id},
            )
            request = self.client.build_request(
                "POST",
                self.cloud_url + "/api/models/requests",
                headers=headers,
                json=body.model_dump(),
                timeout=180,
            )
            task = asyncio.current_task()
            if task:
                self._calls.add(task)
            try:
                response = await self.client.send(request, stream=True)
            finally:
                if task:
                    self._calls.discard(task)
            if response.status_code != 200:
                await response.aread()
                await response.aclose()
                try:
                    code = ErrorCode(response.json()["error"]["code"])
                except (ValueError, KeyError, TypeError):
                    code = ErrorCode.PLATFORM_MODEL_UNAVAILABLE
                raise OctopError.localized(code)
            return LocalModelResponse(response, self._calls)
        except httpx.HTTPError as exc:
            raise OctopError.localized(ErrorCode.CLOUD_UNAVAILABLE) from exc

    async def relay(
        self, path: str, payload: dict[str, Any], agent_id: str, thread_id: str
    ) -> LocalModelResponse:
        runtime = self.runtime
        if runtime is None or thread_id not in runtime.model_turns:
            raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)
        owner, turn_id = runtime.model_turns[thread_id]
        if owner != agent_id:
            raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)
        return await self.open(
            path=path,
            payload=payload,
            context=ModelContext(agent_id=agent_id, thread_id=thread_id, turn_id=turn_id),
            purpose="chat",
        )

    @staticmethod
    def error(exception: Exception) -> OctopError | None:
        """Preserve platform rejection codes carried by native SDK exceptions."""
        body = getattr(exception, "body", None)
        if not isinstance(body, dict):
            return None
        error = body.get("error", body)
        if not isinstance(error, dict):
            return None
        value = error.get("code") or error.get("type")
        if not isinstance(value, str):
            return None
        try:
            code = ErrorCode(value)
        except ValueError:
            return None
        return OctopError.localized(code)

    async def release_factory(self, factory: ChatModelFactory) -> None:
        if factory in self._factories:
            self._factories.remove(factory)
            await factory.aclose()

    async def close(self) -> None:
        self.closed = True
        current = asyncio.current_task()
        tasks = [t for t in self._calls if t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(f.aclose() for f in self._factories))
        self._calls.clear()
        self._factories.clear()


class CatalogFactory(PlatformFactory):
    """Public factory composition keeps logical refs across native protocols."""

    def __init__(self, providers: list[ProviderConfig]) -> None:
        super().__init__([])
        self._limits = {
            "platform/" + p.models[0].id: p.models[0].max_output_tokens for p in providers
        }
        self._delegates = {"platform/" + p.models[0].id: PlatformFactory([p]) for p in providers}

    def provider_configs(self) -> list[ProviderConfig]:
        # Config is descriptive. The actual protocol is resolved by each
        # standard factory; combine all logical models under the public prefix.
        snapshots = [p for f in self._delegates.values() for p in f.provider_configs()]
        if not snapshots:
            return []
        first = snapshots[0]
        return [
            ProviderConfig(
                id="platform",
                base_url=first.base_url,
                api_key=first.api_key,
                models=[p.models[0] for p in snapshots],
            )
        ]

    def list_refs(self, *, enabled_only: bool = True) -> list[str]:
        return list(self._delegates)

    def resolve_ref(self, spec: Any, **kwargs: Any) -> str:
        resolved = super().resolve_ref(spec, **kwargs)
        if resolved not in self._delegates:
            raise ValueError("model is not in the authorized platform catalog")
        return resolved

    def get_chat_model(self, model_ref: str, *, max_retries: int | None = None) -> BaseChatModel:
        if model_ref not in self._delegates:
            raise ValueError("model is not in the authorized platform catalog")
        model = self._delegates[model_ref].get_chat_model(model_ref, max_retries=0)
        # Public LangChain request setting: notably Anthropic otherwise sends
        # its SDK default rather than the authorized output limit.
        return model.model_copy(update={"max_tokens": self._limits[model_ref]})

    async def aclose(self) -> None:
        await asyncio.gather(*(f.aclose() for f in self._delegates.values()))
