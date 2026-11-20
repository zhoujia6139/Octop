"""Manual live maintenance admission, completion, failure and local transport."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from octop.infra.agents.manager import AgentManager
from octop.infra.agents.memory.slim import MemorySlimCoordinator
from octop.infra.agents.memory.slim_control import MemorySlimControl, request_memory_slim


def make_manager(tmp_path):
    from octop_memory import Memory

    path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(path)
    old = SqliteSaver(conn)
    cp = empty_checkpoint()
    cp["channel_values"] = {"memory_contents": {"USER.md": "keep" * 1000}}
    cfg = old.put({"configurable": {"thread_id": "t", "checkpoint_ns": ""}}, cp, {}, {})
    conn.close()
    memory = Memory("test_slim", backend_config={"db_path": str(path)})
    agent = SimpleNamespace(_memory_runtime=SimpleNamespace(memory=memory))
    registry = AgentManager.__new__(AgentManager)
    registry._active_invocations = {}
    registry._invocation_waiters = {}
    registry._history_backfills = {}
    registry._reclaim_pending = set()
    registry._reclaim_holds = {}
    registry._reclaim_task = None
    registry._reclaim_sweep_task = None
    registry._reclaim_wake = None
    registry.get_agent = lambda _: agent
    registry.memory_slim = MemorySlimCoordinator(registry)
    registry._lock = asyncio.Lock()
    registry._harness_manager = None
    registry._host_models = MagicMock()
    return registry.memory_slim, registry, memory, cfg, cp


@pytest.mark.asyncio
async def test_live_job_waits_for_turn_and_releases_gate(tmp_path, monkeypatch):
    from octop_memory.application import checkpoint_maintenance

    coordinator, registry, memory, cfg, cp = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    actual = checkpoint_maintenance.slim_live_checkpoints

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(*args, **kwargs)

    monkeypatch.setattr(checkpoint_maintenance, "slim_live_checkpoints", slow)
    await registry._begin_invocation("a")
    coordinator.start("a")
    await asyncio.sleep(0.05)
    assert coordinator.state["phase"] == "waiting"
    assert not entered.is_set()
    registry._end_invocation("a")
    assert await asyncio.to_thread(entered.wait, 5)
    waiter = asyncio.create_task(registry._begin_invocation("a"))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    release.set()
    await coordinator.task
    await waiter
    registry._end_invocation("a")
    assert coordinator.state["phase"] == "done"
    assert memory.get_tuple(cfg).checkpoint == cp
    memory._checkpointer.conn.close()
    memory.backend.close()


@pytest.mark.asyncio
async def test_live_failure_releases_gate_and_keeps_error(tmp_path, monkeypatch):
    from octop_memory.application import checkpoint_maintenance

    coordinator, registry, memory, _, _ = make_manager(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(checkpoint_maintenance, "slim_live_checkpoints", fail)
    coordinator.start("a")
    await coordinator.task
    assert coordinator.state["phase"] == "failed"
    assert "disk full" in coordinator.state["error"]
    assert not registry._history_backfills
    await asyncio.wait_for(registry._begin_invocation("a"), timeout=1)
    registry._end_invocation("a")
    memory._checkpointer.conn.close()
    memory.backend.close()


@pytest.mark.asyncio
async def test_cancellation_does_not_release_gate_before_worker_finishes(tmp_path, monkeypatch):
    from octop_memory.application import checkpoint_maintenance

    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(checkpoint_maintenance, "slim_live_checkpoints", slow)
    coordinator.start("a")
    assert await asyncio.to_thread(entered.wait, 5)
    coordinator.task.cancel()
    await asyncio.sleep(0.05)
    assert "a" in registry._history_backfills
    assert not coordinator.task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await coordinator.task
    assert not registry._history_backfills
    memory._checkpointer.conn.close()
    memory.backend.close()


@pytest.mark.asyncio
async def test_manager_shutdown_waits_for_maintenance_before_closing_agents(tmp_path, monkeypatch):
    from octop_memory.application import checkpoint_maintenance

    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {}

    def close_agents():
        assert coordinator.task.done()
        assert not registry._history_backfills

    harness_manager = MagicMock()
    harness_manager.aclose = AsyncMock(side_effect=close_agents)
    registry._harness_manager = harness_manager
    monkeypatch.setattr(checkpoint_maintenance, "slim_live_checkpoints", slow)
    coordinator.start("a")
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(registry.shutdown())
        await asyncio.sleep(0.05)
        assert not closing.done()
        harness_manager.aclose.assert_not_awaited()
        assert "a" in registry._history_backfills
        release.set()
        await closing
        harness_manager.aclose.assert_awaited_once()
        assert coordinator.state["phase"] == "done"
        with pytest.raises(ValueError):
            coordinator.start("a")
    finally:
        release.set()
        if closing is not None:
            await closing
        else:
            await registry.shutdown()
        memory._checkpointer.conn.close()
        memory.backend.close()


@pytest.mark.parametrize("phase", ["backing_up", "deduplicating", "compacting"])
def test_memory_dashboard_rejects_busy_before_opening_database(phase, monkeypatch):
    from octop.api.common import memory_client
    from octop.infra.errors import ErrorCode, OctopError

    monkeypatch.setattr(memory_client, "require_agent_owner_row", lambda *args, **kwargs: None)
    opened = []
    bridge = SimpleNamespace(handle=lambda _: {"result": "ok"})

    def open_memory(*args):
        opened.append(True)
        return None, bridge

    monkeypatch.setattr(memory_client, "_open_memory_for_agent", open_memory)
    current = {"phase": phase}
    host = SimpleNamespace(
        app_runtime=SimpleNamespace(
            agent_registry=SimpleNamespace(memory_slim=SimpleNamespace(status=lambda _: current))
        )
    )
    kwargs = {
        "agent_id": "a",
        "method": "stats_counts",
        "params": {},
        "user": None,
        "as_user": None,
        "server": host,
    }
    with pytest.raises(OctopError) as error:
        memory_client.call_memory_rpc(**kwargs)
    assert error.value.code == ErrorCode.AGENT_BUSY
    assert not opened
    current["phase"] = "failed"
    assert memory_client.call_memory_rpc(**kwargs) == "ok"


@pytest.mark.asyncio
async def test_local_control_authenticates_and_streams_completion(tmp_path):
    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    host = SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path),
        app_runtime=None,
    )
    control = MemorySlimControl(host)
    await control.start()
    try:
        # The listener starts even when first-run setup has not created a runtime yet.
        events = await asyncio.to_thread(lambda: list(request_memory_slim(tmp_path, "a")))
        assert events[-1]["phase"] == "failed"
        assert coordinator.task is None
        host.app_runtime = SimpleNamespace(agent_registry=registry)
        endpoint = json.loads((tmp_path / "memory-slim-control.json").read_text())
        reader, writer = await asyncio.open_connection("127.0.0.1", endpoint["port"])
        writer.write(b'{"token":"wrong","agent_id":"a"}\n')
        await writer.drain()
        assert json.loads(await reader.readline())["phase"] == "failed"
        assert coordinator.task is None
        writer.close()
        await writer.wait_closed()
        events = await asyncio.to_thread(lambda: list(request_memory_slim(tmp_path, "a")))
        assert events[-1]["phase"] == "done"
        assert events[-1]["report"]["stats"]["checkpoints_deleted"] == 0
    finally:
        await control.close()
        await registry.shutdown()
        memory._checkpointer.conn.close()
        memory.backend.close()
    assert not (tmp_path / "memory-slim-control.json").exists()


def test_live_discovery_filters_unavailable_agents_without_opening_memory(tmp_path):
    from octop.infra.errors import ErrorCode, OctopError

    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    compatible = registry.get_agent("a")
    registry.list_rows = lambda: [
        SimpleNamespace(agent_id=aid, name=aid) for aid in ["a", "stopped", "no_memory"]
    ]

    def get_agent(agent_id):
        if agent_id == "stopped":
            raise OctopError(ErrorCode.AGENT_NOT_RUNNING, "stopped")
        return compatible if agent_id == "a" else SimpleNamespace()

    registry.get_agent = get_agent
    try:
        assert coordinator.list_agents() == [{"agent_id": "a", "name": "a"}]
        assert coordinator.task is None
        assert not registry._history_backfills
    finally:
        memory._checkpointer.conn.close()
        memory.backend.close()


@pytest.mark.asyncio
async def test_control_discovery_and_heartbeat_during_unchanged_phase(tmp_path, monkeypatch):
    from octop_memory.application import checkpoint_maintenance

    from octop.infra.agents.memory.slim_control import list_memory_slim_agents

    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    registry.list_rows = lambda: [SimpleNamespace(agent_id="a", name="助手")]
    release = threading.Event()

    def slow(*args, **kwargs):
        assert release.wait(5)
        return {}

    monkeypatch.setattr(checkpoint_maintenance, "slim_live_checkpoints", slow)
    host = SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path),
        app_runtime=SimpleNamespace(agent_registry=registry),
    )
    control = MemorySlimControl(host)
    await control.start()
    try:
        agents = await asyncio.to_thread(list_memory_slim_agents, tmp_path)
        assert agents == [{"agent_id": "a", "name": "助手"}]
        assert coordinator.task is None
        endpoint = json.loads((tmp_path / "memory-slim-control.json").read_text())
        reader, writer = await asyncio.open_connection("127.0.0.1", endpoint["port"])
        writer.write((json.dumps({"token": endpoint["token"], "agent_id": "a"}) + "\n").encode())
        await writer.drain()
        events = []
        async with asyncio.timeout(4):
            while True:
                status = json.loads(await reader.readline())
                events.append(status)
                if status["phase"] == "backing_up" and status["elapsed_seconds"] >= 1:
                    break
        backing_up = [s for s in events if s["phase"] == "backing_up"]
        assert len(backing_up) >= 2
        assert backing_up[0]["elapsed_seconds"] < backing_up[-1]["elapsed_seconds"]
        assert all("percent" not in s for s in backing_up)
        writer.close()
        await writer.wait_closed()
        await control.close()
        assert not coordinator.task.done()
        assert "a" in registry._history_backfills
        release.set()
        await coordinator.task
        assert coordinator.state["phase"] == "done"
    finally:
        release.set()
        await control.close()
        await registry.shutdown()
        memory._checkpointer.conn.close()
        memory.backend.close()


def chat_registry(registry):
    rows = {
        aid: SimpleNamespace(agent_id=aid, user_id=uid, name=aid)
        for aid, uid in [("a", 1), ("b", 1), ("c", 1), ("other", 2)]
    }
    registry.list_rows = lambda: list(rows.values())
    registry.list_agents = lambda uid: [row for row in rows.values() if row.user_id == uid]
    registry.get_row = rows.get
    return rows


@pytest.mark.asyncio
async def test_chat_current_runs_real_maintenance_and_preserves_history(tmp_path):
    coordinator, registry, memory, cfg, cp = make_manager(tmp_path)
    chat_registry(registry)
    try:
        assert coordinator.start_chat("a", 1) == 1
        await coordinator.task
        snapshot = coordinator.chat_status("a", 1)
        assert snapshot["phase"] == "done"
        assert snapshot["agents"][0]["report"]["stats"]["checkpoints_deleted"] == 0
        assert memory.get_tuple(cfg).checkpoint == cp
        with pytest.raises(ValueError, match="owned"):
            coordinator.chat_status("a", 2)
        with pytest.raises(ValueError, match="owned"):
            coordinator.start_chat("other", 1)
    finally:
        await coordinator.close()
        memory._checkpointer.conn.close()
        memory.backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", [None, "failure", "ownership", "shutdown"])
async def test_chat_batch_is_owned_serialized_and_stops_safely(tmp_path, monkeypatch, stop_reason):
    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    rows = chat_registry(registry)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(agent_id, locale, *, owner_id=None):
        calls.append(agent_id)
        if agent_id == "a":
            coordinator._update({"phase": "backing_up"})
            entered.set()
            await release.wait()
        coordinator._update({"phase": "failed" if stop_reason == "failure" else "done"})

    monkeypatch.setattr(coordinator, "_run", run)
    try:
        assert coordinator.start_chat("a", 1, all_agents=True) == 3
        await entered.wait()
        assert calls == ["a"]
        assert coordinator.status("b")["phase"] == "queued"
        with pytest.raises(ValueError, match="already"):
            coordinator.start("other")
        with pytest.raises(ValueError, match="already"):
            coordinator.start_chat("other", 2)
        if stop_reason == "ownership":
            rows["b"].user_id = 2
        closing = None
        if stop_reason == "shutdown":
            closing = asyncio.create_task(coordinator.close())
            await asyncio.sleep(0)
        release.set()
        await coordinator.task
        if closing:
            await closing
        assert calls == (["a", "b", "c"] if stop_reason is None else ["a"])
        assert coordinator._chat_phase == ("done" if stop_reason is None else "failed")
        if stop_reason:
            assert coordinator.status("c")["phase"] == "skipped"
        if stop_reason == "ownership":
            with pytest.raises(ValueError, match="owned"):
                coordinator.chat_status("a", 1)
    finally:
        release.set()
        await coordinator.close()
        memory._checkpointer.conn.close()
        memory.backend.close()


@pytest.mark.asyncio
async def test_chat_rechecks_owner_after_waiting_for_active_turn(tmp_path):
    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    rows = chat_registry(registry)
    try:
        await registry._begin_invocation("a")
        coordinator.start_chat("a", 1)
        await asyncio.sleep(0.05)
        assert coordinator.state["phase"] == "waiting"
        rows["a"].user_id = 2
        registry._end_invocation("a")
        await coordinator.task
        assert coordinator.state["phase"] == "failed"
        assert "owned" in coordinator.state["error"]
        assert not list(tmp_path.glob("*.bak"))
        assert not registry._history_backfills
    finally:
        await coordinator.close()
        memory._checkpointer.conn.close()
        memory.backend.close()


def test_chat_preview_has_no_job_reservation_or_database_writes(tmp_path):
    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    chat_registry(registry)
    try:
        before = (tmp_path / "memory.sqlite").read_bytes()
        assert coordinator.preview_chat("a", 1, all_agents=True) == [
            {"agent_id": aid, "name": aid} for aid in ["a", "b", "c"]
        ]
        assert coordinator.task is None
        assert not registry._history_backfills and not coordinator._chat_states
        assert not list(tmp_path.glob("*.bak"))
        assert (tmp_path / "memory.sqlite").read_bytes() == before
    finally:
        memory._checkpointer.conn.close()
        memory.backend.close()


@pytest.mark.parametrize("phase", ["done", "failed", "skipped"])
def test_status_api_preserves_manual_terminal_result(phase):
    from octop.api.routers.agents import _memory_maintenance_status

    state = {"phase": phase, "kind": "memory_slim"}
    host = SimpleNamespace(
        app_runtime=SimpleNamespace(
            agent_registry=SimpleNamespace(memory_slim=SimpleNamespace(status=lambda _: state))
        )
    )
    assert _memory_maintenance_status(host, "a") == state


@pytest.mark.parametrize("reason", ["postgres", "disabled", "stopped", "upgrade"])
def test_preview_reports_specific_ineligibility_without_starting(tmp_path, monkeypatch, reason):
    from octop_memory.application import checkpoint_maintenance

    from octop.infra.errors import ErrorCode, OctopError

    coordinator, registry, memory, _, _ = make_manager(tmp_path)
    chat_registry(registry)
    try:
        if reason == "postgres":
            # PostgreSQL agents stay eligible: slim sweeps deleted leftovers and
            # skips SQLite dedup instead of refusing the command.
            backend = type("PostgresMemoryBackend", (), {})()
            registry.get_agent = lambda _: SimpleNamespace(
                _memory_runtime=SimpleNamespace(memory=SimpleNamespace(backend=backend))
            )
            assert coordinator.preview_chat("a", 1, locale="zh") == [{"agent_id": "a", "name": "a"}]
            assert coordinator.task is None and not registry._history_backfills
            return
        elif reason == "disabled":
            registry.get_agent = lambda _: SimpleNamespace(
                _memory_runtime=SimpleNamespace(memory=None)
            )
            expected = "没有启用记忆库"
        elif reason == "stopped":

            def stopped(_):
                raise OctopError(ErrorCode.AGENT_NOT_RUNNING, "stopped")

            registry.get_agent = stopped
            expected = "尚未启动"
        else:
            monkeypatch.delattr(checkpoint_maintenance, "slim_live_checkpoints")
            expected = "未提供在线整理接口"
            with pytest.raises(ValueError, match=expected):
                coordinator.list_agents(locale="zh")
        with pytest.raises(ValueError, match=expected):
            coordinator.preview_chat("a", 1, locale="zh")
        assert coordinator.task is None and not registry._history_backfills
        assert not list(tmp_path.glob("*.bak"))
    finally:
        memory._checkpointer.conn.close()
        memory.backend.close()
