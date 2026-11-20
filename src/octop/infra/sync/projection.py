"""Persist the local transcript before exposing its corresponding stream event.

Only canonical content, tool calls and tool-result associations enter backups.
Provider response metadata, graph state, credentials and checkpoints do not.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal

from octop.infra.db.repos.local_sync import LocalSyncRepo
from octop.infra.resources.local import durable
from octop.infra.sync.contracts import Attachment, MessageData, Status, ThreadData, digest


def stable_id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:40]


def change(
    item_id: str, agent_id: str, resource_id: str, data: ThreadData | MessageData | None, kind: str
) -> tuple[str, str, str, str, str, str]:
    return (
        item_id,
        agent_id,
        resource_id,
        kind,
        data.model_dump_json() if data else "null",
        digest(data),
    )


class Projection:
    def __init__(
        self, repo: LocalSyncRepo, agent_id: str, resource_id: str, thread_id: str, turn_id: str
    ) -> None:
        self.repo, self.agent_id, self.resource_id = repo, agent_id, resource_id
        self.thread_id, self.turn_id = thread_id, turn_id
        self.messages: dict[str, MessageData] = {}
        self.position = 0
        self.current: str | None = None
        self.earlier_sources: set[str] = set()
        self.thread = ThreadData()

    async def load(self) -> None:
        thread = await durable(self.repo.get, self.thread_id)
        if thread and thread["data"] != "null":
            self.thread = ThreadData.model_validate_json(thread["data"])
        for row in await durable(self.repo.list, self.agent_id, "message"):
            if row["data"] == "null":
                continue
            data = MessageData.model_validate_json(row["data"])
            if data.thread_id == self.thread_id:
                self.position = max(self.position, data.position + 1)
                if data.turn_id == self.turn_id:
                    self.messages[row["item_id"]] = data
                elif data.source_id:
                    self.earlier_sources.add(data.source_id)

    def _new(
        self, role: Literal["user", "assistant", "tool"], *, source_id: str | None = None
    ) -> tuple[str, MessageData]:
        item_id = stable_id(self.turn_id, source_id or f"segment-{self.position}")
        data = MessageData(
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            position=self.position,
            created_at=int(time.time()),
            role=role,
            source_id=source_id,
            content="",
            status="running",
        )
        self.position += 1
        self.messages[item_id] = data
        return item_id, data

    def _change(self, item_id: str) -> tuple[str, str, str, str, str, str]:
        return change(item_id, self.agent_id, self.resource_id, self.messages[item_id], "message")

    async def begin(self, text: str, attachments: list[Attachment], *, resume: bool) -> str:
        await self.load()
        self.thread.status = "running"
        if not self.thread.title and text.strip():
            self.thread.title = text.strip().splitlines()[0][:200]
        self.thread.updated_at = int(time.time())
        changes = [
            change(
                self.thread_id,
                self.agent_id,
                self.resource_id,
                self.thread,
                "thread",
            )
        ]
        item_id = ""
        if not resume:
            item_id, data = self._new("user", source_id=self.turn_id + "-user")
            data.content, data.attachments = text, attachments
            changes.append(self._change(item_id))
        await durable(
            self.repo.batch, changes, turn=(self.turn_id, self.thread_id, "running", not resume)
        )
        return item_id

    async def capture(self, chunk: dict[str, Any]) -> None:
        kind = chunk.get("type")
        changed: set[str] = set()
        if kind == "token":
            if self.current is None:
                self.current, _ = self._new("assistant")
            data = self.messages[self.current]
            data.content = str(data.content) + str(chunk.get("content") or "")
            changed.add(self.current)
        raw_messages = (
            chunk.get("messages", [])
            if kind == "tool_result"
            else chunk.get("data", {}).get("messages", [])
            if kind in {"state_update", "state_snapshot"} and isinstance(chunk.get("data"), dict)
            else []
        )
        if not isinstance(raw_messages, list):
            raw_messages = [raw_messages]
        for raw in raw_messages:
            wire = (
                raw
                if isinstance(raw, dict)
                else raw.model_dump()
                if hasattr(raw, "model_dump")
                else {}
            )
            role = {"ai": "assistant", "human": "user", "tool": "tool"}.get(
                str(wire.get("type") or ""), wire.get("role")
            )
            if role not in {"assistant", "tool"}:
                continue
            source_id = str(wire.get("id") or wire.get("tool_call_id") or "") or None
            if source_id in self.earlier_sources:
                continue
            item_id = next(
                (
                    key
                    for key, value in self.messages.items()
                    if source_id and value.source_id == source_id
                ),
                None,
            )
            if item_id is None and role == "assistant" and self.current:
                item_id = self.current
            if item_id is None:
                item_id, _ = self._new(
                    "assistant" if role == "assistant" else "tool", source_id=source_id
                )
            data = self.messages[item_id]
            data.source_id = source_id
            data.content = wire.get("content", "")
            data.tool_calls = [
                {
                    "id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "args": call.get("args", {}),
                }
                for call in wire.get("tool_calls", [])
            ]
            data.tool_call_id = wire.get("tool_call_id")
            changed.add(item_id)
            if role == "assistant":
                self.current = None
        if changed:
            await durable(
                self.repo.batch, [self._change(key) for key in self.messages if key in changed]
            )

    async def finish(self, status: Status) -> None:
        self.thread.status = status
        self.thread.updated_at = int(time.time())
        for data in self.messages.values():
            data.status = status
        changes = [self._change(key) for key in self.messages]
        changes.append(
            change(self.thread_id, self.agent_id, self.resource_id, self.thread, "thread")
        )
        await durable(self.repo.batch, changes, turn=(self.turn_id, self.thread_id, status, False))
