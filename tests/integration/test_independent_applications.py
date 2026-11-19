"""P1 factory, schema, real-Harness tool loop, approval and cancellation."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from octop.api.applications.cloud import create_cloud_app
from octop.api.applications.local import create_local_app
from octop.application_config import CloudSettings, LocalSettings
from tests.support.http import ws_connect
from tests.support.local_models import LocalModelFactory

TOKEN = "local-session-test-credential-123456789"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def _identity(client: httpx.AsyncClient) -> tuple[str, str]:
    response = await client.post(
        "/api/agents", json={"name": "Reader", "model_id": "platform/test"}, headers=HEADERS
    )
    assert response.status_code == 201, response.text
    agent_id = response.json()["agent_id"]
    response = await client.post(f"/api/agents/{agent_id}/threads", headers=HEADERS)
    assert response.status_code == 201, response.text
    return agent_id, response.json()["thread_id"]


async def _until_status(ws) -> list[dict]:
    result = []
    while True:
        item = await ws.receive_json()
        result.append(item)
        assert item["type"] != "error", item
        if item["type"] == "turn.status":
            return result


async def test_cloud_initializes_only_its_own_schema(tmp_path: Path):
    settings = CloudSettings(home=tmp_path, enable_api_docs=True)
    for _ in range(2):
        app = create_cloud_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                assert (await client.get("/api/health")).json() == {
                    "protocol_version": 1,
                    "role": "cloud",
                    "ok": True,
                    "schema_version": 2,
                }
                paths = (await client.get("/api/openapi.json")).json()["paths"]
                assert "/api/auth/login" in paths
                assert not any("/chat" in path or "/agents" in path for path in paths)
                assert (await client.get("/api/agents")).status_code == 404
                assert (await client.get("/api/docs")).status_code == 200
            with app.state.cloud.db.connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                assert tables == {
                    "cloud_settings",
                    "cloud_schema_version",
                    "cloud_users",
                    "cloud_devices",
                    "cloud_auth_sessions",
                    "cloud_mfa",
                    "cloud_recovery_codes",
                    "cloud_login_attempts",
                    "secrets",
                    "sqlite_sequence",
                }
        assert app.state.cloud.db is None
    assert not (tmp_path / "octop.db").exists()


@pytest.fixture
async def local(tmp_path: Path):
    factory = LocalModelFactory()
    settings = LocalSettings(home=tmp_path, api_token=SecretStr(TOKEN), enable_api_docs=True)
    app = create_local_app(settings, model_factory=factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield app, client, factory


async def test_local_ws_runs_real_harness_tool_loop(local):
    app, client, _ = local
    agent_id, thread_id = await _identity(client)
    agent = await app.state.local._agent(agent_id)
    await agent.workspace.aupload_bytes("note.txt", b"local-content")
    path = f"/api/agents/{agent_id}/threads/{thread_id}/ws"
    async with ws_connect(app, path, headers=HEADERS) as ws:
        await ws.send_json({"type": "start", "text": "read note.txt"})
        events = await _until_status(ws)
    assert events[-1]["data"]["status"] == "completed"
    assert any(event["type"] == "tool_result" for event in events)
    assert "local-content" in str(events)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    with app.state.local.services.db.connect() as conn:
        assert conn.execute("SELECT status FROM local_turns").fetchone()["status"] == "completed"


async def test_local_approval_resumes_same_turn_before_file_side_effect(local):
    app, client, _ = local
    agent_id, thread_id = await _identity(client)
    path = f"/api/agents/{agent_id}/threads/{thread_id}/ws"
    async with ws_connect(app, path, headers=HEADERS) as ws:
        await ws.send_json({"type": "start", "text": "write result.txt"})
        paused = await _until_status(ws)
        assert paused[-1]["data"]["status"] == "awaiting_approval"
        agent = await app.state.local._agent(agent_id)
        assert not await agent.workspace.aexists("result.txt")
        await ws.send_json({"type": "resume", "decisions": [{"type": "approve"}]})
        resumed = await _until_status(ws)
        assert resumed[-1]["data"]["status"] == "completed"
        assert resumed[0]["turn_id"] == paused[0]["turn_id"]
        assert resumed[0]["sequence"] > paused[-1]["sequence"]
        assert await agent.workspace.adownload_bytes("result.txt") == b"saved-by-tool"


async def test_local_ws_cancel_persists_terminal_state(local):
    app, client, factory = local
    agent_id, thread_id = await _identity(client)
    path = f"/api/agents/{agent_id}/threads/{thread_id}/ws"
    async with ws_connect(app, path, headers=HEADERS) as ws:
        await ws.send_json({"type": "start", "text": "wait"})
        assert (await ws.receive_json())["type"] == "turn.started"
        await asyncio.wait_for(factory.model._started.wait(), 10)
        await ws.send_json({"type": "cancel"})
        events = await _until_status(ws)
        assert events[-1]["data"]["status"] == "cancelled"
    assert not app.state.local._active


async def test_cancelled_approval_does_not_execute_on_a_later_turn(local):
    app, client, _ = local
    agent_id, thread_id = await _identity(client)
    path = f"/api/agents/{agent_id}/threads/{thread_id}/ws"
    async with ws_connect(app, path, headers=HEADERS) as ws:
        await ws.send_json({"type": "start", "text": "write result.txt"})
        assert (await _until_status(ws))[-1]["data"]["status"] == "awaiting_approval"
        await ws.send_json({"type": "cancel"})
        assert (await _until_status(ws))[-1]["data"]["status"] == "cancelled"
        agent = await app.state.local._agent(agent_id)
        await agent.workspace.aupload_bytes("note.txt", b"new-request")
        await ws.send_json({"type": "start", "text": "read note.txt"})
        assert (await _until_status(ws))[-1]["data"]["status"] == "completed"
        assert not await agent.workspace.aexists("result.txt")


async def test_local_authentication_and_production_has_no_direct_provider(tmp_path: Path):
    app = create_local_app(LocalSettings(home=tmp_path, api_token=SecretStr(TOKEN)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/api/agents")).status_code == 401
            response = await client.post(
                "/api/agents", json={"name": "Local", "model_id": "platform/test"}, headers=HEADERS
            )
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "AUTH_FAILED"
            response = await client.post(
                "/api/agents",
                json={"name": "Local", "model_id": "x", "api_key": "key", "backend": "filesystem"},
                headers=HEADERS,
            )
            assert response.status_code == 422
        with pytest.raises(WebSocketDisconnect):
            async with ws_connect(app, "/api/agents/AAA/threads/BBB/ws"):
                pass


async def test_local_recovers_interrupted_turn_without_replaying_it(local):
    app, client, _ = local
    agent_id, thread_id = await _identity(client)
    services = app.state.local.services
    services.turns.create("old-turn", thread_id)
    await app.state.local.close()
    await app.state.local.start()
    with app.state.local.services.db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM local_turns WHERE turn_id='old-turn'").fetchone()[
                "status"
            ]
            == "interrupted"
        )
    assert not app.state.local._agents
    assert not app.state.local._active


@pytest.mark.parametrize("role", ["cloud", "local"])
def test_new_factories_do_not_import_old_or_unneeded_services(tmp_path: Path, role: str):
    script = """
import asyncio, importlib.abc, sys
from pathlib import Path
role, directory = sys.argv[1:]
blocked = ('octop.infra.server', 'octop.infra.agents.manager', 'octop.infra.gateway',
           'octop.infra.cron', 'octop.infra.bridge', 'octop.infra.db.services', 'octop.api.app', 'octop.api.deps', 'octop_gateway')
if role == 'cloud':
    blocked += ('octop_harness', 'octop_browser', 'octop_memory')
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == prefix or name.startswith(prefix + '.') for prefix in blocked):
            raise AssertionError('forbidden dependency: ' + name)
sys.meta_path.insert(0, Boundary())
from octop.application_config import CloudSettings, LocalSettings
if role == 'cloud':
    from octop.api.applications.cloud import create_cloud_app
    app = create_cloud_app(CloudSettings(home=Path(directory)))
else:
    from octop.api.applications.local import create_local_app
    app = create_local_app(LocalSettings(home=Path(directory)))
async def main():
    async with app.router.lifespan_context(app):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            assert (await client.get('/api/health')).json()['ok']
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, role, str(tmp_path)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
