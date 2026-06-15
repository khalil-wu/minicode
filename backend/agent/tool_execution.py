from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.harness.issues import classify_tool_issue
from backend.agent.harness.contracts import EvidenceRecord
from backend.agent.message import AgentEvent
from backend.agent.harness._common import WEB_SEARCH_TOOL_NAMES, WEB_FETCH_TOOL_NAMES, WEB_TOOL_NAMES, _text_arg
from backend.agent.harness.guardrails import (
    ToolCallGuardrailController,
    append_guardrail_guidance,
    web_guard_reason,
)
from backend.agent.state import AgentState
from backend.agent.harness.repair import RepairResult, ToolArgRepairEngine, argument_has_value
from backend.agent.harness.resources import (
    ResourceResolver,
    clean_candidate_url,
    inferred_read_file_path_from_recent_list,
)
from backend.agent.harness.catalog import tool_spec_for
from backend.agent.harness.control import CONTROL_TOOL_NAMES, ControlToolRouter
from backend.agent.harness.projection import (
    DEFAULT_PROJECTION_REGISTRY,
    activity_kind_for_tool,
    display_hint_for_tool,
    display_summary_for_result,
    input_summary_for_tool,
    result_kind_for_tool,
)
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import (
    PermissionChecker,
    check_denial_reason,
    check_permission_level,
)
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.review import generate_edit_diff_payload, generate_file_diff_payload
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

CHECKPOINT_WRITE_TOOL_NAMES = {"write_file", "edit_file"}


def _extract_diff_from_tool_result(content: str) -> dict[str, Any] | None:
    """Extract structured +N/-M diff from tool result content for frontend rendering."""
    plus = 0
    minus = 0
    for line in content.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    if plus == 0 and minus == 0:
        return None
    return {"plus": plus, "minus": minus, "files_count": 1}


MUTATING_TOOL_NAMES = CHECKPOINT_WRITE_TOOL_NAMES | {
    "run_command", "git_commit", "save_memory", "remember_memory",
    "create_worktree", "remove_worktree",
}
SPECIAL_TOOL_NAMES = CONTROL_TOOL_NAMES
INTERNAL_GUARDED_TOOL_NAMES = {
    "web_search",
    "web_fetch",
    "run_command",
}
NON_CRITICAL_TIMEOUT_TOOLS = {
    "task",
    "remember_memory",
    "save_memory",
    "recall_memory",
}
COMMAND_OUTPUT_STREAM_TOOL_NAMES = {"run_command", "bash", "powershell"}
CLAUDE_CODE_TOOL_NAME_ALIASES = {
    "Read": "read_file",
}
WORKSPACE_FILE_ARG_ALIAS_TO_CANONICAL = {
    "read_file": ("file_path", ("path", "target", "filename")),
    "write_file": ("file_path", ("path", "target", "filename")),
    "edit_file": ("file_path", ("path", "target", "filename")),
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
    "mcp__memory-rag__recall": 3.0,
    "mcp__memory_rag__recall": 3.0,
    "mcp__memory-rag__remember": 3.0,
    "mcp__memory_rag__remember": 3.0,
}
DEFAULT_TOOL_TIMEOUT = 60.0
MAX_TOOL_CONCURRENCY_ENV = "MINICODE_MAX_TOOL_CONCURRENCY"
DEFAULT_MAX_CONCURRENT_TOOLS = 10
TOOL_BATCH_TIMEOUT_ENV = "MINICODE_TOOL_BATCH_TIMEOUT_SECONDS"
DEFAULT_TOOL_BATCH_TIMEOUT = 240.0


def _resolve_max_concurrent_tools() -> int:
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


def _resolve_tool_timeout(name: str, tool_registry: ToolRegistry) -> float:
    """Per-tool timeout: tool-declared value first, then legacy table, then default.

    Tools self-declare ``timeout_seconds`` (Phase 4.2); the TOOL_TIMEOUTS table
    remains a fallback for tools/MCP proxies that haven't set one.
    """
    tool = tool_registry.get_tool(name)
    declared = getattr(tool, "timeout_seconds", None) if tool is not None else None
    if declared is not None:
        return float(declared)
    return TOOL_TIMEOUTS.get(name, DEFAULT_TOOL_TIMEOUT)


def _resolve_tool_batch_timeout(batch: list[ToolCallEvent], tool_registry: ToolRegistry) -> float:
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
        (_resolve_tool_timeout(tc.name, tool_registry) for tc in batch),
        default=DEFAULT_TOOL_TIMEOUT,
    )
    return max(DEFAULT_TOOL_BATCH_TIMEOUT, max_tool_timeout)


def _tool_batch_timeout_result(tc: ToolCallEvent, timeout: float) -> ToolResult:
    return ToolResult(
        content=(
            f"Tool batch timed out after {timeout:.2f}s before '{tc.name}' completed. "
            "Partial result preserved: no completed output was available for this call. "
            "Do not retry the identical batch; reduce scope, change arguments, or continue from completed results."
        ),
        is_error=False,
        status="partial",
        limitation="batch_timeout",
        display_summary=f"Batch timed out with partial result: {tc.name}",
        result_kind=result_kind_for_tool(tc.name),
    )


def _tool_mutates(name: str, tool_registry: ToolRegistry | None = None) -> bool:
    """Whether a tool call mutates state — tool metadata first, then legacy set."""
    if tool_registry is not None:
        tool = tool_registry.get_tool(name)
        if tool is not None and (
            getattr(tool, "mutates_workspace", False)
            or getattr(tool, "mutates_external_state", False)
        ):
            return True
    return name in MUTATING_TOOL_NAMES


@dataclass(frozen=True)
class _ToolBatchRuntime:
    ctx: ContextBuilder
    state: AgentState
    tool_registry: ToolRegistry
    tool_ctx: ToolExecutionContext
    iteration_id: str
    guardrail_controller: ToolCallGuardrailController | None = None


_SHELL_FILE_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![0-9])>{1,2}\s*(?!&)\S+", re.I),
    re.compile(r"\b1>{1,2}\s*(?!&)\S+", re.I),
    re.compile(r"\b(?:set-content|add-content|out-file|tee-object)\b", re.I),
    re.compile(r"\bcopy\s+(?:nul|/y\b|con\b).+\S", re.I),
    re.compile(r"\btype\s+nul\s*>\s*\S+", re.I),
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-c\b.*\bopen\s*\([^)]*['\"][wa]\b", re.I | re.S),
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-c\b.*\bwrite_text\s*\(", re.I | re.S),
    re.compile(r"\bnode\s+-e\b.*\b(?:writeFileSync|appendFileSync)\s*\(", re.I | re.S),
    re.compile(r"\bcat\s+>{1,2}\s*\S+", re.I),
)
def run_command_file_write_guard_reason(command: str) -> str:
    """Return guidance when run_command is being used as a file editing tool."""
    stripped = command.strip()
    if not stripped:
        return ""
    for pattern in _SHELL_FILE_WRITE_PATTERNS:
        if pattern.search(stripped):
            return (
                "Blocked run_command because it appears to create or edit files through the shell. "
                "Use write_file for complete file writes or edit_file for targeted changes so MiniCode can show a diff review. "
                "Use run_command only for commands such as tests, builds, git inspection, and other shell-only operations."
            )
    return ""


def web_search_guard_result(reason: str) -> ToolResult:
    return ToolResult(
        content=reason,
        is_error=False,
        display_summary="搜索策略调整",
        result_kind="search",
        status="success",
    )


def is_malformed_web_tool_call(reason: str) -> bool:
    return bool(
        re.search(
            r"(?:Invalid web_(?:search|fetch) call|Invalid tool call for 'web_(?:search|fetch)'): "
            r"missing required",
            reason,
            re.I,
        )
    )


def disabled_tool_guard_reason(state: AgentState, tc: ToolCallEvent) -> str:
    if tc.name not in state.disabled_tools:
        return ""
    guidance = " ".join(state.loop_guidance[-2:]).strip()
    if guidance:
        return f"Tool '{tc.name}' is disabled for this turn. {guidance}"
    return f"Tool '{tc.name}' is disabled for this turn. Continue without calling it."


def normalize_tool_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    canonical_path = WORKSPACE_FILE_ARG_ALIAS_TO_CANONICAL.get(name)
    if canonical_path is not None:
        canonical, aliases = canonical_path
        if not argument_has_value(normalized, canonical):
            for alias in aliases:
                if argument_has_value(normalized, alias):
                    normalized[canonical] = normalized[alias]
                    break
    if name in WEB_SEARCH_TOOL_NAMES and not _text_arg(normalized.get("query")):
        query = (
            _text_arg(normalized.get("q"))
            or _text_arg(normalized.get("search_query"))
            or _text_arg(normalized.get("queries"))
            or _text_arg(normalized.get("pattern"))
        )
        if query:
            normalized["query"] = query
    if name in WEB_FETCH_TOOL_NAMES and not _text_arg(normalized.get("url")):
        url = _text_arg(normalized.get("href")) or _text_arg(normalized.get("link"))
        if url:
            normalized["url"] = url
    return normalized


def normalize_tool_call_event(tc: ToolCallEvent, *, fallback_id: str = "") -> ToolCallEvent:
    raw_name = str(tc.name or "").strip()
    name = CLAUDE_CODE_TOOL_NAME_ALIASES.get(raw_name, raw_name)
    args = tc.arguments if isinstance(tc.arguments, dict) else {}
    return replace(
        tc,
        id=str(tc.id or "").strip() or fallback_id,
        name=name,
        arguments=normalize_tool_arguments(name, args),
    )


def unwrap_deferred_tool_call(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
) -> tuple[ToolCallEvent, str]:
    if tc.name != "tool_call":
        return tc, ""
    underlying_name = str((tc.arguments or {}).get("name") or "").strip()
    underlying_args = (tc.arguments or {}).get("arguments")
    if not underlying_name:
        return tc, "tool_call is missing required field 'name'."
    if not isinstance(underlying_args, dict):
        return tc, "tool_call.arguments must be an object."
    if tool_registry.get_tool(underlying_name) is None:
        return tc, f"Deferred tool '{underlying_name}' does not exist."
    get_view = getattr(tool_registry, "get_schema_view", None)
    view = get_view(underlying_name) if callable(get_view) else None
    if view is not None:
        if view.exposure != "deferred" or view.direct:
            return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
        return replace(tc, name=underlying_name, arguments=underlying_args), ""
    spec = tool_spec_for(underlying_name, tool_registry)
    if spec.exposure != "deferred" or getattr(spec, "always_load", False):
        return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
    return replace(tc, name=underlying_name, arguments=underlying_args), ""


def normalized_tool_arguments(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    return normalize_tool_arguments(name, dict(args or {}))


def missing_required_tool_argument_names(tc: ToolCallEvent, tool_registry: ToolRegistry) -> list[str]:
    tc.arguments = normalized_tool_arguments(tc.name, tc.arguments)
    tool = tool_registry.get_tool(tc.name)
    if tool is None:
        return []
    try:
        schema = tool.get_schema()
    except Exception:
        return []
    required_fields = schema.parameters.get("required", []) if schema else []
    return [
        str(field)
        for field in required_fields
        if isinstance(field, str) and not argument_has_value(tc.arguments, field)
    ]


def missing_required_tool_argument_reason(
    state: AgentState,
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
) -> str:
    return ToolArgRepairEngine(state, tool_registry).missing_required_reason(tc)


def tool_call_needs_list_context(tc: ToolCallEvent, tool_registry: ToolRegistry) -> bool:
    spec = tool_spec_for(tc.name, tool_registry)
    for arg in spec.required_args:
        if spec.role_for(arg) == "workspace_file" and not argument_has_value(tc.arguments or {}, arg):
            return True
    return False


def repair_tool_call_sequence(
    state: AgentState,
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext | None = None,
) -> list[ToolCallEvent]:
    """Normalize and repair a model tool-call batch before history/execution."""
    reserved_fetch_urls: set[str] = set()
    repaired: list[ToolCallEvent] = []
    for index, raw_tc in enumerate(tool_calls, 1):
        tc = normalize_tool_call_event(raw_tc, fallback_id=f"tool_{index}")
        repair_result = repair_tool_call_for_execution(
            state,
            tc,
            tool_registry,
            tool_ctx,
            reserved_fetch_urls=reserved_fetch_urls,
        )
        tc = repair_result.tool_call
        if tc.name in WEB_FETCH_TOOL_NAMES:
            url = _text_arg((tc.arguments or {}).get("url"))
            if url:
                reserved_fetch_urls.add(clean_candidate_url(url))
        repaired.append(tc)
    return repaired


def repair_tool_call_for_execution(
    state: AgentState,
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext | None = None,
    *,
    reserved_fetch_urls: set[str] | None = None,
) -> RepairResult:
    """Run the structured argument-repair path used by tool execution."""
    resolver = ResourceResolver(state, tool_ctx, reserved_fetch_urls=reserved_fetch_urls)
    return ToolArgRepairEngine(state, tool_registry, resolver).repair_result(tc)


def repair_result_block_reason(
    state: AgentState,
    repair_result: RepairResult,
    tool_registry: ToolRegistry,
) -> str:
    """Project a structured repair failure into model-facing guidance."""
    if repair_result.needs_model_generation or repair_result.routing_correction:
        return ""
    if not (repair_result.needs_user_input or repair_result.blocked):
        return ""
    tc = repair_result.tool_call
    required_reason = missing_required_tool_argument_reason(state, tc, tool_registry)
    details = [
        text.strip()
        for text in (
            repair_result.user_message,
            repair_result.model_observation,
            required_reason,
        )
        if text and text.strip()
    ]
    return " ".join(dict.fromkeys(details))


def tool_call_is_safe_for_model_history(tc: ToolCallEvent, tool_registry: ToolRegistry) -> bool:
    normalized = normalize_tool_call_event(tc)
    if not normalized.id or not normalized.name:
        return False
    if invalid_tool_call_guard_reason(normalized, tool_registry):
        return False
    if ToolArgRepairEngine(AgentState(user_message=""), tool_registry).missing_required_reason(normalized):
        return False
    if missing_required_tool_argument_names(normalized, tool_registry):
        return False
    return True


def invalid_tool_call_guard_reason(tc: ToolCallEvent, tool_registry: ToolRegistry) -> str:
    name = str(tc.name or "").strip()
    if not name:
        return (
            "Invalid tool call from model: missing tool name. "
            "Re-read the available tool schemas and retry with a valid tool name and required arguments."
        )
    if not isinstance(tc.arguments, dict):
        return (
            f"Invalid tool call for '{name}': arguments must be a JSON object. "
            "Retry with arguments that match the tool schema."
        )
    if (
        tool_registry.get_tool(name) is None
        and name not in SPECIAL_TOOL_NAMES
        and name not in INTERNAL_GUARDED_TOOL_NAMES
    ):
        available = ", ".join(tool_registry.list_tools()) or "none"
        return (
            f"Tool '{name}' does not exist. Available tools: {available}. "
            "Choose one of the available tools or answer without tools."
        )
    return ""


def describe_tool_call(tc: ToolCallEvent) -> str:
    args = tc.arguments or {}
    target = (
        str(args.get("file_path") or args.get("path") or args.get("target") or "").strip()
        or str(args.get("directory") or args.get("cwd") or "").strip()
        or str(args.get("pattern") or args.get("query") or "").strip()
        or str(args.get("command") or "").strip()
    )
    return f"{tc.name} {target}".strip()


def status_for_result(result: ToolResult, requested_status: str | None = None) -> str:
    if requested_status in {"success", "failed", "blocked", "partial"}:
        return requested_status
    if result.status == "timeout" and result.limitation == "non-critical timeout":
        return "success"
    if result.status in {"success", "failed", "blocked", "partial"}:
        return str(result.status)
    return "failed" if result.is_error else "success"


def _tool_start_times(state: AgentState) -> dict[str, float]:
    existing = getattr(state, "_ui_tool_started_at", None)
    if isinstance(existing, dict):
        return existing
    created: dict[str, float] = {}
    setattr(state, "_ui_tool_started_at", created)
    return created


def tool_call_start_event(
    tc: ToolCallEvent,
    *,
    started_epoch: float,
    iteration_id: str,
) -> AgentEvent:
    projection = DEFAULT_PROJECTION_REGISTRY.project_tool_call(tc.name, tc.arguments)
    return AgentEvent.tool_call(
        id=tc.id,
        name=tc.name,
        args=tc.arguments,
        started_at=int(started_epoch * 1000),
        display_hint=projection.display_hint,
        input_summary=projection.input_summary,
        result_kind=projection.result_kind,
        activity_kind=projection.activity_kind,
        group_id=iteration_id,
        step_id=tc.id,
        iteration_id=iteration_id,
        phase="tool",
    )


async def snapshot_before_write(tc: ToolCallEvent, tool_ctx: ToolExecutionContext) -> None:
    if tc.name not in CHECKPOINT_WRITE_TOOL_NAMES:
        return
    manager = getattr(tool_ctx, "checkpoint_manager", None)
    if manager is None:
        return
    try:
        record = await manager.snapshot(
            tool_name=tc.name,
            args=tc.arguments,
            workspace_root=tool_ctx.workspace_root,
            conversation_id=tool_ctx.conversation_id,
            session_id=tool_ctx.session_id,
            tool_call_id=tc.id,
        )
    except Exception as exc:
        logger.warning("checkpoint snapshot failed: %s", exc)
        return
    if record is None:
        return
    emit = getattr(tool_ctx, "emit_event", None)
    if emit:
        try:
            await emit("checkpoint.created", record.to_dict())
        except Exception as exc:
            logger.debug("checkpoint emit failed: %s", exc)


async def run_tool(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> ToolResult:
    from backend.hooks import get_hook_manager

    tc.arguments = normalized_tool_arguments(tc.name, tc.arguments)
    hook_mgr = get_hook_manager()
    if hook_mgr:
        try:
            pre = await hook_mgr.run_pre_tool(tc.name, tc.arguments)
            if pre.blocked:
                return ToolResult(content=f"Tool blocked by hook: {pre.message}", is_error=True)
        except Exception as exc:
            logger.warning("pre_tool hook failed: %s", exc)

    # Validate required arguments against the same helper used by repair/history
    # safety so empty strings and empty containers are handled consistently.
    missing = missing_required_tool_argument_names(tc, tool_registry)
    if missing:
        reason = ToolArgRepairEngine(AgentState(user_message=""), tool_registry).missing_required_reason(tc)
        received = list(tc.arguments.keys())
        return ToolResult(
            content=reason or (
                f"Tool '{tc.name}' is missing required argument(s): {missing}. "
                f"Received keys: {received}. Re-read the tool schema and retry with all required fields."
            ),
            is_error=True,
        )

    await snapshot_before_write(tc, tool_ctx)
    changed_file = changed_file_event_payload(tc, tool_ctx)

    tm = tool_ctx.task_manager
    if tm:
        try:
            managed = tm.create(
                kind="tool_run",
                awaitable=tool_registry.execute(tc.name, tc.arguments, context=tool_ctx),
            )
            result = await tm.wait(managed.id)
        except Exception as exc:
            result = ToolResult(content=f"Task execution failed: {exc}", is_error=True)
    else:
        result = await tool_registry.execute(tc.name, tc.arguments, context=tool_ctx)

    if hook_mgr and not result.is_error:
        try:
            await hook_mgr.run_post_tool(tc.name, tc.arguments, result.content or "")
        except Exception as exc:
            logger.warning("post_tool hook failed: %s", exc)

    if changed_file and not result.is_error:
        emit = getattr(tool_ctx, "emit_event", None)
        if emit:
            try:
                await emit("file.changed", changed_file)
            except Exception as exc:
                logger.debug("file change emit failed: %s", exc)

    return result


def changed_file_event_payload(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
) -> dict[str, Any] | None:
    if tc.name not in CHECKPOINT_WRITE_TOOL_NAMES:
        return None
    raw_path = str(tc.arguments.get("file_path") or "").strip()
    if not raw_path:
        return None
    workspace_root = Path(tool_ctx.workspace_root).resolve() if tool_ctx.workspace_root else None
    path = Path(raw_path)
    resolved = path.resolve() if path.is_absolute() else ((workspace_root / path).resolve() if workspace_root else path.resolve())
    existed_before = resolved.exists()
    event_type = "created" if tc.name == "write_file" and not existed_before else "modified"
    display_path = raw_path
    if workspace_root:
        try:
            display_path = resolved.relative_to(workspace_root).as_posix()
        except ValueError:
            display_path = resolved.as_posix()
    return {
        "path": display_path,
        "event": event_type,
    }


async def run_tool_with_timeout(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> ToolResult:
    timeout = _resolve_tool_timeout(tc.name, tool_registry)
    execution_tool_ctx = tool_context_with_live_output(tc, tool_ctx)
    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            result = await run_tool(tc, tool_registry, execution_tool_ctx)
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - t0) * 1000)
        if is_non_critical_timeout_tool(tc.name):
            return ToolResult(
                content=(
                    f"Optional tool '{tc.name}' timed out after {timeout:.0f}s. "
                    "Do not retry it in this turn; continue with the user-facing answer."
                ),
                is_error=False,
                duration_ms=elapsed,
                status="timeout",
                limitation="non-critical timeout",
                display_summary=f"Optional tool timed out: {tc.name}",
                result_kind=result_kind_for_tool(tc.name),
            )
        return ToolResult(
            content=(
                f"Tool '{tc.name}' timed out after {timeout:.0f}s. "
                "Partial result preserved: the operation did not finish, so use this as incomplete evidence. "
                "Do not retry the identical call; break the operation into smaller steps or try a different approach."
            ),
            is_error=False,
            duration_ms=elapsed,
            status="partial",
            limitation="timeout",
            display_summary=f"Timed out with partial result: {tc.name}",
            result_kind=result_kind_for_tool(tc.name),
        )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if result.duration_ms is None:
        result = replace(result, duration_ms=elapsed)
    return result


def is_non_critical_timeout_tool(name: str) -> bool:
    lower = name.lower()
    if lower in NON_CRITICAL_TIMEOUT_TOOLS:
        return True
    if lower.startswith(("mcp__memory-rag__", "mcp__memory_rag__")):
        return True
    return False


def tool_output_delta_events(tc: ToolCallEvent, result: ToolResult) -> list[AgentEvent]:
    if result.is_error or tc.name not in COMMAND_OUTPUT_STREAM_TOOL_NAMES or not result.content:
        return []

    chunk_size = 2000
    output_text = result.content
    return [
        AgentEvent.tool_output_delta(
            id=tc.id,
            output=output_text[index:index + chunk_size],
            stream="stdout",
        )
        for index in range(0, len(output_text), chunk_size)
    ]


_STREAMED_TOOL_OUTPUT_IDS_KEY = "_streamed_tool_output_ids"


def _mark_tool_output_streamed(tool_ctx: ToolExecutionContext, tool_call_id: str) -> None:
    raw_ids = tool_ctx.metadata.setdefault(_STREAMED_TOOL_OUTPUT_IDS_KEY, set())
    if isinstance(raw_ids, set):
        raw_ids.add(tool_call_id)
        return
    if isinstance(raw_ids, list):
        raw_ids.append(tool_call_id)
        return
    tool_ctx.metadata[_STREAMED_TOOL_OUTPUT_IDS_KEY] = {tool_call_id}


def _tool_output_was_streamed(tool_ctx: ToolExecutionContext, tool_call_id: str) -> bool:
    raw_ids = tool_ctx.metadata.get(_STREAMED_TOOL_OUTPUT_IDS_KEY)
    return isinstance(raw_ids, (set, list, tuple)) and tool_call_id in raw_ids


def tool_context_with_live_output(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
) -> ToolExecutionContext:
    if tc.name not in COMMAND_OUTPUT_STREAM_TOOL_NAMES:
        return tool_ctx
    emit_event = getattr(tool_ctx, "emit_event", None)
    fallback_stream = getattr(tool_ctx, "stream_callback", None)
    if emit_event is None and fallback_stream is None:
        return tool_ctx

    async def _stream_tool_output(output: str, stream: str = "stdout") -> None:
        if not output:
            return
        stream_name = stream if stream in {"stdout", "stderr"} else "stdout"
        _mark_tool_output_streamed(tool_ctx, tc.id)
        if emit_event is not None:
            await emit_event(
                "tool_output_delta",
                {"id": tc.id, "output": output, "stream": stream_name},
            )
            return
        if fallback_stream is not None:
            try:
                await fallback_stream(output, stream_name)
            except TypeError:
                await fallback_stream(output)

    return replace(tool_ctx, stream_callback=_stream_tool_output)


def batch_tool_calls(
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
) -> list[tuple[bool, list[ToolCallEvent]]]:
    """Group tool calls for parallel execution.

    Concurrency-safe tools are collected into a single batch regardless of
    position so they can all execute in parallel via asyncio.gather.
    Mutating tools each get their own serial batch to preserve ordering
    for state-dependent operations.
    """
    if not tool_calls:
        return []
    safe_tools: list[ToolCallEvent] = []
    serial_groups: list[tuple[int, ToolCallEvent]] = []
    for idx, tc in enumerate(tool_calls):
        tool = tool_registry.get_tool(tc.name)
        is_safe = tool.is_concurrency_safe(tc.arguments) if tool else False
        if is_safe:
            safe_tools.append(tc)
        else:
            serial_groups.append((idx, tc))

    batches: list[tuple[bool, list[ToolCallEvent]]] = []
    if safe_tools:
        batches.append((True, safe_tools))
    for _idx, tc in serial_groups:
        batches.append((False, [tc]))
    return batches


def _rejection_result(
    tc: ToolCallEvent,
    reason: str,
    *,
    is_error: bool = True,
    display_summary: str | None = None,
    result_kind: str | None = None,
) -> ToolResult:
    return ToolResult(
        content=reason,
        is_error=is_error,
        display_summary=display_summary,
        result_kind=result_kind or result_kind_for_tool(tc.name),
    )


def _invalid_call_result(
    tc: ToolCallEvent,
    reason: str,
    *,
    malformed_web_call: bool = False,
    is_error: bool = True,
) -> ToolResult:
    return _rejection_result(
        tc,
        reason,
        is_error=is_error,
        display_summary="Invalid web tool call" if malformed_web_call else "Invalid tool call",
        result_kind="search" if tc.name in WEB_SEARCH_TOOL_NAMES else result_kind_for_tool(tc.name),
    )


def guardrail_before_call_result(
    guardrail_controller: ToolCallGuardrailController | None,
    tc: ToolCallEvent,
) -> ToolResult | None:
    if guardrail_controller is None:
        return None
    try:
        decision = guardrail_controller.before_call(tc.name, tc.arguments)
    except Exception as exc:
        logger.warning("guardrail before_call failed for %s: %s", tc.name, exc)
        return None
    if decision is None or decision.allows_execution:
        return None
    return _rejection_result(
        tc,
        decision.message,
        display_summary=f"Guardrail: {decision.code}",
        result_kind="generic",
    )


def guardrail_after_call_result(
    guardrail_controller: ToolCallGuardrailController | None,
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    status: str | None,
    final_status: str,
    append_to_context: bool,
) -> ToolResult:
    if guardrail_controller is None or not append_to_context:
        return result

    # Non-None status means the tool never reached post-execution:
    # repeat guard, permission denial, disabled tool, invalid call, etc.
    # Do not feed those records back through after_call as successes, or the
    # controller can clear the very failure counters that caused the block.
    if status is not None:
        return result

    try:
        # When status is None, the tool actually ran; use the real outcome.
        failed = final_status in ("error", "failed") or result.is_error
        decision = guardrail_controller.after_call(
            tc.name,
            tc.arguments,
            result.content,
            failed=failed,
        )
        if decision.action not in ("warn", "halt"):
            return result
        guidance = append_guardrail_guidance(result.content or "", decision)
        if guidance == (result.content or ""):
            return result
        return replace(result, content=guidance)
    except Exception as exc:
        logger.warning("guardrail after_call failed for %s: %s", tc.name, exc)
        return result


async def _reject_tool_call(
    tc: ToolCallEvent,
    auto_queue: list[ToolCallEvent],
    result: ToolResult,
    *,
    runtime: _ToolBatchRuntime,
    started_epoch: float | None = None,
    status: str = "blocked",
    append_to_context: bool = True,
) -> AsyncIterator[AgentEvent]:
    """Flush pending auto-queue, emit a blocked tool-call event, and store the result."""
    async for ev in flush_queue(
        auto_queue,
        ctx=runtime.ctx,
        state=runtime.state,
        tool_registry=runtime.tool_registry,
        tool_ctx=runtime.tool_ctx,
        iteration_id=runtime.iteration_id,
        guardrail_controller=runtime.guardrail_controller,
    ):
        yield ev
    auto_queue.clear()
    epoch = started_epoch if started_epoch is not None else time.time()
    yield tool_call_start_event(tc, started_epoch=epoch, iteration_id=runtime.iteration_id)
    yield store_result(
        tc,
        result,
        runtime.ctx,
        runtime.state,
        status=status,
        iteration_id=runtime.iteration_id,
        append_to_context=append_to_context,
        guardrail_controller=runtime.guardrail_controller,
    )


async def execute_tool_batch(
    tool_calls: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    approval_handler: Callable | None,
    skill_manager: Any | None,
    permission_context: PermissionContext | None,
    tool_ctx: ToolExecutionContext,
    stagnation_limit: int,
    guardrail_controller: ToolCallGuardrailController | None = None,
) -> AsyncIterator[AgentEvent]:
    auto_queue: list[ToolCallEvent] = []
    iteration_id = f"iter:{max(1, state.iterations)}"
    runtime = _ToolBatchRuntime(
        ctx=ctx,
        state=state,
        tool_registry=tool_registry,
        tool_ctx=tool_ctx,
        iteration_id=iteration_id,
        guardrail_controller=guardrail_controller,
    )
    prepared_tool_calls = repair_tool_call_sequence(state, tool_calls, tool_registry, tool_ctx)

    for index, raw_tc in enumerate(prepared_tool_calls, 1):
        tc = normalize_tool_call_event(
            raw_tc,
            fallback_id=f"tool_{index}",
        )
        tc, bridge_block_reason = unwrap_deferred_tool_call(tc, tool_registry)
        if bridge_block_reason:
            started_epoch = time.time()
            _tool_start_times(state)[tc.id] = started_epoch
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    bridge_block_reason,
                    display_summary="Deferred tool call blocked",
                    result_kind="generic",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
            ):
                yield ev
            continue
        if tool_call_needs_list_context(tc, tool_registry) and auto_queue and not inferred_read_file_path_from_recent_list(state):
            async for ev in flush_queue(
                auto_queue,
                ctx=ctx,
                state=state,
                tool_registry=tool_registry,
                tool_ctx=tool_ctx,
                iteration_id=iteration_id,
                guardrail_controller=guardrail_controller,
            ):
                yield ev
            auto_queue = []
            tc = repair_tool_call_for_execution(state, tc, tool_registry, tool_ctx).tool_call
        repair_result = repair_tool_call_for_execution(state, tc, tool_registry, tool_ctx)
        tc = repair_result.tool_call

        started_epoch = time.time()
        _tool_start_times(state)[tc.id] = started_epoch

        if repair_result.needs_model_generation or repair_result.routing_correction:
            started_epoch_local = started_epoch
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    repair_result.model_observation,
                    is_error=False,
                    display_summary=(
                        "Missing generated content"
                        if repair_result.needs_model_generation
                        else "Routing correction"
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch_local,
                append_to_context=False,
            ):
                yield ev
            state.add_loop_guidance(repair_result.model_observation)
            continue

        if repair_result.repaired and repair_result.model_observation:
            state.add_loop_guidance(repair_result.model_observation)

        invalid_reason = invalid_tool_call_guard_reason(tc, tool_registry)

        repeat_reason = state.repeated_call_guard_reason(tc.name, tc.arguments, limit=stagnation_limit)
        if repeat_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc, repeat_reason,
                    is_error=False,
                    display_summary="Already attempted",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
            ):
                yield ev
            continue

        guardrail_result = guardrail_before_call_result(guardrail_controller, tc)
        if guardrail_result is not None:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                guardrail_result,
                runtime=runtime,
                started_epoch=started_epoch,
            ):
                yield ev
            continue

        if invalid_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    invalid_reason,
                    display_summary="Invalid tool call",
                    result_kind="generic",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
            ):
                yield ev
            continue

        repair_block_reason = repair_result_block_reason(state, repair_result, tool_registry)
        if repair_block_reason:
            malformed_web_tool_call = is_malformed_web_tool_call(repair_block_reason)
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _invalid_call_result(
                    tc,
                    repair_block_reason,
                    malformed_web_call=malformed_web_tool_call,
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
            ):
                yield ev
            continue

        disabled_reason = disabled_tool_guard_reason(state, tc)
        if disabled_reason:
            history_safe = tool_call_is_safe_for_model_history(tc, tool_registry)
            malformed_disabled_web_call = tc.name in WEB_TOOL_NAMES and not history_safe
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    disabled_reason,
                    is_error=malformed_disabled_web_call,
                    display_summary="Invalid web tool call" if malformed_disabled_web_call else "Tool disabled",
                    result_kind="search" if tc.name in WEB_SEARCH_TOOL_NAMES else result_kind_for_tool(tc.name),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked" if malformed_disabled_web_call else "success",
                append_to_context=history_safe,
            ):
                yield ev
            continue

        required_reason = missing_required_tool_argument_reason(state, tc, tool_registry)
        if required_reason:
            malformed_web_tool_call = is_malformed_web_tool_call(required_reason)
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _invalid_call_result(
                    tc,
                    required_reason,
                    malformed_web_call=malformed_web_tool_call,
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
            ):
                yield ev
            continue

        web_reason = web_guard_reason(state, tc, queued_tool_calls=auto_queue)
        if web_reason:
            state.disable_tools({tc.name}, web_reason)
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                web_search_guard_result(web_reason),
                runtime=runtime,
                started_epoch=started_epoch,
                status="success",
            ):
                yield ev
            continue

        command_file_write_reason = (
            run_command_file_write_guard_reason(str(tc.arguments.get("command") or ""))
            if tc.name == "run_command"
            else ""
        )
        if command_file_write_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(tc, command_file_write_reason),
                runtime=runtime,
                started_epoch=started_epoch,
            ):
                yield ev
            continue

        perm_tool = tool_registry.get_tool(tc.name)

        # Tool-owned input validation (Phase 4.1 / CC validateInput): runs after
        # schema/guard checks and before permission. A non-empty message blocks
        # execution and is surfaced as an observation so the model can correct.
        if perm_tool is not None:
            try:
                validate_msg = perm_tool.validate_input(tc.arguments)
            except Exception:
                validate_msg = ""
            if validate_msg:
                async for ev in _reject_tool_call(
                    tc, auto_queue,
                    _rejection_result(
                        tc, validate_msg,
                        display_summary="Invalid tool input",
                        result_kind="generic",
                    ),
                    runtime=runtime,
                    started_epoch=started_epoch,
                    append_to_context=False,
                ):
                    yield ev
                continue

        perm = check_permission_level(permission_checker, tc.name, tc.arguments, context=permission_context, tool=perm_tool)
        denial = check_denial_reason(permission_checker, tc.name, tc.arguments, context=permission_context, tool=perm_tool)

        if denial or perm == PermissionLevel.ALWAYS_DENY:
            msg = denial or f"Tool '{tc.name}' is blocked by policy"
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(tc, msg),
                runtime=runtime,
                started_epoch=started_epoch,
            ):
                yield ev
            continue

        # All guards passed — emit tool_call event to UI (now shows only real executions)
        yield tool_call_start_event(tc, started_epoch=started_epoch, iteration_id=iteration_id)

        if perm == PermissionLevel.AUTO and tc.name not in SPECIAL_TOOL_NAMES:
            auto_queue.append(tc)
            continue

        async for ev in flush_queue(
            auto_queue,
            ctx=ctx,
            state=state,
            tool_registry=tool_registry,
            tool_ctx=tool_ctx,
            iteration_id=iteration_id,
            guardrail_controller=guardrail_controller,
        ):
            yield ev
        auto_queue = []

        async for ev in execute_serial(
            tc,
            perm=perm,
            ctx=ctx,
            state=state,
            tool_registry=tool_registry,
            tool_ctx=tool_ctx,
            approval_handler=approval_handler,
            skill_manager=skill_manager,
            iteration_id=iteration_id,
            guardrail_controller=guardrail_controller,
        ):
            yield ev

    async for ev in flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx, iteration_id=iteration_id, guardrail_controller=guardrail_controller):
        yield ev


async def flush_queue(
    queue: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    iteration_id: str = "",
    guardrail_controller: ToolCallGuardrailController | None = None,
) -> AsyncIterator[AgentEvent]:
    if not queue:
        return

    for is_concurrent, batch in batch_tool_calls(queue, tool_registry):
        if is_concurrent and len(batch) > 1:
            diffs_by_id = {
                tc.id: generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root)
                for tc in batch
                if tc.name in CHECKPOINT_WRITE_TOOL_NAMES
            }
            # Sibling abort (Claude Code pattern): if a bash/command tool
            # fails, cancel all other parallel tools in the same batch.
            cancelled_result = ToolResult(
                content="Cancelled: parallel tool call errored.",
                is_error=True,
                status="failed",
            )

            async def _run_parallel_tool(tc: ToolCallEvent) -> ToolResult:
                try:
                    return await run_tool_with_timeout(tc, tool_registry, tool_ctx)
                except Exception as exc:
                    return ToolResult(
                        content=f"Execution failed: {exc}",
                        is_error=True,
                    )

            max_concurrent_tools = min(len(batch), _resolve_max_concurrent_tools())
            batch_timeout = _resolve_tool_batch_timeout(batch, tool_registry)
            batch_deadline = time.monotonic() + batch_timeout
            pending: dict[asyncio.Task[ToolResult], ToolCallEvent] = {}
            results_by_id: dict[str, ToolResult] = {}
            next_batch_index = 0
            batch_timed_out = False

            def _start_ready_tasks() -> None:
                nonlocal next_batch_index
                while next_batch_index < len(batch) and len(pending) < max_concurrent_tools:
                    tc = batch[next_batch_index]
                    pending[asyncio.create_task(_run_parallel_tool(tc))] = tc
                    next_batch_index += 1

            try:
                _start_ready_tasks()
                while pending:
                    remaining = batch_deadline - time.monotonic()
                    if remaining <= 0:
                        batch_timed_out = True
                        break
                    done, _ = await asyncio.wait(
                        pending.keys(),
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=remaining,
                    )
                    if not done:
                        batch_timed_out = True
                        break
                    should_cancel_siblings = False

                    for task in done:
                        tc = pending.pop(task)
                        try:
                            result = task.result()
                        except asyncio.CancelledError:
                            result = cancelled_result
                        except Exception as exc:
                            result = ToolResult(
                                content=f"Execution failed: {exc}",
                                is_error=True,
                            )
                        results_by_id[tc.id] = result
                        if result.is_error and tc.name in COMMAND_OUTPUT_STREAM_TOOL_NAMES:
                            should_cancel_siblings = True

                    if should_cancel_siblings:
                        remaining = list(pending.items())
                        for task, _tc in remaining:
                            task.cancel()
                        for task, tc in remaining:
                            try:
                                await task
                            except (asyncio.CancelledError, Exception):
                                pass
                            results_by_id[tc.id] = cancelled_result
                            pending.pop(task, None)
                        for tc in batch[next_batch_index:]:
                            results_by_id[tc.id] = cancelled_result
                        next_batch_index = len(batch)
                        break

                    _start_ready_tasks()

                if batch_timed_out:
                    remaining = list(pending.items())
                    for task, _tc in remaining:
                        task.cancel()
                    for task, tc in remaining:
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                        results_by_id[tc.id] = _tool_batch_timeout_result(tc, batch_timeout)
                        pending.pop(task, None)
                    for tc in batch[next_batch_index:]:
                        results_by_id[tc.id] = _tool_batch_timeout_result(tc, batch_timeout)
                    next_batch_index = len(batch)
            finally:
                for task in pending:
                    task.cancel()

            for tc in batch:
                result = results_by_id.get(tc.id, cancelled_result)
                if not _tool_output_was_streamed(tool_ctx, tc.id):
                    for event in tool_output_delta_events(tc, result):
                        yield event
                yield store_result(
                    tc,
                    result,
                    ctx,
                    state,
                    diff=None if result.is_error else diffs_by_id.get(tc.id),
                    iteration_id=iteration_id,
                    guardrail_controller=guardrail_controller,
                    tool_registry=tool_registry,
                )
        else:
            for tc in batch:
                diff = (
                    generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root)
                    if tc.name in CHECKPOINT_WRITE_TOOL_NAMES
                    else None
                )
                result = await run_tool_with_timeout(tc, tool_registry, tool_ctx)
                if not _tool_output_was_streamed(tool_ctx, tc.id):
                    for event in tool_output_delta_events(tc, result):
                        yield event
                yield store_result(
                    tc,
                    result,
                    ctx,
                    state,
                    diff=None if result.is_error else diff,
                    iteration_id=iteration_id,
                    guardrail_controller=guardrail_controller,
                    tool_registry=tool_registry,
                )


async def execute_serial(
    tc: ToolCallEvent,
    *,
    perm: PermissionLevel,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    approval_handler: Callable | None,
    skill_manager: Any | None,
    iteration_id: str = "",
    guardrail_controller: ToolCallGuardrailController | None = None,
) -> AsyncIterator[AgentEvent]:
    diff: dict[str, Any] | None = None

    if perm in (PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW):
        diff = generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root) if perm == PermissionLevel.DIFF_REVIEW else None
        yield AgentEvent.approval_request(
            tool_call_id=tc.id,
            tool_name=tc.name,
            args=tc.arguments,
            diff=diff,
        )
        if approval_handler:
            approval = await approval_handler(tc.id)
            if approval.get("action") == "reject":
                guidance = approval.get("guidance", "user rejected this action")
                result = ToolResult(content=f"Operation rejected: {guidance}", is_error=True)
                yield store_result(tc, result, ctx, state, iteration_id=iteration_id, guardrail_controller=guardrail_controller)
                return
            if approval.get("action") == "partial":
                decisions = approval.get("decisions", {})
                target = tc.arguments.get("file_path") or tc.arguments.get("path") or ""
                rejected = [p for p, d in decisions.items() if d == "rejected"]
                if target and any(target.endswith(rp) or rp.endswith(target) for rp in rejected):
                    result = ToolResult(content=f"Operation rejected for file: {target}", is_error=True)
                    yield store_result(tc, result, ctx, state, iteration_id=iteration_id, guardrail_controller=guardrail_controller)
                    return

    if diff is None and tc.name in CHECKPOINT_WRITE_TOOL_NAMES:
        diff = generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root)

    routed = await ControlToolRouter(
        state=state,
        approval_handler=approval_handler,
        skill_manager=skill_manager,
    ).execute(tc)
    if routed is not None:
        for event in routed.events:
            yield event
        result = routed.result
    else:
        result = await run_tool_with_timeout(tc, tool_registry, tool_ctx)

    yield store_result(tc, result, ctx, state, diff=None if result.is_error else diff, iteration_id=iteration_id, guardrail_controller=guardrail_controller, tool_registry=tool_registry)


def store_result(
    tc: ToolCallEvent,
    result: ToolResult,
    ctx: ContextBuilder,
    state: AgentState,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
    iteration_id: str = "",
    append_to_context: bool = True,
    guardrail_controller: ToolCallGuardrailController | None = None,
    tool_registry: ToolRegistry | None = None,
) -> AgentEvent:
    from backend.tools.base import MAX_TOOL_RESULT_CHARS, truncate_tool_result

    final_status = status_for_result(result, status)
    result_kind = result.result_kind or result_kind_for_tool(tc.name)
    limitation = result.limitation or (
        "unsandboxed background command"
        if tc.name == "run_command" and bool(tc.arguments.get("run_in_background"))
        else ""
    )
    # Auto-generate structured diff for file mutations when not explicitly provided
    if diff is None and tc.name in CHECKPOINT_WRITE_TOOL_NAMES and not result.is_error:
        diff = _extract_diff_from_tool_result(result.content)
    started_at = _tool_start_times(state).get(tc.id)
    duration_ms = result.duration_ms
    if duration_ms is None and isinstance(started_at, (int, float)):
        duration_ms = int(max(0.0, time.time() - started_at) * 1000)

    result = guardrail_after_call_result(
        guardrail_controller,
        tc,
        result,
        status=status,
        final_status=final_status,
        append_to_context=append_to_context,
    )

    # Per-tool result budget. Tools that self-bound and artifact their overflow
    # (read_file, web_fetch, run_command) set max_result_chars=None to opt out of
    # the backstop, so their compact summary isn't truncated a second time.
    cap: int | None = MAX_TOOL_RESULT_CHARS
    if tool_registry is not None:
        tool_obj = tool_registry.get_tool(tc.name)
        if tool_obj is not None:
            cap = getattr(tool_obj, "max_result_chars", MAX_TOOL_RESULT_CHARS)
    if cap is None:
        truncated = result
    else:
        truncated = replace(result, content=truncate_tool_result(result.content, cap))
    display_summary = display_summary_for_result(tc, truncated, status=final_status, diff=diff)
    issue = classify_tool_issue(tc, truncated, final_status)
    truncated = replace(
        truncated,
        status=final_status,
        duration_ms=duration_ms,
        display_summary=display_summary,
        result_kind=result_kind,
        limitation=limitation or truncated.limitation,
    )
    if truncated.status == "timeout" and truncated.limitation == "non-critical timeout":
        state.add_loop_guidance(
            f"Optional tool {tc.name} timed out. Do not retry it this turn; continue with the user-facing answer."
        )
    if issue and issue.model_observation:
        state.add_loop_guidance(issue.model_observation)
    if append_to_context:
        ctx.append_tool_result(tc.id, tc.name, truncated)
    elif truncated.content:
        state.add_loop_guidance(truncated.content)
    state.record_tool_call(
        tc.name,
        tc.arguments,
        truncated.to_context_string(),
        artifact_id=truncated.artifact_id,
        is_error=truncated.is_error,
        mutates=_tool_mutates(tc.name, tool_registry),
        status=final_status,
        source_url=truncated.source_url,
        extraction_status=truncated.extraction_status,
        content_preview=truncated.content_preview,
        evidence_type=truncated.evidence_type,
        provider=truncated.provider,
        provider_error_type=truncated.provider_error_type,
        error_kind=issue.error_kind if issue else None,
        user_summary=issue.user_summary if issue else None,
        developer_detail=issue.developer_detail if issue else None,
        projection=issue.projection if issue else None,
    )
    if truncated.evidence_type:
        state.evidence_records.append(
            EvidenceRecord(
                source_url=truncated.source_url or "",
                source_name=truncated.provider or "",
                retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                evidence_type=truncated.evidence_type,
                confidence=0.7 if truncated.evidence_type == "fetched" else 0.35,
                tool_call_id=tc.id,
                tool_name=tc.name,
            )
        )
    return AgentEvent.tool_result(
        id=tc.id,
        summary=truncated.content,
        artifact_id=truncated.artifact_id,
        is_error=truncated.is_error,
        diff=diff,
        source_url=truncated.source_url,
        extraction_status=truncated.extraction_status,
        content_preview=truncated.content_preview,
        evidence_type=truncated.evidence_type,
        status=final_status,
        duration_ms=duration_ms,
        display_summary=display_summary,
        result_kind=result_kind,
        limitation=limitation or truncated.limitation or "",
        provider=truncated.provider or "",
        provider_error_type=truncated.provider_error_type or "",
        error_info=issue.to_dict() if issue else None,
        error_kind=issue.error_kind if issue else "",
        user_summary=issue.user_summary if issue else "",
        developer_detail=issue.developer_detail if issue else "",
        recoverable=issue.recoverable if issue else True,
        projection=issue.projection if issue else "",
        group_id=iteration_id,
        step_id=tc.id,
        iteration_id=iteration_id,
        phase="tool",
    )


def _resolve_workspace_path_for_diff(file_path: str, workspace_root: Path | str | None) -> Path:
    path = Path(str(file_path))
    if path.is_absolute():
        return path
    if workspace_root:
        return Path(workspace_root).resolve() / path
    return path


def generate_diff(
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None = None,
) -> dict[str, Any] | None:
    if tool_name == "write_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        if file_path and content:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            inject_expected_hash(args, str(resolved_path))
            return generate_file_diff_payload(str(resolved_path), content)
    elif tool_name == "edit_file":
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if file_path and old_string:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            inject_expected_hash(args, str(resolved_path))
            return generate_edit_diff_payload(str(resolved_path), old_string, new_string)
    return None


def inject_expected_hash(args: dict[str, Any], file_path: str) -> None:
    if str(args.get("expected_hash") or "").strip():
        return
    path = Path(str(file_path))
    if not path.exists():
        args["expected_hash"] = ""
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    args["expected_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
