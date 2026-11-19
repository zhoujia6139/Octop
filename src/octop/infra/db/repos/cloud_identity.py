"""SQL-only identity access for the independent cloud schema.

User rows serialize security changes and device/session issuance on both DBs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from octop.infra.db.pool import DatabasePool

SessionKind = Literal["account", "device", "challenge"]


@dataclass(frozen=True)
class IdentityUser:
    user_id: int
    username: str
    password_hash: str
    role: str
    enabled: int
    security_version: int
    binding_generation: int


@dataclass(frozen=True)
class IdentitySession:
    token_hash: str
    user_id: int
    kind: SessionKind
    security_version: int
    device_id: str | None
    generation: int | None
    expires_at: int
    attempts: int


@dataclass(frozen=True)
class IdentityDevice:
    device_id: str
    user_id: int
    label: str
    generation: int
    refresh_hash: str
    refresh_expires_at: int
    revoked: int
    last_seen_at: int


@dataclass(frozen=True)
class IdentityMfa:
    user_id: int
    secret_enc: bytes | None
    last_step: int | None
    pending_enc: bytes | None
    pending_expires_at: int | None
    pending_attempts: int


class IdentityTransaction:
    def __init__(self, conn: Any, *, postgres: bool) -> None:
        self.conn = conn
        self.lock = " FOR UPDATE" if postgres else ""

    def setting(self, key: str, value: str) -> str:
        self.conn.execute(
            "INSERT INTO cloud_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
        return str(
            self.conn.execute(
                f"SELECT value FROM cloud_settings WHERE key=?{self.lock}", (key,)
            ).fetchone()[0]
        )

    def lock_bootstrap(self) -> None:
        self.setting("identity_management_lock", "1")

    def user(
        self, *, user_id: int | None = None, username: str | None = None
    ) -> IdentityUser | None:
        key, value = ("user_id", user_id) if user_id is not None else ("username", username)
        row = self.conn.execute(
            f"SELECT * FROM cloud_users WHERE {key}=?{self.lock}", (value,)
        ).fetchone()
        return IdentityUser(**dict(row)) if row else None

    def users(self) -> list[IdentityUser]:
        return [
            IdentityUser(**dict(r))
            for r in self.conn.execute("SELECT * FROM cloud_users ORDER BY user_id").fetchall()
        ]

    def create_user(self, username: str, password_hash: str, role: str) -> IdentityUser:
        row = self.conn.execute(
            "INSERT INTO cloud_users(username,password_hash,role) VALUES (?,?,?) RETURNING *",
            (username, password_hash, role),
        ).fetchone()
        return IdentityUser(**dict(row))

    def update_user(self, user_id: int, *, enabled: bool, role: str, password_hash: str) -> None:
        self.conn.execute(
            "UPDATE cloud_users SET enabled=?,role=?,password_hash=?,security_version=security_version+1,binding_generation=binding_generation+1 WHERE user_id=?",
            (int(enabled), role, password_hash, user_id),
        )
        self.invalidate(user_id)

    def invalidate(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM cloud_auth_sessions WHERE user_id=?", (user_id,))
        self.conn.execute("UPDATE cloud_devices SET revoked=1 WHERE user_id=?", (user_id,))

    def advance_binding(self, user_id: int) -> int:
        row = self.conn.execute(
            "UPDATE cloud_users SET binding_generation=binding_generation+1 WHERE user_id=? RETURNING binding_generation",
            (user_id,),
        ).fetchone()
        self.conn.execute(
            "DELETE FROM cloud_auth_sessions WHERE user_id=? AND kind='device'", (user_id,)
        )
        return int(row[0])

    def session(self, token_hash: str) -> IdentitySession | None:
        row = self.conn.execute(
            "SELECT * FROM cloud_auth_sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        return IdentitySession(**dict(row)) if row else None

    def prune_sessions(self, user_id: int, now: int) -> None:
        self.conn.execute(
            "DELETE FROM cloud_auth_sessions WHERE user_id=? AND expires_at<=?", (user_id, now)
        )

    def add_session(self, session: IdentitySession) -> None:
        self.conn.execute(
            "INSERT INTO cloud_auth_sessions(token_hash,user_id,kind,security_version,device_id,generation,expires_at,attempts) VALUES (?,?,?,?,?,?,?,?)",
            tuple(session.__dict__.values()),
        )

    def consume_session(self, token_hash: str) -> None:
        self.conn.execute("DELETE FROM cloud_auth_sessions WHERE token_hash=?", (token_hash,))

    def challenge_attempt(self, token_hash: str) -> None:
        self.conn.execute(
            "UPDATE cloud_auth_sessions SET attempts=attempts+1 WHERE token_hash=?", (token_hash,)
        )

    def device(self, user_id: int) -> IdentityDevice | None:
        row = self.conn.execute(
            "SELECT device_id,user_id,label,generation,refresh_hash,refresh_expires_at,revoked,last_seen_at FROM cloud_devices WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return IdentityDevice(**dict(row)) if row else None

    def refresh_user(self, refresh_hash: str) -> int | None:
        row = self.conn.execute(
            "SELECT user_id FROM cloud_devices WHERE refresh_hash=?", (refresh_hash,)
        ).fetchone()
        return int(row[0]) if row else None

    def put_device(self, device: IdentityDevice) -> None:
        self.conn.execute(
            "INSERT INTO cloud_devices(device_id,user_id,label,generation,refresh_hash,refresh_expires_at,revoked,last_seen_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET device_id=excluded.device_id,label=excluded.label,generation=excluded.generation,refresh_hash=excluded.refresh_hash,refresh_expires_at=excluded.refresh_expires_at,revoked=0,last_seen_at=excluded.last_seen_at",
            tuple(device.__dict__.values()),
        )

    def revoke_device(self, user_id: int) -> None:
        self.conn.execute("UPDATE cloud_devices SET revoked=1 WHERE user_id=?", (user_id,))
        self.advance_binding(user_id)

    def seen(self, user_id: int, now: int) -> None:
        self.conn.execute("UPDATE cloud_devices SET last_seen_at=? WHERE user_id=?", (now, user_id))

    def mfa(self, user_id: int) -> IdentityMfa | None:
        row = self.conn.execute("SELECT * FROM cloud_mfa WHERE user_id=?", (user_id,)).fetchone()
        return IdentityMfa(**dict(row)) if row else None

    def pending_mfa(self, user_id: int, blob: bytes, expires_at: int) -> None:
        self.conn.execute(
            "INSERT INTO cloud_mfa(user_id,pending_enc,pending_expires_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET pending_enc=excluded.pending_enc,pending_expires_at=excluded.pending_expires_at,pending_attempts=0",
            (user_id, blob, expires_at),
        )

    def pending_attempt(self, user_id: int) -> None:
        self.conn.execute(
            "UPDATE cloud_mfa SET pending_attempts=pending_attempts+1 WHERE user_id=?", (user_id,)
        )

    def enable_mfa(self, user_id: int) -> None:
        self.conn.execute(
            "UPDATE cloud_mfa SET secret_enc=pending_enc,pending_enc=NULL,pending_expires_at=NULL,last_step=NULL WHERE user_id=?",
            (user_id,),
        )

    def disable_mfa(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM cloud_mfa WHERE user_id=?", (user_id,))
        self.conn.execute("DELETE FROM cloud_recovery_codes WHERE user_id=?", (user_id,))

    def use_step(self, user_id: int, step: int) -> None:
        self.conn.execute("UPDATE cloud_mfa SET last_step=? WHERE user_id=?", (step, user_id))

    def recovery_codes(self, user_id: int) -> list[tuple[int, str]]:
        return [
            (int(r[0]), str(r[1]))
            for r in self.conn.execute(
                "SELECT id,code_hash FROM cloud_recovery_codes WHERE user_id=?", (user_id,)
            ).fetchall()
        ]

    def use_recovery(self, code_id: int) -> None:
        self.conn.execute("DELETE FROM cloud_recovery_codes WHERE id=?", (code_id,))

    def set_recovery(self, user_id: int, hashes: list[str]) -> None:
        self.conn.execute("DELETE FROM cloud_recovery_codes WHERE user_id=?", (user_id,))
        self.conn.executemany(
            "INSERT INTO cloud_recovery_codes(user_id,code_hash) VALUES (?,?)",
            [(user_id, h) for h in hashes],
        )

    def login_attempt(self, key: str, now: int) -> int:
        self.conn.execute(
            "INSERT INTO cloud_login_attempts(username_hash,attempts,window_start) VALUES (?,0,?) ON CONFLICT(username_hash) DO NOTHING",
            (key, now),
        )
        row = self.conn.execute(
            f"SELECT attempts,window_start FROM cloud_login_attempts WHERE username_hash=?{self.lock}",
            (key,),
        ).fetchone()
        count = int(row[0]) if now - int(row[1]) < 300 else 0
        self.conn.execute(
            "UPDATE cloud_login_attempts SET attempts=?,window_start=? WHERE username_hash=?",
            (count + 1, int(row[1]) if count else now, key),
        )
        return count

    def clear_attempts(self, key: str) -> None:
        self.conn.execute("DELETE FROM cloud_login_attempts WHERE username_hash=?", (key,))


class CloudIdentityRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    @contextmanager
    def transaction(self) -> Iterator[IdentityTransaction]:
        with self.db.transaction() as conn:
            yield IdentityTransaction(conn, postgres=self.db.dialect == "postgresql")
