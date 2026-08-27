from __future__ import annotations

import asyncio
from copy import deepcopy
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

from backend.atomic_io import canonical_file_path_key, canonical_path_mapping_key
from backend.agent.context import ContextBuilder
from backend.agent.lifecycle_observer import resolve_lifecycle_runtime
from backend.agent.final_tool_request import (
    FinalExecutableToolRequest,
    canonical_tool_request_digest,
)
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
from backend.agent.control_tools import CONTROL_TOOL_NAMES, ControlToolRouter
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    cancel_and_drain,
    cancel_and_drain_receipt,
)
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
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, validate_tool_input
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

SPECIAL_TOOL_NAMES = CONTROL_TOOL_NAMES
_DEFAULT_PARALLEL_TOOL_CONCURRENCY = 10


class _WorkspaceTargetPathError(ValueError):
    """Raised when an executable mutation target cannot be proven in scope."""


def _parallel_tool_concurrency(batch_size: int) -> int:
    raw = os.environ.get("MINICODE_MAX_TOOL_CONCURRENCY", "").strip()
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


# Tools whose execution overwrites workspace content through a declared path
# must have a recovery checkpoint before the first side effect, regardless of
# their approval level. notebook_edit is a full-file overwrite primitive just
# like write_file/edit_file; its CONFIRM approval level is an interaction
# choice, not a reason to skip the restore boundary.
_CHECKPOINT_TOOL_NAMES = {"notebook_edit"}


def _tool_requires_checkpoint(tool_name: str, tool_registry: ToolRegistry) -> bool:
    if tool_name in _CHECKPOINT_TOOL_NAMES:
        return True
    return _tool_supports_diff(tool_name, tool_registry)


def _tool_streams_output(tool_name: str, tool_registry: ToolRegistry) -> bool:
    tool = tool_registry.get_tool(tool_name)
    return bool(tool is not None and getattr(tool, "streams_output", False))


# The inline artifact preview follows the shared tool-result contract below.
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


def _bind_final_tool_request(
    tc: ToolCallEvent,
    request: FinalExecutableToolRequest,
) -> None:
    request.apply(tc)
    setattr(tc, "_final_tool_request", request)
    setattr(tc, "_final_request_digest", request.digest)


def _final_tool_request_digest(tc: ToolCallEvent) -> str:
    return str(getattr(tc, "_final_request_digest", "") or "").strip()


def _bound_final_tool_request(tc: ToolCallEvent) -> FinalExecutableToolRequest | None:
    request = getattr(tc, "_final_tool_request", None)
    return request if isinstance(request, FinalExecutableToolRequest) else None


@dataclass(frozen=True, slots=True)
class FinalToolAuthorization:
    """The one authorization result allowed to cross into execution."""

    request: FinalExecutableToolRequest | None = None
    permission_decision: PermissionDecision | None = None
    error: str = ""
    error_kind: str = "validation_error"


def _permission_decision_denial(decision: PermissionDecision) -> str:
    if not decision.capability_allowed:
        return decision.capability_reason or "Capability boundary denied the request."
    if decision.decision == "deny" or decision.permission_level == PermissionLevel.ALWAYS_DENY:
        return decision.matched_rule or "Permission policy denied the request."
    return ""


def _authorize_final_tool_request(
    tc: ToolCallEvent,
    *,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    permission_context: PermissionContext | None,
    tool_ctx: ToolExecutionContext,
    check_scope: bool = True,
) -> FinalToolAuthorization:
    """Validate and authorize the exact arguments that will be executed.

    This helper is deliberately synchronous and side-effect free with respect
    to the permission evaluator: it gives that evaluator a detached copy and
    rejects it if it mutates the copy.  Hooks and user edits call this helper
    again instead of reusing an earlier decision.
    """

    invalid_reason = invalid_tool_call_guard_reason(tc, tool_registry)
    if invalid_reason:
        return FinalToolAuthorization(error=invalid_reason, error_kind="validation_error")

    tool = tool_registry.get_tool(tc.name)
    if tool is not None:
        validation_error = validate_tool_input(tool, tc.arguments)
        if validation_error:
            try:
                rejected_request = FinalExecutableToolRequest.freeze(tc)
            except Exception:
                rejected_request = None
            return FinalToolAuthorization(
                request=rejected_request,
                error=validation_error,
                error_kind="validation_error",
            )

    required_reason = missing_required_tool_argument_reason(None, tc, tool_registry)
    if required_reason:
        try:
            rejected_request = FinalExecutableToolRequest.freeze(tc)
        except Exception:
            rejected_request = None
        return FinalToolAuthorization(
            request=rejected_request,
            error=required_reason,
            error_kind="validation_error",
        )
    policy_reason = toolset_policy_guard_reason(tc, tool_registry, tool_ctx)
    if policy_reason:
        try:
            rejected_request = FinalExecutableToolRequest.freeze(tc)
        except Exception:
            rejected_request = None
        return FinalToolAuthorization(
            request=rejected_request,
            error=policy_reason,
            error_kind="tool_unavailable",
        )
    if check_scope:
        scope_reason = subagent_scope_guard_reason(tc, tool_registry, tool_ctx)
        if scope_reason:
            try:
                rejected_request = FinalExecutableToolRequest.freeze(tc)
            except Exception:
                rejected_request = None
            return FinalToolAuthorization(
                request=rejected_request,
                error=scope_reason,
                error_kind="permission_required",
            )

    try:
        request = FinalExecutableToolRequest.freeze(tc)
    except Exception as exc:
        return FinalToolAuthorization(
            error=f"Tool '{tc.name}' arguments could not be frozen ({type(exc).__name__}).",
            error_kind="security_boundary",
        )

    try:
        decision_args = deepcopy(dict(tc.arguments))
        before_decision_args = deepcopy(decision_args)
    except Exception as exc:
        return FinalToolAuthorization(
            request=request,
            error=f"Tool '{tc.name}' arguments could not be detached safely ({type(exc).__name__}).",
            error_kind="security_boundary",
        )
    try:
        decision = evaluate_permission_decision(
            permission_checker,
            tc.name,
            decision_args,
            context=permission_context,
            tool=tool,
        )
    except Exception as exc:
        logger.exception("permission evaluation failed closed for %s", tc.name)
        return FinalToolAuthorization(
            request=request,
            error=(
                f"Tool '{tc.name}' was blocked because its permission policy "
                f"could not be evaluated ({type(exc).__name__})."
            ),
            error_kind="security_boundary",
        )
    try:
        mutated = decision_args != before_decision_args
    except Exception:
        mutated = True
    if mutated:
        return FinalToolAuthorization(
            request=request,
            error=(
                f"Tool '{tc.name}' was blocked because the permission evaluator "
                "mutated the request while evaluating it."
            ),
            error_kind="security_boundary",
        )

    if decision.request_digest != request.digest:
        decision = replace(decision, request_digest=request.digest)
    return FinalToolAuthorization(request=request, permission_decision=decision)


def _detached_tool_diff(
    tc: ToolCallEvent,
    *,
    workspace_root: Path | str | None,
    tool_ctx: ToolExecutionContext,
) -> dict[str, Any] | None:
    """Build review metadata without mutating the authorized request."""

    try:
        args = deepcopy(dict(tc.arguments or {}))
    except Exception:
        return None
    return generate_diff(
        tc.name,
        args,
        workspace_root=workspace_root,
        tool_ctx=tool_ctx,
    )


def _execution_arguments_for_tool(
    tc: ToolCallEvent,
    *,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> dict[str, Any]:
    """Return a mutable execution-only copy of the canonical arguments.

    File mutation tools receive the runtime-owned optimistic-concurrency guard
    here. It is derived from read-time state rather than replayed from review
    metadata and must not rewrite the canonical approved request.
    """

    execution_args = deepcopy(dict(tc.arguments or {}))
    if tc.name in {"write_file", "edit_file", "notebook_edit"}:
        raw_path = str(
            tc.arguments.get("file_path") or tc.arguments.get("notebook_path") or ""
        ).strip()
        if raw_path:
            try:
                resolved = _resolve_workspace_path_for_scope(raw_path, tool_ctx)
            except Exception as exc:
                raise _WorkspaceTargetPathError(str(exc)) from exc
            inject_expected_hash(
                execution_args,
                str(resolved),
                read_time_hashes=_read_time_hashes(tool_ctx),
            )
    return execution_args


def _read_time_hashes(tool_ctx: ToolExecutionContext) -> dict[str, str] | None:
    metadata = (
        tool_ctx.metadata
        if tool_ctx is not None and isinstance(tool_ctx.metadata, dict)
        else {}
    )
    hashes = metadata.get("_read_file_hashes")
    return hashes if isinstance(hashes, dict) else None


_EXACT_TURN_DIFF_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})

async def _invalidate_turn_diff_after_inexact_mutation(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> None:
    """Any successful non-exact mutation invalidates the turn diff.

    Exact file tools publish their own committed before/after deltas. Commands,
    git/worktree, notebooks, MCP and other workspace/external mutations cannot
    provide that proof, so an existing aggregate must be cleared instead of
    presenting a stale authoritative snapshot.
    """

    if tc.name in _EXACT_TURN_DIFF_TOOLS:
        return
    if _tool_side_effect_kind(tc.name, tool_registry, tc.arguments) not in {
        "workspace",
        "external",
        "destructive",
    }:
        return

    # File tools invalidate their known paths themselves.  Every other tool
    # that declares workspace mutation may touch arbitrary files (notebook,
    # worktree, extension, or a future workspace tool), so keep the shared
    # read/list/fuzzy views conservative at the runtime boundary too.  Do this
    # even for an error result: a command can partially mutate before failing.
    tool = tool_registry.get_tool(tc.name)
    if bool(getattr(tool, "mutates_workspace", False)):
        try:
            from backend.tools.file_tools_common import invalidate_workspace_file_caches

            invalidate_workspace_file_caches(
                file_tree_changed=True,
                clear_file_state=True,
            )
        except Exception:
            logger.debug("workspace cache invalidation failed for %s", tc.name, exc_info=True)

    if result.is_error:
        return
    tracker = getattr(tool_ctx, "turn_diff_tracker", None)
    if tracker is None or not hasattr(tracker, "lock"):
        return
    emit = getattr(tool_ctx, "emit_event", None)
    async with tracker.lock:
        # The lock is the commit-order boundary. A successful mutation whose
        # exact before/after content is unknown always invalidates everything
        # committed before it. Exact file mutations that acquire the lock later
        # see the invalid tracker and cannot recreate a misleading partial diff.
        had_diff = bool(tracker.has_unified_diff())
        tracker.invalidate()
        if not had_diff or emit is None:
            return
        await emit(
            "turn.diff.updated",
            AgentEvent.turn_diff_updated(
                thread_id=str(getattr(tool_ctx, "conversation_id", "") or ""),
                turn_id=_tool_turn_id(tool_ctx),
                diff="",
                revision=tracker.revision,
                tool_call_id=tc.id,
            ).data,
        )


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
    from backend.tools.base import tool_result_exceeds_inline_limit, truncate_tool_result

    content = result.content or ""
    if result.artifact_id or tc.name in SKIP_FORCED_ARTIFACT_TOOL_NAMES or inline_limit is None:
        return result
    if not tool_result_exceeds_inline_limit(content, inline_limit):
        return result

    artifact_store = _artifact_store_from_tool_context(tool_ctx)
    if artifact_store is None or not hasattr(artifact_store, "save"):
        return result

    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    original_bytes = len(content.encode("utf-8", errors="replace"))
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
            f"original_bytes: {original_bytes}",
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


def _parallel_fallback_result(
    tc: ToolCallEvent,
    *,
    status: str,
    content: str,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    reason: str,
    requested: bool,
) -> ToolResult:
    """Build a call-specific fallback without losing cleanup evidence."""
    receipt = tool_ctx.cleanup_receipts.get(tc.id)
    if not isinstance(receipt, dict):
        receipt = {}
    if not receipt:
        receipt = {
            "resource_kind": "tool",
            "resource_id": tc.id,
            "reason": reason,
            "requested": bool(requested),
            "acknowledged": not requested,
            "completed": not requested,
            "timed_out": False,
            "pending": 0,
            "side_effect_kind": _tool_side_effect_kind(tc.name, tool_registry, tc.arguments),
            "request_digest": _final_tool_request_digest(tc)
            or canonical_tool_request_digest(tc.name, tc.arguments or {}),
            "retry_safe": not requested
            and _tool_is_idempotent(tc.name, tool_registry, tc.arguments),
            "manual_recovery_required": bool(requested),
        }
        tool_ctx.cleanup_receipts[tc.id] = receipt
    return ToolResult(
        content=content,
        is_error=True,
        status=status,
        limitation=status,
        request_digest=str(receipt.get("request_digest") or ""),
        cleanup_receipt=receipt,
    )


def _watch_cleanup_receipt_settlement(
    task: asyncio.Task[Any],
    tool_ctx: ToolExecutionContext,
    tool_call_id: str,
    execution_tool_ctx: ToolExecutionContext | None = None,
) -> None:
    """Refresh the live receipt when a post-deadline task finally settles."""
    def settled(_completed: asyncio.Task[Any]) -> None:
        tool_ctx.pending_cleanup_tasks.discard(_completed)
        tool_ctx.cleanup_tasks_by_call.pop(tool_call_id, None)
        if execution_tool_ctx is not None:
            execution_tool_ctx.metadata.pop("_active_tool_call_id", None)
        receipt = tool_ctx.cleanup_receipts.get(tool_call_id)
        if not isinstance(receipt, dict) or not receipt.get("pending"):
            return
        receipt["pending"] = 0
        receipt["completed"] = True
        # Preserve that the cleanup deadline was exceeded; this is historical
        # evidence and must not be confused with a clean in-deadline cancel.
        receipt["cleanup_completed_after_deadline"] = True

    task.add_done_callback(settled)


def _bind_parallel_cleanup_receipt(
    tc: ToolCallEvent,
    *,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    reason: str,
    requested: bool,
    task: asyncio.Task[Any],
    aggregate: Any,
) -> None:
    """Project aggregate batch cleanup into one concrete call's receipt."""
    existing = tool_ctx.cleanup_receipts.get(tc.id)
    if isinstance(existing, dict) and existing:
        # The inner executor owns the actual side-effecting coroutine.  A
        # settled/cancelled batch wrapper is not evidence that the inner tool
        # stopped, so never overwrite its more precise receipt with aggregate
        # wrapper state.
        existing.setdefault("batch_reason", reason)
        return
    cleanup_task = tool_ctx.cleanup_tasks_by_call.get(tc.id)
    receipt = aggregate.to_evidence(
        resource_kind="tool", resource_id=tc.id, reason=reason
    )
    pending = cleanup_task is not None and not cleanup_task.done()
    receipt["pending"] = 1 if pending else 0
    receipt["completed"] = not pending
    receipt["timed_out"] = pending
    receipt["acknowledged"] = not pending
    receipt.update(
        {
            "requested": bool(requested),
            "side_effect_kind": _tool_side_effect_kind(tc.name, tool_registry, tc.arguments),
            "request_digest": _final_tool_request_digest(tc)
            or canonical_tool_request_digest(tc.name, tc.arguments or {}),
            "retry_safe": bool(task.done())
            and _tool_is_idempotent(tc.name, tool_registry, tc.arguments),
            "manual_recovery_required": bool(
                (not task.done())
                or not _tool_is_idempotent(tc.name, tool_registry, tc.arguments)
            ),
        }
    )
    tool_ctx.cleanup_receipts[tc.id] = receipt
    if cleanup_task is not None and receipt.get("pending"):
        _watch_cleanup_receipt_settlement(cleanup_task, tool_ctx, tc.id)


def _tool_call_is_read_only(tc: ToolCallEvent, tool_registry: ToolRegistry) -> bool:
    tool = tool_registry.get_tool(tc.name)
    if tool is not None:
        try:
            return bool(tool.is_read_only(tc.arguments))
        except Exception:
            return bool(getattr(tool, "read_only", False))
    return not _tool_mutates(tc.name, tool_registry, tc.arguments)


def _resolve_workspace_path_for_scope(raw_path: str, tool_ctx: ToolExecutionContext) -> Path:
    from backend.tools.path_resolution import _is_bypass_mode, _resolve_path

    # Guard extraction and mutation tools must agree on bypass semantics:
    # explicit bypass may leave the workspace, while current plan files remain
    # writable without pretending they are inside an arbitrary root.
    return _resolve_path(
        raw_path,
        tool_ctx,
        allow_workspace_escape=_is_bypass_mode(tool_ctx),
        allow_current_plan_file=True,
    ).resolve()


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
    del state, tool_ctx
    prepared: list[ToolCallEvent] = []
    for index, raw_tc in enumerate(tool_calls, 1):
        normalized = normalize_tool_call_event(raw_tc, fallback_id=f"tool_{index}")
        tool = tool_registry.get_tool(normalized.name)
        prepare = (
            getattr(tool, "prepare_arguments", None)
            if tool is not None
            else None
        )
        if callable(prepare) and isinstance(normalized.arguments, dict):
            try:
                prepared_args = prepare(dict(normalized.arguments))
                if inspect.isawaitable(prepared_args):
                    raise TypeError("tool argument preparation must be synchronous")
                if not isinstance(prepared_args, dict):
                    raise TypeError("tool argument preparation must return an object")
                normalized = replace(normalized, arguments=prepared_args)
            except Exception as exc:
                setattr(normalized, "_prepare_arguments_error", str(exc))
        prepared.append(normalized)
    # Provider call ids must be non-empty and unique before history commit and
    # execution. Argument preparation runs before the registry
    # validates the resulting object; ordinary tools remain model-authored.
    deduped = _dedupe_tool_call_ids(prepared)
    for original, normalized in zip(prepared, deduped):
        prepare_error = getattr(original, "_prepare_arguments_error", "")
        if prepare_error and not getattr(normalized, "_prepare_arguments_error", ""):
            setattr(normalized, "_prepare_arguments_error", prepare_error)
    return deduped


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
    """Surface the exception message to the model."""
    message = str(exc).strip()
    detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return ToolResult(
        content=f"{label} failed ({detail}).",
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
            status="cancelled",
            limitation="cancelled",
            display_summary=f"Cancelled: {prefetched.tool_call.name}",
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


class StreamingToolExecutor:
    """Collect tool-call diagnostics without executing provider stream data.

    Tools execute only after the assistant response is settled. This
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
    prepare_error = str(getattr(tc, "_prepare_arguments_error", "") or "").strip()
    if prepare_error:
        return (
            f"Tool '{name}' arguments could not be prepared: {prepare_error}. "
            "Retry with arguments accepted by the tool."
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


def toolset_policy_guard_reason(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext | None,
) -> str:
    """Enforce the same capability surface at execution as at schema time.

    A model can fabricate a tool call even when the tool was not in the
    current schema.  The registry/policy boundary must reject that call before
    hooks, approvals, prefetches, or tool-owned side effects run.  Deferred
    tools are intentionally checked with ``is_directly_visible``: discovery
    must activate them for the next iteration first.
    """

    metadata = getattr(tool_ctx, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
    try:
        from backend.tools.toolsets import (
            ACTIVE_TOOLSET_POLICY_METADATA_KEY,
            ToolsetPolicy,
        )
        from backend.tools.tool_search import _toolset_policy_for_context

        policy = metadata.get(ACTIVE_TOOLSET_POLICY_METADATA_KEY)
        if policy is None:
            policy = _toolset_policy_for_context(
                getattr(tool_ctx, "permission", None),
                metadata,
            )
        if not isinstance(policy, ToolsetPolicy):
            return (
                f"Tool '{tc.name}' was blocked because the active execution "
                "capability policy is invalid."
            )
        spec = tool_registry.get_tool_spec(tc.name)
        if policy.is_directly_visible(spec):
            return ""
        if policy.is_available(spec):
            return (
                f"Tool '{tc.name}' is deferred and has not been activated for this turn. "
                "Use tool_search to activate it, then retry on the next iteration."
            )
        return f"Tool '{tc.name}' is unavailable under the active execution capability policy."
    except Exception as exc:
        # A policy/spec parsing failure is a security boundary failure.  Do not
        # fall through to execution when the runtime cannot prove visibility.
        logger.warning("toolset policy evaluation failed closed for %s: %s", tc.name, exc)
        return f"Tool '{tc.name}' was blocked because its execution capability policy could not be verified."


async def snapshot_before_write(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    tool_registry: ToolRegistry,
) -> str | None:
    if not _tool_requires_checkpoint(tc.name, tool_registry):
        return None
    manager = getattr(tool_ctx, "checkpoint_manager", None)
    if manager is None:
        # Lower-level harness callers may provide a ToolExecutionContext
        # directly (for example SDK/eval composition) instead of passing
        # through loop_bootstrap. Establish the same turn-owned checkpoint
        # manager at this boundary so the write invariant remains true without
        # introducing another execution path.
        from backend.checkpoint.manager import CheckpointManager

        manager = CheckpointManager()
        tool_ctx.checkpoint_manager = manager
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
        return (
            f"Tool '{tc.name}' was not executed because its recovery checkpoint "
            f"could not be created ({type(exc).__name__})."
        )
    if record is None:
        return (
            f"Tool '{tc.name}' was not executed because MiniCode could not create "
            "a recovery checkpoint for its target path."
        )
    emit = getattr(tool_ctx, "emit_event", None)
    if emit:
        try:
            await emit("checkpoint.created", record.to_public_dict())
        except Exception as exc:
            logger.debug("checkpoint emit failed: %s", exc)
    return None


async def run_tool(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> ToolResult:
    from backend.hooks import get_hook_manager

    if isinstance(tool_ctx.metadata, dict):
        tool_ctx.metadata.setdefault("_tool_registry", tool_registry)

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

    policy_reason = toolset_policy_guard_reason(tc, tool_registry, tool_ctx)
    if policy_reason:
        return _rejection_result(
            tc,
            policy_reason,
            display_summary="Tool unavailable",
            result_kind=result_kind_for_tool(tc.name, tool_registry),
            error_kind="tool_unavailable",
            user_summary="该工具当前不在此 Agent 的可执行能力范围内。",
            projection="status",
            model_observation=(
                f"The {tc.name} call was not executed because it is outside the "
                "active tool capability surface."
            ),
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

    snapshot_error = await snapshot_before_write(tc, tool_ctx, tool_registry)
    if snapshot_error:
        return ToolResult(
            content=snapshot_error,
            is_error=True,
            status="blocked",
            error_kind="checkpoint",
            user_summary="无法建立写入恢复点，操作未执行。",
            projection="error",
            model_observation=(
                "The write tool was not executed because MiniCode could not create "
                "its recovery checkpoint."
            ),
        )
    changed_file = changed_file_event_payload(tc, tool_ctx, tool_registry)
    # Principle: a side effect without a recovery boundary must be visible
    # evidence, never a silent skip.  Tools like run_command and the worktree
    # family mutate the workspace through paths no file checkpoint can
    # capture; their results must say so.
    checkpoint_limitation = ""
    if not snapshot_error:
        mutating_tool = tool_registry.get_tool(tc.name)
        if (
            mutating_tool is not None
            and bool(getattr(mutating_tool, "mutates_workspace", False))
            and not _tool_requires_checkpoint(tc.name, tool_registry)
            and getattr(tool_ctx, "checkpoint_manager", None) is not None
        ):
            checkpoint_limitation = (
                f"No content-level restore point was created for '{tc.name}': "
                "its affected workspace paths are not capturable by a file checkpoint."
            )
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
        # The frozen request on ``tc`` is the audit/provenance object.  Tool
        # adapters are allowed to coerce input and inject private execution
        # state (expected hashes, cursor handles, etc.), but those mutations
        # must never rewrite the request that was authorized or journaled.
        try:
            execution_args = _execution_arguments_for_tool(
                tc,
                tool_registry=tool_registry,
                tool_ctx=tool_ctx,
            )
        except Exception as exc:
            return ToolResult(
                content=(
                    f"Tool '{tc.name}' was blocked because its arguments could "
                    f"not be detached safely ({type(exc).__name__})."
                ),
                is_error=True,
                status="blocked",
                error_kind="security_boundary",
            )
        if tm:
            try:
                managed = tm.create(
                    kind="tool_run",
                    awaitable=tool_registry.execute(tc.name, execution_args, context=tool_ctx),
                )
                result = await tm.wait(managed.id)
            except Exception as exc:
                result = _execution_exception_result(exc, label="Managed tool execution")
        else:
            result = await tool_registry.execute(tc.name, execution_args, context=tool_ctx)
    finally:
        if owner_token is not None:
            artifact_store.reset_owner(owner_token)
        if metadata_had_tool_id:
            tool_ctx.metadata["_current_tool_call_id"] = previous_tool_id
        else:
            tool_ctx.metadata.pop("_current_tool_call_id", None)

    if checkpoint_limitation and not result.limitation:
        result = replace(result, limitation=checkpoint_limitation)

    if not result.request_digest:
        result = replace(result, request_digest=_final_tool_request_digest(tc) or canonical_tool_request_digest(tc.name, tc.arguments or {}))

    result = await _apply_extension_post_tool_hook(tc, result, tool_ctx)

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
            # Post-tool hooks carry only attachments/progress
            # and updatedMCPToolOutput — there is no block concept after a
            # tool already ran, so a "blocked" flag must not overwrite the
            # successful result.
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
                await emit("file.changed", dict(changed_file))
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
    if hook_mgr is not None and tc.name != "exit_plan_mode":
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
            # Pre-tool hooks run inside the call try: a crashing hook
            # yields an error tool_result and the tool does NOT run. The
            # extension path below already fails closed; match it here.
            logger.exception("pre_tool hook failed")
            return ToolResult(
                content=(
                    f"Tool '{tc.name}' was blocked because a PreToolUse hook "
                    f"failed before execution: {exc}"
                ),
                is_error=True,
                status="blocked",
                display_summary="PreToolUse hook failed",
            )
    lifecycle_runtime = _tool_lifecycle_runtime(tool_ctx)
    if (
        lifecycle_runtime is not None
        and not _active_tool_is_owned_by_lifecycle_runtime(
            tc.name,
            tool_ctx,
            lifecycle_runtime,
        )
    ):
        try:
            before_tool_call = getattr(lifecycle_runtime, "before_tool_call", None)
            if callable(before_tool_call):
                decision = await before_tool_call(
                    tc.id,
                    tc.name,
                    tc.arguments,
                    signal=tool_ctx.cancel_event,
                    tool_context=tool_ctx,
                )
                if bool(getattr(decision, "block", False)):
                    reason = str(getattr(decision, "reason", "") or "").strip()
                    return ToolResult(
                        content=(
                            reason
                            or f"Tool '{tc.name}' was blocked by an executable extension."
                        ),
                        is_error=True,
                        status="blocked",
                        display_summary="Blocked by extension",
                    )
        except Exception as exc:
            # Pre-execution extension callbacks are a security boundary. A
            # failing callback must not accidentally allow the tool through.
            logger.exception("extension tool_call hook failed")
            return ToolResult(
                content=(
                    f"Tool '{tc.name}' was blocked because an executable extension "
                    f"failed before execution: {exc}"
                ),
                is_error=True,
                status="blocked",
                display_summary="Extension hook failed",
            )
    setattr(tc, "_pre_tool_hook_applied", True)
    return None


def _tool_lifecycle_runtime(
    tool_ctx: ToolExecutionContext,
) -> Any | None:
    """Resolve the canonical runtime installed by the session composition root."""

    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    runtime = resolve_lifecycle_runtime(metadata)
    if runtime is None:
        return None
    if not callable(getattr(runtime, "before_tool_call", None)) and not callable(
        getattr(runtime, "after_tool_call", None)
    ):
        return None
    return runtime


def _active_tool_is_owned_by_lifecycle_runtime(
    tool_name: str,
    tool_ctx: ToolExecutionContext,
    lifecycle_runtime: Any,
) -> bool:
    """Avoid double interception for ExtensionToolAdapter executions.

    Extension tools invoke their runtime inside the adapter so they receive the
    extension context and update callback. Built-in/host tools are intercepted here.
    Checking the active registry object (not merely the registered name) keeps
    hooks enabled when a host tool won an explicit collision policy.
    """

    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    registry = metadata.get("_tool_registry")
    get_tool = getattr(registry, "get_tool", None)
    if not callable(get_tool):
        return False
    try:
        tool = get_tool(str(tool_name or ""))
    except Exception:
        return False
    return getattr(tool, "_runner", None) is lifecycle_runtime


async def _apply_extension_post_tool_hook(
    tc: ToolCallEvent,
    result: ToolResult,
    tool_ctx: ToolExecutionContext,
) -> ToolResult:
    lifecycle_runtime = _tool_lifecycle_runtime(tool_ctx)
    if lifecycle_runtime is None or _active_tool_is_owned_by_lifecycle_runtime(
        tc.name,
        tool_ctx,
        lifecycle_runtime,
    ):
        return result
    after_tool_call = getattr(lifecycle_runtime, "after_tool_call", None)
    if not callable(after_tool_call):
        return result
    try:
        patch = await after_tool_call(
            tc.id,
            tc.name,
            tc.arguments,
            result,
            is_error=result.is_error,
            signal=tool_ctx.cancel_event,
            tool_context=tool_ctx,
        )
    except Exception:
        # Post-execution callbacks are observational: record the failure without
        # discarding an already completed tool.
        logger.exception("extension tool_result hook failed")
        return result
    if patch is None:
        return result
    content = result.content
    if bool(getattr(patch, "has_content", False)):
        from backend.agent.content_projection import normalise_content

        content = normalise_content(getattr(patch, "content", content))
    is_error = result.is_error
    patched_error = getattr(patch, "is_error", None)
    if patched_error is not None:
        is_error = bool(patched_error)
    return replace(result, content=content, is_error=is_error)


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
    payload = {
        "path": display_path,
        "event": event_type,
    }
    if tool_ctx.workspace_root is not None:
        payload["workspace_root"] = str(tool_ctx.workspace_root)
    return payload


async def run_tool_with_timeout(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    *,
    iteration_id: str = "",
) -> ToolResult:
    cancel_event = getattr(tool_ctx, "cancel_event", None)
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError

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
    timeout_cleanup_receipt = None
    execution_tool_ctx.metadata["_active_tool_call_id"] = tc.id
    execution_task = asyncio.create_task(run_tool(tc, tool_registry, execution_tool_ctx))
    tool_ctx.cleanup_tasks_by_call[tc.id] = execution_task
    try:
        if timeout is None:
            # Outer turn/batch cancellation must not implicitly transfer
            # ownership to asyncio's await propagation.  Shield the concrete
            # tool task, then handle cancellation below with MiniCode's bounded
            # drain, explicit owner and durable-shaped receipt.
            result = await asyncio.shield(execution_task)
        else:
            done, _ = await asyncio.wait(
                {execution_task},
                timeout=max(0.0, timeout),
            )
            if execution_task in done:
                result = execution_task.result()
            else:
                timeout_cleanup_receipt = await cancel_and_drain_receipt(
                    [execution_task],
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label=f"timed out tool {tc.name}",
                    owner=tool_ctx.pending_cleanup_tasks,
                )
                raise asyncio.TimeoutError
    except asyncio.CancelledError:
        cancellation_receipt = await cancel_and_drain_receipt(
            [execution_task],
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"cancelled tool {tc.name}",
            owner=tool_ctx.pending_cleanup_tasks,
        )
        cleanup_evidence = cancellation_receipt.to_evidence(
            resource_kind="tool", resource_id=tc.id, reason="cancelled"
        )
        nested_receipt = execution_tool_ctx.metadata.get("_registry_cleanup_receipt")
        if isinstance(nested_receipt, dict) and nested_receipt.get("pending"):
            cleanup_evidence.update(
                {
                    "completed": False,
                    "timed_out": True,
                    "pending": int(nested_receipt.get("pending") or 1),
                    "nested_cleanup": dict(nested_receipt),
                }
            )
        cleanup_evidence.update(
            {
                "side_effect_kind": _tool_side_effect_kind(tc.name, tool_registry, tc.arguments),
                "request_digest": _final_tool_request_digest(tc)
                or canonical_tool_request_digest(tc.name, tc.arguments or {}),
                "retry_safe": False,
                "manual_recovery_required": True,
            }
        )
        tool_ctx.cleanup_receipts[tc.id] = cleanup_evidence
        if cleanup_evidence.get("pending"):
            _watch_cleanup_receipt_settlement(
                execution_task,
                tool_ctx,
                tc.id,
                execution_tool_ctx,
            )
        else:
            tool_ctx.cleanup_tasks_by_call.pop(tc.id, None)
            execution_tool_ctx.metadata.pop("_active_tool_call_id", None)
        raise
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - t0) * 1000)
        cleanup_evidence = (
            timeout_cleanup_receipt.to_evidence(
                resource_kind="tool",
                resource_id=tc.id,
                reason="timeout",
            )
            if timeout_cleanup_receipt is not None
            else {
                # Defensive branch: no drain receipt exists, so cleanup cannot
                # be proven complete. Publish unproven evidence (never a fake
                # completed=True) and keep the abandoned task under observation.
                "resource_kind": "tool",
                "resource_id": tc.id,
                "reason": "tool_reported_timeout",
                "requested": False,
                "acknowledged": False,
                "completed": False,
                "timed_out": True,
                "pending": 1,
            }
        )
        nested_receipt = execution_tool_ctx.metadata.get("_registry_cleanup_receipt")
        if isinstance(nested_receipt, dict) and nested_receipt.get("pending"):
            cleanup_evidence.update(
                {
                    "completed": False,
                    "timed_out": True,
                    "pending": int(nested_receipt.get("pending") or 1),
                    "nested_cleanup": dict(nested_receipt),
                }
            )
        cleanup_evidence["side_effect_kind"] = _tool_side_effect_kind(
            tc.name,
            tool_registry,
            tc.arguments,
        )
        cleanup_evidence["request_digest"] = (
            _final_tool_request_digest(tc)
            or canonical_tool_request_digest(tc.name, tc.arguments or {})
        )
        cleanup_evidence["retry_safe"] = bool(
            cleanup_evidence.get("completed")
            and _tool_is_idempotent(tc.name, tool_registry, tc.arguments)
        )
        cleanup_evidence["manual_recovery_required"] = bool(
            cleanup_evidence.get("pending")
            or not cleanup_evidence.get("retry_safe")
        )
        tool_ctx.cleanup_receipts[tc.id] = cleanup_evidence
        if cleanup_evidence.get("pending"):
            tool_ctx.pending_cleanup_tasks.add(execution_task)
            _watch_cleanup_receipt_settlement(
                execution_task,
                tool_ctx,
                tc.id,
                execution_tool_ctx,
            )
        else:
            tool_ctx.cleanup_tasks_by_call.pop(tc.id, None)
            execution_tool_ctx.metadata.pop("_active_tool_call_id", None)
        return ToolResult(
            content=(
                f"Tool '{tc.name}' timed out after {timeout:.0f}s. "
                "The operation did not finish and no complete result is available. "
                "Do not retry the identical call until cleanup is confirmed; break the operation into smaller steps or try a different approach."
            ),
            is_error=True,
            duration_ms=elapsed,
            status="timeout",
            limitation="timeout",
            display_summary=f"Timed out: {tc.name}",
            result_kind=result_kind_for_tool(tc.name, tool_registry),
            cleanup_receipt=cleanup_evidence,
        )
    elapsed = int((time.perf_counter() - t0) * 1000)
    tool_ctx.cleanup_tasks_by_call.pop(tc.id, None)
    execution_tool_ctx.metadata.pop("_active_tool_call_id", None)
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

    Only *consecutive* concurrency-safe tools are batched together. A mutating
    tool that sits between two reads is
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
        try:
            # A third-party/MCP classifier is metadata, not turn control.
            # Claude Code falls back to serial execution when it throws; do
            # the same so a bad extension cannot abort the entire model turn.
            is_safe = bool(tool and tool.is_concurrency_safe(tc.arguments))
        except Exception:
            logger.warning(
                "Tool concurrency classification failed; executing serially: %s",
                tc.name,
                exc_info=True,
            )
            is_safe = False
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
    request_digest = _final_tool_request_digest(tc) or canonical_tool_request_digest(
        tc.name,
        tc.arguments or {},
    )
    if result.request_digest != request_digest:
        result = replace(result, request_digest=request_digest)
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
                    file_changed_payload = {
                        "path": str(item.get("display_path") or item.get("resolved_path") or ""),
                        "event": "deleted",
                        "temporary": True,
                        "supersedes_tool_call_id": str(item.get("tool_call_id") or ""),
                    }
                    if tool_ctx.workspace_root is not None:
                        file_changed_payload["workspace_root"] = str(tool_ctx.workspace_root)
                    await emit("file.changed", file_changed_payload)
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
        key = os.path.normcase(str(resolved))
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
            path_key = canonical_path_mapping_key(hashes, path)
            hashes.pop(path_key, None)
            hashes[path_key] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        except (OSError, UnicodeDecodeError):
            # A deleted/renamed path must not retain the old hash.
            hashes.pop(canonical_path_mapping_key(hashes, path), None)


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
    if isinstance(tool_ctx.metadata, dict):
        tool_ctx.metadata.setdefault("_tool_registry", tool_registry)
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

        policy_reason = toolset_policy_guard_reason(tc, tool_registry, tool_ctx)
        if policy_reason:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _rejection_result(
                    tc,
                    policy_reason,
                    display_summary="Tool unavailable",
                    result_kind=result_kind_for_tool(tc.name, tool_registry),
                    error_kind="tool_unavailable",
                    user_summary="该工具当前不在此 Agent 的可执行能力范围内。",
                    projection="status",
                    model_observation=(
                        f"The {tc.name} call was not executed because it is outside the "
                        "active tool capability surface."
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
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
                request_digest=canonical_tool_request_digest(tc.name, tc.arguments or {}),
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

        authorization = _authorize_final_tool_request(
            tc,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            permission_context=permission_context,
            tool_ctx=tool_ctx,
            check_scope=True,
        )
        if authorization.error or authorization.request is None or authorization.permission_decision is None:
            if authorization.request is not None:
                _bind_final_tool_request(tc, authorization.request)
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _rejection_result(
                    tc,
                    authorization.error or "Tool request authorization failed.",
                    display_summary="Tool request rejected",
                    result_kind=result_kind_for_tool(tc.name, tool_registry),
                    error_kind=authorization.error_kind,
                    user_summary="工具请求未通过最终授权。",
                    projection="approval" if authorization.error_kind == "permission_required" else "error",
                    model_observation=(
                        f"The {tc.name} call was not executed because final request authorization failed."
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue
        final_request = authorization.request
        permission_decision = authorization.permission_decision
        _bind_final_tool_request(tc, final_request)
        perm = permission_decision.permission_level
        pre_hook_decision = str(
            getattr(tc, "_pre_tool_hook_permission_decision", "") or ""
        ).strip().lower()
        if pre_hook_decision == "ask" and perm == PermissionLevel.AUTO:
            # A pre-tool `ask` is an approval floor. Static
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
                request_digest=final_request.digest,
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
            # Auto calls have no later input-mutation boundary. Their frozen
            # request is already final, so claim it before queueing.
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
            permission_checker=permission_checker,
            permission_context=permission_context,
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
                tc.id: _detached_tool_diff(
                    tc,
                    workspace_root=tool_ctx.workspace_root,
                    tool_ctx=tool_ctx,
                )
                for tc in batch
                if _tool_supports_diff(tc.name, tool_registry)
            }
            async def _run_parallel_tool(tc: ToolCallEvent) -> ToolResult:
                prefetched = _take_matching_prefetch(prefetched_results, tc)
                if prefetched is not None:
                    result = await _await_prefetched_result(prefetched)
                else:
                    try:
                        result = await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)
                    except Exception as exc:
                        result = _execution_exception_result(exc)
                await _invalidate_turn_diff_after_inexact_mutation(
                    tc,
                    result,
                    tool_registry=tool_registry,
                    tool_ctx=tool_ctx,
                )
                return result

            # Parallel tool execution is bounded to ten concurrent calls by
            # default, with one environment override. The legacy MiniCode
            # override remains accepted for existing deployments.
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

            def _harvest_batch_result(
                task: asyncio.Task[ToolResult],
                tc: ToolCallEvent,
                fallback: ToolResult,
            ) -> None:
                """Keep a result the task already produced instead of the fallback.

                Cancellation and batch timeouts race the tool that is already
                returning: the cross-transport ``yield`` above hands control to
                the event loop, so siblings routinely finish between that point
                and ``task.cancel()``.  Overwriting a settled task would report
                Cancelled for work the model can legitimately use, discarding
                evidence and forcing a retry.
                """
                if not task.done():
                    _record_batch_result(tc, fallback)
                    return
                try:
                    _record_batch_result(tc, task.result())
                except asyncio.CancelledError:
                    _record_batch_result(tc, fallback)
                except Exception as exc:
                    _record_batch_result(tc, _execution_exception_result(exc))

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
                            result = _parallel_fallback_result(
                                tc,
                                status="cancelled",
                                content="Cancelled: parallel tool call errored.",
                                tool_registry=tool_registry,
                                tool_ctx=tool_ctx,
                                reason="parallel_sibling_cancelled",
                                requested=True,
                            )
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
                        sibling_cleanup = await cancel_and_drain_receipt(
                            (task for task, _ in remaining),
                            timeout=0.5,
                            label="tool batch siblings",
                            owner=tool_ctx.pending_cleanup_tasks,
                        )
                        for task, tc in remaining:
                            _bind_parallel_cleanup_receipt(
                                tc,
                                tool_registry=tool_registry,
                                tool_ctx=tool_ctx,
                                reason="parallel_sibling_cancelled",
                                requested=True,
                                task=task,
                                aggregate=sibling_cleanup,
                            )
                            _harvest_batch_result(
                                task,
                                tc,
                                _parallel_fallback_result(
                                    tc,
                                    status="cancelled",
                                    content="Cancelled: parallel tool call errored.",
                                    tool_registry=tool_registry,
                                    tool_ctx=tool_ctx,
                                    reason="parallel_sibling_cancelled",
                                    requested=True,
                                ),
                            )
                            pending.pop(task, None)
                        for tc in batch[next_executable_index:]:
                            _record_batch_result(
                                tc,
                                _parallel_fallback_result(
                                    tc,
                                    status="cancelled",
                                    content="Not executed because a parallel sibling failed.",
                                    tool_registry=tool_registry,
                                    tool_ctx=tool_ctx,
                                    reason="parallel_sibling_cancelled",
                                    requested=False,
                                ),
                            )
                        next_executable_index = len(batch)
                        async for event in _emit_ordered_ready_results():
                            yield event
                        break

                    await _start_ready_tasks()

                if batch_timed_out:
                    unfinished = list(pending.items())
                    for task, _tc in unfinished:
                        task.cancel()
                    batch_cleanup = await cancel_and_drain_receipt(
                        (task for task, _ in unfinished),
                        timeout=0.5,
                        label="timed out tool batch",
                        owner=tool_ctx.pending_cleanup_tasks,
                    )
                    for task, tc in unfinished:
                        _bind_parallel_cleanup_receipt(
                            tc,
                            tool_registry=tool_registry,
                            tool_ctx=tool_ctx,
                            reason="parallel_batch_timeout",
                            requested=True,
                            task=task,
                            aggregate=batch_cleanup,
                        )
                        _harvest_batch_result(
                            task,
                            tc,
                            _parallel_fallback_result(
                                tc,
                                status="timeout",
                                content="Parallel tool batch timed out before this call completed.",
                                tool_registry=tool_registry,
                                tool_ctx=tool_ctx,
                                reason="parallel_batch_timeout",
                                requested=True,
                            ),
                        )
                        pending.pop(task, None)
                    for tc in batch[next_executable_index:]:
                        _record_batch_result(
                            tc,
                            _parallel_fallback_result(
                                tc,
                                status="timeout",
                                content="Not executed because the parallel tool batch timed out.",
                                tool_registry=tool_registry,
                                tool_ctx=tool_ctx,
                                reason="parallel_batch_timeout",
                                requested=False,
                            ),
                        )
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
                    batch_cleanup = await cancel_and_drain_receipt(
                        pending,
                        timeout=0.5,
                        label="tool batch cleanup",
                        owner=tool_ctx.pending_cleanup_tasks,
                    )
                    for task, tc in pending.items():
                        _bind_parallel_cleanup_receipt(
                            tc,
                            tool_registry=tool_registry,
                            tool_ctx=tool_ctx,
                            reason="parallel_batch_cleanup",
                            requested=True,
                            task=task,
                            aggregate=batch_cleanup,
                        )

            while next_emit_index < len(batch):
                tc = batch[next_emit_index]
                results_by_id.setdefault(
                    tc.id,
                    _parallel_fallback_result(
                        tc,
                        status="cancelled",
                        content="Parallel tool call was cancelled before completion.",
                        tool_registry=tool_registry,
                        tool_ctx=tool_ctx,
                        reason="parallel_batch_cleanup",
                        requested=True,
                    ),
                )
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
                    _detached_tool_diff(tc, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx)
                    if _tool_supports_diff(tc.name, tool_registry)
                    else None
                )
                prefetched = _take_matching_prefetch(prefetched_results, tc)
                if prefetched is not None:
                    result = await _await_prefetched_result(prefetched)
                else:
                    result = await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)
                await _invalidate_turn_diff_after_inexact_mutation(
                    tc,
                    result,
                    tool_registry=tool_registry,
                    tool_ctx=tool_ctx,
                )
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
    *,
    expected_digest: str = "",
) -> dict[str, Any]:
    """Wait for an approval decision without outliving the turn's deadline.

    An approval wait is unbounded work performed on behalf of the turn, so it
    must respect the same wall-clock boundary as tool execution. Without this
    a turn whose user stepped away stays pinned on the handler's own timeout,
    long past ``max_turn_seconds``.
    """
    cancel_event = getattr(tool_ctx, "cancel_event", None)
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError

    deadline = getattr(tool_ctx, "deadline_monotonic", None)
    remaining = (
        None
        if deadline is None
        else float(deadline) - time.monotonic()
    )
    if remaining is not None and remaining <= 0:
        return {
            "action": "reject",
            "guidance": (
                "The turn's time budget was exhausted before this approval was answered. "
                "The action was not performed."
            ),
        }
    approval_task = asyncio.create_task(approval_handler(tc.id))
    cancel_wait_task = (
        asyncio.create_task(cancel_event.wait())
        if cancel_event is not None
        else None
    )
    try:
        wait_tasks = {approval_task}
        if cancel_wait_task is not None:
            wait_tasks.add(cancel_wait_task)
        done, _ = await asyncio.wait(
            wait_tasks,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if approval_task in done:
            raw_result = approval_task.result()
            if not isinstance(raw_result, dict):
                return {
                    "action": "reject",
                    "guidance": "The approval channel returned an invalid response.",
                }
            result = dict(raw_result)
            supplied_digest = str(result.get("request_digest") or "").strip()
            expected = str(expected_digest or "").strip()
            if expected and supplied_digest and supplied_digest != expected:
                return {
                    "action": "reject",
                    "guidance": (
                        "The approval response did not match the exact tool request "
                        "that was shown to the user. The action was not performed."
                    ),
                    "approval_digest_mismatch": True,
                    "request_digest": expected,
                }
            if expected:
                # A trusted server-side approval handler may omit the digest;
                # bind that response to the pending receipt here.  A client
                # supplied digest, when present, was checked above.
                result["request_digest"] = expected
            return result
        await cancel_and_drain(
            [approval_task],
            timeout=0.1,
            label=f"approval wait for {tc.name}",
        )
        if cancel_wait_task is not None and cancel_wait_task in done:
            raise asyncio.CancelledError
        return {
            "action": "reject",
            "guidance": (
                "The turn's time budget was exhausted while waiting for approval. "
                "The action was not performed."
            ),
        }
    except asyncio.CancelledError:
        await cancel_and_drain(
            [approval_task],
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"cancelled approval wait for {tc.name}",
        )
        raise
    finally:
        if cancel_wait_task is not None and not cancel_wait_task.done():
            cancel_wait_task.cancel()
            await cancel_and_drain(
                [cancel_wait_task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"approval cancellation waiter {tc.name}",
            )


async def execute_serial(
    tc: ToolCallEvent,
    *,
    perm: PermissionLevel,
    permission_decision: PermissionDecision,
    permission_checker: PermissionChecker,
    permission_context: PermissionContext | None,
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
    started_epoch = _tool_start_times(state).get(tc.id, time.time())
    tool_call_emitted = False

    def final_tool_call_event() -> AgentEvent:
        return tool_call_start_event(
            tc,
            started_epoch=started_epoch,
            iteration_id=iteration_id,
            tool_registry=tool_registry,
            turn_id=turn_id,
        )

    execution_args = deepcopy(dict(tc.arguments or {}))
    approval_args = dict(execution_args)
    if tc.name == "exit_plan_mode":
        from backend.agent.plans import current_plan_paths, read_plan

        paths = current_plan_paths(tool_ctx.permission)
        if len(paths) == 1:
            plan = read_plan(paths[0])
            approval_args = {
                **execution_args,
                **({"plan": plan} if plan is not None else {}),
                "plan_file_path": str(paths[0]),
            }
        # The plan content/path shown to the user is part of the proposal being
        # authorized, even though it is host-owned rather than model-authored.
        # Bind permission and approval to that exact payload.
        tc.arguments = dict(approval_args)
        proposal_authorization = _authorize_final_tool_request(
            tc,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            permission_context=permission_context,
            tool_ctx=tool_ctx,
            check_scope=True,
        )
        if proposal_authorization.request is not None:
            _bind_final_tool_request(tc, proposal_authorization.request)
        proposal_denial = (
            _permission_decision_denial(proposal_authorization.permission_decision)
            if proposal_authorization.permission_decision is not None
            else ""
        )
        if (
            proposal_authorization.error
            or proposal_authorization.request is None
            or proposal_authorization.permission_decision is None
            or proposal_denial
        ):
            yield final_tool_call_event()
            tool_call_emitted = True
            result = _rejection_result(
                tc,
                proposal_authorization.error
                or proposal_denial
                or "The plan approval request could not be authorized.",
                display_summary="Plan approval rejected",
                result_kind=result_kind_for_tool(tc.name, tool_registry),
                error_kind=(
                    proposal_authorization.error_kind
                    if proposal_authorization.error
                    else "permission_required"
                ),
                user_summary="计划审批请求未通过最终授权。",
                projection="approval",
                model_observation="The plan approval request was not authorized and was not executed.",
            )
            async for event in _finalize_tool_result(
                tc, result, ctx=ctx, state=state, diff=None,
                iteration_id=iteration_id, turn_id=turn_id,
                tool_ctx=tool_ctx, tool_registry=tool_registry,
            ):
                yield event
            return
        permission_decision = proposal_authorization.permission_decision
        perm = permission_decision.permission_level
        execution_args = deepcopy(dict(tc.arguments))
        approval_args = dict(execution_args)
    declared_permission = getattr(tool_registry.get_tool(tc.name), "permission", None)
    needs_diff_review = perm == PermissionLevel.DIFF_REVIEW or declared_permission == PermissionLevel.DIFF_REVIEW

    if perm in (PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW):
        diff = _detached_tool_diff(tc, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx) if needs_diff_review else None
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        hook_requested_allow = (
            str(getattr(tc, "_pre_tool_hook_permission_decision", "") or "").strip().lower()
            == "allow"
        )
        # A pre-tool allow can skip the tool's ordinary interactive
        # prompt, but settings ask rules and capability floors still win.
        hook_overridable_sources = {"tool", "mode", "default", "external_checker"}
        permission_allowed_by_hook = (
            hook_requested_allow
            and permission_decision.matched_rule_source in hook_overridable_sources
            and tc.name != "exit_plan_mode"
        )
        if hook_mgr and not permission_allowed_by_hook and tc.name != "exit_plan_mode":
            try:
                permission_hook = await hook_mgr.run_permission_request(
                    tc.name,
                    approval_args,
                    reason="tool requires user approval",
                    permission_level=perm.value,
                    tool_call_id=tc.id,
                    session_id=tool_ctx.session_id,
                    permission_mode=tool_ctx.permission.mode,
                )
                _remember_hook_model_context(tc, permission_hook)
                if permission_hook.blocked or permission_hook.permission_decision == "deny":
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
                        request_digest=_final_tool_request_digest(tc),
                    )
                    yield final_tool_call_event()
                    tool_call_emitted = True
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
                    # PermissionRequest.updatedInput is a new executable
                    # request, not a cosmetic edit. Re-run every guard and
                    # replace the old decision/digest before considering the
                    # hook's allow/ask response.
                    tc.arguments = dict(permission_hook.updated_input)
                    authorization = _authorize_final_tool_request(
                        tc,
                        tool_registry=tool_registry,
                        permission_checker=permission_checker,
                        permission_context=permission_context,
                        tool_ctx=tool_ctx,
                        check_scope=True,
                    )
                    if (
                        authorization.error
                        or authorization.request is None
                        or authorization.permission_decision is None
                    ):
                        yield final_tool_call_event()
                        tool_call_emitted = True
                        result = _rejection_result(
                            tc,
                            authorization.error or "Updated tool request failed authorization.",
                            display_summary="Updated request rejected",
                            result_kind=result_kind_for_tool(tc.name, tool_registry),
                            error_kind=authorization.error_kind,
                            user_summary="审批钩子修改后的工具请求未通过完整授权。",
                            projection="approval",
                            model_observation=(
                                "The PermissionRequest hook changed the tool input, but the "
                                "new request failed final authorization and was not executed."
                            ),
                        )
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=None,
                            iteration_id=iteration_id, turn_id=turn_id,
                            tool_ctx=tool_ctx, tool_registry=tool_registry,
                        ):
                            yield event
                        return
                    final_request = authorization.request
                    permission_decision = authorization.permission_decision
                    _bind_final_tool_request(tc, final_request)
                    perm = permission_decision.permission_level
                    execution_args = deepcopy(dict(tc.arguments))
                    approval_args = dict(execution_args)
                    declared_permission = getattr(tool_registry.get_tool(tc.name), "permission", None)
                    needs_diff_review = (
                        perm == PermissionLevel.DIFF_REVIEW
                        or declared_permission == PermissionLevel.DIFF_REVIEW
                    )
                    diff = (
                        _detached_tool_diff(
                            tc,
                            workspace_root=tool_ctx.workspace_root,
                            tool_ctx=tool_ctx,
                        )
                        if needs_diff_review
                        else None
                    )
                    yield AgentEvent.permission_decision(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        decision=permission_decision.decision,
                        source="policy",
                        permission_level=perm.value,
                        message=(
                            permission_decision.capability_reason
                            if not permission_decision.capability_allowed
                            else permission_decision.matched_rule
                            if permission_decision.decision == "deny"
                            else "PermissionRequest input was fully re-evaluated."
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
                        request_digest=final_request.digest,
                    )
                    updated_denial = _permission_decision_denial(permission_decision)
                    if updated_denial:
                        yield final_tool_call_event()
                        tool_call_emitted = True
                        result = _rejection_result(
                            tc,
                            updated_denial,
                            display_summary="Updated request denied",
                            result_kind=result_kind_for_tool(tc.name, tool_registry),
                            error_kind="permission_required",
                            user_summary="审批钩子修改后的请求被权限策略拒绝。",
                            projection="approval",
                            model_observation=(
                                "The PermissionRequest hook changed the input, and the refreshed "
                                "permission/capability policy denied the new request."
                            ),
                        )
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=None,
                            iteration_id=iteration_id, turn_id=turn_id,
                            tool_ctx=tool_ctx, tool_registry=tool_registry,
                        ):
                            yield event
                        return
                    if perm == PermissionLevel.AUTO and permission_hook.permission_decision != "ask":
                        permission_allowed_by_hook = True
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
                        request_digest=_final_tool_request_digest(tc),
                    )
                if permission_hook.permission_decision == "ask":
                    # Hook ask is an approval floor even when the refreshed
                    # policy would otherwise auto-allow this request.
                    perm = max(
                        (perm, PermissionLevel.CONFIRM),
                        key=lambda value: {
                            PermissionLevel.AUTO: 0,
                            PermissionLevel.CONFIRM: 1,
                            PermissionLevel.DIFF_REVIEW: 2,
                            PermissionLevel.ALWAYS_DENY: 3,
                        }[value],
                    )
                if (
                    permission_hook.permission_decision == "allow"
                    and tc.name != "exit_plan_mode"
                    and permission_decision.capability_allowed
                    and permission_decision.decision != "deny"
                    and permission_decision.matched_rule_source in hook_overridable_sources
                ):
                    permission_allowed_by_hook = True
            except Exception as exc:
                logger.exception("permission_request hook failed closed")
                yield final_tool_call_event()
                tool_call_emitted = True
                result = _rejection_result(
                    tc,
                    f"PermissionRequest hook failed; tool '{tc.name}' was not executed.",
                    display_summary="Approval hook failed",
                    result_kind=result_kind_for_tool(tc.name, tool_registry),
                    error_kind="security_boundary",
                    user_summary="审批钩子失败，操作已安全拒绝。",
                    projection="approval",
                    model_observation="The permission hook failed, so the tool call was not executed.",
                )
                async for event in _finalize_tool_result(
                    tc, result, ctx=ctx, state=state, diff=None,
                    iteration_id=iteration_id, turn_id=turn_id,
                    tool_ctx=tool_ctx, tool_registry=tool_registry,
                ):
                    yield event
                return
        if permission_allowed_by_hook and not tool_call_emitted:
            yield final_tool_call_event()
            tool_call_emitted = True
        if not permission_allowed_by_hook:
            approval_request = _bound_final_tool_request(tc)
            if approval_request is None:
                approval_request = FinalExecutableToolRequest.freeze(tc)
                _bind_final_tool_request(tc, approval_request)
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
                args=approval_args,
                diff=diff,
                source_agent=str(tool_ctx.metadata.get("run_id") or tool_ctx.metadata.get("agent_role") or "").strip(),
                source_thread=str(tool_ctx.conversation_id or tool_ctx.metadata.get("conversation_id") or tool_ctx.session_id or "").strip(),
                source_tool=tc.name,
                request_digest=_final_tool_request_digest(tc),
            )
            if approval_handler:
                approval = await _await_approval_within_turn_deadline(
                    approval_handler,
                    tc,
                    tool_ctx,
                    expected_digest=_final_tool_request_digest(tc),
                )
                current_digest = canonical_tool_request_digest(
                    tc.name,
                    tc.arguments or {},
                )
                if current_digest != approval_request.digest:
                    # Restore the immutable proposal so neither the rejection
                    # evidence nor a later callback can accidentally adopt the
                    # unapproved mutation.
                    _bind_final_tool_request(tc, approval_request)
                    yield final_tool_call_event()
                    tool_call_emitted = True
                    result = _rejection_result(
                        tc,
                        (
                            "Tool arguments changed while approval was pending. "
                            "The changed request was not executed; submit it again for approval."
                        ),
                        display_summary="Approval became stale",
                        result_kind=result_kind_for_tool(tc.name, tool_registry),
                        error_kind="approval_stale",
                        user_summary="等待审批期间工具参数发生变化，操作已拒绝。",
                        projection="approval",
                        model_observation=(
                            "The tool request changed while awaiting approval. It was not executed; "
                            "issue a new call if the changed action is still needed."
                        ),
                    )
                    async for event in _finalize_tool_result(
                        tc, result, ctx=ctx, state=state, diff=diff,
                        iteration_id=iteration_id, turn_id=turn_id,
                        tool_ctx=tool_ctx, tool_registry=tool_registry,
                    ):
                        yield event
                    return
                if approval.get("action") == "reject":
                    guidance = approval.get("guidance", "user rejected this action")
                    yield final_tool_call_event()
                    tool_call_emitted = True
                    result = ToolResult(content=f"Operation rejected: {guidance}", is_error=True)
                    # Preserve the proposed diff on rejection so the UI can show what
                    # would have been applied so the UI can show the rejected edit.
                    async for event in _finalize_tool_result(
                        tc, result, ctx=ctx, state=state, diff=diff,
                        iteration_id=iteration_id, turn_id=turn_id,
                        tool_ctx=tool_ctx,
                        tool_registry=tool_registry,
                    ):
                        yield event
                    return
                action = str(approval.get("action") or "").strip().lower()
                if action not in {"approve", "partial"}:
                    yield final_tool_call_event()
                    tool_call_emitted = True
                    result = _rejection_result(
                        tc,
                        "Approval response did not contain an explicit approve decision.",
                        display_summary="Invalid approval response",
                        result_kind=result_kind_for_tool(tc.name, tool_registry),
                        error_kind="approval_invalid",
                        user_summary="审批响应无效，操作未执行。",
                        projection="approval",
                        model_observation="The approval response was invalid, so the tool was not executed.",
                    )
                    async for event in _finalize_tool_result(
                        tc, result, ctx=ctx, state=state, diff=diff,
                        iteration_id=iteration_id, turn_id=turn_id,
                        tool_ctx=tool_ctx, tool_registry=tool_registry,
                    ):
                        yield event
                    return

                # Carry explicit approval as exact request evidence into the
                # execution context.  Catastrophic command checks consume this
                # digest; they never infer approval from a broad session mode.
                approved_digests = tool_ctx.metadata.setdefault(
                    "_approved_request_digests", set()
                )
                if isinstance(approved_digests, set):
                    approved_digests.add(_final_tool_request_digest(tc))

                generic_updated_input = approval.get("updated_input")
                if generic_updated_input is None:
                    generic_updated_input = approval.get("updatedInput")
                if tc.name != "exit_plan_mode" and generic_updated_input is not None:
                    yield final_tool_call_event()
                    tool_call_emitted = True
                    result = _rejection_result(
                        tc,
                        (
                            "The approval response proposed different tool arguments. "
                            "A new approval request is required for the changed action."
                        ),
                        display_summary="Changed request needs approval",
                        result_kind=result_kind_for_tool(tc.name, tool_registry),
                        error_kind="approval_stale",
                        user_summary="审批响应修改了工具参数，需要重新发起审批。",
                        projection="approval",
                        model_observation=(
                            "The user changed the proposed tool input. Do not execute it under the old "
                            "approval; issue a new tool call with the changed arguments."
                        ),
                    )
                    async for event in _finalize_tool_result(
                        tc, result, ctx=ctx, state=state, diff=diff,
                        iteration_id=iteration_id, turn_id=turn_id,
                        tool_ctx=tool_ctx, tool_registry=tool_registry,
                    ):
                        yield event
                    return

                if tc.name == "exit_plan_mode":
                    approval_request.apply(tc)
                    edited_plan = approval.get("plan") or approval.get("updated_plan")
                    if isinstance(edited_plan, str):
                        tc.arguments = {**tc.arguments, "plan": edited_plan}
                    approved_prompts = approval.get("command_prompts")
                    if isinstance(approved_prompts, list):
                        tc.arguments = {
                            **tc.arguments,
                            "command_prompts": approved_prompts,
                        }
                if approval.get("action") == "partial":
                    decisions = approval.get("decisions", {})
                    rejected = [str(path) for path, decision in decisions.items() if decision == "rejected"]
                    if rejected:
                        approved = [str(path) for path, decision in decisions.items() if decision == "approved"]
                        guidance = str(approval.get("guidance") or "").strip()
                        result = ToolResult(
                            content=(
                                "Operation rejected because tool approvals are atomic. "
                                "No files were changed. "
                                + (f"Approved files may be proposed again separately: {', '.join(approved)}. " if approved else "")
                                + f"Rejected files: {', '.join(rejected)}."
                                + (f" User guidance: {guidance}" if guidance else "")
                            ),
                            is_error=True,
                        )
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=diff,
                            iteration_id=iteration_id, turn_id=turn_id,
                            tool_ctx=tool_ctx,
                            tool_registry=tool_registry,
                        ):
                            yield event
                        return

                if tc.name == "exit_plan_mode":
                    edited_authorization = _authorize_final_tool_request(
                        tc,
                        tool_registry=tool_registry,
                        permission_checker=permission_checker,
                        permission_context=permission_context,
                        tool_ctx=tool_ctx,
                        check_scope=True,
                    )
                    if edited_authorization.request is not None:
                        _bind_final_tool_request(tc, edited_authorization.request)
                    edited_denial = (
                        _permission_decision_denial(edited_authorization.permission_decision)
                        if edited_authorization.permission_decision is not None
                        else ""
                    )
                    if (
                        edited_authorization.error
                        or edited_authorization.request is None
                        or edited_authorization.permission_decision is None
                        or edited_denial
                    ):
                        yield final_tool_call_event()
                        tool_call_emitted = True
                        result = _rejection_result(
                            tc,
                            edited_authorization.error
                            or edited_denial
                            or "The edited plan request failed authorization.",
                            display_summary="Edited plan rejected",
                            result_kind=result_kind_for_tool(tc.name, tool_registry),
                            error_kind=(
                                edited_authorization.error_kind
                                if edited_authorization.error
                                else "permission_required"
                            ),
                            user_summary="用户编辑后的计划请求未通过完整授权。",
                            projection="approval",
                            model_observation=(
                                "The user-edited plan failed final authorization and was not executed."
                            ),
                        )
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=None,
                            iteration_id=iteration_id, turn_id=turn_id,
                            tool_ctx=tool_ctx, tool_registry=tool_registry,
                        ):
                            yield event
                        return
                    permission_decision = edited_authorization.permission_decision
                    perm = permission_decision.permission_level
                    execution_args = deepcopy(dict(tc.arguments))
                    diff = (
                        _detached_tool_diff(
                            tc,
                            workspace_root=tool_ctx.workspace_root,
                            tool_ctx=tool_ctx,
                        )
                        if _tool_supports_diff(tc.name, tool_registry)
                        else None
                    )
                    approved_prompts = tc.arguments.get("command_prompts")
                    if isinstance(approved_prompts, list):
                        prompt_setter = (tool_ctx.metadata or {}).get(
                            "command_prompt_allow_rules_setter"
                        )
                        if callable(prompt_setter):
                            prompts = [
                                str(item.get("prompt") or "").strip()
                                for item in approved_prompts
                                if isinstance(item, dict)
                                and item.get("tool") == "run_command"
                                and str(item.get("prompt") or "").strip()
                            ]
                            setter_result = prompt_setter(
                                prompts,
                                source="exit_plan_mode.command_prompts",
                            )
                            if inspect.isawaitable(setter_result):
                                await setter_result

                if not tool_call_emitted:
                    yield final_tool_call_event()
                    tool_call_emitted = True
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
                yield final_tool_call_event()
                tool_call_emitted = True
                async for event in _finalize_tool_result(
                    tc, result, ctx=ctx, state=state, diff=diff,
                    iteration_id=iteration_id, turn_id=turn_id,
                    tool_ctx=tool_ctx,
                    tool_registry=tool_registry,
                ):
                    yield event
                return

    if not tool_call_emitted:
        yield final_tool_call_event()
        tool_call_emitted = True

    if diff is None and _tool_supports_diff(tc.name, tool_registry):
        diff = _detached_tool_diff(tc, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx)

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

    await _invalidate_turn_diff_after_inexact_mutation(
        tc,
        result,
        tool_registry=tool_registry,
        tool_ctx=tool_ctx,
    )

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
    visibility = (
        tool_projection.visibility
        if final_status == "success" and not truncated.is_error
        else "timeline"
    )
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
    ctx.append_tool_result(
        tc.id,
        tc.name,
        context_result,
        conversation_id=str(getattr(tool_ctx, "conversation_id", "") or ""),
        workspace_root=getattr(tool_ctx, "workspace_root", None),
    )
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
        request_digest=truncated.request_digest,
        cleanup_receipt=truncated.cleanup_receipt,
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
        visibility=visibility,
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
        cleanup_receipt=truncated.cleanup_receipt,
        output_files=truncated.output_files,
        superseded_tool_call_ids=truncated.superseded_tool_call_ids,
        removed_file_paths=truncated.removed_file_paths,
        request_digest=truncated.request_digest,
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
    image_events: list[AgentEvent] = []
    if result.result_kind == "image_generation" and not result.is_error:
        for image in result.images:
            if not isinstance(image, dict):
                continue
            image_data = str(image.get("data") or "").strip()
            media_type = str(image.get("media_type") or "image/png").strip()
            if image_data:
                image_events.append(AgentEvent.image_chunk(image_data, media_type))
    # Keep the terminal tool_result last because runtime-span settlement reads
    # the final emitted event as the authoritative tool completion record.
    return [*image_events, event]


def _resolve_workspace_path_for_diff(file_path: str, workspace_root: Path | str | None) -> Path:
    path = Path(str(file_path))
    if path.is_absolute():
        return path.resolve()
    if workspace_root:
        # Resolve so "sub/../sub/a.txt" and "./sub/a.txt" collapse to the same
        # key read_file recorded (which always .resolve()s). Without this, a
        # legitimately-read file could be rejected on a spelling variation with
        # a "re-read the file" error that won't fix it.
        return (Path(workspace_root).resolve() / path).resolve()
    return path.resolve()


def generate_diff(
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None = None,
    tool_ctx: ToolExecutionContext | None = None,
) -> dict[str, Any] | None:
    read_time_hashes = _read_time_hashes(tool_ctx)
    if tool_name == "write_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        if file_path and content:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            return generate_file_diff_payload(str(resolved_path), content)
    elif tool_name == "edit_file":
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if file_path and old_string:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            return generate_edit_diff_payload(
                str(resolved_path),
                old_string,
                new_string,
                replace_all=bool((args or {}).get("replace_all")),
            )
    elif tool_name == "apply_patch":
        from backend.tools.apply_patch import build_apply_patch_diff_payload

        patch_text = args.get("patch")
        if isinstance(patch_text, str):
            expected_hashes: dict[str, str] = {}
            payload = build_apply_patch_diff_payload(
                patch_text,
                tool_ctx,
                expected_hashes=expected_hashes,
                read_time_hashes=read_time_hashes,
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
    path_key = (
        canonical_path_mapping_key(read_time_hashes, path)
        if isinstance(read_time_hashes, dict)
        else canonical_file_path_key(path)
    )
    # Prefer the READ-TIME content hash (recorded by read_file) so the
    # "file changed since read" guard spans the read->write window. Existing
    # files without a recorded read deliberately keep an empty expected_hash:
    # edit_file/write_file reject them with the read-before-edit
    # contract instead of blessing a blind edit with a hash read
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
