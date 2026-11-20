"""SQL-only backup versions, scoped content chunks and operation receipts."""

from __future__ import annotations

from typing import Any


class SyncTransaction:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def agent(self, agent_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_sync_agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return dict(row) if row else None

    def bind_agent(self, agent_id: str, user_id: int, resource_id: str) -> None:
        self.conn.execute(
            "INSERT INTO cloud_sync_agents(agent_id,user_id,resource_id) VALUES (?,?,?)",
            (agent_id, user_id, resource_id),
        )

    def item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT i.*,a.resource_id FROM cloud_sync_items i JOIN cloud_sync_agents a ON a.agent_id=i.agent_id WHERE i.item_id=?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None

    def version(self, item_id: str, revision: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_sync_versions WHERE item_id=? AND revision=?", (item_id, revision)
        ).fetchone()
        return dict(row) if row else None

    def operation(
        self, user_id: int, device_id: str, generation: int, operation_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT request_hash,result FROM cloud_sync_operations WHERE user_id=? AND device_id=? AND generation=? AND operation_id=?",
            (user_id, device_id, generation, operation_id),
        ).fetchone()
        return dict(row) if row else None

    def receipt(
        self,
        user_id: int,
        device_id: str,
        generation: int,
        operation_id: str,
        request_hash: str,
        result: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_sync_operations(user_id,device_id,generation,operation_id,request_hash,result) VALUES (?,?,?,?,?,?)",
            (user_id, device_id, generation, operation_id, request_hash, result),
        )

    def append(
        self,
        item_id: str,
        user_id: int,
        agent_id: str,
        kind: str,
        revision: int,
        data: str,
        digest: str,
        size_bytes: int,
    ) -> int:
        self.conn.execute(
            "INSERT INTO cloud_sync_items(item_id,user_id,agent_id,kind,revision) VALUES (?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision",
            (item_id, user_id, agent_id, kind, revision),
        )
        row = self.conn.execute(
            "INSERT INTO cloud_sync_versions(item_id,revision,data,digest,size_bytes) VALUES (?,?,?,?,?) RETURNING cursor",
            (item_id, revision, data, digest, size_bytes),
        ).fetchone()
        return int(row[0])

    def metadata_storage(self, user_id: int) -> int:
        return int(
            self.conn.execute(
                "SELECT COALESCE(SUM(v.size_bytes),0) FROM cloud_sync_versions v JOIN cloud_sync_items i ON i.item_id=v.item_id WHERE i.user_id=?",
                (user_id,),
            ).fetchone()[0]
        )

    def file_versions(self, item_id: str) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT data FROM cloud_sync_versions WHERE item_id=? AND data<>'null' ORDER BY cursor",
                (item_id,),
            ).fetchall()
        ]

    def watermark(self, user_id: int) -> int:
        return int(
            self.conn.execute(
                "SELECT COALESCE(MAX(v.cursor),0) FROM cloud_sync_versions v JOIN cloud_sync_items i ON i.item_id=v.item_id WHERE i.user_id=?",
                (user_id,),
            ).fetchone()[0]
        )

    def changes(
        self, user_id: int, cursor: int, watermark: int, limit: int
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT i.item_id,i.agent_id,a.resource_id,i.kind,v.revision,v.cursor,v.data,v.digest FROM cloud_sync_versions v JOIN cloud_sync_items i ON i.item_id=v.item_id JOIN cloud_sync_agents a ON a.agent_id=i.agent_id WHERE i.user_id=? AND v.cursor>? AND v.cursor<=? ORDER BY v.cursor LIMIT ?",
                (user_id, cursor, watermark, limit),
            ).fetchall()
        ]

    def blob(self, user_id: int, digest: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_sync_blobs WHERE user_id=? AND digest=?", (user_id, digest)
        ).fetchone()
        return dict(row) if row else None

    def storage(self, user_id: int) -> int:
        return int(
            self.conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM cloud_sync_blobs WHERE user_id=?",
                (user_id,),
            ).fetchone()[0]
        )

    def create_blob(self, user_id: int, digest: str, size: int) -> None:
        self.conn.execute(
            "INSERT INTO cloud_sync_blobs(user_id,digest,size_bytes) VALUES (?,?,?)",
            (user_id, digest, size),
        )

    def offsets(self, user_id: int, digest: str) -> list[int]:
        return [
            int(row[0])
            for row in self.conn.execute(
                "SELECT offset_bytes FROM cloud_sync_chunks WHERE user_id=? AND digest=? ORDER BY offset_bytes",
                (user_id, digest),
            ).fetchall()
        ]

    def chunk(self, user_id: int, digest: str, offset: int) -> bytes | None:
        row = self.conn.execute(
            "SELECT content FROM cloud_sync_chunks WHERE user_id=? AND digest=? AND offset_bytes=?",
            (user_id, digest, offset),
        ).fetchone()
        return bytes(row[0]) if row else None

    def save_chunk(self, user_id: int, digest: str, offset: int, content: bytes) -> None:
        self.conn.execute(
            "INSERT INTO cloud_sync_chunks(user_id,digest,offset_bytes,content) VALUES (?,?,?,?)",
            (user_id, digest, offset, content),
        )

    def complete(self, user_id: int, digest: str) -> None:
        self.conn.execute(
            "UPDATE cloud_sync_blobs SET complete=1 WHERE user_id=? AND digest=?", (user_id, digest)
        )
