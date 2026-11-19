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
