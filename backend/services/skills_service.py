from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.skill_events import skill_process_event


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
        raise RuntimeError("Skill manager does not support installation")
    result = install(skill_name)
    if hasattr(result, "__await__"):
        await result
    return SkillInstallResult(
        notice=f"Skill '{skill_name}' installed successfully",
        skills=list_skills(skill_manager),
        installed=True,
    )


def list_skill_marketplace(skill_manager: Any | None) -> list[dict[str, Any]]:
    from backend.skills.marketplace import CURATED_SKILLS

    installed_names = _installed_skill_names(skill_manager)
    marketplace_skills: list[dict[str, Any]] = []
    for name, info in CURATED_SKILLS.items():
        marketplace_skills.append({
            "name": name,
            "title": info.get("title", name),
            "description": info.get("description", ""),
            "triggers": info.get("triggers", []),
            "installed": name in installed_names,
        })
    return marketplace_skills


def list_commands() -> list[dict[str, Any]]:
    from backend.commands.catalog import get_enabled_composer_command_catalog

    return list(get_enabled_composer_command_catalog())


def toggle_skill_events(skill_manager: Any | None, skill_name: str, *, activate: bool) -> list[AgentEvent]:
    clean_name = str(skill_name or "").strip()
    if not skill_manager or not clean_name:
        return [AgentEvent.error("Skills are unavailable", recoverable=True)]

    events: list[AgentEvent] = []
    is_active = getattr(skill_manager, "is_active", None)
    already_active = (
        bool(is_active(clean_name))
        if callable(is_active)
        else clean_name in getattr(skill_manager, "_active", {})
    )
    if activate:
        events.append(skill_process_event(
            clean_name,
            lifecycle="selected",
            trigger_mode="explicit",
            reason="用户从 composer 显式加载该 skill",
            skill_manager=skill_manager,
        ))
        if already_active:
            events.append(skill_process_event(
                clean_name,
                lifecycle="skipped",
                trigger_mode="explicit",
                status="info",
                reason="Skill already active",
                skill_manager=skill_manager,
            ))
            events.append(AgentEvent(
                type="skill_activated",
                data={"skill_name": clean_name, "trigger_mode": "explicit"},
            ))
            return events

    get_active_names = getattr(skill_manager, "get_active_names", None)
    active_before = set(get_active_names() if callable(get_active_names) else [])
    success = skill_manager.activate(clean_name) if activate else skill_manager.deactivate(clean_name)
    if success:
        if activate and callable(get_active_names):
            active_after = set(get_active_names())
            for removed_name in sorted(active_before - active_after):
                events.append(skill_process_event(
                    removed_name,
                    lifecycle="skipped",
                    trigger_mode="explicit",
                    status="info",
                    reason=f"与 {clean_name} 冲突，已自动停用",
                    skill_manager=skill_manager,
                ))
        events.append(skill_process_event(
            clean_name,
            lifecycle="loaded" if activate else "unloaded",
            trigger_mode="explicit",
            status="completed" if activate else "info",
            reason="用户从 composer 显式加载该 skill" if activate else "用户从 composer 停用该 skill",
            skill_manager=skill_manager,
        ))
        events.append(AgentEvent(
            type="skill_activated" if activate else "skill_deactivated",
            data={"skill_name": clean_name, "trigger_mode": "explicit"},
        ))
        return events

    if not activate:
        events.append(skill_process_event(
            clean_name,
            lifecycle="skipped",
            trigger_mode="explicit",
            status="info",
            reason="Skill is not active",
            skill_manager=skill_manager,
        ))
        events.append(AgentEvent(
            type="skill_deactivated",
            data={"skill_name": clean_name, "trigger_mode": "explicit"},
        ))
        return events

    events.append(skill_process_event(
        clean_name,
        lifecycle="failed",
        trigger_mode="explicit",
        status="failed",
        reason=f"Skill '{clean_name}' activate failed",
        skill_manager=skill_manager,
    ))
    events.append(AgentEvent.error(f"Skill '{clean_name}' activate failed", recoverable=True))
    return events


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
