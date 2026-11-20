"""Cloud-only container startup keeps credentials out of command lines/files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX container entrypoint")
ROOT = Path(__file__).resolve().parents[2]


@posix_only
@pytest.mark.parametrize("bootstrap", [False, True])
def test_entrypoint_initializes_only_when_configured_then_scrubs_password(tmp_path, bootstrap):
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "commands.jsonl"
    for command in ("octop-cloud-init", "octop-cloud", "octop"):
        path = binary / command
        path.write_text(
            f'#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\nwith Path(os.environ["P6_COMMAND_LOG"]).open("a") as out: out.write(json.dumps({{"name":Path(sys.argv[0]).name,"args":sys.argv[1:],"bootstrap_present":bool(os.environ.get("OCTOP_BOOTSTRAP_PASSWORD"))}})+"\\n")\n'
        )
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "P6_COMMAND_LOG": str(log),
        "OCTOP_PORT": "9911",
        "OCTOP_ADMIN_USERNAME": "root-admin",
        "OCTOP_BOOTSTRAP_PASSWORD": "Only-ephemeral-9274" if bootstrap else "",
    }
    result = subprocess.run(
        [shutil.which("bash"), str(ROOT / "deploy/docker/docker-entrypoint.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    if bootstrap:
        assert commands[0] == {
            "name": "octop-cloud-init",
            "args": ["--if-empty", "--username", "root-admin"],
            "bootstrap_present": True,
        }
    assert commands[-1] == {
        "name": "octop-cloud",
        "args": ["--host", "0.0.0.0", "--port", "9911"],
        "bootstrap_present": False,
    }
    assert "Only-ephemeral" not in log.read_text() + result.stdout + result.stderr
    assert not (tmp_path / "credential.txt").exists()
