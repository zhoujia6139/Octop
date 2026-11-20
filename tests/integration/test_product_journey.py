"""P7 production CLI → actual HTTP gateway → Harness → backup/restart acceptance."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from octop.infra.utils.ulid import new_ulid
from tests.integration import test_cloud_identity as identity
from tests.integration import test_platform_models as models
from tests.integration import test_platform_resources as resources
from tests.support.product_process import ProductProcess


async def checked(client, method, path, *, status=200, **kwargs):
    response = await client.request(method, path, **kwargs)
    assert response.status_code == status, (path, response.status_code, response.text)
    return response.json() if response.content else None


async def turn(local, agent, thread, text):
    url = str(local.base_url).rstrip("/").replace("http://", "ws://")
    async with (
        asyncio.timeout(30),
        websockets.connect(
            f"{url}/api/agents/{agent}/threads/{thread}/ws",
            additional_headers=identity.LOCAL_HEADERS,
            proxy=None,
        ) as ws,
    ):
        await ws.send(json.dumps({"type": "start", "text": text}))
        events = []
        while True:
            event = json.loads(await ws.recv())
            events.append(event)
            if event["type"] == "error" or event["type"] == "turn.status":
                return events


@pytest.mark.slow
async def test_product_process_journey_and_failure_boundaries(tmp_path):
    """Only the supplier is deterministic; both product processes use production wiring."""
    from octop.application_config import CloudSettings
    from octop.infra.applications.cloud import CloudApplication

    supplier = models.Supplier()
    supplier.mode = "tool"
    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def completion(request: Request):
        response = await supplier(
            httpx.Request("POST", str(request.url), content=await request.body())
        )

        async def body():
            async for chunk in response.aiter_bytes():
                yield chunk.replace(b"note.txt", b"inbound/input.txt")

        return StreamingResponse(
            body(), status_code=response.status_code, media_type=response.headers["content-type"]
        )

    cloud_home = tmp_path / "server" / "cloud"
    await asyncio.to_thread(
        CloudApplication(CloudSettings(home=cloud_home)).initialize_admin,
        "admin",
        identity.PASSWORD,
    )
    cloud = ProductProcess(cloud_home, "cloud")
    local_home = tmp_path / "device" / "local"
    local = ProductProcess(local_home, "local", env={"OCTOP_LOCAL_API_TOKEN": identity.LOCAL_TOKEN})
    # The CLI loads each application's config from OCTOP_HOME/{cloud,local}.
    local_home.mkdir(parents=True)
    (local_home / "config.json").write_text(json.dumps({"cloud_url": cloud.url}))
    try:
        async with models.serve(upstream) as supplier_url:
            await cloud.start()
            await local.start()
            async with (
                httpx.AsyncClient(base_url=cloud.url, trust_env=False, timeout=15) as admin,
                httpx.AsyncClient(base_url=cloud.url, trust_env=False, timeout=15) as account,
                httpx.AsyncClient(
                    base_url=local.url,
                    headers=identity.LOCAL_HEADERS,
                    trust_env=False,
                    timeout=30,
                ) as desktop,
            ):
                admin.headers.update(
                    identity.headers((await identity.login(admin))["session"]["access_token"])
                )
                user = await checked(
                    admin,
                    "POST",
                    "/api/users",
                    status=201,
                    json={"username": "alice", "password": identity.PASSWORD},
                )
                account.headers.update(
                    identity.headers(
                        (await identity.login(account, "alice"))["session"]["access_token"]
                    )
                )
                grant = await identity.bind(account, account.headers["Authorization"].split()[1])
                device_token = grant["session"]["access_token"]
                user_id = user["user_id"]
                await checked(
                    admin,
                    "PUT",
                    "/api/model-admin/providers",
                    json={
                        "provider_id": "supplier",
                        "base_url": supplier_url + "/v1",
                        "api_key": "acceptance-not-a-real-key",
                        "free_rejection_statuses": [429],
                    },
                )
                definition = models.definition()
                definition["input_token_ceiling"] = 128000
                await checked(admin, "PUT", "/api/model-admin/models", json=definition)
                await checked(
                    admin,
                    "PUT",
                    f"/api/model-admin/users/{user_id}/grants/test",
                    status=204,
                    json={"allowed": True},
                )
                await checked(
                    admin,
                    "PUT",
                    f"/api/model-admin/users/{user_id}/budget",
                    json={"currency": "USD", "limit_nanos": 10000000},
                )
                skill, resource = new_ulid(), new_ulid()
                admin_token = admin.headers["Authorization"].split()[1]
                await resources.save(admin, admin_token, skill, resources.skill_spec())
                await resources.publish(admin, admin_token, skill)
                await resources.save(admin, admin_token, resource, resources.agent_spec(skill))
                await resources.publish(admin, admin_token, resource)
                await resources.assign(admin, admin_token, user_id, resource)
                await checked(desktop, "PUT", "/api/cloud/session", json=grant["session"])
                installed = await resources.sync(desktop)
                agent = installed[resource]["agent_id"]
                assert installed[resource]["installed_revision"] == 1
                uploaded = await checked(
                    desktop,
                    "PUT",
                    f"/api/agents/{agent}/files/inbound/input.txt",
                    content=b"P7 local tool evidence",
                )
                thread = (
                    await checked(desktop, "POST", f"/api/agents/{agent}/threads", status=201)
                )["thread_id"]
                events = await turn(desktop, agent, thread, "read inbound/input.txt")
                assert events[-1]["data"]["status"] == "completed", events
                assert "P7 local tool evidence" in str(events)
                ledger = await checked(account, "GET", "/api/usage/requests")
                assert len(ledger) == len(supplier.calls) == 2
                assert all(
                    row["state"] == "settled"
                    and sum(attempt["cost_nanos"] for attempt in row["attempts"]) > 0
                    for row in ledger
                )
                synced = await checked(desktop, "POST", "/api/sync/retry")
                assert synced["pending"] == synced["conflicts"] == 0
                original = await checked(desktop, "GET", "/api/sync/records")
                assert {row["data"]["role"] for row in original if row["kind"] == "message"} == {
                    "user",
                    "assistant",
                    "tool",
                }
                # Preparation + a real process restart must not replay the completed tool/model.
                await checked(desktop, "POST", "/api/lifecycle/prepare", status=204)
                await local.stop()
                await local.start()
                await checked(desktop, "PUT", "/api/cloud/session", json=grant["session"])
                assert (
                    await checked(desktop, "GET", f"/api/agents/{agent}/threads/{thread}/state")
                )["status"] == "completed"
                assert len(supplier.calls) == 2
                assert (await checked(desktop, "GET", "/api/sync/records")) == original
                assert (
                    await desktop.get(f"/api/sync/records/{uploaded['item_id']}/content")
                ).content == b"P7 local tool evidence"
                # A provider rejection must not expose provider details or leave a reservation.
                supplier.mode = "reject"
                failed = await turn(desktop, agent, thread, "supplier is unavailable")
                assert failed[-1]["type"] == "error" or failed[-1]["data"]["status"] == "failed"
                assert "secret provider failure" not in str(failed)
                count = len(supplier.calls)
                budgets = await checked(account, "GET", "/api/usage/budgets")
                assert all(row["reserved_nanos"] == 0 for row in budgets)
                # Exhaustion is rejected before opening a supplier request.
                await checked(
                    admin,
                    "PUT",
                    f"/api/model-admin/users/{user_id}/budget",
                    json={"currency": "USD", "limit_nanos": budgets[0]["spent_nanos"]},
                )
                denied = await turn(desktop, agent, thread, "no remaining budget")
                assert denied[-1]["type"] == "error" or denied[-1]["data"]["status"] == "failed"
                assert len(supplier.calls) == count
                # A real cloud outage keeps local changes pending, then reconnect drains them.
                before_outage = await checked(account, "GET", "/api/usage/requests")
                await cloud.stop()
                await checked(
                    desktop,
                    "PUT",
                    f"/api/agents/{agent}/files/artifacts/offline.txt",
                    content=b"pending",
                )
                failure = await checked(desktop, "POST", "/api/sync/retry", status=503)
                assert failure["error"]["code"] == "CLOUD_UNAVAILABLE"
                offline = await checked(desktop, "GET", "/api/sync/status")
                assert offline["pending"] > 0
                await cloud.start()
                synced = await checked(desktop, "POST", "/api/sync/retry")
                assert synced["pending"] == 0
                # User/device credentials and definitions survive cloud restart.
                after_outage = await checked(account, "GET", "/api/usage/requests")
                assert {row["request_id"]: row for row in after_outage} == {
                    row["request_id"]: row for row in before_outage
                }
                device_id = grant["session"]["identity"]["device_id"]
                await checked(
                    admin, "DELETE", f"/api/users/{user_id}/devices/{device_id}", status=204
                )
                assert (await desktop.post("/api/resources/sync")).status_code in {401, 403}
                assert (
                    await account.get(
                        "/api/resources/changes", headers=identity.headers(device_token)
                    )
                ).status_code in {401, 403}
                assert len(supplier.calls) == count
    finally:
        await local.stop()
        await cloud.stop()
