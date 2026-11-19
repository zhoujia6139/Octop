"""The local application's minimal persistence container."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.local_runtime import LocalAgentRepo, LocalThreadRepo, LocalTurnRepo


@dataclass(frozen=True)
class LocalServices:
    db: SqlitePool
    agents: LocalAgentRepo
    threads: LocalThreadRepo
    turns: LocalTurnRepo

    @classmethod
    def from_pool(cls, db: SqlitePool) -> LocalServices:
        return cls(db, LocalAgentRepo(db), LocalThreadRepo(db), LocalTurnRepo(db))
