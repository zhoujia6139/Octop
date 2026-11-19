"""Live identity, immutable rates and atomic budget/attempt transitions."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from cryptography.fernet import Fernet

from octop.infra.db.repos.cloud_models import CloudModelRepo, ModelTransaction
from octop.infra.db.repos.cloud_resources import ResourceTransaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.service import CloudIdentity
from octop.infra.models.contracts import (
    AttemptView,
    BudgetInput,
    BudgetView,
    ModelContext,
    ModelDefinition,
    ModelRequest,
    ModelView,
    ProviderInput,
    ProviderView,
    ReconcileInput,
    RequestView,
    Usage,
    UsageSummary,
)
from octop.infra.utils.ulid import new_ulid

LEASE_SECONDS = 180


def fail(code: ErrorCode = ErrorCode.RESOURCE_NOT_ALLOWED) -> OctopError:
    return OctopError.localized(code)


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class ModelLedger:
    def __init__(self, repo: CloudModelRepo, identity: CloudIdentity, cipher: Fernet) -> None:
        self.repo, self.identity, self.cipher = repo, identity, cipher

    @contextmanager
    def _account(
        self, token: str, *, admin: bool = False
    ) -> Iterator[tuple[ModelTransaction, int]]:
        with self.identity.authorized_account(token, admin=admin) as (identity_tx, principal):
            yield (
                ModelTransaction(identity_tx.conn, postgres=self.repo.db.dialect == "postgresql"),
                principal.user.user_id,
            )

    @contextmanager
    def _read(self, token: str, user_id: int | None) -> Iterator[tuple[ModelTransaction, int]]:
        if user_id is not None:
            with self._account(token, admin=True) as (tx, _):
                yield tx, user_id
        else:
            with self.identity.authorized_session(token) as (identity_tx, principal):
                yield ModelTransaction(identity_tx.conn), principal.user.user_id

    def providers(self, token: str) -> list[ProviderView]:
        with self._account(token, admin=True) as (tx, _):
            return [
                ProviderView(
                    provider_id=r["provider_id"],
                    base_url=r["base_url"],
                    free_rejection_statuses=json.loads(r["free_statuses"]),
                )
                for r in tx.providers()
            ]

    def put_provider(self, token: str, body: ProviderInput) -> ProviderView:
        encrypted = self.cipher.encrypt(body.api_key.get_secret_value().encode())
        with self._account(token, admin=True) as (tx, _):
            tx.put_provider(
                body.provider_id,
                body.base_url,
                encrypted,
                [int(s) for s in body.free_rejection_statuses],
            )
        return ProviderView(
            provider_id=body.provider_id,
            base_url=body.base_url,
            free_rejection_statuses=[int(status) for status in body.free_rejection_statuses],
        )

    def put_model(self, token: str, body: ModelDefinition) -> ModelDefinition:
        with self._account(token, admin=True) as (tx, _):
            if any(tx.provider(r.provider_id) is None for r in body.routes):
                raise fail(ErrorCode.NOT_FOUND)
            revision = body.rate.revision
            definition = canonical(body.rate.model_dump())
            tx.put_rate(revision, definition)
            if tx.rate(revision) != definition:
                raise fail(ErrorCode.MODEL_REQUEST_CONFLICT)
            tx.put_model(body.model_id, body.model_dump_json())
        return body

    def definitions(self, token: str) -> list[ModelDefinition]:
        with self._account(token, admin=True) as (tx, _):
            return [ModelDefinition.model_validate_json(r) for r in tx.models()]

    def catalog(self, token: str) -> list[ModelView]:
        with self.identity.authorized_session(token) as (identity_tx, principal):
            tx = ModelTransaction(identity_tx.conn)
            return [
                ModelView.model_validate(m.model_dump(), extra="ignore")
                for raw in tx.models(principal.user.user_id)
                if (m := ModelDefinition.model_validate_json(raw)).enabled
                and m.rate.effective_at <= int(time.time())
            ]

    def grant(self, token: str, user_id: int, model_id: str, allowed: bool) -> None:
        with self._account(token, admin=True) as (tx, _):
            # Lock the target exactly as reservations do; a completed revoke
            # cannot race a later reservation using an earlier grant read.
            if not tx.lock_user(user_id) or tx.model(model_id) is None:
                raise fail(ErrorCode.NOT_FOUND)
            tx.grant(user_id, model_id, allowed)

    def put_budget(self, token: str, user_id: int, body: BudgetInput) -> BudgetView:
        with self._account(token, admin=True) as (tx, _):
            if not tx.lock_user(user_id):
                raise fail(ErrorCode.NOT_FOUND)
            existing = tx.budget(user_id, body.currency)
            if existing and body.limit_nanos < existing["spent_nanos"] + existing["reserved_nanos"]:
                raise fail(ErrorCode.MODEL_REQUEST_CONFLICT)
            tx.put_budget(user_id, body.currency, body.limit_nanos)
            return BudgetView.model_validate(tx.budget(user_id, body.currency))

    def budgets(self, token: str, user_id: int | None = None) -> list[BudgetView]:
        with self._read(token, user_id) as (tx, current):
            return [
                BudgetView.model_validate(r)
                for r in tx.budgets(user_id if user_id is not None else current)
            ]

    def register_context(self, token: str, body: ModelContext) -> None:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx = ModelTransaction(identity_tx.conn)
            identity_tx.setting("resource_management_lock", "1")
            resources = ResourceTransaction(identity_tx.conn)
            resource_id = body.resource_id or body.agent_id
            existing = resources.execution_resource(body.agent_id)
            resource = resources.resource(resource_id)
            if not resources.agent_allowed(
                resource_id,
                principal.user.user_id,
                principal.session.device_id,
                principal.session.generation,
            ):
                raise fail()
            if (existing and existing != resource_id) or (
                resource and resource["scope"] == "platform" and body.agent_id == resource_id
            ):
                raise fail(ErrorCode.RESOURCE_INVALID)
            if not tx.register_context(
                principal.user.user_id, body.agent_id, body.thread_id, body.turn_id
            ):
                raise fail()
            resources.bind_execution(body.agent_id, resource_id)

    def _view(self, tx: ModelTransaction, row: dict[str, Any]) -> RequestView:
        attempts = [
            AttemptView.model_validate(
                {**a, "usage": Usage.model_validate_json(a["usage"]) if a["usage"] else None},
                extra="ignore",
            )
            for a in tx.attempts(row["request_id"])
        ]
        return RequestView.model_validate({**row, "attempts": attempts}, extra="ignore")

    def requests(
        self, token: str, *, user_id: int | None = None, limit: int = 100
    ) -> list[RequestView]:
        with self._read(token, user_id) as (tx, current):
            return [
                self._view(tx, r)
                for r in tx.requests(user_id if user_id is not None else current, limit)
            ]

    def request(self, token: str, request_id: str) -> RequestView:
        identity = self.identity.authenticate(token)
        with self.repo.transaction() as tx:
            row = tx.request(request_id)
            if not row or row["user_id"] != identity.user.user_id:
                raise fail(ErrorCode.NOT_FOUND)
            return self._view(tx, row)

    def summary(
        self, token: str, *, user_id: int | None = None, since: int = 0, until: int = 2**62
    ) -> list[UsageSummary]:
        with self._read(token, user_id) as (tx, current):
            return [
                UsageSummary.model_validate(
                    {k: int(v) if k != "model_id" and k != "currency" else v for k, v in r.items()}
                )
                for r in tx.summary(user_id if user_id is not None else current, since, until)
            ]

    def reserve(
        self, token: str, body: ModelRequest
    ) -> tuple[ModelDefinition, str | None, RequestView | None]:
        from octop.infra.models.protocols import validate_payload

        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx = ModelTransaction(identity_tx.conn)
            user_id = principal.user.user_id
            identity_tx.setting("model_request_ids_lock", "1")
            previous = tx.request(body.request_id)
            request_hash = hashlib.sha256(canonical(body.model_dump()).encode()).hexdigest()
            if previous:
                if previous["user_id"] != user_id:
                    raise fail(ErrorCode.NOT_FOUND)
                if (
                    previous["device_id"] != principal.session.device_id
                    or previous["generation"] != principal.session.generation
                    or previous["request_hash"] != request_hash
                ):
                    raise fail(ErrorCode.MODEL_REQUEST_CONFLICT)
                return (
                    ModelDefinition.model_validate_json(previous["definition"]),
                    None,
                    self._view(tx, previous),
                )
            identity_tx.setting("resource_management_lock", "1")
            resources = ResourceTransaction(identity_tx.conn)
            resource_id = resources.execution_resource(body.agent_id)
            if (
                not resource_id
                or (body.resource_id and body.resource_id != resource_id)
                or not resources.agent_allowed(
                    resource_id, user_id, principal.session.device_id, principal.session.generation
                )
            ):
                raise fail()
            if not tx.context_owned(user_id, body.agent_id, body.thread_id, body.turn_id):
                raise fail()
            raw = tx.model(body.model_id)
            if not raw or not tx.allowed(user_id, body.model_id):
                raise fail()
            model = ModelDefinition.model_validate_json(raw)
            if not model.enabled or model.rate.effective_at > int(time.time()):
                raise fail()
            validate_payload(model, body)
            reservation = (
                model.input_token_ceiling
                * max(model.rate.uncached_input, model.rate.cache_read, model.rate.cache_write)
                + model.output_token_ceiling * model.rate.output
            )
            self._reserve(tx, user_id, model.rate.currency, reservation)
            now = int(time.time())
            row = {
                "request_id": body.request_id,
                "user_id": user_id,
                "device_id": principal.session.device_id,
                "generation": principal.session.generation,
                "agent_id": body.agent_id,
                "thread_id": body.thread_id,
                "turn_id": body.turn_id,
                "model_id": body.model_id,
                "purpose": body.purpose,
                "request_hash": request_hash,
                "state": "reserved",
                "currency": model.rate.currency,
                "rate_revision": model.rate.revision,
                "definition": model.model_dump_json(),
                "created_at": now,
                "lease_until": now + LEASE_SECONDS,
            }
            tx.create_request(row)
            attempt_id = new_ulid()
            tx.add_attempt(attempt_id, body.request_id, model.routes[0].provider_id, reservation)
            return model, attempt_id, None

    @staticmethod
    def _reserve(tx: ModelTransaction, user_id: int, currency: str, amount: int) -> None:
        budget = tx.budget(user_id, currency)
        if (
            not budget
            or budget["limit_nanos"] - budget["spent_nanos"] - budget["reserved_nanos"] < amount
        ):
            raise fail(ErrorCode.TOKEN_QUOTA_EXCEEDED)
        tx.balance(user_id, currency, reserved=amount)

    def begin_attempt(
        self, token: str, request_id: str, attempt_id: str, route_index: int
    ) -> dict[str, Any]:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx = ModelTransaction(identity_tx.conn)
            request = tx.request(request_id)
            if (
                not request
                or request["user_id"] != principal.user.user_id
                or request["device_id"] != principal.session.device_id
                or request["generation"] != principal.session.generation
            ):
                raise fail()
            identity_tx.setting("resource_management_lock", "1")
            resources = ResourceTransaction(identity_tx.conn)
            resource_id = resources.execution_resource(request["agent_id"])
            if not resource_id or not resources.agent_allowed(
                resource_id,
                principal.user.user_id,
                principal.session.device_id,
                principal.session.generation,
            ):
                raise fail()
            model = ModelDefinition.model_validate_json(request["definition"])
            current = tx.model(model.model_id)
            if (
                not current
                or not ModelDefinition.model_validate_json(current).enabled
                or not tx.allowed(principal.user.user_id, model.model_id)
            ):
                raise fail()
            attempt = tx.attempt(attempt_id)
            if attempt is None or attempt["state"] != "reserved":
                raise fail(ErrorCode.MODEL_REQUEST_CONFLICT)
            provider = tx.provider(model.routes[route_index].provider_id)
            if provider is None:
                raise fail(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
            tx.attempt_state(attempt_id, "running")
            tx.request_state(request_id, "running", int(time.time()) + LEASE_SECONDS)
            return {
                **provider,
                "api_key": self.cipher.decrypt(bytes(provider["credential_enc"])).decode(),
            }

    @contextmanager
    def _locked_request(self, request_id: str) -> Iterator[tuple[ModelTransaction, dict[str, Any]]]:
        with self.identity.repo.transaction() as identity_tx:
            tx = ModelTransaction(identity_tx.conn, postgres=self.repo.db.dialect == "postgresql")
            request = tx.request(request_id)
            if request is None:
                raise fail(ErrorCode.NOT_FOUND)
            identity_tx.user(user_id=request["user_id"])
            current = tx.request(request_id)
            assert current is not None
            yield tx, current

    def finish(
        self,
        request_id: str,
        attempt_id: str,
        *,
        usage: Usage | None,
        known_free: bool = False,
        reason: str | None = None,
        evidence: str | None = None,
        resolved_by: int | None = None,
    ) -> None:
        with self._locked_request(request_id) as (tx, request):
            self._finish(
                tx,
                request,
                attempt_id,
                usage=usage,
                known_free=known_free,
                reason=reason,
                evidence=evidence,
                resolved_by=resolved_by,
            )

    def _finish(
        self,
        tx: ModelTransaction,
        request: dict[str, Any],
        attempt_id: str,
        *,
        usage: Usage | None,
        known_free: bool,
        reason: str | None,
        evidence: str | None = None,
        resolved_by: int | None = None,
    ) -> None:
        attempt = tx.attempt(attempt_id)
        if (
            attempt is None
            or attempt["request_id"] != request["request_id"]
            or attempt["state"] in {"settled", "released"}
        ):
            return
        if usage is None and not known_free:
            tx.attempt_state(attempt_id, "reconciliation_required", reason=reason)
            tx.request_state(request["request_id"], "reconciliation_required", 0)
            return
        model = ModelDefinition.model_validate_json(request["definition"])
        if (
            usage
            and resolved_by is None
            and (
                usage.uncached_input + usage.cache_read + usage.cache_write
                > model.input_token_ceiling
                or usage.output > model.output_token_ceiling
            )
        ):
            tx.attempt_state(
                attempt_id,
                "reconciliation_required",
                reason="provider_exceeded_configured_ceiling",
                usage=usage.model_dump_json(),
            )
            tx.request_state(request["request_id"], "reconciliation_required", 0)
            return
        cost = usage.cost(model.rate) if usage else 0
        if cost > 10**18:
            tx.attempt_state(attempt_id, "reconciliation_required", reason="usage_out_of_range")
            tx.request_state(request["request_id"], "reconciliation_required", 0)
            return
        state = "settled" if usage else "released"
        tx.balance(
            request["user_id"], request["currency"], spent=cost, reserved=-attempt["reserved_nanos"]
        )
        tx.attempt_state(
            attempt_id,
            state,
            cost=cost,
            usage=usage.model_dump_json() if usage else None,
            reason=reason,
            evidence=evidence,
            resolved_by=resolved_by,
        )
        if usage:
            tx.settle(attempt_id, request, cost, usage.model_dump(), int(time.time()))
        tx.request_state(request["request_id"], state, 0)

    def next_attempt(self, token: str, request_id: str, provider_id: str) -> str:
        with self.identity.authorized_device(token) as (identity_tx, principal):
            tx = ModelTransaction(identity_tx.conn)
            request = tx.request(request_id)
            if (
                not request
                or request["user_id"] != principal.user.user_id
                or request["state"] != "released"
            ):
                raise fail(ErrorCode.MODEL_REQUEST_CONFLICT)
            model = ModelDefinition.model_validate_json(request["definition"])
            amount = (
                model.input_token_ceiling
                * max(model.rate.uncached_input, model.rate.cache_read, model.rate.cache_write)
                + model.output_token_ceiling * model.rate.output
            )
            self._reserve(tx, request["user_id"], request["currency"], amount)
            attempt_id = new_ulid()
            tx.add_attempt(attempt_id, request_id, provider_id, amount)
            tx.request_state(request_id, "reserved", int(time.time()) + LEASE_SECONDS)
            return attempt_id

    def reconcile(self, token: str, attempt_id: str, body: ReconcileInput) -> None:
        with self._account(token, admin=True) as (tx, admin_id):
            attempt = tx.attempt(attempt_id)
            if not attempt:
                raise fail(ErrorCode.NOT_FOUND)
            request = tx.request(attempt["request_id"])
            assert request is not None
            tx.lock_user(request["user_id"])
            attempt = tx.attempt(attempt_id)
            assert attempt is not None
            if attempt["state"] in {"settled", "released"}:
                if attempt["evidence"] == body.evidence and attempt["usage"] == (
                    body.usage.model_dump_json() if body.usage else None
                ):
                    return
                raise fail(ErrorCode.MODEL_REQUEST_CONFLICT)
            if attempt["state"] != "reconciliation_required":
                raise fail(ErrorCode.MODEL_REQUEST_CONFLICT)
            self._finish(
                tx,
                request,
                attempt_id,
                usage=body.usage,
                known_free=body.usage is None,
                reason="administrator_reconciliation",
                evidence=body.evidence,
                resolved_by=admin_id,
            )

    def heartbeat(self, request_id: str) -> None:
        with self._locked_request(request_id) as (tx, _):
            tx.heartbeat(request_id, int(time.time()) + LEASE_SECONDS)

    def recover_stale(self) -> None:
        with self.repo.transaction() as tx:
            stale = tx.stale(int(time.time()))
        for row in stale:
            with self._locked_request(row["request_id"]) as (tx, current):
                if current["state"] not in {"reserved", "running"} or current["lease_until"] >= int(
                    time.time()
                ):
                    continue
                for attempt in tx.attempts(current["request_id"]):
                    if attempt["state"] in {"reserved", "running"}:
                        self._finish(
                            tx,
                            current,
                            attempt["attempt_id"],
                            usage=None,
                            known_free=attempt["state"] == "reserved",
                            reason="expired_worker_lease",
                        )
