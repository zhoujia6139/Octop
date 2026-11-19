"""Provider I/O has its own task lifetime; HTTP disconnect cannot discard billing."""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from functools import partial
from typing import Any

import httpx

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.models.contracts import ModelRequest, RequestView
from octop.infra.models.ledger import ModelLedger
from octop.infra.models.protocols import PATHS, UsageCapture, upstream_payload


async def offload[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return await asyncio.get_running_loop().run_in_executor(
        None, partial(function, *args, **kwargs)
    )


async def committed[T](future: asyncio.Future[T]) -> T:
    """Wait for a DB transition even when shutdown and disconnect both cancel.

    The caller's cancellation still propagates from its surrounding handler;
    cleanup cannot abandon a run_in_executor transaction mid-commit.
    """
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


class Exchange:
    def __init__(self) -> None:
        self.ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue(16)
        self.task: asyncio.Task[None] | None = None

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            while True:
                item = await self.queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            await self.close()

    async def close(self) -> None:
        if self.task is not None:
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class ModelGateway:
    def __init__(
        self, ledger: ModelLedger, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.ledger = ledger
        self.client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(180, connect=10),
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._maintenance: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        await offload(self.ledger.recover_stale)
        self._maintenance = asyncio.create_task(self._sweep())

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(30)
            await offload(self.ledger.recover_stale)

    async def _heartbeat(self, request_id: str) -> None:
        while True:
            await asyncio.sleep(30)
            await offload(self.ledger.heartbeat, request_id)

    async def open(self, token: str, body: ModelRequest) -> Exchange | RequestView:
        if self._closed:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        # Cancellation must not orphan a reservation whose executor is still
        # committing. Await the result before deciding whether to release it.
        reserved = asyncio.create_task(offload(self.ledger.reserve, token, body))
        try:
            model, attempt_id, replay = await asyncio.shield(reserved)
        except asyncio.CancelledError:
            _, attempt_id, _ = await committed(reserved)
            if attempt_id:
                await committed(
                    asyncio.create_task(
                        offload(
                            self.ledger.finish,
                            body.request_id,
                            attempt_id,
                            usage=None,
                            known_free=True,
                            reason="cancelled_before_dispatch",
                        )
                    )
                )
            raise
        if replay is not None:
            return replay
        assert attempt_id is not None
        exchange = Exchange()
        task = asyncio.create_task(self._run(exchange, token, body, model, attempt_id))
        exchange.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            await exchange.ready
        except BaseException:
            await exchange.close()
            raise
        return exchange

    async def _run(
        self, exchange: Exchange, token: str, body: ModelRequest, model: Any, attempt_id: str
    ) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(body.request_id))
        capture = UsageCapture(model.protocol)
        started = False
        finalized = False
        reason = "unknown_result"
        try:
            for index, route in enumerate(model.routes):
                if index:
                    next_reservation = asyncio.create_task(
                        offload(self.ledger.next_attempt, token, body.request_id, route.provider_id)
                    )
                    try:
                        attempt_id = await asyncio.shield(next_reservation)
                    except asyncio.CancelledError:
                        attempt_id = await committed(next_reservation)
                        raise
                # Shield a running marker: after cancellation we must assume
                # dispatch may have happened rather than release a stale hold.
                beginning = asyncio.create_task(
                    offload(self.ledger.begin_attempt, token, body.request_id, attempt_id, index)
                )
                try:
                    provider = await asyncio.shield(beginning)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await committed(beginning)
                        started = True
                    raise
                started = True
                headers = {"Content-Type": "application/json", "X-Octop-Attempt-Id": attempt_id}
                if model.protocol == "messages":
                    headers.update(
                        {"x-api-key": provider["api_key"], "anthropic-version": "2023-06-01"}
                    )
                else:
                    headers["Authorization"] = "Bearer " + provider["api_key"]
                payload = upstream_payload(model, body, route.upstream_model)
                async with self.client.stream(
                    "POST",
                    provider["base_url"] + PATHS[model.protocol],
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 300:
                        known_free = response.status_code in json.loads(provider["free_statuses"])
                        await offload(
                            self.ledger.finish,
                            body.request_id,
                            attempt_id,
                            usage=None,
                            known_free=known_free,
                            reason=f"upstream_http_{response.status_code}",
                        )
                        finalized = True
                        if known_free and index + 1 < len(model.routes):
                            finalized, started = False, False
                            continue
                        raise OctopError.localized(ErrorCode.MODEL_UPSTREAM_ERROR)
                    streamed = bool(body.payload.get("stream", False))
                    content_type = response.headers.get("content-type", "")
                    if (
                        streamed
                        and "text/event-stream" not in content_type
                        or not streamed
                        and "json" not in content_type
                    ):
                        raise ValueError("unexpected provider content type")
                    if not exchange.ready.done():
                        exchange.ready.set_result(streamed)
                    if streamed:
                        decoder = codecs.getincrementaldecoder("utf-8")()
                        async for chunk in response.aiter_bytes():
                            # Buffer one complete native SSE frame before
                            # forwarding: a split provider error must not leak
                            # diagnostic content before it can be recognized.
                            frames = capture.feed(decoder.decode(chunk))
                            if capture.failed:
                                raise OctopError.localized(ErrorCode.MODEL_UPSTREAM_ERROR)
                            for frame in frames:
                                await exchange.queue.put(frame.encode())
                        for frame in capture.feed(decoder.decode(b"", final=True)):
                            await exchange.queue.put(frame.encode())
                    else:
                        output = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(output) + len(chunk) > 16 * 1024 * 1024:
                                raise ValueError("oversized provider response")
                            output.extend(chunk)
                        result = json.loads(output)
                        if not isinstance(result, dict) or result.get("error"):
                            raise OctopError.localized(ErrorCode.MODEL_UPSTREAM_ERROR)
                        capture.json(result)
                        await exchange.queue.put(bytes(output))
                    reason = (
                        "completed" if capture.final_usage() is not None else "missing_final_usage"
                    )
                    break
        except asyncio.CancelledError:
            reason = "cancelled_or_disconnected"
            raise
        except BaseException:
            error = OctopError.localized(ErrorCode.MODEL_UPSTREAM_ERROR)
            if not exchange.ready.done():
                exchange.ready.set_exception(error)
            else:
                await exchange.queue.put(error)
        finally:
            heartbeat.cancel()
            await committed(asyncio.gather(heartbeat, return_exceptions=True))
            if not finalized:
                finalizer = asyncio.create_task(
                    offload(
                        self.ledger.finish,
                        body.request_id,
                        attempt_id,
                        usage=capture.final_usage(),
                        known_free=not started,
                        reason=reason,
                    )
                )
                await committed(finalizer)
            # A cancelled consumer may leave a full queue. Do not block durable
            # accounting on delivering its terminal sentinel.
            if not exchange.ready.done():
                exchange.ready.set_exception(OctopError.localized(ErrorCode.MODEL_UPSTREAM_ERROR))
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                with suppress(asyncio.QueueFull):
                    exchange.queue.put_nowait(None)
            else:
                await exchange.queue.put(None)

    async def close(self) -> None:
        self._closed = True
        if self._maintenance:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
