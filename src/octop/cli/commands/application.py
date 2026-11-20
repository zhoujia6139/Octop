"""Dedicated launch commands for the independent applications."""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Literal

import click

from octop.cli.support.product_locale import cli_locale, cli_text


def _run(
    role: Literal["cloud", "local"], home: Path | None, host: str | None, port: int | None
) -> None:
    from octop.launch import run_application

    try:
        asyncio.run(run_application(role, home=home, host=host, port=port))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command(help=cli_text("cloud"))
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--host", default=None)
@click.option("--port", type=click.IntRange(0, 65535), default=None)
def cloud(home: Path | None, host: str | None, port: int | None) -> None:
    """Run the independent cloud application."""
    _run("cloud", home, host, port)


@click.command(help=cli_text("local"))
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--host", default=None)
@click.option("--port", type=click.IntRange(0, 65535), default=None)
def local(home: Path | None, host: str | None, port: int | None) -> None:
    """Run the independent local application on loopback."""
    _run("local", home, host, port)


@click.command(help=cli_text("cloud_init"))
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--username", prompt=True, envvar="OCTOP_ADMIN_USERNAME")
@click.option("--if-empty", is_flag=True, help=cli_text("if_empty"))
@click.password_option(envvar="OCTOP_BOOTSTRAP_PASSWORD")
def cloud_init(home: Path | None, username: str, password: str, if_empty: bool) -> None:
    """Initialize the first cloud administrator without a public setup endpoint."""
    from octop.application_config import CloudSettings, application_home
    from octop.infra.applications.cloud import CloudApplication
    from octop.infra.errors import OctopError
    from octop.infra.identity.contracts import LoginInput

    async def initialize() -> None:
        body = LoginInput.model_validate({"username": username, "password": password})
        application = CloudApplication(CloudSettings.load(home or application_home("cloud")))
        await asyncio.get_running_loop().run_in_executor(
            None,
            partial(application.initialize_admin, if_empty=if_empty),
            body.username,
            body.password.get_secret_value(),
        )

    try:
        asyncio.run(initialize())
    except (ValueError, OSError, OctopError) as exc:
        message = (
            str(exc.to_envelope(locale=cli_locale())["error"]["message"])
            if isinstance(exc, OctopError)
            else str(exc)
        )
        raise click.ClickException(message) from exc


@click.command("local-config", help=cli_text("local_config"))
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--cloud-url", required=True)
def local_config(home: Path | None, cloud_url: str) -> None:
    """Set the trusted cloud origin before starting the desktop application."""
    from octop.application_config import LocalSettings, application_home
    from octop.infra.utils.json_file import read_json_object, write_json_atomic

    directory = home or application_home("local")
    path = directory / "config.json"
    try:
        data = read_json_object(path) or {}
        data["cloud_url"] = cloud_url
        LocalSettings.model_validate({**data, "home": directory})
        directory.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, data)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(str(path))


@click.command("health", help=cli_text("health"))
@click.option("--url", default="http://127.0.0.1:8088", show_default=True)
@click.option("--role", type=click.Choice(["cloud", "local"]), default="cloud")
def health(url: str, role: str) -> None:
    """Check readiness without requesting credentials or selecting an account."""
    import json

    import httpx

    from octop.infra.applications.contracts import Health

    try:
        response = httpx.get(url.rstrip("/") + "/api/health", timeout=5, follow_redirects=False)
        response.raise_for_status()
        status = Health.model_validate(response.json())
        if not status.ok or status.role != role:
            raise ValueError(cli_text("health_mismatch"))
    except (ValueError, httpx.HTTPError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(status.model_dump()))
