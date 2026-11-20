"""Verify an installed product in isolation, including a real CLI process.

Run with the target environment's Python, outside the source checkout. For a
portable tree pass --packages and --launcher to use its exact entry bootstrap.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import site
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("cloud", "local"))
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--launcher", type=Path)
    args = parser.parse_args()
    if args.packages:
        site.addsitedir(str(args.packages.resolve()))
    if args.role == "cloud":
        assert all(
            importlib.util.find_spec(name) is None
            for name in (
                "octop_harness",
                "octop_memory",
                "octop_browser",
                "octop_gateway",
                "langchain_core",
                "apscheduler",
            )
        ), "Cloud environment contains execution dependencies"
        from octop.api.applications.cloud import create_cloud_app

        assert callable(create_cloud_app)
    else:
        from octop.infra.sync.local import require_sync_runtime

        require_sync_runtime()
        assert importlib.util.find_spec("octop_gateway") is None
    forbidden = (
        "octop.infra.server",
        "octop.infra.agents.manager",
        "octop.infra.gateway.processor",
    )
    assert not any(name in sys.modules for name in forbidden), "Legacy composition imported"

    entry = (
        [sys.executable, str(args.launcher.resolve())]
        if args.launcher
        else [sys.executable, "-m", "octop"]
    )
    with tempfile.TemporaryDirectory(prefix="octop-install-") as directory:
        home = Path(directory)
        env = {**os.environ, "OCTOP_HOME": str(home), "PYTHONNOUSERSITE": "1"}
        # Never let the caller's provider, credentials or database leak into a smoke run.
        for name in list(env):
            if name.startswith("OCTOP_") and name != "OCTOP_HOME":
                del env[name]
        env["OCTOP_LOCAL_API_TOKEN"] = secrets.token_urlsafe(32)
        if args.packages:
            env["PYTHONPATH"] = str(args.packages.resolve())
        else:
            env.pop("PYTHONPATH", None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with (home / "process.log").open("w") as log:
            child = subprocess.Popen(
                [
                    *entry,
                    "run",
                    "--role",
                    "cloud" if args.role == "cloud" else "desktop",
                    "--port",
                    str(port),
                ],
                cwd=home,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 30
                while True:
                    assert child.poll() is None, "Installed CLI exited during startup"
                    try:
                        with opener.open(
                            f"http://127.0.0.1:{port}/api/health", timeout=1
                        ) as response:
                            assert json.load(response)["role"] == args.role
                            break
                    except (urllib.error.URLError, TimeoutError):
                        assert time.monotonic() < deadline, "Installed CLI readiness timed out"
                        time.sleep(0.05)
                path = "/api/agents" if args.role == "cloud" else "/api/cloud/session"
                try:
                    opener.open(f"http://127.0.0.1:{port}{path}", timeout=2)
                    raise AssertionError("Forbidden/unauthenticated route succeeded")
                except urllib.error.HTTPError as error:
                    assert error.code == (404 if args.role == "cloud" else 401)
                if args.role == "cloud":
                    with opener.open(f"http://127.0.0.1:{port}/", timeout=2) as response:
                        assert 'id="root"' in response.read().decode(), "Wheel has no product UI"
            finally:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
    print(f"PASS: installed {args.role} dependency boundary and CLI readiness")


if __name__ == "__main__":
    main()
