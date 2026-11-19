"""HTTP presentation shared by the two independent factories."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from scalar_fastapi import get_scalar_api_reference

from octop.infra.errors import OctopError
from octop.infra.utils.locale import resolve_request_locale


def configure_http(app: FastAPI, *, api_docs: bool) -> None:
    @app.exception_handler(OctopError)
    async def domain_error(request: Request, error: OctopError) -> JSONResponse:
        return JSONResponse(
            error.to_envelope(locale=resolve_request_locale(request)), status_code=error.status
        )

    if api_docs:

        @app.get("/api/docs", include_in_schema=False)
        async def docs() -> HTMLResponse:
            return get_scalar_api_reference(openapi_url=app.openapi_url, title=app.title)
