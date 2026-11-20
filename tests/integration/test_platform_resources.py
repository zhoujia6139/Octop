"""P4 publication, delivery, offline drafts and safe local version switches."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager, suppress

import httpx
import pytest

from octop.api.applications.local import create_local_app
from octop.application_config import LocalSettings
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_ulid
from tests.integration import test_cloud_identity as identity
from tests.integration import test_platform_models as model_tests
from tests.integration.test_independent_applications import (
    tool_approval as tool_approval,
)
from tests.support.local_models import LocalModelFactory

cloud = identity.cloud
platform = model_tests.platform
headers = identity.headers


def file(path, data):
    return {
        "path": path,
        "content_base64": base64.b64encode(data).decode(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def skill_spec(text="version-one"):
    return {
        "kind": "skill",
        "name": "Notes",
        "slug": "notes",
        "files": [
            file(
                "SKILL.md",
                b"---\nname: notes\ndescription: Read workspace notes\n---\n\n" + text.encode(),
            ),
            file("references/info.txt", text.encode()),
        ],
    }


def agent_spec(skill=None, *, prompt="first prompt"):
    return {
        "kind": "agent",
        "name": "Reader",
        "model_id": "platform/test",
        "system_prompt": prompt,
        "skills": [{"resource_id": skill, "revision": 1}] if skill else [],
    }


def mutation(spec, revision=0, operation_id=None):
    return {"operation_id": operation_id or new_ulid(), "expected_revision": revision, "spec": spec}


async def save(client, token, resource_id, spec, revision=0, *, platform=True, operation_id=None):
    path = "/api/resource-admin/resources/" if platform else "/api/resources/personal/"
    response = await client.put(
        path + resource_id, headers=headers(token), json=mutation(spec, revision, operation_id)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def publish(client, token, resource_id, revision=1):
    response = await client.post(
        f"/api/resource-admin/resources/{resource_id}/publish",
        headers=headers(token),
        json={"revision": revision},
    )
    assert response.status_code == 200, response.text


async def assign(client, admin, user_id, resource_id, revision=1):
    response = await client.put(
        f"/api/resource-admin/users/{user_id}/assignments/{resource_id}",
        headers=headers(admin),
        json={"revision": revision},
    )
    assert response.status_code == 200, response.text


@pytest.fixture
async def resources(cloud):
    app, client, admin = cloud
    grant = await identity.bind(client, admin)
    token = grant["session"]["access_token"]
    user_id = grant["session"]["identity"]["user"]["user_id"]
    skill, agent = new_ulid(), new_ulid()
    await save(client, admin, skill, skill_spec())
    await publish(client, admin, skill)
    await save(client, admin, agent, agent_spec(skill))
    await publish(client, admin, agent)
    return app, client, admin, grant, token, user_id, skill, agent


class FaultTransport(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.delegate = httpx.ASGITransport(app=app)
        self.offline = False
        self.lose_write = False
        self.damage = False

    async def handle_async_request(self, request):
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        response = await self.delegate.handle_async_request(request)
        if self.lose_write and request.url.path.startswith("/api/resources/personal/"):
            self.lose_write = False
            await response.aclose()
            raise httpx.ReadError("response lost after durable cloud commit", request=request)
        if self.damage and "/versions/" in request.url.path:
            data = json.loads(await response.aread())
            if data.get("spec", {}).get("kind") == "skill":
                data["spec"]["files"][0]["content_base64"] = base64.b64encode(b"corrupted").decode()
                await response.aclose()
                return httpx.Response(200, json=data, request=request)
        return response


async def stop_polling(service):
    if service._task:
        service._task.cancel()
        with suppress(asyncio.CancelledError):
            await service._task
        service._task = None


@asynccontextmanager
async def desktop(tmp_path, cloud_app, grant, *, transport=None, factory=None):
    transport = transport or FaultTransport(cloud_app)
    factory = factory or LocalModelFactory()
    app = create_local_app(
        LocalSettings(
            home=tmp_path / "desktop",
            api_token=identity.LOCAL_TOKEN,
            cloud_url="http://127.0.0.1:9876",
            enable_api_docs=True,
        ),
        model_factory=factory,
        cloud_transport=transport,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers=identity.LOCAL_HEADERS,
        ) as client,
    ):
        response = await client.put("/api/cloud/session", json=grant["session"])
        assert response.status_code == 200, response.text
        runtime = app.state.accounts.runtime
        await stop_polling(runtime.resources)
        yield app, client, runtime, transport, factory


async def sync(local):
    response = await local.post("/api/resources/sync")
    assert response.status_code == 200, response.text
    return {row["resource_id"]: row for row in response.json()}


async def test_publication_pins_skills_and_assignment_enforces_privacy(resources):
    app, client, admin, grant, token, user_id, skill, agent = resources
    assert (
        await client.get(f"/api/resources/{agent}/versions/1", headers=headers(token))
    ).status_code == 404
    assert (
        await client.put(
            f"/api/resource-admin/resources/{new_ulid()}",
            headers=headers(token),
            json=mutation(skill_spec()),
        )
    ).status_code == 401
    await assign(client, admin, user_id, agent)
    agent_version = (
        await client.get(f"/api/resources/{agent}/versions/1", headers=headers(token))
    ).json()
    digest = agent_version["digest"]
    assert agent_version["spec"]["skills"] == [{"resource_id": skill, "revision": 1}]
    assert (
        await client.get(f"/api/resources/{skill}/versions/1", headers=headers(token))
    ).status_code == 200
    await save(client, admin, skill, skill_spec("second version"), revision=1)
    await publish(client, admin, skill, 2)
    assert (
        await client.get(f"/api/resources/{skill}/versions/2", headers=headers(token))
    ).status_code == 404
    assert (await client.get(f"/api/resources/{agent}/versions/1", headers=headers(token))).json()[
        "digest"
    ] == digest
    denied = await client.put(
        f"/api/resources/personal/{agent}", headers=headers(token), json=mutation(agent_spec(), 1)
    )
    assert denied.status_code == 404
    # Old events resolve to current withdrawal, never to a stale installation.
    await assign(client, admin, user_id, agent, None)
    changes = (await client.get("/api/resources/changes", headers=headers(token))).json()
    assert all(event["revision"] is None for event in changes["changes"])
    assert (
        await client.get(f"/api/resources/{skill}/versions/1", headers=headers(token))
    ).status_code == 404


async def test_personal_idempotency_conflicts_tombstones_and_cross_user(resources):
    app, client, admin, grant, token, user_id, skill, agent = resources
    resource_id, operation_id = new_ulid(), new_ulid()
    payload = mutation(agent_spec(), operation_id=operation_id)
    first = await client.put(
        "/api/resources/personal/" + resource_id, json=payload, headers=headers(token)
    )
    repeated = await client.put(
        "/api/resources/personal/" + resource_id, json=payload, headers=headers(token)
    )
    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    payload["spec"]["name"] = "different operation body"
    assert (
        await client.put(
            "/api/resources/personal/" + resource_id, json=payload, headers=headers(token)
        )
    ).status_code == 409
    await client.post(
        "/api/users",
        json={"username": "other", "password": identity.PASSWORD},
        headers=headers(admin),
    )
    other = (await identity.login(client, "other"))["session"]["access_token"]
    other_device = (await identity.bind(client, other))["session"]["access_token"]
    assert (
        await client.get(f"/api/resources/{resource_id}/versions/1", headers=headers(other_device))
    ).status_code == 404
    assert (
        await client.put(
            "/api/resources/personal/" + resource_id,
            json=mutation(agent_spec(), 1),
            headers=headers(other_device),
        )
    ).status_code == 404
    tombstone = await save(client, token, resource_id, None, 1, platform=False)
    assert tombstone["deleted"] and tombstone["revision"] == 2
    assert (
        await client.put(
            "/api/resources/personal/" + resource_id,
            json=mutation(agent_spec(), 2),
            headers=headers(token),
        )
    ).status_code == 409
    assert (
        await client.put(
            "/api/resources/personal/" + resource_id,
            json=mutation(agent_spec()),
            headers=headers(admin),
        )
    ).status_code == 401
    with app.state.cloud.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cloud_resource_versions WHERE resource_id=?", (resource_id,)
            ).fetchone()[0]
            == 2
        )


async def test_device_replacement_does_not_inherit_platform_assignments(resources):
    app, client, admin, grant, token, user_id, skill, agent = resources
    await assign(client, admin, user_id, agent)
    replacement = await identity.bind(client, admin, replace=True)
    replacement_token = replacement["session"]["access_token"]
    assert (
        await client.get(f"/api/resources/{agent}/versions/1", headers=headers(token))
    ).status_code == 401
    assert (
        await client.get(f"/api/resources/{agent}/versions/1", headers=headers(replacement_token))
    ).status_code == 404
    await assign(client, admin, user_id, agent)
    assert (
        await client.get(f"/api/resources/{agent}/versions/1", headers=headers(replacement_token))
    ).status_code == 200


async def test_desktop_install_reports_skills_and_runs_harness(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, agent_id = resources
    await assign(client, admin, user_id, agent_id)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, _):
        result = await sync(local)
        assert result[agent_id]["installed_revision"] == 1
        instance_id = result[agent_id]["agent_id"]
        assert instance_id != agent_id
        agent = await runtime._agent(instance_id)
        summaries = await agent.list_skill_summaries()
        assert any(item["name"] == "notes" for item in summaries), summaries
        await agent.workspace.aupload_bytes("note.txt", b"published-agent-works")
        thread = (await local.post(f"/api/agents/{instance_id}/threads")).json()["thread_id"]
        events = [
            event async for event in runtime.stream(instance_id, thread, text="read note.txt")
        ]
        assert events[-1].data["status"] == "completed"
        assert "published-agent-works" in str(events)
        await sync(local)
        reports = (
            await client.get(f"/api/resource-admin/users/{user_id}/reports", headers=headers(admin))
        ).json()
        report = next(r for r in reports if r["resource_id"] == agent_id)
        assert report["desired_revision"] == report["installed_revision"] == 1
        assert report["state"] == "installed" and report["runtime_state"] == "idle"
        assert report["reported_at"] > 0
        assert (await local.get("/api/docs")).status_code == 200
        paths = (await local.get("/api/openapi.json")).json()["paths"]
        assert paths["/api/resources/sync"]["post"]["summary"]


async def test_personal_copy_survives_platform_updates_with_own_skill_snapshots(
    resources, tmp_path
):
    app, client, admin, grant, token, user_id, skill, agent = resources
    await assign(client, admin, user_id, agent)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, _):
        await sync(local)
        copy_body = {"resource_id": new_ulid(), "operation_id": new_ulid(), "revision": 1}
        response = await local.post(f"/api/resources/{agent}/copy", json=copy_body)
        assert response.status_code == 201, response.text
        copy_id = response.json()["resource_id"]
        assert copy_id != agent
        repeated = await local.post(f"/api/resources/{agent}/copy", json=copy_body)
        assert repeated.status_code == 201 and repeated.json()["resource_id"] == copy_id
        copied = await sync(local)
        copied_skill = copied[copy_id]["spec"]["skills"][0]["resource_id"]
        assert copied_skill != skill and copied[copied_skill]["source_id"] == skill
        changed = {**copied[copy_id]["spec"], "system_prompt": "my personal prompt"}
        assert (
            await local.put("/api/resources/personal/" + copy_id, json=mutation(changed, 1))
        ).status_code == 200
        await sync(local)
        await save(client, admin, agent, agent_spec(skill, prompt="platform update"), 1)
        await publish(client, admin, agent, 2)
        await assign(client, admin, user_id, agent, 2)
        updated = await sync(local)
        assert updated[agent]["installed_revision"] == 2
        assert updated[copy_id]["spec"]["system_prompt"] == "my personal prompt"
        assert updated[copy_id]["source_revision"] == 1
        assert updated[copied_skill]["revision"] == 1
        # Withdrawal cannot change a previously accepted personal copy.
        await assign(client, admin, user_id, agent, None)
        removed = await sync(local)
        assert removed[agent]["install_state"] == "removed"
        assert removed[copy_id]["installed_revision"] == 2
        personal = (
            await client.get(f"/api/resources/{copy_id}/versions/2", headers=headers(token))
        ).json()
        assert personal["spec"]["system_prompt"] == "my personal prompt"


async def test_install_failure_keeps_previous_complete_tree_and_retries(
    resources, tmp_path, monkeypatch
):
    app, client, admin, grant, token, user_id, skill, agent_id = resources
    await assign(client, admin, user_id, agent_id)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, _):
        await sync(local)
        instance_id = runtime.resources.repo.get(agent_id)["agent_id"]
        previous = await runtime._agent(instance_id)
        old_path = str(previous.config.skills_dir)
        old_file = await previous.workspace.adownload_bytes(old_path + "/notes/references/info.txt")
        await save(client, admin, agent_id, agent_spec(skill, prompt="new prompt"), 1)
        await publish(client, admin, agent_id, 2)
        await assign(client, admin, user_id, agent_id, 2)
        from octop_harness.backends.workspace import BackendWorkspace

        upload = BackendWorkspace.aupload_bytes

        async def fail_second(self, path, data):
            if path.endswith("references/info.txt"):
                raise OSError("simulated incomplete install")
            await upload(self, path, data)

        with monkeypatch.context() as patch:
            patch.setattr(BackendWorkspace, "aupload_bytes", fail_second)
            failed = await sync(local)
        assert failed[agent_id]["install_state"] == "failed"
        assert (
            failed[agent_id]["desired_revision"] == 2
            and failed[agent_id]["installed_revision"] == 1
        )
        assert await runtime._agent(instance_id) is previous
        assert (
            await previous.workspace.adownload_bytes(old_path + "/notes/references/info.txt")
            == old_file
        )
        recovered = await sync(local)
        assert recovered[agent_id]["installed_revision"] == 2
        assert (await runtime._agent(instance_id)).config.system_prompt == "new prompt"
        stable = await runtime._agent(instance_id)
        await sync(local)
        assert await runtime._agent(instance_id) is stable


@pytest.mark.parametrize("pending", ["running", "approval"])
async def test_version_switch_waits_for_turn_and_approval_boundary(
    resources, tmp_path, pending, tool_approval
):
    app, client, admin, grant, token, user_id, skill, agent_id = resources
    await assign(client, admin, user_id, agent_id)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, factory):
        await sync(local)
        instance_id = runtime.resources.repo.get(agent_id)["agent_id"]
        first = await runtime._agent(instance_id)
        await first.workspace.aupload_bytes("note.txt", b"turn-boundary")
        thread = (await local.post(f"/api/agents/{instance_id}/threads")).json()["thread_id"]

        async def collect(**kwargs):
            return [event async for event in runtime.stream(instance_id, thread, **kwargs)]

        if pending == "running":
            task = asyncio.create_task(collect(text="wait"))
            await asyncio.wait_for(factory.model._started.wait(), 5)
        else:
            events = await collect(text="write a file")
            assert events[-1].data["status"] == "awaiting_approval"
        await save(client, admin, agent_id, agent_spec(skill, prompt="next version"), 1)
        await publish(client, admin, agent_id, 2)
        await assign(client, admin, user_id, agent_id, 2)
        result = await sync(local)
        assert (
            result[agent_id]["installed_revision"] == 1
            and result[agent_id]["install_state"] == "pending"
        )
        assert await runtime._agent(instance_id) is first
        if pending == "running":
            await runtime.cancel(thread)
            await task
        else:
            resumed = await collect(decisions=[{"type": "approve"}])
            assert resumed[-1].data["status"] == "completed"
            assert first.config.system_prompt == "first prompt"
        # A new turn installs the waiting version before it starts.
        final = await collect(text="read note.txt")
        assert final[-1].data["status"] == "completed"
        assert (await runtime._agent(instance_id)).config.system_prompt == "next version"


async def test_offline_draft_response_loss_restart_and_cloud_conflict(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, agent = resources
    transport = FaultTransport(app)
    resource_id = new_ulid()
    first_payload = mutation(agent_spec())
    async with desktop(tmp_path, app, grant, transport=transport) as (
        _,
        local,
        runtime,
        transport,
        _,
    ):
        transport.offline = True
        saved = await local.put("/api/resources/personal/" + resource_id, json=first_payload)
        assert saved.status_code == 200 and saved.json()["sync_state"] == "pending"
        assert (await local.post("/api/resources/sync")).status_code == 503
        transport.offline = False
        transport.lose_write = True
        assert (await local.post("/api/resources/sync")).status_code == 503
        assert runtime.resources.repo.outbox()
    # No refresh secret is stored by the sidecar. Reattach the same verified
    # short-lived device grant and replay the durable operation after restart.
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, _):
        result = await sync(local)
        assert result[resource_id]["sync_state"] == "synced"
        assert result[resource_id]["revision"] == 1
        replay = await local.put("/api/resources/personal/" + resource_id, json=first_payload)
        assert replay.status_code == 200
        with app.state.cloud.db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM cloud_resource_versions WHERE resource_id=?",
                    (resource_id,),
                ).fetchone()[0]
                == 1
            )
        # Another authorized client won a revision race. Keep the local edit
        # visible and block overwrite rather than silently accepting cloud data.
        await save(client, token, resource_id, agent_spec(prompt="cloud edit"), 1, platform=False)
        local_edit = mutation(agent_spec(prompt="unsynchronized local edit"), 1)
        assert (
            await local.put("/api/resources/personal/" + resource_id, json=local_edit)
        ).status_code == 200
        conflicted = await sync(local)
        assert conflicted[resource_id]["sync_state"] == "conflict"
        assert conflicted[resource_id]["spec"]["system_prompt"] == "unsynchronized local edit"
        assert runtime.resources.repo.outbox()[0]["state"] == "conflict"


async def test_model_gateway_requires_formal_agent_authority(resources):
    app, client, admin, grant, token, user_id, skill, agent = resources
    context = {
        "resource_id": agent,
        "agent_id": new_ulid(),
        "thread_id": new_ulid(),
        "turn_id": new_ulid(),
    }
    assert (
        await client.post("/api/model-contexts", headers=headers(token), json=context)
    ).status_code == 403
    await assign(client, admin, user_id, agent)
    assert (
        await client.post("/api/model-contexts", headers=headers(token), json=context)
    ).status_code == 204
    await assign(client, admin, user_id, agent, None)
    # A stale consumption-context record never substitutes for assignment.
    assert (
        await client.post("/api/model-contexts", headers=headers(token), json=context)
    ).status_code == 403


async def test_corrupt_download_does_not_advance_cursor_or_activate(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, agent = resources
    await assign(client, admin, user_id, agent)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, transport, _):
        transport.damage = True
        with pytest.raises(OctopError) as failed:
            await runtime.resources.sync()
        assert failed.value.code == ErrorCode.RESOURCE_INVALID
        assert runtime.resources.repo.get(agent)["install_state"] == "failed"
        assert runtime.resources.repo.state("cursor") in {None, "0"}
        assert runtime.services.agents.get(agent) is None
        transport.damage = False
        result = await sync(local)
        assert result[agent]["installed_revision"] == 1


async def test_shared_platform_definition_has_distinct_user_executions(resources):
    app, client, admin, grant, token, user_id, skill, agent = resources
    await assign(client, admin, user_id, agent)
    response = await client.post(
        "/api/users",
        headers=headers(admin),
        json={"username": "second", "password": identity.PASSWORD},
    )
    other_user = response.json()["user_id"]
    other_account = (await identity.login(client, "second"))["session"]["access_token"]
    other_device = (await identity.bind(client, other_account))["session"]["access_token"]
    await assign(client, admin, other_user, agent)
    first = {
        "resource_id": agent,
        "agent_id": new_ulid(),
        "thread_id": new_ulid(),
        "turn_id": new_ulid(),
    }
    second = {
        "resource_id": agent,
        "agent_id": new_ulid(),
        "thread_id": new_ulid(),
        "turn_id": new_ulid(),
    }
    for session, body in [(token, first), (other_device, second)]:
        response = await client.post("/api/model-contexts", headers=headers(session), json=body)
        assert response.status_code == 204, response.text
    assert (
        await client.post("/api/model-contexts", headers=headers(other_device), json=first)
    ).status_code == 403
    await assign(client, admin, user_id, agent, None)
    assert (
        await client.post("/api/model-contexts", headers=headers(token), json=first)
    ).status_code == 403
    assert (
        await client.post("/api/model-contexts", headers=headers(other_device), json=second)
    ).status_code == 204


@pytest.mark.parametrize("deletion_origin", ["local", "cloud"])
async def test_deleted_personal_resource_is_disabled_without_erasing_threads(
    resources, tmp_path, deletion_origin
):
    app, client, admin, grant, token, user_id, skill, agent = resources
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, _):
        resource_id = new_ulid()
        body = mutation(agent_spec())
        assert (
            await local.put("/api/resources/personal/" + resource_id, json=body)
        ).status_code == 200
        thread = (await local.post(f"/api/agents/{resource_id}/threads")).json()["thread_id"]
        delete = mutation(None, 1)
        if deletion_origin == "local":
            response = await local.put("/api/resources/personal/" + resource_id, json=delete)
            assert response.status_code == 200 and response.json()["install_state"] == "removed"
            assert (
                await local.put("/api/resources/personal/" + resource_id, json=delete)
            ).status_code == 200
        else:
            await sync(local)
            await save(client, token, resource_id, None, 1, platform=False)
        result = await sync(local)
        assert (
            result[resource_id]["install_state"] == "removed"
            and result[resource_id]["spec"] is None
        )
        assert result[resource_id]["sync_state"] == "synced"
        assert runtime.services.threads.belongs_to(thread, resource_id)
        assert (await local.post(f"/api/agents/{resource_id}/threads")).status_code == 404
        with pytest.raises(OctopError):
            await runtime._agent(resource_id)
        remote = (
            await client.get(f"/api/resources/{resource_id}/versions/2", headers=headers(token))
        ).json()
        assert remote["spec"] is None and remote["resource"]["deleted"]


async def test_background_delivery_and_revocation_cleanup(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, agent = resources
    await assign(client, admin, user_id, agent)
    async with desktop(tmp_path, app, grant) as (local_app, local, runtime, transport, _):
        runtime.resources.start()
        async with asyncio.timeout(5):
            while (
                not runtime.resources.repo.get(agent)
                or runtime.resources.repo.get(agent)["installed_revision"] != 1
            ):
                await asyncio.sleep(0.01)
        await identity.bind(client, admin, replace=True)
        response = await local.post("/api/resources/sync")
        assert response.status_code in {401, 403}
        async with asyncio.timeout(5):
            while runtime.services is not None:
                await asyncio.sleep(0.01)
        assert local_app.state.accounts.runtime is None


async def test_assigned_agent_real_http_uses_private_model_relay(platform, tmp_path):
    from tests.integration import test_platform_models as models

    app, client, admin, token, supplier, configure, request, grant = platform
    supplier.mode = "tool"
    large = models.definition()
    large["input_token_ceiling"] = 128000
    await configure(large)
    skill, resource_id = new_ulid(), new_ulid()
    await save(client, admin, skill, skill_spec())
    await publish(client, admin, skill)
    await save(client, admin, resource_id, agent_spec(skill))
    await publish(client, admin, resource_id)
    await assign(client, admin, grant["session"]["identity"]["user"]["user_id"], resource_id)
    async with models.serve(app) as cloud_url:
        local_app = create_local_app(
            LocalSettings(
                home=tmp_path / "real-local", cloud_url=cloud_url, api_token=identity.LOCAL_TOKEN
            )
        )
        async with (
            models.serve(local_app, lifespan="on") as local_url,
            httpx.AsyncClient(base_url=local_url, headers=identity.LOCAL_HEADERS) as local,
        ):
            assert (await local.put("/api/cloud/session", json=grant["session"])).status_code == 200
            installed = await sync(local)
            instance_id = installed[resource_id]["agent_id"]
            assert instance_id != resource_id and installed[resource_id]["installed_revision"] == 1
            runtime = local_app.state.accounts.runtime
            agent = await runtime._agent(instance_id)
            await agent.workspace.aupload_bytes("note.txt", b"published-through-gateway")
            thread = (await local.post(f"/api/agents/{instance_id}/threads")).json()["thread_id"]
            events = [
                event async for event in runtime.stream(instance_id, thread, text="read note.txt")
            ]
            assert events[-1].data["status"] == "completed"
            assert "published-through-gateway" in str(events)
            rows = (await client.get("/api/usage/requests", headers=headers(token))).json()
            owned = [r for r in rows if r["agent_id"] == instance_id]
            assert len(owned) == 2 and all(r["state"] == "settled" for r in owned)
            assert len(supplier.calls) == 2
            await assign(
                client, admin, grant["session"]["identity"]["user"]["user_id"], resource_id, None
            )
            denied = await client.post(
                "/api/model-contexts",
                headers=headers(token),
                json={
                    "resource_id": resource_id,
                    "agent_id": instance_id,
                    "thread_id": thread,
                    "turn_id": new_ulid(),
                },
            )
            assert denied.status_code == 403


async def test_simultaneous_starts_cannot_bypass_install_boundary(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, resource_id = resources
    await assign(client, admin, user_id, resource_id)
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, factory):
        result = await sync(local)
        instance = result[resource_id]["agent_id"]
        thread = (await local.post(f"/api/agents/{instance}/threads")).json()["thread_id"]

        async def collect():
            return [event async for event in runtime.stream(instance, thread, text="wait")]

        tasks = [asyncio.create_task(collect()) for _ in range(2)]
        try:
            completed, pending = await asyncio.wait(
                tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            assert len(completed) == len(pending) == 1
            with pytest.raises(OctopError) as error:
                completed.pop().result()
            assert error.value.code == ErrorCode.THREAD_TURN_ACTIVE
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_concurrent_personal_creates_enforce_user_limit(resources, monkeypatch):
    from octop.infra.resources import service

    app, client, admin, grant, token, user_id, skill, agent = resources
    monkeypatch.setattr(service, "PERSONAL_RESOURCE_LIMIT", 1)
    responses = await asyncio.gather(
        *[
            client.put(
                "/api/resources/personal/" + new_ulid(),
                headers=headers(token),
                json=mutation(agent_spec()),
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in responses) == [200, 403]
    rejected = next(r for r in responses if r.status_code == 403)
    assert rejected.json()["error"]["code"] == "RESOURCE_QUOTA_EXCEEDED"


async def test_resource_openapi_is_typed_and_cloud_has_no_execution_routes(resources):
    app, client, admin, grant, token, user_id, skill, agent = resources
    document = (await client.get("/api/openapi.json")).json()
    for path, verbs in document["paths"].items():
        if path.startswith(("/api/resources/", "/api/resource-admin/")):
            for operation in verbs.values():
                assert operation.get("summary")
                assert operation.get("security")
                assert operation.get("responses")
                if "requestBody" in operation:
                    assert (
                        "$ref" in operation["requestBody"]["content"]["application/json"]["schema"]
                    )
    assert not any(
        "/ws" in path or "/terminal" in path or "/workspace" in path or "/chat" in path
        for path in document["paths"]
    )
    assert (await client.get("/api/docs")).status_code == 200


async def test_cloud_and_local_bound_resource_json_before_parsing(resources, tmp_path):
    app, client, admin, grant, token, user_id, skill, agent = resources
    body = b"x" * (8 * 1024 * 1024 + 1)
    response = await client.put(
        "/api/resource-admin/resources/" + new_ulid(), headers=headers(admin), content=body
    )
    assert (
        response.status_code == 413 and response.json()["error"]["code"] == "ATTACHMENT_TOO_LARGE"
    )
    async with desktop(tmp_path, app, grant) as (_, local, runtime, _, _):
        response = await local.put("/api/resources/personal/" + new_ulid(), content=body)
        assert (
            response.status_code == 413
            and response.json()["error"]["code"] == "ATTACHMENT_TOO_LARGE"
        )
        assert runtime.resources.repo.list_resources() == []


async def test_storage_limit_does_not_prevent_personal_deletion(resources, monkeypatch):
    from octop.infra.resources import service

    app, client, admin, grant, token, user_id, skill, agent = resources
    resource_id = new_ulid()
    await save(client, token, resource_id, agent_spec(), platform=False)
    monkeypatch.setattr(service, "PERSONAL_STORAGE_BYTES", 0)
    assert (
        await client.put(
            "/api/resources/personal/" + resource_id,
            headers=headers(token),
            json=mutation(agent_spec(prompt="more storage"), 1),
        )
    ).status_code == 403
    tombstone = await save(client, token, resource_id, None, 1, platform=False)
    assert tombstone["deleted"]
