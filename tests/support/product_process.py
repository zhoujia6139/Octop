"""Real CLI processes for product acceptance; no injected application factories."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx


class ProductProcess:
    def __init__(self, home: Path, role: str, *, env: dict[str, str] | None = None):
        self.home, self.role = home, role
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ, "OCTOP_HOME": str(home.parent), **(env or {})}
        self.process = None
        self.log = None

    async def start(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.log = (self.home / "acceptance.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "octop",
                "run",
                "--role",
                "desktop" if self.role == "local" else "cloud",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            env={**self.env, "OCTOP_HOME": str(self.home.parent)},
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(30), httpx.AsyncClient(trust_env=False) as client:
                while True:
                    assert self.process.poll() is None, "Product exited; inspect acceptance.log"
                    try:
                        response = await client.get(self.url + "/api/health", timeout=0.5)
                        if response.status_code == 200 and response.json()["role"] == self.role:
                            return self
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self.process is not None:
            child, self.process = self.process, None
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.get_running_loop().run_in_executor(None, child.wait, 10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.get_running_loop().run_in_executor(None, child.wait)
        if self.log:
            self.log.close()
            self.log = None
