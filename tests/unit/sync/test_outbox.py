"""An unattempted streaming snapshot may coalesce; attempted requests are immutable."""

from __future__ import annotations

import json

from octop.infra.db.application_migrations import migrate_application
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.local_sync import LocalSyncRepo
from octop.infra.sync.contracts import digest


def test_streaming_snapshots_coalesce_until_claimed_then_preserve_request(tmp_path):
    db = SqlitePool(tmp_path / "local.db")
    migrate_application(db, "local")
    repo = LocalSyncRepo(db)
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_agents(agent_id,name,model_id,created_at) VALUES ('agent-one','A','platform/test',0)"
            )
        for number in range(100):
            data = {"kind": "thread", "title": str(number), "status": "running"}
            repo.save(
                "thread-one", "agent-one", "resource-one", "thread", json.dumps(data), digest(data)
            )
        pending = repo.pending()
        assert len(pending) == 1
        assert pending[0]["revision"] == 1
        claimed = repo.claim(pending[0]["operation_id"])
        assert json.loads(claimed["mutation"])["data"]["title"] == "99"
        data = {"kind": "thread", "title": "Finished", "status": "completed"}
        repo.save(
            "thread-one", "agent-one", "resource-one", "thread", json.dumps(data), digest(data)
        )
        assert len(repo.pending()) == 2
        assert repo.claim(claimed["operation_id"])["mutation"] == claimed["mutation"]
        repo.acknowledge(claimed["operation_id"], "thread-one", 1)
        assert repo.get("thread-one")["state"] == "pending"
        next_operation = repo.pending()[0]
        assert next_operation["revision"] == 2
        repo.acknowledge(next_operation["operation_id"], "thread-one", 2)
        assert repo.get("thread-one")["state"] == "synced"
        assert repo.pending() == []
    finally:
        db.close()


def test_never_attempted_deletion_still_waits_for_cloud_tombstone_ack(tmp_path):
    db = SqlitePool(tmp_path / "local.db")
    migrate_application(db, "local")
    repo = LocalSyncRepo(db)
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_agents(agent_id,name,model_id,created_at) VALUES ('agent-one','A','platform/test',0)"
            )
        data = {"kind": "thread"}
        repo.save(
            "thread-one", "agent-one", "resource-one", "thread", json.dumps(data), digest(data)
        )
        repo.save("thread-one", "agent-one", "resource-one", "thread", "null", digest(None))
        assert len(repo.pending()) == 1
        assert json.loads(repo.pending()[0]["mutation"])["data"] is None
        assert repo.get("thread-one")["state"] == "pending"
        assert repo.get("thread-one")["data"] == "null"
        repo.save(
            "thread-one", "agent-one", "resource-one", "thread", json.dumps(data), digest(data)
        )
        assert repo.get("thread-one")["data"] == "null"
    finally:
        db.close()
