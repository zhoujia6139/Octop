"""Cloud backup authorization and durable acknowledgement, without execution libraries."""

from __future__ import annotations

import hashlib
import json

from octop.infra.db.repos.cloud_resources import ResourceTransaction
from octop.infra.db.repos.cloud_sync import SyncTransaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.service import CloudIdentity
from octop.infra.sync.contracts import (
    CHUNK_BYTES,
    METADATA_BYTES,
    STORAGE_BYTES,
    BlobInput,
    BlobState,
    FileData,
    MessageData,
    Mutation,
    Page,
    Record,
    digest,
)


def fail(code: ErrorCode = ErrorCode.SYNC_INVALID) -> OctopError:
    return OctopError.localized(code)


class CloudSync:
    def __init__(self, identity: CloudIdentity) -> None:
        self.identity = identity

    def mutate(self, token: str, body: Mutation) -> Record:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            identity_tx.setting("resource_management_lock", "1")
            tx = SyncTransaction(identity_tx.conn)
            user_id, device_id, generation = (
                principal.user.user_id,
                principal.session.device_id or "",
                principal.session.generation or 0,
            )
            request_hash = digest(body)
            receipt = tx.operation(user_id, device_id, generation, body.operation_id)
            if receipt:
                if receipt["request_hash"] != request_hash:
                    raise fail(ErrorCode.SYNC_CONFLICT)
                return Record.model_validate_json(receipt["result"])
            agent = tx.agent(body.agent_id)
            if agent and (agent["user_id"] != user_id or agent["resource_id"] != body.resource_id):
                raise fail(ErrorCode.NOT_FOUND)
            if not agent:
                resources = ResourceTransaction(identity_tx.conn)
                resource = resources.resource(body.resource_id)
                if (
                    resource
                    and resource["scope"] == "platform"
                    and body.agent_id == body.resource_id
                ):
                    raise fail(ErrorCode.RESOURCE_NOT_ALLOWED)
                if (
                    not resource
                    or resource["kind"] != "agent"
                    or not resources.agent_allowed(body.resource_id, user_id, device_id, generation)
                ):
                    raise fail(ErrorCode.RESOURCE_NOT_ALLOWED)
                execution = identity_tx.conn.execute(
                    "SELECT user_id,resource_id FROM cloud_model_agents WHERE agent_id=?",
                    (body.agent_id,),
                ).fetchone()
                if execution and (
                    execution["user_id"] != user_id or execution["resource_id"] != body.resource_id
                ):
                    raise fail(ErrorCode.RESOURCE_NOT_ALLOWED)
                tx.bind_agent(body.agent_id, user_id, body.resource_id)
            current = tx.item(body.item_id)
            if current and (
                current["user_id"] != user_id
                or current["agent_id"] != body.agent_id
                or current["kind"] != body.kind
            ):
                raise fail(ErrorCode.NOT_FOUND)
            if (current["revision"] if current else 0) != body.expected_revision:
                raise fail(ErrorCode.SYNC_CONFLICT)
            if current:
                previous = tx.version(body.item_id, current["revision"])
                if previous and previous["data"] == "null":
                    raise fail(ErrorCode.SYNC_CONFLICT)
            if isinstance(body.data, MessageData):
                thread = tx.item(body.data.thread_id)
                if not thread or thread["kind"] != "thread" or thread["agent_id"] != body.agent_id:
                    raise fail()
                version = tx.version(thread["item_id"], thread["revision"])
                if not version or version["data"] == "null":
                    raise fail()
                for attachment in body.data.attachments:
                    file = tx.item(attachment.file_id)
                    if (
                        not file
                        or file["kind"] != "file"
                        or file["agent_id"] != body.agent_id
                        or not any(
                            json.loads(version).get("sha256") == attachment.sha256
                            for version in tx.file_versions(attachment.file_id)
                        )
                    ):
                        raise fail()
            if isinstance(body.data, FileData):
                blob = tx.blob(user_id, body.data.sha256)
                if not blob or not blob["complete"] or blob["size_bytes"] != body.data.size:
                    raise fail()
            data = body.data.model_dump_json() if body.data else "null"
            size_bytes = len(body.model_dump_json().encode())
            if (body.data is not None or current is None) and tx.metadata_storage(
                user_id
            ) + size_bytes > METADATA_BYTES:
                raise fail(ErrorCode.SYNC_QUOTA_EXCEEDED)
            revision = body.expected_revision + 1
            checksum = digest(body.data)
            cursor = tx.append(
                body.item_id,
                user_id,
                body.agent_id,
                body.kind,
                revision,
                data,
                checksum,
                size_bytes,
            )
            result = Record(
                item_id=body.item_id,
                agent_id=body.agent_id,
                resource_id=body.resource_id,
                kind=body.kind,
                revision=revision,
                cursor=cursor,
                digest=checksum,
                data=body.data,
            )
            tx.receipt(
                user_id,
                device_id,
                generation,
                body.operation_id,
                request_hash,
                result.model_dump_json(),
            )
            return result

    def changes(self, token: str, cursor: int, limit: int, watermark: int | None) -> Page:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx = SyncTransaction(identity_tx.conn)
            latest = tx.watermark(principal.user.user_id)
            boundary = latest if watermark is None else watermark
            if boundary > latest or cursor > boundary:
                raise fail()
            rows = tx.changes(principal.user.user_id, cursor, boundary, limit + 1)
            records = [
                Record.model_validate({**row, "data": json.loads(row["data"])})
                for row in rows[:limit]
            ]
            return Page(
                watermark=boundary,
                cursor=records[-1].cursor if records else cursor,
                has_more=len(rows) > limit,
                records=records,
            )

    def prepare_blob(self, token: str, body: BlobInput) -> BlobState:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            identity_tx.setting("sync_storage_lock", "1")
            tx, user_id = SyncTransaction(identity_tx.conn), principal.user.user_id
            blob = tx.blob(user_id, body.sha256)
            if not blob:
                if tx.storage(user_id) + body.size > STORAGE_BYTES:
                    raise fail(ErrorCode.SYNC_QUOTA_EXCEEDED)
                tx.create_blob(user_id, body.sha256, body.size)
                blob = tx.blob(user_id, body.sha256)
            if not blob or blob["size_bytes"] != body.size:
                raise fail()
            return BlobState(
                **body.model_dump(),
                offsets=tx.offsets(user_id, body.sha256),
                complete=bool(blob["complete"]),
            )

    def put_chunk(self, token: str, checksum: str, offset: int, content: bytes) -> None:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            identity_tx.setting("sync_storage_lock", "1")
            tx, user_id = SyncTransaction(identity_tx.conn), principal.user.user_id
            blob = tx.blob(user_id, checksum)
            if not blob:
                raise fail(ErrorCode.NOT_FOUND)
            size = blob["size_bytes"]
            if (
                offset < 0
                or offset % CHUNK_BYTES
                or offset >= size
                or len(content) != min(CHUNK_BYTES, size - offset)
            ):
                raise fail()
            existing = tx.chunk(user_id, checksum, offset)
            if existing is not None:
                if existing != content:
                    raise fail(ErrorCode.SYNC_CONFLICT)
                return
            if blob["complete"]:
                raise fail(ErrorCode.SYNC_CONFLICT)
            tx.save_chunk(user_id, checksum, offset, content)

    def complete_blob(self, token: str, checksum: str) -> BlobState:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            identity_tx.setting("sync_storage_lock", "1")
            tx, user_id = SyncTransaction(identity_tx.conn), principal.user.user_id
            blob = tx.blob(user_id, checksum)
            if not blob:
                raise fail(ErrorCode.NOT_FOUND)
            hasher = hashlib.sha256()
            for offset in range(0, blob["size_bytes"], CHUNK_BYTES):
                content = tx.chunk(user_id, checksum, offset)
                if content is None:
                    raise fail()
                hasher.update(content)
            if hasher.hexdigest() != checksum:
                raise fail()
            tx.complete(user_id, checksum)
            return BlobState(
                sha256=checksum,
                size=blob["size_bytes"],
                offsets=tx.offsets(user_id, checksum),
                complete=True,
            )

    def get_chunk(self, token: str, checksum: str, offset: int) -> bytes:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx, user_id = SyncTransaction(identity_tx.conn), principal.user.user_id
            blob = tx.blob(user_id, checksum)
            if not blob or not blob["complete"]:
                raise fail(ErrorCode.NOT_FOUND)
            chunk = tx.chunk(user_id, checksum, offset)
            if chunk is None:
                raise fail(ErrorCode.NOT_FOUND)
            return chunk
