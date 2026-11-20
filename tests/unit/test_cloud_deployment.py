"""The default cloud image cannot gain execution extras through packaging changes."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_product_dependency_sets_and_container_select_only_their_role():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    extras = project["project"]["optional-dependencies"]
    assert set(extras) == {"cloud", "local-runtime", "dev"}
    assert all(
        "octop-gateway" not in entry and "apscheduler" not in entry
        for entry in extras["local-runtime"]
    )
    docker = (ROOT / "deploy/docker/Dockerfile").read_text()
    assert docker.count("uv sync --frozen --no-dev --extra cloud") == 2
    assert "bake-desktop.sh" not in docker
    assert "COPY --from=frontend-builder /usr/local/bin/node" not in docker
    compose = yaml.safe_load((ROOT / "deploy/docker/docker-compose.yml").read_text())
    service = compose["services"]["octop"]
    assert "OCTOP_CLOUD_DATABASE_URL" in service["environment"]
    assert not any("API_KEY" in key or key.startswith("LANGFUSE") for key in service["environment"])
    assert service["ulimits"]["nofile"]["soft"] >= 65535
    assert "shm_size" not in service


def test_retired_deployments_are_not_executable_old_server_products():
    for path in ("docker-compose.cluster.yml", "docker-compose.mobile.yml"):
        assert not (ROOT / "deploy/docker" / path).exists()
    for path in (
        "fnos/native/app/bin/octop",
        "fnos/native/app/bin/octop-cli",
        "fnos/docker/cmd/main",
    ):
        script = (ROOT / path).read_text()
        assert "retired" in script
        assert "exit 1" in script
        assert "octop init" not in script
