"""Cloud resource authorization and transactional version publication.

No filesystem, Harness, workspace, or Agent lifecycle is needed here.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from octop.infra.db.repos.cloud_resources import ResourceTransaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.service import CloudIdentity, Principal
from octop.infra.resources.contracts import (
    PERSONAL_RESOURCE_LIMIT,
    PERSONAL_STORAGE_BYTES,
    AgentSpec,
    Change,
    ChangePage,
    DeviceReport,
    InstallReport,
    Mutation,
    ResourceView,
    VersionView,
    digest_spec,
)
from octop.infra.utils.ulid import new_ulid


def fail(code: ErrorCode = ErrorCode.RESOURCE_NOT_ALLOWED) -> OctopError:
    return OctopError.localized(code)


def view(row: dict[str, Any]) -> ResourceView:
    return ResourceView.model_validate(row, extra="ignore")


class CloudResources:
    def __init__(self, identity: CloudIdentity) -> None:
        self.identity = identity

    @staticmethod
    def _readable(
        tx: ResourceTransaction, principal: Principal, resource_id: str, revision: int
    ) -> None:
        if not tx.can_read(
            resource_id,
            revision,
            principal.user.user_id,
            principal.session.device_id,
            principal.session.generation,
        ):
            raise fail(ErrorCode.NOT_FOUND)

    def definitions(self, token: str) -> list[ResourceView]:
        with self.identity.authorized_account(token, admin=True) as (identity_tx, _):
            return [view(r) for r in ResourceTransaction(identity_tx.conn).resources()]

    def mutate(
        self, token: str, resource_id: str, body: Mutation, *, platform: bool = False
    ) -> ResourceView:
        authorize = (
            self.identity.authorized_account(token, admin=True)
            if platform
            else self.identity.authorized_device(token)
        )
        with authorize as (identity_tx, principal):
            # Resource writers and model authority checks share this short lock
            # after identity/user locks, so withdrawal serializes with dispatch.
            identity_tx.setting("resource_management_lock", "1")
            tx = ResourceTransaction(identity_tx.conn)
            user_id = principal.user.user_id
            request_hash = hashlib.sha256(
                json.dumps([resource_id, platform, body.model_dump()], sort_keys=True).encode()
            ).hexdigest()
            previous = tx.operation(user_id, body.operation_id)
            if previous:
                if previous["request_hash"] != request_hash:
                    raise fail(ErrorCode.RESOURCE_CONFLICT)
                return ResourceView.model_validate_json(previous["result"])
            row = tx.resource(resource_id)
            scope = "platform" if platform else "personal"
            if row and (row["scope"] != scope or (not platform and row["owner_id"] != user_id)):
                raise fail(ErrorCode.NOT_FOUND)
            if (row["revision"] if row else 0) != body.expected_revision or (
                row and row["deleted"]
            ):
                raise fail(ErrorCode.RESOURCE_CONFLICT)
            if row is None and body.spec is None:
                raise fail(ErrorCode.NOT_FOUND)
            if row and body.spec and row["kind"] != body.spec.kind:
                raise fail(ErrorCode.RESOURCE_INVALID)
            if body.source:
                if row or platform:
                    raise fail(ErrorCode.RESOURCE_INVALID)
                source = tx.resource(body.source.resource_id)
                self._readable(tx, principal, body.source.resource_id, body.source.revision)
                if (
                    not source
                    or source["scope"] != "platform"
                    or not body.spec
                    or source["kind"] != body.spec.kind
                ):
                    raise fail(ErrorCode.RESOURCE_INVALID)
            if isinstance(body.spec, AgentSpec):
                slugs: set[str] = set()
                for ref in body.spec.skills:
                    dependency = tx.resource(ref.resource_id)
                    version = tx.version(ref.resource_id, ref.revision)
                    if (
                        not dependency
                        or dependency["kind"] != "skill"
                        or not version
                        or version["spec"] == "null"
                    ):
                        raise fail(ErrorCode.RESOURCE_INVALID)
                    if platform:
                        if dependency["scope"] != "platform" or not version["published"]:
                            raise fail(ErrorCode.RESOURCE_INVALID)
                    else:
                        self._readable(tx, principal, ref.resource_id, ref.revision)
                    slug = json.loads(version["spec"])["slug"]
                    if slug in slugs:
                        raise fail(ErrorCode.RESOURCE_INVALID)
                    slugs.add(slug)
            serialized = body.spec.model_dump_json() if body.spec else "null"
            if not platform:
                count, size = tx.usage(user_id)
                if (row is None and count >= PERSONAL_RESOURCE_LIMIT) or (
                    body.spec is not None
                    and size + len(serialized.encode()) > PERSONAL_STORAGE_BYTES
                ):
                    raise fail(ErrorCode.RESOURCE_QUOTA_EXCEEDED)
            if row is None:
                assert body.spec is not None
                tx.create(
                    resource_id,
                    scope,
                    body.spec.kind,
                    None if platform else user_id,
                    body.source.resource_id if body.source else None,
                    body.source.revision if body.source else None,
                )
            revision = body.expected_revision + 1
            tx.append(
                resource_id, revision, new_ulid(), serialized, digest_spec(body.spec), not platform
            )
            if isinstance(body.spec, AgentSpec):
                for ref in body.spec.skills:
                    tx.dependency(resource_id, revision, ref.resource_id, ref.revision)
            if not platform:
                tx.change(user_id, resource_id, revision if body.spec else None)
            # Deleting a platform definition also withdraws every assignment.
            if platform and body.spec is None:
                for assignment in tx.assigned(resource_id):
                    tx.assign(
                        new_ulid(),
                        resource_id,
                        assignment["user_id"],
                        assignment["device_id"],
                        assignment["generation"],
                        None,
                    )
                    tx.change(
                        assignment["user_id"],
                        resource_id,
                        None,
                        assignment["device_id"],
                        assignment["generation"],
                    )
            result = view(tx.resource(resource_id) or {})
            tx.save_operation(user_id, body.operation_id, request_hash, result.model_dump_json())
            return result

    def publish(self, token: str, resource_id: str, revision: int) -> ResourceView:
        with self.identity.authorized_account(token, admin=True) as (identity_tx, _):
            identity_tx.setting("resource_management_lock", "1")
            tx = ResourceTransaction(identity_tx.conn)
            row, version = tx.resource(resource_id), tx.version(resource_id, revision)
            if (
                not row
                or row["scope"] != "platform"
                or row["deleted"]
                or not version
                or version["spec"] == "null"
            ):
                raise fail(ErrorCode.NOT_FOUND)
            # Publication never mutates the version's content or pinned skills.
            tx.publish(resource_id, revision)
            return view(tx.resource(resource_id) or {})

    def assign(
        self, token: str, user_id: int, resource_id: str, revision: int | None
    ) -> ResourceView:
        with self.identity.authorized_account(token, admin=True) as (identity_tx, _):
            user = identity_tx.user(user_id=user_id)
            device = identity_tx.device(user_id)
            identity_tx.setting("resource_management_lock", "1")
            tx = ResourceTransaction(identity_tx.conn)
            row = tx.resource(resource_id)
            if (
                not user
                or not user.enabled
                or not device
                or device.revoked
                or device.generation != user.binding_generation
                or not row
                or row["scope"] != "platform"
                or row["deleted"]
            ):
                raise fail(ErrorCode.NOT_FOUND)
            if revision is not None:
                version = tx.version(resource_id, revision)
                if not version or not version["published"] or version["spec"] == "null":
                    raise fail(ErrorCode.RESOURCE_INVALID)
            if tx.assignment(resource_id, device.device_id, device.generation) != revision:
                tx.assign(
                    new_ulid(), resource_id, user_id, device.device_id, device.generation, revision
                )
                tx.change(user_id, resource_id, revision, device.device_id, device.generation)
            return view(row)

    def version(
        self, token: str, resource_id: str, revision: int, *, admin: bool = False
    ) -> VersionView:
        authorize = (
            self.identity.authorized_account(token, admin=True)
            if admin
            else self.identity.authorized_device(token)
        )
        with authorize as (identity_tx, principal):
            tx = ResourceTransaction(identity_tx.conn)
            if not admin:
                self._readable(tx, principal, resource_id, revision)
            row, version = tx.resource(resource_id), tx.version(resource_id, revision)
            if not row or not version:
                raise fail(ErrorCode.NOT_FOUND)
            return VersionView(
                resource=view(row),
                revision=revision,
                spec=json.loads(version["spec"]),
                digest=version["digest"],
                published=bool(version["published"]),
            )

    def changes(self, token: str, cursor: int, limit: int) -> ChangePage:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx = ResourceTransaction(identity_tx.conn)
            rows = tx.changes(
                principal.user.user_id,
                principal.session.device_id or "",
                principal.session.generation or 0,
                cursor,
                limit + 1,
            )
            page = rows[:limit]
            # Return the CURRENT desired version for each event. A device may
            # have missed several updates, including withdrawal; stale events
            # must not reinstall revoked versions.
            changes = []
            for row in page:
                resource = tx.resource(row["resource_id"])
                assert resource is not None
                revision = (
                    None
                    if resource["deleted"]
                    else (
                        resource["revision"]
                        if resource["owner_id"] == principal.user.user_id
                        else tx.assignment(
                            row["resource_id"],
                            principal.session.device_id or "",
                            principal.session.generation or 0,
                        )
                    )
                )
                changes.append(
                    Change(
                        cursor=row["cursor"],
                        resource_id=row["resource_id"],
                        revision=revision,
                        resource=view(resource),
                    )
                )
            return ChangePage(
                cursor=page[-1]["cursor"] if page else cursor,
                changes=changes,
                has_more=len(rows) > limit,
            )

    def report(self, token: str, body: InstallReport) -> None:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx = ResourceTransaction(identity_tx.conn)
            row = tx.resource(body.resource_id)
            if not row:
                raise fail(ErrorCode.NOT_FOUND)
            desired = (
                None
                if row["deleted"]
                else (
                    row["revision"]
                    if row["owner_id"] == principal.user.user_id
                    else tx.assignment(
                        body.resource_id,
                        principal.session.device_id or "",
                        principal.session.generation or 0,
                    )
                )
            )
            if row["owner_id"] != principal.user.user_id and not tx.has_assignment(
                body.resource_id,
                principal.user.user_id,
                principal.session.device_id,
                principal.session.generation,
            ):
                raise fail(ErrorCode.NOT_FOUND)
            if desired != body.desired_revision:
                raise fail(ErrorCode.RESOURCE_CONFLICT)
            if body.installed_revision is not None and not tx.version(
                body.resource_id, body.installed_revision
            ):
                raise fail(ErrorCode.RESOURCE_INVALID)
            tx.report(
                principal.session.device_id or "",
                principal.session.generation or 0,
                principal.user.user_id,
                body.resource_id,
                json.dumps({**body.model_dump(), "reported_at": int(time.time())}),
            )

    def reports(self, token: str, user_id: int) -> list[DeviceReport]:
        with self.identity.authorized_account(token, admin=True) as (identity_tx, _):
            return [
                DeviceReport(
                    **json.loads(r["report"]),
                    device_id=r["device_id"],
                    generation=r["generation"],
                    user_id=r["user_id"],
                )
                for r in ResourceTransaction(identity_tx.conn).reports(user_id)
            ]
