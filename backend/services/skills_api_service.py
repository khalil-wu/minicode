from __future__ import annotations

from typing import Any, Awaitable, Callable

from backend.skills.marketplace import (
    install_marketplace_skill,
    remove_user_skill,
)

MarketplaceLoader = Callable[..., Awaitable[dict[str, Any]]]


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
) -> dict[str, Any]:
    payload = await list_marketplace(installed_names=installed_skill_names(skill_manager))
    return {
        "skills": payload["skills"],
        "source_status": {"openai_skills": payload["source_status"]["openai_skills"]},
        "generated_at": payload["generated_at"],
    }


async def extensions_marketplace_payload(
    *,
    skill_manager: Any | None,
    list_marketplace: MarketplaceLoader,
) -> dict[str, Any]:
    return await list_marketplace(installed_names=installed_skill_names(skill_manager))


async def install_skill_from_marketplace(skill_name: str, *, skill_manager: Any | None) -> dict[str, Any]:
    result = await install_marketplace_skill(skill_name)
    return {**result, "skills": refresh_skill_list(skill_manager)}


def remove_skill(skill_name: str, *, skill_manager: Any | None) -> dict[str, Any]:
    result = remove_user_skill(skill_name)
    if skill_manager is not None:
        skill_manager.deactivate(result["skill"]["name"])
    return {**result, "skills": refresh_skill_list(skill_manager)}


def refresh_skill_list(skill_manager: Any | None) -> list[dict[str, Any]]:
    if skill_manager is None:
        return []
    skill_manager.discover()
    return skill_manager.list_all()
