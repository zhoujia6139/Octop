"""Independent launch configuration must preserve process and storage boundaries."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from octop import launch
from octop.application_config import CloudSettings, LocalSettings, application_home
from octop.cli.commands.application import cloud, local


@pytest.mark.parametrize("role", ["cloud", "local"])
async def test_launch_selects_new_factory_and_preserves_port_zero(
    tmp_path: Path, monkeypatch, role
):
    seen = []

    async def serve(server):
        assert server.config.port == 0
        app = server.config.app
        assert not hasattr(app.state, "octop_server")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client,
        ):
            result = (await client.get("/api/health")).json()
            assert result["role"] == role
            assert result["ok"]
            seen.append(result)

    monkeypatch.setattr(launch, "_serve", serve)
    await launch.run_application(role, home=tmp_path, port=0)
    assert len(seen) == 1
    assert not (tmp_path / "octop.db").exists()


@pytest.mark.parametrize("command,role", [(cloud, "cloud"), (local, "local")])
def test_dedicated_cli_dispatches_role_without_old_server(
    tmp_path: Path, monkeypatch, command, role
):
    seen = []

    async def run(selected, **kwargs):
        seen.append((selected, kwargs))

    monkeypatch.setattr(launch, "run_application", run)
    result = CliRunner().invoke(command, ["--home", str(tmp_path), "--port", "0"])
    assert result.exit_code == 0, result.output
    assert seen == [(role, {"home": tmp_path, "host": None, "port": 0})]


def test_application_config_is_isolated_and_local_has_no_supplier_fields(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    assert application_home("cloud") == tmp_path / "cloud"
    assert application_home("local") == tmp_path / "local"
    with pytest.raises(ValidationError):
        LocalSettings(home=tmp_path, bind_host="0.0.0.0")
    with pytest.raises(ValidationError):
        LocalSettings(home=tmp_path, api_key="supplier-key")
    with pytest.raises(ValidationError):
        LocalSettings(home=tmp_path, database_url="postgresql://host/db")
    with pytest.raises(ValidationError):
        CloudSettings(home=tmp_path, database_url="https://invalid.example")


def test_cloud_initializer_creates_only_first_admin(tmp_path, monkeypatch):
    from octop.cli.commands.application import cloud_init

    monkeypatch.delenv("OCTOP_CLOUD_DATABASE_URL", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cloud_init,
        ["--home", str(tmp_path), "--username", "Admin"],
        input="Bootstrap-good-5391\nBootstrap-good-5391\n",
    )
    assert result.exit_code == 0, result.output
    assert "Bootstrap-good-5391" not in result.output
    result = runner.invoke(
        cloud_init,
        ["--home", str(tmp_path), "--username", "second"],
        input="Bootstrap-good-5391\nBootstrap-good-5391\n",
    )
    assert result.exit_code != 0


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://user:password@example.test",
        "https://cloud.test/path",
        "https://cloud.test/?token=secret",
    ],
)
def test_local_cloud_origin_rejects_insecure_or_credential_urls(tmp_path, url):
    with pytest.raises(ValidationError):
        LocalSettings(home=tmp_path, cloud_url=url)


def test_cloud_initializer_does_not_boot_a_gateway_or_rewrite_existing_account(
    tmp_path, monkeypatch
):
    from octop.cli.commands.application import cloud_init
    from octop.infra.models.gateway import ModelGateway

    def forbidden(*args, **kwargs):
        raise AssertionError("account initialization must not create a model gateway")

    monkeypatch.setattr(ModelGateway, "__init__", forbidden)
    monkeypatch.delenv("OCTOP_CLOUD_DATABASE_URL", raising=False)
    runner = CliRunner()
    arguments = ["--home", str(tmp_path), "--username", "admin", "--if-empty"]
    assert (
        runner.invoke(
            cloud_init, arguments, input="Bootstrap-good-5391\nBootstrap-good-5391\n"
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cloud_init, arguments, input="Changed-but-ignored-7491\nChanged-but-ignored-7491\n"
        ).exit_code
        == 0
    )
