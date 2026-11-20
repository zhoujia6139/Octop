"""Explicit backup restoration with a durable interruption barrier and fixed snapshot.

Content is downloaded and verified before visible reference rows are committed.
A failed/interrupted Agent restoration keeps its barrier, blocking execution and
normal scans until an explicit retry finishes the same cloud watermark.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import TYPE_CHECKING

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.resources.contracts import AgentSpec, SkillSpec, VersionView
from octop.infra.resources.local import durable
from octop.infra.sync.contracts import (
    CHUNK_BYTES,
    FileData,
    MemoryData,
    MessageData,
    Page,
    Record,
    RestoreResult,
    digest,
    sync_path,
)
from octop.infra.sync.projection import stable_id

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace

    from octop.infra.sync.local import LocalSync


def invalid() -> OctopError:
    return OctopError.localized(ErrorCode.SYNC_INVALID)


async def download(service: LocalSync, data: FileData) -> None:
    hasher = hashlib.sha256()
    for offset in range(0, data.size, CHUNK_BYTES):
        content = await durable(service.repo.chunk, data.sha256, offset)
        if content is None:
            content = (
                await service._request("GET", f"blobs/{data.sha256}/chunks/{offset}")
            ).content
            if len(content) != min(CHUNK_BYTES, data.size - offset):
                raise invalid()
            await durable(service.repo.chunk, data.sha256, offset, content)
        hasher.update(content)
    if hasher.hexdigest() != data.sha256:
        await durable(service.repo.remove_chunks, data.sha256)
        raise invalid()


async def verify_existing(
    service: LocalSync, workspace: BackendWorkspace, records: list[Record]
) -> None:
    allowed: dict[str, set[str]] = {}
    for row in await service.records(agent_id=records[0].agent_id, kind="file"):
        if row["data"]:
            allowed.setdefault(row["data"]["path"], set()).add(row["data"]["sha256"])
    for record in records:
        if isinstance(record.data, FileData):
            allowed.setdefault(record.data.path, set()).add(record.data.sha256)
    for entry in await durable(workspace.scan_sync_files, ["inbound", "artifacts"]):
        try:
            path = sync_path(unicodedata.normalize("NFC", entry.path))
        except ValueError:
            continue
        hasher = hashlib.sha256()
        for offset in range(0, entry.size, CHUNK_BYTES):
            hasher.update(
                await durable(
                    workspace.read_sync_chunk, entry.path, offset, CHUNK_BYTES, entry.fingerprint
                )
            )
        await durable(workspace.read_sync_chunk, entry.path, entry.size, 1, entry.fingerprint)
        if hasher.hexdigest() not in allowed.get(path, set()):
            raise OctopError.localized(ErrorCode.SYNC_CONFLICT)


async def restore_agent(
    service: LocalSync,
    records: list[Record],
    file_versions: dict[tuple[str, str], FileData],
    memory_identity: dict[str, MemoryData],
) -> None:
    from octop_memory.operations.sync import MemoryChange

    agent_id, resource_id = records[0].agent_id, records[0].resource_id
    resource = await durable(service.resources.repo.get, resource_id)
    # The marker persists across every cancellation point, including a crash
    # after the final transaction but before clearing this barrier.
    await durable(service.repo.state, "restore_agent:" + agent_id, "1")
    can_run = (
        resource is not None
        and resource["installed_snapshot"] is not None
        and resource["sync_state"] == "synced"
        and resource["desired_revision"] is not None
    )
    version: VersionView | None = None
    skills: list[SkillSpec] = []
    if can_run:
        assert resource is not None
        async with service.runtime._agent_lock:
            for instance in {resource["agent_id"], agent_id}:
                if previous := service.runtime._agents.pop(instance, None):
                    await service.runtime._close_agent(previous)
            await durable(service.repo.restore_agent, agent_id, resource_id)
        version = VersionView.model_validate_json(resource["installed_snapshot"])
        assert isinstance(version.spec, AgentSpec)
        for ref in version.spec.skills:
            snapshot = await durable(service.resources.repo.version, ref.resource_id, ref.revision)
            dependency = VersionView.model_validate_json(snapshot or "null")
            if not isinstance(dependency.spec, SkillSpec):
                raise invalid()
            skills.append(dependency.spec)
    else:
        # Backup ownership survives withdrawal of execution permissions. Keep
        # transcripts, files and logical memory viewable in a disabled instance.
        async with service.runtime._agent_lock:
            if previous := service.runtime._agents.pop(agent_id, None):
                await service.runtime._close_agent(previous)
            await durable(service.repo.restore_archive_agent, agent_id, resource_id)
    workspace = await service.workspace(agent_id)
    await verify_existing(service, workspace, records)
    required: dict[str, FileData] = {}
    for record in records:
        if isinstance(record.data, FileData):
            required[record.data.sha256] = record.data
        if isinstance(record.data, MessageData):
            for attachment in record.data.attachments:
                file = file_versions.get((attachment.file_id, attachment.sha256))
                if file is None:
                    raise invalid()
                required[file.sha256] = file
    for file in required.values():
        await download(service, file)
    # Memory validation and application is one library-owned transaction; it
    # also rebuilds the relational indexes and excludes checkpoints entirely.
    memory = await service._memory(agent_id)
    try:
        changes = []
        for record in records:
            source = memory_identity.get(record.item_id)
            if record.kind == "memory" and source:
                changes.append(
                    MemoryChange(
                        record.cursor,
                        source.table,
                        source.key,
                        record.revision,
                        source.record if record.data is not None else None,
                    )
                )
        await durable(memory.restore_sync_changes, changes)
    finally:
        await durable(memory.close)
    for record in records:
        if isinstance(record.data, FileData):
            file = record.data
            # Stable per-item staging names make retry after a process crash
            # idempotent. They remain excluded from normal export roots.
            staged = ".octop/sync-staging/" + record.item_id + "-" + str(record.revision)
            await durable(workspace.remove_sync_file, staged)
            await durable(workspace.write_sync_chunk, staged, 0, b"")
            for offset in range(0, file.size, CHUNK_BYTES):
                content = await durable(service.repo.chunk, file.sha256, offset)
                assert content is not None
                await durable(workspace.write_sync_chunk, staged, offset, content)
            await durable(workspace.commit_sync_file, staged, file.path)
        elif record.kind == "file" and record.data is None:
            versions = [
                data for (item_id, _), data in file_versions.items() if item_id == record.item_id
            ]
            if versions:
                await durable(workspace.remove_sync_file, versions[-1].path)
    if version is not None:
        # Start the replacement only after logical restoration, so its memory
        # timers cannot write during the exclusive recovery boundary.
        await service.runtime.install_resource(version, skills, service.resources.repo)
    await durable(service.repo.restore, [record.model_dump(mode="json") for record in records])
    for record in sorted(records, key=lambda item: item.cursor):
        if record.kind == "memory" and (source := memory_identity.get(record.item_id)):
            import json

            base = stable_id(agent_id, "memory", source.table, json.dumps(source.key))
            await durable(service.repo.state, "memory_item:" + base, record.item_id)
        elif record.kind == "file":
            versions = [
                data for (item_id, _), data in file_versions.items() if item_id == record.item_id
            ]
            if versions:
                await durable(
                    service.repo.state, "file:" + agent_id + ":" + versions[-1].path, record.item_id
                )
    await service.recover()
    await durable(service.repo.state, "restore_agent:" + agent_id, "")


async def restore(service: LocalSync) -> RestoreResult:
    if service.runtime._active or service.runtime._pending:
        raise OctopError.localized(ErrorCode.AGENT_BUSY)
    cursor = 0
    saved = await durable(service.repo.state, "active_restore_watermark")
    watermark = int(saved) if saved else None
    latest: dict[str, Record] = {}
    file_versions: dict[tuple[str, str], FileData] = {}
    memory_identity: dict[str, MemoryData] = {}
    while True:
        params: dict[str, int] = {"cursor": cursor}
        if watermark is not None:
            params["watermark"] = watermark
        page = Page.model_validate((await service._request("GET", "changes", params=params)).json())
        if (watermark is not None and page.watermark != watermark) or (
            page.has_more and page.cursor <= cursor
        ):
            raise invalid()
        watermark = page.watermark
        await durable(service.repo.state, "active_restore_watermark", str(watermark))
        for record in page.records:
            previous = latest.get(record.item_id)
            if (
                record.cursor <= cursor
                or record.cursor > watermark
                or digest(record.data) != record.digest
                or (
                    previous
                    and (
                        record.revision != previous.revision + 1
                        or record.agent_id != previous.agent_id
                        or record.kind != previous.kind
                    )
                )
            ):
                raise invalid()
            latest[record.item_id] = record
            if isinstance(record.data, MemoryData):
                memory_identity[record.item_id] = record.data
            if isinstance(record.data, FileData):
                file_versions[record.item_id, record.data.sha256] = record.data
            cursor = record.cursor
        if page.cursor != cursor:
            raise invalid()
        if not page.has_more:
            break
    await service._scan()
    local = await durable(service.repo.list)
    dirty_agents = {row["agent_id"] for row in local if row["state"] != "synced"}
    dirty_resources = {row["resource_id"] for row in local if row["state"] != "synced"}
    restored, skipped = 0, 0
    for agent_id in dict.fromkeys(record.agent_id for record in latest.values()):
        records = [record for record in latest.values() if record.agent_id == agent_id]
        resource_id = records[0].resource_id
        if agent_id in dirty_agents or resource_id in dirty_resources:
            skipped += len(records)
            continue
        paths: set[str] = set()
        for record in records:
            if record.resource_id != resource_id:
                raise invalid()
            if isinstance(record.data, FileData):
                path = record.data.path.casefold()
                if path in paths:
                    raise invalid()
                paths.add(path)
            if isinstance(record.data, MessageData):
                thread = latest.get(record.data.thread_id)
                if not thread or thread.agent_id != agent_id or thread.kind != "thread":
                    raise invalid()
        await restore_agent(service, records, file_versions, memory_identity)
        restored += len(records)
    await durable(service.repo.state, "restore_watermark", str(watermark or 0))
    await durable(service.repo.state, "active_restore_watermark", "")
    return RestoreResult(watermark=watermark or 0, restored=restored, skipped=skipped)
