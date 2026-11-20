"""Lazy CLI registry integrity — every COMMANDS attr must resolve."""

from __future__ import annotations

import importlib

import click

from octop.cli.registry import COMMANDS


def test_all_registry_attrs_are_click_commands() -> None:
    for name, (module_path, attr, _help) in COMMANDS.items():
        mod = importlib.import_module(module_path, package="octop.cli")
        assert hasattr(mod, attr), f"{name}: module {module_path} missing attr {attr!r}"
        result = getattr(mod, attr)
        assert isinstance(result, click.Command), (
            f"{name}: {module_path}.{attr} is {type(result).__name__}, not click.Command"
        )
