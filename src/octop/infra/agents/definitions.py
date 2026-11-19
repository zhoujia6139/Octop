"""Agent definition management without a Harness instance or workspace."""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from octop.infra.db.repos.agents import AgentRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.groups import model_ref_allowed
from octop.infra.users.identity import User
from octop.infra.users.resource_policy import assert_agent_quota_available
from octop.infra.utils.ulid import new_short_id

AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class AgentDefinitionInput(BaseModel):
    """Portable definition fields; device paths and credentials are not accepted."""

    model_config = ConfigDict(extra="forbid")

    name: AgentName
    description: str | None = None
    system_prompt: str | None = None
    default_model: str | None = None


class AgentDefinitionService:
    """Persist definitions independently of the local runtime lifecycle.

    Each mutation runs in one executor job; the process lock serializes local
    name/quota checks. Cross-process resource publication is added with the
    versioned resource transaction in P4.
    """

    def __init__(self, services: SharedServices) -> None:
        self._services = services
        self._mutation_lock = asyncio.Lock()

    def list(self, user: User) -> list[AgentRow]:
        return self._services.agent_repo.list_by_user(user.id)

    def get(self, agent_id: str, user: User) -> AgentRow:
        row = self._services.agent_repo.get(agent_id)
        if row is None:
            raise OctopError.localized(ErrorCode.AGENT_NOT_FOUND)
        if row.user_id != user.id and not user.is_admin:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        return row

    def _validate(
        self, body: AgentDefinitionInput, user_id: int, *, is_admin: bool, exclude: str = ""
    ) -> None:
        rows = self._services.agent_repo.list_by_user(user_id)
        if any(row.name == body.name and row.agent_id != exclude for row in rows):
            raise OctopError.localized(ErrorCode.AGENT_NAME_TAKEN)
        if not model_ref_allowed(
            self._services.repos,
            user_id=user_id,
            is_admin=is_admin,
            model_ref=body.default_model,
        ):
            raise OctopError.localized(ErrorCode.RESOURCE_NOT_ALLOWED)

    def _create(self, body: AgentDefinitionInput, user: User) -> AgentRow:
        self._validate(body, user.id, is_admin=user.is_admin)
        assert_agent_quota_available(
            self._services.user_policy_repo, self._services.agent_repo, user.id
        )
        agent_id = new_short_id()
        self._services.agent_repo.create(agent_id=agent_id, user_id=user.id, **body.model_dump())
        self._services.audit_repo.write(
            actor=user.username, action="agent.definition.create", target=agent_id
        )
        return self.get(agent_id, user)

    async def create(self, body: AgentDefinitionInput, user: User) -> AgentRow:
        async with self._mutation_lock:
            return await asyncio.get_running_loop().run_in_executor(None, self._create, body, user)

    def _replace(self, agent_id: str, body: AgentDefinitionInput, user: User) -> AgentRow:
        row = self.get(agent_id, user)
        # Admin bypass permits access, but model grants and name checks apply
        # to the owner, not the admin making the change.
        owner = self._services.user_repo.get(int(row.user_id)) if row.user_id is not None else None
        if owner is None:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        self._validate(body, owner.id, is_admin=owner.role == "admin", exclude=agent_id)
        self._services.agent_repo.update_config(agent_id, **body.model_dump())
        return self.get(agent_id, user)

    async def replace(self, agent_id: str, body: AgentDefinitionInput, user: User) -> AgentRow:
        async with self._mutation_lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._replace, agent_id, body, user
            )

    def _delete(self, agent_id: str, user: User) -> None:
        self.get(agent_id, user)
        self._services.agent_repo.delete(agent_id)
        self._services.audit_repo.write(
            actor=user.username, action="agent.definition.delete", target=agent_id
        )

    async def delete(self, agent_id: str, user: User) -> None:
        async with self._mutation_lock:
            await asyncio.get_running_loop().run_in_executor(None, self._delete, agent_id, user)
