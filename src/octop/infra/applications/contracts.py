"""Versioned application contracts, free of runtime and database dependencies."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PublicId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")]


class Health(BaseModel):
    protocol_version: Literal[1] = 1
    role: Literal["cloud", "local"]
    ok: bool
    schema_version: int


class LocalAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    model_id: str = Field(min_length=1, description="Authorized logical platform model identifier.")
    system_prompt: str | None = None


class LocalAgentView(LocalAgentInput):
    agent_id: PublicId


class ThreadView(BaseModel):
    thread_id: PublicId
    agent_id: PublicId


class TurnStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class TurnEvent(BaseModel):
    protocol_version: Literal[1] = 1
    turn_id: PublicId
    thread_id: PublicId
    sequence: int
    type: str
    data: dict[str, Any]
