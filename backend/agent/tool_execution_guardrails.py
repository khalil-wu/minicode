"""
Tool execution guardrails — extracted from tool_execution.py.

Provides pre-execution and post-execution safety checks:
  - run_command_file_write_guard_reason: detect file writes via shell
  - subagent_scope_guard_reason: enforce subagent tool scope restrictions
  - is_malformed_web_tool_call: detect malformed web tool calls
  - disabled_tool_guard_reason: check if a tool is disabled
  - repeated_similar_web_search_result: detect duplicate web searches
  - invalid_tool_call_guard_reason: validate tool call structure
  - workspace_write_targets: identify workspace write targets
  - path_is_within_any_scope: check if a path is within allowed scopes
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.llm.base import ToolCallEvent
from backend.agent.tool_guardrails import (
    ToolCallGuardrailController,
    duplicate_output_write_guard_reason,
)
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── Patterns for detecting file writes via shell commands ───────────

# ── Web tool duplicate detection ────────────────────────────────────

_MAX_SIMILAR_SEARCH_HISTORY = 5
_SIMILARITY_THRESHOLD = 0.85


def run_command_file_write_guard_reason(command: str) -> str:
    """Compatibility wrapper around the production write-guard in tool_execution.

    Keep a single implementation so tests and alternate import paths cannot
    drift into a stricter (false-positive) or weaker (false-negative) policy.
    """
    from backend.agent.tool_execution import (
        run_command_file_write_guard_reason as _impl,
    )

    return _impl(command)


def subagent_scope_guard_reason(
    tc: ToolCallEvent,
    *,
    allowed_tools: set[str] | None = None,
    blocked_tools: set[str] | None = None,
) -> str:
    """Check if a tool call is allowed in the subagent scope.

    Returns a guard reason string if the tool is blocked, or empty string
    if it's allowed.
    """
    tool_name = str(tc.name or "").strip()
    if not tool_name:
        return "Tool name is empty"

    if blocked_tools and tool_name in blocked_tools:
        return f"Tool '{tool_name}' is blocked in subagent scope"

    if allowed_tools is not None and tool_name not in allowed_tools:
        return f"Tool '{tool_name}' is not in the allowed tools for this subagent scope"

    return ""


def is_malformed_web_tool_call(reason: str) -> bool:
    """Check if a guard reason indicates a malformed web tool call."""
    malformed_indicators = (
        "missing url",
        "missing query",
        "invalid url",
        "empty query",
        "malformed",
    )
    reason_lower = reason.lower().strip()
    return any(indicator in reason_lower for indicator in malformed_indicators)


def disabled_tool_guard_reason(
    state: Any,
    tc: ToolCallEvent,
) -> str:
    """Check if a tool is disabled in the current state."""
    tool_name = str(tc.name or "").strip()
    if not tool_name:
        return ""
    disabled_tools = getattr(state, "disabled_tools", None) or set()
    if tool_name in disabled_tools:
        return f"Tool '{tool_name}' is disabled"
    return ""


def repeated_similar_web_search_result(
    state: Any,
    tc: ToolCallEvent,
) -> ToolResult | None:
    """Detect if a web search query is too similar to recent ones.

    Returns a ToolResult with the previous result if the query is a
    duplicate, or None if it's a new query.
    """
    tool_name = str(tc.name or "").strip()
    if tool_name not in ("web_search", "search"):
        return None

    args = tc.arguments if isinstance(tc.arguments, dict) else {}
    query = str(args.get("query") or "").strip().lower()
    if not query:
        return None

    search_history = getattr(state, "_web_search_history", None)
    if search_history is None:
        search_history = []
        try:
            setattr(state, "_web_search_history", search_history)
        except (AttributeError, TypeError):
            return None

    # Check similarity against recent searches
    for entry in search_history[-_MAX_SIMILAR_SEARCH_HISTORY:]:
        prev_query = str(entry.get("query") or "").lower()
        if not prev_query:
            continue
        similarity = _string_similarity(query, prev_query)
        if similarity >= _SIMILARITY_THRESHOLD:
            prev_result = entry.get("result")
            if prev_result:
                return ToolResult(
                    content=str(prev_result),
                    is_error=False,
                    display_summary=f"Duplicate search (similarity={similarity:.0%})",
                    limitation="This search was similar to a recent one; returning cached result.",
                )

    return None


def invalid_tool_call_guard_reason(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
) -> str:
    """Validate that a tool call has the required structure.

    Returns a guard reason string if invalid, or empty string if valid.
    """
    tool_name = str(tc.name or "").strip()
    if not tool_name:
        return "Tool call has no tool name"

    tc_id = str(getattr(tc, "id", "") or "").strip()
    if not tc_id:
        return f"Tool call '{tool_name}' has no id"

    # Check if the tool exists in the registry
    tool = tool_registry.get_tool(tool_name) if hasattr(tool_registry, "get_tool") else None
    if tool is None:
        return f"Tool '{tool_name}' is not registered"

    # Check arguments
    args = tc.arguments
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return f"Tool call '{tool_name}' arguments must be a JSON object"

    return ""


def rejection_result(
    tc: ToolCallEvent,
    message: str,
    *,
    is_error: bool = True,
    display_summary: str = "Tool call rejected",
    result_kind: str = "generic",
) -> ToolResult:
    """Create a ToolResult for a tool call rejected before execution."""
    return ToolResult(
        content=str(message or "Tool call rejected"),
        is_error=is_error,
        display_summary=display_summary,
        result_kind=result_kind,
        status="blocked" if is_error else "completed",
    )


def invalid_call_result(
    tc: ToolCallEvent,
    reason: str,
    *,
    malformed_web_call: bool = False,
) -> ToolResult:
    """Create a consistent ToolResult for malformed/invalid model tool calls."""
    return rejection_result(
        tc,
        reason,
        is_error=True,
        display_summary="Invalid web tool call" if malformed_web_call else "Invalid tool call",
        result_kind="search" if malformed_web_call else "generic",
    )


def duplicate_output_write_guard_result(state: Any, tc: ToolCallEvent) -> ToolResult | None:
    """Map duplicate output-write guard decisions into a runtime ToolResult."""
    reason = duplicate_output_write_guard_reason(state, tc)
    if not reason:
        return None
    return ToolResult(
        content=reason,
        is_error=True,
        display_summary="Duplicate output write blocked",
        result_kind="file",
        status="blocked",
    )


def guardrail_before_call_result(
    controller: ToolCallGuardrailController | None,
    tc: ToolCallEvent,
) -> ToolResult | None:
    """Return a rejection result when the progressive guardrail blocks a call."""
    if controller is None:
        return None
    try:
        decision = controller.before_call(tc.name, tc.arguments)
    except Exception as exc:
        logger.debug("Tool guardrail before_call failed: %s", exc)
        return None
    if getattr(decision, "allows_execution", True):
        return None
    code = str(getattr(decision, "code", "") or getattr(decision, "action", "") or "blocked")
    return ToolResult(
        content=str(getattr(decision, "message", "") or f"Tool call blocked by guardrail: {code}"),
        is_error=True,
        display_summary=f"Guardrail: {code}",
        result_kind="generic",
        status="blocked",
    )


def guardrail_after_call_result(
    controller: ToolCallGuardrailController | None,
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    status: str | None,
    final_status: str,
    append_to_context: bool,
) -> ToolResult:
    """Record a completed call and append guardrail guidance when warranted."""
    if controller is None or not append_to_context:
        return result
    if status == "blocked" or final_status == "blocked":
        return result
    failed = bool(result.is_error or final_status in {"failed", "error", "timeout"})
    try:
        decision = controller.after_call(tc.name, tc.arguments, result.content, failed=failed)
    except Exception as exc:
        logger.debug("Tool guardrail after_call failed: %s", exc)
        return result
    action = str(getattr(decision, "action", "") or "allow")
    if action != "warn":
        return result
    code = str(getattr(decision, "code", "") or "guardrail_warning")
    message = str(getattr(decision, "message", "") or "").strip()
    if not message:
        return result
    guidance = (
        "\n\n<system-reminder>\n"
        f"工具循环警告 ({code}): {message}\n"
        "</system-reminder>"
    )
    return replace(result, content=f"{result.content}{guidance}")


def workspace_write_targets(tc: ToolCallEvent) -> list[str] | None:
    """Identify workspace file paths that a tool call will write to.

    Returns a list of file paths, or None if the tool doesn't write to
    the workspace.
    """
    tool_name = str(tc.name or "").strip()
    write_tools = {"write_file", "edit_file", "apply_patch"}
    if tool_name not in write_tools:
        return None

    args = tc.arguments if isinstance(tc.arguments, dict) else {}

    if tool_name == "apply_patch":
        patch_text = str(args.get("patch") or "")
        return apply_patch_target_paths(patch_text)

    for key in ("file_path", "path", "target", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]

    return []


def apply_patch_target_paths(patch_text: str) -> list[str] | None:
    """Extract file paths from an apply_patch text."""
    if not patch_text:
        return None
    paths: list[str] = []
    # Look for file path patterns in patch headers
    for match in re.finditer(r'^\+\+\+\s+(.+?)$', patch_text, re.MULTILINE):
        path = match.group(1).strip()
        if path and path != "/dev/null":
            paths.append(path)
    if not paths:
        # Try simpler pattern: Update File: path
        for match in re.finditer(r'(?:Update File|Create File|Delete File):\s*(.+)', patch_text):
            path = match.group(1).strip()
            if path:
                paths.append(path)
    return paths if paths else None


def path_is_within_any_scope(
    path: Path,
    scopes: list[Path],
) -> bool:
    """Check if a path is within any of the allowed scopes."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    for scope in scopes:
        try:
            resolved_scope = scope.resolve()
            if resolved == resolved_scope:
                return True
            if str(resolved).startswith(str(resolved_scope) + os.sep):
                return True
        except (OSError, RuntimeError):
            continue
    return False


# ── Helpers ─────────────────────────────────────────────────────────

import os


def _string_similarity(a: str, b: str) -> float:
    """Compute a simple similarity ratio between two strings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Use difflib for sequence matching
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _resolve_workspace_path_for_scope(
    raw_path: str,
    workspace_root: Path | str | None,
) -> Path:
    """Resolve a workspace-relative path to an absolute path."""
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if workspace_root:
        return Path(workspace_root) / path
    return path.resolve()
