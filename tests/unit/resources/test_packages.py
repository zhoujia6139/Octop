"""Cross-platform package safety and content-integrity boundaries."""

import base64
import hashlib

import pytest
from pydantic import ValidationError

from octop.infra.resources.contracts import MAX_PACKAGE_BYTES, SkillSpec


def file(path, data=b"data"):
    return {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode(),
    }


def spec(files):
    return {
        "name": "Safe skill",
        "slug": "safe-skill",
        "files": [file("SKILL.md", b"# Read notes"), *files],
    }


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "C:/escape",
        "a/../../escape",
        "a\\escape",
        "a/.octop/token",
        ".octop-auth/token",
        ".env",
        "a/NUL.txt",
        "a/con",
        "a/trailing.",
        "a/trailing ",
        "a/file:stream",
        "a/\x00name",
        "a//file",
        "a/./file",
        "empty/",
    ],
)
def test_unsafe_or_nonportable_paths_are_rejected(path):
    with pytest.raises(ValidationError):
        SkillSpec.model_validate(spec([file(path)]))


@pytest.mark.parametrize(
    "files",
    [
        [file("skill.md")],
        [file("ref"), file("REF")],
        [file("ref"), file("ref/child")],
        [file("REF"), file("ref/child")],
        [file("references/é.txt"), file("references/e\u0301.txt")],
    ],
)
def test_case_collisions_and_file_directory_collisions_are_rejected(files):
    with pytest.raises(ValidationError):
        SkillSpec.model_validate(spec(files))


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "0" * 64},
        {"size": 999},
        {"content_base64": "not base64"},
        {"mode": "symlink", "target": "../secret"},
    ],
)
def test_digest_size_encoding_and_symlink_metadata_are_rejected(change):
    bad = {**file("readme.txt"), **change}
    with pytest.raises(ValidationError):
        SkillSpec.model_validate(spec([bad]))


def test_manifest_and_total_decoded_size_are_required():
    with pytest.raises(ValidationError):
        SkillSpec.model_validate(
            {"name": "No manifest", "slug": "test", "files": [file("README.md")]}
        )
    with pytest.raises(ValidationError):
        SkillSpec.model_validate(spec([file("large.bin", b"x" * MAX_PACKAGE_BYTES)]))


def test_binary_files_and_unicode_paths_round_trip():
    data = bytes(range(256))
    package = SkillSpec.model_validate(spec([file("references/说明.bin", data)]))
    assert SkillSpec.model_validate_json(package.model_dump_json()).files[-1].content() == data
