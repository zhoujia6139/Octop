"""SQL repositories for the independent local runtime schema."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts


@dataclass(frozen=True)
class LocalAgentRow:
    agent_id: str
    name: str
    model_id: str
    system_prompt: str | None
    enabled: int = 1
    resource_snapshot: str | None = None
    skills_path: str | None = None
    resource_id: str | None = None


class LocalAgentRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(self, agent_id: str, name: str, model_id: str, system_prompt: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_agents(agent_id,name,model_id,system_prompt,created_at) VALUES (?,?,?,?,?)",
                (agent_id, name, model_id, system_prompt, now_ts()),
            )

    def get(self, agent_id: str) -> LocalAgentRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT agent_id,name,model_id,system_prompt,enabled,resource_snapshot,skills_path,resource_id FROM local_agents WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
        return LocalAgentRow(**dict(row)) if row is not None else None

    def list(self) -> list[LocalAgentRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT agent_id,name,model_id,system_prompt,enabled,resource_snapshot,skills_path,resource_id FROM local_agents ORDER BY id"
            ).fetchall()
        return [LocalAgentRow(**dict(row)) for row in rows]


class LocalThreadRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(self, thread_id: str, agent_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_threads(thread_id,agent_id,created_at) VALUES (?,?,?)",
                (thread_id, agent_id, now_ts()),
            )

    def belongs_to(self, thread_id: str, agent_id: str) -> bool:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM local_threads WHERE thread_id=? AND agent_id=?",
                (thread_id, agent_id),
            ).fetchone()
        return row is not None


class LocalTurnRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(self, turn_id: str, thread_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_turns(turn_id,thread_id,status,started_at) VALUES (?,?,'running',?)",
                (turn_id, thread_id, now_ts()),
            )

    def belongs_to(self, turn_id: str, thread_id: str) -> bool:
        with self._db.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM local_turns WHERE turn_id=? AND thread_id=?",
                    (turn_id, thread_id),
                ).fetchone()
                is not None
            )

    def set_status(self, turn_id: str, status: str) -> None:
        finished = None if status in {"running", "awaiting_approval"} else now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE local_turns SET status=?,finished_at=? WHERE turn_id=?",
                (status, finished, turn_id),
            )

    def recover_interrupted(self) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE local_turns SET status='interrupted',finished_at=? WHERE status IN ('running','awaiting_approval')",
                (now_ts(),),
            )
