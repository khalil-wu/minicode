"""Health, status, doctor diagnostics, and guidelines endpoints."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Query, Response

from backend.agent.claude_md import load_project_guideline_bundle
from backend.config import PROJECT_ROOT
from backend.workspace.state import get_active_workspace_root
from backend.services.health_service import (
    build_capability_fallback_payload,
    build_capability_status_payload,
    build_doctor_payload,
    build_health_payload,
    build_llm_status_payload,
    build_status_payload,
    cached_payload,
)

from . import _state
from .tool_registry import _build_tool_registry

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Health / status endpoints ──


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check endpoint."""
    return build_health_payload(bootstrap=_state.bootstrap, active_sessions=_state.ws_manager.active_count)


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
        result = build_capability_fallback_payload()
        _state.capability_cache_payload = result
        _state.capability_cache_expires_at = now + 5
        return result

def _build_llm_status_payload() -> dict[str, Any]:
    return build_llm_status_payload()

def _build_doctor_payload() -> dict[str, Any]:
    workspace_root = get_active_workspace_root(PROJECT_ROOT).resolve()
    return build_doctor_payload(
        workspace_root=workspace_root,
        runtime_snapshot=_state.ws_manager.runtime_snapshot(),
        active_sessions=_state.ws_manager.active_count,
        llm=_build_llm_status_payload(),
        mcp_status=get_mcp_status(),
        capabilities=_build_capability_status_payload(),
        preview_processes=_build_preview_doctor_payload(),
    )

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
