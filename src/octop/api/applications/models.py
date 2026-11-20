"""Thin cloud model management, native streaming and ledger HTTP adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from octop.api.applications.identity import Token
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.models.contracts import (
    BudgetInput,
    BudgetView,
    GrantInput,
    ModelContext,
    ModelDefinition,
    ModelRequest,
    ModelView,
    ProviderInput,
    ProviderView,
    ReconcileInput,
    RequestView,
    UsageSummary,
)
from octop.infra.models.gateway import ModelGateway, offload

router = APIRouter(prefix="/api", tags=["models"])


async def model_gateway(request: Request) -> ModelGateway:
    gateway = request.app.state.cloud.models
    if not isinstance(gateway, ModelGateway):
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED, status=503)
    return gateway


Gateway = Annotated[ModelGateway, Depends(model_gateway)]


@router.get(
    "/models",
    response_model=list[ModelView],
    summary="List authorized logical models, capabilities and rates",
)
async def catalog(gateway: Gateway, token: Token) -> list[ModelView]:
    return await offload(gateway.ledger.catalog, token)


@router.get(
    "/model-admin/providers",
    response_model=list[ProviderView],
    summary="List provider endpoints as an administrator; credentials are never returned",
)
async def providers(gateway: Gateway, token: Token) -> list[ProviderView]:
    return await offload(gateway.ledger.providers, token)


@router.put(
    "/model-admin/providers",
    response_model=ProviderView,
    summary="Configure a provider and encrypt its credential",
)
async def put_provider(body: ProviderInput, gateway: Gateway, token: Token) -> ProviderView:
    return await offload(gateway.ledger.put_provider, token, body)


@router.get(
    "/model-admin/models",
    response_model=list[ModelDefinition],
    summary="List administrator routing definitions",
)
async def definitions(gateway: Gateway, token: Token) -> list[ModelDefinition]:
    return await offload(gateway.ledger.definitions, token)


@router.put(
    "/model-admin/models",
    response_model=ModelDefinition,
    summary="Publish model routing and an immutable rate revision",
)
async def put_model(body: ModelDefinition, gateway: Gateway, token: Token) -> ModelDefinition:
    return await offload(gateway.ledger.put_model, token, body)


@router.put(
    "/model-admin/users/{user_id}/grants/{model_name}",
    status_code=204,
    summary="Authorize or revoke a logical model for one user",
)
async def grant(
    user_id: int, model_name: str, body: GrantInput, gateway: Gateway, token: Token
) -> Response:
    await offload(gateway.ledger.grant, token, user_id, "platform/" + model_name, body.allowed)
    return Response(status_code=204)


@router.put(
    "/model-admin/users/{user_id}/budget",
    response_model=BudgetView,
    summary="Set an absolute spending limit without changing settled or held amounts",
    description="The stored limit is enforced for regular users. Current admin roles are exempt; their usage and reservations remain recorded.",
)
async def budget(user_id: int, body: BudgetInput, gateway: Gateway, token: Token) -> BudgetView:
    return await offload(gateway.ledger.put_budget, token, user_id, body)


@router.get(
    "/usage/budgets",
    response_model=list[BudgetView],
    tags=["usage"],
    summary="Get owned budgets; administrators may select a user",
    description="unlimited reflects the current cloud role. For administrators, limit_nanos is stored configuration rather than an enforced cap. Accounting rows are created on first consumption.",
)
async def budgets(gateway: Gateway, token: Token, user_id: int | None = None) -> list[BudgetView]:
    return await offload(gateway.ledger.budgets, token, user_id)


@router.post(
    "/model-contexts",
    status_code=204,
    summary="Register an authorized Agent instance/thread/turn execution context",
    description="Current device only. Requires an owned personal Agent or current platform assignment. Shared definitions use distinct account-local instance IDs. Registers consumption associations without granting resource or model permissions.",
)
async def context(body: ModelContext, gateway: Gateway, token: Token) -> Response:
    await offload(gateway.ledger.register_context, token, body)
    return Response(status_code=204)


@router.post(
    "/models/requests",
    response_model=RequestView,
    summary="Invoke a model once using a stable logical request ID",
    description="Current device only. First execution returns native JSON or SSE; replay returns HTTP 202 with the durable request status and never invokes a provider. There is no stream replay cache.",
    responses={
        200: {
            "description": "Native model JSON or SSE",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}},
                "application/json": {"schema": {"type": "object", "additionalProperties": True}},
            },
        },
        202: {"model": RequestView},
    },
)
async def invoke(body: ModelRequest, gateway: Gateway, token: Token) -> Response:
    exchange = await gateway.open(token, body)
    if isinstance(exchange, RequestView):
        return JSONResponse(exchange.model_dump(mode="json"), status_code=202)
    streamed = exchange.ready.result()
    if streamed:

        async def events() -> AsyncIterator[bytes]:
            try:
                async for chunk in exchange.chunks():
                    yield chunk
            except OctopError as error:
                yield (
                    "event: error\ndata: "
                    + json.dumps(
                        {
                            "type": "error",
                            "error": {"type": "api_error", **error.to_envelope()["error"]},
                        }
                    )
                    + "\n\n"
                ).encode()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
            background=BackgroundTask(exchange.close),
        )
    data = bytearray()
    async for chunk in exchange.chunks():
        data.extend(chunk)
    return Response(bytes(data), media_type="application/json")


@router.get(
    "/models/requests/{request_id}",
    response_model=RequestView,
    summary="Get an owned request and its attempt accounting states",
)
async def status(request_id: str, gateway: Gateway, token: Token) -> RequestView:
    return await offload(gateway.ledger.request, token, request_id)


@router.get(
    "/usage/requests",
    response_model=list[RequestView],
    tags=["usage"],
    summary="List owned request costs; administrators may select a user",
)
async def requests(
    gateway: Gateway,
    token: Token,
    user_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[RequestView]:
    return await offload(gateway.ledger.requests, token, user_id=user_id, limit=limit)


@router.get(
    "/usage/summary",
    response_model=list[UsageSummary],
    tags=["usage"],
    summary="Aggregate settled usage and costs by logical model and currency",
)
async def summary(
    gateway: Gateway,
    token: Token,
    user_id: int | None = None,
    since: int = Query(default=0, ge=0),
    until: int = Query(default=2**62, ge=1),
) -> list[UsageSummary]:
    return await offload(gateway.ledger.summary, token, user_id=user_id, since=since, until=until)


@router.post(
    "/model-admin/attempts/{attempt_id}/reconcile",
    status_code=204,
    tags=["usage"],
    summary="Resolve unknown usage with an administrator and provider evidence",
    description="Only reconciliation_required attempts can change. Resolution is atomic and idempotent for the same evidence and usage. Device-reported token counts cannot settle a request.",
)
async def reconcile(
    attempt_id: str, body: ReconcileInput, gateway: Gateway, token: Token
) -> Response:
    await offload(gateway.ledger.reconcile, token, attempt_id, body)
    return Response(status_code=204)
