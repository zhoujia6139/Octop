"""Thin resource publication and current-device delivery HTTP adapters."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from octop.api.applications.identity import Token
from octop.infra.applications.contracts import PublicId
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.models.gateway import offload
from octop.infra.resources.contracts import (
    AssignmentInput,
    ChangePage,
    DeviceReport,
    InstallReport,
    Mutation,
    ResourceView,
    RevisionInput,
    VersionView,
)
from octop.infra.resources.service import CloudResources

router = APIRouter(prefix="/api", tags=["resources"])


def resources(request: Request) -> CloudResources:
    service = request.app.state.cloud.resources
    if not isinstance(service, CloudResources):
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED, status=503)
    return service


Resources = Annotated[CloudResources, Depends(resources)]


@router.get(
    "/resource-admin/resources",
    response_model=list[ResourceView],
    summary="List resource definitions as an administrator",
)
async def definitions(service: Resources, token: Token) -> list[ResourceView]:
    return await offload(service.definitions, token)


@router.put(
    "/resource-admin/resources/{resource_id}",
    response_model=ResourceView,
    summary="Save a platform draft or a deletion tombstone",
)
async def save_platform(
    resource_id: PublicId, body: Mutation, service: Resources, token: Token
) -> ResourceView:
    return await offload(service.mutate, token, resource_id, body, platform=True)


@router.post(
    "/resource-admin/resources/{resource_id}/publish",
    response_model=ResourceView,
    summary="Publish an immutable version with pinned skill dependencies",
)
async def publish(
    resource_id: PublicId, body: RevisionInput, service: Resources, token: Token
) -> ResourceView:
    return await offload(service.publish, token, resource_id, body.revision)


@router.get(
    "/resource-admin/resources/{resource_id}/versions/{revision}",
    response_model=VersionView,
    summary="Inspect a resource draft or historical version as an administrator",
)
async def admin_version(
    resource_id: PublicId, revision: int, service: Resources, token: Token
) -> VersionView:
    return await offload(service.version, token, resource_id, revision, admin=True)


@router.put(
    "/resource-admin/users/{user_id}/assignments/{resource_id}",
    response_model=ResourceView,
    summary="Assign a published version to the current device or withdraw it",
    description="Targets the authoritative current device and binding generation. Rebinding does not inherit a prior device assignment.",
)
async def assign(
    user_id: int, resource_id: PublicId, body: AssignmentInput, service: Resources, token: Token
) -> ResourceView:
    return await offload(service.assign, token, user_id, resource_id, body.revision)


@router.get(
    "/resource-admin/users/{user_id}/reports",
    response_model=list[DeviceReport],
    summary="Inspect reported desired and installed versions separately",
)
async def reports(user_id: int, service: Resources, token: Token) -> list[DeviceReport]:
    return await offload(service.reports, token, user_id)


@router.put(
    "/resources/personal/{resource_id}",
    response_model=ResourceView,
    summary="Synchronize an owned personal resource revision",
    description="Current device only. Stable operation IDs make response-loss retries idempotent. Revision conflicts preserve the device draft. Source provenance is checked only at creation; editing never modifies the platform source.",
)
async def personal(
    resource_id: PublicId, body: Mutation, service: Resources, token: Token
) -> ResourceView:
    return await offload(service.mutate, token, resource_id, body)


@router.get(
    "/resources/changes",
    response_model=ChangePage,
    summary="Pull desired resource revisions using a durable cursor",
)
async def changes(
    service: Resources,
    token: Token,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> ChangePage:
    return await offload(service.changes, token, cursor, limit)


@router.get(
    "/resources/{resource_id}/versions/{revision}",
    response_model=VersionView,
    summary="Download an owned or currently assigned version and its verified manifest",
)
async def version(
    resource_id: PublicId, revision: int, service: Resources, token: Token
) -> VersionView:
    return await offload(service.version, token, resource_id, revision)


@router.put(
    "/resources/reports",
    status_code=204,
    summary="Report actual installation without changing cloud authority",
)
async def report(body: InstallReport, service: Resources, token: Token) -> Response:
    await offload(service.report, token, body)
    return Response(status_code=204)
