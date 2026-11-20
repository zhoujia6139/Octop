"""Product launch command; no legacy server or setup configuration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from octop.cli.support.product_locale import cli_text


@click.command("run", help=cli_text("run"))
@click.option("--role", type=click.Choice(["cloud", "desktop"]), default="cloud", show_default=True)
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--host", default=None)
@click.option("--port", type=click.IntRange(0, 65535), default=None)
@click.option(
    "--log-level", type=click.Choice(["error", "warning", "info", "debug"]), default="info"
)
@click.option("--ssl-certfile", type=click.Path(exists=True), default=None)
@click.option("--ssl-keyfile", type=click.Path(exists=True), default=None)
def run(
    role: str,
    home: Path | None,
    host: str | None,
    port: int | None,
    log_level: str,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> None:
    """Start cloud management (default), or the authenticated desktop sidecar."""
    from octop.launch import run_application

    if bool(ssl_certfile) != bool(ssl_keyfile):
        raise click.UsageError(cli_text("tls_pair"))
    try:
        asyncio.run(
            run_application(
                "local" if role == "desktop" else "cloud",
                home=home,
                host=host,
                port=port,
                log_level=log_level,
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
            )
        )
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
