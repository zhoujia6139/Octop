"""P6 product routes and lifecycle on actual independent cloud/local applications."""

from __future__ import annotations

import asyncio
import json

import httpx

from octop.api.applications.local import create_local_app
from octop.application_config import LocalSettings
from octop.infra.resources.local import durable
from tests.integration import test_cloud_identity as identity
from tests.integration import test_platform_resources as resources
from tests.integration import test_platform_sync as sync
from tests.integration.test_independent_applications import (
    HEADERS,
    _identity,
    _until_status,
)
from tests.integration.test_independent_applications import (
    local as local,
)
from tests.support.http import ws_connect

cloud = identity.cloud
platform_resources = resources.resources


async def test_cloud_management_spa_and_execution_route_boundary(cloud):
    app, client, _ = cloud
    assert (await client.get("/api/application")).json() == {
        "role": "cloud",
        "timezone": "Asia/Shanghai",
    }
    for path in ("/", "/login", "/users", "/resources", "/models", "/usage", "/security"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert 'id="root"' in response.text
    for path in (
        "/chat",
        "/files",
        "/terminal",
        "/control",
        "/setup",
        "/api/agents",
        "/api/chats",
        "/api/cron/jobs",
        "/api/browser",
        "/api/terminal",
        "/api/lifecycle/prepare",
    ):
        assert (await client.get(path)).status_code == 404, path
    schema = (await client.get("/api/openapi.json")).json()
    assert not any("chat" in path or "terminal" in path for path in schema["paths"])
    assert (await client.get("/api/docs")).status_code == 200


async def test_admin_device_inspection_and_revocation_are_account_authorized(cloud):
    _, client, admin = cloud
    created = await client.post(
        "/api/users",
        headers=identity.headers(admin),
        json={"username": "alice", "password": identity.PASSWORD},
    )
    user_id = created.json()["user_id"]
    account = (await identity.login(client, "alice"))["session"]["access_token"]
    grant = await identity.bind(client, account)
    device_id = grant["session"]["identity"]["device_id"]
    path = f"/api/users/{user_id}/devices"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=identity.headers(account))).status_code == 403
    assert (
        await client.get(path, headers=identity.headers(grant["session"]["access_token"]))
    ).status_code == 401
    assert (await client.get(path, headers=identity.headers(admin))).json()[0][
        "device_id"
    ] == device_id
    assert (
        await client.delete(path + "/" + device_id, headers=identity.headers(admin))
    ).status_code == 204
    refresh = await client.post(
        "/api/auth/device/refresh", json={"refresh_token": grant["refresh_token"]}
    )
    assert refresh.status_code == 403


async def test_first_start_origin_configuration_is_local_authenticated_and_durable(tmp_path):
    settings = LocalSettings(home=tmp_path, api_token="x" * 40)
    app = create_local_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        assert (
            await client.put("/api/cloud/config", json={"cloud_url": "https://cloud.test"})
        ).status_code == 401
        headers = {"Authorization": "Bearer " + "x" * 40}
        assert (
            await client.put(
                "/api/cloud/config", headers=headers, json={"cloud_url": "http://public.test"}
            )
        ).status_code == 400
        response = await client.put(
            "/api/cloud/config", headers=headers, json={"cloud_url": "https://cloud.test/"}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"cloud_url": "https://cloud.test"}
        assert (await client.get("/api/cloud/config", headers=headers)).json() == response.json()
    assert json.loads((tmp_path / "config.json").read_text()) == {"cloud_url": "https://cloud.test"}


async def test_pending_tool_is_readable_but_shutdown_never_executes_it(local):
    app, client, _ = local
    agent, thread = await _identity(client)
    runtime = app.state.local
    async with ws_connect(app, f"/api/agents/{agent}/threads/{thread}/ws", headers=HEADERS) as ws:
        await ws.send_json({"type": "start", "text": "write result.txt"})
        assert (await _until_status(ws))[-1]["data"]["status"] == "awaiting_approval"
        state = (
            await client.get(f"/api/agents/{agent}/threads/{thread}/state", headers=HEADERS)
        ).json()
        assert state["status"] == "awaiting_approval"
        assert state["approval"]["action_requests"][0]["name"] == "write_file"
        harness = await runtime._agent(agent)
        workspace = harness.workspace
        response = await client.post("/api/lifecycle/prepare", headers=HEADERS)
        assert response.status_code == 204, response.text
        assert not await workspace.aexists("result.txt")
    assert app.state.accounts.runtime is None
    from octop.infra.applications.local import LocalApplication

    restarted = LocalApplication(runtime.settings, model_factory=app.state.accounts._model_factory)
    await restarted.start()
    try:
        state = await restarted.thread_state(agent, thread)
        assert state.status == "interrupted" and state.approval is None
        events = [event async for event in restarted.stream(agent, thread, text="hello")]
        assert events[-1].data["status"] == "completed"
        assert not await (await restarted._agent(agent)).workspace.aexists("result.txt")
    finally:
        await restarted.close(close_model_factory=False)


async def test_inflight_turn_is_interrupted_before_local_detachment(local):
    app, client, factory = local
    agent, thread = await _identity(client)
    runtime = app.state.local
    async with ws_connect(app, f"/api/agents/{agent}/threads/{thread}/ws", headers=HEADERS) as ws:
        await ws.send_json({"type": "start", "text": "wait"})
        assert (await ws.receive_json())["type"] == "turn.started"
        await asyncio.wait_for(factory.model._started.wait(), 10)
        await runtime.quiesce()
        assert (await _until_status(ws))[-1]["data"]["status"] == "interrupted"
        assert (await runtime.thread_state(agent, thread)).status == "interrupted"
        await ws.send_json({"type": "start", "text": "hello"})
        assert (await ws.receive_json())["error"]["code"] == "LOCAL_RUNTIME_PAUSED"


async def test_failed_final_upload_keeps_durable_outbox_for_same_account(
    platform_resources, tmp_path
):
    app, client, admin, grant, _, user_id, _, resource = platform_resources
    await resources.assign(client, admin, user_id, resource)
    transport = sync.SyncTransport(app)
    async with sync.desktop(tmp_path, app, grant, transport=transport) as (
        local_app,
        local_client,
        runtime,
        _,
        _,
    ):
        agent = await sync.install(local_client, resource)
        thread = await runtime.create_thread(agent)
        transport.offline = True
        response = await local_client.post("/api/lifecycle/prepare")
        assert response.status_code == 204, response.text
        assert local_app.state.accounts.runtime is None
        transport.offline = False
        response = await local_client.put("/api/cloud/session", json=grant["session"])
        assert response.status_code == 200, response.text
        reopened = local_app.state.accounts.runtime
        await resources.stop_polling(reopened.resources)
        await resources.stop_polling(reopened.sync)
        stored = await durable(reopened.sync.repo.get, thread.thread_id)
        assert stored["state"] == "pending"
        await reopened.sync.sync()
        assert (await durable(reopened.sync.repo.get, thread.thread_id))["state"] == "synced"


async def test_force_killed_process_recovers_interrupted_history_without_replaying_tool(tmp_path):
    """Kill after a pending tool is committed, then boot the real ASGI process."""
    import os
    import socket
    import subprocess
    import sys
    from pathlib import Path

    import websockets
    from octop_harness.backends.workspace import BackendWorkspace

    script = tmp_path / "sidecar.py"
    script.write_text("""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import uvicorn
from octop.application_config import LocalSettings
from octop.api.applications.local import create_local_app
from tests.support.local_models import LocalModelFactory
uvicorn.run(create_local_app(LocalSettings(home=Path(sys.argv[1]),api_token=sys.argv[3]),model_factory=LocalModelFactory()),host='127.0.0.1',port=int(sys.argv[2]),log_level='error')
""")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    home = tmp_path / "runtime"
    token = HEADERS["Authorization"].removeprefix("Bearer ")
    log = (tmp_path / "sidecar.log").open("w")
    process = None

    async def start():
        child = subprocess.Popen(
            [sys.executable, str(script), str(home), str(port), token],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "OCTOP_HOME": str(tmp_path)},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        async with httpx.AsyncClient() as http:
            for _ in range(150):
                assert child.poll() is None, (tmp_path / "sidecar.log").read_text()
                try:
                    if (
                        await http.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
                    ).status_code == 200:
                        return child
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        child.kill()
        await asyncio.get_running_loop().run_in_executor(None, child.wait)
        raise AssertionError("sidecar did not become ready")

    async def until_terminal(ws):
        async with asyncio.timeout(15):
            while True:
                event = json.loads(await ws.recv())
                if event["type"] == "turn.status":
                    return event["data"]["status"]

    try:
        process = await start()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            agent, thread = await _identity(client)
            path = f"/api/agents/{agent}/threads/{thread}"
            async with websockets.connect(
                f"ws://127.0.0.1:{port}{path}/ws", additional_headers=HEADERS
            ) as ws:
                await ws.send(json.dumps({"type": "start", "text": "write result.txt"}))
                assert await until_terminal(ws) == "awaiting_approval"
                process.kill()
                await asyncio.get_running_loop().run_in_executor(None, process.wait)
            process = await start()
            state = (await client.get(path + "/state", headers=HEADERS)).json()
            assert state["status"] == "interrupted" and state["approval"] is None
            workspace = BackendWorkspace.for_local_directory(home / "workspaces" / agent)
            assert not await workspace.aexists("result.txt")
            async with websockets.connect(
                f"ws://127.0.0.1:{port}{path}/ws", additional_headers=HEADERS
            ) as ws:
                await ws.send(json.dumps({"type": "start", "text": "hello"}))
                assert await until_terminal(ws) == "completed"
            assert not await workspace.aexists("result.txt")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: process.wait(timeout=10)
                )
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.get_running_loop().run_in_executor(None, process.wait)
        log.close()


async def test_cloud_bootstrap_if_empty_does_not_change_existing_password(cloud):
    app, client, _ = cloud
    from octop.infra.applications.cloud import CloudApplication

    application = CloudApplication(app.state.cloud.settings)
    await asyncio.to_thread(
        application.initialize_admin, "ignored", "Other-strong-7481", if_empty=True
    )
    assert (await identity.login(client, "admin"))["session"]
    bad = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "Other-strong-7481"}
    )
    assert bad.status_code == 401


async def test_logout_queued_behind_prepare_does_not_deadlock_final_authorization(
    local, monkeypatch
):
    app, _, _ = local
    accounts = app.state.accounts
    entered = asyncio.Event()

    async def prepare():
        async with accounts._lock:
            entered.set()

    monkeypatch.setattr(accounts.runtime, "prepare_shutdown", prepare)
    await accounts._lock.acquire()
    try:
        preparing = asyncio.create_task(accounts.prepare_shutdown())
        await asyncio.sleep(0)
        detaching = asyncio.create_task(accounts.detach())
        await asyncio.sleep(0)
    finally:
        accounts._lock.release()
    await asyncio.wait_for(asyncio.gather(preparing, detaching), 10)
    assert entered.is_set()
    assert accounts.runtime is None


async def test_renderer_disconnect_before_sleep_preparation_is_interrupted(local):
    app, client, factory = local
    agent, thread = await _identity(client)
    runtime = app.state.local
    async with ws_connect(app, f"/api/agents/{agent}/threads/{thread}/ws", headers=HEADERS) as ws:
        await ws.send_json({"type": "start", "text": "wait"})
        assert (await ws.receive_json())["type"] == "turn.started"
        await asyncio.wait_for(factory.model._started.wait(), 10)
    state = await runtime.thread_state(agent, thread)
    assert state.status == "interrupted"
    assert not runtime._active
