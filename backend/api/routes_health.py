"""Health, status, doctor diagnostics, and guidelines endpoints."""

from __future__ import annotations

import logging
import asyncio
import time
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Response

from backend.agent.instruction_discovery import load_project_guideline_bundle
from backend.config import PROJECT_ROOT, STATE_ROOT, DATA_ROOT
from backend.workspace.state import get_active_workspace_root
from backend.services.health_service import (
    build_capability_unavailable_payload,
    build_capability_status_payload,
    build_doctor_payload,
    build_git_doctor_payload,
    build_health_payload,
    build_llm_status_payload,
    build_status_payload,
    cached_payload,
)

from . import _state
from backend.services.tool_registry_factory import build_tool_registry as _build_tool_registry

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Health / status endpoints ──


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check endpoint."""
    return build_health_payload(bootstrap=_state.bootstrap, active_sessions=_state.ws_manager.active_count)


@router.get("/readyz")
async def readiness_check(response: Response) -> dict[str, Any]:
    """Report whether the backend composition root can accept sessions."""
    ready = bool(
        _state.bootstrap is not None
        and getattr(_state.bootstrap, "config", None) is not None
    )
    response.status_code = 200 if ready else 503
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok" if ready else "starting", "ready": ready}


@router.get("/healthz")
async def liveness_check(response: Response) -> dict[str, str]:
    """Report that the HTTP process is alive independently of optional services."""
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok"}


@router.get("/api/status")
async def system_status(response: Response) -> dict[str, Any]:
    """Return runtime status for the desktop sidebar."""
    response.headers["Cache-Control"] = "public, max-age=30"
    return {
        **_get_cached_status_payload(),
        "llm": _build_llm_status_payload(),
    }


@router.get("/api/doctor")
async def doctor_status(response: Response) -> dict[str, Any]:
    """Aggregate desktop workbench diagnostics for the right-side Doctor panel."""
    response.headers["Cache-Control"] = "no-store"
    return await _build_doctor_payload()


def _build_sandbox_status_payload() -> dict[str, Any]:
    from backend.permissions.context import PermissionContext
    from backend.permissions.profiles import refresh_native_os_sandbox, sandbox_capability_for_context

    # Initialization changes OS/runtime state. Re-probe the same default
    # managed profile used at startup and replace its cached detection.
    refresh_native_os_sandbox(workspace_root=PROJECT_ROOT)
    capability = sandbox_capability_for_context(
        PROJECT_ROOT, PermissionContext(mode="confirm", workspace_root=PROJECT_ROOT),
    )
    available = capability["backend_available"] is True and capability["filesystem_isolated"] is True
    native_available = False
    setup_supported = False
    sandbox_executable = None
    if sys.platform == "win32":
        from backend.sandbox.windows_native import _candidate_executables, discover_runtime

        runtime, _native_reason = discover_runtime()
        native_available = runtime is not None
        sandbox_executable = next((str(path.resolve()) for path in _candidate_executables() if path.is_file()), None)
        setup_supported = sandbox_executable is not None
    setup_required = sys.platform == "win32" and not available and not native_available
    description = (
        "受限命令执行环境已就绪。" if available
        else "Windows 执行环境已初始化，当前策略仍受限制。" if native_available
        else "需要初始化 Windows 命令执行环境。" if setup_supported
        else "Windows 隔离组件尚未安装。" if sys.platform == "win32"
        else "当前系统没有可用的受限命令执行环境。"
    )
    return {
        "platform": sys.platform,
        "available": available,
        "backend": capability["backend"],
        "reason": capability["reason"],
        "description": description,
        "setup_required": setup_required,
        "setup_supported": setup_supported,
        "native_available": native_available,
        "permission_mode": "confirm",
        "state_root": str(STATE_ROOT),
        "sandbox_home": str(Path(str(os.environ.get("MINICODE_WINDOWS_SANDBOX_HOME") or "").strip() or DATA_ROOT / "windows-sandbox").expanduser().resolve()),
        "sandbox_executable": sandbox_executable,
    }


@router.get("/api/sandbox/status")
async def sandbox_status(response: Response) -> dict[str, Any]:
    """Read actual managed capability and refresh its host detection cache."""
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(_build_sandbox_status_payload)


@router.get("/api/guidelines")
async def project_guidelines_status(
    response: Response,
    workspace_dir: str | None = Query(default=None),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    bundle = load_project_guideline_bundle(workspace_dir=workspace_dir)
    return bundle.to_dict()


# ── Status payload builders ──


def _get_cached_status_payload() -> dict[str, Any]:
    if _state.bootstrap is not None:
        return _state.bootstrap.build_status_payload()

    now = time.monotonic()
    if _state.status_cache_payload is not None and now < _state.status_cache_expires_at:
        return _state.status_cache_payload

    payload, expires_at = cached_payload(
        now=now,
        cached=_state.status_cache_payload,
        expires_at=_state.status_cache_expires_at,
        ttl=_state.STATUS_CACHE_TTL_SECONDS,
        builder=_build_status_payload,
    )
    _state.status_cache_payload = payload
    _state.status_cache_expires_at = expires_at
    return payload


def _build_status_payload() -> dict[str, Any]:
    return build_status_payload(
        bootstrap=_state.bootstrap,
        runtime_snapshot=_state.ws_manager.runtime_snapshot(),
        capability_payload=_build_capability_status_payload(),
    )

def _build_capability_status_payload() -> dict[str, Any]:
    now = time.monotonic()
    if _state.capability_cache_payload is not None and now < _state.capability_cache_expires_at:
        return _state.capability_cache_payload
    try:
        snapshot = build_capability_status_payload(
            bootstrap=_state.bootstrap,
            build_tool_registry=_build_tool_registry,
        )
        _state.capability_cache_payload = snapshot
        _state.capability_cache_expires_at = now + _state.CAPABILITY_CACHE_TTL_SECONDS
        return snapshot
    except Exception as exc:
        logger.warning("Capability snapshot build failed: %s", exc)
        result = build_capability_unavailable_payload()
        result.update(
            {
                "status": "error",
                "error": {
                    "type": "capability_snapshot_failed",
                    "detail": type(exc).__name__,
                },
            }
        )
        _state.capability_cache_payload = result
        _state.capability_cache_expires_at = now + 5
        return result

def _build_llm_status_payload() -> dict[str, Any]:
    return build_llm_status_payload()

async def _build_doctor_payload() -> dict[str, Any]:
    workspace_root = get_active_workspace_root(PROJECT_ROOT).resolve()
    git_payload = await asyncio.to_thread(build_git_doctor_payload, workspace_root)
    return build_doctor_payload(
        workspace_root=workspace_root,
        runtime_snapshot=_state.ws_manager.runtime_snapshot(),
        active_sessions=_state.ws_manager.active_count,
        llm=_build_llm_status_payload(),
        mcp_status=get_mcp_status(),
        capabilities=_build_capability_status_payload(),
        preview_processes=_build_preview_doctor_payload(),
        git_payload=git_payload,
    )

def _build_preview_doctor_payload() -> list[dict[str, Any]]:
    try:
        from backend.preview import all_running_preview_processes

        return [process.to_dict() for process in all_running_preview_processes()]
    except Exception as exc:
        logger.warning("Preview doctor payload failed: %s", exc)
        return []


def get_mcp_status() -> list[dict[str, Any]]:
    """Return MCP server status for websocket handlers."""
    if _state.bootstrap is not None:
        return _state.bootstrap.get_mcp_status()
    return []


def get_mcp_manager():
    """Return the MCP manager instance."""
    if _state.bootstrap is not None:
        return _state.bootstrap.mcp_manager
    return None
