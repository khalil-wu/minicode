from __future__ import annotations

import logging
import os

from backend.agent.tool_projection import result_kind_for_tool
from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

CHECKPOINT_WRITE_TOOL_NAMES = {"write_file", "edit_file", "apply_patch"}
MUTATING_TOOL_NAMES = CHECKPOINT_WRITE_TOOL_NAMES | {
    "run_command",
    "git_commit",
    "save_memory",
    "remember_memory",
    "create_worktree",
    "remove_worktree",
}
TOOL_TIMEOUTS: dict[str, float] = {
    "run_command": 120.0,
    "task": 360.0,
    "write_file": 30.0,
    "edit_file": 30.0,
    "read_file": 10.0,
    "list_files": 10.0,
    "grep_files": 15.0,
    "glob_files": 10.0,
    "fuzzy_search": 15.0,
    "web_search": 30.0,
    "web_fetch": 30.0,
}
DEFAULT_TOOL_TIMEOUT = 60.0
MAX_TOOL_CONCURRENCY_ENV = "MINICODE_MAX_TOOL_CONCURRENCY"
DEFAULT_MAX_CONCURRENT_TOOLS = 10
TOOL_BATCH_TIMEOUT_ENV = "MINICODE_TOOL_BATCH_TIMEOUT_SECONDS"
DEFAULT_TOOL_BATCH_TIMEOUT = 240.0


def resolve_max_concurrent_tools() -> int:
    raw = os.environ.get(MAX_TOOL_CONCURRENCY_ENV)
    if raw is None:
        return DEFAULT_MAX_CONCURRENT_TOOLS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default max tool concurrency %s",
            MAX_TOOL_CONCURRENCY_ENV,
            raw,
            DEFAULT_MAX_CONCURRENT_TOOLS,
        )
        return DEFAULT_MAX_CONCURRENT_TOOLS
    if value < 1:
        logger.warning(
            "Invalid %s=%r; using default max tool concurrency %s",
            MAX_TOOL_CONCURRENCY_ENV,
            raw,
            DEFAULT_MAX_CONCURRENT_TOOLS,
        )
        return DEFAULT_MAX_CONCURRENT_TOOLS
    return value


def resolve_tool_timeout(name: str, tool_registry: ToolRegistry) -> float:
    """Per-tool timeout: tool-declared value first, then legacy table."""
    tool = tool_registry.get_tool(name)
    declared = getattr(tool, "timeout_seconds", None) if tool is not None else None
    if declared is not None:
        return float(declared)
    return TOOL_TIMEOUTS.get(name, DEFAULT_TOOL_TIMEOUT)


def resolve_tool_batch_timeout(batch: list[ToolCallEvent], tool_registry: ToolRegistry) -> float:
    raw = os.environ.get(TOOL_BATCH_TIMEOUT_ENV)
    if raw is not None:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.warning(
            "Invalid %s=%r; using default tool batch timeout",
            TOOL_BATCH_TIMEOUT_ENV,
            raw,
        )

    max_tool_timeout = max(
        (resolve_tool_timeout(tc.name, tool_registry) for tc in batch),
        default=DEFAULT_TOOL_TIMEOUT,
    )
    return max(DEFAULT_TOOL_BATCH_TIMEOUT, max_tool_timeout)


def tool_batch_timeout_result(tc: ToolCallEvent, timeout: float) -> ToolResult:
    return ToolResult(
        content=(
            f"Tool batch timed out after {timeout:.2f}s before '{tc.name}' completed. "
            "No completed output was available for this call. "
            "Do not retry the identical batch; reduce scope, change arguments, or continue from completed results."
        ),
        is_error=True,
        status="failed",
        limitation="batch_timeout",
        display_summary=f"Batch timed out before completion: {tc.name}",
        result_kind=result_kind_for_tool(tc.name),
    )


def tool_mutates(name: str, tool_registry: ToolRegistry | None = None) -> bool:
    """Whether a tool call mutates state — tool metadata first, then legacy set."""
    if tool_registry is not None:
        tool = tool_registry.get_tool(name)
        if tool is not None:
            side_effect_kind = tool_side_effect_kind(name, tool_registry)
            if side_effect_kind in {"workspace", "external", "destructive"}:
                return True
            if (
                getattr(tool, "mutates_workspace", False)
                or getattr(tool, "mutates_external_state", False)
            ):
                return True
    return name in MUTATING_TOOL_NAMES


def tool_side_effect_kind(
    name: str,
    tool_registry: ToolRegistry | None = None,
    args: dict[str, object] | None = None,
) -> str:
    """Return a tool-owned side-effect class, falling back to legacy names."""
    if tool_registry is not None:
        tool = tool_registry.get_tool(name)
        if tool is not None:
            get_kind = getattr(tool, "get_side_effect_kind", None)
            if callable(get_kind):
                try:
                    return str(get_kind(args)).strip().lower() or "none"
                except Exception:
                    pass
            if getattr(tool, "destructive", False):
                return "destructive"
            if getattr(tool, "mutates_external_state", False):
                return "external"
            if getattr(tool, "mutates_workspace", False):
                return "workspace"
            return "none"
    if name in CHECKPOINT_WRITE_TOOL_NAMES:
        return "workspace"
    if name in MUTATING_TOOL_NAMES:
        return "external" if name in {"git_commit", "save_memory", "remember_memory"} else "workspace"
    return "none"


def tool_is_idempotent(
    name: str,
    tool_registry: ToolRegistry,
    args: dict[str, object] | None = None,
) -> bool:
    """Whether repeating the exact call is safe for loop guardrails/retries."""
    tool = tool_registry.get_tool(name)
    if tool is None:
        return False
    is_idempotent = getattr(tool, "is_idempotent", None)
    if callable(is_idempotent):
        try:
            return bool(is_idempotent(args))
        except Exception:
            return bool(getattr(tool, "read_only", False)) and not tool_mutates(name, tool_registry)
    return bool(getattr(tool, "read_only", False)) and not tool_mutates(name, tool_registry)
