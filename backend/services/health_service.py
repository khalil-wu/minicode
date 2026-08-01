from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from backend.artifact.store import ArtifactStore
from backend.commands.catalog import get_enabled_composer_command_catalog
from backend.config import get_available_models, get_llm_provider, get_llm_settings_payload
from backend.feature_flags import feature_flags_payload
from backend.version import __version__


def build_health_payload(*, bootstrap: Any | None, active_sessions: int) -> dict[str, Any]:
    core_ready = bootstrap is not None and getattr(bootstrap, "config", None) is not None
    components: dict[str, Any] = {
        "core": {"status": "ok" if core_ready else "starting"},
        "memory": {
            "status": "ok" if bootstrap is not None and getattr(bootstrap, "file_memory", None) is not None else "degraded"
        },
        "skills": {
            "status": "ok" if bootstrap is not None and getattr(bootstrap, "skill_manager", None) is not None else "degraded"
        },
    }
    mcp_servers: list[dict[str, Any]] = []
    if bootstrap is not None and getattr(bootstrap, "mcp_manager", None):
        mcp_servers = bootstrap.mcp_manager.get_all_status()
    failed_mcp = sum(
        1
        for server in mcp_servers
        if str(server.get("phase") or server.get("status") or "").lower()
        in {"failed", "error", "auth_required", "expired"}
    )
    components["mcp"] = {
        "status": "degraded" if failed_mcp else "ok",
        "configured": len(mcp_servers),
        "failed": failed_mcp,
    }
    llm = add_llm_secret_status(build_llm_status_payload())
    components["llm"] = {
        "status": "configured" if str(llm.get("active_model") or "").strip() else "degraded",
        "provider": llm.get("provider") or "",
        "model": llm.get("active_model") or "",
        "has_api_key": llm.get("has_api_key"),
    }
    degraded = any(
        component.get("status") == "degraded"
        for component in components.values()
        if isinstance(component, dict)
    )
    result: dict[str, Any] = {
        "status": "starting" if not core_ready else "degraded" if degraded else "ok",
        "ready": core_ready,
        "version": __version__,
        "active_sessions": active_sessions,
        "components": components,
    }
    if bootstrap is not None:
        if mcp_servers:
            result["mcp_servers"] = mcp_servers
        if bootstrap.skill_manager:
            result["skills_count"] = len(bootstrap.skill_manager.list_all())
    return result


def build_status_payload(
    *,
    bootstrap: Any | None,
    runtime_snapshot: dict[str, Any],
    capability_payload: dict[str, Any],
) -> dict[str, Any]:
    mcp_mgr = bootstrap.mcp_manager if bootstrap else None
    skill_mgr = bootstrap.skill_manager if bootstrap else None
    file_mem = bootstrap.file_memory if bootstrap else None
    return {
        "mcp": mcp_mgr.get_all_status() if mcp_mgr else [],
        "skills": skill_mgr.list_all() if skill_mgr else [],
        "runtime": runtime_snapshot,
        "memory": {
            "available": file_mem is not None,
            "files": file_mem.list_files() if file_mem else [],
        },
        "capabilities": capability_payload,
    }


def build_capability_status_payload(
    *,
    bootstrap: Any | None,
    build_tool_registry: Callable[[ArtifactStore], Any],
) -> dict[str, Any]:
    if bootstrap is not None:
        snapshot = bootstrap.build_capability_snapshot()
    else:
        registry = build_tool_registry(ArtifactStore())
        snapshot = registry.build_snapshot()
    snapshot["composer_commands"] = get_enabled_composer_command_catalog()
    snapshot["feature_flags"] = feature_flags_payload()
    return snapshot


def build_capability_fallback_payload() -> dict[str, Any]:
    return {
        "version": 0,
        "tools": [],
        "commands": [],
        "skills": [],
        "composer_commands": get_enabled_composer_command_catalog(),
        "feature_flags": feature_flags_payload(),
    }


def build_llm_status_payload() -> dict[str, Any]:
    llm_settings = get_llm_settings_payload()
    current_model = str(llm_settings.get("active_model", "")).strip()
    return {
        "provider": get_llm_provider(),
        "current_model": current_model,
        "active_model": current_model,
        "available_models": get_available_models(),
    }


def add_llm_secret_status(llm: dict[str, Any]) -> dict[str, Any]:
    next_llm = dict(llm)
    llm_settings = get_llm_settings_payload()
    active_provider = str(llm_settings.get("provider") or next_llm.get("provider") or "")
    provider_payload = llm_settings.get(active_provider)
    if isinstance(provider_payload, dict):
        next_llm["has_api_key"] = bool(provider_payload.get("has_api_key"))
        next_llm["base_url"] = provider_payload.get("base_url") or ""
    return next_llm


def build_doctor_payload(
    *,
    workspace_root: Path,
    runtime_snapshot: dict[str, Any],
    active_sessions: int,
    llm: dict[str, Any],
    mcp_status: list[dict[str, Any]],
    capabilities: dict[str, Any],
    preview_processes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "backend": {
            "status": "ok",
            "version": __version__,
            "active_sessions": active_sessions,
            "runtime": runtime_snapshot,
        },
        "llm": add_llm_secret_status(llm),
        "mcp": mcp_status,
        "workspace": {
            "root": str(workspace_root),
            "exists": workspace_root.exists(),
            "writable": os.access(workspace_root, os.W_OK),
        },
        "git": build_git_doctor_payload(workspace_root),
        "preview": {
            "count": len(preview_processes),
            "processes": preview_processes,
            "url": next((item.get("url") for item in preview_processes if item.get("url")), ""),
        },
        "terminal": {
            "active_sessions": active_sessions,
            "runtime": "desktop-or-websocket",
        },
        "capabilities": capabilities,
    }


def build_git_doctor_payload(workspace_root: Any) -> dict[str, Any]:
    try:
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        changes = [line for line in status_result.stdout.splitlines() if line.strip()]
        return {
            "available": branch_result.returncode == 0 or status_result.returncode == 0,
            "branch": branch_result.stdout.strip(),
            "changed": len(changes),
            "clean": len(changes) == 0,
            "error": branch_result.stderr.strip() or status_result.stderr.strip(),
        }
    except Exception as exc:
        return {"available": False, "branch": "", "changed": 0, "clean": False, "error": str(exc)}


def cached_payload(
    *,
    now: float,
    cached: dict[str, Any] | None,
    expires_at: float,
    ttl: float,
    builder: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], float]:
    if cached is not None and now < expires_at:
        return cached, expires_at
    payload = builder()
    return payload, now + ttl
