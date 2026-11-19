"""Versioned Agent/skill contracts; no runtime or persistence dependencies."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from octop.infra.applications.contracts import PublicId
from octop.infra.skills.skill_packages import normalize_skill_files

MAX_PACKAGE_BYTES = 4 * 1024 * 1024
PERSONAL_RESOURCE_LIMIT = 100
PERSONAL_STORAGE_BYTES = 64 * 1024 * 1024


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillFile(Contract):
    path: str = Field(min_length=1, max_length=500)
    content_base64: str = Field(max_length=6 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0, le=MAX_PACKAGE_BYTES)

    def content(self) -> bytes:
        data = base64.b64decode(self.content_base64, validate=True)
        if len(data) != self.size or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("content digest or size mismatch")
        return data


class SkillSpec(Contract):
    kind: Literal["skill"] = "skill"
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    files: list[SkillFile] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_files(self) -> SkillSpec:
        pairs = [(f.path, f.content()) for f in self.files]
        normalized = normalize_skill_files(pairs)
        if sum(len(data) for _, data in normalized) > MAX_PACKAGE_BYTES:
            raise ValueError("resource package exceeds 4 MiB")
        seen: set[str] = set()
        reserved = {
            "con",
            "prn",
            "aux",
            "nul",
            *[f"com{i}" for i in range(1, 10)],
            *[f"lpt{i}" for i in range(1, 10)],
        }
        for original, (path, _) in zip(self.files, normalized, strict=True):
            # Match on every supported OS and exclude system data. Directory
            # entries and symlink metadata are deliberately absent in this API.
            if path != original.path or path.endswith("/"):
                raise ValueError("canonical file paths required")
            for part in PurePosixPath(path).parts:
                if (
                    part.startswith(".")
                    or part.rstrip(". ") != part
                    or any(c in part for c in '<>:"|?*')
                    or any(ord(c) < 32 for c in part)
                    or part.split(".", 1)[0].lower() in reserved
                ):
                    raise ValueError("unsupported package path")
            key = unicodedata.normalize("NFC", path).casefold()
            if key in seen:
                raise ValueError("case-insensitive duplicate path")
            seen.add(key)
        if any(
            parent.as_posix().casefold() in seen
            for path in seen
            for parent in PurePosixPath(path).parents
            if parent != PurePosixPath(".")
        ):
            raise ValueError("file/directory collision")
        return self


class SkillRef(Contract):
    resource_id: PublicId
    revision: int = Field(ge=1)


class AgentSpec(Contract):
    kind: Literal["agent"] = "agent"
    name: str = Field(min_length=1, max_length=200)
    model_id: str = Field(pattern=r"^platform/[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
    system_prompt: str | None = Field(default=None, max_length=100_000)
    skills: list[SkillRef] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_skills(self) -> AgentSpec:
        if len({s.resource_id for s in self.skills}) != len(self.skills):
            raise ValueError("duplicate skill reference")
        return self


Spec = Annotated[AgentSpec | SkillSpec, Field(discriminator="kind")]


class Mutation(Contract):
    operation_id: PublicId
    expected_revision: int = Field(ge=0)
    spec: Spec | None = Field(
        description="Null creates a deletion tombstone; existing published versions remain immutable."
    )
    source: SkillRef | None = Field(
        default=None,
        description="Creation-only provenance, checked against an accessible platform version.",
    )


class CopyInput(Contract):
    resource_id: PublicId = Field(description="Stable ID of the new personal copy.")
    revision: int = Field(ge=1)
    operation_id: PublicId


class RevisionInput(Contract):
    revision: int = Field(ge=1)


class AssignmentInput(Contract):
    revision: int | None = Field(
        default=None, ge=1, description="Null withdraws the device assignment."
    )


class ResourceView(Contract):
    resource_id: PublicId
    scope: Literal["platform", "personal"]
    kind: Literal["agent", "skill"]
    owner_id: int | None
    revision: int
    published_revision: int | None = None
    source_id: PublicId | None = None
    source_revision: int | None = None
    deleted: bool = False


class VersionView(Contract):
    resource: ResourceView
    revision: int
    spec: Spec | None
    digest: str
    published: bool


class Change(Contract):
    cursor: int
    resource_id: PublicId
    revision: int | None
    resource: ResourceView


class ChangePage(Contract):
    cursor: int
    changes: list[Change]
    has_more: bool


class InstallReport(Contract):
    resource_id: PublicId
    desired_revision: int | None
    installed_revision: int | None
    state: Literal["installed", "pending", "failed", "removed"]
    error_code: str | None = Field(default=None, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$")
    runtime_state: Literal["stopped", "idle", "running", "awaiting_approval"] = "stopped"


class DeviceReport(InstallReport):
    device_id: str
    generation: int
    user_id: int
    reported_at: int


class LocalResourceView(Contract):
    resource_id: PublicId
    agent_id: PublicId | None = Field(
        default=None,
        description="Account-local running instance ID, distinct from a shared platform definition.",
    )
    scope: Literal["platform", "personal"]
    kind: Literal["agent", "skill"]
    revision: int
    desired_revision: int | None
    installed_revision: int | None
    spec: Spec | None
    source_id: str | None = None
    source_revision: int | None = None
    sync_state: Literal["pending", "synced", "conflict"]
    install_state: Literal["installed", "pending", "failed", "removed"]
    error_code: str | None = None


def digest_spec(spec: AgentSpec | SkillSpec | None) -> str:
    raw = json.dumps(
        spec.model_dump(mode="json") if spec else None,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()
