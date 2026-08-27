from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any



@dataclass
class SkillInstallResult:
    notice: str
    skills: list[dict[str, Any]]
    installed: bool = False


def list_skills(skill_manager: Any | None) -> list[dict[str, Any]]:
    if not skill_manager:
        return []
    list_all = getattr(skill_manager, "list_all", None)
    if not callable(list_all):
        return []
    return list(list_all() or [])


async def install_skill(skill_manager: Any | None, name: str) -> SkillInstallResult:
    skill_name = str(name or "").strip()
    if not skill_name:
        raise ValueError("Skill name is required")
    if not skill_manager:
        return SkillInstallResult(
            notice=f"Skill '{skill_name}' registered (skill manager not available)",
            skills=[],
            installed=False,
        )
    install = getattr(skill_manager, "install", None)
    if not callable(install):
        # cc's CLI install path exists for every skill source; a runtime
        # without install support must fail loudly instead of reporting a
        # fake success notice.
        raise RuntimeError(
            "Skill installation is unavailable in this runtime; "
            "use the HTTP skill install endpoint instead."
        )
    result = install(skill_name)
    if hasattr(result, "__await__"):
        await result
    return SkillInstallResult(
        notice=f"Skill '{skill_name}' installed successfully",
        skills=list_skills(skill_manager),
        installed=True,
    )


def list_commands(
    workspace_root: str | Path | None = None,
    *,
    resolve_active_workspace: bool = True,
) -> list[dict[str, Any]]:
    from backend.commands.catalog import get_enabled_composer_command_catalog

    return list(
        get_enabled_composer_command_catalog(
            workspace_root,
            resolve_active_workspace=resolve_active_workspace,
        )
    )


def _installed_skill_names(skill_manager: Any | None) -> set[str]:
    if not skill_manager:
        return set()
    loader = getattr(skill_manager, "_loader", None)
    list_skill_names = getattr(loader, "list_skill_names", None)
    if callable(list_skill_names):
        return {str(name) for name in list_skill_names()}
    return {
        str(skill.get("name"))
        for skill in list_skills(skill_manager)
        if isinstance(skill, dict) and skill.get("name")
    }
