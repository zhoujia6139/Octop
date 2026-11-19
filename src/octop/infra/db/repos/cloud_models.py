"""SQL-only model catalog and ledger access; callers hold the user row lock."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from octop.infra.db.pool import DatabasePool


class ModelTransaction:
    def __init__(self, conn: Any, *, postgres: bool = False) -> None:
        self.conn = conn
        self.lock = " FOR UPDATE" if postgres else ""

    def lock_user(self, user_id: int) -> bool:
        return (
            self.conn.execute(
                f"SELECT user_id FROM cloud_users WHERE user_id=?{self.lock}", (user_id,)
            ).fetchone()
            is not None
        )

    def provider(self, provider_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_model_providers WHERE provider_id=?", (provider_id,)
        ).fetchone()
        return dict(row) if row else None

    def providers(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cloud_model_providers ORDER BY provider_id"
            ).fetchall()
        ]

    def put_provider(
        self, provider_id: str, base_url: str, credential_enc: bytes, statuses: list[int]
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_model_providers(provider_id,base_url,credential_enc,free_statuses) VALUES (?,?,?,?) ON CONFLICT(provider_id) DO UPDATE SET base_url=excluded.base_url,credential_enc=excluded.credential_enc,free_statuses=excluded.free_statuses",
            (provider_id, base_url, credential_enc, json.dumps(statuses)),
        )

    def rate(self, revision: str) -> str | None:
        row = self.conn.execute(
            "SELECT definition FROM cloud_model_rates WHERE revision=?", (revision,)
        ).fetchone()
        return str(row[0]) if row else None

    def put_rate(self, revision: str, definition: str) -> None:
        self.conn.execute(
            "INSERT INTO cloud_model_rates(revision,definition) VALUES (?,?) ON CONFLICT(revision) DO NOTHING",
            (revision, definition),
        )

    def model(self, model_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT definition FROM cloud_models WHERE model_id=?", (model_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def put_model(self, model_id: str, definition: str) -> None:
        self.conn.execute(
            "INSERT INTO cloud_models(model_id,definition) VALUES (?,?) ON CONFLICT(model_id) DO UPDATE SET definition=excluded.definition",
            (model_id, definition),
        )

    def models(self, user_id: int | None = None) -> list[str]:
        if user_id is None:
            rows = self.conn.execute(
                "SELECT definition FROM cloud_models ORDER BY model_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT m.definition FROM cloud_models m JOIN cloud_model_grants g ON g.model_id=m.model_id WHERE g.user_id=? AND g.allowed=1 ORDER BY m.model_id",
                (user_id,),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def allowed(self, user_id: int, model_id: str) -> bool:
        row = self.conn.execute(
            "SELECT allowed FROM cloud_model_grants WHERE user_id=? AND model_id=?",
            (user_id, model_id),
        ).fetchone()
        return row is not None and bool(row[0])

    def grant(self, user_id: int, model_id: str, allowed: bool) -> None:
        self.conn.execute(
            "INSERT INTO cloud_model_grants(user_id,model_id,allowed) VALUES (?,?,?) ON CONFLICT(user_id,model_id) DO UPDATE SET allowed=excluded.allowed",
            (user_id, model_id, int(allowed)),
        )

    def budget(self, user_id: int, currency: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT * FROM cloud_model_budgets WHERE user_id=? AND currency=?{self.lock}",
            (user_id, currency),
        ).fetchone()
        return dict(row) if row else None

    def budgets(self, user_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cloud_model_budgets WHERE user_id=? ORDER BY currency", (user_id,)
            ).fetchall()
        ]

    def put_budget(self, user_id: int, currency: str, limit: int) -> None:
        self.conn.execute(
            "INSERT INTO cloud_model_budgets(user_id,currency,limit_nanos) VALUES (?,?,?) ON CONFLICT(user_id,currency) DO UPDATE SET limit_nanos=excluded.limit_nanos",
            (user_id, currency, limit),
        )

    def balance(self, user_id: int, currency: str, *, spent: int = 0, reserved: int = 0) -> None:
        self.conn.execute(
            "UPDATE cloud_model_budgets SET spent_nanos=spent_nanos+?,reserved_nanos=reserved_nanos+? WHERE user_id=? AND currency=?",
            (spent, reserved, user_id, currency),
        )

    def register_context(self, user_id: int, agent_id: str, thread_id: str, turn_id: str) -> bool:
        # Global public IDs cannot be claimed by another user or reassociated.
        self.conn.execute(
            "INSERT INTO cloud_model_agents(agent_id,user_id) VALUES (?,?) ON CONFLICT(agent_id) DO NOTHING",
            (agent_id, user_id),
        )
        if (
            self.conn.execute(
                "SELECT user_id FROM cloud_model_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()[0]
            != user_id
        ):
            return False
        self.conn.execute(
            "INSERT INTO cloud_model_threads(thread_id,agent_id) VALUES (?,?) ON CONFLICT(thread_id) DO NOTHING",
            (thread_id, agent_id),
        )
        if (
            self.conn.execute(
                "SELECT agent_id FROM cloud_model_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()[0]
            != agent_id
        ):
            return False
        self.conn.execute(
            "INSERT INTO cloud_model_turns(turn_id,thread_id) VALUES (?,?) ON CONFLICT(turn_id) DO NOTHING",
            (turn_id, thread_id),
        )
        return self.context_owned(user_id, agent_id, thread_id, turn_id)

    def context_owned(self, user_id: int, agent_id: str, thread_id: str, turn_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM cloud_model_turns u JOIN cloud_model_threads t ON u.thread_id=t.thread_id JOIN cloud_model_agents a ON t.agent_id=a.agent_id WHERE a.user_id=? AND a.agent_id=? AND t.thread_id=? AND u.turn_id=?",
                (user_id, agent_id, thread_id, turn_id),
            ).fetchone()
            is not None
        )

    def request(self, request_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_model_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def requests(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cloud_model_requests WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        ]

    def create_request(self, data: dict[str, Any]) -> None:
        columns = "request_id,user_id,device_id,generation,agent_id,thread_id,turn_id,model_id,purpose,request_hash,state,currency,rate_revision,definition,created_at,lease_until"
        self.conn.execute(
            f"INSERT INTO cloud_model_requests({columns}) VALUES ({','.join('?' for _ in columns.split(','))})",
            tuple(data[k] for k in columns.split(",")),
        )

    def request_state(self, request_id: str, state: str, lease_until: int) -> None:
        self.conn.execute(
            "UPDATE cloud_model_requests SET state=?,lease_until=? WHERE request_id=?",
            (state, lease_until, request_id),
        )

    def add_attempt(
        self, attempt_id: str, request_id: str, provider_id: str, reserved: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_model_attempts(attempt_id,request_id,provider_id,state,reserved_nanos) VALUES (?,?,?,'reserved',?)",
            (attempt_id, request_id, provider_id, reserved),
        )

    def attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_model_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return dict(row) if row else None

    def attempts(self, request_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cloud_model_attempts WHERE request_id=? ORDER BY id", (request_id,)
            ).fetchall()
        ]

    def attempt_state(
        self,
        attempt_id: str,
        state: str,
        *,
        cost: int | None = None,
        usage: str | None = None,
        reason: str | None = None,
        evidence: str | None = None,
        resolved_by: int | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE cloud_model_attempts SET state=?,cost_nanos=?,usage=?,reason=?,evidence=?,resolved_by=? WHERE attempt_id=?",
            (state, cost, usage, reason, evidence, resolved_by, attempt_id),
        )

    def settle(
        self, attempt_id: str, request: dict[str, Any], cost: int, usage: dict[str, int], now: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO cloud_model_settlements(attempt_id,user_id,model_id,currency,rate_revision,cost_nanos,uncached_input,cache_read,cache_write,output,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                request["user_id"],
                request["model_id"],
                request["currency"],
                request["rate_revision"],
                cost,
                usage["uncached_input"],
                usage["cache_read"],
                usage["cache_write"],
                usage["output"],
                now,
            ),
        )

    def stale(self, now: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM cloud_model_requests WHERE state IN ('reserved','running') AND lease_until<?",
                (now,),
            ).fetchall()
        ]

    def heartbeat(self, request_id: str, until: int) -> None:
        self.conn.execute(
            "UPDATE cloud_model_requests SET lease_until=? WHERE request_id=? AND state IN ('reserved','running')",
            (until, request_id),
        )

    def summary(self, user_id: int, since: int, until: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT user_id,model_id,currency,SUM(cost_nanos) AS cost_nanos,SUM(uncached_input) AS uncached_input,SUM(cache_read) AS cache_read,SUM(cache_write) AS cache_write,SUM(output) AS output,COUNT(*) AS settled_attempts FROM cloud_model_settlements WHERE user_id=? AND created_at>=? AND created_at<? GROUP BY user_id,model_id,currency ORDER BY model_id,currency",
                (user_id, since, until),
            ).fetchall()
        ]


class CloudModelRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    @contextmanager
    def transaction(self) -> Iterator[ModelTransaction]:
        with self.db.transaction() as conn:
            yield ModelTransaction(conn, postgres=self.db.dialect == "postgresql")
