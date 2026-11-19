"""Local Agent hosting directly on Harness public lifecycle and turn APIs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from octop.application_config import LocalSettings
from octop.infra.applications.contracts import (
    LocalAgentInput,
    LocalAgentView,
    ThreadView,
    TurnEvent,
    TurnStatus,
)
from octop.infra.applications.local_services import LocalServices
from octop.infra.db.application_migrations import migrate_application
from octop.infra.db.pool import SqlitePool
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop_harness import HarnessAgent
    from octop_harness.llm.factory import ChatModelFactory

    from octop.infra.applications.local_models import LocalModels


class LocalApplication:
    """No Gateway, GlobalProcessor, old AgentManager or cloud credential store.

    The platform model adapter is installed by trusted application composition.
    P1 tests inject a model factory; JSON settings and HTTP cannot configure a
    provider URL, supplier key or test-mode switch. Without the P3 platform
    adapter, production turn execution remains unavailable.
    """

    def __init__(
        self,
        settings: LocalSettings,
        *,
        model_factory: ChatModelFactory | None = None,
        model_platform: LocalModels | None = None,
    ) -> None:
        self.settings = settings
        self.services: LocalServices | None = None
        self.schema_version = 0
        self._model_factory = model_factory
        self.model_platform = model_platform
        self.model_turns: dict[str, tuple[str, str]] = {}
        self._agents: dict[str, HarnessAgent] = {}
        self._agent_lock = asyncio.Lock()
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._pending: dict[str, tuple[str, int]] = {}
        self._closing = False

    def _open(self) -> tuple[LocalServices, int]:
        db = SqlitePool(self.settings.home / "local.db")
        try:
            version = migrate_application(db, "local")
            services = LocalServices.from_pool(db)
            services.turns.recover_interrupted()
            return services, version
        except BaseException:
            db.close()
            raise

    async def start(self) -> None:
        if self.services is None:
            self.services, self.schema_version = await asyncio.get_running_loop().run_in_executor(
                None, self._open
            )
            self._closing = False

    def _storage(self) -> LocalServices:
        if self.services is None or self._closing:
            raise OctopError.localized(ErrorCode.SETUP_REQUIRED, status=503)
        return self.services

    async def list_agents(self) -> list[LocalAgentView]:
        rows = await asyncio.get_running_loop().run_in_executor(None, self._storage().agents.list)
        return [LocalAgentView(**asdict(row)) for row in rows]

    async def _agent(self, agent_id: str) -> HarnessAgent:
        async with self._agent_lock:
            if agent_id in self._agents:
                return self._agents[agent_id]
            row = await asyncio.get_running_loop().run_in_executor(
                None, self._storage().agents.get, agent_id
            )
            if row is None:
                raise OctopError.localized(ErrorCode.AGENT_NOT_FOUND)
            factory = self._model_factory
            if self.model_platform:
                factory = await self.model_platform.factory(agent_id)
            if factory is None:
                raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
            from octop_harness import HarnessAgent, HarnessAgentConfig

            workspace = self.settings.home / "workspaces" / agent_id
            config = HarnessAgentConfig(
                name=row.name,
                workspace_dir=workspace,
                system_files_path=".octop",
                providers=factory.provider_configs(),
                default_model=row.model_id,
                system_prompt=row.system_prompt,
                backend={"type": "local_shell", "root_dir": str(workspace), "virtual_mode": False},
                memory_enabled=False,
                memory_jsonl_enabled=False,
                memory=None,
                web_search_tools=False,
                acp_runners={},
                todos_enabled=False,
                interrupt_on={"write_file": True, "edit_file": True, "execute": True},
                default_timezone=self.settings.default_timezone,
            )
            # Graph compilation and workspace initialization perform blocking I/O.
            initialized = asyncio.get_running_loop().run_in_executor(
                None,
                lambda: HarnessAgent(config, model_factory=factory, agent_id=agent_id),
            )
            try:
                agent = await asyncio.shield(initialized)
            except asyncio.CancelledError:
                agent = await initialized
                await agent.aclose()
                raise
            self._agents[agent_id] = agent
            return agent

    async def create_agent(self, body: LocalAgentInput) -> LocalAgentView:
        if self.model_platform:
            await self.model_platform.validate_model(body.model_id)
        elif self._model_factory:
            try:
                self._model_factory.resolve_ref(body.model_id)
            except (KeyError, ValueError) as exc:
                raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED) from exc
        else:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        agent_id = new_ulid()
        await asyncio.get_running_loop().run_in_executor(
            None,
            self._storage().agents.create,
            agent_id,
            body.name,
            body.model_id,
            body.system_prompt,
        )
        # Instantiation remains lazy: failed model availability does not lose a
        # persisted definition, and startup never auto-runs a previous task.
        return LocalAgentView(agent_id=agent_id, **body.model_dump())

    async def create_thread(self, agent_id: str) -> ThreadView:
        storage = self._storage()
        row = await asyncio.get_running_loop().run_in_executor(None, storage.agents.get, agent_id)
        if row is None:
            raise OctopError.localized(ErrorCode.AGENT_NOT_FOUND)
        thread_id = new_ulid()
        await asyncio.get_running_loop().run_in_executor(
            None, storage.threads.create, thread_id, agent_id
        )
        return ThreadView(thread_id=thread_id, agent_id=agent_id)

    async def validate_model_context(self, agent_id: str, thread_id: str, turn_id: str) -> None:
        storage = self._storage()
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(
            None, storage.threads.belongs_to, thread_id, agent_id
        ) or not await loop.run_in_executor(None, storage.turns.belongs_to, turn_id, thread_id):
            raise OctopError.localized(ErrorCode.NOT_FOUND)

    async def cancel(self, thread_id: str) -> TurnEvent | None:
        if task := self._active.get(thread_id):
            for agent in self._agents.values():
                agent.cancel(thread_id)
            task.cancel()
        elif pending := self._pending.pop(thread_id, None):
            turn_id, sequence = pending
            await asyncio.get_running_loop().run_in_executor(
                None, self._storage().turns.set_status, turn_id, "cancelled"
            )
            return TurnEvent(
                turn_id=turn_id,
                thread_id=thread_id,
                sequence=sequence + 1,
                type="turn.status",
                data={"status": "cancelled"},
            )
        return None

    async def stream(
        self,
        agent_id: str,
        thread_id: str,
        *,
        text: str | None = None,
        decisions: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        if thread_id in self._active:
            raise OctopError.localized(ErrorCode.THREAD_TURN_ACTIVE)
        storage = self._storage()
        task = asyncio.current_task()
        assert task is not None
        self._active[thread_id] = task
        turn_id, sequence = new_ulid(), 0
        status = TurnStatus.INTERRUPTED
        created = False

        def event(kind: str, data: dict[str, Any]) -> TurnEvent:
            nonlocal sequence
            sequence += 1
            return TurnEvent(
                turn_id=turn_id, thread_id=thread_id, sequence=sequence, type=kind, data=data
            )

        try:
            belongs = await asyncio.get_running_loop().run_in_executor(
                None, storage.threads.belongs_to, thread_id, agent_id
            )
            if not belongs:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            agent = await self._agent(agent_id)
            if decisions is not None:
                pending = self._pending.pop(thread_id, None)
                if pending is None:
                    raise OctopError.localized(ErrorCode.LOCAL_APPROVAL_NOT_PENDING)
                turn_id, sequence = pending
                source = agent.resume_hitl(thread_id, decisions)
                await asyncio.get_running_loop().run_in_executor(
                    None, storage.turns.set_status, turn_id, "running"
                )
            else:
                if thread_id in self._pending:
                    raise OctopError.localized(ErrorCode.THREAD_TURN_ACTIVE)
                from octop_harness import ChatRequest

                source = agent.stream(
                    ChatRequest(
                        messages=text or "",
                        thread_id=thread_id,
                        agent_id=agent_id,
                        source="desktop",
                    )
                )
                await asyncio.get_running_loop().run_in_executor(
                    None, storage.turns.create, turn_id, thread_id
                )
            created = True
            self.model_turns[thread_id] = (agent_id, turn_id)
            status = TurnStatus.COMPLETED
            yield event("turn.started", {})
            async with aclosing(source) as chunks:
                async for chunk in chunks:
                    if isinstance(chunk, dict):
                        kind = str(chunk.get("type", "state_update"))
                        if kind == "hitl_required":
                            status = TurnStatus.AWAITING_APPROVAL
                        yield event(kind, chunk)
        except asyncio.CancelledError:
            status = TurnStatus.INTERRUPTED if self._closing else TurnStatus.CANCELLED
        except GeneratorExit:
            status = TurnStatus.INTERRUPTED
            raise
        except Exception as exc:
            status = TurnStatus.FAILED
            if (
                self.model_platform
                and not isinstance(exc, OctopError)
                and (platform_error := self.model_platform.error(exc))
            ):
                raise platform_error from exc
            raise
        except BaseException:
            status = TurnStatus.FAILED
            raise
        finally:
            if created:
                await asyncio.shield(
                    asyncio.get_running_loop().run_in_executor(
                        None, storage.turns.set_status, turn_id, status.value
                    )
                )
            self._active.pop(thread_id, None)
            self.model_turns.pop(thread_id, None)
        terminal = event("turn.status", {"status": status.value})
        if status == TurnStatus.AWAITING_APPROVAL:
            self._pending[thread_id] = (turn_id, sequence)
        yield terminal

    async def close(self, *, close_model_factory: bool = True) -> None:
        self._closing = True
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            results = await asyncio.gather(
                *(agent.aclose() for agent in self._agents.values()), return_exceptions=True
            )
            if self.model_platform:
                await self.model_platform.close()
            if close_model_factory and self._model_factory is not None:
                await self._model_factory.aclose()
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            self._agents.clear()
            self._pending.clear()
            if self.services is not None:
                services, self.services = self.services, None
                await asyncio.get_running_loop().run_in_executor(None, services.db.close)
