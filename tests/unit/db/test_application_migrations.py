"""Independent migration sets must be transactional and dialect-compatible."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from tests.support.postgresql import requires_postgresql

from octop.infra.db.application_migrations import migrate_application
from octop.infra.db.pool import PostgresPool, SqlitePool
from octop.infra.utils.ulid import new_ulid


def test_new_watermark_is_independent_of_legacy_version(tmp_path: Path):
    db = SqlitePool(tmp_path / "test.db")
    try:
        with db.transaction() as conn:
            conn.execute("CREATE TABLE _schema_version (version INTEGER)")
            conn.execute("INSERT INTO _schema_version VALUES (999)")
        assert migrate_application(db, "cloud") == 2
        assert migrate_application(db, "cloud") == 2
        with db.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 999
            conn.execute("UPDATE cloud_schema_version SET version=999")
        with pytest.raises(RuntimeError, match="newer"):
            migrate_application(db, "cloud")
    finally:
        db.close()


def test_failed_migration_rolls_back_ddl_and_watermark(tmp_path: Path, monkeypatch):
    from octop.infra.db import application_migrations

    root = tmp_path / "migrations"
    (root / "cloud").mkdir(parents=True)
    (root / "cloud" / "001_bad.sql").write_text(
        "CREATE TABLE transient (id INTEGER); INVALID SQL;", encoding="utf-8"
    )
    monkeypatch.setattr(application_migrations, "_ROOT", root)
    db = SqlitePool(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate_application(db, "cloud")
        with db.connect() as conn:
            assert (
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
            )
    finally:
        db.close()


@requires_postgresql
@pytest.mark.postgresql
def test_postgresql_schema_pairs_initialize_idempotently_and_reference_public_ids():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    dsn = os.environ["OCTOP_TEST_DATABASE_URL"]
    schema = "p1_" + new_ulid().lower()
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db = None
        try:
            db = PostgresPool(make_conninfo(dsn, options=f"-csearch_path={schema}"))
            for role in ("cloud", "local"):
                assert migrate_application(db, role) == (2 if role == "cloud" else 1)
                assert migrate_application(db, role) == (2 if role == "cloud" else 1)
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO local_agents(agent_id,name,model_id,created_at) VALUES ('agent-a','A','platform/model',1)"
                )
                conn.execute(
                    "INSERT INTO local_threads(thread_id,agent_id,created_at) VALUES ('thread-a','agent-a',1)"
                )
                conn.execute(
                    "INSERT INTO local_turns(turn_id,thread_id,status,started_at) VALUES ('turn-a','thread-a','running',1)"
                )
                conn.execute("DELETE FROM local_agents WHERE agent_id='agent-a'")
                assert conn.execute("SELECT COUNT(*) FROM local_turns").fetchone()[0] == 0
        finally:
            if db is not None:
                db.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
