"""Loopback backup status, archived transcript, attachment and explicit restore APIs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from octop.infra.applications.contracts import PublicId
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.sync.contracts import (
    FileData,
    Kind,
    LocalRecord,
    RestoreResult,
    SyncState,
)
from octop.infra.sync.local import LocalSync

router = APIRouter(tags=["sync"])


def sync_service(request: Request) -> LocalSync:
    service = request.app.state.accounts.require_runtime().sync
    if not isinstance(service, LocalSync):
        raise OctopError.localized(ErrorCode.AUTH_FAILED)
    return service


Service = Annotated[LocalSync, Depends(sync_service)]


@router.get(
    "/api/sync/status",
    response_model=SyncState,
    summary="Inspect pending backups, conflicts and the most recent complete file scan",
)
async def status(service: Service) -> SyncState:
    return await service.status()


@router.get(
    "/api/sync/records",
    response_model=list[LocalRecord],
    summary="Read local archived messages, file references and logical memory without a model",
)
async def records(
    service: Service, agent_id: PublicId | None = None, kind: Kind | None = None
) -> list[LocalRecord]:
    return [
        LocalRecord.model_validate(row, extra="ignore")
        for row in await service.records(agent_id=agent_id, kind=kind)
    ]


@router.post(
    "/api/sync/retry",
    response_model=SyncState,
    summary="Scan supported local content and retry durable data uploads",
)
async def retry(service: Service) -> SyncState:
    return await service.sync()


@router.post(
    "/api/sync/restore",
    response_model=RestoreResult,
    summary="Explicitly restore verified backups without restoring or replaying tools",
    description="Active turns prevent restoration. Agents with local unsynchronized data are skipped. Missing or withdrawn definitions restore as disabled archives. Execution requires installing a currently authorized definition. Checkpoints, credentials and device state are never restored.",
)
async def restore(service: Service) -> RestoreResult:
    return await service.restore()


@router.delete(
    "/api/sync/records/{item_id}",
    status_code=204,
    summary="Delete local synchronized content and queue its permanent tombstone",
)
async def delete(item_id: PublicId, service: Service) -> Response:
    await service.delete(item_id)
    return Response(status_code=204)


@router.put(
    "/api/agents/{agent_id}/files/{path:path}",
    response_model=LocalRecord,
    summary="Copy an attachment or product into the supported workspace export roots",
    description="Streams at most 256 MiB through a no-follow workspace staging file. Only inbound/ and artifacts/ paths are accepted; a partial upload never replaces an existing file.",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload(agent_id: PublicId, path: str, request: Request, service: Service) -> LocalRecord:
    data = await service.upload(agent_id, path, request.stream())
    rows = await service.records(agent_id=agent_id, kind="file")
    row = next(row for row in reversed(rows) if row["data"] == data.model_dump())
    return LocalRecord.model_validate(row, extra="ignore")


@router.get(
    "/api/sync/records/{item_id}/content",
    response_class=Response,
    summary="Read one bounded chunk of an archived local file",
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def file_content(
    item_id: PublicId, service: Service, offset: int = Query(default=0, ge=0)
) -> Response:
    from octop.infra.resources.local import durable

    row = await durable(service.repo.get, item_id)
    if not row or row["kind"] != "file" or row["data"] == "null":
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    data = FileData.model_validate_json(row["data"])
    content = await durable(service.repo.chunk, data.sha256, offset)
    if content is None and data.size:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return Response(content=content or b"", media_type="application/octet-stream")


@router.get(
    "/api/sync/messages/{message_id}/attachments/{file_id}",
    response_class=Response,
    summary="Read the immutable attachment version referenced by an archived message",
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def attachment_content(
    message_id: PublicId, file_id: PublicId, service: Service, offset: int = Query(default=0, ge=0)
) -> Response:
    from octop.infra.resources.local import durable
    from octop.infra.sync.contracts import MessageData

    row = await durable(service.repo.get, message_id)
    if not row or row["kind"] != "message" or row["data"] == "null":
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    data = MessageData.model_validate_json(row["data"])
    attachment = next((a for a in data.attachments if a.file_id == file_id), None)
    if attachment is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    content = await durable(service.repo.chunk, attachment.sha256, offset)
    size = await durable(service.repo.blob_size, attachment.sha256)
    if content is None and (
        size
        or attachment.sha256 != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return Response(
        content=content or b"",
        media_type="application/octet-stream",
        headers={"X-Octop-File-Size": str(size)},
    )
