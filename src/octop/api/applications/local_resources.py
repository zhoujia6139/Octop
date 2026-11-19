"""Account-bound desktop draft, copy, installation and sync HTTP adapters."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from octop.infra.applications.contracts import PublicId
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.resources.contracts import CopyInput, LocalResourceView, Mutation
from octop.infra.resources.local import LocalResources

router = APIRouter(prefix="/api/resources", tags=["resources"])


def resources(request: Request) -> LocalResources:
    service = request.app.state.accounts.require_runtime().resources
    if not isinstance(service, LocalResources):
        raise OctopError.localized(ErrorCode.AUTH_FAILED)
    return service


Resources = Annotated[LocalResources, Depends(resources)]


@router.get(
    "",
    response_model=list[LocalResourceView],
    summary="List local drafts, installed versions and synchronization state",
)
async def list_resources(service: Resources) -> list[LocalResourceView]:
    return await service.list_resources()


@router.put(
    "/personal/{resource_id}",
    response_model=LocalResourceView,
    summary="Save an owned personal draft and its durable upload operation",
    description="Works offline in an already selected account. Platform resources must first be copied. Expected revision refers to the local draft; cloud conflicts preserve it.",
)
async def edit(resource_id: PublicId, body: Mutation, service: Resources) -> LocalResourceView:
    return await service.edit(resource_id, body)


@router.post(
    "/{resource_id}/copy",
    response_model=LocalResourceView,
    status_code=201,
    summary="Copy an installed platform version and its pinned skills into personal resources",
)
async def copy(resource_id: PublicId, body: CopyInput, service: Resources) -> LocalResourceView:
    return await service.copy(resource_id, body)


@router.post(
    "/sync",
    response_model=list[LocalResourceView],
    summary="Retry resource uploads, pull assignments, install and report actual versions",
    description="restore_personal=true explicitly downloads personal resources on a replacement installation. Normal pulls never replace unsynchronized local edits.",
)
async def sync(
    service: Resources, restore_personal: bool = Query(default=False)
) -> list[LocalResourceView]:
    return await service.sync(restore_personal=restore_personal)
