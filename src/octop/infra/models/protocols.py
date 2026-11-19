"""Native wire validation and provider usage normalization, without an Agent SDK.

OpenAI input includes cache subcategories; Anthropic input excludes them.
Reasoning is already included in output. Only text/image/function tools are
advertised. Hosted tools, stateful Responses and non-token charges are refused.
"""

from __future__ import annotations

import json
from typing import Any

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.models.contracts import ModelDefinition, ModelRequest, Protocol, Usage

PATHS: dict[Protocol, str] = {
    "chat_completions": "/chat/completions",
    "responses": "/responses",
    "messages": "/messages",
    "embeddings": "/embeddings",
}
COMMON = {"model", "stream", "temperature", "top_p", "tools", "tool_choice", "stop", "metadata"}
ALLOWED = {
    "chat_completions": COMMON
    | {
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "stream_options",
        "parallel_tool_calls",
        "response_format",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "reasoning_effort",
    },
    "responses": COMMON
    | {
        "input",
        "instructions",
        "max_output_tokens",
        "parallel_tool_calls",
        "text",
        "reasoning",
        "include",
        "store",
    },
    "messages": COMMON | {"messages", "system", "max_tokens", "top_k", "thinking", "cache_control"},
    "embeddings": {"model", "input", "dimensions", "encoding_format"},
}


def invalid() -> OctopError:
    return OctopError.localized(ErrorCode.MODEL_REQUEST_INVALID)


def _walk(value: Any, model: ModelDefinition) -> None:
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in {"image", "image_url", "input_image"} and "image" not in model.input_modalities:
            raise invalid()
        if kind in {"input_audio", "audio", "file", "input_file", "document", "video"}:
            raise invalid()
        if "cache_control" in value:
            cache = value["cache_control"]
            if not isinstance(cache, dict) or cache.get("ttl", "5m") != "5m":
                raise invalid()
        for child in value.values():
            _walk(child, model)
    elif isinstance(value, list):
        for child in value:
            _walk(child, model)


def validate_payload(model: ModelDefinition, request: ModelRequest) -> None:
    body = request.payload
    if set(body) - ALLOWED[model.protocol] or body.get("model", model.model_id) != model.model_id:
        raise invalid()
    if not isinstance(body.get("stream", False), bool):
        raise invalid()
    if (
        request.purpose == "embedding"
        and model.protocol != "embeddings"
        or model.protocol == "embeddings"
        and request.purpose != "embedding"
    ):
        raise invalid()
    if model.protocol == "responses" and body.get("store", False) is not False:
        raise invalid()
    if model.protocol in {"chat_completions", "messages"} and not isinstance(
        body.get("messages"), list
    ):
        raise invalid()
    if model.protocol in {"responses", "embeddings"} and not isinstance(
        body.get("input"), (str, list)
    ):
        raise invalid()
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if key in body and (
            type(body[key]) is not int or not 0 < body[key] <= model.output_token_ceiling
        ):
            raise invalid()
    tools = body.get("tools", [])
    if not isinstance(tools, list) or (tools and not model.tools):
        raise invalid()
    for tool in tools:
        if not isinstance(tool, dict):
            raise invalid()
        if model.protocol == "messages":
            if tool.get("type", "custom") != "custom" or "input_schema" not in tool:
                raise invalid()
        elif tool.get("type") != "function":
            raise invalid()
    _walk(body, model)


def upstream_payload(
    model: ModelDefinition, request: ModelRequest, upstream_model: str
) -> dict[str, Any]:
    body = {**request.payload, "model": upstream_model}
    if model.protocol == "chat_completions":
        key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
        body.pop("max_tokens" if key == "max_completion_tokens" else "max_completion_tokens", None)
        body.setdefault(key, model.output_token_ceiling)
        if body.get("stream"):
            body["stream_options"] = {"include_usage": True}
    elif model.protocol == "responses":
        body.setdefault("max_output_tokens", model.output_token_ceiling)
        body["store"] = False
    elif model.protocol == "messages":
        body.setdefault("max_tokens", model.output_token_ceiling)
    return body


def count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 10**9:
        raise ValueError("invalid usage count")
    return value


def normalize(protocol: Protocol, raw: dict[str, Any]) -> Usage:
    if protocol == "messages":
        return Usage(
            uncached_input=count(raw["input_tokens"]),
            cache_read=count(raw.get("cache_read_input_tokens", 0)),
            cache_write=count(raw.get("cache_creation_input_tokens", 0)),
            output=count(raw["output_tokens"]),
        )
    input_key = "input_tokens" if protocol == "responses" else "prompt_tokens"
    output_key = "output_tokens" if protocol == "responses" else "completion_tokens"
    details = (
        raw.get("input_tokens_details" if protocol == "responses" else "prompt_tokens_details")
        or {}
    )
    cached = count(details.get("cached_tokens", 0))
    written = count(details.get("cache_write_tokens", 0))
    uncached = count(count(raw[input_key]) - cached - written)
    return Usage(
        uncached_input=uncached,
        cache_read=cached,
        cache_write=written,
        output=0 if protocol == "embeddings" else count(raw[output_key]),
    )


class UsageCapture:
    """Cumulative final usage; partial stream counters never imply finality."""

    def __init__(self, protocol: Protocol) -> None:
        self.protocol = protocol
        self.usage: Usage | None = None
        self.complete = False
        self.failed = False
        self._buffer = ""
        self._anthropic: dict[str, Any] = {}
        self._finished_choice = False

    def json(self, body: dict[str, Any]) -> None:
        try:
            self.usage = normalize(self.protocol, body["usage"])
            self.complete = not body.get("error") and (
                self.protocol == "embeddings"
                and isinstance(body.get("data"), list)
                or self.protocol == "messages"
                and body.get("stop_reason") is not None
                or self.protocol == "responses"
                and body.get("status") in {"completed", "incomplete"}
                or self.protocol == "chat_completions"
                and bool(body.get("choices"))
                and all(c.get("finish_reason") is not None for c in body["choices"])
            )
        except (KeyError, TypeError, ValueError):
            self.failed = True

    def feed(self, chunk: str) -> list[str]:
        frames: list[str] = []
        self._buffer = (self._buffer + chunk).replace("\r\n", "\n")
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            if len(frame) > 2 * 1024 * 1024:
                raise ValueError("oversized SSE frame")
            data = "\n".join(
                line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")
            )
            if not data:
                frames.append(frame + "\n\n")
                continue
            if data == "[DONE]":
                frames.append(frame + "\n\n")
                if self.protocol == "chat_completions":
                    self.complete = (
                        self._finished_choice and self.usage is not None and not self.failed
                    )
                continue
            try:
                self.event(json.loads(data))
            except (KeyError, TypeError, ValueError):
                self.failed = True
                self.complete = False
            if not self.failed:
                frames.append(frame + "\n\n")

        if len(self._buffer) > 2 * 1024 * 1024:
            raise ValueError("oversized SSE frame")
        return frames

    def event(self, data: dict[str, Any]) -> None:
        if data.get("error") or data.get("type") in {"error", "response.failed", "response.error"}:
            self.failed = True
            self.complete = False
            return
        if self.protocol == "chat_completions":
            if data.get("usage"):
                self.usage = normalize(self.protocol, data["usage"])
            if any(c.get("finish_reason") is not None for c in data.get("choices", [])):
                self._finished_choice = True
        elif self.protocol == "responses" and data.get("type") in {
            "response.completed",
            "response.incomplete",
        }:
            self.json(data["response"])
        elif self.protocol == "messages":
            kind = data.get("type")
            if kind == "message_start":
                self._anthropic.update(data["message"].get("usage", {}))
            elif kind == "message_delta":
                self._anthropic.update(data.get("usage", {}))
                if data.get("delta", {}).get("stop_reason") is not None:
                    self.usage = normalize(self.protocol, self._anthropic)
            elif kind == "message_stop":
                self.complete = self.usage is not None and not self.failed

    def final_usage(self) -> Usage | None:
        return self.usage if self.complete and not self.failed else None
