from __future__ import annotations

from typing import Any, Awaitable, Callable
from pathlib import Path

from backend.async_cleanup import to_thread_cancel_safe
from backend.config import SETTINGS_FILE, _update_settings_json
from backend.hooks.runtime import raise_if_config_change_blocked, run_config_change_hook

from backend.skills.marketplace import (
    import_local_skill,
    install_marketplace_skill,
    remove_user_skill,
)

MarketplaceLoader = Callable[..., Awaitable[dict[str, Any]]]


def skills_settings_payload(*, skill_manager: Any, workspace_root: str = "") -> dict[str, Any]:
    snapshot = skill_manager.snapshot(workspace_root or None)
    return {"skills": snapshot.list_settings(), "available_skills": snapshot.list_all(),
        "plugin_skills": snapshot.list_settings(source_level="plugin"),
        "workspace_root": workspace_root}


def skill_detail_payload(path: str, *, skill_manager: Any, workspace_root: str = "") -> dict[str, Any]:
    detail = skill_manager.snapshot(workspace_root or None).read_skill_detail(path)
    if detail is None:
        raise ValueError("Skill is not registered or its contents cannot be read.")
    return detail


async def update_skill_enabled(path: str, enabled: bool, *, skill_manager: Any, workspace_root: str = "") -> dict[str, Any]:
    requested = Path(path).expanduser().resolve()
    inventory = await to_thread_cancel_safe(skills_settings_payload, skill_manager=skill_manager, workspace_root=workspace_root)
    target = next((skill for skill in [*inventory["skills"], *inventory["plugin_skills"]]
        if Path(skill["path"]).resolve() == requested), None)
    if target is None:
        raise ValueError("Only an installed user or plugin skill can be enabled or disabled here.")
    hook = await run_config_change_hook(source="user_settings", file_path=str(SETTINGS_FILE))
    raise_if_config_change_blocked(hook, source="user_settings", file_path=str(SETTINGS_FILE))

    def apply(settings: dict[str, Any]) -> None:
        skills = settings.setdefault("skills", {})
        rules = [entry for entry in skills.get("config", [])
            if entry.get("path") is None or (SETTINGS_FILE.parent / Path(entry["path"]).expanduser()).resolve() != requested]
        rules.append({"path": str(requested), "enabled": enabled})
        skills["config"] = rules

    await to_thread_cancel_safe(_update_settings_json, apply)
    result = await to_thread_cancel_safe(skills_settings_payload, skill_manager=skill_manager, workspace_root=workspace_root)
    current = next(skill for skill in [*result["skills"], *result["plugin_skills"]]
        if Path(skill["path"]).resolve() == requested)
    if current["enabled"] != enabled:
        raise ValueError("Another configuration layer overrides this skill setting.")
    return result


def installed_skill_names(skill_manager: Any | None) -> set[str]:
    if skill_manager is None:
        return set()
    return {
        str(skill.get("name"))
        for skill in skill_manager.list_all()
        if isinstance(skill, dict)
    }


async def skills_marketplace_payload(
    *,
    skill_manager: Any | None,
    list_marketplace: MarketplaceLoader,
    force_refresh: bool = False,
) -> dict[str, Any]:
    payload = await list_marketplace(
        installed_names=installed_skill_names(skill_manager),
        force_refresh=force_refresh,
    )
    return {
        "skills": payload["skills"],
        "source_status": {"openai_skills": payload["source_status"]["openai_skills"]},
        "generated_at": payload["generated_at"],
    }


async def extensions_marketplace_payload(
    *,
    skill_manager: Any | None,
    mcp_manager: Any | None = None,
    list_marketplace: MarketplaceLoader,
    force_refresh: bool = False,
) -> dict[str, Any]:
    from backend.services.mcp_service import installed_connector_names

    return await list_marketplace(
        installed_names=installed_skill_names(skill_manager),
        installed_mcp_names=installed_connector_names(mcp_manager),
        force_refresh=force_refresh,
    )


async def install_skill_from_marketplace(skill_name: str, *, skill_manager: Any | None) -> dict[str, Any]:
    result = await install_marketplace_skill(skill_name)
    return {**result, "skills": refresh_skill_list(skill_manager)}


def remove_skill(skill_name: str, *, skill_manager: Any | None) -> dict[str, Any]:
    result = remove_user_skill(skill_name)
    return {**result, "skills": refresh_skill_list(skill_manager)}


def import_skill(source_path: str, *, skill_manager: Any | None) -> dict[str, Any]:
    result = import_local_skill(source_path)
    return {**result, "skills": refresh_skill_list(skill_manager)}


def refresh_skill_list(skill_manager: Any | None) -> list[dict[str, Any]]:
    if skill_manager is None:
        return []
    skill_manager.discover()
    return skill_manager.list_all()
