"""P3 native protocols, concurrent budgets and real desktop/Harness gateway I/O."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from octop.api.applications.local import create_local_app
from octop.application_config import LocalSettings
from octop.infra.models.contracts import ModelRequest
from octop.infra.models.gateway import offload
from octop.infra.utils.ulid import new_ulid
from tests.integration import test_cloud_identity as identity_tests
from tests.support.http import ws_connect

cloud = identity_tests.cloud
headers = identity_tests.headers
LOCAL_TOKEN = identity_tests.LOCAL_TOKEN
LOCAL_HEADERS = identity_tests.LOCAL_HEADERS


def sse(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


class ProviderStream(httpx.AsyncByteStream):
    def __init__(self, parts, *, wait=False, broken=False):
        self.parts, self.wait, self.broken = parts, wait, broken
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        for part in self.parts:
            yield part
        self.started.set()
        if self.wait:
            await asyncio.Future()
        if self.broken:
            raise httpx.ReadError("upstream disconnected after consuming tokens")

    async def aclose(self):
        self.closed.set()


class Supplier:
    def __init__(self):
        self.calls = []
        self.mode = "normal"
        self.streams = []

    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append((request, body))
        if request.url.host == "reject.test" or self.mode == "reject":
            return httpx.Response(429, json={"error": {"message": "secret provider failure"}})
        if self.mode == "unknown-http":
            return httpx.Response(500, json={"error": {"message": "never forward this secret"}})
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                    "model": body["model"],
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                },
            )
        if request.url.path == "/v1/messages":
            usage = {
                "input_tokens": 5,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
                "output_tokens": 4,
            }
            message = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "model": body["model"],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": usage,
            }
            parts = [
                sse(
                    {
                        "type": "message_start",
                        "message": {
                            **message,
                            "content": [],
                            "stop_reason": None,
                            "usage": {**usage, "output_tokens": 0},
                        },
                    }
                ),
                sse(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }
                ),
                sse(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Hello"},
                    }
                ),
                sse({"type": "content_block_stop", "index": 0}),
                sse(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 4},
                    }
                ),
                sse({"type": "message_stop"}),
            ]
        elif request.url.path == "/v1/responses":
            usage = {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
                "output_tokens": 4,
                "output_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 14,
            }
            message = {
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "model": body["model"],
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "Hello", "annotations": []}],
                    }
                ],
                "usage": usage,
                "parallel_tool_calls": False,
                "tools": [],
                "tool_choice": "auto",
            }
            parts = [
                sse(
                    {
                        "type": "response.created",
                        "response": {
                            **message,
                            "status": "in_progress",
                            "output": [],
                            "usage": None,
                        },
                        "sequence_number": 0,
                    }
                ),
                sse(
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "Hello",
                        "sequence_number": 1,
                    }
                ),
                sse({"type": "response.completed", "response": message, "sequence_number": 2}),
            ]
        else:
            usage = {
                "prompt_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
                "completion_tokens": 4,
                "completion_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 14,
            }
            completed = body.get("messages", [])[-1].get("role") == "tool"
            if self.mode == "tool" and not completed:
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"file_path":"note.txt"}',
                            },
                        }
                    ],
                }
                reason = "tool_calls"
                reply = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {k: v for k, v in delta["tool_calls"][0].items() if k != "index"}
                    ],
                }
            else:
                text = (
                    "Tool completed: " + str(body["messages"][-1]["content"])
                    if completed
                    else "Hello"
                )
                delta = {"role": "assistant", "content": text}
                reason = "stop"
                reply = delta
            base = {
                "id": "chat_test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body["model"],
            }
            message = {
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": reply, "finish_reason": reason}],
                "usage": usage,
            }
            parts = [
                sse({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
                sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}),
                sse({**base, "choices": [], "usage": usage}),
                b"data: [DONE]\n\n",
            ]
        if self.mode == "tool" and request.url.path == "/v1/messages":
            last = body["messages"][-1].get("content", [])
            completed = isinstance(last, list) and any(
                item.get("type") == "tool_result" for item in last
            )
            if not completed:
                tool = {
                    "type": "tool_use",
                    "id": "call_read",
                    "name": "read_file",
                    "input": {"file_path": "note.txt"},
                }
                message["content"], message["stop_reason"] = [tool], "tool_use"
                parts = [
                    sse(
                        {
                            "type": "message_start",
                            "message": {
                                **message,
                                "content": [],
                                "stop_reason": None,
                                "usage": {**usage, "output_tokens": 0},
                            },
                        }
                    ),
                    sse(
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {**tool, "input": {}},
                        }
                    ),
                    sse(
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "input_json_delta", "partial_json": '{"file_path":'},
                        }
                    ),
                    sse(
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "input_json_delta", "partial_json": '"note.txt"}'},
                        }
                    ),
                    sse({"type": "content_block_stop", "index": 0}),
                    sse(
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                            "usage": {"output_tokens": 4},
                        }
                    ),
                    sse({"type": "message_stop"}),
                ]
            else:
                text = "Tool completed: " + str(last)
                message["content"][0]["text"] = text
                for index, part in enumerate(parts):
                    parts[index] = part.replace(b"Hello", text.encode())
        if self.mode == "tool" and request.url.path == "/v1/responses":
            completed = any(item.get("type") == "function_call_output" for item in body["input"])
            if not completed:
                tool = {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_read",
                    "name": "read_file",
                    "arguments": '{"file_path":"note.txt"}',
                    "status": "completed",
                }
                message["output"] = [tool]
                parts = [
                    sse(
                        {
                            "type": "response.created",
                            "response": {
                                **message,
                                "status": "in_progress",
                                "output": [],
                                "usage": None,
                            },
                            "sequence_number": 0,
                        }
                    ),
                    sse(
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {**tool, "arguments": "", "status": "in_progress"},
                            "sequence_number": 1,
                        }
                    ),
                    sse(
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": 0,
                            "item_id": "fc_1",
                            "delta": '{"file_path":',
                            "sequence_number": 2,
                        }
                    ),
                    sse(
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": 0,
                            "item_id": "fc_1",
                            "delta": '"note.txt"}',
                            "sequence_number": 3,
                        }
                    ),
                    sse(
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": tool,
                            "sequence_number": 4,
                        }
                    ),
                    sse({"type": "response.completed", "response": message, "sequence_number": 5}),
                ]
            else:
                text = "Tool completed: " + str(
                    [item for item in body["input"] if item.get("type") == "function_call_output"]
                )
                message["output"][0]["content"][0]["text"] = text
                for index, part in enumerate(parts):
                    parts[index] = part.replace(b"Hello", text.encode())
        if not body.get("stream"):
            if self.mode == "missing-usage":
                message.pop("usage", None)
            return httpx.Response(200, json=message)
        if self.mode == "sse-error":
            # Split within both JSON fields and the secret. The gateway must
            # validate the entire error frame before forwarding any of it.
            failure = sse(
                {"type": "error", "error": {"type": "api_error", "message": "supplier-private-key"}}
            )
            parts = parts[:1] + [failure[index : index + 1] for index in range(len(failure))]
        if self.mode in {"hang", "broken", "missing-usage"}:
            parts = parts[:1]
        if request.url.path == "/v1/messages":
            parts = [
                b"event: "
                + json.loads(part.decode().removeprefix("data: "))["type"].encode()
                + b"\n"
                + part
                for part in parts
            ]
        stream = ProviderStream(parts, wait=self.mode == "hang", broken=self.mode == "broken")
        self.streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


def definition(protocol="chat_completions", name="test", *, fallback=False):
    return {
        "model_id": "platform/" + name,
        "name": name,
        "protocol": protocol,
        "input_modalities": ["text", "image"] if protocol != "embeddings" else ["text"],
        "tools": protocol != "embeddings",
        "input_token_ceiling": 1000,
        "output_token_ceiling": 0 if protocol == "embeddings" else 100,
        "enabled": True,
        "rate": {
            "revision": "rate-" + name,
            "currency": "USD",
            "effective_at": 0,
            "uncached_input": 3,
            "cache_read": 1,
            "cache_write": 4,
            "output": 8,
        },
        "routes": ([{"provider_id": "reject", "upstream_model": "native"}] if fallback else [])
        + [{"provider_id": "supplier", "upstream_model": "native"}],
    }


@pytest.fixture
async def platform(cloud, request):
    app, client, account = cloud
    gateway = app.state.cloud.models
    await gateway.client.aclose()
    supplier = Supplier()
    gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(supplier))
    for provider_id, host in [("supplier", "supplier.test"), ("reject", "reject.test")]:
        response = await client.put(
            "/api/model-admin/providers",
            headers=headers(account),
            json={
                "provider_id": provider_id,
                "base_url": "https://" + host + "/v1",
                "api_key": "supplier-private-key",
                "free_rejection_statuses": [429],
            },
        )
        assert response.status_code == 200, response.text
        assert "supplier-private-key" not in response.text
    role = getattr(request, "param", "admin")
    consumer = account
    if role == "user":
        created = await client.post(
            "/api/users",
            headers=headers(account),
            json={"username": "consumer", "password": identity_tests.PASSWORD, "role": "user"},
        )
        assert created.status_code == 201, created.text
        consumer = (await identity_tests.login(client, "consumer"))["session"]["access_token"]
    grant = await identity_tests.bind(client, consumer)
    device = grant["session"]["access_token"]
    user_id = grant["session"]["identity"]["user"]["user_id"]

    async def configure(body=None, budget=1000000):
        body = body or definition()
        response = await client.put("/api/model-admin/models", headers=headers(account), json=body)
        assert response.status_code == 200, response.text
        response = await client.put(
            f"/api/model-admin/users/{user_id}/grants/" + body["model_id"].split("/")[1],
            headers=headers(account),
            json={"allowed": True},
        )
        assert response.status_code == 204, response.text
        if budget is not None:
            response = await client.put(
                f"/api/model-admin/users/{user_id}/budget",
                headers=headers(account),
                json={"currency": "USD", "limit_nanos": budget},
            )
            assert response.status_code == 200, response.text
        return body

    await configure(budget=None if role == "admin" else 1000000)
    context = {"agent_id": new_ulid(), "thread_id": new_ulid(), "turn_id": new_ulid()}
    # P4 consumption contexts require a formal personal or assigned Agent.
    created = await client.put(
        "/api/resources/personal/" + context["agent_id"],
        headers=headers(device),
        json={
            "operation_id": new_ulid(),
            "expected_revision": 0,
            "spec": {"kind": "agent", "name": "Test", "model_id": "platform/test"},
        },
    )
    assert created.status_code == 200, created.text
    assert (
        await client.post("/api/model-contexts", headers=headers(device), json=context)
    ).status_code == 204

    def request(payload=None, model="platform/test", purpose="chat"):
        return {
            **context,
            "request_id": new_ulid(),
            "model_id": model,
            "purpose": purpose,
            "payload": payload or {"messages": [{"role": "user", "content": "hello"}]},
        }

    return app, client, account, device, supplier, configure, request, grant


@pytest.mark.parametrize("budget", [None, 0])
@pytest.mark.parametrize("fallback", [False, True])
async def test_admin_ignores_budget_limits_and_keeps_exact_idempotent_accounting(
    platform, budget, fallback
):
    app, client, account, device, supplier, configure, request, grant = platform
    await configure(definition(fallback=fallback), budget=budget)
    if budget is None:
        assert (await client.get("/api/usage/budgets", headers=headers(device))).json() == []
    body = request()
    for expected_status in (200, 202):
        response = await client.post("/api/models/requests", headers=headers(device), json=body)
        assert response.status_code == expected_status, response.text
    status = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert status["state"] == "settled"
    assert [a["state"] for a in status["attempts"]] == (
        ["released", "settled"] if fallback else ["settled"]
    )
    assert len(supplier.calls) == (2 if fallback else 1)
    assert (
        await client.post("/api/models/requests", headers=headers(device), json=request())
    ).status_code == 200
    balance = (await client.get("/api/usage/budgets", headers=headers(device))).json()[0]
    assert balance == {
        "user_id": grant["session"]["identity"]["user"]["user_id"],
        "currency": "USD",
        "limit_nanos": 0,
        "spent_nanos": 116,
        "reserved_nanos": 0,
        "unlimited": True,
    }
    summary = (await client.get("/api/usage/summary", headers=headers(device))).json()
    assert summary[0]["cost_nanos"] == 116
    assert summary[0]["settled_attempts"] == 2


@pytest.mark.parametrize("platform", ["user"], indirect=True)
async def test_budget_exemption_follows_current_role_and_preserves_held_amounts(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    await configure(budget=0)
    user_id = grant["session"]["identity"]["user"]["user_id"]
    rejected = await client.post("/api/models/requests", headers=headers(device), json=request())
    assert rejected.json()["error"]["code"] == "TOKEN_QUOTA_EXCEEDED"
    assert not supplier.calls
    response = await client.put(
        f"/api/users/{user_id}",
        headers=headers(account),
        json={"enabled": True, "role": "admin"},
    )
    assert response.status_code == 200, response.text
    consumer = (await identity_tests.login(client, "consumer"))["session"]["access_token"]
    device = (await identity_tests.bind(client, consumer))["session"]["access_token"]
    ledger = app.state.cloud.models.ledger
    attempts = await asyncio.gather(
        offload(ledger.reserve, device, ModelRequest.model_validate(request())),
        offload(ledger.reserve, device, ModelRequest.model_validate(request())),
    )
    balance = (await client.get("/api/usage/budgets", headers=headers(device))).json()[0]
    assert balance["unlimited"] is True
    assert balance["reserved_nanos"] == 9600
    response = await client.put(
        f"/api/users/{user_id}",
        headers=headers(account),
        json={"enabled": True, "role": "user"},
    )
    assert response.status_code == 200, response.text
    consumer = (await identity_tests.login(client, "consumer"))["session"]["access_token"]
    device = (await identity_tests.bind(client, consumer))["session"]["access_token"]
    rejected = await client.post("/api/models/requests", headers=headers(device), json=request())
    assert rejected.json()["error"]["code"] == "TOKEN_QUOTA_EXCEEDED"
    balance = (await client.get("/api/usage/budgets", headers=headers(device))).json()[0]
    assert balance["unlimited"] is False
    assert balance["reserved_nanos"] == 9600
    for _, attempt_id, _ in attempts:
        with ledger.repo.transaction() as tx:
            attempt = tx.attempt(attempt_id)
        await offload(ledger.finish, attempt["request_id"], attempt_id, usage=None, known_free=True)
    balance = (await client.get("/api/usage/budgets", headers=headers(device))).json()[0]
    assert balance["reserved_nanos"] == 0


async def test_catalog_credential_storage_authorization_and_immutable_rates(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    response = await client.get("/api/models", headers=headers(device))
    assert response.status_code == 200
    assert response.json()[0]["model_id"] == "platform/test"
    assert all(
        secret not in response.text
        for secret in ["supplier.test", "native", "supplier-private-key", "routes", "base_url"]
    )
    with app.state.cloud.db.connect() as conn:
        secret = bytes(
            conn.execute("SELECT credential_enc FROM cloud_model_providers LIMIT 1").fetchone()[0]
        )
        assert b"supplier-private-key" not in secret
    assert (
        await client.get("/api/model-admin/providers", headers=headers(device))
    ).status_code == 401
    body = definition()
    body["rate"]["output"] = 999
    assert (
        await client.put("/api/model-admin/models", headers=headers(account), json=body)
    ).status_code == 409
    assert (
        await client.post("/api/models/requests", headers=headers(account), json=request())
    ).status_code == 401
    for change in [
        {"user_id": 99},
        {"payload": {"messages": [], "base_url": "https://evil.test"}},
        {"payload": {"model": "other", "messages": []}},
        {"payload": {"messages": [], "max_tokens": 101}},
        {"payload": {"messages": [], "n": 2}},
        {"payload": {"messages": [], "tools": [{"type": "web_search"}]}},
    ]:
        assert (
            await client.post(
                "/api/models/requests", headers=headers(device), json={**request(), **change}
            )
        ).status_code == 422
    oversized = request({"messages": [{"role": "user", "content": "x" * (8 * 1024 * 1024)}]})
    response = await client.post("/api/models/requests", headers=headers(device), json=oversized)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "MODEL_REQUEST_TOO_LARGE"
    assert not supplier.calls
    assert (
        await client.put("/api/model-admin/models", headers=headers(device), json=definition())
    ).status_code == 401
    assert (await client.get("/api/docs")).status_code == 200
    schema = (await client.get("/api/openapi.json")).json()
    assert schema["paths"]["/api/models/requests"]["post"]["responses"]["202"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("RequestView")


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages", "embeddings"])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_protocols_preserve_tools_images_usage_and_settle_once(
    platform, protocol, stream
):
    if protocol == "embeddings" and stream:
        pytest.skip("embedding is a separate non-streaming capability")
    app, client, account, device, supplier, configure, request, grant = platform
    await configure(definition(protocol))
    image = "data:image/png;base64,iVBORw0KGgo="
    if protocol == "chat_completions":
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ],
            "tools": [
                {"type": "function", "function": {"name": "test", "parameters": {"type": "object"}}}
            ],
        }
    elif protocol == "responses":
        payload = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe"},
                        {"type": "input_image", "image_url": image},
                    ],
                }
            ],
            "tools": [{"type": "function", "name": "test", "parameters": {"type": "object"}}],
        }
    elif protocol == "messages":
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                }
            ],
            "tools": [{"name": "test", "input_schema": {"type": "object"}}],
        }
    else:
        payload = {"input": ["text"]}
    if protocol != "embeddings":
        payload["stream"] = stream
    body = request(payload, purpose="embedding" if protocol == "embeddings" else "chat")
    response = await client.post("/api/models/requests", headers=headers(device), json=body)
    assert response.status_code == 200, response.text
    assert "Hello" in response.text or protocol == "embeddings"
    sent = supplier.calls[0][1]
    assert sent["model"] == "native"
    if protocol != "embeddings":
        assert sent["tools"] == payload["tools"]
        assert "image" in json.dumps(sent)
    status = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert status["state"] == "settled", status
    attempt = status["attempts"][0]
    assert attempt["usage"] == (
        {"uncached_input": 10, "cache_read": 0, "cache_write": 0, "output": 0}
        if protocol == "embeddings"
        else {"uncached_input": 5, "cache_read": 3, "cache_write": 2, "output": 4}
    )
    assert attempt["cost_nanos"] == (30 if protocol == "embeddings" else 58)
    replay = await client.post("/api/models/requests", headers=headers(device), json=body)
    assert replay.status_code == 202 and replay.json()["state"] == "settled"
    assert len(supplier.calls) == 1
    changed = {**body, "payload": {**body["payload"], "temperature": 0.5}}
    assert (
        await client.post("/api/models/requests", headers=headers(device), json=changed)
    ).status_code == 409
    budgets = (await client.get("/api/usage/budgets", headers=headers(device))).json()
    assert budgets[0]["reserved_nanos"] == 0
    assert budgets[0]["spent_nanos"] == attempt["cost_nanos"]
    summary = (await client.get("/api/usage/summary", headers=headers(device))).json()
    assert summary[0]["cost_nanos"] == attempt["cost_nanos"]
    assert summary[0]["settled_attempts"] == 1


@pytest.mark.parametrize("platform", ["user"], indirect=True)
async def test_atomic_concurrent_budget_and_idempotent_inflight_replay(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    await configure(budget=4800)
    supplier.mode = "hang"
    body = request({"messages": [{"role": "user", "content": "wait"}], "stream": True})
    gateway = app.state.cloud.models
    exchange = await gateway.open(device, ModelRequest.model_validate(body))
    assert not hasattr(exchange, "state")
    await asyncio.wait_for(supplier.streams[0].started.wait(), 5)
    replay, other = await asyncio.gather(
        client.post("/api/models/requests", headers=headers(device), json=body),
        client.post("/api/models/requests", headers=headers(device), json=request()),
    )
    assert replay.status_code == 202 and replay.json()["state"] == "running"
    assert other.status_code == 403 and other.json()["error"]["code"] == "TOKEN_QUOTA_EXCEEDED"
    assert len(supplier.calls) == 1
    await exchange.close()
    assert supplier.streams[0].closed.is_set()
    state = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert state["state"] == "reconciliation_required"
    budget = (await client.get("/api/usage/budgets", headers=headers(device))).json()[0]
    assert budget["reserved_nanos"] == 4800 and budget["spent_nanos"] == 0


async def test_failover_only_confirmed_free_attempts_and_reconciliation_idempotency(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    await configure(definition(fallback=True))
    body = request()
    assert (
        await client.post("/api/models/requests", headers=headers(device), json=body)
    ).status_code == 200
    status = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert [a["state"] for a in status["attempts"]] == ["released", "settled"]
    assert len({r.headers["x-octop-attempt-id"] for r, _ in supplier.calls}) == 2
    assert (await client.get("/api/usage/summary", headers=headers(device))).json()[0][
        "cost_nanos"
    ] == 58
    await configure()
    supplier.mode = "unknown-http"
    body = request()
    assert (
        await client.post("/api/models/requests", headers=headers(device), json=body)
    ).status_code == 503
    status = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert status["state"] == "reconciliation_required"
    attempt = status["attempts"][0]["attempt_id"]
    proof = {
        "usage": {"uncached_input": 10, "cache_read": 0, "cache_write": 0, "output": 2},
        "evidence": "supplier receipt ref-123",
    }
    assert (
        await client.post(
            f"/api/model-admin/attempts/{attempt}/reconcile", headers=headers(device), json=proof
        )
    ).status_code == 401
    for _ in range(2):
        assert (
            await client.post(
                f"/api/model-admin/attempts/{attempt}/reconcile",
                headers=headers(account),
                json=proof,
            )
        ).status_code == 204
    assert (await client.get("/api/usage/summary", headers=headers(device))).json()[0][
        "cost_nanos"
    ] == 104
    assert (
        await client.post(
            f"/api/model-admin/attempts/{attempt}/reconcile",
            headers=headers(account),
            json={**proof, "usage": None},
        )
    ).status_code == 409
    status = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert status["attempts"][0]["evidence"] == proof["evidence"]
    assert status["attempts"][0]["resolved_by"] == grant["session"]["identity"]["user"]["user_id"]


@pytest.mark.parametrize("mode", ["broken", "missing-usage", "sse-error"])
async def test_incomplete_stream_is_held_for_review(platform, mode):
    app, client, account, device, supplier, configure, request, grant = platform
    supplier.mode = mode
    body = request({"messages": [{"role": "user", "content": "hello"}], "stream": True})
    response = await client.post("/api/models/requests", headers=headers(device), json=body)
    assert response.status_code == 200
    assert "supplier-private-key" not in response.text
    status = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert status["state"] == "reconciliation_required"
    assert (await client.get("/api/usage/budgets", headers=headers(device))).json()[0][
        "reserved_nanos"
    ] == 4800
    proof = {"usage": None, "evidence": "provider confirms request not charged"}
    attempt = status["attempts"][0]["attempt_id"]
    assert (
        await client.post(
            f"/api/model-admin/attempts/{attempt}/reconcile", headers=headers(account), json=proof
        )
    ).status_code == 204
    assert (await client.get("/api/usage/budgets", headers=headers(device))).json()[0][
        "reserved_nanos"
    ] == 0


async def test_live_revoke_cross_user_context_and_requests_are_denied(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    other = await client.post(
        "/api/users",
        headers=headers(account),
        json={"username": "other", "password": identity_tests.PASSWORD},
    )
    other_id = other.json()["user_id"]
    other_account = (await identity_tests.login(client, "other"))["session"]["access_token"]
    other_device = (await identity_tests.bind(client, other_account))["session"]["access_token"]
    body = request()
    assert (
        await client.post("/api/models/requests", headers=headers(device), json=body)
    ).status_code == 200
    context = {k: body[k] for k in ["agent_id", "thread_id", "turn_id"]}
    assert (
        await client.post("/api/model-contexts", headers=headers(other_device), json=context)
    ).status_code == 403
    assert (
        await client.get(
            "/api/models/requests/" + body["request_id"], headers=headers(other_device)
        )
    ).status_code == 404
    assert (
        await client.post("/api/models/requests", headers=headers(other_device), json=body)
    ).status_code == 404
    assert (
        await client.get(
            "/api/usage/summary", headers=headers(other_device), params={"user_id": other_id}
        )
    ).status_code == 401
    assert (
        await client.get(
            "/api/usage/summary", headers=headers(other_account), params={"user_id": 1}
        )
    ).status_code == 403
    assert (
        await client.get(
            "/api/usage/requests",
            headers=headers(account),
            params={"user_id": grant["session"]["identity"]["user"]["user_id"]},
        )
    ).status_code == 200
    await identity_tests.bind(client, account, replace=True)
    assert (
        await client.post("/api/models/requests", headers=headers(device), json=request())
    ).status_code in {401, 403}
    assert len(supplier.calls) == 1


async def test_worker_crash_recovery_does_not_release_unknown_consumption(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    ledger = app.state.cloud.models.ledger
    body = ModelRequest.model_validate(request())
    _, attempt, _ = await offload(ledger.reserve, device, body)
    await offload(ledger.begin_attempt, device, body.request_id, attempt, 0)
    with app.state.cloud.db.transaction() as conn:
        conn.execute(
            "UPDATE cloud_model_requests SET lease_until=0 WHERE request_id=?", (body.request_id,)
        )
    await offload(ledger.recover_stale)
    assert (
        await client.get("/api/models/requests/" + body.request_id, headers=headers(device))
    ).json()["state"] == "reconciliation_required"
    untouched = ModelRequest.model_validate(request())
    await offload(ledger.reserve, device, untouched)
    with app.state.cloud.db.transaction() as conn:
        conn.execute(
            "UPDATE cloud_model_requests SET lease_until=0 WHERE request_id=?",
            (untouched.request_id,),
        )
    await offload(ledger.recover_stale)
    assert (
        await client.get("/api/models/requests/" + untouched.request_id, headers=headers(device))
    ).json()["state"] == "released"
    assert (await client.get("/api/usage/budgets", headers=headers(device))).json()[0][
        "reserved_nanos"
    ] == 4800


@asynccontextmanager
async def serve(app, *, lifespan="off"):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan=lifespan))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
async def test_real_local_harness_model_tool_model_and_auxiliary_use_gateway(
    platform, tmp_path, protocol
):
    cloud_app, client, account, device, supplier, configure, request, grant = platform
    supplier.mode = "tool"
    large = definition(protocol)
    large["input_token_ceiling"] = 128000
    await configure(large)
    await configure(definition("embeddings", "embedding"))
    async with serve(cloud_app) as cloud_url:
        local = create_local_app(
            LocalSettings(
                home=tmp_path / "local",
                cloud_url=cloud_url,
                api_token=LOCAL_TOKEN,
                enable_api_docs=True,
            )
        )
        async with (
            serve(local, lifespan="on") as local_url,
            httpx.AsyncClient(base_url=local_url, headers=LOCAL_HEADERS) as desktop,
        ):
            response = await desktop.put("/api/cloud/session", json=grant["session"])
            assert response.status_code == 200, response.text
            catalog = (await desktop.get("/api/models")).json()
            assert len(catalog) == 2
            assert "supplier-private-key" not in json.dumps(catalog)
            created = await desktop.post(
                "/api/agents", json={"name": "Reader", "model_id": "platform/test"}
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["agent_id"]
            thread_id = (await desktop.post(f"/api/agents/{agent_id}/threads")).json()["thread_id"]
            runtime = local.state.accounts.runtime
            agent = await runtime._agent(agent_id)
            await agent.workspace.aupload_bytes("note.txt", b"local-file-read-by-tool")
            events = []
            async with ws_connect(
                local, f"/api/agents/{agent_id}/threads/{thread_id}/ws", headers=LOCAL_HEADERS
            ) as ws:
                await ws.send_json({"type": "start", "text": "read note.txt"})
                while True:
                    event = await ws.receive_json()
                    assert event["type"] != "error", event
                    events.append(event)
                    if event["type"] == "turn.status":
                        break
            assert events[-1]["data"]["status"] == "completed", events
            assert any(e["type"] == "tool_result" for e in events)
            assert "local-file-read-by-tool" in str(events)
            assert len(supplier.calls) == 2
            costs = (await client.get("/api/usage/requests", headers=headers(device))).json()
            assert len(costs) == 2 and all(r["state"] == "settled" for r in costs)
            assert len({r["request_id"] for r in costs}) == 2
            assert all(
                r["turn_id"] == events[0]["turn_id"]
                and r["thread_id"] == thread_id
                and r["agent_id"] == agent_id
                for r in costs
            )
            assert all(
                r.headers.get("authorization") == "Bearer supplier-private-key"
                or r.headers.get("x-api-key") == "supplier-private-key"
                for r, _ in supplier.calls
            )
            # Only the private account-scoped relay token can access SDK routes.
            assert (
                await desktop.post("/api/platform/chat/completions", json={})
            ).status_code == 401
            context = {
                "agent_id": agent_id,
                "thread_id": thread_id,
                "turn_id": events[0]["turn_id"],
            }
            embed = await desktop.post(
                "/api/models/auxiliary",
                json={
                    "context": context,
                    "purpose": "embedding",
                    "model_id": "platform/embedding",
                    "payload": {"input": ["local text"]},
                },
            )
            assert embed.status_code == 200, embed.text
            assert embed.json()["data"][0]["embedding"] == [0.1, 0.2]
            desktop_costs = (await desktop.get("/api/usage/requests")).json()
            assert len(desktop_costs) == 3
            assert all(
                r["user_id"] == grant["session"]["identity"]["user"]["user_id"]
                for r in desktop_costs
            )
            assert (await desktop.get("/api/usage/budgets")).json()[0]["spent_nanos"] == 146
            assert (
                sum(r["cost_nanos"] for r in (await desktop.get("/api/usage/summary")).json())
                == 146
            )
            assert (await desktop.delete("/api/cloud/session")).status_code == 204
            assert (await desktop.get("/api/models")).status_code == 401


async def test_unknown_http_never_fails_over_or_releases_without_evidence(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    model = definition(fallback=True)
    model["routes"].reverse()
    await configure(model)
    supplier.mode = "unknown-http"
    body = request()
    response = await client.post("/api/models/requests", headers=headers(device), json=body)
    assert response.status_code == 503
    assert "never forward this secret" not in response.text
    assert len(supplier.calls) == 1
    status = (
        await client.get("/api/models/requests/" + body["request_id"], headers=headers(device))
    ).json()
    assert status["state"] == "reconciliation_required"
    assert len(status["attempts"]) == 1
    limit = await client.put(
        "/api/model-admin/users/1/budget",
        headers=headers(account),
        json={"currency": "USD", "limit_nanos": 1},
    )
    assert limit.status_code == 409


async def test_rate_snapshot_survives_catalog_change_and_excess_usage_requires_review(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    ledger = app.state.cloud.models.ledger
    body = ModelRequest.model_validate(request())
    _, attempt, _ = await offload(ledger.reserve, device, body)
    changed = definition()
    changed["rate"].update(revision="rate-second", output=18)
    await configure(changed)
    await offload(ledger.begin_attempt, device, body.request_id, attempt, 0)
    from octop.infra.models.contracts import Usage

    usage = Usage(uncached_input=5, cache_read=3, cache_write=2, output=4)
    await offload(ledger.finish, body.request_id, attempt, usage=usage)
    await offload(ledger.finish, body.request_id, attempt, usage=usage)
    record = (
        await client.get("/api/models/requests/" + body.request_id, headers=headers(device))
    ).json()
    assert record["rate_revision"] == "rate-test"
    assert record["attempts"][0]["cost_nanos"] == 58
    assert (await client.get("/api/usage/summary", headers=headers(device))).json()[0][
        "settled_attempts"
    ] == 1
    excess = ModelRequest.model_validate(request())
    _, attempt, _ = await offload(ledger.reserve, device, excess)
    await offload(ledger.begin_attempt, device, excess.request_id, attempt, 0)
    await offload(
        ledger.finish, excess.request_id, attempt, usage=usage.model_copy(update={"output": 101})
    )
    record = (
        await client.get("/api/models/requests/" + excess.request_id, headers=headers(device))
    ).json()
    assert record["state"] == "reconciliation_required"
    assert record["attempts"][0]["reason"] == "provider_exceeded_configured_ceiling"


async def test_cloud_http_disconnect_preserves_reservation_and_cancels_provider(platform):
    app, client, account, device, supplier, configure, request, grant = platform
    supplier.mode = "hang"
    body = request({"messages": [{"role": "user", "content": "hello"}], "stream": True})
    async with (
        serve(app) as url,
        httpx.AsyncClient(base_url=url, headers=headers(device)) as network,
    ):
        async with network.stream("POST", "/api/models/requests", json=body) as response:
            assert response.status_code == 200
            assert await anext(response.aiter_bytes())
        async with asyncio.timeout(5):
            while True:
                record = (
                    await client.get(
                        "/api/models/requests/" + body["request_id"], headers=headers(device)
                    )
                ).json()
                if record["state"] == "reconciliation_required":
                    break
                await asyncio.sleep(0.01)
    assert supplier.streams[0].closed.is_set()
    budget = (await client.get("/api/usage/budgets", headers=headers(device))).json()[0]
    assert budget["reserved_nanos"] == 4800 and budget["spent_nanos"] == 0


async def test_desktop_logout_cancels_real_platform_model_stream(platform, tmp_path):
    cloud_app, client, account, device, supplier, configure, request, grant = platform
    supplier.mode = "hang"
    large = definition()
    large["input_token_ceiling"] = 128000
    await configure(large)
    async with serve(cloud_app) as cloud_url:
        local = create_local_app(
            LocalSettings(home=tmp_path / "local", cloud_url=cloud_url, api_token=LOCAL_TOKEN)
        )
        async with (
            serve(local, lifespan="on") as local_url,
            httpx.AsyncClient(base_url=local_url, headers=LOCAL_HEADERS) as desktop,
        ):
            assert (
                await desktop.put("/api/cloud/session", json=grant["session"])
            ).status_code == 200
            agent_id = (
                await desktop.post(
                    "/api/agents", json={"name": "Wait", "model_id": "platform/test"}
                )
            ).json()["agent_id"]
            thread_id = (await desktop.post(f"/api/agents/{agent_id}/threads")).json()["thread_id"]
            runtime = local.state.accounts.runtime
            async with ws_connect(
                local, f"/api/agents/{agent_id}/threads/{thread_id}/ws", headers=LOCAL_HEADERS
            ) as ws:
                await ws.send_json({"type": "start", "text": "wait for model"})
                assert (await ws.receive_json())["type"] == "turn.started"
                async with asyncio.timeout(10):
                    while not supplier.streams:
                        await asyncio.sleep(0.01)
                await supplier.streams[0].started.wait()
                response = await desktop.delete("/api/cloud/session")
                assert response.status_code == 204, response.text
                async with asyncio.timeout(10):
                    while True:
                        event = await ws.receive_json()
                        if event["type"] == "turn.status":
                            assert event["data"]["status"] == "interrupted"
                            break
            assert not runtime._active
            assert runtime.services is None
            async with asyncio.timeout(5):
                while not supplier.streams[0].closed.is_set():
                    await asyncio.sleep(0.01)
            states = (await client.get("/api/usage/requests", headers=headers(device))).json()
            assert len(states) == 1 and states[0]["state"] == "reconciliation_required"


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
@pytest.mark.parametrize("platform", ["user"], indirect=True)
async def test_platform_quota_rejection_reaches_desktop_with_its_error_code(
    platform, tmp_path, protocol
):
    cloud_app, client, account, device, supplier, configure, request, grant = platform
    model = definition(protocol)
    model["input_token_ceiling"] = 128000
    await configure(model, budget=0)
    async with serve(cloud_app) as cloud_url:
        local = create_local_app(
            LocalSettings(home=tmp_path / "local", cloud_url=cloud_url, api_token=LOCAL_TOKEN)
        )
        async with (
            serve(local, lifespan="on") as local_url,
            httpx.AsyncClient(base_url=local_url, headers=LOCAL_HEADERS) as desktop,
        ):
            assert (
                await desktop.put("/api/cloud/session", json=grant["session"])
            ).status_code == 200
            agent_id = (
                await desktop.post(
                    "/api/agents", json={"name": "Limited", "model_id": "platform/test"}
                )
            ).json()["agent_id"]
            thread_id = (await desktop.post(f"/api/agents/{agent_id}/threads")).json()["thread_id"]
            async with ws_connect(
                local, f"/api/agents/{agent_id}/threads/{thread_id}/ws", headers=LOCAL_HEADERS
            ) as ws:
                await ws.send_json({"type": "start", "text": "hello"})
                async with asyncio.timeout(10):
                    while True:
                        event = await ws.receive_json()
                        if event["type"] == "error":
                            assert event["error"]["code"] == "TOKEN_QUOTA_EXCEEDED", event
                            break
            assert not supplier.calls
