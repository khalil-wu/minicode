"""Session-scoped helpers used by the Agent runtime.

These functions own environment projection, provider adapter limits, MCP
    registry inspection. Callers that need
one of these capabilities import its owning module directly; ``loop.py`` keeps
only the public turn entrypoint and session-context facade.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext
from backend.tools.toolset_runtime import resolve_context_toolset_policy
from backend.tools.toolsets import ToolsetPolicy


logger = logging.getLogger(__name__)


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
    # Host-owned lifecycle capability. The core borrows it for one turn and
    # never owns or shuts it down.
    lifecycle_runtime: Any | None = None
    # A mutable session owner may change model/tool selection between provider
    # boundaries; the loop borrows that owner instead of freezing a copy.
    agent_session: Any | None = None
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
    state.max_output_no_progress_count = 0
    state.max_output_last_partial_text = ""
    state.reactive_compaction_attempted = False
    state.terminal_status = None
    # A turn must not inherit the previous turn's stop decision: iteration
    # admission terminates immediately on any truthy stopped_reason, so a host
    # that reuses one AgentState across turns (sdk.py and QuerySubmission.state
    # both accept a caller-supplied state) would get a turn that never calls the
    # provider and reports the previous turn's reply as completed.
    state.stopped_reason = None
    state.reply = ""
    state.clear_transition()
    if not isinstance(getattr(state, "prompt_context", None), dict):
        state.prompt_context = {}


def collect_mcp_instructions(manager: Any | None = None) -> dict[str, str]:
    """Fetch server-declared MCP instructions, tolerant only of no manager.

    An initialized manager is part of the prompt/tool contract for the turn.
    If it cannot provide its instructions, continuing with an empty block can
    make the model operate against a stale or incomplete MCP registry.  Keep
    the no-manager case compatible with startup/tests, but propagate real
    manager failures so the caller fails closed.
    """

    try:
        if manager is None:
            from backend.api.routes_health import get_mcp_manager

            manager = get_mcp_manager()
        if manager is not None:
            return manager.get_server_instructions()
    except Exception:
        logger.exception("Failed to collect instructions from the MCP manager")
        raise
    return {}


_MCP_MANAGER_UNSET = object()


def mcp_registry_version(manager: Any | None = _MCP_MANAGER_UNSET) -> int:
    """Return the MCP registry generation for tool-schema cache invalidation.

    A registry read failure must not be converted into generation ``0``:
    doing so can reuse a tool-schema cache built for a different MCP state.
    """

    try:
        if manager is _MCP_MANAGER_UNSET:
            from backend.api.routes_health import get_mcp_manager

            manager = get_mcp_manager()
        if manager is not None:
            return int(getattr(manager, "registry_version", 0) or 0)
    except Exception:
        logger.exception("Failed to read the MCP registry version")
        raise
    return 0


def active_toolset_policy_for_context(
    *,
    permission_context: PermissionContext,
    session_policy: ToolsetPolicy | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    # MiniCode has one canonical model-facing tool surface. Protocol-specific
    # names are translated at an external boundary, never selected by a run.
    return resolve_context_toolset_policy(
        permission_context,
        metadata,
        session_policy=session_policy or ToolsetPolicy.default(),
        prefer_active=False,
    )


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
    cwd = str(workspace_root or "")
    environment["cwd"] = cwd
    if workspace_root is not None:
        environment["workspace_roots"] = [str(workspace_root)]
    else:
        environment["workspace_roots"] = []
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
    mode = str(getattr(permission_context, "mode", "") or "confirm")
    if mode == "bypass":
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
    plan_paths = list((getattr(permission_context, "filesystem_constraints", {}) or {}).get("plan_files", []))
    if mode == "plan" and len(plan_paths) == 1:
        prompt_context["plan_file_path"] = str(plan_paths[0])
        try:
            prompt_context["plan_file_exists"] = Path(str(plan_paths[0])).exists()
        except OSError:
            prompt_context["plan_file_exists"] = False
    else:
        prompt_context.pop("plan_file_path", None)
        prompt_context.pop("plan_file_exists", None)
    prompt_context["environment"] = environment
    prompt_context["collaboration_mode"] = (
        "plan"
        if mode == "plan"
        or str(metadata.get("collaboration_mode") or "").strip().lower() == "plan"
        else "confirm"
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
    # Keep the query-source identity in the durable turn context so
    # ContextBuilder can apply Claude Code's main-thread-only time-based
    # microcompact rule without inspecting transport metadata at provider
    # boundaries.  Subagents/side queries are filtered by their explicit role.
    prompt_context["query_source"] = str(
        metadata.get("query_source") or "user"
    ).strip() or "user"
    prompt_context["agent_role"] = str(
        metadata.get("agent_role") or "main"
    ).strip() or "main"
