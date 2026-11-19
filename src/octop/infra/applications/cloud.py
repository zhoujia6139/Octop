"""Minimal cloud lifecycle; no old server, repo bundle or Agent dependencies."""

from __future__ import annotations

import asyncio

from octop.application_config import CloudSettings
from octop.infra.db.application_migrations import migrate_application
from octop.infra.db.pool import DatabasePool, PostgresPool, SqlitePool
from octop.infra.db.repos.cloud_identity import CloudIdentityRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.identity.service import CloudIdentity
from octop.infra.utils.ulid import new_ulid


class CloudApplication:
    def __init__(self, settings: CloudSettings) -> None:
        self.settings = settings
        self.db: DatabasePool | None = None
        self.schema_version = 0
        self.identity: CloudIdentity | None = None

    def _open(self) -> tuple[DatabasePool, int]:
        self.settings.home.mkdir(parents=True, exist_ok=True)
        db: DatabasePool = (
            PostgresPool(self.settings.database_url.get_secret_value())
            if self.settings.database_url is not None
            else SqlitePool(self.settings.home / "cloud.db")
        )
        try:
            version = migrate_application(db, "cloud")
            repo = CloudIdentityRepo(db)
            with repo.transaction() as tx:
                cloud_id = tx.setting("cloud_id", new_ulid())
            self.identity = CloudIdentity(
                repo, SecretRepo(db), cloud_id=cloud_id, timezone=self.settings.default_timezone
            )
            return db, version
        except BaseException:
            db.close()
            raise

    async def start(self) -> None:
        if self.db is None:
            self.db, self.schema_version = await asyncio.get_running_loop().run_in_executor(
                None, self._open
            )

    async def close(self) -> None:
        if self.db is not None:
            self.identity = None
            db, self.db = self.db, None
            await asyncio.get_running_loop().run_in_executor(None, db.close)
