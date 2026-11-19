"""Cloud management must work independently of local Agent execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from octop.api.app import build_app
from octop.infra.server import OctopServer
from octop.infra.users.identity import Role


@pytest.fixture
async def cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, OctopServer]]:
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"enable_api_docs": True}), encoding="utf-8")
    server = OctopServer(tmp_path, role="cloud")
    await server.start()
    try:
        assert server.user_manager is not None
        await server.user_manager.create(username="owner", password="Secret123!", role=Role.USER)
        await server.user_manager.create(username="other", password="Secret123!", role=Role.USER)
        app = build_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client, server
    finally:
        await server.stop()


async def _login(client: httpx.AsyncClient, username: str = "owner") -> dict[str, str]:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": "Secret123!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_definition_crud_does_not_create_runtime_or_workspace(cloud):
    client, server = cloud
    headers = await _login(client)
    response = await client.post(
        "/api/agents",
        json={"name": "  Local assistant  ", "system_prompt": "Help with local files"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Local assistant"
    assert "last_state" not in body
    agent_id = body["agent_id"]
    assert server.app_runtime is None
    assert server.plugin_manager is None
    assert not server.paths.agents_dir.exists()

    listed = await client.get("/api/agents", headers=headers)
    assert [item["agent_id"] for item in listed.json()] == [agent_id]
    updated = await client.put(f"/api/agents/{agent_id}", json={"name": "Renamed"}, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["system_prompt"] is None
    deleted = await client.delete(f"/api/agents/{agent_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/agents/{agent_id}", headers=headers)).status_code == 404


async def test_cloud_ownership_authentication_and_input_boundary(cloud):
    client, _ = cloud
    assert (await client.get("/api/agents")).status_code == 401
    headers = await _login(client)
    created = await client.post("/api/agents", json={"name": "Private"}, headers=headers)
    agent_id = created.json()["agent_id"]
    other_headers = await _login(client, "other")
    assert (await client.get("/api/agents", headers=other_headers)).json() == []
    for method in ("GET", "PUT", "DELETE"):
        response = await client.request(
            method,
            f"/api/agents/{agent_id}",
            json={"name": "Taken"} if method == "PUT" else None,
            headers=other_headers,
        )
        assert response.status_code == 403, response.text
    for body in (
        {"name": " "},
        {"name": "Unsafe", "config": {"workspace_dir": "outside"}},
        {"name": "Unsafe", "api_key": "secret"},
        {"name": "Unsafe", "user_id": 123},
    ):
        assert (await client.post("/api/agents", json=body, headers=headers)).status_code == 422
    duplicate = await client.post("/api/agents", json={"name": "Private"}, headers=headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "AGENT_NAME_TAKEN"


async def test_cloud_openapi_and_health_exclude_execution(cloud):
    client, _ = cloud
    health = await client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["role"] == "cloud"
    assert health.json()["db"] is True
    assert "agents_running" not in health.json()
    schema = (await client.get("/api/openapi.json")).json()
    paths = schema["paths"]
    assert "/api/agents" in paths
    assert paths["/api/agents"]["post"]["summary"]
    assert paths["/api/agents"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
    for excluded in (
        "chat",
        "terminal",
        "workspace",
        "browser",
        "cron",
        "channels",
        "bridge",
        "device",
    ):
        assert not any(excluded in path.split("/") for path in paths)
    docs = await client.get("/api/docs")
    assert docs.status_code == 200
    assert "/api/openapi.json" in docs.text
    headers = await _login(client)
    for path in (
        "/api/agents/any/chat/ws",
        "/api/agents/any/start",
        "/api/cron/jobs",
        "/api/bridge/connections",
    ):
        assert (await client.get(path, headers=headers)).status_code == 404


async def test_cloud_survives_restart_and_stop_is_idempotent(cloud):
    client, server = cloud
    headers = await _login(client)
    created = await client.post("/api/agents", json={"name": "Persistent"}, headers=headers)
    agent_id = created.json()["agent_id"]
    await server.stop()
    await server.stop()
    assert not server.database_bound
    await server.start()
    assert server.database_bound
    # The existing JWT is still checked against the persistent secret and user.
    response = await client.get(f"/api/agents/{agent_id}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Persistent"
    assert server.app_runtime is None


def test_cloud_boot_login_and_management_without_execution_libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    script = """
import asyncio
import importlib.abc
import sys
from pathlib import Path
import os

class NoExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'octop_harness', 'octop_gateway', 'octop_browser', 'octop_memory'}:
            raise AssertionError(f'cloud imported execution library: {fullname}')

sys.meta_path.insert(0, NoExecution())
from octop.infra.server import OctopServer
from octop.api.app import build_app
from octop.infra.users.identity import Role
import httpx

async def main():
    server = OctopServer(Path(os.environ['OCTOP_HOME']), role='cloud')
    await server.start()
    try:
        await server.user_manager.create(username='admin', password='Secret123!', role=Role.ADMIN)
        app = build_app(server)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            response = await client.post('/api/auth/login', json={'username':'admin','password':'Secret123!'})
            assert response.status_code == 200, response.text
            headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
            profile = await client.get('/api/auth/me', headers=headers)
            assert profile.status_code == 200, profile.text
            response = await client.post('/api/agents', json={'name':'Local'}, headers=headers)
            assert response.status_code == 201, response.text
        assert server.app_runtime is None
    finally:
        await server.stop()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
