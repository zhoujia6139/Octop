"""Agent settings; pure profile imports must not load execution backends."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octop.infra.agents.settings.acp import ACPSettingsStore as ACPSettingsStore
    from octop.infra.agents.settings.langfuse import (
        LangfuseSettings as LangfuseSettings,
    )
    from octop.infra.agents.settings.langfuse import (
        LangfuseSettingsStore as LangfuseSettingsStore,
    )
    from octop.infra.agents.settings.langfuse import (
        verify_langfuse_credentials as verify_langfuse_credentials,
    )
    from octop.infra.agents.settings.media_generation import (
        MEDIA_PROVIDER_PRESETS as MEDIA_PROVIDER_PRESETS,
    )
    from octop.infra.agents.settings.media_generation import (
        MediaGenerationSettings as MediaGenerationSettings,
    )
    from octop.infra.agents.settings.media_generation import (
        MediaGenerationSettingsStore as MediaGenerationSettingsStore,
    )
    from octop.infra.agents.settings.media_generation import (
        MediaProviderName as MediaProviderName,
    )
    from octop.infra.agents.settings.media_generation import (
        MediaProviderSettings as MediaProviderSettings,
    )
    from octop.infra.agents.settings.media_generation import (
        MediaProviderUpdate as MediaProviderUpdate,
    )


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    module = (
        "acp"
        if name == "ACPSettingsStore"
        else "langfuse"
        if name in {"LangfuseSettings", "LangfuseSettingsStore", "verify_langfuse_credentials"}
        else "media_generation"
    )
    return getattr(import_module(f"{__name__}.{module}"), name)


__all__ = [
    "ACPSettingsStore",
    "MEDIA_PROVIDER_PRESETS",
    "LangfuseSettings",
    "LangfuseSettingsStore",
    "MediaGenerationSettings",
    "MediaGenerationSettingsStore",
    "MediaProviderName",
    "MediaProviderSettings",
    "MediaProviderUpdate",
    "verify_langfuse_credentials",
]
