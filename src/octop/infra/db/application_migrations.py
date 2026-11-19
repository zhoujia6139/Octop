"""Independent schema discovery and watermarks for the new applications."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from octop.infra.db.pool import DatabasePool

_ROOT = Path(__file__).parent / "migrations"


def migrate_application(db: DatabasePool, role: Literal["cloud", "local"]) -> int:
    directory = _ROOT / role
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    files = [
        path for path in files if path.name.endswith(".pg.sql") == (db.dialect == "postgresql")
    ]
    versions = [int(path.name[:3]) for path in files]
    if not versions or versions != list(range(1, len(files) + 1)):
        raise RuntimeError(f"invalid {role} migration sequence")
    # role is a closed literal chosen by the application, never request input.
    table = f"{role}_schema_version"
    with db.transaction() as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL)"
        )
        conn.execute(f"INSERT INTO {table}(id, version) VALUES (1, 0) ON CONFLICT(id) DO NOTHING")
        lock = " FOR UPDATE" if db.dialect == "postgresql" else ""
        current = int(
            conn.execute(f"SELECT version FROM {table} WHERE id = 1{lock}").fetchone()["version"]
        )
        if current > versions[-1]:
            raise RuntimeError(f"{role} database is newer than this application")
        for version, path in zip(versions, files, strict=True):
            if version <= current:
                continue
            # Application migrations contain simple DDL, without procedural SQL
            # or semicolons embedded in values. Keep each script transactional.
            for statement in path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"UPDATE {table} SET version = ? WHERE id = 1", (version,))
    return versions[-1]
