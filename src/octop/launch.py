"""Composition root — wire OctopServer, FastAPI, and uvicorn for ``octop run``."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from octop.infra.desktop import desktop_shell_enabled
from octop.infra.utils.accept_errors import coalesce_accept_errors

logger = logging.getLogger(__name__)

# uvicorn's default ping_timeout is 20s. A busy peer — demo.octop.chat runs
# dozens of agents on one event loop — often reads the pong later than that
# and then closes the socket with 1011 "keepalive ping timeout". The dialer
# redials at once, so cloud collab looks like it is flapping. Keep sending
# pings; do not drop the connection on a late pong. BridgeSession's
# application heartbeat still detects a half-open link.
_WS_PING_INTERVAL = 20.0
_WS_PING_TIMEOUT: float | None = None
_WS_MAX_BYTES = 32 * 1024 * 1024


def _ensure_linux_bubblewrap() -> None:
    """Best-effort startup provisioning; never fail the ``octop run`` process."""
    try:
        from octop.infra.utils.bwrap import ensure_bubblewrap

        result = ensure_bubblewrap()
    except Exception:
        logger.warning("background bubblewrap provisioning failed", exc_info=True)
        return
    status = result.get("status")
    detail = result.get("detail")
    if status == "degraded":
        logger.warning("background bubblewrap provisioning degraded: %s", detail or "unknown")
    else:
        logger.info("background bubblewrap provisioning status=%s", status)


def _schedule_linux_bubblewrap_ensure() -> asyncio.Task[None] | None:
    """Run bubblewrap provisioning once in the background on Linux."""
    if not sys.platform.startswith("linux"):
        return None
    return asyncio.create_task(
        asyncio.to_thread(_ensure_linux_bubblewrap),
        name="ensure-linux-bubblewrap",
    )


async def _cancel_background_task(task: asyncio.Task[None] | None) -> None:
    """Drop a startup background task on shutdown; never raise."""
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


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
    if role is not None:
        if reload or workers != 1:
            raise ValueError("independent applications currently use one worker without reload")
        await run_application(
            "cloud" if role == "cloud" else "local",
            host=host,
            port=port,
            log_level=log_level,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
        return
    import uvicorn

    from octop.api.app import build_app
    from octop.infra.server import OctopServer
    from octop.infra.setup.tls.http_companion import build_http_companion_app
    from octop.infra.setup.tls.listeners import build_listen_plan
    from octop.infra.setup.tls.store import resolve_tls_paths

    # Default product entry switches in P6 after the UI has its new API client.
    selected_role = "desktop"
    srv = OctopServer()
    await srv.start()
    bwrap_task = _schedule_linux_bubblewrap_ensure() if selected_role == "desktop" else None
    # Greenfield deferral leaves services unset until /setup/database binds.
    cfg = srv.services.config if srv.services is not None else srv.config
    assert cfg is not None
    bind_host = host or cfg.bind_host
    bind_port = port or cfg.port
    if desktop_shell_enabled():
        # Desktop-hosted sidecar always serves one loopback HTTP listener; it
        # must ignore any persisted TLS / dual-port configuration.
        ssl_certfile = None
        ssl_keyfile = None
    elif ssl_certfile is None and ssl_keyfile is None:
        ssl_certfile, ssl_keyfile = resolve_tls_paths(srv.paths.root, cfg.tls)

    plan = build_listen_plan(
        cfg,
        bind_host=bind_host,
        bind_port=bind_port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    level = (log_level or cfg.log_level).lower()
    worker_count = 1 if plan.dual_listeners or reload else workers
    if plan.dual_listeners and (reload or workers > 1):
        logger.warning(
            "TLS dual-port mode forces workers=1 and disables reload",
        )
        reload = False

    app = build_app(srv)
    servers: list[uvicorn.Server] = []

    if plan.dual_listeners:
        assert plan.https_port is not None
        assert plan.http_port is not None
        https_config = uvicorn.Config(
            app,
            host=plan.bind_host,
            port=plan.https_port,
            log_level=level,
            workers=worker_count,
            reload=False,
            ssl_certfile=plan.ssl_certfile,
            ssl_keyfile=plan.ssl_keyfile,
            ws_max_size=_WS_MAX_BYTES,
            ws_ping_interval=_WS_PING_INTERVAL,
            ws_ping_timeout=_WS_PING_TIMEOUT,
        )
        companion = build_http_companion_app(https_port=plan.https_port)
        http_config = uvicorn.Config(
            companion,
            host=plan.bind_host,
            port=plan.http_port,
            log_level=level,
            workers=1,
            reload=False,
        )
        servers.append(uvicorn.Server(https_config))
        servers.append(uvicorn.Server(http_config))
        logger.info(
            "Listening on https://%s:%s and http://%s:%s (ACME + redirect)",
            plan.bind_host,
            plan.https_port,
            plan.bind_host,
            plan.http_port,
        )
    else:
        single_config = uvicorn.Config(
            app,
            host=plan.bind_host,
            port=plan.http_port or bind_port,
            log_level=level,
            workers=worker_count,
            reload=reload,
            ssl_certfile=plan.ssl_certfile,
            ssl_keyfile=plan.ssl_keyfile,
            ws_max_size=_WS_MAX_BYTES,
            ws_ping_interval=_WS_PING_INTERVAL,
            ws_ping_timeout=_WS_PING_TIMEOUT,
        )
        servers.append(uvicorn.Server(single_config))

    memory_control = None
    if selected_role == "desktop":
        from octop.infra.agents.memory.slim_control import MemorySlimControl

        memory_control = MemorySlimControl(srv)
    with coalesce_accept_errors(asyncio.get_running_loop()):
        try:
            if memory_control is not None:
                await memory_control.start()
            await asyncio.gather(*(_serve(s) for s in servers))
        finally:
            if memory_control is not None:
                await memory_control.close()
            await _cancel_background_task(bwrap_task)
            await srv.stop()


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
