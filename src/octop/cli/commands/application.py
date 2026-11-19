"""Dedicated launch commands for the independent applications."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import click


def _run(
    role: Literal["cloud", "local"], home: Path | None, host: str | None, port: int | None
) -> None:
    from octop.launch import run_application

    try:
        asyncio.run(run_application(role, home=home, host=host, port=port))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command()
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--host", default=None)
@click.option("--port", type=click.IntRange(0, 65535), default=None)
def cloud(home: Path | None, host: str | None, port: int | None) -> None:
    """Run the independent cloud application."""
    _run("cloud", home, host, port)


@click.command()
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--host", default=None)
@click.option("--port", type=click.IntRange(0, 65535), default=None)
def local(home: Path | None, host: str | None, port: int | None) -> None:
    """Run the independent local application on loopback."""
    _run("local", home, host, port)


@click.command()
@click.option("--home", type=click.Path(path_type=Path), default=None)
@click.option("--username", prompt=True)
@click.password_option()
def cloud_init(home: Path | None, username: str, password: str) -> None:
    """Initialize the first cloud administrator without a public setup endpoint."""
    from octop.application_config import CloudSettings, application_home
    from octop.infra.applications.cloud import CloudApplication
    from octop.infra.errors import OctopError
    from octop.infra.identity.contracts import LoginInput

    async def initialize() -> None:
        body = LoginInput.model_validate({"username": username, "password": password})
        application = CloudApplication(CloudSettings.load(home or application_home("cloud")))
        await application.start()
        try:
            assert application.identity is not None
            await asyncio.get_running_loop().run_in_executor(
                None,
                application.identity.bootstrap,
                body.username,
                body.password.get_secret_value(),
            )
        finally:
            await application.close()

    try:
        asyncio.run(initialize())
    except (ValueError, OSError, OctopError) as exc:
        raise click.ClickException(str(exc)) from exc
