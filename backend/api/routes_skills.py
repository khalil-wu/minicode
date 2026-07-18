"""Skills marketplace and extension routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from backend.skills.marketplace import list_extensions_marketplace
from backend.services.skills_api_service import (
    extensions_marketplace_payload,
    install_skill_from_marketplace,
    remove_skill,
    skills_marketplace_payload,
)
from backend.services.plugin_settings_service import (
    PluginSettingsError,
    get_plugin_settings,
    import_plugin_from_path,
    package_plugin_directory,
    update_plugin_enabled,
    validate_plugin_directory,
)
from backend.config import SETTINGS_FILE
from backend.hooks.runtime import run_config_change_hook

from . import _state
from .models import PluginImportRequest, PluginPackageRequest, PluginStateUpdateRequest, PluginValidateRequest, SkillInstallRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/skills/marketplace")
async def get_skills_marketplace_api(response: Response) -> dict[str, Any]:
    """Compatibility endpoint for clients that only know about Skill marketplace entries."""
    response.headers["Cache-Control"] = "no-store"
    return await skills_marketplace_payload(
        skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        list_marketplace=list_extensions_marketplace,
    )


@router.get("/api/extensions/marketplace")
async def get_extensions_marketplace_api(response: Response) -> dict[str, Any]:
    """List real Skills and MCP marketplace entries from upstream catalogs with safe fallbacks."""
    response.headers["Cache-Control"] = "no-store"
    return await extensions_marketplace_payload(
        skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        list_marketplace=list_extensions_marketplace,
    )


@router.get("/api/plugins")
def list_plugins_api(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return get_plugin_settings()


@router.put("/api/plugins/{plugin_name}/state")
async def update_plugin_state_api(plugin_name: str, request: PluginStateUpdateRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await update_plugin_enabled(
            plugin_name,
            request.enabled,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
        )
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    from backend.config import load_config

    config = load_config()
    if _state.bootstrap is not None:
        if config is not None:
            _state.bootstrap.config = config
        _refresh_plugin_runtime_state()
    _state.invalidate_status_cache()
    return result


@router.post("/api/plugins/import")
async def import_plugin_api(request: PluginImportRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await import_plugin_from_path(
            request.source_path,
            overwrite=request.overwrite,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
        )
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if _state.bootstrap is not None:
        from backend.config import load_config

        _state.bootstrap.config = load_config()
        _refresh_plugin_runtime_state()
    _state.invalidate_status_cache()
    return result


@router.post("/api/plugins/validate")
async def validate_plugin_api(request: PluginValidateRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return validate_plugin_directory(request.source_path)
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/api/plugins/package")
async def package_plugin_api(request: PluginPackageRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return package_plugin_directory(request.source_path, request.output_dir)
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/api/skills/install")
async def install_skill_api(request: SkillInstallRequest, response: Response) -> dict[str, Any]:
    """Install an OpenAI curated Skill as a real SKILL.md file, then refresh Skill discovery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await install_skill_from_marketplace(
            request.skill_name,
            skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to install Skill: {exc}") from exc

    _state.invalidate_status_cache()
    return result


@router.delete("/api/skills/{skill_name}")
async def remove_skill_api(skill_name: str, response: Response) -> dict[str, Any]:
    """Remove a user-installed Skill directory, then refresh Skill discovery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = remove_skill(
            skill_name,
            skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to remove Skill: {exc}") from exc

    _state.invalidate_status_cache()
    return result


def _refresh_plugin_runtime_state() -> None:
    bootstrap = _state.bootstrap
    if bootstrap is None:
        return
    if bootstrap.skill_manager is not None:
        try:
            bootstrap.skill_manager.discover()
        except Exception:
            logger.debug("Failed to refresh bootstrap skills after plugin state update", exc_info=True)
    for session in list(getattr(_state.ws_manager, "_sessions", {}).values()):
        registry = getattr(session, "command_registry", None)
        if registry is not None:
            try:
                from backend.commands.slash_commands import refresh_slash_commands

                refresh_slash_commands(registry)
            except Exception:
                logger.debug("Failed to refresh slash commands for session %s", getattr(session, "session_id", ""), exc_info=True)
        skill_manager = getattr(session, "skill_manager", None)
        if skill_manager is not None:
            try:
                skill_manager.discover()
            except Exception:
                logger.debug("Failed to refresh session skills for session %s", getattr(session, "session_id", ""), exc_info=True)
