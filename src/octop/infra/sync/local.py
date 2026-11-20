"""Account-bound durable data uploads and explicit, integrity-checked restoration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

import httpx

from octop.infra.db.repos.local_sync import LocalSyncRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.resources.local import LocalResources, durable
from octop.infra.sync.contracts import (
    CHUNK_BYTES,
    FILE_BYTES,
    Attachment,
    BlobState,
    FileData,
    Kind,
    MemoryData,
    Mutation,
    Record,
    RestoreResult,
    SyncState,
    ThreadData,
    digest,
    sync_path,
)
from octop.infra.sync.projection import Projection, change, stable_id
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace
    from octop_memory import Memory

    from octop.infra.applications.local import LocalApplication


def require_sync_runtime() -> None:
    from octop_harness.backends.workspace import BackendWorkspace
    from octop_memory import Memory

    if not all(
        callable(getattr(owner, name, None))
        for owner, name in (
            (BackendWorkspace, "for_local_directory"),
            (BackendWorkspace, "scan_sync_files"),
            (BackendWorkspace, "read_sync_chunk"),
            (BackendWorkspace, "write_sync_chunk"),
            (BackendWorkspace, "commit_sync_file"),
            (BackendWorkspace, "remove_sync_file"),
            (Memory, "sync_changes"),
            (Memory, "restore_sync_changes"),
            (Memory, "close"),
        )
    ):
        raise OctopError.localized(ErrorCode.LOCAL_RUNTIME_UPGRADE_REQUIRED)


class LocalSync:
    def __init__(self, runtime: LocalApplication, resources: LocalResources) -> None:
        self.runtime, self.resources = runtime, resources
        self.repo = LocalSyncRepo(runtime._storage().db)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._calls: set[asyncio.Task[Any]] = set()
        self.closed = False
        self.restoring = False

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            async with self._lock:
                if self.closed:
                    raise OctopError.localized(ErrorCode.AUTH_FAILED)
                yield
        except asyncio.CancelledError:
            if self.closed:
                raise OctopError.localized(ErrorCode.AUTH_FAILED) from None
            raise
        except (OSError, ValueError) as exc:
            raise OctopError.localized(ErrorCode.SYNC_INVALID) from exc
        finally:
            self._calls.discard(task)

    async def ensure_ready(self, agent_id: str, thread_id: str | None = None) -> None:
        marker = await durable(self.repo.state, "restore_agent:" + agent_id)
        if self.restoring or marker:
            raise OctopError.localized(ErrorCode.SYNC_CONFLICT)
        if thread_id:
            row = await durable(self.repo.get, thread_id)
            if row and row["data"] == "null":
                raise OctopError.localized(ErrorCode.NOT_FOUND)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def changed(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self.closed:
            try:
                await self.sync()
            except (OctopError, OSError, ValueError) as exc:
                if self.closed:
                    return
                await durable(
                    self.repo.state,
                    "last_error",
                    exc.code.value if isinstance(exc, OctopError) else ErrorCode.SYNC_INVALID.value,
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            self._wake.clear()

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

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self.closed:
            raise OctopError.localized(ErrorCode.AUTH_FAILED)
        return await self.resources._request(method, "sync/" + path, **kwargs)

    def _resource_id(self, agent_id: str) -> str:
        agent = self.runtime._storage().agents.get(agent_id)
        if not agent:
            raise OctopError.localized(ErrorCode.AGENT_NOT_FOUND)
        return agent.resource_id or self.repo.source(agent_id) or agent.agent_id

    async def workspace(self, agent_id: str) -> BackendWorkspace:
        from octop_harness.backends.workspace import BackendWorkspace

        await durable(self._resource_id, agent_id)
        return await durable(
            BackendWorkspace.for_local_directory,
            self.runtime.settings.home / "workspaces" / agent_id,
        )

    async def _memory(self, agent_id: str) -> Memory:
        from octop_memory import Memory

        await durable(self._resource_id, agent_id)
        return await durable(
            Memory,
            namespace="agent_" + agent_id,
            backend_config={
                "db_path": str(self.runtime.settings.home / "memory" / (agent_id + ".sqlite"))
            },
        )

    async def recover(self) -> None:
        await durable(self.repo.discard_abandoned_chunks)
        await durable(
            self.resources.repo.new_binding,
            f"{self.resources.identity.device_id}:{self.resources.identity.binding_generation}",
        )
        binding = json.dumps(
            [self.resources.identity.device_id, self.resources.identity.binding_generation]
        )
        previous = await durable(self.repo.state, "binding")
        if previous and binding != previous:
            await durable(self.repo.changed_binding)
        await durable(self.repo.state, "binding", binding)
        changes = []
        for row in await durable(self.repo.list):
            if row["kind"] not in {"thread", "message"} or row["data"] == "null":
                continue
            data = json.loads(row["data"])
            if data.get("status") in {"running", "awaiting_approval"}:
                data["status"] = "interrupted"
                changes.append(
                    (
                        row["item_id"],
                        row["agent_id"],
                        row["resource_id"],
                        row["kind"],
                        json.dumps(data),
                        digest(data),
                    )
                )
        await durable(self.repo.batch, changes)

    async def create_thread(self, thread_id: str, agent_id: str) -> None:
        async with self._operation():
            await self.ensure_ready(agent_id)
            resource_id = await durable(self._resource_id, agent_id)
            await durable(
                self.repo.batch,
                [
                    change(
                        thread_id,
                        agent_id,
                        resource_id,
                        ThreadData(created_at=int(time.time()), updated_at=int(time.time())),
                        "thread",
                    )
                ],
                thread=(thread_id, agent_id),
            )
        self.changed()

    async def projection(self, agent_id: str, thread_id: str, turn_id: str) -> Projection:
        return Projection(
            self.repo, agent_id, await durable(self._resource_id, agent_id), thread_id, turn_id
        )

    async def records(
        self, *, agent_id: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await durable(self.repo.list, agent_id, kind)
        return [{**row, "data": json.loads(row["data"])} for row in rows]

    async def status(self) -> SyncState:
        rows = await durable(self.repo.list)
        return SyncState(
            pending=sum(r["state"] == "pending" for r in rows),
            conflicts=sum(r["state"] == "conflict" for r in rows),
            last_error=await durable(self.repo.state, "last_error") or None,
            scan_generation=int(await durable(self.repo.state, "scan_generation") or 0),
        )

    async def _save(self, item_id: str, agent_id: str, data: Any, kind: Kind) -> None:
        resource_id = await durable(self._resource_id, agent_id)
        # Validate the exact persisted envelope before it can enter an outbox.
        row = await durable(self.repo.get, item_id)
        body = Mutation(
            operation_id=new_ulid(),
            item_id=item_id,
            agent_id=agent_id,
            resource_id=resource_id,
            kind=kind,
            expected_revision=row["revision"] if row else 0,
            data=data,
        )
        await durable(
            self.repo.save,
            item_id,
            agent_id,
            resource_id,
            kind,
            body.data.model_dump_json() if body.data else "null",
            digest(body.data),
        )

    async def _scan_files(self, agent_id: str) -> None:
        workspace = await self.workspace(agent_id)
        entries = await durable(workspace.scan_sync_files, ["inbound", "artifacts"])
        present = set()
        # Fail rather than permit case/normalization aliases across OSes.
        keys: set[str] = set()
        for entry in entries:
            try:
                path = sync_path(unicodedata.normalize("NFC", entry.path))
            except ValueError:
                continue
            if path.casefold() in keys or entry.size > FILE_BYTES:
                raise OctopError.localized(ErrorCode.SYNC_INVALID)
            keys.add(path.casefold())
            item_id = stable_id(agent_id, "file", path)
            mapping = "file:" + agent_id + ":" + path
            item_id = await durable(self.repo.state, mapping) or item_id
            existing = await durable(self.repo.get, item_id)
            if existing and existing["data"] == "null":
                # Deleted IDs never revive. A recreated path gets a new ID,
                # while stable scan state remembers it across retries.
                item_id = new_ulid()
                await durable(self.repo.state, mapping, item_id)
            present.add(item_id)
            staged = "staged-" + new_ulid()
            hasher = hashlib.sha256()
            try:
                for offset in range(0, entry.size, CHUNK_BYTES):
                    content = await durable(
                        workspace.read_sync_chunk,
                        entry.path,
                        offset,
                        CHUNK_BYTES,
                        entry.fingerprint,
                    )
                    if len(content) != min(CHUNK_BYTES, entry.size - offset):
                        raise OSError("incomplete workspace file")
                    hasher.update(content)
                    await durable(self.repo.chunk, staged, offset, content)
                # Also validate an empty file and detect a final change before
                # installing its content-addressed immutable upload payload.
                await durable(
                    workspace.read_sync_chunk, entry.path, entry.size, 1, entry.fingerprint
                )
                checksum = hasher.hexdigest()
                await durable(self.repo.finish_chunks, staged, checksum)
                await self._save(
                    item_id, agent_id, FileData(path=path, sha256=checksum, size=entry.size), "file"
                )
            finally:
                await durable(self.repo.finish_chunks, staged, None)
        # This point is reached only after a complete successful scan. Partial
        # enumerations, unreadable files and changing files cannot delete data.
        for row in await durable(self.repo.list, agent_id, "file"):
            if row["data"] != "null" and row["item_id"] not in present:
                await self._save(row["item_id"], agent_id, None, "file")

    async def _scan_memory(self, agent_id: str) -> None:
        memory = await self._memory(agent_id)
        try:
            key = "memory_cursor:" + agent_id
            cursor = int(await durable(self.repo.state, key) or 0)
            while True:
                page = await durable(memory.sync_changes, cursor)
                for entry in page.changes:
                    item_id = stable_id(agent_id, "memory", entry.table, json.dumps(entry.key))
                    mapping = "memory_item:" + item_id
                    item_id = await durable(self.repo.state, mapping) or item_id
                    if entry.data is None:
                        await self._save(item_id, agent_id, None, "memory")
                    else:
                        existing = await durable(self.repo.get, item_id)
                        if existing and existing["data"] == "null":
                            # Memory primary keys can be intentionally reused;
                            # source revision provides a new nonreviving ID.
                            item_id = stable_id(item_id, str(entry.revision))
                            await durable(self.repo.state, mapping, item_id)
                        await self._save(
                            item_id,
                            agent_id,
                            MemoryData(table=entry.table, key=entry.key, record=entry.data),
                            "memory",
                        )
                await durable(self.repo.state, key, str(page.cursor))
                cursor = page.cursor
                if not page.has_more:
                    break
        finally:
            await durable(memory.close)

    async def _scan(self) -> None:
        restoring = await durable(self.repo.restoring)
        for agent in await durable(self.runtime._storage().agents.list):
            if self.runtime.agent_busy(agent.agent_id) or agent.agent_id in restoring:
                continue
            await self._scan_files(agent.agent_id)
            await self._scan_memory(agent.agent_id)
        generation = int(await durable(self.repo.state, "scan_generation") or 0) + 1
        await durable(self.repo.state, "scan_generation", str(generation))

    async def _upload_blob(self, data: FileData) -> None:
        state = BlobState.model_validate(
            (
                await self._request(
                    "POST", "blobs", json={"sha256": data.sha256, "size": data.size}
                )
            ).json()
        )
        if state.sha256 != data.sha256 or state.size != data.size:
            raise OctopError.localized(ErrorCode.SYNC_INVALID)
        if state.complete:
            return
        for offset in range(0, data.size, CHUNK_BYTES):
            if offset in state.offsets:
                continue
            content = await durable(self.repo.chunk, data.sha256, offset)
            if content is None:
                raise OctopError.localized(ErrorCode.SYNC_INVALID)
            await self._request(
                "PUT",
                f"blobs/{data.sha256}/chunks/{offset}",
                content=content,
            )
        completed = BlobState.model_validate(
            (await self._request("POST", f"blobs/{data.sha256}/complete")).json()
        )
        if not completed.complete or completed.sha256 != data.sha256:
            raise OctopError.localized(ErrorCode.SYNC_INVALID)

    async def sync(self) -> SyncState:
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            async with self._lock:
                if self.closed:
                    raise OctopError.localized(ErrorCode.AUTH_FAILED)
                await self.resources.sync()
                await self._scan()
                for operation in await durable(self.repo.pending):
                    claimed = await durable(self.repo.claim, operation["operation_id"])
                    if claimed is None:
                        continue
                    operation = claimed
                    try:
                        body = Mutation.model_validate_json(operation["mutation"])
                    except ValueError:
                        await durable(
                            self.repo.conflict,
                            operation["operation_id"],
                            operation["item_id"],
                            ErrorCode.SYNC_INVALID.value,
                        )
                        break
                    try:
                        if isinstance(body.data, FileData):
                            await self._upload_blob(body.data)
                        record = Record.model_validate(
                            (
                                await self._request(
                                    "PUT", "records", json=body.model_dump(mode="json")
                                )
                            ).json()
                        )
                        if (
                            record.item_id != body.item_id
                            or record.agent_id != body.agent_id
                            or record.resource_id != body.resource_id
                            or record.kind != body.kind
                            or record.revision != operation["revision"]
                            or record.digest != digest(body.data)
                        ):
                            raise OctopError.localized(ErrorCode.SYNC_INVALID)
                        await durable(
                            self.repo.acknowledge, body.operation_id, body.item_id, record.revision
                        )
                    except OctopError as exc:
                        if exc.code in {
                            ErrorCode.SYNC_CONFLICT,
                            ErrorCode.SYNC_QUOTA_EXCEEDED,
                            ErrorCode.SYNC_INVALID,
                            ErrorCode.RESOURCE_NOT_ALLOWED,
                            ErrorCode.NOT_FOUND,
                        }:
                            await durable(
                                self.repo.conflict, body.operation_id, body.item_id, exc.code.value
                            )
                            # Subsequent revisions of this same item must not
                            # cross a failed predecessor in this batch.
                            break
                        raise
                await durable(self.repo.state, "last_error", "")
                return await self.status()
        except OctopError as exc:
            if not self.closed:
                await durable(self.repo.state, "last_error", exc.code.value)
            raise
        except (OSError, ValueError) as exc:
            if not self.closed:
                await durable(self.repo.state, "last_error", ErrorCode.SYNC_INVALID.value)
            raise OctopError.localized(ErrorCode.SYNC_INVALID) from exc
        except asyncio.CancelledError:
            if self.closed:
                raise OctopError.localized(ErrorCode.AUTH_FAILED) from None
            raise
        finally:
            self._calls.discard(task)

    async def delete(self, item_id: str) -> None:
        async with self._operation():
            row = await durable(self.repo.get, item_id)
            if not row:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            if self.runtime.agent_busy(row["agent_id"]):
                raise OctopError.localized(ErrorCode.AGENT_BUSY)
            await self.ensure_ready(row["agent_id"])
            if row["kind"] == "file" and row["data"] != "null":
                workspace = await self.workspace(row["agent_id"])
                await durable(
                    workspace.remove_sync_file, FileData.model_validate_json(row["data"]).path
                )
            if row["kind"] == "memory" and row["data"] != "null":
                from octop_memory.operations.sync import MemoryChange

                data = MemoryData.model_validate_json(row["data"])
                memory = await self._memory(row["agent_id"])
                try:
                    await durable(
                        memory.restore_sync_changes,
                        [MemoryChange(0, data.table, data.key, row["revision"] + 1, None)],
                    )
                finally:
                    await durable(memory.close)
            if row["kind"] == "thread":
                changes = []
                for message in await durable(self.repo.list, row["agent_id"], "message"):
                    if (
                        message["data"] != "null"
                        and json.loads(message["data"])["thread_id"] == item_id
                    ):
                        changes.append(
                            (
                                message["item_id"],
                                message["agent_id"],
                                message["resource_id"],
                                "message",
                                "null",
                                digest(None),
                            )
                        )
                changes.append(
                    (item_id, row["agent_id"], row["resource_id"], "thread", "null", digest(None))
                )
                await durable(self.repo.batch, changes)
            else:
                await self._save(item_id, row["agent_id"], None, row["kind"])
            self.changed()

    async def attachments(self, agent_id: str, ids: list[str]) -> list[Attachment]:
        result = []
        for item_id in ids:
            row = await durable(self.repo.get, item_id)
            if (
                not row
                or row["agent_id"] != agent_id
                or row["kind"] != "file"
                or row["data"] == "null"
            ):
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            data = FileData.model_validate_json(row["data"])
            result.append(Attachment(file_id=item_id, sha256=data.sha256))
        return result

    async def upload(self, agent_id: str, path: str, chunks: Any) -> FileData:
        try:
            sync_path(path)
        except ValueError as exc:
            raise OctopError.localized(ErrorCode.SYNC_INVALID) from exc
        async with self._operation():
            await self.ensure_ready(agent_id)
            if self.runtime.agent_busy(agent_id):
                raise OctopError.localized(ErrorCode.AGENT_BUSY)
            workspace = await self.workspace(agent_id)
            staged = ".octop/sync-staging/" + new_ulid()
            size = 0
            try:
                async for incoming in chunks:
                    for offset in range(0, len(incoming), CHUNK_BYTES):
                        chunk = incoming[offset : offset + CHUNK_BYTES]
                        if size + len(chunk) > FILE_BYTES:
                            raise OctopError.localized(ErrorCode.ATTACHMENT_TOO_LARGE)
                        await durable(workspace.write_sync_chunk, staged, size, chunk)
                        size += len(chunk)
                if size == 0:
                    await durable(workspace.write_sync_chunk, staged, 0, b"")
                await durable(workspace.commit_sync_file, staged, path)
                await self._scan_files(agent_id)
            finally:
                await durable(workspace.remove_sync_file, staged)
            rows = await durable(self.repo.list, agent_id, "file")
            self.changed()
            return next(
                FileData.model_validate_json(row["data"])
                for row in reversed(rows)
                if row["data"] != "null" and json.loads(row["data"])["path"] == path
            )

    async def rehydrate(self, agent_id: str, thread_id: str) -> None:
        if not await durable(self.repo.restored_thread, thread_id):
            return
        from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

        rows = await self.records(agent_id=agent_id, kind="message")
        messages = sorted(
            (r for r in rows if r["data"] and r["data"]["thread_id"] == thread_id),
            key=lambda r: r["data"]["position"],
        )
        # Keep unmatched/unfinished tool groups viewable in the archive, but
        # never replay their requests or synthesize tool results into the graph.
        tool_results = {
            (r["data"]["turn_id"], r["data"]["tool_call_id"])
            for r in messages
            if r["data"]["role"] == "tool"
        }
        accepted_calls: set[tuple[str, str]] = set()
        accepted: list[BaseMessage] = []
        for row in messages:
            data, item_id = row["data"], row["item_id"]
            if data["role"] == "user":
                accepted.append(HumanMessage(id=item_id, content=data["content"]))
            elif data["role"] == "assistant":
                calls = data["tool_calls"]
                if calls and any(
                    (data["turn_id"], call["id"]) not in tool_results for call in calls
                ):
                    if data["content"]:
                        accepted.append(AIMessage(id=item_id, content=data["content"]))
                    continue
                accepted_calls.update((data["turn_id"], call["id"]) for call in calls)
                accepted.append(AIMessage(id=item_id, content=data["content"], tool_calls=calls))
            elif (data["turn_id"], data["tool_call_id"]) in accepted_calls:
                accepted.append(
                    ToolMessage(
                        id=item_id, content=data["content"], tool_call_id=data["tool_call_id"]
                    )
                )
        agent = await self.runtime._agent(agent_id)
        await agent.aappend_messages(thread_id, accepted)
        await durable(self.repo.thread_rehydrated, thread_id)

    async def restore(self) -> RestoreResult:
        from octop.infra.sync.restore import restore

        async with self._operation(), self.resources._operation():
            self.restoring = True
            try:
                return await restore(self)
            finally:
                self.restoring = False
