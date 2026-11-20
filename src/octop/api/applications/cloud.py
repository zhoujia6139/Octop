"""Cloud application factory, independent of the legacy FastAPI app."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from octop.api.applications.common import bounded_json_body, configure_http
from octop.api.applications.identity import router as identity_router
from octop.api.applications.models import router as models_router
from octop.api.applications.resources import router as resources_router
from octop.api.applications.sync import router as sync_router
from octop.application_config import CloudSettings
from octop.infra.applications.cloud import CloudApplication
from octop.infra.applications.contracts import Health
from octop.infra.errors import ErrorCode


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
                "name": "sync",
                "description": "Device-authorized logical backups, immutable revisions and resumable user-scoped content. No Agent execution.",
            },
            {
                "name": "resources",
                "description": "Immutable Agent/skill versions, current-device assignments, personal resources and installation receipts.",
            },
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
    app.include_router(resources_router)
    app.include_router(sync_router)

    @app.middleware("http")
    async def protect_credentials(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (
            request.url.path == "/api/models/requests"
            or request.url.path.startswith(("/api/resources/", "/api/resource-admin/"))
            or request.url.path in {"/api/sync/records", "/api/sync/blobs"}
        ):
            code = (
                ErrorCode.MODEL_REQUEST_TOO_LARGE
                if request.url.path == "/api/models/requests"
                else ErrorCode.ATTACHMENT_TOO_LARGE
            )
            if rejected := await bounded_json_body(request, code):
                return rejected
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
