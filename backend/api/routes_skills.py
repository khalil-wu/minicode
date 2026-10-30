"""Skills marketplace and extension routes."""

from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe

import asyncio
import logging
from typing import Any
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.skills.marketplace import bundled_skill_detail, list_extensions_marketplace
from backend.services.skills_api_service import (
    extensions_marketplace_payload,
    install_skill_from_marketplace,
    remove_skill,
    import_skill,
    skills_marketplace_payload,
    skills_settings_payload,
    skill_detail_payload,
    update_skill_enabled,
)
from backend.plugins.package import package_plugin_directory, validate_plugin_directory
from backend.hooks.runtime import ConfigChangeHookBlocked
from backend.plugins.policy import PluginSettingsError
from backend.services.plugin_settings_service import (
    get_plugin_settings,
    import_plugin_from_path,
    remove_plugin,
    resolve_plugin_asset,
    update_plugin_enabled,
)
from backend.plugins.manager import MarketplaceRegistry, PluginManager
from backend.plugins.materializer import MaterializationError
from backend.config import load_config_layer_stack
from backend.config import SETTINGS_FILE
from backend.hooks.runtime import run_config_change_hook

from . import _state
from .models import (
    PluginImportRequest,
    PluginInstallRequest,
    PluginMarketplaceRequest,
    PluginMarketplaceRefreshRequest,
    PluginPackageRequest,
    PluginStateUpdateRequest,
    PluginValidateRequest,
    SkillInstallRequest,
    SkillImportRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SkillEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    enabled: bool
    workspace_root: str = ""


def _settings_skill_manager():
    if _state.bootstrap is None or _state.bootstrap.skill_manager is None:
        raise HTTPException(503, detail="Skill inventory is unavailable")
    return _state.bootstrap.skill_manager


@router.get("/api/skills/settings")
async def get_skill_settings_api(response: Response, workspace_root: str = "", include_projects: bool = False) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await to_thread_cancel_safe(skills_settings_payload,
        skill_manager=_settings_skill_manager(), workspace_root=workspace_root, include_projects=include_projects)


@router.put("/api/skills/settings")
async def update_skill_settings_api(request: SkillEnabledRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await update_skill_enabled(request.path, request.enabled,
            skill_manager=_settings_skill_manager(), workspace_root=request.workspace_root)
    except (ValueError, ConfigChangeHookBlocked) as error:
        raise HTTPException(400, detail=str(error)) from error
    _state.invalidate_status_cache()
    return result


@router.get("/api/skills/detail")
async def get_skill_detail_api(
    response: Response,
    skill_path: str = Query(..., min_length=1),
    workspace_root: str = "",
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await to_thread_cancel_safe(skill_detail_payload, skill_path,
            skill_manager=_settings_skill_manager(), workspace_root=workspace_root)
    except ValueError as error:
        raise HTTPException(404, detail=str(error)) from error


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
    workspace_root: str = Query(""),
) -> FileResponse:
    """Serve metadata icons only for exact Skills in the discovered catalog."""
    _ = asset_token
    manager = _state.bootstrap.skill_manager if _state.bootstrap is not None else None
    root = Path(workspace_root).expanduser().resolve() if workspace_root.strip() else None
    if root is not None:
        from backend.workspace.trust import is_workspace_trusted

        if not root.is_dir():
            raise HTTPException(status_code=409, detail="Workspace folder does not exist.")
        if not is_workspace_trusted(root):
            raise HTTPException(status_code=403, detail="Workspace folder is not trusted.")
    asset = (
        await to_thread_cancel_safe(lambda: manager.snapshot(root).resolve_asset(skill_path, variant))
        if manager is not None else None
    )
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
    workspace_root: str | None = None,
) -> FileResponse:
    """Serve an official plugin interface asset from an installed bundle."""
    _ = asset_token
    stack, root = _plugin_scope(workspace_root)
    asset = resolve_plugin_asset(plugin_path, variant, config_stack=stack, workspace_root=root)
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
    """List the bundled public Skill metadata and current installed state."""
    response.headers["Cache-Control"] = "no-store"
    return await skills_marketplace_payload(
        skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
    )


@router.get("/api/skills/marketplace/{skill_name}")
async def get_bundled_skill_detail_api(skill_name: str, response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await to_thread_cancel_safe(bundled_skill_detail, skill_name)
    except KeyError as error:
        raise HTTPException(404, detail="Skill not found") from error
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from error


@router.get("/api/extensions/marketplace")
async def get_extensions_marketplace_api(response: Response, refresh: bool = False) -> dict[str, Any]:
    """List upstream Skills and MCP catalog entries with explicit source availability."""
    response.headers["Cache-Control"] = "no-store"
    from backend.api.routes_health import get_mcp_manager

    return await extensions_marketplace_payload(
        skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        mcp_manager=get_mcp_manager(),
        list_marketplace=list_extensions_marketplace,
        force_refresh=refresh,
    )


@router.get("/api/plugins")
def list_plugins_api(response: Response, workspace_root: str | None = None) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    stack, _ = _plugin_scope(workspace_root)
    payload = get_plugin_settings(config_stack=stack)
    from backend.config_layers import ConfigLayerSource
    origins = stack.origins()
    for plugin in payload["plugins"]:
        origin = origins.get(f"plugins.{plugin['id']}.enabled")
        plugin["user_config_writable"] = origin is None or origin.source.precedence <= ConfigLayerSource("user").precedence
    return payload


def _plugin_scope(workspace_root: str | None):
    if workspace_root is None:
        from backend.workspace.state import get_explicit_active_workspace_root
        root = get_explicit_active_workspace_root()
        return load_config_layer_stack(cwd=root), root
    root = Path(workspace_root).expanduser().resolve() if workspace_root.strip() else None
    if root is not None and not root.is_dir():
        raise HTTPException(status_code=409, detail="Workspace folder does not exist.")
    return load_config_layer_stack(cwd=root), root


@router.get("/api/plugins/marketplaces")
def list_plugin_marketplaces_api(response: Response, workspace_root: str | None = None) -> dict[str, Any]:
    """List registered marketplace sources and their materialized metadata."""
    response.headers["Cache-Control"] = "no-store"
    stack, root = _plugin_scope(workspace_root)
    try:
        from backend.plugins.policy import _plugin_policy_from_stack

        policy = _plugin_policy_from_stack(stack)
        registry = MarketplaceRegistry(config_stack=stack, workspace_root=root, include_bundled=True)
        return {"marketplaces": registry.list(policy=policy)}
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Plugin marketplace listing failed: {exc}") from exc


@router.get("/api/plugins/reconcile")
def reconcile_plugins_api(response: Response) -> dict[str, Any]:
    """Return one integrity report for marketplace/store/runtime state."""
    response.headers["Cache-Control"] = "no-store"
    stack = load_config_layer_stack()
    from backend.plugins.policy import _plugin_policy_from_stack

    policy = _plugin_policy_from_stack(stack)
    manager = PluginManager(config_stack=stack)
    return {
        "marketplaces": MarketplaceRegistry().reconcile(policy=policy),
        "plugins": manager.reconcile(),
    }


@router.post("/api/plugins/marketplaces")
def add_plugin_marketplace_api(
    request: PluginMarketplaceRequest,
    response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    stack, _ = _plugin_scope(request.workspace_root)
    try:
        from backend.plugins.policy import _plugin_policy_from_stack

        policy = _plugin_policy_from_stack(stack)
        registry = MarketplaceRegistry()
        if isinstance(request.source, str):
            record, already_added = registry.add_source(request.source, ref_name=request.ref_name, sparse_paths=request.sparse_paths or (), policy=policy)
        else:
            record = registry.add(request.name, request.source, policy=policy)
            already_added = False
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (ValueError, MaterializationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Plugin policy unavailable: {exc}") from exc
    return {"marketplace": record, "already_added": already_added}


@router.delete("/api/plugins/marketplaces/{marketplace_name}")
def remove_plugin_marketplace_api(marketplace_name: str, response: Response, workspace_root: str | None = None) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    stack, root = _plugin_scope(workspace_root)
    try:
        from backend.plugins.policy import _plugin_policy_from_stack

        policy = _plugin_policy_from_stack(stack)
        registry = MarketplaceRegistry(config_stack=stack, workspace_root=root, include_bundled=True)
        removed = registry.remove(marketplace_name, policy=policy)
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Plugin policy unavailable: {exc}") from exc
    if removed is None:
        raise HTTPException(status_code=404, detail="Marketplace not found")
    return {"removed": removed, "marketplaces": registry.list(policy=policy)}


@router.post("/api/plugins/marketplaces/{marketplace_name}/refresh")
def refresh_plugin_marketplace_api(
    marketplace_name: str,
    response: Response,
    request: PluginMarketplaceRefreshRequest | None = None,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    # ``request`` is accepted for clients that send a JSON body; the path is
    # authoritative and avoids two competing marketplace identities.
    del request
    response.headers["Cache-Control"] = "no-store"
    stack, root = _plugin_scope(workspace_root)
    try:
        from backend.plugins.policy import _plugin_policy_from_stack

        policy = _plugin_policy_from_stack(stack)
        refreshed = MarketplaceRegistry(config_stack=stack, workspace_root=root, include_bundled=True).refresh(marketplace_name, policy=policy)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Marketplace not found") from exc
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"marketplace": refreshed}


@router.post("/api/plugins/install")
async def install_plugin_api(request: PluginInstallRequest, response: Response) -> dict[str, Any]:
    """Materialize a local source or one entry from a registered marketplace."""
    response.headers["Cache-Control"] = "no-store"
    try:
        if request.plugin_name:
            stack, root = await to_thread_cancel_safe(_plugin_scope, request.workspace_root)
            manager = PluginManager(config_stack=stack, workspace_root=root)
            result = await manager.install_marketplace_plugin(
                request.marketplace,
                request.plugin_name,
                overwrite=request.overwrite,
                settings_file=SETTINGS_FILE,
                config_change_hook=run_config_change_hook,
                refresh=request.refresh_marketplace,
            )
        else:
            result = await import_plugin_from_path(
                request.source_path,
                overwrite=request.overwrite,
                marketplace=request.marketplace,
                settings_file=SETTINGS_FILE,
                config_change_hook=run_config_change_hook,
            )
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _state.bootstrap is not None:
        result["runtime_refresh"] = await _refresh_plugin_runtime_state()
    _state.invalidate_status_cache()
    return result


@router.put("/api/plugins/{plugin_name}/state")
async def update_plugin_state_api(plugin_name: str, request: PluginStateUpdateRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    stack, _ = _plugin_scope(request.workspace_root)
    from backend.config_layers import ConfigLayerSource
    origin = stack.origins().get(f"plugins.{plugin_name}.enabled")
    if origin is not None and origin.source.precedence > ConfigLayerSource("user").precedence:
        raise HTTPException(status_code=409, detail="Plugin state is controlled by the current workspace configuration.")
    try:
        result = await update_plugin_enabled(
            plugin_name,
            request.enabled,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
            config_stack=stack,
        )
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    from backend.config import load_config

    config = await to_thread_cancel_safe(load_config)
    if _state.bootstrap is not None:
        if config is not None:
            _state.bootstrap.config = config
        runtime_refresh = await _refresh_plugin_runtime_state()
        if isinstance(result, dict):
            result["runtime_refresh"] = runtime_refresh
    _state.invalidate_status_cache()
    return result


@router.post("/api/plugins/runtime/refresh")
async def refresh_plugin_runtime_api(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"runtime_refresh": await _refresh_plugin_runtime_state()}


@router.post("/api/plugins/import")
async def import_plugin_api(request: PluginImportRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await import_plugin_from_path(
            request.source_path,
            overwrite=request.overwrite,
            marketplace=request.marketplace,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
        )
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if _state.bootstrap is not None:
        from backend.config import load_config

        _state.bootstrap.config = await to_thread_cancel_safe(load_config)
        runtime_refresh = await _refresh_plugin_runtime_state()
        if isinstance(result, dict):
            result["runtime_refresh"] = runtime_refresh
    _state.invalidate_status_cache()
    return result


@router.delete("/api/plugins/{plugin_name}")
async def remove_plugin_api(plugin_name: str, response: Response, workspace_root: str | None = None) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    stack, _ = _plugin_scope(workspace_root)
    try:
        result = await remove_plugin(
            plugin_name,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
            config_stack=stack,
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
        return await asyncio.to_thread(validate_plugin_directory, request.source_path)
    except PluginSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/api/plugins/package")
async def package_plugin_api(request: PluginPackageRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await asyncio.to_thread(
            package_plugin_directory,
            request.source_path,
            request.output_dir,
        )
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
        result = await to_thread_cancel_safe(
            remove_skill,
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


@router.post("/api/skills/import")
async def import_skill_api(request: SkillImportRequest, response: Response) -> dict[str, Any]:
    """Import a local SKILL.md into MiniCode's private extension directory."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await to_thread_cancel_safe(
            import_skill,
            request.source_path,
            skill_manager=_state.bootstrap.skill_manager if _state.bootstrap is not None else None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _state.invalidate_status_cache()
    return result


async def _refresh_plugin_runtime_state() -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": True,
        "warnings": [],
        "errors": [],
        "refreshed": [],
        "sessions": [],
    }
    bootstrap = _state.bootstrap
    if bootstrap is None:
        return {
            "ok": False,
            "warnings": [],
            "errors": ["Plugin settings changed before runtime initialization"],
            "refreshed": [],
            "sessions": [],
        }
    if bootstrap.skill_manager is not None:
        try:
            bootstrap.skill_manager.discover()
            report["refreshed"].append("bootstrap.skills")
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(f"Bootstrap skill refresh failed: {exc}")
            logger.warning(
                "Failed to refresh bootstrap skills after plugin state update",
                exc_info=True,
            )
    if bootstrap.mcp_manager is not None:
        try:
            reload_managers = getattr(bootstrap, "reload_mcp_managers", None)
            if callable(reload_managers):
                await reload_managers()
            else:
                await bootstrap.mcp_manager.reload_config()
            report["refreshed"].append("bootstrap.mcp")
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(f"MCP refresh failed: {exc}")
            logger.warning("Failed to refresh plugin MCP servers", exc_info=True)

    iter_sessions = getattr(_state.ws_manager, "iter_sessions", None)
    sessions = (
        list(iter_sessions())
        if callable(iter_sessions)
        else list(getattr(_state.ws_manager, "_sessions", {}).values())
    )
    for session in sessions:
        session_id = str(getattr(session, "session_id", "") or "")
        session_report: dict[str, Any] = {
            "session_id": session_id,
            "skills_refreshed": False,
            "runtime": None,
        }
        skill_manager = getattr(session, "skill_manager", None)
        if skill_manager is not None:
            try:
                skill_manager.discover()
                session_report["skills_refreshed"] = True
                report["refreshed"].append(f"session:{session_id}:skills")
            except Exception as exc:
                report["ok"] = False
                report["errors"].append(
                    f"Session {session_id} skill refresh failed: {exc}"
                )
                logger.warning(
                    "Failed to refresh session skills for session %s",
                    session_id,
                    exc_info=True,
                )

        refresh_runtime = getattr(session, "refresh_plugin_runtime_state", None)
        if callable(refresh_runtime):
            try:
                runtime_report = await refresh_runtime(reason="plugin.settings")
                session_report["runtime"] = runtime_report
                report["refreshed"].append(f"session:{session_id}:plugin_runtime")
                if not bool(runtime_report.get("ok", False)):
                    report["ok"] = False
                report["warnings"].extend(
                    f"Session {session_id}: {warning}"
                    for warning in runtime_report.get("warnings", [])
                )
                report["errors"].extend(
                    f"Session {session_id}: {error}"
                    for error in runtime_report.get("errors", [])
                )
            except Exception as exc:
                report["ok"] = False
                report["errors"].append(
                    f"Session {session_id} plugin runtime refresh failed: {exc}"
                )
                logger.warning(
                    "Failed to refresh plugin runtime for session %s",
                    session_id,
                    exc_info=True,
                )
        else:
            report["warnings"].append(
                f"Session {session_id} has no plugin runtime refresh capability"
            )
        report["sessions"].append(session_report)
    return report
