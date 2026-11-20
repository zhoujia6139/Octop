"""P5 durable backups: actual cloud/local applications and public runtime libraries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager

import httpx
import pytest

from octop.infra.resources.local import durable
from octop.infra.sync.contracts import CHUNK_BYTES
from octop.infra.utils.ulid import new_ulid
from tests.integration import test_cloud_identity as identity
from tests.integration import test_platform_resources as resource_tests

cloud = identity.cloud
resources = resource_tests.resources
headers = identity.headers


class SyncTransport(resource_tests.FaultTransport):
    def __init__(self, app):
        super().__init__(app)
        self.lose_record = False
        self.damage_chunk = False
        self.seen_operations = []

    async def handle_async_request(self, request):
        if request.url.path == "/api/sync/records":
            self.seen_operations.append(json.loads(request.content)["operation_id"])
        response = await super().handle_async_request(request)
        if self.lose_record and request.url.path == "/api/sync/records":
            self.lose_record = False
            await response.aclose()
            raise httpx.ReadError("lost after backup commit", request=request)
        if self.damage_chunk and request.method == "GET" and "/chunks/" in request.url.path:
            content = await response.aread()
            await response.aclose()
            return httpx.Response(200, content=b"x" * len(content), request=request)
        return response


@asynccontextmanager
async def desktop(tmp_path, app, grant, *, transport=None):
    transport = transport or SyncTransport(app)
    async with resource_tests.desktop(tmp_path, app, grant, transport=transport) as value:
        await resource_tests.stop_polling(value[2].sync)
        yield value


async def install(local, agent):
    return (await resource_tests.sync(local))[agent]["agent_id"]


def mutation(item, agent, resource, data, revision=0, *, kind=None, operation=None):
    return {
        "operation_id": operation or new_ulid(),
        "item_id": item,
        "agent_id": agent,
        "resource_id": resource,
        "kind": kind or data["kind"],
        "expected_revision": revision,
        "data": data,
    }


async def records(client, token):
    cursor, result, watermark = 0, [], None
    while True:
        params = {"cursor": cursor, "limit": 3}
        if watermark is not None:
            params["watermark"] = watermark
        response = await client.get("/api/sync/changes", params=params, headers=headers(token))
        assert response.status_code == 200, response.text
        page = response.json()
        watermark, cursor = page["watermark"], page["cursor"]
        result.extend(page["records"])
        if not page["has_more"]:
            return result


async def test_cloud_revision_idempotency_deletions_and_device_fence(resources):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    agent, thread, operation = new_ulid(), new_ulid(), new_ulid()
    body = mutation(
        thread, agent, resource, {"kind": "thread", "title": "One"}, operation=operation
    )
    first = await client.put("/api/sync/records", headers=headers(token), json=body)
    assert first.status_code == 200, first.text
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=body)
    ).json() == first.json()
    changed = {**body, "data": {"kind": "thread", "title": "Changed"}}
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=changed)
    ).status_code == 409
    deleted = mutation(thread, agent, resource, None, 1, kind="thread")
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=deleted)
    ).status_code == 200
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=deleted)
    ).status_code == 200
    revive = mutation(thread, agent, resource, {"kind": "thread"}, 2)
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=revive)
    ).status_code == 409
    assert len(await records(client, token)) == 2
    replacement = await identity.bind(client, admin, replace=True)
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=body)
    ).status_code == 401
    new_token = replacement["session"]["access_token"]
    assert len(await records(client, new_token)) == 2


async def test_initial_tombstone_prevents_late_creation_and_requires_ack(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    agent, item = new_ulid(), new_ulid()
    deleted = mutation(item, agent, resource, None, kind="thread")
    response = await client.put("/api/sync/records", headers=headers(token), json=deleted)
    assert response.status_code == 200 and response.json()["data"] is None, response.text
    delayed = mutation(item, agent, resource, {"kind": "thread"})
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=delayed)
    ).status_code == 409
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, factory):
        agent_id = await install(local, resource)
        thread_id = new_ulid()
        remote = mutation(
            thread_id, agent_id, resource, {"kind": "thread", "title": "existing backup"}
        )
        assert (
            await client.put("/api/sync/records", headers=headers(token), json=remote)
        ).status_code == 200
        await runtime.sync.create_thread(thread_id, agent_id)
        assert (await local.delete("/api/sync/records/" + thread_id)).status_code == 204
        assert (await runtime.sync.status()).pending == 1
        response = await local.post("/api/sync/retry")
        assert response.status_code == 200 and response.json()["conflicts"] == 1, response.text
        stored = [row for row in await records(client, token) if row["item_id"] == thread_id]
        assert len(stored) == 1 and stored[0]["data"]["title"] == "existing backup"


async def test_chunk_resume_integrity_and_missing_content_reference(resources):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    agent, item = new_ulid(), new_ulid()
    content = b"a" * CHUNK_BYTES + b"tail"
    checksum = hashlib.sha256(content).hexdigest()
    spec = {"sha256": checksum, "size": len(content)}
    prepared = await client.post("/api/sync/blobs", headers=headers(token), json=spec)
    assert prepared.status_code == 200, prepared.text
    data = {"kind": "file", "path": "inbound/a.bin", **spec}
    body = mutation(item, agent, resource, data)
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=body)
    ).status_code == 422
    path = f"/api/sync/blobs/{checksum}"
    assert (
        await client.put(path + "/chunks/0", content=content[:CHUNK_BYTES], headers=headers(token))
    ).status_code == 204
    assert (await client.post(path + "/complete", headers=headers(token))).status_code == 422
    assert (await client.post("/api/sync/blobs", headers=headers(token), json=spec)).json()[
        "offsets"
    ] == [0]
    assert (
        await client.put(path + "/chunks/0", content=content[:CHUNK_BYTES], headers=headers(token))
    ).status_code == 204
    assert (
        await client.put(
            path + f"/chunks/{CHUNK_BYTES}", content=content[CHUNK_BYTES:], headers=headers(token)
        )
    ).status_code == 204
    assert (await client.post(path + "/complete", headers=headers(token))).json()["complete"]
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=body)
    ).status_code == 200
    assert (
        await client.get(path + f"/chunks/{CHUNK_BYTES}", headers=headers(token))
    ).content == b"tail"
    assert (
        await client.put(path + "/chunks/1", content=b"bad", headers=headers(token))
    ).status_code == 422
    assert (
        await client.put(path + "/chunks/0", content=b"b" * CHUNK_BYTES, headers=headers(token))
    ).status_code == 409


async def test_real_chat_file_memory_backup_and_explicit_restore(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path / "original", app, grant) as (
        _,
        local,
        runtime,
        transport,
        factory,
    ):
        agent_id = await install(local, resource)
        content = b"attachment-" * 120000
        uploaded = await local.put(
            f"/api/agents/{agent_id}/files/inbound/input.bin", content=content
        )
        assert uploaded.status_code == 200, uploaded.text
        attachment_id = uploaded.json()["item_id"]
        agent = await runtime._agent(agent_id)
        await agent.workspace.aupload_bytes("note.txt", b"read-local-only")
        await agent.workspace.aupload_bytes("artifacts/report.txt", b"cloud-product")
        await durable(agent.memory.store, "Keep logical memory in this namespace")
        thread_id = (await runtime.create_thread(agent_id)).thread_id
        events = [
            event
            async for event in runtime.stream(
                agent_id, thread_id, text="read note.txt", attachment_ids=[attachment_id]
            )
        ]
        assert events[-1].data["status"] == "completed"
        synced = await local.post("/api/sync/retry")
        assert synced.status_code == 200, synced.text
        assert synced.json()["pending"] == synced.json()["conflicts"] == 0, synced.text
        original = await runtime.sync.records(agent_id=agent_id)
        messages = [row for row in original if row["kind"] == "message"]
        assert {row["data"]["role"] for row in messages} == {"user", "assistant", "tool"}
        assert any(row["data"]["attachments"] for row in messages)
        assert any(row["data"]["tool_call_id"] == "read-1" for row in messages)
        assert any(
            row["data"]["role"] == "assistant" and "read-local-only" in str(row["data"]["content"])
            for row in messages
        )
        assert all(row["data"]["status"] == "completed" for row in messages)
        assert not any(
            row["kind"] == "file" and row["data"]["path"] == "note.txt" for row in original
        )
        before = await records(client, token)
    async with desktop(tmp_path / "replacement", app, grant) as (_, local, runtime, _, factory):
        replacement_id = await install(local, resource)
        assert replacement_id != agent_id
        assert await runtime.sync.records() == []
        result = await local.post("/api/sync/restore")
        assert result.status_code == 200, result.text
        assert result.json()["restored"] == len(original), result.text
        restored = await runtime.sync.records(agent_id=agent_id)
        assert {row["item_id"] for row in restored} == {row["item_id"] for row in original}
        agent = await runtime._agent(agent_id)
        assert await agent.workspace.adownload_bytes("inbound/input.bin") == content
        assert await agent.workspace.adownload_bytes("artifacts/report.txt") == b"cloud-product"
        assert await agent.workspace.adownload_bytes("note.txt") is None
        assert len(await durable(agent.memory.list_atoms)) == 1
        assert any(item["name"] == "notes" for item in await agent.list_skill_summaries())
        # Recovery appends canonical history only when a new user turn starts.
        assert await agent.aget_history(thread_id) == []
        await agent.workspace.aupload_bytes("note.txt", b"next-round")
        events = [event async for event in runtime.stream(agent_id, thread_id, text="read again")]
        assert events[-1].data["status"] == "completed"
        assert "next-round" in str(events)
        history = await agent.aget_history(thread_id)
        assert any("read-local-only" in str(message.content) for message in history)
        assert len(await records(client, token)) >= len(before)


async def test_response_loss_retries_original_operation_after_restart(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    transport = SyncTransport(app)
    async with desktop(tmp_path, app, grant, transport=transport) as (
        _,
        local,
        runtime,
        _,
        factory,
    ):
        agent_id = await install(local, resource)
        await runtime.create_thread(agent_id)
        transport.lose_record = True
        response = await local.post("/api/sync/retry")
        assert response.status_code == 503, response.text
        assert (await runtime.sync.status()).pending == 1
        operation_id = transport.seen_operations[-1]
        assert len(await records(client, token)) == 1
    async with desktop(tmp_path, app, grant, transport=transport) as (
        _,
        local,
        runtime,
        _,
        factory,
    ):
        response = await local.post("/api/sync/retry")
        assert response.status_code == 200, response.text
        assert transport.seen_operations[-1] == operation_id
        assert response.json()["pending"] == 0
        assert len(await records(client, token)) == 1


async def test_corrupt_content_does_not_publish_restored_references_and_retry_finishes(
    resources, tmp_path
):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path / "source", app, grant) as (_, local, runtime, _, factory):
        agent_id = await install(local, resource)
        assert (
            await local.put(
                f"/api/agents/{agent_id}/files/artifacts/a.txt", content=b"correct-data"
            )
        ).status_code == 200
        await runtime.create_thread(agent_id)
        response = await local.post("/api/sync/retry")
        assert response.json()["pending"] == response.json()["conflicts"] == 0
    transport = SyncTransport(app)
    async with desktop(tmp_path / "target", app, grant, transport=transport) as (
        _,
        local,
        runtime,
        _,
        factory,
    ):
        await install(local, resource)
        transport.damage_chunk = True
        response = await local.post("/api/sync/restore")
        assert response.status_code == 422, response.text
        assert await runtime.sync.records(agent_id=agent_id) == []
        workspace = await runtime.sync.workspace(agent_id)
        assert await workspace.adownload_bytes("artifacts/a.txt") is None
        transport.damage_chunk = False
        response = await local.post("/api/sync/restore")
        assert response.status_code == 200, response.text
        assert response.json()["restored"] > 0
        assert await workspace.adownload_bytes("artifacts/a.txt") == b"correct-data"


async def test_dirty_local_file_is_not_overwritten_by_explicit_restore(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, factory):
        agent_id = await install(local, resource)
        await local.put(f"/api/agents/{agent_id}/files/artifacts/a.txt", content=b"cloud-version")
        assert (await local.post("/api/sync/retry")).json()["pending"] == 0
        agent = await runtime._agent(agent_id)
        await agent.workspace.aupload_bytes("artifacts/a.txt", b"unsynchronized-local-change")
        response = await local.post("/api/sync/restore")
        assert response.status_code == 200, response.text
        assert response.json()["restored"] == 0 and response.json()["skipped"] > 0
        assert (
            await agent.workspace.adownload_bytes("artifacts/a.txt")
            == b"unsynchronized-local-change"
        )
        assert (await runtime.sync.status()).pending > 0


async def test_backups_and_equal_content_digests_are_isolated_by_user(resources):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    agent, thread = new_ulid(), new_ulid()
    body = mutation(thread, agent, resource, {"kind": "thread"})
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=body)
    ).status_code == 200
    content = b"private-even-with-the-same-digest"
    checksum = hashlib.sha256(content).hexdigest()
    await client.post(
        "/api/sync/blobs", json={"sha256": checksum, "size": len(content)}, headers=headers(token)
    )
    path = f"/api/sync/blobs/{checksum}"
    await client.put(path + "/chunks/0", content=content, headers=headers(token))
    await client.post(path + "/complete", headers=headers(token))
    assert (
        await client.post(
            "/api/users",
            headers=headers(admin),
            json={"username": "alice", "password": identity.PASSWORD},
        )
    ).status_code == 201
    account = (await identity.login(client, "alice"))["session"]["access_token"]
    other = (await identity.bind(client, account))["session"]["access_token"]
    assert await records(client, other) == []
    assert (await client.get(path + "/chunks/0", headers=headers(other))).status_code == 404
    assert (await client.post(path + "/complete", headers=headers(other))).status_code == 404
    assert (
        await client.put("/api/sync/records", json=body, headers=headers(other))
    ).status_code == 404
    # An equal digest reserves a distinct user-owned, empty upload.
    prepared = await client.post(
        "/api/sync/blobs", json={"sha256": checksum, "size": len(content)}, headers=headers(other)
    )
    assert prepared.json()["offsets"] == [] and not prepared.json()["complete"]


async def test_file_deletion_recreation_and_historical_attachment_survive_restore(
    resources, tmp_path
):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path / "source", app, grant) as (_, local, runtime, _, factory):
        agent_id = await install(local, resource)
        first = (
            await local.put(f"/api/agents/{agent_id}/files/inbound/a.txt", content=b"first-version")
        ).json()
        thread_id = (await runtime.create_thread(agent_id)).thread_id
        agent = await runtime._agent(agent_id)
        await agent.workspace.aupload_bytes("note.txt", b"local-note")
        _ = [
            event
            async for event in runtime.stream(
                agent_id, thread_id, text="read", attachment_ids=[first["item_id"]]
            )
        ]
        assert (await local.post("/api/sync/retry")).json()["conflicts"] == 0
        assert (await local.delete("/api/sync/records/" + first["item_id"])).status_code == 204
        assert (await local.post("/api/sync/retry")).json()["conflicts"] == 0
        second = (
            await local.put(
                f"/api/agents/{agent_id}/files/inbound/a.txt", content=b"second-version"
            )
        ).json()
        assert second["item_id"] != first["item_id"]
        assert (await local.post("/api/sync/retry")).json()["conflicts"] == 0
        third = (
            await local.put(f"/api/agents/{agent_id}/files/inbound/a.txt", content=b"third-version")
        ).json()
        assert third["item_id"] == second["item_id"]
        assert (await local.post("/api/sync/retry")).json()["conflicts"] == 0
    async with desktop(tmp_path / "target", app, grant) as (_, local, runtime, _, factory):
        await install(local, resource)
        result = await local.post("/api/sync/restore")
        assert result.status_code == 200, result.text
        agent = await runtime._agent(agent_id)
        assert await agent.workspace.adownload_bytes("inbound/a.txt") == b"third-version"
        message = next(
            row
            for row in await runtime.sync.records(agent_id=agent_id, kind="message")
            if row["data"]["attachments"]
        )
        response = await local.get(
            f"/api/sync/messages/{message['item_id']}/attachments/{first['item_id']}"
        )
        assert response.status_code == 200 and response.content == b"first-version"
        assert response.headers["X-Octop-File-Size"] == str(len(b"first-version"))
        before = len(await records(client, token))
        response = await local.post("/api/sync/retry")
        assert response.json()["pending"] == response.json()["conflicts"] == 0, response.text
        assert len(await records(client, token)) == before


async def test_interrupted_projection_recovery_and_no_automatic_execution(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, transport, factory):
        agent_id = await install(local, resource)
        thread_id = (await runtime.create_thread(agent_id)).thread_id
        turn_id = new_ulid()
        projection = await runtime.sync.projection(agent_id, thread_id, turn_id)
        await projection.begin("unfinished input", [], resume=False)
        await projection.capture({"type": "token", "content": "partial text"})
        # Simulate durable state left by a process crash, without executing a model.
        assert any(row["data"].get("status") == "running" for row in await runtime.sync.records())
    async with desktop(tmp_path, app, grant) as (_, local, runtime, transport, factory):
        messages = await runtime.sync.records(kind="message")
        assert len(messages) == 2
        assert all(row["data"]["status"] == "interrupted" for row in messages)
        assert any(row["data"]["content"] == "partial text" for row in messages)
        assert not runtime._active and not runtime._pending and not factory.model._started.is_set()
        response = await local.post("/api/sync/retry")
        assert response.json()["pending"] == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink/hard-link setup")
async def test_export_scope_excludes_credentials_checkpoints_and_external_links(
    resources, tmp_path
):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, factory):
        agent_id = await install(local, resource)
        workspace = await runtime.sync.workspace(agent_id)
        for path in (
            ".env",
            ".octop/auth/key",
            ".octop/sessions/checkpoint.sqlite",
            "inbound/.env",
            "artifacts/cache/temp",
            "artifacts/profile/cookie",
            "artifacts/data.sqlite",
            "elsewhere/normal.txt",
        ):
            await workspace.aupload_bytes(path, b"excluded-secret")
        await workspace.aupload_bytes("artifacts/allowed.txt", b"allowed")
        external = tmp_path / "external-secret"
        external.write_bytes(b"must-not-upload")
        (workspace.workspace_dir / "inbound" / "external-link").symlink_to(external)
        os.link(external, workspace.workspace_dir / "inbound" / "external-hardlink")
        response = await local.post("/api/sync/retry")
        assert response.status_code == 200, response.text
        uploaded = [row["data"] for row in await records(client, token) if row["kind"] == "file"]
        assert [row["path"] for row in uploaded] == ["artifacts/allowed.txt"]


async def test_sync_metadata_quota_still_allows_tombstones(resources, monkeypatch):
    from octop.infra.sync import service

    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    agent, thread = new_ulid(), new_ulid()
    body = mutation(thread, agent, resource, {"kind": "thread"})
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=body)
    ).status_code == 200
    monkeypatch.setattr(service, "METADATA_BYTES", 0)
    update = mutation(thread, agent, resource, {"kind": "thread", "title": "blocked"}, 1)
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=update)
    ).status_code == 403
    tombstone = mutation(thread, agent, resource, None, 1, kind="thread")
    assert (
        await client.put("/api/sync/records", headers=headers(token), json=tombstone)
    ).status_code == 200


async def test_new_binding_preserves_old_pending_data_as_conflicts(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, transport, factory):
        agent_id = await install(local, resource)
        await runtime.create_thread(agent_id)
        assert (await local.post("/api/sync/retry")).json()["pending"] == 0
        await runtime.create_thread(agent_id)
        assert (await runtime.sync.status()).pending == 1
    replacement = await identity.bind(client, admin, replace=True)
    new_token = replacement["session"]["access_token"]
    async with desktop(tmp_path, app, replacement) as (_, local, runtime, _, factory):
        assert (await runtime.sync.status()).conflicts == 1
        result = await local.post("/api/sync/retry")
        assert result.status_code == 200, result.text
        assert result.json()["conflicts"] == 1
        assert len(await records(client, new_token)) == 1


async def test_live_cancellation_commits_partial_transcript_before_retry(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, factory):
        agent_id = await install(local, resource)
        thread = (await runtime.create_thread(agent_id)).thread_id

        async def collect():
            return [
                event
                async for event in runtime.stream(agent_id, thread, text="wait for cancellation")
            ]

        task = asyncio.create_task(collect())
        await asyncio.wait_for(factory.model._started.wait(), 10)
        await runtime.cancel(thread)
        events = await asyncio.wait_for(task, 10)
        assert events[-1].data["status"] == "cancelled"
        messages = await runtime.sync.records(kind="message")
        assert len(messages) == 1 and messages[0]["data"]["status"] == "cancelled"
        response = await local.post("/api/sync/retry")
        assert response.status_code == 200 and response.json()["pending"] == 0, response.text
        uploaded = [
            record for record in await records(client, token) if record["kind"] == "message"
        ]
        assert uploaded[-1]["data"]["status"] == "cancelled"


async def test_sync_openapi_is_typed_and_cloud_never_mounts_execution(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    schema = (await client.get("/api/openapi.json")).json()
    for path, operations in schema["paths"].items():
        if path.startswith("/api/sync"):
            assert all(operation.get("summary") for operation in operations.values())
            assert all(operation.get("responses") for operation in operations.values())
    assert "/api/sync/records" in schema["paths"]
    assert not any("/ws" in path or "/files" in path for path in schema["paths"])
    assert (await client.get("/api/docs")).status_code == 200
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, factory):
        assert (await local.get("/api/docs")).status_code == 200
        schema = (await local.get("/api/openapi.json")).json()
        assert "/api/sync/restore" in schema["paths"]
        assert "/api/sync/records" in schema["paths"]
        assert "/api/sync/blobs" not in schema["paths"]


async def test_withdrawal_keeps_owned_backups_restorable_without_execution(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource = resources
    await resource_tests.assign(client, admin, user_id, resource)
    async with desktop(tmp_path / "source", app, grant) as (_, local, runtime, _, factory):
        agent_id = await install(local, resource)
        await runtime.create_thread(agent_id)
        response = await local.put(
            f"/api/agents/{agent_id}/files/artifacts/report.txt", content=b"owned-backup"
        )
        file_id = response.json()["item_id"]
        assert (await local.post("/api/sync/retry")).json()["pending"] == 0
    await resource_tests.assign(client, admin, user_id, resource, None)
    async with desktop(tmp_path / "target", app, grant) as (_, local, runtime, _, factory):
        await resource_tests.sync(local)
        result = await local.post("/api/sync/restore")
        assert result.status_code == 200 and result.json()["restored"] > 0, result.text
        agents = (await local.get("/api/agents")).json()
        assert agents[0]["agent_id"] == agent_id and not agents[0]["enabled"]
        assert (await local.post(f"/api/agents/{agent_id}/threads")).status_code == 404
        response = await local.get(f"/api/sync/records/{file_id}/content")
        assert response.status_code == 200 and response.content == b"owned-backup"


async def test_missing_runtime_contract_is_reported_without_switching_account(
    resources, tmp_path, monkeypatch
):
    from octop_memory import Memory

    app, client, admin, grant, token, user_id, skill, resource = resources
    async with desktop(tmp_path, app, grant) as (local_app, local, runtime, _, factory):
        monkeypatch.setattr(Memory, "restore_sync_changes", None)
        response = await local.put("/api/cloud/session", json=grant["session"])
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "LOCAL_RUNTIME_UPGRADE_REQUIRED"
        assert local_app.state.accounts.runtime is runtime
