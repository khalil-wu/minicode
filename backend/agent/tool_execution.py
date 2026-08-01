from __future__ import annotations

import asyncio
import inspect
import difflib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.tools.contracts import EvidenceRecord
from backend.agent.message import AgentEvent
from backend.agent.runtime_spans import runtime_span_from_tool_context
from backend.agent.tool_execution_guardrails import (
    invalid_call_result as _invalid_call_result,
    rejection_result as _rejection_result,
)
from backend.agent.state import AgentState
from backend.agent.tool_events import (
    status_for_result,
    tool_call_start_event,
    tool_start_times as _tool_start_times,
)
from backend.agent.tool_issues import classify_tool_issue
from backend.tools.catalog import tool_spec_for
from backend.tools.tool_search import deferred_catalog_scope_allows
from backend.agent.control_tools import CONTROL_TOOL_NAMES, ControlToolRouter
from backend.async_cleanup import CANCELLATION_DRAIN_TIMEOUT_SECONDS, cancel_and_drain
from backend.agent.tool_projection import (
    display_summary_for_result,
    projection_for_tool,
    result_kind_for_tool,
)
from backend.agent.tool_runtime import (
    resolve_tool_timeout as _resolve_tool_timeout,
    tool_is_idempotent as _tool_is_idempotent,
    tool_mutates as _tool_mutates,
    tool_side_effect_kind as _tool_side_effect_kind,
)
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import (
    PermissionChecker,
    evaluate_permission_decision,
)
from backend.permissions.context import PermissionContext, PermissionDecision, ToolExecutionContext
from backend.permissions.review import generate_edit_diff_payload, generate_file_diff_payload
from backend.tools.base import PermissionLevel, ToolResult, validate_tool_input
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import is_subagent_permission_context, subagent_toolset_policy

logger = logging.getLogger(__name__)

SPECIAL_TOOL_NAMES = CONTROL_TOOL_NAMES
_DEFAULT_PARALLEL_TOOL_CONCURRENCY = 10


def _parallel_tool_concurrency(batch_size: int) -> int:
    raw = (
        os.environ.get("MINICODE_MAX_TOOL_CONCURRENCY")
        or os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY")
        or ""
    ).strip()
    try:
        configured = int(raw) if raw else _DEFAULT_PARALLEL_TOOL_CONCURRENCY
    except ValueError:
        configured = _DEFAULT_PARALLEL_TOOL_CONCURRENCY
    return min(batch_size, max(1, configured))


def _parallel_tool_batch_timeout() -> float | None:
    raw = os.environ.get("MINICODE_TOOL_BATCH_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return None
    try:
        timeout = float(raw)
    except ValueError:
        return None
    return timeout if timeout > 0 else None


def argument_has_value(args: dict[str, Any], field: str) -> bool:
    if field not in args or args.get(field) is None:
        return False
    value = args[field]
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def task_parallel_tasks_have_value(args: dict[str, Any] | None) -> bool:
    raw_tasks = (args or {}).get("parallel_tasks")
    if not isinstance(raw_tasks, list) or len(raw_tasks) < 2:
        return False
    valid = [
        item for item in raw_tasks
        if isinstance(item, dict)
        and argument_has_value(item, "description")
        and argument_has_value(item, "prompt")
    ]
    return len(valid) >= 2


def _tool_supports_diff(tool_name: str, tool_registry: ToolRegistry) -> bool:
    tool = tool_registry.get_tool(tool_name)
    return bool(tool is not None and tool.permission == PermissionLevel.DIFF_REVIEW)


def _tool_streams_output(tool_name: str, tool_registry: ToolRegistry) -> bool:
    tool = tool_registry.get_tool(tool_name)
    return bool(tool is not None and getattr(tool, "streams_output", False))


# The inline artifact preview follows Pi's shared tool-result contract below.
# Do not introduce a second, tool-execution-specific preview threshold here.
SKIP_FORCED_ARTIFACT_TOOL_NAMES = {"read_artifact"}
_CREATED_FILE_EDIT_TRACKER_KEY = "_created_file_edit_records"


@dataclass(frozen=True)
class _ToolBatchRuntime:
    ctx: ContextBuilder
    state: AgentState
    tool_registry: ToolRegistry
    tool_ctx: ToolExecutionContext
    iteration_id: str
    turn_id: str = ""


class StreamingToolStatus(str, Enum):
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    YIELDED = "yielded"
    CANCELLED = "cancelled"


@dataclass
class PrefetchedToolExecution:
    """Background execution started when a complete safe tool block arrives."""

    tool_call: ToolCallEvent
    task: asyncio.Task[ToolResult]
    started_epoch: float
    status: StreamingToolStatus = StreamingToolStatus.EXECUTING


@dataclass
class StreamingToolRecord:
    tool_call: ToolCallEvent
    status: StreamingToolStatus = StreamingToolStatus.QUEUED
    prefetched: PrefetchedToolExecution | None = None


def _tool_turn_id(tool_ctx: ToolExecutionContext) -> str:
    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    # QueryEngine's run_id is the typed turn identity stamped by
    # EventEnvelope. assistant_message_id is only the transport target.
    return str(
        metadata.get("run_id")
        or metadata.get("turn_id")
        or metadata.get("assistant_message_id")
        or ""
    ).strip()


def _artifact_store_from_tool_context(tool_ctx: ToolExecutionContext | None) -> Any | None:
    if tool_ctx is None:
        return None
    artifact_store = getattr(tool_ctx, "artifact_store", None)
    if artifact_store is not None:
        return artifact_store
    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    return metadata.get("artifact_store")


def _force_artifact_for_oversized_tool_result(
    tc: ToolCallEvent,
    result: ToolResult,
    tool_ctx: ToolExecutionContext | None,
    *,
    inline_limit: int | None = None,
) -> ToolResult:
    """Persist very large raw tool output and keep only stable metadata inline."""
    from backend.tools.base import MAX_TOOL_RESULT_LINES, truncate_text_head, truncate_tool_result

    content = result.content or ""
    if result.artifact_id or tc.name in SKIP_FORCED_ARTIFACT_TOOL_NAMES or inline_limit is None:
        return result
    truncation = truncate_text_head(
        content,
        max_lines=MAX_TOOL_RESULT_LINES,
        max_bytes=inline_limit,
    )
    if not truncation.truncated:
        return result

    artifact_store = _artifact_store_from_tool_context(tool_ctx)
    if artifact_store is None or not hasattr(artifact_store, "save"):
        return result

    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    line_count = len(content.splitlines())
    try:
        artifact_id = artifact_store.save(
            content=content,
            source=f"{tc.name}({tc.id})",
            type="tool_result",
            conversation_id=str(getattr(tool_ctx, "conversation_id", "") or ""),
            workspace_root=str(getattr(tool_ctx, "workspace_root", "") or ""),
        )
    except Exception as exc:
        logger.warning(
            "large tool result artifact save failed tool=%s call_id=%s chars=%s error=%r",
            tc.name,
            tc.id,
            len(content),
            exc,
        )
        return result

    summary = "\n".join(
        [
            "Large tool result stored as artifact.",
            f"tool: {tc.name}",
            f"artifact_id: {artifact_id}",
            f"original_bytes: {truncation.total_bytes}",
            f"line_count: {line_count}",
            f"content_hash: sha256:{content_hash}",
            f"Call read_artifact with artifact_id='{artifact_id}' (offset/limit accepted) for the full output.",
        ]
    )
    return replace(
        result,
        content=summary,
        artifact_id=artifact_id,
        artifact_preview=result.artifact_preview
        or truncate_tool_result(content),
        display_summary=result.display_summary or f"Stored large result from {tc.name} as artifact",
        limitation=result.limitation or "large result stored as artifact",
    )


def _metadata_bool(metadata: dict[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _metadata_string_list(metadata: dict[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _tool_call_is_read_only(tc: ToolCallEvent, tool_registry: ToolRegistry) -> bool:
    tool = tool_registry.get_tool(tc.name)
    if tool is not None:
        try:
            return bool(tool.is_read_only(tc.arguments))
        except Exception:
            return bool(getattr(tool, "read_only", False))
    return not _tool_mutates(tc.name, tool_registry, tc.arguments)


def _resolve_workspace_path_for_scope(raw_path: str, tool_ctx: ToolExecutionContext) -> Path:
    from backend.tools.path_resolution import _resolve_path

    return _resolve_path(raw_path, tool_ctx, allow_workspace_escape=False).resolve()


def _apply_patch_target_paths(patch_text: str) -> list[str] | None:
    try:
        from backend.tools.apply_patch_parser import patch_target_paths

        return patch_target_paths(patch_text)
    except Exception:
        return None


def _workspace_write_targets(tc: ToolCallEvent) -> list[str] | None:
    if tc.name in {"write_file", "edit_file"}:
        raw = tc.arguments.get("file_path")
        text = str(raw or "").strip()
        return [text] if text else []
    if tc.name == "apply_patch":
        patch = tc.arguments.get("patch")
        if not isinstance(patch, str):
            return []
        return _apply_patch_target_paths(patch)
    return None


def _path_is_within_any_scope(path: Path, scopes: list[Path]) -> bool:
    for scope in scopes:
        if path == scope:
            return True
        try:
            path.relative_to(scope)
            return True
        except ValueError:
            continue
    return False


def subagent_scope_guard_reason(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> str:
    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    read_only = _metadata_bool(metadata, "read_only")
    write_scope = _metadata_string_list(metadata, "write_scope")
    if not read_only and not write_scope:
        return ""

    if read_only and not _tool_call_is_read_only(tc, tool_registry):
        return (
            f"Blocked tool '{tc.name}' because this subagent is marked read_only. "
            "Return findings without modifying files, running mutating commands, or changing external state."
        )

    if not write_scope:
        return ""

    tool = tool_registry.get_tool(tc.name)
    may_mutate_workspace = bool(getattr(tool, "mutates_workspace", False))
    if not may_mutate_workspace or _tool_call_is_read_only(tc, tool_registry):
        return ""

    raw_targets = _workspace_write_targets(tc)
    if raw_targets is None:
        return (
            f"Blocked tool '{tc.name}' because this subagent has write_scope={write_scope} "
            "but the tool may mutate the workspace and its target paths cannot be proven to stay in scope."
        )
    if not raw_targets:
        return (
            f"Blocked tool '{tc.name}' because this subagent has write_scope={write_scope} "
            "but the target path could not be determined."
        )

    try:
        resolved_scopes = [
            _resolve_workspace_path_for_scope(scope, tool_ctx)
            for scope in write_scope
        ]
        resolved_targets = [
            _resolve_workspace_path_for_scope(target, tool_ctx)
            for target in raw_targets
        ]
    except Exception as exc:
        return f"Blocked tool '{tc.name}' because write_scope path validation failed: {exc}"

    outside = [
        str(target)
        for target in resolved_targets
        if not _path_is_within_any_scope(target, resolved_scopes)
    ]
    if outside:
        return (
            f"Blocked tool '{tc.name}' because it writes outside this subagent's write_scope. "
            f"Allowed scope(s): {', '.join(write_scope)}. Outside target(s): {', '.join(outside)}."
        )
    return ""


def disabled_tool_guard_reason(state: AgentState, tc: ToolCallEvent) -> str:
    if tc.name not in state.disabled_tools:
        return ""
    return f"Tool '{tc.name}' is disabled for this turn. Continue without calling it."


def stale_subagent_context_guard_reason(tool_ctx: ToolExecutionContext | None) -> str:
    """Reject tool work emitted by an obsolete child incarnation."""
    metadata = getattr(tool_ctx, "metadata", None) if tool_ctx is not None else None
    if not isinstance(metadata, dict):
        return ""
    role = str(metadata.get("agent_role") or "")
    mode = str(metadata.get("agent_mode") or "")
    if not role.startswith("subagent:") and mode != "subagent":
        return ""
    runtime = metadata.get("agent_runtime")
    accepts = getattr(runtime, "accepts_subagent_incarnation", None)
    if not callable(accepts):
        return "Subagent runtime identity is unavailable; refusing an unfenced tool call."
    subagent_id = str(metadata.get("run_id") or getattr(tool_ctx, "task_id", "") or "").strip()
    try:
        mailbox_epoch = int(metadata.get("mailbox_epoch"))
    except (TypeError, ValueError):
        mailbox_epoch = None
    if not subagent_id or not accepts(
        subagent_id,
        agent_path=str(metadata.get("agent_path") or ""),
        mailbox_epoch=mailbox_epoch,
        require_running=True,
    ):
        return f"Subagent {subagent_id or '<unknown>'} is no longer the current running incarnation."
    return ""


def normalize_tool_call_event(tc: ToolCallEvent, *, fallback_id: str = "") -> ToolCallEvent:
    args = dict(tc.arguments) if isinstance(tc.arguments, dict) else tc.arguments
    return replace(
        tc,
        id=str(tc.id or "").strip() or fallback_id,
        name=str(tc.name or "").strip(),
        arguments=args,
    )


def unwrap_deferred_tool_call(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    permission_context: PermissionContext | None = None,
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
    toolset_policy = subagent_toolset_policy() if is_subagent_permission_context(permission_context) else None
    get_view = getattr(tool_registry, "get_schema_view", None)
    view = (
        get_view(underlying_name, toolset_policy=toolset_policy)
        if callable(get_view)
        else None
    )
    if view is not None:
        if view.exposure != "deferred" or view.direct:
            return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
        if not deferred_catalog_scope_allows(view.runtime_metadata or {}, "default"):
            return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
        return replace(tc, name=underlying_name, arguments=underlying_args), ""
    spec = tool_spec_for(underlying_name, tool_registry)
    if spec.exposure != "deferred" or getattr(spec, "always_load", False):
        return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
    tool = tool_registry.get_tool(underlying_name)
    meta = tool.to_runtime_metadata() if tool is not None and hasattr(tool, "to_runtime_metadata") else {}
    if not deferred_catalog_scope_allows(meta, "default"):
        return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
    return replace(tc, name=underlying_name, arguments=underlying_args), ""


def missing_required_tool_argument_names(tc: ToolCallEvent, tool_registry: ToolRegistry) -> list[str]:
    # Compute on a local copy; do NOT mutate the caller's tc. This is a query
    # helper called from history-safety / prefetch paths on tc objects that are
    # also written to history. Reassigning tc.arguments here was a hidden source
    # of prefetch-signature drift (args rewritten under the prefetch's feet).
    args = dict(tc.arguments) if isinstance(tc.arguments, dict) else {}
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
        if isinstance(field, str) and not argument_has_value(args, field)
    ]


def missing_required_tool_argument_reason(
    state: AgentState | None,
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
) -> str:
    del state
    if tc.name == "task" and task_parallel_tasks_have_value(tc.arguments):
        return ""
    missing = [
        name
        for name in tool_spec_for(tc.name, tool_registry).required_args
        if not argument_has_value(tc.arguments or {}, name)
    ]
    if not missing:
        return ""
    return (
        f"Invalid tool call for '{tc.name}': missing required argument(s): {missing}. "
        f"Received keys: {list((tc.arguments or {}).keys())}."
    )


def _dedupe_tool_call_ids(tool_calls: list[ToolCallEvent]) -> list[ToolCallEvent]:
    """Force every tool_call id in a batch to be non-empty and unique.

    OpenAI-compatible providers require each assistant ``tool_calls`` entry to
    have exactly one matching ``tool`` reply, keyed by id. Streamed providers
    (DeepSeek-style) can emit blank or repeated ids across a batch; a collision
    makes two tool results share one id, so the next request is rejected for a
    missing/duplicate reply. We suffix repeats with ``:dup{n}`` so history and
    tool results stay in lockstep. Idempotent: an already-unique batch is
    returned unchanged (suffix only applies on a real collision), so running
    this again on the same batch is a no-op.
    """
    seen: set[str] = set()
    result: list[ToolCallEvent] = []
    for index, tc in enumerate(tool_calls, 1):
        call_id = str(tc.id or "").strip() or f"tool_{index}"
        if call_id in seen:
            suffix = 2
            while f"{call_id}:dup{suffix}" in seen:
                suffix += 1
            call_id = f"{call_id}:dup{suffix}"
        seen.add(call_id)
        duplicate = tc.id != call_id and bool(str(tc.id or "").strip())
        result.append(tc if tc.id == call_id else replace(tc, id=call_id, duplicate_id=duplicate))
    return result


def prepare_tool_call_sequence(
    state: AgentState,
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext | None = None,
) -> list[ToolCallEvent]:
    """Normalize a model tool-call batch once for history and execution."""
    del state, tool_registry, tool_ctx
    prepared: list[ToolCallEvent] = []
    for index, raw_tc in enumerate(tool_calls, 1):
        prepared.append(normalize_tool_call_event(raw_tc, fallback_id=f"tool_{index}"))
    # Provider call ids must be non-empty and unique before history commit and
    # execution. Arguments remain exactly model-authored and are validated by
    # the tool schema; the runtime does not guess or rewrite them.
    return _dedupe_tool_call_ids(prepared)


def _matching_prefetch(
    prefetched_results: dict[str, PrefetchedToolExecution] | None,
    tc: ToolCallEvent,
) -> PrefetchedToolExecution | None:
    if not prefetched_results:
        return None
    prefetched = prefetched_results.get(tc.id)
    if prefetched is None:
        return None
    if (
        str(prefetched.tool_call.name or "") != str(tc.name or "")
        or prefetched.tool_call.arguments != tc.arguments
    ):
        return None
    return prefetched


def _take_matching_prefetch(
    prefetched_results: dict[str, PrefetchedToolExecution] | None,
    tc: ToolCallEvent,
) -> PrefetchedToolExecution | None:
    prefetched = _matching_prefetch(prefetched_results, tc)
    if prefetched is None or prefetched_results is None:
        return None
    taken = prefetched_results.pop(tc.id, None)
    if taken is not None:
        taken.status = StreamingToolStatus.YIELDED
    return taken


def _execution_exception_result(exc: BaseException, *, label: str = "Tool execution") -> ToolResult:
    """Keep raw exception text diagnostic-only, never model-visible."""
    return ToolResult(
        content=f"{label} failed ({type(exc).__name__}).",
        is_error=True,
        status="failed",
        error_kind="execution_error",
        user_summary="工具执行失败。",
        developer_detail=str(exc),
        recoverable=True,
        projection="error",
        model_observation="The tool execution failed. Check the arguments or try another approach.",
    )


async def _await_prefetched_result(prefetched: PrefetchedToolExecution) -> ToolResult:
    try:
        return await prefetched.task
    except asyncio.CancelledError:
        return ToolResult(
            content=f"Prefetched tool '{prefetched.tool_call.name}' was cancelled before completion.",
            is_error=True,
            status="failed",
        )
    except Exception as exc:
        return _execution_exception_result(exc, label="Prefetched tool execution")


def cancel_prefetched_tool_executions(
    prefetched_results: dict[str, PrefetchedToolExecution] | None,
) -> None:
    if not prefetched_results:
        return
    for prefetched in prefetched_results.values():
        if not prefetched.task.done():
            prefetched.task.cancel()
        prefetched.status = StreamingToolStatus.CANCELLED
    prefetched_results.clear()


def maybe_start_prefetched_tool_execution(
    raw_tc: ToolCallEvent,
    *,
    state: AgentState,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    permission_context: PermissionContext | None,
    tool_ctx: ToolExecutionContext,
    existing: dict[str, PrefetchedToolExecution],
) -> PrefetchedToolExecution | None:
    """Compatibility hook for the retired speculative execution path.

    Pi's contract is stricter: a tool call is executable only after the
    provider response has settled and the complete assistant item has been
    committed. Starting work from a streaming frame can leak reads, network
    requests, or other observable effects when the response is retried,
    cancelled, or truncated. Keep this symbol for older callers, but make the
    safe behavior an unconditional no-op; ``execute_tool_batch`` is the only
    execution boundary.
    """
    del raw_tc, state, tool_registry, permission_checker, permission_context, tool_ctx, existing
    return None

class StreamingToolExecutor:
    """Collect tool-call diagnostics without executing provider stream data.

    Pi executes tools only after the assistant response is settled. This
    object remains as the stream-side collector used by recovery/control code,
    but it deliberately has no speculative execution capability.
    """

    def __init__(
        self,
        *,
        state: AgentState,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        permission_context: PermissionContext | None,
        tool_ctx: ToolExecutionContext,
        execution_limit: int | None = None,
    ) -> None:
        self.state = state
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.permission_context = permission_context
        self.tool_ctx = tool_ctx
        self.execution_limit = execution_limit
        self.prefetched_results: dict[str, PrefetchedToolExecution] = {}
        self.tracked_tools: dict[str, StreamingToolRecord] = {}
        self.blocked_by_order = False

    def add_tool(self, tool_call: ToolCallEvent) -> PrefetchedToolExecution | None:
        tc_id = str(tool_call.id or "")
        if tc_id and tc_id not in self.tracked_tools:
            self.tracked_tools[tc_id] = StreamingToolRecord(tool_call=tool_call)
        record = self.tracked_tools.get(tc_id)
        # Do not execute from a provider stream. The complete assistant item
        # is committed first; the transition controller then invokes the
        # ordinary ordered batch runner. ``prefetched_results`` remains empty
        # by design and is retained only for compatibility with older callers.
        if record is not None:
            record.status = StreamingToolStatus.QUEUED
        return None

    def add_tools(self, tool_calls: list[ToolCallEvent]) -> None:
        for tool_call in tool_calls:
            self.add_tool(tool_call)

    def status_snapshot(self) -> tuple[tuple[str, str], ...]:
        """Stable diagnostic projection in provider tool-call order."""
        return tuple(
            (
                tool_id,
                (
                    record.prefetched.status
                    if record.prefetched is not None
                    else record.status
                ).value,
            )
            for tool_id, record in self.tracked_tools.items()
        )

    def mark_yielded(self, tool_call_id: str) -> None:
        record = self.tracked_tools.get(str(tool_call_id or ""))
        if record is not None:
            record.status = StreamingToolStatus.YIELDED
            if record.prefetched is not None:
                record.prefetched.status = StreamingToolStatus.YIELDED

    def get_completed_results(self, ordered_tool_calls: list[ToolCallEvent]) -> list[PrefetchedToolExecution]:
        completed: list[PrefetchedToolExecution] = []
        for tool_call in ordered_tool_calls:
            prefetched = _matching_prefetch(
                self.prefetched_results,
                normalize_tool_call_event(tool_call),
            )
            if prefetched is None or not prefetched.task.done():
                break
            record = self.tracked_tools.get(str(tool_call.id or ""))
            if record is not None and record.status is StreamingToolStatus.EXECUTING:
                record.status = StreamingToolStatus.COMPLETED
            completed.append(prefetched)
        return completed

    def get_remaining_results(self, ordered_tool_calls: list[ToolCallEvent]) -> list[PrefetchedToolExecution]:
        completed_ids = {item.tool_call.id for item in self.get_completed_results(ordered_tool_calls)}
        remaining: list[PrefetchedToolExecution] = []
        for tool_call in ordered_tool_calls:
            prefetched = _matching_prefetch(
                self.prefetched_results,
                normalize_tool_call_event(tool_call),
            )
            if prefetched is not None and prefetched.tool_call.id not in completed_ids:
                remaining.append(prefetched)
        return remaining

    def cancel_remaining(self) -> None:
        cancel_prefetched_tool_executions(self.prefetched_results)
        for record in self.tracked_tools.values():
            if record.status is not StreamingToolStatus.YIELDED:
                record.status = StreamingToolStatus.CANCELLED
        self.tracked_tools.clear()
        # The executor is reused when the provider retries the same model turn
        # (fallback/max-output recovery). A non-prefetchable block from the
        # abandoned attempt must not permanently suppress safe prefetch in the
        # fresh attempt.
        self.blocked_by_order = False


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
    if tc.arguments_repaired:
        return (
            f"Tool '{name}' had malformed provider JSON and was not executed. "
            "Send the same intended call again with a complete JSON object."
        )
    if tc.duplicate_id and _tool_mutates(name, tool_registry, tc.arguments):
        return (
            f"Duplicate tool-call id for side-effectful tool '{name}' was not executed. "
            "Issue one new call with a unique id if the action is still needed."
        )
    if (
        tool_registry.get_tool(name) is None
        and name not in SPECIAL_TOOL_NAMES
    ):
        available_names = tool_registry.list_tools()
        suggestions = difflib.get_close_matches(name, available_names, n=5, cutoff=0.25)
        available = ", ".join(suggestions) or ", ".join(available_names[:5]) or "none"
        return (
            f"Tool '{name}' does not exist. Closest available tools: {available}. "
            "Choose an appropriate tool or answer without tools."
        )
    return ""


async def snapshot_before_write(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    tool_registry: ToolRegistry,
) -> None:
    if not _tool_supports_diff(tc.name, tool_registry):
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

    stale_reason = stale_subagent_context_guard_reason(tool_ctx)
    if stale_reason:
        return ToolResult(
            content=stale_reason,
            is_error=True,
            status="blocked",
            display_summary="Stale subagent tool call rejected",
        )

    # Validate required arguments before invoking the tool so empty strings and
    # empty containers are handled consistently with the provider schema.
    missing = missing_required_tool_argument_names(tc, tool_registry)
    if missing:
        reason = missing_required_tool_argument_reason(None, tc, tool_registry)
        received = list(tc.arguments.keys())
        return ToolResult(
            content=reason or (
                f"Tool '{tc.name}' is missing required argument(s): {missing}. "
                f"Received keys: {received}. Re-read the tool schema and retry with all required fields."
            ),
            is_error=True,
        )

    if getattr(tc, "_pre_tool_hook_applied", False):
        delattr(tc, "_pre_tool_hook_applied")
    else:
        pre_result = await _apply_pre_tool_hook(tc, tool_ctx)
        if hasattr(tc, "_pre_tool_hook_applied"):
            delattr(tc, "_pre_tool_hook_applied")
        if pre_result is not None:
            return pre_result
    hook_mgr = get_hook_manager()

    await snapshot_before_write(tc, tool_ctx, tool_registry)
    changed_file = changed_file_event_payload(tc, tool_ctx, tool_registry)
    metadata_had_tool_id = "_current_tool_call_id" in tool_ctx.metadata
    previous_tool_id = tool_ctx.metadata.get("_current_tool_call_id")
    tool_ctx.metadata["_current_tool_call_id"] = tc.id

    tm = tool_ctx.task_manager
    artifact_store = getattr(tool_ctx, "artifact_store", None)
    owner_token = None
    if artifact_store is not None and hasattr(artifact_store, "bind_owner"):
        owner_token = artifact_store.bind_owner(
            tool_ctx.conversation_id or str(tool_ctx.metadata.get("conversation_id") or ""),
            str(tool_ctx.workspace_root or ""),
        )
    try:
        if tm:
            try:
                managed = tm.create(
                    kind="tool_run",
                    awaitable=tool_registry.execute(tc.name, tc.arguments, context=tool_ctx),
                )
                result = await tm.wait(managed.id)
            except Exception as exc:
                result = _execution_exception_result(exc, label="Managed tool execution")
        else:
            result = await tool_registry.execute(tc.name, tc.arguments, context=tool_ctx)
    finally:
        if owner_token is not None:
            artifact_store.reset_owner(owner_token)
        if metadata_had_tool_id:
            tool_ctx.metadata["_current_tool_call_id"] = previous_tool_id
        else:
            tool_ctx.metadata.pop("_current_tool_call_id", None)

    if hook_mgr and not result.is_error:
        try:
            post = await hook_mgr.run_post_tool(
                tc.name,
                tc.arguments,
                result.content or "",
                tool_call_id=tc.id,
                session_id=tool_ctx.session_id,
                permission_mode=tool_ctx.permission.mode,
            )
            _remember_hook_model_context(tc, post)
            await _emit_hook_system_message(tool_ctx, post)
            if tc.name.startswith("mcp__") and post.updated_mcp_tool_output is not None:
                replacement = post.updated_mcp_tool_output
                if not isinstance(replacement, str):
                    replacement = json.dumps(replacement, ensure_ascii=False, default=str)
                result = replace(result, content=replacement)
            if post.blocked:
                result = ToolResult(
                    content=f"Tool stopped by PostToolUse hook: {post.message}",
                    is_error=True,
                    status="blocked",
                )
        except Exception as exc:
            logger.warning("post_tool hook failed: %s", exc)
    elif hook_mgr and result.is_error:
        try:
            post = await hook_mgr.run_post_tool_failure(
                tc.name,
                tc.arguments,
                result.content or "",
                tool_call_id=tc.id,
                session_id=tool_ctx.session_id,
                permission_mode=tool_ctx.permission.mode,
            )
            _remember_hook_model_context(tc, post)
            await _emit_hook_system_message(tool_ctx, post)
        except Exception as exc:
            logger.warning("post_tool_failure hook failed: %s", exc)

    if changed_file and not result.is_error:
        emit = getattr(tool_ctx, "emit_event", None)
        if emit:
            try:
                await emit("file.changed", changed_file)
            except Exception as exc:
                logger.debug("file change emit failed: %s", exc)
        if hook_mgr:
            try:
                await hook_mgr.run_file_changed(
                    str(changed_file.get("path") or ""),
                    event=str(changed_file.get("event") or "modified"),
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                )
            except Exception as exc:
                logger.warning("file_changed hook failed: %s", exc)

    return result


def _remember_hook_model_context(tc: ToolCallEvent, hook_result: Any) -> None:
    parts: list[str] = []
    feedback = str(getattr(hook_result, "feedback", "") or "").strip()
    additional = str(getattr(hook_result, "additional_context", "") or "").strip()
    if feedback:
        parts.append(feedback)
    if additional:
        parts.append(additional)
    if not parts:
        return
    existing = getattr(tc, "_hook_model_context", None)
    values = list(existing) if isinstance(existing, list) else []
    values.extend(parts)
    setattr(tc, "_hook_model_context", values)


async def _emit_hook_system_message(tool_ctx: ToolExecutionContext, hook_result: Any) -> None:
    message = str(getattr(hook_result, "system_message", "") or "").strip()
    emit = getattr(tool_ctx, "emit_event", None)
    if not message or not callable(emit):
        return
    try:
        await emit("system_notice", {"content": message})
    except Exception as exc:
        logger.debug("hook systemMessage emit failed: %s", exc)


async def _apply_pre_tool_hook(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
) -> ToolResult | None:
    from backend.hooks import get_hook_manager

    tc.arguments = dict(tc.arguments or {})
    hook_mgr = get_hook_manager()
    if hook_mgr is not None:
        try:
            pre = await hook_mgr.run_pre_tool(
                tc.name,
                tc.arguments,
                tool_call_id=tc.id,
                session_id=tool_ctx.session_id,
                permission_mode=tool_ctx.permission.mode,
            )
            _remember_hook_model_context(tc, pre)
            await _emit_hook_system_message(tool_ctx, pre)
            if pre.has_permission_decision:
                setattr(tc, "_pre_tool_hook_permission_decision", pre.permission_decision)
                setattr(tc, "_pre_tool_hook_permission_reason", pre.permission_decision_reason)
            if pre.blocked:
                return ToolResult(content=f"Tool blocked by hook: {pre.message}", is_error=True)
            if isinstance(pre.updated_input, dict):
                tc.arguments = dict(pre.updated_input)
        except Exception as exc:
            logger.warning("pre_tool hook failed: %s", exc)
    setattr(tc, "_pre_tool_hook_applied", True)
    return None


def _display_path_for_tool_arg(raw_path: str, tool_ctx: ToolExecutionContext) -> tuple[Path, str]:
    workspace_root = Path(tool_ctx.workspace_root).resolve() if tool_ctx.workspace_root else None
    path = Path(raw_path)
    resolved = path.resolve() if path.is_absolute() else ((workspace_root / path).resolve() if workspace_root else path.resolve())
    display_path = raw_path
    if workspace_root:
        try:
            display_path = resolved.relative_to(workspace_root).as_posix()
        except ValueError:
            display_path = resolved.as_posix()
    return resolved, display_path


def changed_file_event_payload(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    tool_registry: ToolRegistry,
) -> dict[str, Any] | None:
    if not _tool_supports_diff(tc.name, tool_registry):
        return None
    raw_path = str(tc.arguments.get("file_path") or "").strip()
    if not raw_path:
        return None
    resolved, display_path = _display_path_for_tool_arg(raw_path, tool_ctx)
    existed_before = resolved.exists()
    event_type = "created" if tc.name == "write_file" and not existed_before else "modified"
    return {
        "path": display_path,
        "event": event_type,
    }


async def run_tool_with_timeout(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    *,
    iteration_id: str = "",
) -> ToolResult:
    timeout = _resolve_tool_timeout(tc.name, tool_registry, tc.arguments)
    deadline = getattr(tool_ctx, "deadline_monotonic", None)
    if deadline is not None:
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            timeout = 0.0
        elif timeout is None:
            timeout = remaining
        else:
            timeout = min(timeout, remaining)
    execution_tool_ctx = tool_context_with_live_output(
        tc,
        tool_ctx,
        tool_registry,
        iteration_id=iteration_id,
    )
    t0 = time.perf_counter()
    execution_task = asyncio.create_task(run_tool(tc, tool_registry, execution_tool_ctx))
    try:
        if timeout is None:
            result = await execution_task
        else:
            done, _ = await asyncio.wait({execution_task}, timeout=max(0.0, timeout))
            if execution_task not in done:
                await cancel_and_drain(
                    [execution_task],
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label=f"timed out tool {tc.name}",
                )
                raise asyncio.TimeoutError
            result = execution_task.result()
    except asyncio.CancelledError:
        await cancel_and_drain(
            [execution_task],
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"cancelled tool {tc.name}",
        )
        raise
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return ToolResult(
            content=(
                f"Tool '{tc.name}' timed out after {timeout:.0f}s. "
                "The operation did not finish and no complete result is available. "
                "Do not retry the identical call; break the operation into smaller steps or try a different approach."
            ),
            is_error=True,
            duration_ms=elapsed,
            status="timeout",
            limitation="timeout",
            display_summary=f"Timed out: {tc.name}",
            result_kind=result_kind_for_tool(tc.name, tool_registry),
        )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if result.duration_ms is None:
        result = replace(result, duration_ms=elapsed)
    return result


def tool_output_delta_events(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    tool_registry: ToolRegistry,
    turn_id: str = "",
    iteration_id: str = "",
) -> list[AgentEvent]:
    if result.is_error or not _tool_streams_output(tc.name, tool_registry) or not result.content:
        return []

    # A buffered tool has no genuine provider-side delta boundaries. Emit the
    # complete normalized result once; live subprocess tools use
    # ``stream_callback`` above for their real chunks. A local fixed-size
    # re-chunking layer only invents protocol events and can split UTF-8/text
    # boundaries without adding information.
    return [
        AgentEvent.tool_output_delta(
            id=tc.id,
            output=result.content,
            stream="stdout",
            turn_id=turn_id,
            iteration_id=iteration_id,
            step_id=tc.id,
        )
    ]


def _emit_runtime_event(tool_ctx: ToolExecutionContext, event: AgentEvent) -> Awaitable[None] | None:
    emit_event = getattr(tool_ctx, "emit_event", None)
    if emit_event is None:
        return None
    return emit_event(event.type, dict(event.data))


def _tool_runtime_span_event(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    *,
    event: str,
    iteration_id: str = "",
    phase: str = "tool",
    status: str = "running",
    summary: str = "",
    detail: str = "",
    waiting_on: str = "",
    blocking_reason: str = "",
    ui_visible: bool = True,
    duration_ms: int | None = None,
    data: dict[str, Any] | None = None,
) -> AgentEvent:
    payload_data = dict(data or {})
    if detail:
        payload_data["detail"] = detail
    return runtime_span_from_tool_context(
        event,
        span_id=f"tool:{tc.id}",
        tool_ctx=tool_ctx,
        iteration_id=iteration_id,
        phase=phase,
        status=status,
        label=tc.name,
        summary=summary,
        tool_call_id=tc.id,
        tool_name=tc.name,
        waiting_on=waiting_on,
        blocking_reason=blocking_reason,
        ui_visible=ui_visible,
        duration_ms=duration_ms,
        data=payload_data or None,
    )


async def _emit_tool_runtime_span(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    *,
    event: str,
    iteration_id: str = "",
    phase: str = "tool",
    status: str = "running",
    summary: str = "",
    detail: str = "",
    waiting_on: str = "",
    blocking_reason: str = "",
    ui_visible: bool = True,
    duration_ms: int | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    pending = _emit_runtime_event(
        tool_ctx,
        _tool_runtime_span_event(
            tc,
            tool_ctx,
            event=event,
            iteration_id=iteration_id,
            phase=phase,
            status=status,
            summary=summary,
            detail=detail,
            waiting_on=waiting_on,
            blocking_reason=blocking_reason,
            ui_visible=ui_visible,
            duration_ms=duration_ms,
            data=data,
        ),
    )
    if pending is not None:
        await pending


async def _emit_tool_completed_runtime_span(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    result_event: AgentEvent,
    *,
    iteration_id: str = "",
) -> None:
    payload = result_event.data if isinstance(result_event.data, dict) else {}
    final_status = str(payload.get("status") or "").strip().lower()
    failed = final_status in {"failed", "blocked", "timeout"} or bool(payload.get("is_error"))
    duration_value = payload.get("duration_ms")
    duration_ms = int(duration_value) if isinstance(duration_value, int) else None
    summary = str(
        payload.get("display_summary")
        or payload.get("user_summary")
        or payload.get("summary")
        or ("Failed " + tc.name if failed else "Completed " + tc.name)
    )
    await _emit_tool_runtime_span(
        tc,
        tool_ctx,
        event="tool.completed",
        iteration_id=iteration_id,
        phase="tool",
        status="failed" if failed else "completed",
        summary=summary,
        detail=str(payload.get("limitation") or ""),
        duration_ms=duration_ms,
        data={
            "tool_status": final_status,
            "result_kind": payload.get("result_kind") or "",
            "projection": payload.get("projection") or "",
        },
    )


async def _emit_tool_first_output_span(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    *,
    iteration_id: str = "",
    detail: str = "",
) -> None:
    first_output_key = "_tool_first_output_ids"
    raw_ids = tool_ctx.metadata.setdefault(first_output_key, set())
    if isinstance(raw_ids, set):
        if tc.id in raw_ids:
            return
        raw_ids.add(tc.id)
    elif isinstance(raw_ids, list):
        if tc.id in raw_ids:
            return
        raw_ids.append(tc.id)
    else:
        tool_ctx.metadata[first_output_key] = {tc.id}
    await _emit_tool_runtime_span(
        tc,
        tool_ctx,
        event="tool.first_output",
        iteration_id=iteration_id,
        phase="tool",
        status="running",
        summary=f"Streaming output from {tc.name}",
        detail=detail,
    )


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
    tool_registry: ToolRegistry,
    *,
    iteration_id: str = "",
) -> ToolExecutionContext:
    if not _tool_streams_output(tc.name, tool_registry):
        return tool_ctx
    emit_event = getattr(tool_ctx, "emit_event", None)
    fallback_stream = getattr(tool_ctx, "stream_callback", None)
    if emit_event is None and fallback_stream is None:
        return tool_ctx

    async def _stream_tool_output(output: str, stream: str = "stdout") -> None:
        if not output:
            return
        stream_name = stream if stream in {"stdout", "stderr"} else "stdout"
        await _emit_tool_first_output_span(
            tc,
            tool_ctx,
            iteration_id=iteration_id,
            detail=f"Received {stream_name} output",
        )
        _mark_tool_output_streamed(tool_ctx, tc.id)
        if emit_event is not None:
            payload = {
                "id": tc.id,
                "output": output,
                "stream": stream_name,
                "iteration_id": iteration_id,
                "step_id": tc.id,
            }
            turn_id = _tool_turn_id(tool_ctx)
            if turn_id:
                payload["turn_id"] = turn_id
            await emit_event(
                "tool_output_delta",
                payload,
            )
            return
        if fallback_stream is not None:
            try:
                parameters = tuple(inspect.signature(fallback_stream).parameters.values())
                accepts_varargs = any(
                    parameter.kind == inspect.Parameter.VAR_POSITIONAL
                    for parameter in parameters
                )
                positional_count = sum(
                    parameter.kind
                    in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                accepts_varargs = False
                positional_count = 1
            callback_args = (output, stream_name, tc.id)
            result = fallback_stream(
                *(
                    callback_args
                    if accepts_varargs
                    else callback_args[:positional_count]
                )
            )
            if inspect.isawaitable(result):
                await result

    return replace(tool_ctx, stream_callback=_stream_tool_output)


def batch_tool_calls(
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
) -> list[tuple[bool, list[ToolCallEvent]]]:
    """Group tool calls for parallel execution, preserving model order.

    Only *consecutive* concurrency-safe tools are batched together (cc's
    partitionToolCalls pattern). A mutating tool that sits between two reads is
    NOT reordered past them: `read A → edit B → read C` stays in that order, so
    a read that depends on an earlier edit's result cannot run before it. Each
    maximal run of adjacent safe tools becomes one parallel batch; every other
    call starts its own batch.
    """
    if not tool_calls:
        return []
    batches: list[tuple[bool, list[ToolCallEvent]]] = []
    for tc in tool_calls:
        tool = tool_registry.get_tool(tc.name)
        is_safe = tool.is_concurrency_safe(tc.arguments) if tool else False
        if is_safe and batches and batches[-1][0]:
            batches[-1][1].append(tc)
        else:
            batches.append((is_safe, [tc]))
    return batches


async def _finalize_tool_result(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_ctx: ToolExecutionContext,
    iteration_id: str,
    turn_id: str,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
    tool_registry: ToolRegistry,
) -> AsyncIterator[AgentEvent]:
    """Persist and emit one terminal result for every tool exit path."""
    if not result.is_error:
        _refresh_read_file_hashes_after_write(tc, diff, tool_ctx)
        _track_created_file_edits(tc, diff, tool_ctx)
    removed_records = _reconcile_removed_created_file_edits(tool_ctx)
    if removed_records:
        superseded_ids = list(dict.fromkeys([
            *result.superseded_tool_call_ids,
            *(str(item.get("tool_call_id") or "") for item in removed_records),
        ]))
        superseded_ids = [value for value in superseded_ids if value]
        removed_paths = list(dict.fromkeys([
            *result.removed_file_paths,
            *(str(item.get("display_path") or item.get("resolved_path") or "") for item in removed_records),
        ]))
        removed_paths = [value for value in removed_paths if value]
        result = replace(
            result,
            superseded_tool_call_ids=superseded_ids,
            removed_file_paths=removed_paths,
        )
        emit = getattr(tool_ctx, "emit_event", None)
        if emit is not None:
            for item in removed_records:
                try:
                    await emit("file.changed", {
                        "path": str(item.get("display_path") or item.get("resolved_path") or ""),
                        "event": "deleted",
                        "temporary": True,
                        "supersedes_tool_call_id": str(item.get("tool_call_id") or ""),
                    })
                except Exception as exc:
                    logger.debug("temporary file removal emit failed: %s", exc)
    events = store_result_events(
        tc,
        result,
        ctx,
        state,
        status=status,
        diff=diff,
        iteration_id=iteration_id,
        turn_id=turn_id,
        tool_ctx=tool_ctx,
        tool_registry=tool_registry,
    )
    for event in events:
        yield event
    if events:
        await _emit_tool_completed_runtime_span(
            tc,
            tool_ctx,
            events[-1],
            iteration_id=iteration_id,
        )


async def _reject_tool_call(
    tc: ToolCallEvent,
    auto_queue: list[ToolCallEvent],
    result: ToolResult,
    *,
    runtime: _ToolBatchRuntime,
    started_epoch: float | None = None,
    status: str = "blocked",
    prefetched_results: dict[str, PrefetchedToolExecution] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Flush pending auto-queue, emit a blocked tool-call event, and store the result."""
    async for ev in _flush_queue(
        auto_queue,
        ctx=runtime.ctx,
        state=runtime.state,
        tool_registry=runtime.tool_registry,
        tool_ctx=runtime.tool_ctx,
        iteration_id=runtime.iteration_id,
        prefetched_results=prefetched_results,
    ):
        yield ev
    auto_queue.clear()
    epoch = started_epoch if started_epoch is not None else time.time()
    yield tool_call_start_event(
        tc,
        started_epoch=epoch,
        iteration_id=runtime.iteration_id,
        tool_registry=runtime.tool_registry,
        turn_id=runtime.turn_id,
    )
    async for event in _finalize_tool_result(
        tc,
        result,
        ctx=runtime.ctx,
        state=runtime.state,
        status=status,
        diff=None,
        iteration_id=runtime.iteration_id,
        turn_id=runtime.turn_id,
        tool_ctx=runtime.tool_ctx,
        tool_registry=runtime.tool_registry,
    ):
        yield event


def _track_created_file_edits(
    tc: ToolCallEvent,
    diff: dict[str, Any] | None,
    tool_ctx: ToolExecutionContext,
) -> None:
    if not isinstance(diff, dict):
        return
    files = diff.get("files")
    if not isinstance(files, list):
        return
    tracker = tool_ctx.metadata.setdefault(_CREATED_FILE_EDIT_TRACKER_KEY, {})
    if not isinstance(tracker, dict):
        tracker = {}
        tool_ctx.metadata[_CREATED_FILE_EDIT_TRACKER_KEY] = tracker
    for item in files:
        if not isinstance(item, dict) or str(item.get("status") or "").lower() not in {"added", "created", "new"}:
            continue
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            continue
        resolved, display_path = _display_path_for_tool_arg(raw_path, tool_ctx)
        key = str(resolved).replace("\\", "/").casefold()
        tracker[key] = {
            "tool_call_id": tc.id,
            "resolved_path": str(resolved),
            "display_path": display_path,
        }


def _refresh_read_file_hashes_after_write(
    tc: ToolCallEvent,
    diff: dict[str, Any] | None,
    tool_ctx: ToolExecutionContext,
) -> None:
    """Advance optimistic edit guards after a successful in-turn write.

    Read-time hashes intentionally protect against external changes, but they
    must be replaced after the agent itself writes a file. Otherwise a second
    edit in the same turn is rejected with its own previous hash and the model
    can spend the rest of the turn repeating stale edits.
    """
    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    hashes = metadata.get("_read_file_hashes")
    if not isinstance(hashes, dict):
        return
    raw_paths: list[str] = []
    if tc.name in {"edit_file", "write_file"}:
        raw_path = str(tc.arguments.get("file_path") or "").strip()
        if raw_path:
            raw_paths.append(raw_path)
    elif tc.name == "apply_patch" and isinstance(diff, dict):
        for item in diff.get("files") or []:
            if isinstance(item, dict):
                for key in ("path", "old_path"):
                    raw_path = str(item.get(key) or "").strip()
                    if raw_path:
                        raw_paths.append(raw_path)
    for raw_path in raw_paths:
        path = _resolve_workspace_path_for_diff(raw_path, tool_ctx.workspace_root)
        try:
            content = path.read_text(encoding="utf-8")
            path_key = str(path)
            hashes.pop(path_key, None)
            hashes[path_key] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        except (OSError, UnicodeDecodeError):
            # A deleted/renamed path must not retain the old hash.
            hashes.pop(str(path), None)


def _reconcile_removed_created_file_edits(
    tool_ctx: ToolExecutionContext,
) -> list[dict[str, str]]:
    tracker = tool_ctx.metadata.get(_CREATED_FILE_EDIT_TRACKER_KEY)
    if not isinstance(tracker, dict) or not tracker:
        return []
    removed: list[dict[str, str]] = []
    for key, raw_record in list(tracker.items()):
        if not isinstance(raw_record, dict):
            tracker.pop(key, None)
            continue
        resolved_path = str(raw_record.get("resolved_path") or "").strip()
        if not resolved_path or Path(resolved_path).exists():
            continue
        removed.append({
            "tool_call_id": str(raw_record.get("tool_call_id") or ""),
            "resolved_path": resolved_path,
            "display_path": str(raw_record.get("display_path") or resolved_path),
        })
        tracker.pop(key, None)
    return removed


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
    prefetched_results: dict[str, PrefetchedToolExecution] | None = None,
    prepared_tool_calls: list[ToolCallEvent] | None = None,
    execution_limit: int | None = None,
    execution_limit_reason: str = "",
) -> AsyncIterator[AgentEvent]:
    auto_queue: list[ToolCallEvent] = []
    iteration_id = f"iter:{max(1, state.iterations)}"
    runtime = _ToolBatchRuntime(
        ctx=ctx,
        state=state,
        tool_registry=tool_registry,
        tool_ctx=tool_ctx,
        iteration_id=iteration_id,
        turn_id=_tool_turn_id(tool_ctx),
    )
    prepared_calls = (
        list(prepared_tool_calls)
        if prepared_tool_calls is not None
        else prepare_tool_call_sequence(state, tool_calls, tool_registry, tool_ctx)
    )

    for index, prepared_call in enumerate(prepared_calls, 1):
        tc = normalize_tool_call_event(
            prepared_call,
            fallback_id=f"tool_{index}",
        )
        if execution_limit is not None and index > max(0, execution_limit):
            started_epoch = time.time()
            _tool_start_times(state)[tc.id] = started_epoch
            reason = execution_limit_reason.strip() or (
                "Tool call was not executed because the explicit per-turn tool-call budget was exhausted."
            )
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _rejection_result(
                    tc,
                    reason,
                    display_summary="Tool-call budget exhausted",
                    result_kind=result_kind_for_tool(tc.name, tool_registry),
                    error_kind="execution_limit",
                    user_summary="本轮工具调用预算已耗尽。",
                    recoverable=True,
                    projection="warning",
                    model_observation=(
                        f"The {tc.name} call was not executed because the explicit "
                        "per-turn tool-call budget was exhausted. Finish with available evidence."
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue
        tc, bridge_block_reason = unwrap_deferred_tool_call(tc, tool_registry, permission_context)
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
                    error_kind="routing_error",
                    user_summary="延迟工具路由未能解析，本次调用未执行。",
                    recoverable=True,
                    projection="error",
                    model_observation=(
                        f"The deferred {tc.name} call could not be routed. "
                        "Use tool_search/tool_describe or call the resolved tool directly."
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue
        prefetched = None
        started_epoch = time.time()
        _tool_start_times(state)[tc.id] = started_epoch

        invalid_reason = invalid_tool_call_guard_reason(tc, tool_registry)
        if invalid_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    invalid_reason,
                    display_summary="Invalid tool call",
                    result_kind="generic",
                    error_kind="validation_error",
                    user_summary="工具调用格式无效。",
                    projection="error",
                    model_observation=f"The {tc.name} call is invalid and was not executed.",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        required_reason = missing_required_tool_argument_reason(state, tc, tool_registry)
        if required_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _invalid_call_result(
                    tc,
                    required_reason,
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        pre_tool_result = await _apply_pre_tool_hook(tc, tool_ctx)
        if pre_tool_result is not None:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                pre_tool_result,
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        prefetched = _matching_prefetch(prefetched_results, tc)
        if prefetched is not None:
            started_epoch = prefetched.started_epoch
            _tool_start_times(state)[tc.id] = started_epoch

        disabled_reason = disabled_tool_guard_reason(state, tc)
        if disabled_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    disabled_reason,
                    display_summary="Tool disabled",
                    result_kind=result_kind_for_tool(tc.name, tool_registry),
                    error_kind="tool_disabled",
                    user_summary="该工具本轮不可用。",
                    projection="status",
                    model_observation=f"The {tc.name} tool is disabled for this turn.",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        scope_guard_reason = subagent_scope_guard_reason(tc, tool_registry, tool_ctx)
        if scope_guard_reason:
            yield AgentEvent.permission_decision(
                tool_call_id=tc.id,
                tool_name=tc.name,
                decision="deny",
                source="policy",
                permission_level=PermissionLevel.ALWAYS_DENY.value,
                message=scope_guard_reason,
                capability={"allowed": False, "reason": scope_guard_reason},
                approval_policy="deny",
                matched_rule={"source": "subagent_scope", "rule": "write_scope"},
                risk="high",
                scope={
                    "workspace_scope": tool_ctx.permission.workspace_scope,
                    "boundary": "subagent_scope",
                    "task_id": tool_ctx.task_id,
                },
                expiry="policy",
            )
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _rejection_result(
                    tc,
                    scope_guard_reason,
                    display_summary="Subagent scope blocked",
                    result_kind=result_kind_for_tool(tc.name, tool_registry),
                    error_kind="permission_required",
                    user_summary="该工具调用超出子 Agent 的允许范围。",
                    projection="approval",
                    model_observation=f"The {tc.name} call is outside this subagent's allowed scope.",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        perm_tool = tool_registry.get_tool(tc.name)

        # Validate the declared schema, then tool-owned semantics, before any
        # permission prompt or execution (CC validateInput ordering).
        if perm_tool is not None:
            validate_msg = validate_tool_input(perm_tool, tc.arguments)
            if validate_msg:
                async for ev in _reject_tool_call(
                    tc, auto_queue,
                    _rejection_result(
                        tc, validate_msg,
                        display_summary="Invalid tool input",
                        result_kind="generic",
                        error_kind="validation_error",
                        user_summary="工具输入未通过结构化校验。",
                        projection="error",
                        model_observation=f"The {tc.name} input failed validation and must be corrected.",
                    ),
                    runtime=runtime,
                    started_epoch=started_epoch,
                    prefetched_results=prefetched_results,
                ):
                    yield ev
                continue

        permission_decision = evaluate_permission_decision(
            permission_checker,
            tc.name,
            tc.arguments,
            context=permission_context,
            tool=perm_tool,
        )
        perm = permission_decision.permission_level
        pre_hook_decision = str(
            getattr(tc, "_pre_tool_hook_permission_decision", "") or ""
        ).strip().lower()
        if pre_hook_decision == "ask" and perm == PermissionLevel.AUTO:
            # Claude Code treats PreToolUse `ask` as an approval floor. Static
            # and capability denials below remain authoritative.
            perm = PermissionLevel.CONFIRM
        denial = (
            permission_decision.capability_reason
            if not permission_decision.capability_allowed
            else permission_decision.matched_rule
            if permission_decision.decision == "deny"
            else None
        )
        if permission_decision.decision != "allow" and getattr(permission_context, "mode", "") != "auto":
            yield AgentEvent.permission_decision(
                tool_call_id=tc.id,
                tool_name=tc.name,
                decision=permission_decision.decision,
                source="policy",
                permission_level=perm.value,
                message=(
                    permission_decision.capability_reason
                    if not permission_decision.capability_allowed
                    else f"Denied by {permission_decision.matched_rule_source}: {permission_decision.matched_rule}"
                    if permission_decision.decision == "deny"
                    else ""
                ),
                capability={
                    "allowed": permission_decision.capability_allowed,
                    "reason": permission_decision.capability_reason,
                },
                approval_policy=permission_decision.approval_policy,
                matched_rule={
                    "source": permission_decision.matched_rule_source,
                    "rule": permission_decision.matched_rule,
                },
                risk=permission_decision.risk,
                scope=permission_decision.scope,
                expiry=permission_decision.expiry,
            )

        if denial or perm == PermissionLevel.ALWAYS_DENY:
            msg = denial or f"Tool '{tc.name}' is blocked by policy"
            from backend.hooks import get_hook_manager

            hook_mgr = get_hook_manager()
            permission_denied_retry = False
            if hook_mgr:
                try:
                    denied_hook = await hook_mgr.run_permission_denied(
                        tc.name,
                        tc.arguments,
                        reason=msg,
                        permission_level=perm.value,
                        tool_call_id=tc.id,
                        session_id=tool_ctx.session_id,
                        permission_mode=tool_ctx.permission.mode,
                    )
                    permission_denied_retry = denied_hook.retry
                except Exception as exc:
                    logger.warning("permission_denied hook failed: %s", exc)
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    msg,
                    error_kind="permission_required",
                    user_summary="该工具调用被权限策略阻止。",
                    projection="approval",
                    model_observation=(
                        f"The {tc.name} call was blocked by permission policy. "
                        "The PermissionDenied hook indicated the call may be retried."
                        if permission_denied_retry
                        else f"The {tc.name} call was blocked by permission policy."
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        # All guards passed — emit tool_call event to UI (now shows only real executions)
        yield tool_call_start_event(
            tc,
            started_epoch=started_epoch,
            iteration_id=iteration_id,
            tool_registry=tool_registry,
            turn_id=runtime.turn_id,
        )
        await _emit_tool_runtime_span(
            tc,
            tool_ctx,
            event="tool.preparing",
            iteration_id=iteration_id,
            phase="tool",
            status="running",
            summary=f"Preparing {tc.name}",
            ui_visible=False,
        )

        if perm == PermissionLevel.AUTO and tc.name not in SPECIAL_TOOL_NAMES:
            await _emit_tool_runtime_span(
                tc,
                tool_ctx,
                event="tool.queued",
                iteration_id=iteration_id,
                phase="tool",
                status="running",
                summary=f"Queued {tc.name}",
                detail="Queued for parallel execution",
                ui_visible=False,
            )
            auto_queue.append(tc)
            continue

        async for ev in _flush_queue(
            auto_queue,
            ctx=ctx,
            state=state,
            tool_registry=tool_registry,
            tool_ctx=tool_ctx,
            iteration_id=iteration_id,
            prefetched_results=prefetched_results,
        ):
            yield ev
        auto_queue = []

        async for ev in execute_serial(
            tc,
            perm=perm,
            permission_decision=permission_decision,
            ctx=ctx,
            state=state,
            tool_registry=tool_registry,
            tool_ctx=tool_ctx,
            approval_handler=approval_handler,
            skill_manager=skill_manager,
            iteration_id=iteration_id,
            prefetched=prefetched,
        ):
            yield ev

    async for ev in _flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx, iteration_id=iteration_id, prefetched_results=prefetched_results):
        yield ev


async def _flush_queue(
    queue: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    iteration_id: str = "",
    prefetched_results: dict[str, PrefetchedToolExecution] | None = None,
) -> AsyncIterator[AgentEvent]:
    if not queue:
        return
    turn_id = _tool_turn_id(tool_ctx)

    for is_concurrent, batch in batch_tool_calls(queue, tool_registry):
        if is_concurrent and len(batch) > 1:
            diffs_by_id = {
                tc.id: generate_diff(
                    tc.name,
                    tc.arguments,
                    workspace_root=tool_ctx.workspace_root,
                    tool_ctx=tool_ctx,
                )
                for tc in batch
                if _tool_supports_diff(tc.name, tool_registry)
            }
            # Sibling abort (Claude Code pattern): if a bash/command tool
            # fails, cancel all other parallel tools in the same batch.
            cancelled_result = ToolResult(
                content="Cancelled: parallel tool call errored.",
                is_error=True,
                status="failed",
            )
            timeout_result = ToolResult(
                content="Parallel tool batch timed out before all calls completed.",
                is_error=True,
                status="timeout",
            )

            async def _run_parallel_tool(tc: ToolCallEvent) -> ToolResult:
                prefetched = _take_matching_prefetch(prefetched_results, tc)
                if prefetched is not None:
                    return await _await_prefetched_result(prefetched)
                try:
                    return await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)
                except Exception as exc:
                    return _execution_exception_result(exc)

            # Claude Code bounds parallel tool execution to ten calls by
            # default and exposes one environment override. The legacy
            # MiniCode override remains accepted for existing deployments.
            max_concurrent_tools = _parallel_tool_concurrency(len(batch))
            batch_timeout = _parallel_tool_batch_timeout()
            batch_deadline = (
                asyncio.get_running_loop().time() + batch_timeout
                if batch_timeout is not None
                else None
            )
            pending: dict[asyncio.Task[ToolResult], ToolCallEvent] = {}
            results_by_id: dict[str, ToolResult] = {}
            next_executable_index = 0
            next_emit_index = 0

            def _record_batch_result(tc: ToolCallEvent, result: ToolResult) -> None:
                results_by_id[tc.id] = result

            async def _start_ready_tasks() -> None:
                nonlocal next_executable_index
                while next_executable_index < len(batch) and len(pending) < max_concurrent_tools:
                    tc = batch[next_executable_index]
                    detail = f"Parallel batch {next_executable_index + 1}/{len(batch)}"
                    await _emit_tool_runtime_span(
                        tc,
                        tool_ctx,
                        event="tool.started",
                        iteration_id=iteration_id,
                        phase="tool",
                        status="running",
                        summary=f"Running {tc.name}",
                        detail=detail,
                        data={"parallel_index": next_executable_index + 1, "parallel_total": len(batch)},
                    )
                    pending[asyncio.create_task(_run_parallel_tool(tc))] = tc
                    next_executable_index += 1

            def _pop_ready_ordered_results() -> list[tuple[ToolCallEvent, ToolResult]]:
                nonlocal next_emit_index
                ready: list[tuple[ToolCallEvent, ToolResult]] = []
                while next_emit_index < len(batch):
                    tc = batch[next_emit_index]
                    result = results_by_id.pop(tc.id, None)
                    if result is None:
                        break
                    ready.append((tc, result))
                    next_emit_index += 1
                return ready

            async def _emit_ordered_ready_results() -> AsyncIterator[AgentEvent]:
                for ready_tc, ready_result in _pop_ready_ordered_results():
                    if not _tool_output_was_streamed(tool_ctx, ready_tc.id):
                        if _tool_streams_output(ready_tc.name, tool_registry) and ready_result.content and not ready_result.is_error:
                            await _emit_tool_first_output_span(
                                ready_tc,
                                tool_ctx,
                                iteration_id=iteration_id,
                                detail="Buffered command output available",
                            )
                        for event in tool_output_delta_events(
                            ready_tc,
                            ready_result,
                            tool_registry=tool_registry,
                            turn_id=turn_id,
                            iteration_id=iteration_id,
                        ):
                            yield event
                    async for event in _finalize_tool_result(
                        ready_tc,
                        ready_result,
                        ctx=ctx,
                        state=state,
                        diff=None if ready_result.is_error else diffs_by_id.get(ready_tc.id),
                        iteration_id=iteration_id,
                        turn_id=turn_id,
                        tool_ctx=tool_ctx,
                        tool_registry=tool_registry,
                    ):
                        yield event

            try:
                await _start_ready_tasks()
                batch_timed_out = False
                while pending:
                    wait_timeout = None
                    if batch_deadline is not None:
                        wait_timeout = max(0.0, batch_deadline - asyncio.get_running_loop().time())
                        if wait_timeout <= 0:
                            batch_timed_out = True
                            break
                    done, _ = await asyncio.wait(
                        pending.keys(),
                        timeout=wait_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
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
                            result = _execution_exception_result(exc)
                        _record_batch_result(tc, result)
                        if result.is_error and (
                            _tool_streams_output(tc.name, tool_registry)
                            or _tool_mutates(tc.name, tool_registry, tc.arguments)
                        ):
                            should_cancel_siblings = True
                    async for event in _emit_ordered_ready_results():
                        yield event

                    if should_cancel_siblings:
                        remaining = list(pending.items())
                        for task, _tc in remaining:
                            task.cancel()
                        # Use gather for parallel cancellation — consistent with
                        # the finally block. Sequential await delays sibling cleanup
                        # behind the slowest-to-cancel task (e.g. subprocess teardown).
                        await cancel_and_drain(
                            (task for task, _ in remaining),
                            timeout=0.5,
                            label="tool batch siblings",
                        )
                        for task, tc in remaining:
                            _record_batch_result(tc, cancelled_result)
                            pending.pop(task, None)
                        for tc in batch[next_executable_index:]:
                            _record_batch_result(tc, cancelled_result)
                        next_executable_index = len(batch)
                        async for event in _emit_ordered_ready_results():
                            yield event
                        break

                    await _start_ready_tasks()

                if batch_timed_out:
                    unfinished = list(pending.items())
                    for task, _tc in unfinished:
                        task.cancel()
                    await cancel_and_drain(
                        (task for task, _ in unfinished),
                        timeout=0.5,
                        label="timed out tool batch",
                    )
                    for task, tc in unfinished:
                        _record_batch_result(tc, timeout_result)
                        pending.pop(task, None)
                    for tc in batch[next_executable_index:]:
                        _record_batch_result(tc, timeout_result)
                    next_executable_index = len(batch)
                    async for event in _emit_ordered_ready_results():
                        yield event

            finally:
                # Cancel + await any still-pending tasks. Reached when the
                # generator is closed early (consumer stops iterating / interrupt
                # unwinds through the `async for`). Cancelling without awaiting
                # raises CancelledError at an arbitrary await point and the
                # coroutine's own finally (metadata restore, checkpoint cleanup)
                # may not run before the frame is gone — orphaned subprocess /
                # file handles and "Task was destroyed but it is pending!".
                if pending:
                    for task in pending:
                        task.cancel()
                    await cancel_and_drain(
                        pending,
                        timeout=0.5,
                        label="tool batch cleanup",
                    )

            while next_emit_index < len(batch):
                tc = batch[next_emit_index]
                results_by_id.setdefault(tc.id, cancelled_result)
                async for event in _emit_ordered_ready_results():
                    yield event
        else:
            for tc in batch:
                await _emit_tool_runtime_span(
                    tc,
                    tool_ctx,
                    event="tool.started",
                    iteration_id=iteration_id,
                    phase="tool",
                    status="running",
                    summary=f"Running {tc.name}",
                )
                diff = (
                    generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx)
                    if _tool_supports_diff(tc.name, tool_registry)
                    else None
                )
                prefetched = _take_matching_prefetch(prefetched_results, tc)
                if prefetched is not None:
                    result = await _await_prefetched_result(prefetched)
                else:
                    result = await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)
                if not _tool_output_was_streamed(tool_ctx, tc.id):
                    if _tool_streams_output(tc.name, tool_registry) and result.content and not result.is_error:
                        await _emit_tool_first_output_span(
                            tc,
                            tool_ctx,
                            iteration_id=iteration_id,
                            detail="Buffered command output available",
                        )
                    for event in tool_output_delta_events(
                        tc,
                        result,
                        tool_registry=tool_registry,
                        turn_id=turn_id,
                        iteration_id=iteration_id,
                    ):
                        yield event
                async for event in _finalize_tool_result(
                    tc,
                    result,
                    ctx=ctx,
                    state=state,
                    diff=None if result.is_error else diff,
                    iteration_id=iteration_id,
                    turn_id=turn_id,
                    tool_ctx=tool_ctx,
                    tool_registry=tool_registry,
                ):
                    yield event


async def _await_approval_within_turn_deadline(
    approval_handler: Callable,
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
) -> dict[str, Any]:
    """Wait for an approval decision without outliving the turn's deadline.

    An approval wait is unbounded work performed on behalf of the turn, so it
    must respect the same wall-clock boundary as tool execution. Without this
    a turn whose user stepped away stays pinned on the handler's own timeout,
    long past ``max_turn_seconds``.
    """
    deadline = getattr(tool_ctx, "deadline_monotonic", None)
    if deadline is None:
        return await approval_handler(tc.id)

    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        return {
            "action": "reject",
            "guidance": (
                "The turn's time budget was exhausted before this approval was answered. "
                "The action was not performed."
            ),
        }
    approval_task = asyncio.create_task(approval_handler(tc.id))
    done, _ = await asyncio.wait({approval_task}, timeout=remaining)
    if approval_task not in done:
        await cancel_and_drain(
            [approval_task],
            timeout=0.1,
            label=f"approval wait for {tc.name}",
        )
        return {
            "action": "reject",
            "guidance": (
                "The turn's time budget was exhausted while waiting for approval. "
                "The action was not performed."
            ),
        }
    return approval_task.result()


async def execute_serial(
    tc: ToolCallEvent,
    *,
    perm: PermissionLevel,
    permission_decision: PermissionDecision,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    approval_handler: Callable | None,
    skill_manager: Any | None,
    iteration_id: str = "",
    prefetched: PrefetchedToolExecution | None = None,
) -> AsyncIterator[AgentEvent]:
    diff: dict[str, Any] | None = None
    turn_id = _tool_turn_id(tool_ctx)
    declared_permission = getattr(tool_registry.get_tool(tc.name), "permission", None)
    needs_diff_review = perm == PermissionLevel.DIFF_REVIEW or declared_permission == PermissionLevel.DIFF_REVIEW

    if perm in (PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW):
        diff = generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx) if needs_diff_review else None
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        hook_requested_allow = (
            str(getattr(tc, "_pre_tool_hook_permission_decision", "") or "").strip().lower()
            == "allow"
        )
        # CC lets a PreToolUse allow skip the tool's ordinary interactive
        # prompt, but settings ask rules and capability floors still win.
        hook_overridable_sources = {"tool", "mode", "default", "external_checker"}
        permission_allowed_by_hook = (
            hook_requested_allow
            and permission_decision.matched_rule_source in hook_overridable_sources
        )
        if hook_mgr and not permission_allowed_by_hook:
            try:
                permission_hook = await hook_mgr.run_permission_request(
                    tc.name,
                    tc.arguments,
                    reason="tool requires user approval",
                    permission_level=perm.value,
                    tool_call_id=tc.id,
                    session_id=tool_ctx.session_id,
                    permission_mode=tool_ctx.permission.mode,
                )
                _remember_hook_model_context(tc, permission_hook)
                if permission_hook.blocked:
                    message = permission_hook.message or permission_hook.feedback or "permission request blocked by hook"
                    yield AgentEvent.permission_decision(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        decision=permission_hook.permission_decision or "deny",
                        permission_level=perm.value,
                        message=message,
                        capability={"allowed": True, "reason": "Capability boundary allows this tool call."},
                        approval_policy=perm.value,
                        matched_rule={"source": "hook", "rule": "permission_request"},
                        scope={"workspace_scope": tool_ctx.permission.workspace_scope},
                        expiry="call",
                    )
                    result = ToolResult(content=f"Permission request blocked by hook: {message}", is_error=True)
                    async for event in _finalize_tool_result(
                        tc,
                        result,
                        ctx=ctx,
                        state=state,
                        diff=diff,
                        iteration_id=iteration_id,
                        turn_id=turn_id,
                        tool_ctx=tool_ctx,
                        tool_registry=tool_registry,
                    ):
                        yield event
                    return
                if isinstance(permission_hook.updated_input, dict):
                    tc.arguments = dict(permission_hook.updated_input)
                    tool = tool_registry.get_tool(tc.name)
                    validation_error = validate_tool_input(tool, tc.arguments) if tool is not None else ""
                    if validation_error:
                        result = _invalid_call_result(tc, validation_error)
                        async for event in _finalize_tool_result(
                            tc,
                            result,
                            ctx=ctx,
                            state=state,
                            diff=None,
                            iteration_id=iteration_id,
                            turn_id=turn_id,
                            tool_ctx=tool_ctx,
                            tool_registry=tool_registry,
                        ):
                            yield event
                        return
                    diff = (
                        generate_diff(
                            tc.name,
                            tc.arguments,
                            workspace_root=tool_ctx.workspace_root,
                            tool_ctx=tool_ctx,
                        )
                        if needs_diff_review
                        else None
                    )
                if permission_hook.has_permission_decision:
                    yield AgentEvent.permission_decision(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        decision=permission_hook.permission_decision,
                        permission_level=perm.value,
                        message=permission_hook.permission_decision_reason or permission_hook.message,
                        capability={"allowed": True, "reason": "Capability boundary allows this tool call."},
                        approval_policy=perm.value,
                        matched_rule={"source": "hook", "rule": "permission_request"},
                        scope={"workspace_scope": tool_ctx.permission.workspace_scope},
                        expiry="call",
                    )
                if permission_hook.permission_decision == "allow":
                    permission_allowed_by_hook = True
            except Exception as exc:
                logger.warning("permission_request hook failed: %s", exc)
        if not permission_allowed_by_hook:
            await _emit_tool_runtime_span(
                tc,
                tool_ctx,
                event="approval.waiting",
                iteration_id=iteration_id,
                phase="approval",
                status="running",
                summary=f"Waiting for approval: {tc.name}",
                detail="Awaiting explicit approval before this tool can run",
                waiting_on="user",
                blocking_reason="approval_required",
            )
            yield AgentEvent.approval_request(
                tool_call_id=tc.id,
                tool_name=tc.name,
                args=tc.arguments,
                diff=diff,
                source_agent=str(tool_ctx.metadata.get("run_id") or tool_ctx.metadata.get("agent_role") or "").strip(),
                source_thread=str(tool_ctx.conversation_id or tool_ctx.metadata.get("conversation_id") or tool_ctx.session_id or "").strip(),
                source_tool=tc.name,
            )
            if approval_handler:
                approval = await _await_approval_within_turn_deadline(
                    approval_handler,
                    tc,
                    tool_ctx,
                )
                if approval.get("action") == "reject":
                    guidance = approval.get("guidance", "user rejected this action")
                    result = ToolResult(content=f"Operation rejected: {guidance}", is_error=True)
                    # Preserve the proposed diff on rejection so the UI can show what
                    # would have been applied (cc shows the rejected edit's diff).
                    async for event in _finalize_tool_result(
                        tc, result, ctx=ctx, state=state, diff=diff,
                        iteration_id=iteration_id, turn_id=turn_id,
                        tool_ctx=tool_ctx,
                        tool_registry=tool_registry,
                    ):
                        yield event
                    return
                if approval.get("action") == "partial":
                    decisions = approval.get("decisions", {})
                    target = tc.arguments.get("file_path") or tc.arguments.get("path") or ""
                    rejected = [p for p, d in decisions.items() if d == "rejected"]
                    if tc.name == "apply_patch" and rejected:
                        result = ToolResult(content="Operation rejected because one or more patch files were rejected.", is_error=True)
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=diff,
                            iteration_id=iteration_id, turn_id=turn_id,
                            tool_ctx=tool_ctx,
                            tool_registry=tool_registry,
                        ):
                            yield event
                        return
                    if target and any(target.endswith(rp) or rp.endswith(target) for rp in rejected):
                        result = ToolResult(content=f"Operation rejected for file: {target}", is_error=True)
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=diff,
                            iteration_id=iteration_id, turn_id=turn_id,
                            tool_ctx=tool_ctx,
                            tool_registry=tool_registry,
                        ):
                            yield event
                        return
            else:
                # The centralized policy asked for approval and this entry
                # point has nobody to ask. Fail closed for every such call,
                # including unknown/open-world MCP tools whose side effects
                # cannot be classified reliably. Unattended callers opt out
                # explicitly with permission mode "bypass".
                result = _rejection_result(
                    tc,
                    (
                        f"Tool '{tc.name}' requires approval, "
                        "but this run has no approval channel. Configure permission mode "
                        "'bypass' for unattended runs, or run it from a session that can "
                        "prompt the user."
                    ),
                    display_summary="Approval unavailable",
                    result_kind=result_kind_for_tool(tc.name, tool_registry),
                    error_kind="permission_required",
                    user_summary="该操作需要审批，但当前运行没有审批入口。",
                    projection="approval",
                    model_observation=(
                        f"The {tc.name} call was not executed: it requires approval and this "
                        "run cannot prompt anyone. Continue with actions that do not need approval."
                    ),
                )
                async for event in _finalize_tool_result(
                    tc, result, ctx=ctx, state=state, diff=diff,
                    iteration_id=iteration_id, turn_id=turn_id,
                    tool_ctx=tool_ctx,
                    tool_registry=tool_registry,
                ):
                    yield event
                return

    if diff is None and _tool_supports_diff(tc.name, tool_registry):
        diff = generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx)

    await _emit_tool_runtime_span(
        tc,
        tool_ctx,
        event="tool.started",
        iteration_id=iteration_id,
        phase="tool",
        status="running",
        summary=f"Running {tc.name}",
    )

    control_router = ControlToolRouter(
        state=state,
        approval_handler=approval_handler,
        skill_manager=skill_manager,
        await_response=(
            (
                lambda control_tc: _await_approval_within_turn_deadline(
                    approval_handler,
                    control_tc,
                    tool_ctx,
                )
            )
            if approval_handler is not None
            else None
        ),
    )
    for event in control_router.pre_wait_events(tc):
        yield event
    routed = await control_router.execute(tc)
    if routed is not None:
        for event in routed.events:
            yield event
        result = routed.result
    elif prefetched is not None:
        result = await _await_prefetched_result(prefetched)
    else:
        result = await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)

    async for event in _finalize_tool_result(
        tc,
        result,
        ctx=ctx,
        state=state,
        diff=None if result.is_error else diff,
        iteration_id=iteration_id,
        turn_id=turn_id,
        tool_ctx=tool_ctx,
        tool_registry=tool_registry,
    ):
        yield event


def store_result(
    tc: ToolCallEvent,
    result: ToolResult,
    ctx: ContextBuilder,
    state: AgentState,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
    iteration_id: str = "",
    turn_id: str = "",
    tool_ctx: ToolExecutionContext | None = None,
    tool_registry: ToolRegistry | None = None,
) -> AgentEvent:
    from backend.tools.base import MAX_TOOL_RESULT_CHARS, truncate_tool_result

    final_status = status_for_result(result, status)
    result_kind = result.result_kind or result_kind_for_tool(tc.name, tool_registry)
    limitation = result.limitation or ""
    started_at = _tool_start_times(state).get(tc.id)
    duration_ms = result.duration_ms
    if duration_ms is None and isinstance(started_at, (int, float)):
        duration_ms = int(max(0.0, time.time() - started_at) * 1000)

    # Per-tool result budget. Tools that self-bound and artifact their overflow
    # (read_file, web_fetch, run_command) set max_result_chars=None to opt out of
    # the backstop, so their compact summary isn't truncated a second time.
    cap: int | None = MAX_TOOL_RESULT_CHARS
    if tool_registry is not None:
        tool_obj = tool_registry.get_tool(tc.name)
        if tool_obj is not None:
            cap = getattr(tool_obj, "max_result_chars", MAX_TOOL_RESULT_CHARS)
    result_for_issue = result
    result = _force_artifact_for_oversized_tool_result(
        tc,
        result,
        tool_ctx,
        inline_limit=cap,
    )
    if cap is None:
        truncated = result
    else:
        truncated = replace(result, content=truncate_tool_result(result.content, cap))
    display_summary = display_summary_for_result(
        tc,
        truncated,
        status=final_status,
        diff=diff,
        tool_registry=tool_registry,
    )
    issue = classify_tool_issue(tc, result_for_issue, final_status)
    issue_projection = issue.projection if issue else ""
    tool_projection = projection_for_tool(tc.name, tool_registry)
    side_effect_kind = _tool_side_effect_kind(tc.name, tool_registry, tc.arguments)
    idempotent = _tool_is_idempotent(tc.name, tool_registry, tc.arguments) if tool_registry is not None else False
    idempotency_key = ""
    if tool_registry is not None:
        tool_obj = tool_registry.get_tool(tc.name)
        get_key = getattr(tool_obj, "idempotency_key", None)
        if callable(get_key):
            try:
                idempotency_key = str(get_key(tc.arguments) or "")
            except Exception:
                idempotency_key = ""
    truncated = replace(
        truncated,
        status=final_status,
        duration_ms=duration_ms,
        display_summary=display_summary,
        result_kind=result_kind,
        limitation=limitation or truncated.limitation,
    )
    context_result = truncated
    hook_context = getattr(tc, "_hook_model_context", None)
    if isinstance(hook_context, list):
        context_parts = [str(value).strip() for value in hook_context if str(value).strip()]
        if context_parts:
            context_result = replace(
                truncated,
                content=(
                    f"{truncated.content}\n\nHook context:\n"
                    + "\n\n".join(context_parts)
                ),
            )
    ctx.append_tool_result(tc.id, tc.name, context_result)
    state.record_tool_call(
        tc.name,
        tc.arguments,
        context_result.to_context_string(),
        artifact_id=truncated.artifact_id,
        is_error=truncated.is_error,
        mutates=_tool_mutates(tc.name, tool_registry, tc.arguments),
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
        recoverable=issue.recoverable if issue else True,
        projection=issue.projection if issue else None,
        model_observation=issue.model_observation if issue else None,
        turn_id=turn_id or None,
        iteration_id=iteration_id or f"iter:{max(1, state.iterations)}",
    )
    if truncated.evidence_type:
        state.evidence_records.append(
            EvidenceRecord(
                source_url=truncated.source_url or "",
                source_name=truncated.provider or "",
                retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                evidence_type=truncated.evidence_type,
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
        activity_kind=tool_projection.activity_kind,
        limitation=limitation or truncated.limitation or "",
        provider=truncated.provider or "",
        provider_error_type=truncated.provider_error_type or "",
        error_info=issue.to_dict() if issue else None,
        error_kind=issue.error_kind if issue else "",
        user_summary=issue.user_summary if issue else "",
        developer_detail=issue.developer_detail if issue else "",
        recoverable=issue.recoverable if issue else True,
        projection=issue_projection,
        turn_id=turn_id,
        group_id=iteration_id,
        step_id=tc.id,
        iteration_id=iteration_id,
        phase="tool",
        side_effect_kind=side_effect_kind,
        idempotent=idempotent,
        idempotency_key=idempotency_key,
        output_files=truncated.output_files,
        superseded_tool_call_ids=truncated.superseded_tool_call_ids,
        removed_file_paths=truncated.removed_file_paths,
    )


def store_result_events(
    tc: ToolCallEvent,
    result: ToolResult,
    ctx: ContextBuilder,
    state: AgentState,
    *,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
    iteration_id: str = "",
    turn_id: str = "",
    tool_ctx: ToolExecutionContext | None = None,
    tool_registry: ToolRegistry | None = None,
) -> list[AgentEvent]:
    event = store_result(
        tc,
        result,
        ctx,
        state,
        status=status,
        diff=diff,
        iteration_id=iteration_id,
        turn_id=turn_id,
        tool_ctx=tool_ctx,
        tool_registry=tool_registry,
    )
    return [event]


def _resolve_workspace_path_for_diff(file_path: str, workspace_root: Path | str | None) -> Path:
    path = Path(str(file_path))
    if path.is_absolute():
        return path.resolve()
    if workspace_root:
        # Resolve so "sub/../sub/a.txt" and "./sub/a.txt" collapse to the same
        # key read_file recorded (which always .resolve()s, like Claude Code's
        # expandPath). Without this a legitimately-read file could be rejected
        # on a spelling variation with a "re-read the file" error that won't fix it.
        return (Path(workspace_root).resolve() / path).resolve()
    return path.resolve()


def generate_diff(
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None = None,
    tool_ctx: ToolExecutionContext | None = None,
) -> dict[str, Any] | None:
    _meta = tool_ctx.metadata if (tool_ctx is not None and isinstance(tool_ctx.metadata, dict)) else {}
    _read_time_hashes = _meta.get("_read_file_hashes")
    if not isinstance(_read_time_hashes, dict):
        _read_time_hashes = None
    if tool_name == "write_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        if file_path and content:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            inject_expected_hash(args, str(resolved_path), read_time_hashes=_read_time_hashes)
            return generate_file_diff_payload(str(resolved_path), content)
    elif tool_name == "edit_file":
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if file_path and old_string:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            inject_expected_hash(args, str(resolved_path), read_time_hashes=_read_time_hashes)
            return generate_edit_diff_payload(str(resolved_path), old_string, new_string)
    elif tool_name == "apply_patch":
        from backend.tools.apply_patch import build_apply_patch_diff_payload

        patch_text = args.get("patch")
        if isinstance(patch_text, str):
            expected_hashes: dict[str, str] = {}
            payload = build_apply_patch_diff_payload(
                patch_text,
                tool_ctx,
                expected_hashes=expected_hashes,
                read_time_hashes=_read_time_hashes,
            )
            if payload is not None:
                args["_expected_hashes"] = expected_hashes
            return payload
    return None


def inject_expected_hash(
    args: dict[str, Any],
    file_path: str,
    *,
    read_time_hashes: dict[str, str] | None = None,
) -> None:
    if str(args.get("expected_hash") or "").strip():
        return
    path = Path(str(file_path))
    path_key = str(path)
    # Prefer the READ-TIME content hash (recorded by read_file) so the
    # "file changed since read" guard spans the read->write window. Existing
    # files without a recorded read deliberately keep an empty expected_hash:
    # edit_file/write_file will reject them with the same read-before-edit
    # contract as Claude Code instead of blessing a blind edit with a hash read
    # immediately before execution.
    if isinstance(read_time_hashes, dict) and read_time_hashes.get(path_key):
        expected_hash = read_time_hashes.pop(path_key)
        read_time_hashes[path_key] = expected_hash
        args["expected_hash"] = expected_hash
        return
    if not path.exists():
        args["expected_hash"] = ""
        return
    args["expected_hash"] = ""


# Public compatibility name retained for callers that predate the internal
# helper split. Keeping the alias here preserves one implementation path.
flush_queue = _flush_queue
