"""Explicit model and billing contracts; all monetary values are integer nanos."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from octop.infra.applications.contracts import PublicId

Protocol = Literal["chat_completions", "responses", "messages", "embeddings"]
Purpose = Literal["chat", "subagent", "summary", "title", "memory", "embedding"]


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderInput(ModelInput):
    provider_id: PublicId
    base_url: str = Field(
        description="Administrator-controlled API base, including /v1 if required."
    )
    api_key: SecretStr = Field(min_length=1, max_length=8192)
    free_rejection_statuses: list[Literal[400, 401, 403, 404, 422, 429]] = Field(
        default_factory=list,
        description="Only statuses contractually guaranteed to have no billable usage allow release/failover.",
    )

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme != "https"
                and not (
                    parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
        ):
            raise ValueError("provider base requires HTTPS; HTTP is allowed only on loopback")
        return value.rstrip("/")


class ProviderView(ModelInput):
    provider_id: str
    base_url: str
    free_rejection_statuses: list[int]
    credential_configured: bool = True


class Rate(ModelInput):
    revision: PublicId
    currency: str = Field(
        pattern=r"^[A-Z]{3}$", description="ISO currency; one nano is 10^-9 currency units."
    )
    effective_at: int = Field(ge=0)
    uncached_input: int = Field(ge=0, le=10**12, description="Nanos per uncached input token.")
    cache_read: int = Field(ge=0, le=10**12)
    cache_write: int = Field(ge=0, le=10**12)
    output: int = Field(
        ge=0, le=10**12, description="Includes reasoning tokens; never billed twice."
    )


class Route(ModelInput):
    provider_id: PublicId
    upstream_model: str = Field(min_length=1, max_length=200)


class ModelDefinition(ModelInput):
    model_id: str = Field(pattern=r"^platform/[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=200)
    protocol: Protocol
    input_modalities: list[Literal["text", "image"]] = Field(default=["text"], min_length=1)
    tools: bool = False
    input_token_ceiling: int = Field(
        ge=1,
        le=10**7,
        description="Verified provider-enforced maximum input, including images/tools; the entire ceiling is reserved.",
    )
    output_token_ceiling: int = Field(ge=0, le=10**7)
    rate: Rate
    routes: list[Route] = Field(min_length=1, max_length=4)
    enabled: bool = True

    @model_validator(mode="after")
    def bounds(self) -> ModelDefinition:
        if self.protocol == "embeddings":
            if self.output_token_ceiling or self.tools or self.input_modalities != ["text"]:
                raise ValueError("embeddings require text only, no tools and zero output ceiling")
        elif not self.output_token_ceiling:
            raise ValueError("generation requires an output ceiling")
        if (
            self.input_token_ceiling
            * max(self.rate.uncached_input, self.rate.cache_read, self.rate.cache_write)
            + self.output_token_ceiling * self.rate.output
            > 10**18
        ):
            raise ValueError("reservation exceeds supported integer range")
        return self


class ModelView(ModelInput):
    model_id: str
    name: str
    protocol: Protocol
    input_modalities: list[Literal["text", "image"]]
    tools: bool
    input_token_ceiling: int
    output_token_ceiling: int
    rate: Rate


class BudgetInput(ModelInput):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    limit_nanos: int = Field(
        ge=0,
        le=10**18,
        description="Absolute spending limit; this API is not a payment or top-up mechanism.",
    )


class BudgetView(BudgetInput):
    user_id: int
    spent_nanos: int
    reserved_nanos: int


class GrantInput(ModelInput):
    allowed: bool


class ModelContext(ModelInput):
    agent_id: PublicId
    thread_id: PublicId
    turn_id: PublicId


class ModelRequest(ModelContext):
    request_id: PublicId
    model_id: str
    purpose: Purpose
    payload: dict[str, Any] = Field(
        description="Native protocol request, without credentials, URLs or client billing claims."
    )


class Usage(ModelInput):
    uncached_input: int = Field(ge=0, le=10**9)
    cache_read: int = Field(ge=0, le=10**9)
    cache_write: int = Field(ge=0, le=10**9)
    output: int = Field(ge=0, le=10**9)

    def cost(self, rate: Rate) -> int:
        return sum(getattr(self, key) * getattr(rate, key) for key in type(self).model_fields)


class ReconcileInput(ModelInput):
    usage: Usage | None = Field(
        description="Authoritative provider usage, or null only for confirmed no consumption."
    )
    evidence: str = Field(
        min_length=3,
        max_length=2000,
        description="Required provider receipt/reference and reason; stored with the administrator identity.",
    )


class AttemptView(ModelInput):
    attempt_id: str
    provider_id: str
    state: str
    reserved_nanos: int
    cost_nanos: int | None
    usage: Usage | None
    reason: str | None
    evidence: str | None
    resolved_by: int | None


class RequestView(ModelContext):
    request_id: str
    user_id: int
    device_id: str
    model_id: str
    purpose: Purpose
    state: str
    currency: str
    rate_revision: str
    created_at: int
    attempts: list[AttemptView]


class UsageSummary(ModelInput):
    user_id: int
    model_id: str
    currency: str
    cost_nanos: int
    uncached_input: int
    cache_read: int
    cache_write: int
    output: int
    settled_attempts: int
