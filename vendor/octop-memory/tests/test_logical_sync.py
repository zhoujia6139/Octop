"""Namespace revisions, deletions, full logical coverage, validation and indexes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_backend_parity import (
    _make_digest,
    _make_episode,
)
from test_backend_parity import (
    backend as _backend_fixture,
)

from octop_memory import Memory

backend = _backend_fixture


def test_logical_capture_is_durable_and_tracks_updates_deletes(backend: Any) -> None:
    memory = Memory(namespace="logical-sync", backend=backend)
    memory.store("Logical searchable memory evidence")
    first = memory.sync_changes(limit=1000)
    assert {entry.table for entry in first.changes} >= {"atoms", "memory_nodes", "entities", "raw_events", "journal"}
    assert memory.sync_changes(first.cursor).changes == []
    replay = Memory(namespace="logical-sync", backend=backend).sync_changes()
    assert replay.changes == first.changes
    atom = memory.list_atoms()[0]
    memory.deprecate_atom(atom.id)
    update = memory.sync_changes(first.cursor)
    assert any(entry.table == "atoms" and entry.revision == 2 for entry in update.changes)
    memory.delete_journal(
        actions=[entry.action for entry in memory.list_journal()],
        before=datetime.now(UTC) + timedelta(days=1),
        limit=1000,
    )
    deleted = memory.sync_changes(update.cursor)
    assert any(entry.table == "journal" and entry.data is None for entry in deleted.changes)
    assert Memory(namespace="logical-sync", backend=backend).sync_changes(update.cursor).changes == deleted.changes


def test_logical_restore_roundtrip_and_atomic_validation(backend: Any) -> None:
    memory = Memory(namespace="logical-sync", backend=backend)
    memory.store("Unique searchable logical snapshot")
    snapshot = memory.sync_changes(limit=1000)
    memory.restore_sync_changes(snapshot.changes)
    assert len(memory.list_atoms()) == 1
    assert memory.search_atoms("searchable")
    changed = replace(snapshot.changes[0], data={"injected_column": "bad"})
    with pytest.raises(ValueError, match="schema"):
        memory.restore_sync_changes([*snapshot.changes, changed])
    assert len(memory.list_atoms()) == 1
    assert memory.sync_changes(snapshot.cursor).changes == []
    with pytest.raises(ValueError, match="invalid"):
        memory.restore_sync_changes([replace(snapshot.changes[0], table="checkpoints")])
    # Checkpoint/metadata tables do not appear even after they are created.
    memory.set_meta("credential-like-host-state", "must-stay-local")
    assert not any(entry.table in {"meta", "checkpoints", "writes"} for entry in memory.sync_changes().changes)


def test_logical_cursor_pages_and_bad_inputs(backend: Any) -> None:
    memory = Memory(namespace="logical-sync", backend=backend)
    memory.store("One logical memory")
    cursor, entries = 0, []
    while True:
        page = memory.sync_changes(cursor, limit=1)
        entries.extend(page.changes)
        assert page.cursor > cursor
        cursor = page.cursor
        if not page.has_more:
            break
    assert len(entries) >= 5
    with pytest.raises(ValueError):
        memory.sync_changes(-1)
    with pytest.raises(ValueError):
        memory.sync_changes(limit=1001)


def test_all_logical_types_restore_into_fresh_store_and_rebuild_search(backend: Any, tmp_path: Path) -> None:
    source = Memory(namespace="source", backend=backend)
    source.store("Portable searchable atom")
    source.add_episodes([_make_episode(summary="Portable searchable episode")])
    source.upsert_digest(_make_digest(markdown="Portable digest"))
    captured = source.sync_changes(limit=1000)
    assert {change.table for change in captured.changes} >= {"episodes", "digests"}
    target = Memory(namespace="destination", backend_config={"db_path": str(tmp_path / "destination.sqlite")})
    try:
        target.restore_sync_changes(captured.changes)
        assert target.search_atoms("searchable")
        assert target.search_episodes("searchable")
        assert target.list_digests()[0].markdown == "Portable digest"
        assert len(target.get_tree()) == len(source.get_tree())
        episode_change = next(change for change in captured.changes if change.table == "episodes")
        target.restore_sync_changes([replace(episode_change, revision=2, data=None)])
        assert not target.list_episodes()
        assert source.list_episodes()
    finally:
        target.close()
