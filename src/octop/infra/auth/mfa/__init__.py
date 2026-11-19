"""MFA primitives are importable without the legacy service container."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octop.infra.auth.mfa.service import MfaService as MfaService

__all__ = ["MfaService"]


def __getattr__(name: str) -> object:
    if name == "MfaService":
        from octop.infra.auth.mfa.service import MfaService

        return MfaService
    raise AttributeError(name)
