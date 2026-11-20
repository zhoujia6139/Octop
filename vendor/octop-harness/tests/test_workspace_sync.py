"""Bounded local transfers must never traverse links or return partial scans."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from octop_harness.backends.workspace import BackendWorkspace


def test_bounded_transfer_stage_commit_and_fingerprint(tmp_path: Path) -> None:
    workspace = BackendWorkspace.for_local_directory(tmp_path)
    workspace.write_sync_chunk("inbound/a.bin", 0, b"abc")
    workspace.write_sync_chunk("inbound/a.bin", 3, b"def")
    entries = workspace.scan_sync_files(["inbound", "artifacts"])
    assert len(entries) == 1 and entries[0].size == 6
    assert workspace.read_sync_chunk(entries[0].path, 1, 3, entries[0].fingerprint) == b"bcd"
    workspace.write_sync_chunk(".octop/staging/file", 0, b"new")
    workspace.commit_sync_file(".octop/staging/file", "inbound/a.bin")
    assert workspace.read_sync_chunk("inbound/a.bin", 0, 10) == b"new"
    with pytest.raises(OSError, match="changed"):
        workspace.read_sync_chunk("inbound/a.bin", 0, 1, entries[0].fingerprint)
    workspace.remove_sync_file("inbound/a.bin")
    assert workspace.scan_sync_files(["inbound"]) == []


@pytest.mark.parametrize("path", ["../outside", "/root", "a/../file", "a\\b", "C:/file", "a//b", "a/./b", "a\x00b"])
def test_transfer_rejects_noncanonical_paths(tmp_path: Path, path: str) -> None:
    workspace = BackendWorkspace.for_local_directory(tmp_path)
    with pytest.raises(ValueError):
        workspace.read_sync_chunk(path, 0, 1)
    with pytest.raises(ValueError):
        workspace.write_sync_chunk(path, 0, b"bad")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and hard-link guarantees")
def test_never_reads_or_modifies_linked_external_files(tmp_path: Path) -> None:
    workspace = BackendWorkspace.for_local_directory(tmp_path / "workspace")
    external = tmp_path / "secret"
    external.write_bytes(b"secret")
    root = workspace.workspace_dir
    (root / "inbound").mkdir()
    (root / "inbound" / "link").symlink_to(external)
    (root / "inbound" / "directory").symlink_to(tmp_path, target_is_directory=True)
    os.link(external, root / "inbound" / "hardlink")
    assert workspace.scan_sync_files(["inbound"]) == []
    for name in ("link", "hardlink", "directory/secret"):
        with pytest.raises((OSError, PermissionError)):
            workspace.read_sync_chunk("inbound/" + name, 0, 10)
        with pytest.raises((OSError, PermissionError)):
            workspace.write_sync_chunk("inbound/" + name, 0, b"bad")
    assert external.read_bytes() == b"secret"


def test_transfer_limits_and_partial_enumeration_fail(tmp_path: Path) -> None:
    workspace = BackendWorkspace.for_local_directory(tmp_path)
    workspace.write_sync_chunk("inbound/a", 0, b"a")
    workspace.write_sync_chunk("inbound/b", 0, b"b")
    with pytest.raises(ValueError, match="limit"):
        workspace.scan_sync_files(["inbound"], limit=1)
    with pytest.raises(ValueError, match="bounded"):
        workspace.read_sync_chunk("inbound/a", 0, 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="bounded"):
        workspace.write_sync_chunk("inbound/a", 0, b"a" * (1024 * 1024 + 1))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink setup")
def test_replaced_workspace_root_and_ancestor_cannot_redirect_transfer(tmp_path: Path) -> None:
    root = tmp_path / "parent" / "workspace"
    workspace = BackendWorkspace.for_local_directory(root)
    workspace.write_sync_chunk("inbound/a", 0, b"original")
    external = tmp_path / "external"
    (external / "inbound").mkdir(parents=True)
    (external / "inbound" / "a").write_bytes(b"secret")
    root.rename(root.with_name("old-workspace"))
    root.symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        workspace.read_sync_chunk("inbound/a", 0, 10)
    with pytest.raises(OSError):
        BackendWorkspace.for_local_directory(root)
    root.unlink()
    parent = root.parent
    parent.rename(parent.with_name("old-parent"))
    parent.symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        workspace.scan_sync_files(["inbound"])
