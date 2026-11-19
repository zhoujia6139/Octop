"""SQL-only version, assignment, ownership and delivery repositories."""

from __future__ import annotations

from typing import Any


class ResourceTransaction:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def resource(self, resource_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_resources WHERE resource_id=?", (resource_id,)
        ).fetchone()
        return dict(row) if row else None

    def resources(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM cloud_resources ORDER BY id").fetchall()
        ]

    def version(self, resource_id: str, revision: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_resource_versions WHERE resource_id=? AND revision=?",
            (resource_id, revision),
        ).fetchone()
        return dict(row) if row else None

    def operation(self, user_id: int, operation_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT request_hash,result FROM cloud_resource_operations WHERE user_id=? AND operation_id=?",
            (user_id, operation_id),
        ).fetchone()
        return dict(row) if row else None

    def save_operation(
        self, user_id: int, operation_id: str, request_hash: str, result: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_resource_operations(user_id,operation_id,request_hash,result) VALUES (?,?,?,?)",
            (user_id, operation_id, request_hash, result),
        )

    def usage(self, user_id: int) -> tuple[int, int]:
        count = self.conn.execute(
            "SELECT COUNT(*) FROM cloud_resources WHERE owner_id=? AND deleted=0", (user_id,)
        ).fetchone()[0]
        size = self.conn.execute(
            "SELECT COALESCE(SUM(v.size_bytes),0) FROM cloud_resource_versions v JOIN cloud_resources r ON r.resource_id=v.resource_id WHERE r.owner_id=?",
            (user_id,),
        ).fetchone()[0]
        return int(count), int(size)

    def create(
        self,
        resource_id: str,
        scope: str,
        kind: str,
        owner_id: int | None,
        source_id: str | None,
        source_revision: int | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_resources(resource_id,scope,kind,owner_id,revision,source_id,source_revision) VALUES (?,?,?,?,0,?,?)",
            (resource_id, scope, kind, owner_id, source_id, source_revision),
        )

    def append(
        self,
        resource_id: str,
        revision: int,
        version_id: str,
        spec: str,
        digest: str,
        published: bool,
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_resource_versions(version_id,resource_id,revision,spec,digest,size_bytes,published) VALUES (?,?,?,?,?,?,?)",
            (version_id, resource_id, revision, spec, digest, len(spec.encode()), int(published)),
        )
        self.conn.execute(
            "UPDATE cloud_resources SET revision=?,deleted=?,published_revision=CASE WHEN ?=1 THEN ? ELSE published_revision END WHERE resource_id=?",
            (revision, int(spec == "null"), int(published), revision, resource_id),
        )

    def dependency(
        self, resource_id: str, revision: int, skill_id: str, skill_revision: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_resource_dependencies(resource_id,revision,skill_id,skill_revision) VALUES (?,?,?,?)",
            (resource_id, revision, skill_id, skill_revision),
        )

    def publish(self, resource_id: str, revision: int) -> None:
        self.conn.execute(
            "UPDATE cloud_resource_versions SET published=1 WHERE resource_id=? AND revision=?",
            (resource_id, revision),
        )
        self.conn.execute(
            "UPDATE cloud_resources SET published_revision=? WHERE resource_id=?",
            (revision, resource_id),
        )

    def assignment(self, resource_id: str, device_id: str, generation: int) -> int | None:
        row = self.conn.execute(
            "SELECT revision FROM cloud_resource_assignments WHERE resource_id=? AND device_id=? AND generation=?",
            (resource_id, device_id, generation),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def assign(
        self,
        assignment_id: str,
        resource_id: str,
        user_id: int,
        device_id: str,
        generation: int,
        revision: int | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_resource_assignments(assignment_id,resource_id,user_id,device_id,generation,revision) VALUES (?,?,?,?,?,?) ON CONFLICT(resource_id,device_id,generation) DO UPDATE SET revision=excluded.revision",
            (assignment_id, resource_id, user_id, device_id, generation, revision),
        )

    def assigned(self, resource_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cloud_resource_assignments WHERE resource_id=? AND revision IS NOT NULL",
                (resource_id,),
            ).fetchall()
        ]

    def change(
        self,
        user_id: int,
        resource_id: str,
        revision: int | None,
        device_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_resource_changes(user_id,device_id,generation,resource_id,revision) VALUES (?,?,?,?,?)",
            (user_id, device_id, generation, resource_id, revision),
        )

    def changes(
        self, user_id: int, device_id: str, generation: int, cursor: int, limit: int
    ) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT cursor,resource_id,revision FROM cloud_resource_changes WHERE user_id=? AND (device_id IS NULL OR (device_id=? AND generation=?)) AND cursor>? ORDER BY cursor LIMIT ?",
                (user_id, device_id, generation, cursor, limit),
            ).fetchall()
        ]

    def can_read(
        self,
        resource_id: str,
        revision: int,
        user_id: int,
        device_id: str | None,
        generation: int | None,
    ) -> bool:
        if self.conn.execute(
            "SELECT 1 FROM cloud_resources WHERE resource_id=? AND owner_id=?",
            (resource_id, user_id),
        ).fetchone():
            return True
        if self.conn.execute(
            "SELECT 1 FROM cloud_resource_dependencies d JOIN cloud_resources r ON r.resource_id=d.resource_id WHERE r.owner_id=? AND r.deleted=0 AND d.skill_id=? AND d.skill_revision=?",
            (user_id, resource_id, revision),
        ).fetchone():
            return True
        return bool(
            self.conn.execute(
                "SELECT 1 FROM cloud_resource_assignments a JOIN cloud_resources r ON r.resource_id=a.resource_id WHERE a.user_id=? AND a.device_id=? AND a.generation=? AND r.deleted=0 AND ((a.resource_id=? AND a.revision=?) OR EXISTS (SELECT 1 FROM cloud_resource_dependencies d WHERE d.resource_id=a.resource_id AND d.revision=a.revision AND d.skill_id=? AND d.skill_revision=?))",
                (user_id, device_id, generation, resource_id, revision, resource_id, revision),
            ).fetchone()
        )

    def has_assignment(
        self, resource_id: str, user_id: int, device_id: str | None, generation: int | None
    ) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM cloud_resource_assignments WHERE resource_id=? AND user_id=? AND device_id=? AND generation=?",
                (resource_id, user_id, device_id, generation),
            ).fetchone()
        )

    def execution_resource(self, agent_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT resource_id FROM cloud_model_agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def bind_execution(self, agent_id: str, resource_id: str) -> None:
        self.conn.execute(
            "UPDATE cloud_model_agents SET resource_id=? WHERE agent_id=?", (resource_id, agent_id)
        )

    def agent_allowed(
        self, agent_id: str, user_id: int, device_id: str | None, generation: int | None
    ) -> bool:
        row = self.resource(agent_id)
        return bool(
            row
            and row["kind"] == "agent"
            and not row["deleted"]
            and (
                row["owner_id"] == user_id
                or self.assignment(agent_id, device_id or "", generation or 0) is not None
            )
        )

    def report(
        self, device_id: str, generation: int, user_id: int, resource_id: str, report: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_resource_reports(device_id,generation,user_id,resource_id,report) VALUES (?,?,?,?,?) ON CONFLICT(device_id,generation,resource_id) DO UPDATE SET report=excluded.report",
            (device_id, generation, user_id, resource_id, report),
        )

    def reports(self, user_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cloud_resource_reports WHERE user_id=? ORDER BY resource_id",
                (user_id,),
            ).fetchall()
        ]
