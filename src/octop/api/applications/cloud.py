"""Cloud application factory, independent of the legacy FastAPI app."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from octop.api.applications.common import configure_http
from octop.application_config import CloudSettings
from octop.infra.applications.cloud import CloudApplication
from octop.infra.applications.contracts import Health


def create_cloud_app(settings: CloudSettings) -> FastAPI:
    services = CloudApplication(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await services.start()
        try:
            yield
        finally:
            await services.close()

    app = FastAPI(
        title="Octop Cloud API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
    )
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
