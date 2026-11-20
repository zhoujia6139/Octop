"""Thin device-authorized backup and bounded binary transfer adapters."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from octop.api.applications.identity import Token
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.models.gateway import offload
from octop.infra.sync.contracts import (
    CHUNK_BYTES,
    BlobInput,
    BlobState,
    Digest,
    Mutation,
    Page,
    Record,
)
from octop.infra.sync.service import CloudSync

router = APIRouter(prefix="/api/sync", tags=["sync"])


def sync_service(request: Request) -> CloudSync:
    service = request.app.state.cloud.sync
    if not isinstance(service, CloudSync):
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED, status=503)
    return service


Service = Annotated[CloudSync, Depends(sync_service)]


@router.put(
    "/records",
    response_model=Record,
    summary="Commit one revision and its idempotency receipt atomically",
)
async def mutate(body: Mutation, service: Service, token: Token) -> Record:
    return await offload(service.mutate, token, body)


@router.get(
    "/changes",
    response_model=Page,
    summary="Read this user's backup revisions at a fixed restore watermark",
    description="Device authorization is revalidated on every page. Supplying the first response's watermark gives a consistent bounded restore; ordinary startup does not apply these records.",
)
async def changes(
    service: Service,
    token: Token,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    watermark: int | None = Query(default=None, ge=0),
) -> Page:
    return await offload(service.changes, token, cursor, limit, watermark)


@router.post(
    "/blobs",
    response_model=BlobState,
    summary="Reserve user-scoped content and list durably received chunk offsets",
)
async def prepare(body: BlobInput, service: Service, token: Token) -> BlobState:
    return await offload(service.prepare_blob, token, body)


@router.put(
    "/blobs/{checksum}/chunks/{offset}",
    status_code=204,
    summary="Upload or retry one aligned binary chunk of at most 1 MiB",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def put_chunk(
    checksum: Digest, offset: int, request: Request, service: Service, token: Token
) -> Response:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > CHUNK_BYTES:
            raise OctopError.localized(ErrorCode.ATTACHMENT_TOO_LARGE, status=413)
        chunks.append(chunk)
    await offload(service.put_chunk, token, checksum, offset, b"".join(chunks))
    return Response(status_code=204)


@router.post(
    "/blobs/{checksum}/complete",
    response_model=BlobState,
    summary="Verify complete ordered content before allowing file references",
)
async def complete(checksum: Digest, service: Service, token: Token) -> BlobState:
    return await offload(service.complete_blob, token, checksum)


@router.get(
    "/blobs/{checksum}/chunks/{offset}",
    response_class=Response,
    summary="Download one verified content chunk belonging to this user",
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def get_chunk(checksum: Digest, offset: int, service: Service, token: Token) -> Response:
    content = await offload(service.get_chunk, token, checksum, offset)
    return Response(content=content, media_type="application/octet-stream")
