"""P6 cloud image smoke; run against an explicitly built image (P7 packaging)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from tests.unit.test_docker_image_arch import normalized_cpu_arch

IMAGE = os.environ.get("OCTOP_TEST_IMAGE", "octop:test")


def _run(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return _run(["docker", "image", "inspect", IMAGE]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


docker_live = pytest.mark.skipif(
    not _available(), reason="requires an explicitly built cloud image"
)


def _http(name: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    script = """
import json, sys, urllib.request, urllib.error
request = urllib.request.Request('http://127.0.0.1:8088'+sys.argv[1],data=sys.argv[2].encode() if sys.argv[2] else None,headers={'Content-Type':'application/json'})
try:
 response = urllib.request.urlopen(request, timeout=3)
except urllib.error.HTTPError as error:
 response = error
print(json.dumps({'status':response.status,'body':response.read().decode()}))
"""
    result = _run(
        [
            "docker",
            "exec",
            name,
            "/app/.venv/bin/python",
            "-c",
            script,
            path,
            json.dumps(body) if body else "",
        ]
    )
    if result.returncode:
        return 0, None
    value = json.loads(result.stdout)
    try:
        payload = json.loads(value["body"])
    except ValueError:
        payload = value["body"]
    return value["status"], payload


@pytest.fixture
def docker_boot() -> Iterator[Any]:
    name = "octop-p6-image-" + uuid.uuid4().hex[:10]

    def start(password: str | None):
        command = ["docker", "run", "-d", "--name", name]
        if password:
            command.extend(["-e", "OCTOP_BOOTSTRAP_PASSWORD=" + password])
        result = _run([*command, IMAGE])
        assert result.returncode == 0, result.stderr
        for _ in range(90):
            if _http(name, "/api/health")[0] == 200:
                return name
            time.sleep(0.5)
        raise AssertionError("cloud image did not become ready")

    try:
        yield start
    finally:
        _run(["docker", "rm", "-f", name])


@pytest.mark.slow
@docker_live
def test_docker_valid_password_creates_admin_and_serves(docker_boot):
    name = docker_boot("Cloud-image-74913")
    status, login = _http(
        name, "/api/auth/login", {"username": "admin", "password": "Cloud-image-74913"}
    )
    assert status == 200
    assert login["session"]["identity"]["user"]["role"] == "admin"
    assert _http(name, "/")[0] == 200
    assert _http(name, "/api/setup/status")[0] == 404


@pytest.mark.live
@pytest.mark.slow
@docker_live
def test_docker_image_runs_on_this_cpu_and_stays_disabled(docker_boot):
    """Retained CI selector now verifies execution components are entirely absent."""
    name = docker_boot(None)
    guest = _run(["docker", "exec", name, "uname", "-m"])
    assert normalized_cpu_arch(guest.stdout) == normalized_cpu_arch(platform.machine())
    status, health = _http(name, "/api/health")
    assert status == 200 and health["role"] == "cloud"
    for path in (
        "/api/agents",
        "/api/chats",
        "/api/terminal",
        "/api/browser",
        "/api/cron/jobs",
        "/chat",
    ):
        assert _http(name, path)[0] == 404
    check = _run(
        [
            "docker",
            "exec",
            name,
            "/app/.venv/bin/python",
            "-c",
            "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('octop_harness','octop_memory','octop_gateway','octop_browser'))",
        ]
    )
    assert check.returncode == 0, check.stderr
