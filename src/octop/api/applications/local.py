"""Loopback HTTP/WS directly backed by the independent local runtime."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from octop.api.applications.common import configure_http
from octop.application_config import LocalSettings
from octop.infra.applications.contracts import (
    Health,
    LocalAgentInput,
    LocalAgentView,
    PublicId,
    ThreadView,
)
from octop.infra.applications.local import LocalApplication
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.locale import resolve_request_locale

if TYPE_CHECKING:
    from octop_harness.llm.factory import ChatModelFactory

logger = logging.getLogger(__name__)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["approve", "reject"]
    message: str | None = None


class TurnCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["start", "resume", "cancel"]
    text: str | None = Field(default=None, max_length=100_000)
    decisions: list[ApprovalDecision] | None = None

    @model_validator(mode="after")
    def required_payload(self) -> TurnCommand:
        if self.type == "start" and (
            not self.text or not self.text.strip() or self.decisions is not None
        ):
            raise ValueError("start requires text only")
        if self.type == "resume" and (not self.decisions or self.text is not None):
            raise ValueError("resume requires approval decisions only")
        if self.type == "cancel" and (self.text is not None or self.decisions is not None):
            raise ValueError("cancel does not accept a payload")
        return self


def create_local_app(
    settings: LocalSettings, *, model_factory: ChatModelFactory | None = None
) -> FastAPI:
    runtime = LocalApplication(settings, model_factory=model_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title="Octop Local API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
    )
    app.state.local = runtime
    configure_http(app, api_docs=settings.enable_api_docs)

    def authenticated(value: str) -> bool:
        return hmac.compare_digest(
            value.encode(), f"Bearer {settings.api_token.get_secret_value()}".encode()
        )

    @app.middleware("http")
    async def authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path != "/api/health" and not authenticated(
            request.headers.get("authorization", "")
        ):
            error = OctopError.localized(ErrorCode.AUTH_FAILED)
            return JSONResponse(
                error.to_envelope(locale=resolve_request_locale(request)), status_code=401
            )
        return await call_next(request)

    @app.get(
        "/api/health", response_model=Health, tags=["health"], summary="Local runtime readiness"
    )
    async def health() -> Health:
        return Health(
            role="local", ok=runtime.services is not None, schema_version=runtime.schema_version
        )

    @app.get(
        "/api/agents",
        response_model=list[LocalAgentView],
        tags=["agents"],
        summary="List local instances",
    )
    async def list_agents() -> list[LocalAgentView]:
        return await runtime.list_agents()

    @app.post(
        "/api/agents",
        response_model=LocalAgentView,
        status_code=201,
        tags=["agents"],
        summary="Create a local instance",
        description="Requires a trusted platform model adapter. Provider credentials and backend paths are not accepted.",
    )
    async def create_agent(body: LocalAgentInput) -> LocalAgentView:
        return await runtime.create_agent(body)

    @app.post(
        "/api/agents/{agent_id}/threads",
        response_model=ThreadView,
        status_code=201,
        tags=["chat"],
        summary="Create a local conversation",
    )
    async def create_thread(agent_id: PublicId) -> ThreadView:
        return await runtime.create_thread(agent_id)

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
