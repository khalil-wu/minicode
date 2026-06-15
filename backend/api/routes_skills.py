"""Skills marketplace and extension routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from backend.skills.marketplace import (
    install_marketplace_skill,
    list_extensions_marketplace,
    remove_user_skill,
)

from . import _state
from .models import SkillInstallRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/skills/marketplace")
async def get_skills_marketplace_api(response: Response) -> dict[str, Any]:
    """Compatibility endpoint for clients that only know about Skill marketplace entries."""
    response.headers["Cache-Control"] = "no-store"
    installed = set()
    if _state.bootstrap is not None and _state.bootstrap.skill_manager is not None:
        installed = {str(skill.get("name")) for skill in _state.bootstrap.skill_manager.list_all() if isinstance(skill, dict)}
    payload = await list_extensions_marketplace(installed_names=installed)
    return {
        "skills": payload["skills"],
        "source_status": {"openai_skills": payload["source_status"]["openai_skills"]},
        "generated_at": payload["generated_at"],
    }


@router.get("/api/extensions/marketplace")
async def get_extensions_marketplace_api(response: Response) -> dict[str, Any]:
    """List real Skills and MCP marketplace entries from upstream catalogs with safe fallbacks."""
    response.headers["Cache-Control"] = "no-store"
    installed = set()
    if _state.bootstrap is not None and _state.bootstrap.skill_manager is not None:
        installed = {str(skill.get("name")) for skill in _state.bootstrap.skill_manager.list_all() if isinstance(skill, dict)}
    return await list_extensions_marketplace(installed_names=installed)


@router.post("/api/skills/install")
async def install_skill_api(request: SkillInstallRequest, response: Response) -> dict[str, Any]:
    """Install an OpenAI curated Skill as a real SKILL.md file, then refresh Skill discovery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await install_marketplace_skill(request.skill_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to install Skill: {exc}") from exc

    skills: list[dict[str, Any]] = []
    if _state.bootstrap is not None and _state.bootstrap.skill_manager is not None:
        _state.bootstrap.skill_manager.discover()
        skills = _state.bootstrap.skill_manager.list_all()
    _state.invalidate_status_cache()

    return {
        **result,
        "skills": skills,
    }


@router.delete("/api/skills/{skill_name}")
async def remove_skill_api(skill_name: str, response: Response) -> dict[str, Any]:
    """Remove a user-installed Skill directory, then refresh Skill discovery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = remove_user_skill(skill_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to remove Skill: {exc}") from exc

    skills: list[dict[str, Any]] = []
    if _state.bootstrap is not None and _state.bootstrap.skill_manager is not None:
        _state.bootstrap.skill_manager.deactivate(result["skill"]["name"])
        _state.bootstrap.skill_manager.discover()
        skills = _state.bootstrap.skill_manager.list_all()
    _state.invalidate_status_cache()

    return {
        **result,
        "skills": skills,
    }
