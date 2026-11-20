"""Desktop resource outbox, verified delivery cache and installation coordinator."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from octop.infra.db.repos.local_resources import LocalResourceRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.contracts import IdentityView
from octop.infra.resources.contracts import (
    PERSONAL_RESOURCE_LIMIT,
    PERSONAL_STORAGE_BYTES,
    AgentSpec,
    ChangePage,
    CopyInput,
    InstallReport,
    LocalResourceView,
    Mutation,
    ResourceView,
    SkillRef,
    SkillSpec,
    VersionView,
    digest_spec,
)
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop.infra.applications.local import LocalApplication


async def durable[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    future = asyncio.get_running_loop().run_in_executor(None, partial(function, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
        future.result()
        raise


def local_view(row: dict[str, Any]) -> LocalResourceView:
    return LocalResourceView.model_validate(
        {**row, "spec": json.loads(row["spec"])}, extra="ignore"
    )


class LocalResources:
    def __init__(
        self,
        runtime: LocalApplication,
        *,
        client: httpx.AsyncClient,
        cloud_url: str,
        identity: IdentityView,
        access: Callable[..., Awaitable[str]],
    ) -> None:
        self.runtime, self.client, self.cloud_url = runtime, client, cloud_url
        self.identity, self.access = identity, access
        self.repo = LocalResourceRepo(runtime._storage().db)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.closed = False
        self._calls: set[asyncio.Task[Any]] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self.closed:
            try:
                await self.sync()
                await durable(self.repo.state, "last_error", "")
            except (OctopError, ValueError) as error:
                if self.closed:
                    return
                code = (
                    error.code.value
                    if isinstance(error, OctopError)
                    else ErrorCode.RESOURCE_INVALID.value
                )
                await durable(self.repo.state, "last_error", code)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            self._wake.clear()

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            if self.closed:
                raise OctopError.localized(ErrorCode.AUTH_FAILED)
            async with self._lock:
                yield
        except asyncio.CancelledError:
            if self.closed:
                raise OctopError.localized(ErrorCode.AUTH_FAILED) from None
            raise
        finally:
            self._calls.discard(task)

    async def close(self) -> None:
        self.closed = True
        self._wake.set()
        current = asyncio.current_task()
        tasks = {t for t in self._calls if t is not current and not t.done()}
        if self._task and self._task is not current and not self._task.done():
            tasks.add(self._task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self.closed:
            raise OctopError.localized(ErrorCode.AUTH_FAILED)
        token = await self.access(expected_identity=self.identity)
        try:
            response = await self.client.request(
                method,
                self.cloud_url + "/api/" + path,
                headers={"Authorization": "Bearer " + token},
                **kwargs,
            )
        except httpx.HTTPError as error:
            raise OctopError.localized(ErrorCode.CLOUD_UNAVAILABLE) from error
        if response.is_error:
            try:
                code = ErrorCode(response.json()["error"]["code"])
            except (ValueError, KeyError, TypeError):
                code = (
                    ErrorCode.RESOURCE_INVALID
                    if response.status_code == 422
                    else ErrorCode.CLOUD_UNAVAILABLE
                )
            raise OctopError.localized(code)
        return response

    async def list_resources(self) -> list[LocalResourceView]:
        return [local_view(row) for row in await durable(self.repo.list_resources)]

    async def _edit(self, resource_id: str, body: Mutation) -> LocalResourceView:
        previous = await durable(self.repo.operation, body.operation_id)
        row = await durable(self.repo.get, resource_id)
        if previous:
            if (
                previous["resource_id"] != resource_id
                or previous["mutation"] != body.model_dump_json()
            ):
                raise OctopError.localized(ErrorCode.RESOURCE_CONFLICT)
            assert row is not None
            return local_view(row)
        if row and row["scope"] != "personal":
            raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)
        if body.expected_revision != (row["revision"] if row else 0) or (
            row and row["spec"] == "null"
        ):
            raise OctopError.localized(ErrorCode.RESOURCE_CONFLICT)
        if row and (body.source or (body.spec and body.spec.kind != row["kind"])):
            raise OctopError.localized(ErrorCode.RESOURCE_INVALID)
        if not row and body.spec is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        all_rows = await durable(self.repo.list_resources)
        if (
            not row
            and sum(r["scope"] == "personal" and r["spec"] != "null" for r in all_rows)
            >= PERSONAL_RESOURCE_LIMIT
        ):
            raise OctopError.localized(ErrorCode.RESOURCE_QUOTA_EXCEEDED)
        if body.spec is not None and (
            sum(len(r["spec"].encode()) for r in all_rows if r["scope"] == "personal")
            + len(body.model_dump_json().encode())
            > PERSONAL_STORAGE_BYTES
        ):
            raise OctopError.localized(ErrorCode.RESOURCE_QUOTA_EXCEEDED)
        if isinstance(body.spec, AgentSpec):
            slugs = set()
            for ref in body.spec.skills:
                raw = await durable(self.repo.version, ref.resource_id, ref.revision)
                if (
                    not raw
                    or not isinstance(
                        (skill := VersionView.model_validate_json(raw)).spec, SkillSpec
                    )
                    or skill.spec.slug in slugs
                ):
                    raise OctopError.localized(ErrorCode.RESOURCE_INVALID)
                slugs.add(skill.spec.slug)
        assert body.spec is not None or row is not None
        kind = body.spec.kind if body.spec else (row or {})["kind"]
        revision = body.expected_revision + 1
        source_id = body.source.resource_id if body.source else (row["source_id"] if row else None)
        source_revision = (
            body.source.revision if body.source else (row["source_revision"] if row else None)
        )
        resource = ResourceView(
            resource_id=resource_id,
            scope="personal",
            kind=kind,
            owner_id=self.identity.user.user_id,
            revision=revision,
            source_id=source_id,
            source_revision=source_revision,
            deleted=body.spec is None,
        )
        snapshot = VersionView(
            resource=resource,
            revision=revision,
            spec=body.spec,
            digest=digest_spec(body.spec),
            published=True,
        )
        await durable(
            self.repo.draft,
            resource_id,
            resource.kind,
            revision,
            body.spec.model_dump_json() if body.spec else "null",
            snapshot.model_dump_json(),
            source_id,
            source_revision,
            body.operation_id,
            body.model_dump_json(),
        )
        self._wake.set()
        result = await durable(self.repo.get, resource_id)
        assert result is not None
        return local_view(result)

    async def edit(self, resource_id: str, body: Mutation) -> LocalResourceView:
        async with self._operation():
            if self.closed:
                raise OctopError.localized(ErrorCode.AUTH_FAILED)
            await self._edit(resource_id, body)
            # Personal edits can take effect offline with an already available
            # factory; synchronization is not reported successful until ack.
            await self._install()
            saved = await durable(self.repo.get, resource_id)
            assert saved is not None
            return local_view(saved)

    async def copy(self, resource_id: str, body: CopyInput) -> LocalResourceView:
        async with self._operation():
            raw = await durable(self.repo.version, resource_id, body.revision)
            if raw is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            version = VersionView.model_validate_json(raw)
            if version.resource.scope != "platform" or version.spec is None:
                raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)
            spec = version.spec.model_copy(deep=True)
            if isinstance(spec, AgentSpec):
                references = []
                for ref in spec.skills:
                    skill_raw = await durable(self.repo.version, ref.resource_id, ref.revision)
                    if skill_raw is None:
                        raise OctopError.localized(ErrorCode.RESOURCE_INVALID)
                    skill = VersionView.model_validate_json(skill_raw)
                    if not isinstance(skill.spec, SkillSpec):
                        raise OctopError.localized(ErrorCode.RESOURCE_INVALID)
                    new_id = hashlib.sha256(
                        f"{body.operation_id}:{ref.resource_id}:{ref.revision}".encode()
                    ).hexdigest()
                    await self._edit(
                        new_id,
                        Mutation(
                            operation_id=new_id,
                            expected_revision=0,
                            spec=skill.spec,
                            source=ref,
                        ),
                    )
                    references.append(SkillRef(resource_id=new_id, revision=1))
                spec.skills = references
            result = await self._edit(
                body.resource_id,
                Mutation(
                    operation_id=body.operation_id,
                    expected_revision=0,
                    spec=spec,
                    source=SkillRef(resource_id=resource_id, revision=body.revision),
                ),
            )
            await self._install()
            saved = await durable(self.repo.get, result.resource_id)
            assert saved is not None
            return local_view(saved)

    async def _version(self, resource_id: str, revision: int) -> VersionView:
        response = await self._request("GET", f"resources/{resource_id}/versions/{revision}")
        try:
            version = VersionView.model_validate(response.json())
            if (
                version.resource.resource_id != resource_id
                or version.revision != revision
                or version.digest != digest_spec(version.spec)
            ):
                raise ValueError("resource manifest mismatch")
        except ValueError as error:
            raise OctopError.localized(ErrorCode.RESOURCE_INVALID) from error
        cached = await durable(self.repo.version, resource_id, revision)
        if cached and VersionView.model_validate_json(cached).digest != version.digest:
            raise OctopError.localized(ErrorCode.RESOURCE_INVALID)
        # Cache only verified manifests; embedded files validate size/digest in
        # SkillSpec before they can be persisted or installed.
        await durable(self.repo.cache, resource_id, revision, version.model_dump_json())
        return version

    async def sync(self, *, restore_personal: bool = False) -> list[LocalResourceView]:
        async with self._operation():
            if self.closed:
                raise OctopError.localized(ErrorCode.AUTH_FAILED)
            await durable(
                self.repo.new_binding,
                f"{self.identity.device_id}:{self.identity.binding_generation}",
            )
            blocked = set()
            for operation in await durable(self.repo.outbox):
                resource_id = operation["resource_id"]
                if resource_id in blocked or operation["state"] == "conflict":
                    blocked.add(resource_id)
                    continue
                try:
                    response = await self._request(
                        "PUT",
                        "resources/personal/" + resource_id,
                        json=json.loads(operation["mutation"]),
                    )
                except OctopError as error:
                    if error.code in {
                        ErrorCode.RESOURCE_CONFLICT,
                        ErrorCode.RESOURCE_INVALID,
                        ErrorCode.RESOURCE_QUOTA_EXCEEDED,
                        ErrorCode.RESOURCE_NOT_ALLOWED,
                        ErrorCode.NOT_FOUND,
                    }:
                        await durable(
                            self.repo.conflict,
                            resource_id,
                            operation["operation_id"],
                            error.code.value,
                        )
                        blocked.add(resource_id)
                        continue
                    raise
                result = ResourceView.model_validate(response.json())
                await durable(
                    self.repo.acknowledge, operation["operation_id"], resource_id, result.revision
                )
            saved_cursor = await durable(self.repo.state, "cursor")
            cursor = 0 if restore_personal else int(saved_cursor or "0")
            while True:
                response = await self._request(
                    "GET", "resources/changes", params={"cursor": cursor}
                )
                page = ChangePage.model_validate(response.json())
                for change in page.changes:
                    row = await durable(self.repo.get, change.resource_id)
                    if row and row["sync_state"] != "synced":
                        continue
                    if change.revision is None:
                        if row and row["scope"] == "personal":
                            tombstone = await self._version(
                                change.resource_id, change.resource.revision
                            )
                            await durable(
                                self.repo.receive,
                                change.resource_id,
                                "personal",
                                change.resource.kind,
                                tombstone.revision,
                                "null",
                                change.resource.source_id,
                                change.resource.source_revision,
                                row["agent_id"],
                            )
                            continue
                        await durable(self.repo.withdraw, change.resource_id)
                        continue
                    if change.resource.scope == "personal" and row is None and not restore_personal:
                        continue
                    instance_id = (
                        (change.resource_id if change.resource.scope == "personal" else new_ulid())
                        if change.resource.kind == "agent"
                        else None
                    )
                    await durable(
                        self.repo.note_delivery,
                        change.resource_id,
                        change.resource.scope,
                        change.resource.kind,
                        change.revision,
                        instance_id,
                    )
                    try:
                        version = await self._version(change.resource_id, change.revision)
                        if isinstance(version.spec, AgentSpec):
                            for ref in version.spec.skills:
                                await self._version(ref.resource_id, ref.revision)
                    except OctopError as error:
                        # An assignment can change after the change-page read.
                        # Do not advance its cursor until a fresh pull resolves it.
                        await durable(self.repo.failed, change.resource_id, error.code.value)
                        failed = await durable(self.repo.get, change.resource_id)
                        assert failed is not None
                        report = InstallReport(
                            resource_id=change.resource_id,
                            desired_revision=change.revision,
                            installed_revision=failed["installed_revision"],
                            state="failed",
                            error_code=error.code.value,
                            runtime_state=self.runtime.resource_state(failed["agent_id"]),
                        )
                        with suppress(OctopError):
                            await self._request(
                                "PUT", "resources/reports", json=report.model_dump()
                            )
                        raise
                    await durable(
                        self.repo.receive,
                        change.resource_id,
                        version.resource.scope,
                        version.resource.kind,
                        version.revision,
                        version.spec.model_dump_json() if version.spec else "null",
                        version.resource.source_id,
                        version.resource.source_revision,
                        (change.resource_id if version.resource.scope == "personal" else new_ulid())
                        if version.resource.kind == "agent"
                        else None,
                    )
                cursor = page.cursor
                await durable(self.repo.state, "cursor", str(cursor))
                if not page.has_more:
                    break
            await self._install()
            for row in await durable(self.repo.list_resources):
                # Never claim a dirty/conflicting personal draft as the desired
                # cloud revision. Its outbox will make a later report valid.
                if row["sync_state"] != "synced":
                    continue
                report = InstallReport(
                    resource_id=row["resource_id"],
                    desired_revision=row["desired_revision"],
                    installed_revision=row["installed_revision"],
                    state=row["install_state"],
                    error_code=row["error_code"],
                    runtime_state=self.runtime.resource_state(row["agent_id"]),
                )
                try:
                    await self._request("PUT", "resources/reports", json=report.model_dump())
                except OctopError as error:
                    if (
                        error.code == ErrorCode.NOT_FOUND
                        and row["scope"] == "platform"
                        and row["desired_revision"] is None
                    ):
                        # A replacement binding has no assignment to report.
                        # Keep its archive locally without claiming cloud access.
                        continue
                    if error.code != ErrorCode.RESOURCE_CONFLICT:
                        raise
            return await self.list_resources()

    async def install_pending(self) -> None:
        async with self._operation():
            await self._install()

    def changed(self) -> None:
        self._wake.set()

    async def _install(self) -> None:
        for row in await durable(self.repo.list_resources):
            if row["install_state"] in {"installed", "removed"}:
                continue
            try:
                if row["desired_revision"] is None:
                    await self.runtime.remove_resource(row["resource_id"], self.repo)
                    continue
                raw = await durable(self.repo.version, row["resource_id"], row["desired_revision"])
                if raw is None:
                    raise OctopError.localized(ErrorCode.RESOURCE_INVALID)
                version = VersionView.model_validate_json(raw)
                if isinstance(version.spec, AgentSpec):
                    skills = []
                    for ref in version.spec.skills:
                        content = await durable(self.repo.version, ref.resource_id, ref.revision)
                        if not content or not isinstance(
                            (dependency := VersionView.model_validate_json(content)).spec, SkillSpec
                        ):
                            raise OctopError.localized(ErrorCode.RESOURCE_INVALID)
                        skills.append(dependency.spec)
                    await self.runtime.install_resource(version, skills, self.repo)
                elif isinstance(version.spec, SkillSpec):
                    await durable(
                        self.repo.installed,
                        row["resource_id"],
                        version.revision,
                        version.model_dump_json(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = (
                    error.code.value
                    if isinstance(error, OctopError)
                    else ErrorCode.RESOURCE_INSTALL_FAILED.value
                )
                await durable(self.repo.failed, row["resource_id"], code)
