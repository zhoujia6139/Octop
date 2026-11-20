"""Minimal cloud lifecycle; no old server, repo bundle or Agent dependencies."""

from __future__ import annotations

import asyncio

import httpx

from octop.application_config import CloudSettings
from octop.infra.crypto.fernet import PURPOSE_PROVIDER, get_fernet
from octop.infra.db.application_migrations import migrate_application
from octop.infra.db.pool import DatabasePool, PostgresPool, SqlitePool
from octop.infra.db.repos.cloud_identity import CloudIdentityRepo
from octop.infra.db.repos.cloud_models import CloudModelRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.identity.contracts import UserView
from octop.infra.identity.service import CloudIdentity
from octop.infra.models.gateway import ModelGateway
from octop.infra.models.ledger import ModelLedger
from octop.infra.resources.service import CloudResources
from octop.infra.sync.service import CloudSync
from octop.infra.utils.ulid import new_ulid


class CloudApplication:
    def __init__(
        self, settings: CloudSettings, *, provider_transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._provider_transport = provider_transport
        self.resources: CloudResources | None = None
        self.models: ModelGateway | None = None
        self.db: DatabasePool | None = None
        self.schema_version = 0
        self.identity: CloudIdentity | None = None
        self.sync: CloudSync | None = None

    def _open_identity(self) -> tuple[DatabasePool, int, CloudIdentity]:
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
            identity = CloudIdentity(
                repo, SecretRepo(db), cloud_id=cloud_id, timezone=self.settings.default_timezone
            )
            return db, version, identity
        except BaseException:
            db.close()
            raise

    def initialize_admin(self, username: str, password: str, *, if_empty: bool = False) -> UserView:
        """Offline initialization never starts a second gateway/recovery loop."""
        db, _, identity = self._open_identity()
        try:
            return identity.bootstrap(username, password, if_empty=if_empty)
        finally:
            db.close()

    def _open(self) -> tuple[DatabasePool, int]:
        db, version, self.identity = self._open_identity()
        try:
            self.resources = CloudResources(self.identity)
            self.sync = CloudSync(self.identity)
            self.models = ModelGateway(
                ModelLedger(
                    CloudModelRepo(db), self.identity, get_fernet(SecretRepo(db), PURPOSE_PROVIDER)
                ),
                transport=self._provider_transport,
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
            assert self.models is not None
            await self.models.start()

    async def close(self) -> None:
        if self.db is not None:
            if self.models:
                await self.models.close()
                self.models = None
            self.resources = None
            self.sync = None
            self.identity = None
            db, self.db = self.db, None
            await asyncio.get_running_loop().run_in_executor(None, db.close)
