"""HTTP presentation shared by the two independent factories."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from scalar_fastapi import get_scalar_api_reference

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.locale import resolve_request_locale


async def bounded_json_body(request: Request, code: ErrorCode) -> JSONResponse | None:
    """Bound JSON envelopes before parsing embedded model/skill file content."""
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > 8 * 1024 * 1024:
            return JSONResponse(
                OctopError.localized(code).to_envelope(locale=resolve_request_locale(request)),
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        chunks.append(chunk)
    request._body = b"".join(chunks)
    return None


def configure_http(app: FastAPI, *, api_docs: bool) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Validation errors must not reflect passwords, recovery codes or
        # renewable credentials back into responses and diagnostic clients.
        return JSONResponse(
            {
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                    for item in error.errors()
                ]
            },
            status_code=422,
        )

    @app.exception_handler(OctopError)
    async def domain_error(request: Request, error: OctopError) -> JSONResponse:
        return JSONResponse(
            error.to_envelope(locale=resolve_request_locale(request)), status_code=error.status
        )

    if api_docs:

        @app.get("/api/docs", include_in_schema=False)
        async def docs() -> HTMLResponse:
            return get_scalar_api_reference(openapi_url=app.openapi_url, title=app.title)


class ProductConfig(BaseModel):
    role: Literal["cloud", "local"]
    timezone: str


def configure_product(
    app: FastAPI, role: Literal["cloud", "local"], timezone: Callable[[], str]
) -> None:
    """Expose role/timezone and only the cloud management SPA's allowlisted routes."""
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    @app.get(
        "/api/application",
        response_model=ProductConfig,
        tags=["health"],
        summary="Get product role and platform timezone",
    )
    async def product() -> ProductConfig:
        return ProductConfig(role=role, timezone=timezone())

    @app.get(
        "/api/settings/timezone",
        response_model=dict[str, str],
        tags=["health"],
        summary="Get platform timezone",
    )
    async def server_timezone() -> dict[str, str]:
        return {"timezone": timezone()}

    if role != "cloud":
        return
    root = Path(__file__).resolve().parents[2] / "dashboard"
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")
    if (root / "favico.svg").is_file():

        async def favicon() -> FileResponse:
            return FileResponse(root / "favico.svg")

        app.add_api_route("/favico.svg", favicon, include_in_schema=False)
    if (root / "index.html").is_file():

        async def index() -> FileResponse:
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-store"})

        for path in (
            "/",
            "/login",
            "/users",
            "/devices",
            "/resources",
            "/models",
            "/usage",
            "/security",
        ):
            app.add_api_route(path, index, methods=["GET"], include_in_schema=False)
