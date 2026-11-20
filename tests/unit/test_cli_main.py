"""P6 product entry points replace the legacy all-in-one CLI."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from click.testing import CliRunner

from octop.cli.main import cli


def test_help_is_lazy_and_exposes_only_product_commands():
    script = """
import json, sys
from click.testing import CliRunner
from octop.cli.main import cli
result = CliRunner().invoke(cli, ['--help'])
assert result.exit_code == 0, result.output
print(json.dumps({'help': result.output, 'modules': list(sys.modules)}))
"""
    response = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    result = json.loads(response.stdout)
    for name in ("cloud-init", "local-config", "health", "run"):
        assert name in result["help"]
    assert not any(name.startswith("octop.cli.commands.") for name in result["modules"])
    assert not any(
        name.startswith(("octop.infra.server", "octop_gateway", "octop_harness"))
        for name in result["modules"]
    )


@pytest.mark.parametrize(
    "name",
    ["agent", "chats", "channel", "cron", "provider", "bridge", "acp", "init", "service", "memory"],
)
def test_retired_commands_cannot_be_invoked(name):
    result = CliRunner().invoke(cli, [name, "--help"])
    assert result.exit_code == 2
    assert "No such command" in result.output


@pytest.mark.parametrize(
    "args,role", [([], "cloud"), (["--role", "cloud"], "cloud"), (["--role", "desktop"], "local")]
)
def test_run_selects_new_factory_and_keeps_port_zero(tmp_path, monkeypatch, args, role):
    from octop import launch

    seen = []

    async def run(selected, **kwargs):
        seen.append((selected, kwargs))

    monkeypatch.setattr(launch, "run_application", run)
    result = CliRunner().invoke(cli, ["run", *args, "--home", str(tmp_path), "--port", "0"])
    assert result.exit_code == 0, result.output
    assert seen[0][0] == role
    assert seen[0][1]["port"] == 0
    assert seen[0][1]["home"] == tmp_path
    assert not (tmp_path / "config.json").exists()


def test_local_config_validates_origin_without_persisting_credentials(tmp_path):
    runner = CliRunner()
    assert (
        runner.invoke(
            cli, ["local-config", "--home", str(tmp_path), "--cloud-url", "http://untrusted.test"]
        ).exit_code
        != 0
    )
    assert not (tmp_path / "config.json").exists()
    result = runner.invoke(
        cli, ["local-config", "--home", str(tmp_path), "--cloud-url", "https://cloud.test"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "config.json").read_text()) == {"cloud_url": "https://cloud.test"}
