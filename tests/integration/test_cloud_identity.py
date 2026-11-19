"""P2 real cloud HTTP identity, second factors, revocation and local isolation."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
import pyotp
import pytest

from octop.api.applications.cloud import create_cloud_app
from octop.api.applications.local import create_local_app
from octop.application_config import CloudSettings, LocalSettings
from octop.infra.errors import OctopError
from octop.infra.utils.ulid import new_ulid
from tests.support.http import ws_connect
from tests.support.local_models import LocalModelFactory
from tests.support.postgresql import requires_postgresql

PASSWORD = "Cloud-valid-7359"
LOCAL_TOKEN = "local-token-completely-separate-123456789"
LOCAL_HEADERS = {"Authorization": f"Bearer {LOCAL_TOKEN}"}


def headers(token):
    return {"Authorization": f"Bearer {token}"}


async def login(client, username="admin", password=PASSWORD):
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def bind(client, account, *, replace=False):
    response = await client.post(
        "/api/auth/devices",
        json={"label": "test-desktop", "replace": replace},
        headers=headers(account),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgresql", marks=[requires_postgresql, pytest.mark.postgresql]),
    ]
)
async def cloud(tmp_path, request):
    schema = "p2_" + new_ulid().lower()
    database_url = None
    if request.param == "postgresql":
        import psycopg
        from psycopg import sql

        def create_schema():
            with psycopg.connect(os.environ["OCTOP_TEST_DATABASE_URL"], autocommit=True) as conn:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        await asyncio.to_thread(create_schema)
        # CloudSettings accepts URLs, not libpq keyword strings.
        from urllib.parse import quote

        database_url = (
            os.environ["OCTOP_TEST_DATABASE_URL"] + "?options=" + quote(f"-csearch_path={schema}")
        )
    app = create_cloud_app(
        CloudSettings(home=tmp_path / "cloud", enable_api_docs=True, database_url=database_url)
    )
    async with app.router.lifespan_context(app):
        await asyncio.to_thread(app.state.cloud.identity.bootstrap, "admin", PASSWORD)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://cloud.test"
        ) as client:
            account = (await login(client))["session"]["access_token"]
            try:
                yield app, client, account
            finally:
                if request.param == "postgresql":
                    # Close pooled connections before dropping only this test's schema.
                    await app.state.cloud.close()

                    def drop_schema():
                        with psycopg.connect(
                            os.environ["OCTOP_TEST_DATABASE_URL"], autocommit=True
                        ) as conn:
                            conn.execute(
                                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                            )

                    await asyncio.to_thread(drop_schema)


async def test_password_policy_user_permissions_and_live_disable(cloud):
    app, client, admin = cloud
    assert (await client.get("/api/users")).status_code == 401
    weak = await client.post(
        "/api/users", json={"username": "weak", "password": "password"}, headers=headers(admin)
    )
    assert weak.status_code == 400
    response = await client.post(
        "/api/users", json={"username": "ALICE", "password": PASSWORD}, headers=headers(admin)
    )
    assert response.status_code == 201
    alice_id = response.json()["user_id"]
    assert response.json()["username"] == "alice"
    account = (await login(client, "Alice"))["session"]["access_token"]
    assert (await client.get("/api/users", headers=headers(account))).status_code == 403
    grant = await bind(client, account)
    device = grant["session"]["access_token"]
    assert (await client.get("/api/users", headers=headers(device))).status_code == 401
    assert (
        await client.put(
            f"/api/users/{alice_id}",
            json={"enabled": False, "role": "user"},
            headers=headers(admin),
        )
    ).status_code == 200
    for token in (account, device):
        assert (await client.get("/api/auth/me", headers=headers(token))).status_code == 401
    assert (
        await client.post(
            "/api/auth/device/refresh", json={"refresh_token": grant["refresh_token"]}
        )
    ).status_code == 403
    assert (
        await client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    ).status_code == 403
    assert (
        await client.put(
            f"/api/users/{alice_id}", json={"enabled": True, "role": "user"}, headers=headers(admin)
        )
    ).status_code == 200
    assert (await client.get("/api/auth/me", headers=headers(device))).status_code == 401
    with pytest.raises(OctopError):
        await asyncio.to_thread(app.state.cloud.identity.bootstrap, "another", PASSWORD)


async def test_mfa_challenge_replay_recovery_single_use_and_purpose(cloud):
    app, client, account = cloud
    enrolled = await client.post("/api/auth/mfa/enroll", headers=headers(account))
    assert enrolled.status_code == 200
    secret = enrolled.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirmed = await client.post(
        "/api/auth/mfa/confirm", json={"code": code}, headers=headers(account)
    )
    assert confirmed.status_code == 200, confirmed.text
    recovery = confirmed.json()["codes"]
    assert len(recovery) == 10
    assert (await client.get("/api/auth/me", headers=headers(account))).status_code == 401
    challenge = (await login(client))["mfa_token"]
    assert (await client.get("/api/auth/me", headers=headers(challenge))).status_code == 401
    assert (
        await client.post("/api/auth/devices", json={"label": "bypass"}, headers=headers(challenge))
    ).status_code == 401
    response = await client.post(
        "/api/auth/mfa/verify", json={"mfa_token": challenge, "code": code}
    )
    assert response.status_code == 200, response.text
    account = response.json()["access_token"]
    assert (
        await client.post("/api/auth/mfa/verify", json={"mfa_token": challenge, "code": code})
    ).status_code == 401
    challenge2 = (await login(client))["mfa_token"]
    assert (
        await client.post("/api/auth/mfa/verify", json={"mfa_token": challenge2, "code": code})
    ).status_code == 401
    recovered = await client.post(
        "/api/auth/mfa/verify", json={"mfa_token": challenge2, "code": recovery[0]}
    )
    assert recovered.status_code == 200
    challenge3 = (await login(client))["mfa_token"]
    assert (
        await client.post(
            "/api/auth/mfa/verify", json={"mfa_token": challenge3, "code": recovery[0]}
        )
    ).status_code == 401
    with app.state.cloud.db.connect() as conn:
        assert (
            bytes(conn.execute("SELECT secret_enc FROM cloud_mfa").fetchone()[0]) != secret.encode()
        )
        assert all(
            c not in str(conn.execute("SELECT * FROM cloud_recovery_codes").fetchall())
            for c in recovery
        )
    disabled = await client.post(
        "/api/auth/mfa/disable",
        json={"password": PASSWORD, "code": recovery[1]},
        headers=headers(account),
    )
    assert disabled.status_code == 204
    assert (await client.get("/api/auth/me", headers=headers(account))).status_code == 401
    assert (await login(client))["session"] is not None


async def test_failed_login_and_mfa_attempt_limits_persist(cloud):
    app, client, account = cloud
    for _ in range(5):
        assert (
            await client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})
        ).status_code == 401
    assert (
        await client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})
    ).status_code == 429
    enrolled = (await client.post("/api/auth/mfa/enroll", headers=headers(account))).json()
    codes = (
        await client.post(
            "/api/auth/mfa/confirm",
            json={"code": pyotp.TOTP(enrolled["secret"]).now()},
            headers=headers(account),
        )
    ).json()["codes"]
    challenge = (await login(client))["mfa_token"]
    for _ in range(5):
        assert (
            await client.post(
                "/api/auth/mfa/verify", json={"mfa_token": challenge, "code": "invalid"}
            )
        ).status_code == 401
    assert (
        await client.post("/api/auth/mfa/verify", json={"mfa_token": challenge, "code": codes[0]})
    ).status_code == 429
    fresh = (await login(client))["mfa_token"]
    assert (
        await client.post("/api/auth/mfa/verify", json={"mfa_token": fresh, "code": codes[0]})
    ).status_code == 429
    with app.state.cloud.db.transaction() as conn:
        conn.execute("UPDATE cloud_login_attempts SET window_start=0")
    assert (
        await client.post("/api/auth/mfa/verify", json={"mfa_token": fresh, "code": codes[0]})
    ).status_code == 200


async def test_device_binding_replacement_cross_user_revoke_and_logout(cloud):
    _, client, account = cloud
    first = await bind(client, account)
    token = first["session"]["access_token"]
    assert (await client.get("/api/auth/device/me", headers=headers(account))).status_code == 401
    assert (await client.get("/api/auth/device/me", headers=headers(token))).status_code == 200
    assert (
        await client.post("/api/auth/device/refresh", json={"refresh_token": token})
    ).status_code == 403
    assert (
        await client.post("/api/auth/devices", json={"label": "new"}, headers=headers(account))
    ).status_code == 409
    second = await bind(client, account, replace=True)
    assert (
        second["session"]["identity"]["binding_generation"]
        > first["session"]["identity"]["binding_generation"]
    )
    assert (await client.get("/api/auth/device/me", headers=headers(token))).status_code == 401
    assert (
        await client.post(
            "/api/auth/device/refresh", json={"refresh_token": first["refresh_token"]}
        )
    ).status_code == 403
    await client.post(
        "/api/users", json={"username": "bob", "password": PASSWORD}, headers=headers(account)
    )
    bob = (await login(client, "bob"))["session"]["access_token"]
    device_id = second["session"]["identity"]["device_id"]
    assert (
        await client.delete(f"/api/auth/devices/{device_id}", headers=headers(bob))
    ).status_code == 404
    assert (
        await client.delete(f"/api/auth/devices/{device_id}", headers=headers(account))
    ).status_code == 204
    assert (
        await client.post(
            "/api/auth/device/refresh", json={"refresh_token": second["refresh_token"]}
        )
    ).status_code == 403
    third = await bind(client, account)
    assert (
        await client.post("/api/auth/logout", headers=headers(third["session"]["access_token"]))
    ).status_code == 204
    assert (
        await client.post(
            "/api/auth/device/refresh", json={"refresh_token": third["refresh_token"]}
        )
    ).status_code == 403
    assert (await client.post("/api/auth/logout", headers=headers(account))).status_code == 204
    assert (await client.get("/api/auth/me", headers=headers(account))).status_code == 401


async def test_cloud_restart_renews_device_and_password_change_revokes_it(tmp_path):
    home = tmp_path / "cloud"

    async def run_once():
        app = create_cloud_app(CloudSettings(home=home))
        return app

    app = await run_once()
    async with app.router.lifespan_context(app):
        await asyncio.to_thread(app.state.cloud.identity.bootstrap, "admin", PASSWORD)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://cloud.test"
        ) as client:
            account = (await login(client))["session"]["access_token"]
            grant = await bind(client, account)
    app = await run_once()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://cloud.test"
        ) as client,
    ):
        renewed = await client.post(
            "/api/auth/device/refresh", json={"refresh_token": grant["refresh_token"]}
        )
        assert renewed.status_code == 200
        assert renewed.json()["identity"] == grant["session"]["identity"]
        response = await client.post(
            "/api/auth/password",
            json={"password": PASSWORD, "new_password": "Changed-password-8372"},
            headers=headers(account),
        )
        assert response.status_code == 204
        assert (
            await client.get("/api/auth/me", headers=headers(renewed.json()["access_token"]))
        ).status_code == 401
        assert (
            await client.post(
                "/api/auth/device/refresh", json={"refresh_token": grant["refresh_token"]}
            )
        ).status_code == 403
        assert (await login(client, password="Changed-password-8372"))["session"]


async def test_secret_validation_is_not_reflected_and_openapi_is_typed(cloud):
    _, client, account = cloud
    secret = "SECRET-DO-NOT-ECHO"
    response = await client.post("/api/auth/device/refresh", json={"refresh_token": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    extra = await client.post(
        "/api/auth/devices", json={"label": "x", "user_id": 999}, headers=headers(account)
    )
    assert extra.status_code == 422
    spec = (await client.get("/api/openapi.json")).json()
    for path, methods in spec["paths"].items():
        for operation in methods.values():
            assert operation["summary"], path
    assert spec["components"]["securitySchemes"]["HTTPBearer"]
    assert (await client.get("/api/docs")).status_code == 200


async def test_local_account_isolation_logout_restart_and_revocation(cloud, tmp_path):
    cloud_app, client, admin = cloud
    await client.post(
        "/api/users", json={"username": "bob", "password": PASSWORD}, headers=headers(admin)
    )
    bob = (await login(client, "bob"))["session"]["access_token"]
    grants = [await bind(client, admin), await bind(client, bob)]
    settings = LocalSettings(
        home=tmp_path / "local", cloud_url="https://cloud.test", api_token=LOCAL_TOKEN
    )

    @asynccontextmanager
    async def desktop():
        app = create_local_app(
            settings,
            model_factory=LocalModelFactory(),
            cloud_transport=httpx.ASGITransport(app=cloud_app),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as local,
        ):
            yield app, local

    async with desktop() as (app, local):
        assert (await local.put("/api/cloud/session", json=grants[0]["session"])).status_code == 401
        assert (
            await local.get("/api/agents", headers=headers(grants[0]["session"]["access_token"]))
        ).status_code == 401
        paths = []
        agents = []
        for grant in grants:
            response = await local.put(
                "/api/cloud/session", json=grant["session"], headers=LOCAL_HEADERS
            )
            assert response.status_code == 200, response.text
            assert (await local.get("/api/agents", headers=LOCAL_HEADERS)).json() == []
            response = await local.post(
                "/api/agents",
                json={"name": "Private", "model_id": "platform/test"},
                headers=LOCAL_HEADERS,
            )
            assert response.status_code == 201
            agents.append(response.json()["agent_id"])
            runtime = app.state.accounts.runtime
            paths.append(runtime.settings.home)
            # Future outbox stays in the same account partition through logout.
            (runtime.settings.home / "pending-example").write_text("pending", encoding="utf-8")
        assert paths[0] != paths[1]
        with pytest.raises(OctopError):
            await app.state.accounts.device_access(
                expected_identity=app.state.accounts.identity.model_copy(
                    update={
                        "user": app.state.accounts.identity.user.model_copy(update={"user_id": 999})
                    }
                )
            )
        assert (
            await local.post(f"/api/agents/{agents[0]}/threads", headers=LOCAL_HEADERS)
        ).status_code == 404
        assert (await local.delete("/api/cloud/session", headers=LOCAL_HEADERS)).status_code == 204
        assert (await local.get("/api/agents", headers=LOCAL_HEADERS)).status_code == 401
        assert all((p / "pending-example").exists() for p in paths)
    async with desktop() as (app, local):
        renewed = (
            await client.post(
                "/api/auth/device/refresh", json={"refresh_token": grants[0]["refresh_token"]}
            )
        ).json()
        assert (
            await local.put("/api/cloud/session", json=renewed, headers=LOCAL_HEADERS)
        ).status_code == 200
        rows = (await local.get("/api/agents", headers=LOCAL_HEADERS)).json()
        assert [row["agent_id"] for row in rows] == [agents[0]]
        assert (
            await app.state.accounts.device_access(expected_identity=app.state.accounts.identity)
            == renewed["access_token"]
        )
        await client.delete(
            f"/api/auth/devices/{renewed['identity']['device_id']}", headers=headers(admin)
        )
        with pytest.raises(OctopError):
            await app.state.accounts.device_access(expected_identity=app.state.accounts.identity)
        assert (await local.get("/api/agents", headers=LOCAL_HEADERS)).status_code == 401
        assert (
            await local.put("/api/cloud/session", json=renewed, headers=LOCAL_HEADERS)
        ).status_code == 403


async def test_expired_sessions_challenges_and_refresh_fail_closed(cloud):
    app, client, account = cloud
    grant = await bind(client, account)
    with app.state.cloud.db.transaction() as conn:
        conn.execute("UPDATE cloud_auth_sessions SET expires_at=0")
    assert (await client.get("/api/auth/me", headers=headers(account))).status_code == 401
    assert (
        await client.get("/api/auth/device/me", headers=headers(grant["session"]["access_token"]))
    ).status_code == 401
    renewed = await client.post(
        "/api/auth/device/refresh", json={"refresh_token": grant["refresh_token"]}
    )
    assert renewed.status_code == 200
    with app.state.cloud.db.transaction() as conn:
        conn.execute("UPDATE cloud_devices SET refresh_expires_at=0")
    assert (
        await client.get("/api/auth/device/me", headers=headers(renewed.json()["access_token"]))
    ).status_code == 403
    assert (
        await client.post(
            "/api/auth/device/refresh", json={"refresh_token": grant["refresh_token"]}
        )
    ).status_code == 403


async def test_concurrent_single_device_bind_is_atomic(cloud):
    _, client, account = cloud
    results = await asyncio.gather(
        *[
            client.post(
                "/api/auth/devices", json={"label": f"device-{index}"}, headers=headers(account)
            )
            for index in range(4)
        ]
    )
    assert sorted(result.status_code for result in results) == [201, 409, 409, 409]
    grants = await asyncio.gather(
        *[
            client.post(
                "/api/auth/devices",
                json={"label": f"replacement-{index}", "replace": True},
                headers=headers(account),
            )
            for index in range(4)
        ]
    )
    assert all(response.status_code == 201 for response in grants)
    checks = await asyncio.gather(
        *[
            client.get(
                "/api/auth/device/me", headers=headers(response.json()["session"]["access_token"])
            )
            for response in grants
        ]
    )
    assert sorted(response.status_code for response in checks) == [200, 401, 401, 401]


async def test_logout_interrupts_active_turn_and_old_websocket_cannot_switch_accounts(
    cloud, tmp_path
):
    cloud_app, client, account = cloud
    grant = await bind(client, account)
    factory = LocalModelFactory()
    app = create_local_app(
        LocalSettings(
            home=tmp_path / "sidecar", cloud_url="https://cloud.test", api_token=LOCAL_TOKEN
        ),
        model_factory=factory,
        cloud_transport=httpx.ASGITransport(app=cloud_app),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as local,
    ):
        assert (
            await local.put("/api/cloud/session", json=grant["session"], headers=LOCAL_HEADERS)
        ).status_code == 200
        agent = (
            await local.post(
                "/api/agents",
                json={"name": "Busy", "model_id": "platform/test"},
                headers=LOCAL_HEADERS,
            )
        ).json()["agent_id"]
        thread = (await local.post(f"/api/agents/{agent}/threads", headers=LOCAL_HEADERS)).json()[
            "thread_id"
        ]
        async with ws_connect(
            app, f"/api/agents/{agent}/threads/{thread}/ws", headers=LOCAL_HEADERS
        ) as ws:
            await ws.send_json({"type": "start", "text": "wait"})
            assert (await ws.receive_json())["type"] == "turn.started"
            await asyncio.wait_for(factory.model._started.wait(), 10)
            assert (
                await local.delete("/api/cloud/session", headers=LOCAL_HEADERS)
            ).status_code == 204
            while True:
                event = await ws.receive_json()
                if event["type"] == "turn.status":
                    assert event["data"]["status"] == "interrupted"
                    break
            await ws.send_json({"type": "start", "text": "wait"})
            assert (await ws.receive_json())["type"] == "error"


async def test_production_local_requires_device_and_keeps_models_unavailable(cloud, tmp_path):
    cloud_app, client, account = cloud
    grant = await bind(client, account)
    app = create_local_app(
        LocalSettings(
            home=tmp_path / "production-local",
            cloud_url="https://cloud.test",
            api_token=LOCAL_TOKEN,
        ),
        cloud_transport=httpx.ASGITransport(app=cloud_app),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as local,
    ):
        assert (await local.get("/api/agents", headers=LOCAL_HEADERS)).status_code == 401
        spoofed = {
            **grant["session"],
            "identity": {
                **grant["session"]["identity"],
                "user": {**grant["session"]["identity"]["user"], "user_id": 999},
            },
        }
        assert (
            await local.put("/api/cloud/session", json=spoofed, headers=LOCAL_HEADERS)
        ).status_code == 401
        assert (
            await local.put("/api/cloud/session", json=grant["session"], headers=LOCAL_HEADERS)
        ).status_code == 200
        response = await local.post(
            "/api/agents",
            json={"name": "No fallback", "model_id": "platform/model"},
            headers=LOCAL_HEADERS,
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PLATFORM_MODEL_UNAVAILABLE"
        assert not (tmp_path / "production-local" / "local.db").exists()
