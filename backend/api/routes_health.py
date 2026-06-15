"""Health, status, doctor diagnostics, and guidelines endpoints."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

from fastapi import APIRouter, Query, Response

from backend.agent.claude_md import load_project_guideline_bundle
from backend.artifact.store import ArtifactStore
from backend.commands.catalog import get_enabled_composer_command_catalog
from backend.config import (
    PROJECT_ROOT,
    get_available_models,
    get_llm_provider,
    get_llm_settings_payload,
)
from backend.workspace.state import get_active_workspace_root

from . import _state
from .tool_registry import _build_tool_registry

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Health / status endpoints ──


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check endpoint."""
    bootstrap = _state.bootstrap
    result: dict[str, Any] = {
        "status": "ok",
        "version": "0.2.0",
        "active_sessions": _state.ws_manager.active_count,
    }

    if bootstrap is not None:
        # MCP status
        if bootstrap.mcp_manager:
            result["mcp_servers"] = bootstrap.mcp_manager.get_all_status()

        # Skills status
        if bootstrap.skill_manager:
            result["skills_count"] = len(bootstrap.skill_manager.list_all())

        # RAG status
        if bootstrap.rag_pipeline:
            result["rag"] = bootstrap.rag_pipeline.stats

    return result


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
    return _build_doctor_payload()


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

    payload = _build_status_payload()
    _state.status_cache_payload = payload
    _state.status_cache_expires_at = now + _state.STATUS_CACHE_TTL_SECONDS
    return payload


def _build_status_payload() -> dict[str, Any]:
    bootstrap = _state.bootstrap
    mcp_mgr = bootstrap.mcp_manager if bootstrap else None
    skill_mgr = bootstrap.skill_manager if bootstrap else None
    file_mem = bootstrap.file_memory if bootstrap else None
    rag = bootstrap.rag_pipeline if bootstrap else None
    return {
        "mcp": mcp_mgr.get_all_status() if mcp_mgr else [],
        "skills": skill_mgr.list_all() if skill_mgr else [],
        "runtime": _state.ws_manager.runtime_snapshot(),
        "memory": {
            "available": file_mem is not None,
            "files": file_mem.list_files() if file_mem else [],
        },
        "rag": rag.stats if rag else {"available": False},
        "capabilities": _build_capability_status_payload(),
    }


def _build_capability_status_payload() -> dict[str, Any]:
    now = time.monotonic()
    if _state.capability_cache_payload is not None and now < _state.capability_cache_expires_at:
        return _state.capability_cache_payload
    try:
        bootstrap = _state.bootstrap
        if bootstrap is not None:
            snapshot = bootstrap.build_capability_snapshot()
        else:
            registry = _build_tool_registry(ArtifactStore())
            snapshot = registry.build_snapshot()
        snapshot["composer_commands"] = get_enabled_composer_command_catalog()
        _state.capability_cache_payload = snapshot
        _state.capability_cache_expires_at = now + _state.CAPABILITY_CACHE_TTL_SECONDS
        return snapshot
    except Exception as exc:
        logger.warning("Capability snapshot build failed: %s", exc)
        result = {
            "version": 0,
            "tools": [],
            "commands": [],
            "skills": [],
            "composer_commands": get_enabled_composer_command_catalog(),
        }
        _state.capability_cache_payload = result
        _state.capability_cache_expires_at = now + 5
        return result


def _build_llm_status_payload() -> dict[str, Any]:
    llm_settings = get_llm_settings_payload()
    current_model = str(llm_settings.get("active_model", "")).strip()
    return {
        "provider": get_llm_provider(),
        "current_model": current_model,
        "active_model": current_model,
        "available_models": get_available_models(),
    }


def _build_doctor_payload() -> dict[str, Any]:
    workspace_root = get_active_workspace_root(PROJECT_ROOT).resolve()
    git_status = _build_git_doctor_payload(workspace_root)
    preview_processes = _build_preview_doctor_payload()
    runtime = _state.ws_manager.runtime_snapshot()
    llm = _build_llm_status_payload()
    llm_settings = get_llm_settings_payload()
    active_provider = str(llm_settings.get("provider") or llm.get("provider") or "")
    provider_payload = llm_settings.get(active_provider)
    if isinstance(provider_payload, dict):
        llm["has_api_key"] = bool(provider_payload.get("has_api_key"))
        llm["base_url"] = provider_payload.get("base_url") or ""

    return {
        "backend": {
            "status": "ok",
            "version": "0.2.0",
            "active_sessions": _state.ws_manager.active_count,
            "runtime": runtime,
        },
        "llm": llm,
        "mcp": get_mcp_status(),
        "workspace": {
            "root": str(workspace_root),
            "exists": workspace_root.exists(),
            "writable": os.access(workspace_root, os.W_OK),
        },
        "git": git_status,
        "preview": {
            "count": len(preview_processes),
            "processes": preview_processes,
            "url": next((item.get("url") for item in preview_processes if item.get("url")), ""),
        },
        "terminal": {
            "active_sessions": _state.ws_manager.active_count,
            "runtime": "desktop-or-websocket",
        },
        "capabilities": _build_capability_status_payload(),
    }


def _build_git_doctor_payload(workspace_root: Any) -> dict[str, Any]:
    try:
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace_root,
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=3,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=workspace_root,
            capture_output=True,
            text=True, encoding="utf-8",
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


def _build_preview_doctor_payload() -> list[dict[str, Any]]:
    try:
        from backend.preview import running_preview_processes

        return [process.to_dict() for process in running_preview_processes()]
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
