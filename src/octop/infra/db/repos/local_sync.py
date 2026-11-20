"""Local backup projection, immutable upload payloads and durable revision queue."""

from __future__ import annotations

import builtins
import json
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts
from octop.infra.utils.ulid import new_ulid


class LocalSyncRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def state(self, key: str, value: str | None = None) -> str:
        with self.db.transaction() as conn:
            if value is not None:
                conn.execute(
                    "INSERT INTO local_sync_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            row = conn.execute("SELECT value FROM local_sync_state WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else ""

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_sync_items WHERE item_id=?", (item_id,)
            ).fetchone()
            return dict(row) if row else None

    def list(
        self, agent_id: str | None = None, kind: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        with self.db.connect() as conn:
            conditions, args = [], []
            if agent_id is not None:
                conditions.append("agent_id=?")
                args.append(agent_id)
            if kind is not None:
                conditions.append("kind=?")
                args.append(kind)
            clause = " WHERE " + " AND ".join(conditions) if conditions else ""
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT *, (SELECT error FROM local_sync_outbox o WHERE o.item_id=local_sync_items.item_id AND o.state='conflict' ORDER BY o.id DESC LIMIT 1) AS error_code FROM local_sync_items"
                    + clause
                    + " ORDER BY id",
                    args,
                ).fetchall()
            ]

    @staticmethod
    def enqueue(
        conn: Any, item_id: str, agent_id: str, resource_id: str, kind: str, data: str, digest: str
    ) -> bool:
        row = conn.execute(
            "SELECT revision,cloud_revision,digest,data FROM local_sync_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row and (row["digest"] == digest or row["data"] == "null"):
            return False
        pending = (
            conn.execute(
                "SELECT operation_id FROM local_sync_outbox WHERE item_id=? AND revision=? AND state='pending'",
                (item_id, row["revision"]),
            ).fetchone()
            if row
            else None
        )
        # Unsent streaming updates share one pending snapshot. Once a request
        # is claimed, its body is immutable even if the response is lost.
        revision = int(row["revision"]) if pending else int(row["revision"]) + 1 if row else 1
        operation_id = pending["operation_id"] if pending else new_ulid()
        body = {
            "protocol_version": 1,
            "operation_id": operation_id,
            "item_id": item_id,
            "agent_id": agent_id,
            "resource_id": resource_id,
            "kind": kind,
            "expected_revision": revision - 1,
            "data": json.loads(data),
        }
        state = (
            "conflict"
            if conn.execute(
                "SELECT 1 FROM local_sync_outbox WHERE item_id=? AND state='conflict'", (item_id,)
            ).fetchone()
            else "pending"
        )
        conn.execute(
            "INSERT INTO local_sync_items(item_id,agent_id,resource_id,kind,revision,digest,data,state) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,data=excluded.data,state=excluded.state",
            (item_id, agent_id, resource_id, kind, revision, digest, data, state),
        )
        conn.execute(
            "INSERT INTO local_sync_outbox(operation_id,item_id,revision,mutation) VALUES (?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET mutation=excluded.mutation",
            (operation_id, item_id, revision, json.dumps(body)),
        )
        return True

    def save(
        self, item_id: str, agent_id: str, resource_id: str, kind: str, data: str, digest: str
    ) -> None:
        with self.db.transaction() as conn:
            self.enqueue(conn, item_id, agent_id, resource_id, kind, data, digest)

    def pending(self) -> builtins.list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM local_sync_outbox WHERE state IN ('pending','sending') AND item_id NOT IN (SELECT item_id FROM local_sync_outbox WHERE state='conflict') ORDER BY id"
                ).fetchall()
            ]

    def claim(self, operation_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM local_sync_outbox WHERE operation_id=? AND state IN ('pending','sending')",
                (operation_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE local_sync_outbox SET state='sending' WHERE operation_id=?", (operation_id,)
            )
            return dict(row)

    def changed_binding(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_sync_outbox SET state='conflict',error='SYNC_CONFLICT' WHERE state IN ('pending','sending')"
            )
            conn.execute("UPDATE local_sync_items SET state='conflict' WHERE state='pending'")

    def discard_abandoned_chunks(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM local_sync_chunks WHERE digest LIKE 'staged-%'")

    def restoring(self) -> builtins.list[str]:
        with self.db.connect() as conn:
            return [
                row[0].removeprefix("restore_agent:")
                for row in conn.execute(
                    "SELECT key FROM local_sync_state WHERE key LIKE 'restore_agent:%' AND value<>''"
                ).fetchall()
            ]

    def acknowledge(self, operation_id: str, item_id: str, revision: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_sync_outbox SET state='done',error=NULL WHERE operation_id=?",
                (operation_id,),
            )
            conn.execute(
                "UPDATE local_sync_items SET cloud_revision=?,state=CASE WHEN revision=? THEN 'synced' ELSE state END WHERE item_id=?",
                (revision, revision, item_id),
            )

    def conflict(self, operation_id: str, item_id: str, error: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_sync_outbox SET state='conflict',error=? WHERE operation_id=?",
                (error, operation_id),
            )
            conn.execute("UPDATE local_sync_items SET state='conflict' WHERE item_id=?", (item_id,))

    def chunk(self, checksum: str, offset: int, content: bytes | None = None) -> bytes | None:
        with self.db.transaction() as conn:
            if content is not None:
                conn.execute(
                    "INSERT INTO local_sync_chunks(digest,offset_bytes,content) VALUES (?,?,?) ON CONFLICT(digest,offset_bytes) DO NOTHING",
                    (checksum, offset, content),
                )
            row = conn.execute(
                "SELECT content FROM local_sync_chunks WHERE digest=? AND offset_bytes=?",
                (checksum, offset),
            ).fetchone()
            return bytes(row[0]) if row else None

    def blob_size(self, checksum: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(length(content)),0) FROM local_sync_chunks WHERE digest=?",
                (checksum,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def finish_chunks(self, staged: str, checksum: str | None) -> None:
        with self.db.transaction() as conn:
            if checksum:
                conn.execute(
                    "INSERT INTO local_sync_chunks(digest,offset_bytes,content) SELECT ?,offset_bytes,content FROM local_sync_chunks WHERE digest=? ON CONFLICT(digest,offset_bytes) DO NOTHING",
                    (checksum, staged),
                )
            conn.execute("DELETE FROM local_sync_chunks WHERE digest=?", (staged,))

    def remove_chunks(self, checksum: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM local_sync_chunks WHERE digest=?", (checksum,))

    def restore_agent(self, agent_id: str, resource_id: str) -> None:
        # The service has verified the installed definition and paused runtime
        # instances. Preserve the original instance IDs in consumption contexts.
        with self.db.transaction() as conn:
            resource = conn.execute(
                "SELECT agent_id FROM local_resources WHERE resource_id=? AND installed_snapshot IS NOT NULL AND kind='agent'",
                (resource_id,),
            ).fetchone()
            if not resource:
                return
            current = resource["agent_id"]
            row = conn.execute("SELECT * FROM local_agents WHERE agent_id=?", (current,)).fetchone()
            if row and current != agent_id:
                conn.execute(
                    "INSERT INTO local_agents(agent_id,name,model_id,system_prompt,created_at,enabled,resource_snapshot,skills_path,resource_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO NOTHING",
                    (
                        agent_id,
                        row["name"],
                        row["model_id"],
                        row["system_prompt"],
                        row["created_at"],
                        row["enabled"],
                        row["resource_snapshot"],
                        row["skills_path"],
                        resource_id,
                    ),
                )
                conn.execute("UPDATE local_agents SET enabled=0 WHERE agent_id=?", (current,))
                conn.execute(
                    "UPDATE local_resources SET agent_id=? WHERE resource_id=?",
                    (agent_id, resource_id),
                )

    def restore_archive_agent(self, agent_id: str, resource_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_agents(agent_id,name,model_id,created_at,enabled) VALUES (?,?,'platform/unavailable',?,0) ON CONFLICT(agent_id) DO NOTHING",
                (agent_id, resource_id, now_ts()),
            )

    def source(self, agent_id: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT resource_id FROM local_sync_items WHERE agent_id=? ORDER BY id LIMIT 1",
                (agent_id,),
            ).fetchone()
            return str(row[0]) if row else None

    def batch(
        self,
        changes: builtins.list[tuple[str, str, str, str, str, str]],
        *,
        thread: tuple[str, str] | None = None,
        turn: tuple[str, str, str, bool] | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            if thread:
                conn.execute(
                    "INSERT INTO local_threads(thread_id,agent_id,created_at) VALUES (?,?,?)",
                    (*thread, now_ts()),
                )
            if turn:
                turn_id, thread_id, status, create = turn
                if create:
                    conn.execute(
                        "INSERT INTO local_turns(turn_id,thread_id,status,started_at) VALUES (?,?,?,?)",
                        (turn_id, thread_id, status, now_ts()),
                    )
                else:
                    conn.execute(
                        "UPDATE local_turns SET status=?,finished_at=CASE WHEN ? IN ('running','awaiting_approval') THEN NULL ELSE ? END WHERE turn_id=?",
                        (status, status, now_ts(), turn_id),
                    )
            for change in changes:
                self.enqueue(conn, *change)

    def restore(self, records: builtins.list[dict[str, Any]]) -> None:
        with self.db.transaction() as conn:
            for record in records:
                data = json.dumps(record["data"])
                conn.execute(
                    "INSERT INTO local_sync_items(item_id,agent_id,resource_id,kind,revision,cloud_revision,digest,data,state) VALUES (?,?,?,?,?,?,?,?,'synced') ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,cloud_revision=excluded.cloud_revision,digest=excluded.digest,data=excluded.data,state='synced'",
                    (
                        record["item_id"],
                        record["agent_id"],
                        record["resource_id"],
                        record["kind"],
                        record["revision"],
                        record["revision"],
                        record["digest"],
                        data,
                    ),
                )
                if record["kind"] == "thread" and record["data"] is not None:
                    conn.execute(
                        "INSERT INTO local_threads(thread_id,agent_id,created_at,restored) VALUES (?,?,?,1) ON CONFLICT(thread_id) DO NOTHING",
                        (record["item_id"], record["agent_id"], record["data"]["created_at"]),
                    )

    def restored_thread(self, thread_id: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT restored FROM local_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            return bool(row and row[0])

    def thread_rehydrated(self, thread_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE local_threads SET restored=0 WHERE thread_id=?", (thread_id,))
