"""Versioned backup records and bounded, resumable content transfer contracts."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from octop.infra.applications.contracts import PublicId

CHUNK_BYTES = 1024 * 1024
FILE_BYTES = 256 * CHUNK_BYTES
STORAGE_BYTES = 2 * 1024 * CHUNK_BYTES
METADATA_BYTES = 128 * CHUNK_BYTES
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Kind = Literal["thread", "message", "file", "memory"]
Status = Literal["running", "awaiting_approval", "completed", "interrupted", "cancelled", "failed"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def sync_path(value: str) -> str:
    parts = PurePosixPath(value).parts
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *[f"com{i}" for i in range(1, 10)],
        *[f"lpt{i}" for i in range(1, 10)],
    }
    if (
        not value
        or len(value) > 500
        or PurePosixPath(value).is_absolute()
        or PurePosixPath(value).as_posix() != value
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(
            p in {".", ".."}
            or p.rstrip(". ") != p
            or any(c in p for c in '<>:"|?*')
            or any(ord(c) < 32 for c in p)
            or p.split(".", 1)[0].casefold() in reserved
            for p in parts
        )
    ):
        raise ValueError("canonical portable relative paths required")
    if parts[0] not in {"inbound", "artifacts"} or len(parts) < 2:
        raise ValueError("only inbound/ and artifacts/ content is synchronized")
    excluded = {"node_modules", "__pycache__", "cache", "tmp", "temp", "profile", "profiles"}
    if any(p.startswith(".") or p.casefold() in excluded for p in parts):
        raise ValueError("system, hidden, cache and profile paths are excluded")
    if any(
        p.casefold().endswith((".db", ".sqlite", ".sqlite3", ".lock", ".tmp", ".pem", ".key"))
        for p in parts
    ):
        raise ValueError("databases, credentials and temporary files are excluded")
    return value


class ThreadData(Contract):
    kind: Literal["thread"] = "thread"
    title: str = Field(default="", max_length=200)
    created_at: int = Field(default=0, ge=0)
    updated_at: int = Field(default=0, ge=0)
    status: Status = "completed"


class Attachment(Contract):
    file_id: PublicId
    sha256: Digest


class MessageData(Contract):
    kind: Literal["message"] = "message"
    thread_id: PublicId
    turn_id: PublicId
    position: int = Field(ge=0)
    created_at: int = Field(default=0, ge=0)
    role: Literal["user", "assistant", "tool"]
    source_id: str | None = Field(default=None, max_length=200)
    content: str | list[dict[str, Any]]
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    status: Status = "completed"


class FileData(Contract):
    kind: Literal["file"] = "file"
    path: str
    sha256: Digest
    size: int = Field(ge=0, le=FILE_BYTES)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return sync_path(value)


class MemoryData(Contract):
    kind: Literal["memory"] = "memory"
    table: Literal[
        "entities",
        "raw_events",
        "candidates",
        "atoms",
        "memory_nodes",
        "aliases",
        "entity_pages",
        "thread_active_entities",
        "episodes",
        "digests",
        "journal",
    ]
    key: list[str] = Field(min_length=1, max_length=3)
    record: dict[str, Any]


Data = Annotated[ThreadData | MessageData | FileData | MemoryData, Field(discriminator="kind")]


class Mutation(Contract):
    protocol_version: Literal[1] = 1
    operation_id: PublicId
    item_id: PublicId
    agent_id: PublicId
    resource_id: PublicId
    kind: Kind
    expected_revision: int = Field(ge=0)
    data: Data | None

    @model_validator(mode="after")
    def valid_content(self) -> Self:
        if self.data is not None and self.data.kind != self.kind:
            raise ValueError("record kind mismatch")
        if len(self.model_dump_json().encode()) > CHUNK_BYTES:
            raise ValueError("backup record exceeds 1 MiB")
        return self


class Record(Contract):
    item_id: PublicId
    agent_id: PublicId
    resource_id: PublicId
    kind: Kind
    revision: int = Field(ge=1)
    cursor: int = Field(ge=1)
    digest: Digest
    data: Data | None


class Page(Contract):
    watermark: int
    cursor: int
    has_more: bool
    records: list[Record]


class BlobInput(Contract):
    sha256: Digest
    size: int = Field(ge=0, le=FILE_BYTES)


class BlobState(BlobInput):
    offsets: list[int]
    complete: bool


class SyncState(Contract):
    pending: int
    conflicts: int
    last_error: str | None = None
    scan_generation: int = 0


class RestoreResult(Contract):
    watermark: int
    restored: int
    skipped: int


class LocalRecord(Contract):
    item_id: PublicId
    agent_id: PublicId
    resource_id: PublicId
    kind: Kind
    revision: int
    cloud_revision: int
    digest: Digest
    data: Data | None
    state: Literal["pending", "synced", "conflict"]
    error_code: str | None = None


def digest(data: Any) -> str:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
