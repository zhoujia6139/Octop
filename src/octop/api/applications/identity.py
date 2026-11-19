"""Thin HTTP adapters for the independent cloud identity service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.contracts import (
    DeviceBind,
    DeviceGrant,
    DeviceRefresh,
    DeviceView,
    IdentityView,
    LoginInput,
    LoginView,
    MfaCode,
    MfaEnroll,
    MfaVerify,
    PasswordChange,
    PasswordProof,
    RecoveryView,
    TokenView,
    UserCreate,
    UserUpdate,
    UserView,
)
from octop.infra.identity.service import CloudIdentity


async def _call[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return await asyncio.get_running_loop().run_in_executor(
        None, partial(function, *args, **kwargs)
    )


router = APIRouter(prefix="/api")
bearer = HTTPBearer(auto_error=False)


async def identity_service(request: Request) -> CloudIdentity:
    service = request.app.state.cloud.identity
    if not isinstance(service, CloudIdentity):
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED, status=503)
    return service


async def access_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None:
        raise OctopError.localized(ErrorCode.AUTH_FAILED)
    return credentials.credentials


Service = Annotated[CloudIdentity, Depends(identity_service)]
Token = Annotated[str, Depends(access_token)]


@router.post(
    "/auth/login", tags=["auth"], response_model=LoginView, summary="Sign in using a password"
)
async def login(body: LoginInput, service: Service) -> LoginView:
    return await _call(service.login, body.username, body.password.get_secret_value())


@router.post(
    "/auth/mfa/verify",
    tags=["auth"],
    response_model=TokenView,
    summary="Complete a single-use MFA challenge",
)
async def verify(body: MfaVerify, service: Service) -> TokenView:
    return await _call(
        service.verify_mfa, body.mfa_token.get_secret_value(), body.code.get_secret_value()
    )


@router.get(
    "/auth/me",
    tags=["auth"],
    response_model=IdentityView,
    summary="Get the current live account or device identity",
)
async def me(service: Service, token: Token) -> IdentityView:
    return await _call(service.authenticate, token)


@router.get(
    "/auth/device/me",
    tags=["auth"],
    response_model=IdentityView,
    summary="Validate the current executing device and binding generation",
    description="Shared authorization boundary for subsequent model and synchronization APIs. Account and MFA tokens are rejected.",
)
async def device_me(service: Service, token: Token) -> IdentityView:
    return await _call(partial(service.authenticate, token, kind="device"))


@router.post(
    "/auth/logout",
    tags=["auth"],
    status_code=204,
    summary="Revoke this session; a device logout also revokes device renewal",
)
async def logout(service: Service, token: Token) -> Response:
    await _call(service.logout, token)
    return Response(status_code=204)


@router.post(
    "/auth/password",
    tags=["auth"],
    status_code=204,
    summary="Change password and revoke all sessions and device grants",
)
async def password(body: PasswordChange, service: Service, token: Token) -> Response:
    await _call(
        service.change_password,
        token,
        body.password.get_secret_value(),
        body.new_password.get_secret_value(),
        body.code.get_secret_value() if body.code else None,
    )
    return Response(status_code=204)


@router.post(
    "/auth/mfa/enroll",
    tags=["auth"],
    response_model=MfaEnroll,
    summary="Begin TOTP enrollment using a recent account login",
)
async def enroll(service: Service, token: Token) -> MfaEnroll:
    return await _call(service.begin_mfa, token)


@router.post(
    "/auth/mfa/confirm",
    tags=["auth"],
    response_model=RecoveryView,
    summary="Enable TOTP, return recovery codes once and revoke existing sessions",
)
async def confirm(body: MfaCode, service: Service, token: Token) -> RecoveryView:
    return await _call(service.confirm_mfa, token, body.code.get_secret_value())


@router.post(
    "/auth/mfa/disable",
    tags=["auth"],
    status_code=204,
    summary="Disable TOTP with password and a second factor; revoke all sessions",
)
async def disable(body: PasswordProof, service: Service, token: Token) -> Response:
    await _call(
        service.disable_mfa,
        token,
        body.password.get_secret_value(),
        body.code.get_secret_value() if body.code else "",
    )
    return Response(status_code=204)


@router.post(
    "/auth/devices",
    tags=["auth"],
    response_model=DeviceGrant,
    status_code=201,
    summary="Bind one executing device using a recent account login",
    description="Returns a long-lived device credential only on binding. Explicit replace revokes all earlier device grants immediately.",
)
async def bind(body: DeviceBind, service: Service, token: Token) -> DeviceGrant:
    return await _call(service.bind, token, body.label, body.replace)


@router.post(
    "/auth/device/refresh",
    tags=["auth"],
    response_model=TokenView,
    summary="Exchange a device credential for short-lived device access",
)
async def refresh(body: DeviceRefresh, service: Service) -> TokenView:
    return await _call(service.refresh, body.refresh_token.get_secret_value())


@router.get(
    "/auth/devices",
    tags=["auth"],
    response_model=list[DeviceView],
    summary="List the account's current device binding",
)
async def devices(service: Service, token: Token) -> list[DeviceView]:
    return await _call(service.devices, token)


@router.delete(
    "/auth/devices/{device_id}",
    tags=["auth"],
    status_code=204,
    summary="Revoke the owned device and every associated access token",
)
async def revoke(device_id: str, service: Service, token: Token) -> Response:
    await _call(service.revoke, token, device_id)
    return Response(status_code=204)


@router.get(
    "/users",
    response_model=list[UserView],
    tags=["users"],
    summary="List users as an administrator",
)
async def users(service: Service, token: Token) -> list[UserView]:
    return await _call(service.users, token)


@router.post(
    "/users",
    response_model=UserView,
    status_code=201,
    tags=["users"],
    summary="Create a password account as an administrator",
)
async def create(body: UserCreate, service: Service, token: Token) -> UserView:
    return await _call(
        service.create_user, token, body.username, body.password.get_secret_value(), body.role
    )


@router.put(
    "/users/{user_id}",
    response_model=UserView,
    tags=["users"],
    summary="Change user role or availability and revoke their authorization",
)
async def update(user_id: int, body: UserUpdate, service: Service, token: Token) -> UserView:
    return await _call(
        partial(service.update_user, token, user_id, enabled=body.enabled, role=body.role)
    )
