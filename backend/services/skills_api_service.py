from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable
from pathlib import Path

from backend.atomic_io import canonical_file_path_key
from backend.async_cleanup import to_thread_cancel_safe
from backend.config import SETTINGS_FILE, _update_settings_json
from backend.hooks.runtime import raise_if_config_change_blocked, run_config_change_hook

from backend.skills.marketplace import (
    bundled_skills_marketplace,
    import_local_skill,
    install_marketplace_skill,
    remove_user_skill,
    user_skill_uninstall_name,
)

MarketplaceLoader = Callable[..., Awaitable[dict[str, Any]]]


def skills_settings_payload(*, skill_manager: Any, workspace_root: str = "", include_projects: bool = False) -> dict[str, Any]:
    snapshot = skill_manager.snapshot(workspace_root or None)
    inventory = snapshot.list_settings()
    skills = [{**skill, "uninstall_name": user_skill_uninstall_name(skill["path"])}
        for skill in inventory if skill["source_level"] != "plugin"]
    if include_projects:
        from backend.workspace.recent_projects import RecentProjectStore

        roots: dict[str, tuple[str, str, str]] = {}
        for project in RecentProjectStore().list():
            for root in project.workspace_roots:
                roots.setdefault(canonical_file_path_key(root), (root, project.path, project.name))
        current_key = canonical_file_path_key(workspace_root) if workspace_root else ""
        if workspace_root and current_key not in roots:
            roots[current_key] = (workspace_root, workspace_root, Path(workspace_root).name)

        def scoped(skill: dict[str, Any], root: str, group_id: str, group_name: str) -> dict[str, Any]:
            return {**skill, "catalog_scope": {"workspace_root": root, "group_id": group_id, "group_name": group_name}}

        if current_key:
            root, group_id, group_name = roots[current_key]
            skills = [scoped(skill, root, group_id, group_name) if skill["source_level"] == "workspace" else skill for skill in skills]
        seen = {canonical_file_path_key(skill["path"]) for skill in skills}
        other_roots = [value for key, value in roots.items() if key != current_key]

        def project_skills(item: tuple[str, str, str]) -> list[dict[str, Any]]:
            root, group_id, group_name = item
            project = snapshot.snapshot(root, force_reload=True)
            return [scoped(skill, root, group_id, group_name) for skill in project.list_settings(source_level="workspace")]

        if other_roots:
            with ThreadPoolExecutor(max_workers=5) as pool:
                for entries in pool.map(project_skills, other_roots):
                    for skill in entries:
                        key = canonical_file_path_key(skill["path"])
                        if key not in seen:
                            seen.add(key)
                            skills.append(skill)
    return {"skills": skills, "available_skills": snapshot.list_all(),
        "plugin_skills": [skill for skill in inventory if skill["source_level"] == "plugin"],
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
        raise ValueError("Only a registered skill can be enabled or disabled here.")
    hook = await run_config_change_hook(source="user_settings", file_path=str(SETTINGS_FILE))
    raise_if_config_change_blocked(hook, source="user_settings", file_path=str(SETTINGS_FILE))

    def apply(settings: dict[str, Any]) -> None:
        skills = settings.setdefault("skills", {})
        rules = [entry for entry in skills.get("config", [])
            if entry.get("path") is None or (SETTINGS_FILE.parent / Path(entry["path"]).expanduser()).resolve() != requested]
        rules.append({"path": str(requested), "enabled": enabled})
        skills["config"] = rules

    await to_thread_cancel_safe(_update_settings_json, apply)
    return await to_thread_cancel_safe(skills_settings_payload, skill_manager=skill_manager, workspace_root=workspace_root)


def installed_skill_names(skill_manager: Any | None) -> set[str]:
    if skill_manager is None:
        return set()
    return {
        str(skill.get("name"))
        for skill in skill_manager.snapshot().list_settings()
        if isinstance(skill, dict)
    }


async def skills_marketplace_payload(
    *,
    skill_manager: Any | None,
) -> dict[str, Any]:
    return await to_thread_cancel_safe(lambda: bundled_skills_marketplace(installed_skill_names(skill_manager)))


async def extensions_marketplace_payload(
    *,
    skill_manager: Any | None,
    mcp_manager: Any | None = None,
    list_marketplace: MarketplaceLoader,
    force_refresh: bool = False,
) -> dict[str, Any]:
    from backend.services.mcp_service import installed_connector_names

    return await list_marketplace(
        installed_names=await to_thread_cancel_safe(installed_skill_names, skill_manager),
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
