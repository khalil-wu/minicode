"""Skills marketplace and extension routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse

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
    remove_plugin,
    resolve_plugin_asset,
    update_plugin_enabled,
    validate_plugin_directory,
)
from backend.config import SETTINGS_FILE
from backend.hooks.runtime import run_config_change_hook

from . import _state
from .models import PluginImportRequest, PluginPackageRequest, PluginStateUpdateRequest, PluginValidateRequest, SkillInstallRequest

logger = logging.getLogger(__name__)

router = APIRouter()

_SKILL_ASSET_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}


@router.get("/api/skills/asset")
async def get_skill_asset_api(
    skill_path: str = Query(..., min_length=1),
    variant: str = Query("small", pattern="^(small|large)$"),
    asset_token: str | None = Query(None),
) -> FileResponse:
    """Serve metadata icons only for exact Skills in the discovered catalog."""
    _ = asset_token
    manager = _state.bootstrap.skill_manager if _state.bootstrap is not None else None
    asset = manager.resolve_asset(skill_path, variant) if manager is not None else None
    if asset is None:
        raise HTTPException(status_code=404, detail="Skill asset not found")
    media_type = _SKILL_ASSET_MEDIA_TYPES.get(asset.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=415, detail="Unsupported Skill asset type")
    return FileResponse(
        asset,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


@router.get("/api/plugins/asset")
async def get_plugin_asset_api(
    plugin_path: str = Query(..., min_length=1),
    variant: str = Query("logo", pattern="^(composer|logo|logo-dark)$"),
    asset_token: str | None = Query(None),
) -> FileResponse:
    """Serve an official plugin interface asset from an installed bundle."""
    _ = asset_token
    asset = resolve_plugin_asset(plugin_path, variant)
    if asset is None:
        raise HTTPException(status_code=404, detail="Plugin asset not found")
    media_type = _SKILL_ASSET_MEDIA_TYPES.get(asset.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=415, detail="Unsupported plugin asset type")
    return FileResponse(
        asset,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


@router.get("/api/skills/marketplace")
async def get_skills_marketplace_api(response: Response, refresh: bool = False) -> dict[str, Any]:
    """Compatibility endpoint for clients that only know about Skill marketplace entries."""
    response.headers["Cache-Control"] = "no-store"
    return await skills_marketplace_payload(
        skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        list_marketplace=list_extensions_marketplace,
        force_refresh=refresh,
    )


@router.get("/api/extensions/marketplace")
async def get_extensions_marketplace_api(response: Response, refresh: bool = False) -> dict[str, Any]:
    """List real Skills and MCP marketplace entries from upstream catalogs with safe fallbacks."""
    response.headers["Cache-Control"] = "no-store"
    from backend.api.routes_health import get_mcp_manager

    return await extensions_marketplace_payload(
        skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        mcp_manager=get_mcp_manager(),
        list_marketplace=list_extensions_marketplace,
        force_refresh=refresh,
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
        runtime_refresh = await _refresh_plugin_runtime_state()
        if isinstance(result, dict):
            result["runtime_refresh"] = runtime_refresh
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
        runtime_refresh = await _refresh_plugin_runtime_state()
        if isinstance(result, dict):
            result["runtime_refresh"] = runtime_refresh
    _state.invalidate_status_cache()
    return result


@router.delete("/api/plugins/{plugin_name}")
async def remove_plugin_api(plugin_name: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await remove_plugin(
            plugin_name,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
        )
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if _state.bootstrap is not None:
        runtime_refresh = await _refresh_plugin_runtime_state()
        result["runtime_refresh"] = runtime_refresh
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


async def _refresh_plugin_runtime_state() -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True, "warnings": [], "refreshed": []}
    bootstrap = _state.bootstrap
    if bootstrap is None:
        return {"ok": False, "warnings": ["Plugin settings changed before runtime initialization"], "refreshed": []}
    if bootstrap.skill_manager is not None:
        try:
            bootstrap.skill_manager.discover()
            report["refreshed"].append("bootstrap.skills")
        except Exception as exc:
            report["ok"] = False
            report["warnings"].append(f"Bootstrap skill refresh failed: {exc}")
            logger.warning("Failed to refresh bootstrap skills after plugin state update", exc_info=True)
    if bootstrap.mcp_manager is not None:
        try:
            await bootstrap.mcp_manager.reload_config()
            report["refreshed"].append("bootstrap.mcp")
        except Exception as exc:
            report["ok"] = False
            report["warnings"].append(f"MCP refresh failed: {exc}")
            logger.warning("Failed to refresh plugin MCP servers", exc_info=True)
    for session in list(getattr(_state.ws_manager, "_sessions", {}).values()):
        skill_manager = getattr(session, "skill_manager", None)
        if skill_manager is not None:
            try:
                skill_manager.discover()
                report["refreshed"].append(f"session:{getattr(session, 'session_id', '')}:skills")
            except Exception as exc:
                report["ok"] = False
                report["warnings"].append(
                    f"Session {getattr(session, 'session_id', '')} skill refresh failed: {exc}"
                )
                logger.warning("Failed to refresh session skills for session %s", getattr(session, "session_id", ""), exc_info=True)
    return report
