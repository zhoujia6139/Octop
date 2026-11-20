"""tests/unit/test_release_download_links.py"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_download_links.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_download_links", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_split_tag_accepts_with_or_without_v() -> None:
    mod = _load()
    assert mod.split_tag("0.9.31") == ("v0.9.31", "0.9.31")
    assert mod.split_tag("v0.9.31") == ("v0.9.31", "0.9.31")


def test_render_download_section_matches_github_asset_names() -> None:
    mod = _load()
    body = mod.render_download_section("0.9.31")
    base = "https://github.com/TencentCloud/Octop/releases/download/v0.9.31"
    assert f"{base}/Octop-desktop-windows-amd64-0.9.31.exe" in body
    assert f"{base}/Octop-desktop-windows-arm64-0.9.31.exe" in body
    assert f"{base}/Octop-portable-windows-amd64-0.9.31.zip" in body
    assert f"{base}/Octop-portable-windows-arm64-0.9.31.zip" in body
    assert f"{base}/Octop-desktop-darwin-arm64-0.9.31.dmg" in body
    assert f"{base}/Octop-desktop-darwin-amd64-0.9.31.dmg" in body
    assert f"{base}/Octop-portable-darwin-arm64-0.9.31.zip" in body
    assert f"{base}/Octop-portable-darwin-amd64-0.9.31.zip" in body
    # Linux ships deb (default) and rpm.
    for arch in ("amd64", "arm64"):
        for ext in ("deb", "rpm"):
            assert f"{base}/Octop-desktop-linux-{arch}-0.9.31.{ext}" in body
    assert "AppImage" not in body
    assert f"{base}/Octop-portable-linux-amd64-0.9.31.zip" in body
    assert f"{base}/Octop-portable-linux-arm64-0.9.31.zip" in body
    assert "Octop-fnos-" not in body
    assert "ghcr.io/tencentcloud/octop:0.9.31` (`:latest`)" in body
    assert "0.9.31-full" not in body
    assert "local Agent runtime" in body
    assert "pip install" not in body
    assert "## Downloads" in body


def test_patch_release_body_removes_legacy_fnos_without_erasing_cloud():
    mod = _load()
    body = (
        mod.render_download_section("1.0.3b7")
        + "\n### fnOS NAS\n- retired.fpk\n\n## What's Changed\n- kept\n"
    )
    patched = mod.patch_release_body(body, "1.0.3b7", [])
    assert "retired.fpk" not in patched
    assert "### fnOS NAS" not in patched
    assert "ghcr.io/tencentcloud/octop:1.0.3b7" in patched
    assert "## What's Changed" in patched
    assert "- kept" in patched


def test_download_links_follow_the_fork(monkeypatch) -> None:
    # GitHub Actions sets GITHUB_REPOSITORY, so a fork's release notes must point
    # at the fork, not at upstream.
    monkeypatch.setenv("GITHUB_REPOSITORY", "liukewia/Octop")
    mod = _load()
    body = mod.render_download_section("1.0.3b1")
    assert "https://github.com/liukewia/Octop/releases/download/v1.0.3b1/" in body
    assert "https://github.com/TencentCloud/Octop/" not in body
