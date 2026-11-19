"""Cloud application factory, independent of the legacy FastAPI app."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from octop.api.applications.common import configure_http
from octop.api.applications.identity import router as identity_router
from octop.api.applications.models import router as models_router
from octop.application_config import CloudSettings
from octop.infra.applications.cloud import CloudApplication
from octop.infra.applications.contracts import Health
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.locale import resolve_request_locale


def create_cloud_app(
    settings: CloudSettings, *, provider_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    services = CloudApplication(settings, provider_transport=provider_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await services.start()
        try:
            yield
        finally:
            await services.close()

    app = FastAPI(
        title="Octop Cloud API",
        description="Cloud accounts and single-device authorization. Initialize the first administrator with octop-cloud-init; use password and optional TOTP MFA to obtain a short-lived account session.",
        openapi_tags=[
            {
                "name": "auth",
                "description": "Password/TOTP accounts, purpose-scoped sessions and single-device authorization.",
            },
            {
                "name": "users",
                "description": "Administrator account management and live authorization revocation.",
            },
            {
                "name": "models",
                "description": "Logical models, native protocols, encrypted routing and device-authorized requests.",
            },
            {
                "name": "usage",
                "description": "Integer-nano budgets, durable attempts, settlement and administrator reconciliation.",
            },
            {"name": "health", "description": "Cloud readiness and independent schema version."},
        ],
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
    )
    app.include_router(identity_router)
    app.include_router(models_router)

    @app.middleware("http")
    async def protect_credentials(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == "/api/models/requests":
            # Read incrementally before Pydantic JSON parsing to bound model
            # input memory, including base64 multimodal payloads.
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    return JSONResponse(
                        OctopError.localized(ErrorCode.MODEL_REQUEST_TOO_LARGE).to_envelope(
                            locale=resolve_request_locale(request)
                        ),
                        status_code=413,
                        headers={"Cache-Control": "no-store"},
                    )
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.state.cloud = services
    configure_http(app, api_docs=settings.enable_api_docs)

    @app.get(
        "/api/health", response_model=Health, tags=["health"], summary="Cloud application readiness"
    )
    async def health() -> Health:
        return Health(
            role="cloud", ok=services.db is not None, schema_version=services.schema_version
        )

    return app
