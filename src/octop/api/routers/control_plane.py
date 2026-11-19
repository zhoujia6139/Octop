"""Cloud-only management surface. No runtime or workspace endpoints."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import current_user, get_server
from octop.infra.agents.definitions import AgentDefinitionInput
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()


class ControlPlaneHealth(BaseModel):
    ok: bool
    role: Literal["cloud"] = "cloud"
    db: bool
    users_loaded: int
    started_at: int | None


class AgentDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: str = Field(description="Public Agent identifier, independent of its runtime.")
    user_id: int | None
    name: str
    description: str | None
    system_prompt: str | None
    default_model: str | None
    created_at: int
    updated_at: int


@router.get("/health", tags=["health"], summary="Cloud management health")
async def health(server: OctopServer = Depends(get_server)) -> ControlPlaneHealth:
    return ControlPlaneHealth(
        ok=server.database_bound,
        db=server.database_bound,
        users_loaded=server.user_manager.count() if server.user_manager is not None else 0,
        started_at=server._started_at,
    )


@router.get("/agents", tags=["agents"], summary="List personal Agent definitions")
async def list_agents(
    user: User = Depends(current_user), server: OctopServer = Depends(get_server)
) -> list[AgentDefinitionResponse]:
    assert server.agent_definitions is not None
    rows = await asyncio.get_running_loop().run_in_executor(
        None, server.agent_definitions.list, user
    )
    return [AgentDefinitionResponse.model_validate(row) for row in rows]


@router.post(
    "/agents",
    status_code=201,
    tags=["agents"],
    summary="Create a personal Agent definition",
    description="Saves configuration only. Does not start an Agent or create a server workspace.",
)
async def create_agent(
    body: AgentDefinitionInput,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AgentDefinitionResponse:
    assert server.agent_definitions is not None
    return AgentDefinitionResponse.model_validate(await server.agent_definitions.create(body, user))


@router.get("/agents/{agent_id}", tags=["agents"], summary="Read an Agent definition")
async def get_agent(
    agent_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AgentDefinitionResponse:
    assert server.agent_definitions is not None
    row = await asyncio.get_running_loop().run_in_executor(
        None, server.agent_definitions.get, agent_id, user
    )
    return AgentDefinitionResponse.model_validate(row)


@router.put("/agents/{agent_id}", tags=["agents"], summary="Replace a personal Agent definition")
async def replace_agent(
    agent_id: str,
    body: AgentDefinitionInput,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AgentDefinitionResponse:
    assert server.agent_definitions is not None
    row = await server.agent_definitions.replace(agent_id, body, user)
    return AgentDefinitionResponse.model_validate(row)


@router.delete(
    "/agents/{agent_id}", status_code=204, tags=["agents"], summary="Delete an Agent definition"
)
async def delete_agent(
    agent_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    assert server.agent_definitions is not None
    await server.agent_definitions.delete(agent_id, user)
    return Response(status_code=204)
