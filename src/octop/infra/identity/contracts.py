"""Typed identity contracts, shared without importing an application runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class IdentityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginInput(IdentityInput):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: SecretStr = Field(min_length=1, max_length=1024)


class UserCreate(LoginInput):
    role: Literal["admin", "user"] = "user"


class UserUpdate(IdentityInput):
    enabled: bool
    role: Literal["admin", "user"]


class UserView(BaseModel):
    user_id: int
    username: str
    role: Literal["admin", "user"]
    enabled: bool
    mfa_enabled: bool


class IdentityView(BaseModel):
    cloud_id: str
    user: UserView
    device_id: str | None = None
    binding_generation: int | None = None
    timezone: str


class TokenView(IdentityInput):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    identity: IdentityView


class LoginView(BaseModel):
    session: TokenView | None = None
    mfa_token: str | None = None
    expires_in: int = Field(
        default=300, description="MFA challenge lifetime; access lifetime is session.expires_in."
    )


class MfaVerify(IdentityInput):
    mfa_token: SecretStr = Field(min_length=32, max_length=256)
    code: SecretStr = Field(min_length=1, max_length=64)


class MfaCode(IdentityInput):
    code: SecretStr = Field(min_length=1, max_length=64)


class MfaEnroll(BaseModel):
    secret: str
    otpauth_uri: str
    expires_in: int = 600


class RecoveryView(BaseModel):
    codes: list[str]


class PasswordProof(IdentityInput):
    password: SecretStr = Field(min_length=1, max_length=1024)
    code: SecretStr | None = Field(default=None, max_length=64)


class PasswordChange(PasswordProof):
    new_password: SecretStr = Field(min_length=1, max_length=1024)


class DeviceBind(IdentityInput):
    label: str = Field(min_length=1, max_length=128)
    replace: bool = Field(
        default=False, description="Explicitly replace and revoke the existing executing device."
    )


class DeviceRefresh(IdentityInput):
    refresh_token: SecretStr = Field(min_length=32, max_length=256)


class DeviceGrant(BaseModel):
    session: TokenView
    refresh_token: str
    refresh_expires_in: int


class DeviceView(BaseModel):
    device_id: str
    label: str
    binding_generation: int
    revoked: bool
    last_seen_at: int
