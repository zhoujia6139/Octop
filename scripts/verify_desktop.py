"""Native macOS arm64 acceptance using the built Electron shell/portable payload.

Run from an Octop development checkout after pnpm build and portable packaging:
  uv run python scripts/verify_desktop.py

Uses isolated profiles and a temporary cloud with a deterministic supplier;
never calls a paid model. Verifies actual keychain behavior but does not simulate
or claim real OS sleep, other CPU architectures, or Windows/Linux installation.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from playwright.async_api import async_playwright
from tests.integration import test_platform_resources as resources
from tests.integration.test_platform_models import Supplier, definition, serve

from octop.api.applications.cloud import create_cloud_app
from octop.application_config import CloudSettings
from octop.infra.applications.cloud import CloudApplication
from octop.infra.utils.ulid import new_ulid

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "desktop/src/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
PAYLOAD = ROOT / "desktop/portable/release/Octop-darwin-arm64"
PASSWORD = "Native-test-83654"


async def main():
    if sys.platform != "darwin" or platform.machine().lower() != "arm64":
        raise SystemExit(
            "This acceptance script targets native macOS arm64. Use the documented platform checklist elsewhere."
        )
    with tempfile.TemporaryDirectory(prefix="octop-p7-native-") as directory:
        work = Path(directory)
        settings = CloudSettings(home=work / "cloud")
        await asyncio.to_thread(CloudApplication(settings).initialize_admin, "admin", PASSWORD)
        supplier = Supplier()
        supplier.mode = "tool"

        async def upstream(request):
            response = await supplier(request)
            content = await response.aread()
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=content.replace(b"note.txt", b"inbound/input.txt"),
            )

        app = create_cloud_app(settings, provider_transport=httpx.MockTransport(upstream))
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        bootstrap = work / "bootstrap.cjs"
        bootstrap.write_text(
            "const {app}=require('electron');app.setPath('userData',process.env.OCTOP_NATIVE_TEST_PROFILE);app.setVersion("
            + json.dumps(version)
            + ");require("
            + json.dumps(str(ROOT / "desktop/src/out/main/index.js"))
            + ");"
        )
        env = {
            **os.environ,
            "OCTOP_HOME": str(work / "data"),
            "OCTOP_NATIVE_TEST_PROFILE": str(work / "profile"),
            "OCTOP_PORTABLE_DIR": str(PAYLOAD),
            "OCTOP_DASHBOARD_DIR": str(PAYLOAD / "packages/octop/dashboard"),
            "LANG": "en_US.UTF-8",
        }
        env.pop("ELECTRON_RUN_AS_NODE", None)
        process = None
        async with serve(app, lifespan="on") as cloud_url, async_playwright() as driver:

            async def launch():
                nonlocal process
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                process = subprocess.Popen(
                    [
                        str(BINARY),
                        str(bootstrap),
                        "--remote-debugging-port=" + str(port),
                        "--lang=en-US",
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=(work / "electron.stderr").open("a"),
                )
                async with httpx.AsyncClient(trust_env=False) as client:
                    async with asyncio.timeout(30):
                        while True:
                            assert process.poll() is None, "Native Electron exited during startup"
                            try:
                                response = await client.get(f"http://127.0.0.1:{port}/json/version")
                                if response.status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(0.1)
                browser = await driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                async with asyncio.timeout(40):
                    while True:
                        pages = [
                            page
                            for context in browser.contexts
                            for page in context.pages
                            if page.url.startswith("octop://app")
                        ]
                        if pages:
                            return browser, pages[0]
                        await asyncio.sleep(0.1)

            async def stop():
                nonlocal process
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, 15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait)
                process = None

            try:
                browser, page = await launch()
                await page.wait_for_load_state("domcontentloaded")
                await page.evaluate("localStorage.setItem('octop:ui-locale','en')")
                await page.reload()
                await page.get_by_placeholder("https://cloud.example.com").fill(cloud_url)
                await page.get_by_role("button", name="Save cloud address", exact=True).click()
                await page.get_by_label("Username", exact=True).fill("admin")
                await page.get_by_label("Password", exact=True).fill(PASSWORD)
                await page.get_by_role("button", name="Sign in", exact=True).click()
                await page.get_by_role("menuitem", name="Chat", exact=True).wait_for(timeout=30000)
                assert (
                    await page.evaluate("fetch('octop://app/api/application').then(r=>r.json())")
                )["role"] == "local"
                assert (
                    await page.evaluate("fetch('octop://app/api/cloud/session').then(r=>r.status)")
                    == 403
                )
                assert (
                    await page.evaluate(
                        "fetch('octop://app/api/lifecycle/prepare',{method:'POST'}).then(r=>r.status)"
                    )
                    == 403
                )
                assert (
                    await page.evaluate("fetch('octop://app/api/agents').then(r=>r.status)") == 200
                )
                async with httpx.AsyncClient(base_url=cloud_url, trust_env=False) as admin:
                    session = (
                        await admin.post(
                            "/api/auth/login", json={"username": "admin", "password": PASSWORD}
                        )
                    ).json()["session"]
                    token = session["access_token"]
                    uid = session["identity"]["user"]["user_id"]
                    admin.headers["Authorization"] = "Bearer " + token

                    async def put(path, body, status=200):
                        response = await admin.put(path, json=body)
                        assert response.status_code == status, (path, response.status_code)

                    await put(
                        "/api/model-admin/providers",
                        {
                            "provider_id": "supplier",
                            "base_url": "https://supplier.test/v1",
                            "api_key": "native-dummy-key",
                            "free_rejection_statuses": [429],
                        },
                    )
                    model = definition()
                    model["input_token_ceiling"] = 128000
                    await put("/api/model-admin/models", model)
                    await put(f"/api/model-admin/users/{uid}/grants/test", {"allowed": True}, 204)
                    await put(
                        f"/api/model-admin/users/{uid}/budget",
                        {"currency": "USD", "limit_nanos": 10000000},
                    )
                    skill, resource = new_ulid(), new_ulid()
                    await resources.save(admin, token, skill, resources.skill_spec())
                    await resources.publish(admin, token, skill)
                    await resources.save(admin, token, resource, resources.agent_spec(skill))
                    await resources.publish(admin, token, resource)
                    await resources.assign(admin, token, uid, resource)
                    installed = await page.evaluate(
                        "fetch('octop://app/api/resources/sync',{method:'POST'}).then(r=>r.json())"
                    )
                    agent = next(
                        row["agent_id"] for row in installed if row["resource_id"] == resource
                    )
                    await page.evaluate(
                        "path=>fetch('octop://app'+path,{method:'PUT',body:'Native GUI tool evidence'}).then(r=>{if(!r.ok)throw Error(r.status);return r.json()})",
                        f"/api/agents/{agent}/files/inbound/input.txt",
                    )
                    await page.reload()
                    await page.get_by_role("button", name="New chat", exact=True).click(
                        timeout=30000
                    )
                    await page.get_by_label("Message", exact=True).fill("read inbound/input.txt")
                    await page.get_by_role("button", name="Send", exact=True).click()
                    await page.get_by_text("Tool completed:", exact=False).wait_for(timeout=30000)
                    assert "Native GUI tool evidence" in await page.locator("body").inner_text()
                    ledger = (await admin.get("/api/usage/requests")).json()
                    assert len(ledger) == len(supplier.calls) == 2 and all(
                        row["state"] == "settled" for row in ledger
                    )
                    synced = await page.evaluate(
                        "fetch('octop://app/api/sync/retry',{method:'POST'}).then(r=>r.json())"
                    )
                    assert synced["pending"] == synced["conflicts"] == 0
                await page.screenshot(path="/tmp/octop-p7-native-desktop.png")
                credential = work / "data/cloud-session/desktop-device.json"
                if credential.exists():
                    data = json.loads(credential.read_text())
                    assert data["encrypted"] is True and PASSWORD not in credential.read_text()
                    remembered = True
                else:
                    remembered = False
                await stop()
                browser, page = await launch()
                if remembered:
                    await page.get_by_role("menuitem", name="Chat", exact=True).wait_for(
                        timeout=30000
                    )
                    assert await page.get_by_label("Password", exact=True).count() == 0
                else:
                    await page.get_by_label("Password", exact=True).wait_for(timeout=30000)
                assert len(supplier.calls) == 2
                await stop()
                print(
                    "PASS: native Electron 45 arm64, real portable payload, cloud login, publication/install, UI WebSocket tool loop, settlement, sync, renderer proxy controls, graceful quit/restart without model replay. OS-keychain remembered credential:",
                    remembered,
                )
            finally:
                await stop()


if __name__ == "__main__":
    asyncio.run(main())
