"""Composition root for the independent cloud and loopback applications."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

_WS_MAX_BYTES = 32 * 1024 * 1024


async def _serve(server: Any) -> None:
    await server.serve()


async def run_foreground(
    *,
    role: Literal["cloud", "desktop"] | None = None,
    host: str | None,
    port: int | None,
    reload: bool,
    workers: int,
    log_level: str | None,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> None:
    """Boot the domain server, serve the HTTP API, then shut down cleanly."""
    if reload or workers != 1:
        raise ValueError("independent applications use one worker without reload")
    await run_application(
        "local" if role == "desktop" else "cloud",
        host=host,
        port=port,
        log_level=log_level,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


def run_foreground_blocking(**kwargs: Any) -> None:
    """Sync entry for Click commands and tests."""
    asyncio.run(run_foreground(**kwargs))


async def run_application(
    role: Literal["cloud", "local"],
    *,
    home: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    log_level: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    """Run one independent factory; uvicorn owns its ASGI lifespan and cleanup."""
    import uvicorn

    from octop.application_config import CloudSettings, LocalSettings, application_home

    settings_type = CloudSettings if role == "cloud" else LocalSettings
    settings = await asyncio.get_running_loop().run_in_executor(
        None, settings_type.load, home or application_home(role)
    )
    updates = {
        key: value for key, value in {"bind_host": host, "port": port}.items() if value is not None
    }
    settings = settings_type.model_validate({**settings.model_dump(), **updates})
    if isinstance(settings, CloudSettings):
        from octop.api.applications.cloud import create_cloud_app

        app = create_cloud_app(settings)
    else:
        from octop.api.applications.local import create_local_app

        if ssl_certfile is not None or ssl_keyfile is not None:
            raise ValueError("the loopback application does not use TLS")
        app = create_local_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.bind_host,
            port=settings.port,
            log_level=log_level or "info",
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            ws_max_size=_WS_MAX_BYTES,
        )
    )
    await _serve(server)
