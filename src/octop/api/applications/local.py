"""Loopback HTTP/WS directly backed by the independent local runtime."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, Literal

import httpx
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.background import BackgroundTask

from octop.api.applications.common import bounded_json_body, configure_http
from octop.api.applications.local_resources import router as resources_router
from octop.api.applications.local_sync import router as sync_router
from octop.application_config import LocalSettings
from octop.infra.applications.contracts import (
    Health,
    LocalAgentInput,
    LocalAgentView,
    PublicId,
    ThreadView,
)
from octop.infra.applications.local_accounts import LocalAccounts
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.contracts import IdentityView, TokenView
from octop.infra.models.contracts import (
    BudgetView,
    ModelContext,
    ModelInput,
    ModelView,
    Purpose,
    RequestView,
    UsageSummary,
)
from octop.infra.models.protocols import PATHS
from octop.infra.utils.locale import resolve_request_locale

if TYPE_CHECKING:
    from octop_harness.llm.factory import ChatModelFactory

logger = logging.getLogger(__name__)


class CloudConfig(BaseModel):
    cloud_url: str | None


class AuxiliaryModelInput(ModelInput):
    context: ModelContext
    purpose: Purpose
    model_id: str
    payload: dict[str, Any]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["approve", "reject"]
    message: str | None = None


class TurnCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["start", "resume", "cancel"]
    text: str | None = Field(default=None, max_length=100_000)
    decisions: list[ApprovalDecision] | None = None
    attachment_ids: list[PublicId] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def required_payload(self) -> TurnCommand:
        if self.type == "start" and (
            not self.text or not self.text.strip() or self.decisions is not None
        ):
            raise ValueError("start requires text only")
        if self.type == "resume" and (not self.decisions or self.text is not None):
            raise ValueError("resume requires approval decisions only")
        if self.type != "start" and self.attachment_ids:
            raise ValueError("attachments require a new turn")
        if self.type == "cancel" and (self.text is not None or self.decisions is not None):
            raise ValueError("cancel does not accept a payload")
        return self


def create_local_app(
    settings: LocalSettings,
    *,
    model_factory: ChatModelFactory | None = None,
    cloud_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    accounts = LocalAccounts(settings, model_factory=model_factory, cloud_transport=cloud_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await accounts.start()
        try:
            yield
        finally:
            await accounts.close()

    app = FastAPI(
        title="Octop Local API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
    )
    app.include_router(resources_router)
    app.include_router(sync_router)
    app.state.accounts = accounts
    app.state.local = accounts.runtime
    configure_http(app, api_docs=settings.enable_api_docs)

    def authenticated(value: str) -> bool:
        return hmac.compare_digest(
            value.encode(), f"Bearer {settings.api_token.get_secret_value()}".encode()
        )

    @app.middleware("http")
    async def authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/api/platform/"):
            runtime = accounts.runtime
            platform = runtime.model_platform if runtime else None
            token = request.headers.get("authorization", "")
            # Anthropic SDK authenticates via x-api-key rather than Bearer.
            incoming = request.headers.get("x-api-key") or token.removeprefix("Bearer ")
            if not platform or not hmac.compare_digest(
                incoming.encode(), platform.api_token.encode()
            ):
                return Response(status_code=401)
            return await call_next(request)
        if request.url.path != "/api/health" and not authenticated(
            request.headers.get("authorization", "")
        ):
            error = OctopError.localized(ErrorCode.AUTH_FAILED)
            return JSONResponse(
                error.to_envelope(locale=resolve_request_locale(request)), status_code=401
            )
        if request.url.path.startswith("/api/resources") and (
            rejected := await bounded_json_body(request, ErrorCode.ATTACHMENT_TOO_LARGE)
        ):
            return rejected
        return await call_next(request)

    @app.get(
        "/api/health", response_model=Health, tags=["health"], summary="Local runtime readiness"
    )
    async def health() -> Health:
        return Health(
            role="local",
            ok=accounts.started,
            schema_version=accounts.runtime.schema_version if accounts.runtime else 3,
        )

    @app.get(
        "/api/cloud/config",
        response_model=CloudConfig,
        tags=["auth"],
        summary="Get the configured cloud origin for the desktop main process",
    )
    async def cloud_config() -> CloudConfig:
        return CloudConfig(cloud_url=settings.cloud_url)

    @app.put(
        "/api/cloud/session",
        response_model=IdentityView,
        tags=["auth"],
        summary="Verify short-lived device access and select its local account",
    )
    async def attach_cloud(body: TokenView, request: Request) -> IdentityView:
        # ASGI server address is supplied by uvicorn, unlike the caller's Host.
        server = request.scope.get("server")
        if server and isinstance(server[1], int) and 0 < server[1] < 65536:
            host = "[::1]" if settings.bind_host == "::1" else "127.0.0.1"
            accounts.model_origin = f"http://{host}:{server[1]}"
        identity = await accounts.attach(body)
        app.state.local = accounts.runtime
        return identity

    @app.get(
        "/api/cloud/session",
        response_model=IdentityView | None,
        tags=["auth"],
        summary="Get the selected local account; does not grant cloud access",
    )
    async def cloud_session() -> IdentityView | None:
        return accounts.identity

    @app.delete(
        "/api/cloud/session",
        status_code=204,
        tags=["auth"],
        summary="Close the local account without deleting data or pending uploads",
    )
    async def detach_cloud() -> Response:
        await accounts.detach()
        app.state.local = None
        return Response(status_code=204)

    @app.get(
        "/api/models",
        response_model=list[ModelView],
        tags=["models"],
        summary="List models authorized for this desktop account",
    )
    async def models() -> list[ModelView]:
        platform = accounts.require_runtime().model_platform
        if not platform:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        return await platform.catalog()

    @app.get(
        "/api/usage/budgets",
        response_model=list[BudgetView],
        tags=["usage"],
        summary="Read this desktop account's authoritative cloud budgets",
    )
    async def usage_budgets() -> list[BudgetView]:
        platform = accounts.require_runtime().model_platform
        if not platform:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        return await platform.budgets()

    @app.get(
        "/api/usage/requests",
        response_model=list[RequestView],
        tags=["usage"],
        summary="Read this desktop account's model attempts and settlement states",
    )
    async def usage_requests(limit: int = Query(default=100, ge=1, le=500)) -> list[RequestView]:
        platform = accounts.require_runtime().model_platform
        if not platform:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        return await platform.requests(limit)

    @app.get(
        "/api/usage/summary",
        response_model=list[UsageSummary],
        tags=["usage"],
        summary="Read this desktop account's settled model costs",
    )
    async def usage_summary(
        since: int = Query(default=0, ge=0), until: int = Query(default=2**62, ge=1)
    ) -> list[UsageSummary]:
        platform = accounts.require_runtime().model_platform
        if not platform:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        return await platform.summary(since, until)

    async def native_response(result: Any) -> Response:
        media = result.response.headers.get("content-type", "application/json")
        return StreamingResponse(
            result.chunks(), media_type=media, background=BackgroundTask(result.close)
        )

    @app.post("/api/platform/chat/completions", include_in_schema=False)
    @app.post("/api/platform/responses", include_in_schema=False)
    @app.post("/api/platform/v1/messages", include_in_schema=False)
    async def platform_relay(request: Request) -> Response:
        platform = accounts.require_runtime().model_platform
        if not platform:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        path = request.url.path.removeprefix("/api/platform")
        if path == "/v1/messages":
            path = "/messages"
        result = await platform.relay(
            path,
            await request.json(),
            request.headers.get("x-octop-agent-id", ""),
            request.headers.get("x-octop-thread-id", ""),
        )
        return await native_response(result)

    @app.post(
        "/api/models/auxiliary",
        tags=["models"],
        summary="Call an auxiliary platform model in an owned local turn",
        description="Routes summary, title, memory, subagent and embedding calls through the same device authorization and ledger. Does not enable those product features.",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Native model JSON or SSE",
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "additionalProperties": True}
                    },
                    "text/event-stream": {"schema": {"type": "string"}},
                },
            }
        },
    )
    async def auxiliary(body: AuxiliaryModelInput) -> Response:
        runtime = accounts.require_runtime()
        platform = runtime.model_platform
        if not platform:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        await runtime.validate_model_context(
            body.context.agent_id, body.context.thread_id, body.context.turn_id
        )
        model = next((m for m in await platform.catalog() if m.model_id == body.model_id), None)
        if model is None:
            raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)
        result = await platform.open(
            path=PATHS[model.protocol],
            payload={**body.payload, "model": body.model_id},
            context=body.context,
            purpose=body.purpose,
        )
        return await native_response(result)

    @app.get(
        "/api/agents",
        response_model=list[LocalAgentView],
        tags=["agents"],
        summary="List local instances",
    )
    async def list_agents() -> list[LocalAgentView]:
        return await accounts.require_runtime().list_agents()

    @app.post(
        "/api/agents",
        response_model=LocalAgentView,
        status_code=201,
        tags=["agents"],
        summary="Create a local instance",
        description="Requires a trusted platform model adapter. Provider credentials and backend paths are not accepted.",
    )
    async def create_agent(body: LocalAgentInput) -> LocalAgentView:
        return await accounts.require_runtime().create_agent(body)

    @app.post(
        "/api/agents/{agent_id}/threads",
        response_model=ThreadView,
        status_code=201,
        tags=["chat"],
        summary="Create a local conversation",
    )
    async def create_thread(agent_id: PublicId) -> ThreadView:
        return await accounts.require_runtime().create_thread(agent_id)

    @app.websocket("/api/agents/{agent_id}/threads/{thread_id}/ws")
    async def chat(websocket: WebSocket, agent_id: PublicId, thread_id: PublicId) -> None:
        authorization = websocket.headers.get("authorization", "")
        if not authenticated(authorization):
            await websocket.close(code=1008)
            return
        origin = websocket.headers.get("origin")
        if origin and origin not in {"octop://app", f"http://{websocket.headers.get('host', '')}"}:
            await websocket.close(code=1008)
            return
        try:
            runtime = accounts.require_runtime()
        except OctopError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        send_lock = asyncio.Lock()
        active: asyncio.Task[None] | None = None

        async def send(value: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(value)

        async def run(command: TurnCommand) -> None:
            try:
                async with aclosing(
                    runtime.stream(
                        agent_id,
                        thread_id,
                        text=command.text,
                        attachment_ids=command.attachment_ids,
                        decisions=[item.model_dump(exclude_none=True) for item in command.decisions]
                        if command.decisions is not None
                        else None,
                    )
                ) as events:
                    async for event in events:
                        await send(event.model_dump(mode="json", fallback=str))
            except OctopError as error:
                await send({"type": "error", **error.to_envelope()})
            except WebSocketDisconnect:
                return
            except Exception:
                logger.exception("local turn failed")
                internal_error = OctopError.localized(ErrorCode.INTERNAL_ERROR)
                with suppress(WebSocketDisconnect, RuntimeError):
                    await send({"type": "error", **internal_error.to_envelope()})

        try:
            while True:
                try:
                    command = TurnCommand.model_validate(await websocket.receive_json())
                except (ValidationError, ValueError):
                    error = OctopError.localized(ErrorCode.SLASH_BAD_ARGS)
                    await send({"type": "error", **error.to_envelope()})
                    continue
                if command.type == "cancel":
                    if terminal := await runtime.cancel(thread_id):
                        await send(terminal.model_dump(mode="json"))
                elif active is not None and not active.done():
                    error = OctopError.localized(ErrorCode.THREAD_TURN_ACTIVE)
                    await send({"type": "error", **error.to_envelope()})
                else:
                    active = asyncio.create_task(run(command))
        except WebSocketDisconnect:
            pass
        finally:
            if active is not None and not active.done():
                active.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await active

    return app
