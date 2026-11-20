"""Ephemeral cloud authorization and durable, account-scoped local runtimes.

Only the Electron main process owns the renewable cloud credential. This
service receives short-lived access and verifies it at the configured cloud.
Account namespaces are derived from server identity, never client paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from octop.application_config import LocalSettings
from octop.infra.applications.local import LocalApplication
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.contracts import IdentityView, TokenView

if TYPE_CHECKING:
    from octop_harness.llm.factory import ChatModelFactory


class LocalAccounts:
    def __init__(
        self,
        settings: LocalSettings,
        *,
        model_factory: ChatModelFactory | None = None,
        cloud_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._model_factory = model_factory
        self._test_sandbox = model_factory is not None
        self.runtime: LocalApplication | None = (
            LocalApplication(settings, model_factory=model_factory) if self._test_sandbox else None
        )
        self.identity: IdentityView | None = None
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._retiring: asyncio.Task[None] | None = None
        self._client = httpx.AsyncClient(
            transport=cloud_transport, trust_env=False, follow_redirects=False, timeout=10
        )
        self.model_origin: str | None = None
        self._preparing = False
        self._prepared = asyncio.Event()
        self._prepared.set()
        self.started = False

    async def start(self) -> None:
        if self.runtime:
            await self.runtime.start()
        self.started = True

    async def close(self) -> None:
        async with self._idle_lock():
            try:
                await self._detach()
            finally:
                try:
                    await self._client.aclose()
                    if self._model_factory:
                        await self._model_factory.aclose()
                finally:
                    self.started = False

    def _retire_revoked(self) -> None:
        # Revocation can be discovered inside an installation/model callback.
        # Clear authority immediately, then retire outside that callback's task
        # so cleanup never awaits a parent that is waiting for this callback.
        self.identity = None
        self._access_token = None
        self._expires_at = 0
        if self.runtime:
            previous, self.runtime = self.runtime, None
            self._retiring = asyncio.create_task(previous.close(close_model_factory=False))

    async def _detach(self) -> None:
        if self._retiring:
            retiring, self._retiring = self._retiring, None
            await retiring
        self.identity = None
        self._access_token = None
        self._expires_at = 0
        if self.runtime:
            previous, self.runtime = self.runtime, None
            await previous.close(close_model_factory=False)

    async def detach(self) -> None:
        async with self._idle_lock():
            await self._detach()

    @asynccontextmanager
    async def _idle_lock(self) -> AsyncIterator[None]:
        while True:
            await self._prepared.wait()
            async with self._lock:
                # A preparation may have acquired the lock ahead of us after
                # the event was read. Its final upload still needs this lock.
                if self._preparing:
                    continue
                yield
                return

    async def configure_cloud(self, cloud_url: str) -> None:
        from octop.infra.resources.local import durable
        from octop.infra.utils.json_file import read_json_object, write_json_atomic

        async with self._lock:
            if self.identity or self.runtime or self._preparing:
                raise OctopError.localized(ErrorCode.AUTH_FAILED)
            settings = LocalSettings.model_validate(
                {**self.settings.model_dump(), "cloud_url": cloud_url}
            )

            def save() -> None:
                path = settings.home / "config.json"
                data = read_json_object(path) or {}
                data["cloud_url"] = settings.cloud_url
                settings.home.mkdir(parents=True, exist_ok=True)
                write_json_atomic(path, data)

            await durable(save)
            self.settings = settings

    async def prepare_shutdown(self) -> None:
        async with self._lock:
            if self._preparing:
                raise OctopError.localized(ErrorCode.LOCAL_RUNTIME_PAUSED)
            self._preparing = True
            self._prepared.clear()
            runtime = self.runtime
        try:
            # Do not hold the account lock while a final upload acquires its
            # identity-scoped device access. Concurrent logout/revocation can
            # still retire this runtime and deny that upload.
            if runtime:
                await runtime.prepare_shutdown()
        finally:
            async with self._lock:
                try:
                    if self.runtime is runtime:
                        await self._detach()
                finally:
                    self._preparing = False
                    self._prepared.set()

    async def _validate(self, access_token: str) -> IdentityView:
        if not self.settings.cloud_url:
            raise OctopError.localized(ErrorCode.CLOUD_UNAVAILABLE)
        try:
            response = await self._client.get(
                self.settings.cloud_url + "/api/auth/device/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OctopError.localized(ErrorCode.CLOUD_UNAVAILABLE) from exc
        if response.status_code in {401, 403}:
            raise OctopError.localized(ErrorCode.DEVICE_REVOKED)
        if response.status_code != 200:
            raise OctopError.localized(ErrorCode.CLOUD_UNAVAILABLE)
        try:
            identity = IdentityView.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise OctopError.localized(ErrorCode.CLOUD_UNAVAILABLE) from exc
        if not identity.device_id or identity.binding_generation is None:
            raise OctopError.localized(ErrorCode.AUTH_FAILED)
        return identity

    async def attach(self, grant: TokenView) -> IdentityView:
        async with self._lock:
            if self._preparing:
                raise OctopError.localized(ErrorCode.LOCAL_RUNTIME_PAUSED)
            verified = await self._validate(grant.access_token)
            if verified != grant.identity:
                raise OctopError.localized(ErrorCode.AUTH_FAILED)
            from octop.infra.sync.local import require_sync_runtime

            require_sync_runtime()
            if self.identity != verified or self.runtime is None:
                await self._detach()
                namespace = hashlib.sha256(
                    f"{self.settings.cloud_url}:{verified.cloud_id}:{verified.user.user_id}".encode()
                ).hexdigest()
                scoped = self.settings.model_copy(
                    update={
                        "home": self.settings.home / "accounts" / namespace,
                        "default_timezone": verified.timezone,
                    }
                )
                platform = None
                if not self._test_sandbox and self.model_origin and self.settings.cloud_url:
                    from octop.infra.applications.local_models import LocalModels

                    platform = LocalModels(
                        client=self._client,
                        cloud_url=self.settings.cloud_url,
                        local_origin=self.model_origin,
                        identity=verified,
                        access=self.device_access,
                    )
                runtime = LocalApplication(
                    scoped, model_factory=self._model_factory, model_platform=platform
                )
                if platform:
                    platform.runtime = runtime
                await runtime.start()
                self.runtime = runtime
            self.identity = verified
            self._access_token = grant.access_token
            self._expires_at = time.monotonic() + min(grant.expires_in, 300)
            if self.runtime and self.runtime.resources is None and self.settings.cloud_url:
                from octop.infra.resources.local import LocalResources

                self.runtime.resources = LocalResources(
                    self.runtime,
                    client=self._client,
                    cloud_url=self.settings.cloud_url,
                    identity=verified,
                    access=self.device_access,
                )
                from octop.infra.sync.local import LocalSync

                self.runtime.sync = LocalSync(self.runtime, self.runtime.resources)
                await self.runtime.sync.recover()
                self.runtime.resources.start()
                self.runtime.sync.start()
            return verified

    def require_runtime(self) -> LocalApplication:
        # Only trusted Python test composition can use the P1 sandbox. A
        # production launch always requires a verified device to select data.
        if self.runtime is None or (self.identity is None and not self._test_sandbox):
            raise OctopError.localized(ErrorCode.AUTH_FAILED)
        return self.runtime

    async def device_access(self, *, expected_identity: IdentityView) -> str:
        """P3/P5 must acquire authorization here before any cloud write/call.

        No cached generation claim is a permission grant. The cloud also
        revalidates its authoritative binding inside each future write.
        """
        async with self._lock:
            if expected_identity != self.identity:
                raise OctopError.localized(ErrorCode.AUTH_FAILED)
            if not self._access_token or time.monotonic() >= self._expires_at:
                raise OctopError.localized(ErrorCode.AUTH_FAILED)
            try:
                verified = await self._validate(self._access_token)
            except OctopError as exc:
                if exc.code == ErrorCode.DEVICE_REVOKED:
                    self._retire_revoked()
                raise
            if verified != self.identity:
                self._retire_revoked()
                raise OctopError.localized(ErrorCode.DEVICE_REVOKED)
            return self._access_token
