"""Skills domain: package validation, global packages, URL import, SkillHub market."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from octop.infra.skills.install import (
    SkillAlreadyExistsError,
    SkillInstallTarget,
    commit_skill_install,
    install_skill_from_skillhub,
    install_skill_from_url,
    prepare_skillhub_package,
    resolve_url_import,
)
from octop.infra.skills.skill_packages import (
    ResolvedSkillPackage,
    SkillPackageError,
    SkillPackageTooLarge,
    normalize_skill_files,
    resolve_skill_package,
    resolve_workspace_uploads,
    validate_skill_slug,
)

if TYPE_CHECKING:
    from octop.infra.skills.skill_package_store import SkillPackageStore


def __getattr__(name: str) -> Any:
    # Pure package validation is also used by the cloud-only application.
    # The legacy disk store imports Harness backends and must remain optional.
    if name == "SkillPackageStore":
        from octop.infra.skills.skill_package_store import SkillPackageStore

        return SkillPackageStore
    raise AttributeError(name)


__all__ = [
    "ResolvedSkillPackage",
    "SkillAlreadyExistsError",
    "SkillInstallTarget",
    "SkillPackageError",
    "SkillPackageStore",
    "SkillPackageTooLarge",
    "commit_skill_install",
    "install_skill_from_skillhub",
    "install_skill_from_url",
    "normalize_skill_files",
    "prepare_skillhub_package",
    "resolve_skill_package",
    "resolve_url_import",
    "resolve_workspace_uploads",
    "validate_skill_slug",
]
