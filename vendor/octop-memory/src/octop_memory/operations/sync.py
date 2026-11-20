"""Public logical namespace synchronization, excluding checkpoints and host state.

Changes are captured by a transactional comparison with a durable mirror. A
host persists the returned cursor only after its own outbox commits, so process
failure between capture and upload cannot lose a revision or a tombstone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryChange:
    cursor: int
    table: str
    key: list[str]
    revision: int
    data: dict[str, Any] | None


@dataclass(frozen=True)
class MemoryChanges:
    cursor: int
    has_more: bool
    changes: list[MemoryChange]


__all__ = ["MemoryChange", "MemoryChanges"]
