"""Session-scoped helpers used by the Agent runtime.

These functions own environment projection, provider adapter limits, MCP
    registry inspection. Callers that need
one of these capabilities import its owning module directly; ``loop.py`` keeps
only the public turn entrypoint and session-context facade.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext
from backend.tools.subagent_context import (
    is_subagent_permission_context,
    subagent_toolset_policy,
)


@dataclass
class AgentLoopSessionContext:
    """Per-session runtime dependencies bag."""

    skill_manager: Any | None = None
    permission_context: PermissionContext | None = None
    workspace_root: Path | None = None
    session_id: str = ""
    task_id: str = ""
    task_manager: Any | None = None
    background_manager: Any | None = None
    terminal_manager: Any | None = None
    cancel_event: asyncio.Event | None = None
    stream_callback: Callable[[str], None] | None = None
    emit_event: Callable[[AgentEvent], None] | None = None
    metadata: dict[str, Any] | None = None


def prepare_turn_state(
    state: AgentState,
    *,
    settings: Any,
) -> None:
    """Reset exactly the ephemeral fields owned by a new user turn.

    QueryEngine calls this for the canonical path; direct loop callers use the
    same function as a compatibility adapter. Keeping one reset contract avoids
    state leaking or being cleared twice as lifecycle ownership moves outward.
    """
    state.max_total_retries = max(0, int(getattr(settings, "turn_error_budget", 0) or 0))
    state.disabled_tools.clear()
    state.stop_hook_feedback_count = 0
    state.max_output_recovery_count = 0
    state.max_output_partial_text = ""
    state.reactive_compaction_attempted = False
    state.terminal_status = None
    state.clear_transition()
    if not isinstance(getattr(state, "prompt_context", None), dict):
        state.prompt_context = {}


def collect_mcp_instructions() -> dict[str, str]:
    """Fetch server-declared MCP instructions, tolerant of an absent manager."""

    try:
        from backend.api.routes_health import get_mcp_manager

        manager = get_mcp_manager()
        if manager is not None:
            return manager.get_server_instructions()
    except Exception:  # pragma: no cover - manager unavailable / not started
        pass
    return {}


def mcp_registry_version() -> int:
    """Return the MCP registry generation for tool-schema cache invalidation."""

    try:
        from backend.api.routes_health import get_mcp_manager

        manager = get_mcp_manager()
        if manager is not None:
            return int(getattr(manager, "registry_version", 0) or 0)
    except Exception:  # pragma: no cover - manager unavailable / not started
        pass
    return 0


def active_toolset_policy_for_context(
    *,
    permission_context: PermissionContext,
) -> Any | None:
    if is_subagent_permission_context(permission_context):
        return subagent_toolset_policy()
    return None


def populate_prompt_context(
    *,
    state: AgentState,
    metadata: dict[str, Any],
    workspace_root: Path | None,
    permission_context: PermissionContext,
) -> None:
    """Expose volatile runtime context to the next user-turn prompt block."""

    prompt_context = getattr(state, "prompt_context", None)
    if not isinstance(prompt_context, dict):
        prompt_context = {}
        state.prompt_context = prompt_context

    if session_id := str(
        metadata.get("session_id") or metadata.get("minicode_session_id") or ""
    ).strip():
        prompt_context["session_id"] = session_id
    if conversation_id := str(metadata.get("conversation_id") or "").strip():
        prompt_context["conversation_id"] = conversation_id

    environment = prompt_context.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    cwd = str(workspace_root or metadata.get("cwd") or environment.get("cwd") or Path.cwd())
    environment.setdefault("cwd", cwd)
    if workspace_root is not None:
        environment["workspace_roots"] = [str(workspace_root)]
    elif not isinstance(environment.get("workspace_roots"), list):
        environment["workspace_roots"] = [cwd] if cwd.strip() else []
    user_directories = {
        name: str(os.environ.get(env_name) or "").strip()
        for name, env_name in {
            "desktop": "MINICODE_DESKTOP_DIR",
            "documents": "MINICODE_DOCUMENTS_DIR",
            "downloads": "MINICODE_DOWNLOADS_DIR",
        }.items()
        if str(os.environ.get(env_name) or "").strip()
    }
    if user_directories:
        environment["user_directories"] = user_directories

    permission = environment.get("permission")
    if not isinstance(permission, dict):
        permission = {}
    mode = str(getattr(permission_context, "mode", "") or "default")
    if mode in {"bypass", "full_access", "full-access", "danger-full-access"}:
        file_system_type = "unrestricted"
    elif mode == "plan":
        file_system_type = "read_only"
    elif workspace_root is not None:
        file_system_type = "workspace"
    else:
        file_system_type = "computer"
    permission.update(
        {
            "mode": mode,
            "source": str(getattr(permission_context, "source", "") or "runtime"),
            "workspace_scope": str(
                getattr(permission_context, "workspace_scope", "")
                or ("project" if workspace_root else "computer")
            ),
            "file_system_type": file_system_type,
        }
    )
    environment["permission"] = permission
    prompt_context["environment"] = environment
    prompt_context["collaboration_mode"] = (
        "plan"
        if mode == "plan"
        or str(metadata.get("collaboration_mode") or "").strip().lower() == "plan"
        else "default"
    )
    agent_mode = str(
        metadata.get("agent_mode") or metadata.get("agentMode") or "build"
    ).strip().lower()
    prompt_context["agent_mode"] = (
        agent_mode if agent_mode in {"build", "plan", "review", "explore"} else "build"
    )
    prompt_context["previous_turn_aborted"] = bool(
        metadata.get("previous_turn_aborted") or metadata.get("turn_aborted")
    )
