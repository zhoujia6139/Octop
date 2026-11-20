"""Local Agent hosting directly on Harness public lifecycle and turn APIs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from octop.application_config import LocalSettings
from octop.infra.applications.contracts import (
    LocalAgentInput,
    LocalAgentView,
    ThreadState,
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
    from octop.infra.db.repos.local_resources import LocalResourceRepo
    from octop.infra.resources.contracts import SkillSpec, VersionView
    from octop.infra.resources.local import LocalResources
    from octop.infra.sync.local import LocalSync


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
        self.resources: LocalResources | None = None
        self.sync: LocalSync | None = None
        self._turn_agents: dict[str, str] = {}
        self.model_turns: dict[str, tuple[str, str]] = {}
        self._agents: dict[str, HarnessAgent] = {}
        self._agent_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._user_cancelled: set[str] = set()
        self._pending: dict[str, tuple[str, int]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._closing = False
        self._quiescing = False

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
        return [LocalAgentView.model_validate(asdict(row), extra="ignore") for row in rows]

    async def _build_agent(
        self, agent_id: str, body: LocalAgentInput, skills_path: str | None = None
    ) -> HarnessAgent:
        factory = self._model_factory
        if self.model_platform:
            factory = await self.model_platform.factory(agent_id)
        if factory is None:
            raise OctopError.localized(ErrorCode.PLATFORM_MODEL_UNAVAILABLE)
        from octop_harness import HarnessAgent, HarnessAgentConfig

        workspace = self.settings.home / "workspaces" / agent_id
        config = HarnessAgentConfig(
            name=body.name,
            workspace_dir=workspace,
            system_files_path=".octop",
            skills_dir=skills_path,
            providers=factory.provider_configs(),
            default_model=body.model_id,
            system_prompt=body.system_prompt,
            backend={"type": "local_shell", "root_dir": str(workspace), "virtual_mode": False},
            memory_enabled=self.sync is not None,
            memory_namespace="agent_" + agent_id,
            memory_backend_config={
                "db_path": str(self.settings.home / "memory" / (agent_id + ".sqlite"))
            },
            memory_aux_model_enabled=False,
            memory_extract_on_session_end=False,
            memory_jsonl_enabled=False,
            memory=None,
            web_search_tools=False,
            acp_runners={},
            todos_enabled=False,
            interrupt_on={},
            default_timezone=self.settings.default_timezone,
        )
        initialized = asyncio.get_running_loop().run_in_executor(
            None, lambda: HarnessAgent(config, model_factory=factory, agent_id=agent_id)
        )
        try:
            return await asyncio.shield(initialized)
        except asyncio.CancelledError:
            try:
                agent = await initialized
                await agent.aclose()
            finally:
                if self.model_platform:
                    await self.model_platform.release_factory(factory)
            raise
        except BaseException:
            if self.model_platform:
                await self.model_platform.release_factory(factory)
            raise

    async def _close_agent(self, agent: HarnessAgent) -> None:
        await agent.aclose(wait_for_memory=True)
        if self.model_platform:
            await self.model_platform.release_factory(agent.model_factory)

    async def _agent(self, agent_id: str) -> HarnessAgent:
        async with self._agent_lock:
            if self._quiescing or self._closing:
                raise OctopError.localized(ErrorCode.LOCAL_RUNTIME_PAUSED)
            row = await asyncio.get_running_loop().run_in_executor(
                None, self._storage().agents.get, agent_id
            )
            if row is None or not row.enabled:
                raise OctopError.localized(ErrorCode.AGENT_NOT_FOUND)
            if agent_id not in self._agents:
                self._agents[agent_id] = await self._build_agent(
                    agent_id,
                    LocalAgentInput(
                        name=row.name, model_id=row.model_id, system_prompt=row.system_prompt
                    ),
                    row.skills_path,
                )
            return self._agents[agent_id]

    def agent_busy(self, agent_id: str) -> bool:
        return agent_id in self._turn_agents.values()

    def resource_state(
        self, agent_id: str
    ) -> Literal["stopped", "idle", "running", "awaiting_approval"]:
        if any(self._turn_agents.get(thread_id) == agent_id for thread_id in self._pending):
            return "awaiting_approval"
        if self.agent_busy(agent_id):
            return "running"
        return "idle" if agent_id in self._agents else "stopped"

    async def remove_resource(self, resource_id: str, repo: LocalResourceRepo) -> None:
        from octop.infra.resources.local import durable

        async with self._agent_lock:
            row = await durable(repo.get, resource_id)
            instance_id = row["agent_id"] if row else None
            if instance_id and self.agent_busy(instance_id):
                return
            await durable(repo.installed, resource_id, None, None)
            if instance_id and (previous := self._agents.pop(instance_id, None)):
                await self._close_agent(previous)

    async def install_resource(
        self, version: VersionView, skills: list[SkillSpec], repo: LocalResourceRepo
    ) -> None:
        # Installation is an owned transition. Disconnect/shutdown waits for
        # either a complete pointer switch or full candidate cleanup.
        task = asyncio.create_task(self._install_resource(version, skills, repo))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    async def _install_resource(
        self, version: VersionView, skills: list[SkillSpec], repo: LocalResourceRepo
    ) -> None:
        import hashlib

        from octop.infra.resources.contracts import AgentSpec
        from octop.infra.resources.local import durable

        assert isinstance(version.spec, AgentSpec)
        resource_id, spec = version.resource.resource_id, version.spec
        async with self._agent_lock:
            row = await durable(repo.get, resource_id)
            assert row is not None and row["agent_id"] is not None
            agent_id = row["agent_id"]
            if self.agent_busy(agent_id) or self._quiescing:
                return
            # A fresh immutable directory means incomplete downloads never
            # overwrite an installed skill tree or add stale files to a version.
            skills_path = f".octop/resource-versions/{new_ulid()}/skills" if skills else None
            candidate = await self._build_agent(
                agent_id,
                LocalAgentInput(
                    name=spec.name, model_id=spec.model_id, system_prompt=spec.system_prompt
                ),
                skills_path,
            )
            try:
                for skill in skills:
                    for file in skill.files:
                        path = f"{skills_path}/{skill.slug}/{file.path}"
                        await durable(candidate.workspace.resolve_path, path)
                        await candidate.workspace.aupload_bytes(path, file.content())
                        actual = await candidate.workspace.adownload_bytes(path)
                        if (
                            actual is None
                            or len(actual) != file.size
                            or hashlib.sha256(actual).hexdigest() != file.sha256
                        ):
                            raise OctopError.localized(ErrorCode.RESOURCE_INSTALL_FAILED)
                await durable(
                    repo.installed,
                    resource_id,
                    version.revision,
                    version.model_dump_json(),
                    agent=spec.model_dump(),
                    skills_path=skills_path,
                )
            except BaseException:
                await self._close_agent(candidate)
                raise
            previous = self._agents.get(agent_id)
            self._agents[agent_id] = candidate
            if previous:
                await self._close_agent(previous)

    async def create_agent(self, body: LocalAgentInput) -> LocalAgentView:
        if self.resources:
            from octop.infra.resources.contracts import AgentSpec, Mutation

            agent_id = new_ulid()
            await self.resources.edit(
                agent_id,
                Mutation(
                    operation_id=new_ulid(),
                    expected_revision=0,
                    spec=AgentSpec(**body.model_dump()),
                ),
            )
            try:
                await self.resources.sync()
            except OctopError as error:
                if error.code not in {ErrorCode.CLOUD_UNAVAILABLE, ErrorCode.AUTH_FAILED}:
                    raise
            return LocalAgentView(agent_id=agent_id, **body.model_dump())
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
        if row is None or not row.enabled:
            raise OctopError.localized(ErrorCode.AGENT_NOT_FOUND)
        thread_id = new_ulid()
        if self.sync:
            await self.sync.create_thread(thread_id, agent_id)
            return ThreadView(thread_id=thread_id, agent_id=agent_id)
        await asyncio.get_running_loop().run_in_executor(
            None, storage.threads.create, thread_id, agent_id
        )
        return ThreadView(thread_id=thread_id, agent_id=agent_id)

    async def thread_state(self, agent_id: str, thread_id: str) -> ThreadState:
        storage = self._storage()
        if not await asyncio.get_running_loop().run_in_executor(
            None, storage.threads.belongs_to, thread_id, agent_id
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        latest = await asyncio.get_running_loop().run_in_executor(
            None, storage.turns.latest, thread_id
        )
        return ThreadState.model_validate(
            {**(latest or {}), "approval": self._approvals.get(thread_id)}
        )

    async def validate_model_context(self, agent_id: str, thread_id: str, turn_id: str) -> None:
        storage = self._storage()
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(
            None, storage.threads.belongs_to, thread_id, agent_id
        ) or not await loop.run_in_executor(None, storage.turns.belongs_to, turn_id, thread_id):
            raise OctopError.localized(ErrorCode.NOT_FOUND)

    async def cancel(self, thread_id: str) -> TurnEvent | None:
        if task := self._active.get(thread_id):
            self._user_cancelled.add(thread_id)
            for agent in self._agents.values():
                agent.cancel(thread_id)
            task.cancel()
        elif pending := self._pending.pop(thread_id, None):
            self._approvals.pop(thread_id, None)
            turn_id, sequence = pending
            agent_id = self._turn_agents.pop(thread_id, None)
            if self.sync:
                assert agent_id is not None
                projection = await self.sync.projection(agent_id, thread_id, turn_id)
                await projection.load()
                await projection.finish("cancelled")
                self.sync.changed()
            else:
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
        attachment_ids: list[str] | None = None,
    ) -> AsyncGenerator[TurnEvent, None]:
        if self._quiescing or self._closing:
            raise OctopError.localized(ErrorCode.LOCAL_RUNTIME_PAUSED)
        if thread_id in self._active:
            raise OctopError.localized(ErrorCode.THREAD_TURN_ACTIVE)
        if self.sync:
            await self.sync.ensure_ready(agent_id, thread_id)
        if self.resources:
            await self.resources.install_pending()
        # Installing a pending version yields; another start may have claimed
        # this thread while we were waiting at the configuration boundary.
        if thread_id in self._active:
            raise OctopError.localized(ErrorCode.THREAD_TURN_ACTIVE)
        storage = self._storage()
        task = asyncio.current_task()
        assert task is not None
        self._active[thread_id] = task
        self._turn_agents[thread_id] = agent_id
        turn_id, sequence = new_ulid(), 0
        status = TurnStatus.INTERRUPTED
        created = False
        projection = None

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
            if self.sync:
                await self.sync.ensure_ready(agent_id, thread_id)
            attachments = (
                await self.sync.attachments(agent_id, attachment_ids or []) if self.sync else []
            )
            if decisions is not None:
                pending = self._pending.pop(thread_id, None)
                if pending is None:
                    raise OctopError.localized(ErrorCode.LOCAL_APPROVAL_NOT_PENDING)
                turn_id, sequence = pending
                self._approvals.pop(thread_id, None)
                source = agent.resume_hitl(thread_id, decisions)
                if self.sync:
                    projection = await self.sync.projection(agent_id, thread_id, turn_id)
                    await projection.begin("", [], resume=True)
                else:
                    await asyncio.get_running_loop().run_in_executor(
                        None, storage.turns.set_status, turn_id, "running"
                    )
            else:
                if thread_id in self._pending:
                    raise OctopError.localized(ErrorCode.THREAD_TURN_ACTIVE)
                from langchain_core.messages import BaseMessage, HumanMessage
                from octop_harness import ChatRequest

                message_id = None
                if self.sync:
                    await self.sync.rehydrate(agent_id, thread_id)
                    projection = await self.sync.projection(agent_id, thread_id, turn_id)
                    message_id = await projection.begin(text or "", attachments, resume=False)
                content = text or ""
                if attachments and self.sync:
                    for attachment in attachments:
                        row = await asyncio.get_running_loop().run_in_executor(
                            None, self.sync.repo.get, attachment.file_id
                        )
                        if row:
                            import json

                            content += "\n[Attachment: " + json.loads(row["data"])["path"] + "]"

                input_messages: list[BaseMessage] = [HumanMessage(id=message_id, content=content)]
                source = agent.stream(
                    ChatRequest(
                        messages=input_messages,
                        thread_id=thread_id,
                        agent_id=agent_id,
                        source="desktop",
                    )
                )
                if not self.sync:
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
                            self._approvals[thread_id] = chunk.get("request", {})
                        if projection:
                            await projection.capture(chunk)
                        yield event(kind, chunk)
        except asyncio.CancelledError:
            status = (
                TurnStatus.CANCELLED
                if thread_id in self._user_cancelled and not self._closing and not self._quiescing
                else TurnStatus.INTERRUPTED
            )
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
                if projection:
                    await projection.finish(status.value)
                else:
                    await asyncio.shield(
                        asyncio.get_running_loop().run_in_executor(
                            None, storage.turns.set_status, turn_id, status.value
                        )
                    )
            self._active.pop(thread_id, None)
            self._user_cancelled.discard(thread_id)
            self.model_turns.pop(thread_id, None)
            if status != TurnStatus.AWAITING_APPROVAL:
                self._turn_agents.pop(thread_id, None)
            if self.resources and not self._closing:
                self.resources.changed()
            if self.sync and not self._closing:
                self.sync.changed()
        terminal = event("turn.status", {"status": status.value})
        if status == TurnStatus.AWAITING_APPROVAL:
            self._pending[thread_id] = (turn_id, sequence)
        yield terminal

    async def quiesce(self) -> None:
        """Stop admission and persist interruptions without resuming pending tools."""
        self._quiescing = True
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for thread_id, (turn_id, _) in list(self._pending.items()):
            agent_id = self._turn_agents.get(thread_id)
            if self.sync and agent_id:
                projection = await self.sync.projection(agent_id, thread_id, turn_id)
                await projection.load()
                await projection.finish("interrupted")
            elif self.services:
                await asyncio.get_running_loop().run_in_executor(
                    None, self.services.turns.set_status, turn_id, "interrupted"
                )
        self._pending.clear()
        self._approvals.clear()
        self._turn_agents.clear()

    async def prepare_shutdown(self) -> None:
        """Persist first, then make one bounded best-effort backup attempt."""
        async with self._lifecycle_lock:
            if self.services is not None and not self._closing:
                await self._prepare_shutdown()

    async def _prepare_shutdown(self) -> None:
        await self.quiesce()
        # Close memory writers before taking the final logical snapshot. No new
        # Agent may be created while quiescing; the offline workspace facade
        # remains available to synchronization.
        async with self._agent_lock:
            agents, self._agents = self._agents, {}
            await asyncio.gather(*(self._close_agent(a) for a in agents.values()))
        try:
            async with asyncio.timeout(2):
                if self.resources:
                    await self.resources.sync()
                if self.sync:
                    await self.sync.sync()
        except (TimeoutError, OctopError, OSError, ValueError):
            # Pending rows remain durable. An unacknowledged request is never
            # marked synced merely because this grace period ended.
            pass

    async def close(self, *, close_model_factory: bool = True) -> None:
        async with self._lifecycle_lock:
            await self._close(close_model_factory=close_model_factory)

    async def _close(self, *, close_model_factory: bool) -> None:
        self._quiescing = True
        if self.sync:
            await self.sync.close()
        if self.resources:
            await self.resources.close()
        await self.quiesce()
        self._closing = True
        try:
            results = await asyncio.gather(
                *(self._close_agent(agent) for agent in self._agents.values()),
                return_exceptions=True,
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
