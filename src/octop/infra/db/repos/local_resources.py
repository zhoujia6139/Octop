"""Account-scoped resource drafts, immutable cache, delivery and outbox SQL."""

from __future__ import annotations

from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts


class LocalResourceRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def list_resources(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM local_resources ORDER BY id").fetchall()
            ]

    def get(self, resource_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
            return dict(row) if row else None

    def version(self, resource_id: str, revision: int) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT snapshot FROM local_resource_versions WHERE resource_id=? AND revision=?",
                (resource_id, revision),
            ).fetchone()
            return str(row[0]) if row else None

    def cache(self, resource_id: str, revision: int, snapshot: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_resource_versions(resource_id,revision,snapshot) VALUES (?,?,?) ON CONFLICT(resource_id,revision) DO NOTHING",
                (resource_id, revision, snapshot),
            )

    def draft(
        self,
        resource_id: str,
        kind: str,
        revision: int,
        spec: str,
        snapshot: str,
        source_id: str | None,
        source_revision: int | None,
        operation_id: str,
        mutation: str,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_resources(resource_id,agent_id,scope,kind,revision,desired_revision,spec,source_id,source_revision,sync_state,install_state) VALUES (?,?,'personal',?,?,?,?,?,?,'pending','pending') ON CONFLICT(resource_id) DO UPDATE SET revision=excluded.revision,desired_revision=excluded.desired_revision,spec=excluded.spec,sync_state='pending',install_state='pending',error_code=NULL",
                (
                    resource_id,
                    resource_id if kind == "agent" else None,
                    kind,
                    revision,
                    revision if spec != "null" else None,
                    spec,
                    source_id,
                    source_revision,
                ),
            )
            conn.execute(
                "INSERT INTO local_resource_versions(resource_id,revision,snapshot) VALUES (?,?,?)",
                (resource_id, revision, snapshot),
            )
            conn.execute(
                "INSERT INTO local_resource_outbox(operation_id,resource_id,mutation) VALUES (?,?,?)",
                (operation_id, resource_id, mutation),
            )

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_resource_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return dict(row) if row else None

    def outbox(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM local_resource_outbox WHERE state != 'done' ORDER BY sequence"
                ).fetchall()
            ]

    def acknowledge(self, operation_id: str, resource_id: str, revision: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_resource_outbox SET state='done' WHERE operation_id=?",
                (operation_id,),
            )
            pending = conn.execute(
                "SELECT 1 FROM local_resource_outbox WHERE resource_id=? AND state != 'done'",
                (resource_id,),
            ).fetchone()
            conn.execute(
                "UPDATE local_resources SET cloud_revision=?,sync_state=?,error_code=NULL WHERE resource_id=?",
                (revision, "pending" if pending else "synced", resource_id),
            )

    def conflict(self, resource_id: str, operation_id: str, code: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_resource_outbox SET state='conflict' WHERE operation_id=?",
                (operation_id,),
            )
            conn.execute(
                "UPDATE local_resources SET sync_state='conflict',error_code=? WHERE resource_id=?",
                (code, resource_id),
            )

    def note_delivery(
        self, resource_id: str, scope: str, kind: str, revision: int, agent_id: str | None
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_resources(resource_id,agent_id,scope,kind,revision,desired_revision,spec,sync_state,install_state) VALUES (?,?,?,?,?,?,'null','synced','pending') ON CONFLICT(resource_id) DO UPDATE SET desired_revision=excluded.desired_revision,install_state=CASE WHEN local_resources.installed_revision=excluded.desired_revision THEN local_resources.install_state ELSE 'pending' END",
                (resource_id, agent_id, scope, kind, revision, revision),
            )

    def receive(
        self,
        resource_id: str,
        scope: str,
        kind: str,
        revision: int,
        spec: str,
        source_id: str | None,
        source_revision: int | None,
        agent_id: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            # A queued local edit is authoritative until accepted or explicitly
            # resolved. Never overwrite it with a slower cloud response.
            if conn.execute(
                "SELECT 1 FROM local_resource_outbox WHERE resource_id=? AND state != 'done'",
                (resource_id,),
            ).fetchone():
                return
            conn.execute(
                "INSERT INTO local_resources(resource_id,agent_id,scope,kind,revision,cloud_revision,desired_revision,spec,source_id,source_revision,sync_state,install_state) VALUES (?,?,?,?,?,?,?,?,?,?,'synced','pending') ON CONFLICT(resource_id) DO UPDATE SET revision=excluded.revision,cloud_revision=excluded.cloud_revision,desired_revision=excluded.desired_revision,spec=excluded.spec,sync_state='synced',install_state=CASE WHEN local_resources.installed_revision=excluded.revision THEN local_resources.install_state ELSE 'pending' END",
                (
                    resource_id,
                    agent_id,
                    scope,
                    kind,
                    revision,
                    revision,
                    revision if spec != "null" else None,
                    spec,
                    source_id,
                    source_revision,
                ),
            )

    def withdraw(self, resource_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_resources SET desired_revision=NULL,install_state='pending' WHERE resource_id=? AND scope='platform'",
                (resource_id,),
            )

    def installed(
        self,
        resource_id: str,
        revision: int | None,
        snapshot: str | None,
        *,
        agent: dict[str, Any] | None = None,
        skills_path: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_resources SET installed_revision=?,installed_snapshot=?,install_state=?,error_code=NULL WHERE resource_id=?",
                (
                    revision,
                    snapshot,
                    "installed" if revision is not None else "removed",
                    resource_id,
                ),
            )
            if agent is not None:
                instance = conn.execute(
                    "SELECT agent_id FROM local_resources WHERE resource_id=?", (resource_id,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO local_agents(agent_id,name,model_id,system_prompt,created_at,resource_snapshot,skills_path,resource_id) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET name=excluded.name,model_id=excluded.model_id,system_prompt=excluded.system_prompt,resource_snapshot=excluded.resource_snapshot,skills_path=excluded.skills_path,enabled=1",
                    (
                        instance,
                        agent["name"],
                        agent["model_id"],
                        agent.get("system_prompt"),
                        now_ts(),
                        snapshot,
                        skills_path,
                        resource_id,
                    ),
                )
            elif revision is None:
                conn.execute(
                    "UPDATE local_agents SET enabled=0 WHERE resource_id=?", (resource_id,)
                )

    def failed(self, resource_id: str, code: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE local_resources SET install_state='failed',error_code=? WHERE resource_id=?",
                (code, resource_id),
            )

    def state(self, key: str, value: str | None = None) -> str | None:
        with self.db.transaction() as conn:
            if value is not None:
                conn.execute(
                    "INSERT INTO local_resource_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            row = conn.execute(
                "SELECT value FROM local_resource_state WHERE key=?", (key,)
            ).fetchone()
            return str(row[0]) if row else None

    def new_binding(self, binding: str) -> None:
        with self.db.transaction() as conn:
            previous = conn.execute(
                "SELECT value FROM local_resource_state WHERE key='binding'"
            ).fetchone()
            if not previous or previous[0] != binding:
                if previous:
                    conn.execute(
                        "UPDATE local_resource_outbox SET state='conflict' WHERE state='pending'"
                    )
                    conn.execute(
                        "UPDATE local_resources SET sync_state='conflict',error_code='RESOURCE_CONFLICT' WHERE scope='personal' AND sync_state='pending'"
                    )
                conn.execute(
                    "UPDATE local_resources SET desired_revision=NULL,install_state='pending' WHERE scope='platform'"
                )
                conn.execute(
                    "INSERT INTO local_resource_state(key,value) VALUES ('cursor','0') ON CONFLICT(key) DO UPDATE SET value='0'"
                )
                conn.execute(
                    "INSERT INTO local_resource_state(key,value) VALUES ('binding',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (binding,),
                )
