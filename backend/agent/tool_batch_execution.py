"""Ordered tool-batch execution after a provider response settles."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import inspect
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from backend.atomic_io import canonical_path_mapping_key
from backend.agent.context import ContextBuilder
from backend.agent.control_tools import CONTROL_TOOL_NAMES, ControlToolRouter
from backend.agent.final_tool_request import (
    FinalExecutableToolRequest,
    canonical_tool_request_digest,
)
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_events import (
    tool_call_start_event,
    tool_start_times as _tool_start_times,
)
from backend.agent.tool_execution import (
    _apply_pre_tool_hook,
    _authorize_final_tool_request,
    _bind_final_tool_request,
    _bound_final_tool_request,
    _detached_tool_diff,
    _display_path_for_tool_arg,
    _emit_tool_first_output_span,
    _emit_tool_runtime_span,
    _execution_exception_result,
    _final_tool_request_digest,
    _invalidate_turn_diff_after_inexact_mutation,
    _permission_decision_denial,
    _remember_hook_model_context,
    _resolve_workspace_path_for_diff,
    _tool_hook_manager,
    _tool_output_was_streamed,
    _tool_streams_output,
    _tool_supports_diff,
    _tool_turn_id,
    _watch_cleanup_receipt_settlement,
    invalid_tool_call_guard_reason,
    missing_required_tool_argument_reason,
    normalize_tool_call_event,
    prepare_tool_call_sequence,
    run_tool_with_timeout,
    store_result_events,
    subagent_scope_guard_reason,
    toolset_policy_guard_reason,
)
from backend.agent.tool_execution_guardrails import (
    invalid_call_result as _invalid_call_result,
    rejection_result as _rejection_result,
)
from backend.agent.tool_projection import result_kind_for_tool
from backend.agent.tool_runtime import (
    tool_is_idempotent as _tool_is_idempotent,
    tool_mutates as _tool_mutates,
    tool_side_effect_kind as _tool_side_effect_kind,
)
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    cancel_and_drain,
    cancel_and_drain_receipt,
)
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import (
    PermissionContext,
    PermissionDecision,
    ToolExecutionContext,
)
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_DEFAULT_PARALLEL_TOOL_CONCURRENCY = 10
_CREATED_FILE_EDIT_TRACKER_KEY = "_created_file_edit_records"


@dataclass(frozen=True)
class _ToolBatchRuntime:
    ctx: ContextBuilder
    state: AgentState
    tool_registry: ToolRegistry
    tool_ctx: ToolExecutionContext
    iteration_id: str
    turn_id: str = ""


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


def tool_output_delta_events(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    tool_registry: ToolRegistry,
    turn_id: str = "",
    iteration_id: str = "",
) -> list[AgentEvent]:
    if (
        result.is_error
        or not _tool_streams_output(tc.name, tool_registry)
        or not result.content
    ):
        return []

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


async def _emit_tool_completed_runtime_span(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    result_event: AgentEvent,
    *,
    iteration_id: str = "",
) -> None:
    payload = result_event.data if isinstance(result_event.data, dict) else {}
    final_status = str(payload.get("status") or "").strip().lower()
    failed = final_status in {"failed", "blocked", "timeout"} or bool(
        payload.get("is_error")
    )
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
        superseded_ids = list(
            dict.fromkeys(
                [
                    *result.superseded_tool_call_ids,
                    *(str(item.get("tool_call_id") or "") for item in removed_records),
                ]
            )
        )
        superseded_ids = [value for value in superseded_ids if value]
        removed_paths = list(
            dict.fromkeys(
                [
                    *result.removed_file_paths,
                    *(
                        str(item.get("display_path") or item.get("resolved_path") or "")
                        for item in removed_records
                    ),
                ]
            )
        )
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
                        "path": str(
                            item.get("display_path") or item.get("resolved_path") or ""
                        ),
                        "event": "deleted",
                        "temporary": True,
                        "supersedes_tool_call_id": str(item.get("tool_call_id") or ""),
                    }
                    if tool_ctx.workspace_root is not None:
                        file_changed_payload["workspace_root"] = str(
                            tool_ctx.workspace_root
                        )
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
) -> AsyncIterator[AgentEvent]:
    """Flush pending auto-queue, emit a blocked tool-call event, and store the result."""
    async for ev in _flush_queue(
        auto_queue,
        ctx=runtime.ctx,
        state=runtime.state,
        tool_registry=runtime.tool_registry,
        tool_ctx=runtime.tool_ctx,
        iteration_id=runtime.iteration_id,
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
        if not isinstance(item, dict) or str(item.get("status") or "").lower() not in {
            "added",
            "created",
            "new",
        }:
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
        removed.append(
            {
                "tool_call_id": str(raw_record.get("tool_call_id") or ""),
                "resolved_path": resolved_path,
                "display_path": str(raw_record.get("display_path") or resolved_path),
            }
        )
        tracker.pop(key, None)
    return removed


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
            "side_effect_kind": _tool_side_effect_kind(
                tc.name, tool_registry, tc.arguments
            ),
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
        existing.setdefault("batch_reason", reason)
        return
    cleanup_task = tool_ctx.cleanup_tasks_by_call.get(tc.id)
    receipt = aggregate.to_evidence(
        resource_kind="tool",
        resource_id=tc.id,
        reason=reason,
    )
    pending = cleanup_task is not None and not cleanup_task.done()
    receipt["pending"] = 1 if pending else 0
    receipt["completed"] = not pending
    receipt["timed_out"] = pending
    receipt["acknowledged"] = not pending
    receipt.update(
        {
            "requested": bool(requested),
            "side_effect_kind": _tool_side_effect_kind(
                tc.name, tool_registry, tc.arguments
            ),
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
    prepared_tool_calls: list[ToolCallEvent] | None = None,
    execution_limit: int | None = None,
    execution_limit_reason: str = "",
) -> AsyncIterator[AgentEvent]:
    tool_ctx.tool_registry = tool_registry
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
            ):
                yield ev
            continue
        started_epoch = time.time()
        _tool_start_times(state)[tc.id] = started_epoch

        invalid_reason = invalid_tool_call_guard_reason(tc, tool_registry)
        if invalid_reason:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
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
            ):
                yield ev
            continue

        required_reason = missing_required_tool_argument_reason(
            state, tc, tool_registry
        )
        if required_reason:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _invalid_call_result(
                    tc,
                    required_reason,
                ),
                runtime=runtime,
                started_epoch=started_epoch,
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
                request_digest=canonical_tool_request_digest(
                    tc.name, tc.arguments or {}
                ),
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
        if (
            authorization.error
            or authorization.request is None
            or authorization.permission_decision is None
        ):
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
                    projection="approval"
                    if authorization.error_kind == "permission_required"
                    else "error",
                    model_observation=(
                        f"The {tc.name} call was not executed because final request authorization failed."
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
            ):
                yield ev
            continue
        final_request = authorization.request
        permission_decision = authorization.permission_decision
        _bind_final_tool_request(tc, final_request)
        perm = permission_decision.permission_level
        pre_hook_decision = (
            str(getattr(tc, "_pre_tool_hook_permission_decision", "") or "")
            .strip()
            .lower()
        )
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
        if (
            permission_decision.decision != "allow"
            and getattr(permission_context, "mode", "") != "auto"
        ):
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
            hook_mgr = _tool_hook_manager(tool_ctx)
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
                tc,
                auto_queue,
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

        if perm == PermissionLevel.AUTO and tc.name not in CONTROL_TOOL_NAMES:
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
        ):
            yield ev

    async for ev in _flush_queue(
        auto_queue,
        ctx=ctx,
        state=state,
        tool_registry=tool_registry,
        tool_ctx=tool_ctx,
        iteration_id=iteration_id,
    ):
        yield ev


async def _flush_queue(
    queue: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    iteration_id: str = "",
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
                try:
                    result = await run_tool_with_timeout(
                        tc,
                        tool_registry,
                        tool_ctx,
                        iteration_id=iteration_id,
                    )
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
                while (
                    next_executable_index < len(batch)
                    and len(pending) < max_concurrent_tools
                ):
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
                        data={
                            "parallel_index": next_executable_index + 1,
                            "parallel_total": len(batch),
                        },
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
                        if (
                            _tool_streams_output(ready_tc.name, tool_registry)
                            and ready_result.content
                            and not ready_result.is_error
                        ):
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
                        diff=None
                        if ready_result.is_error
                        else diffs_by_id.get(ready_tc.id),
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
                        wait_timeout = max(
                            0.0, batch_deadline - asyncio.get_running_loop().time()
                        )
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
                    _detached_tool_diff(
                        tc, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx
                    )
                    if _tool_supports_diff(tc.name, tool_registry)
                    else None
                )
                result = await run_tool_with_timeout(
                    tc,
                    tool_registry,
                    tool_ctx,
                    iteration_id=iteration_id,
                )
                await _invalidate_turn_diff_after_inexact_mutation(
                    tc,
                    result,
                    tool_registry=tool_registry,
                    tool_ctx=tool_ctx,
                )
                if not _tool_output_was_streamed(tool_ctx, tc.id):
                    if (
                        _tool_streams_output(tc.name, tool_registry)
                        and result.content
                        and not result.is_error
                    ):
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
    remaining = None if deadline is None else float(deadline) - time.monotonic()
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
        asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
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
        permission_decision = proposal_authorization.permission_decision
        perm = permission_decision.permission_level
        execution_args = deepcopy(dict(tc.arguments))
        approval_args = dict(execution_args)
    declared_permission = getattr(tool_registry.get_tool(tc.name), "permission", None)
    needs_diff_review = (
        perm == PermissionLevel.DIFF_REVIEW
        or declared_permission == PermissionLevel.DIFF_REVIEW
    )

    if perm in (PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW):
        diff = (
            _detached_tool_diff(
                tc, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx
            )
            if needs_diff_review
            else None
        )
        hook_mgr = _tool_hook_manager(tool_ctx)
        hook_requested_allow = (
            str(getattr(tc, "_pre_tool_hook_permission_decision", "") or "")
            .strip()
            .lower()
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
                if (
                    permission_hook.blocked
                    or permission_hook.permission_decision == "deny"
                ):
                    message = (
                        permission_hook.message
                        or permission_hook.feedback
                        or "permission request blocked by hook"
                    )
                    yield AgentEvent.permission_decision(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        decision=permission_hook.permission_decision or "deny",
                        permission_level=perm.value,
                        message=message,
                        capability={
                            "allowed": True,
                            "reason": "Capability boundary allows this tool call.",
                        },
                        approval_policy=perm.value,
                        matched_rule={"source": "hook", "rule": "permission_request"},
                        scope={"workspace_scope": tool_ctx.permission.workspace_scope},
                        expiry="call",
                        request_digest=_final_tool_request_digest(tc),
                    )
                    yield final_tool_call_event()
                    tool_call_emitted = True
                    result = ToolResult(
                        content=f"Permission request blocked by hook: {message}",
                        is_error=True,
                    )
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
                            authorization.error
                            or "Updated tool request failed authorization.",
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
                    final_request = authorization.request
                    permission_decision = authorization.permission_decision
                    _bind_final_tool_request(tc, final_request)
                    perm = permission_decision.permission_level
                    execution_args = deepcopy(dict(tc.arguments))
                    approval_args = dict(execution_args)
                    declared_permission = getattr(
                        tool_registry.get_tool(tc.name), "permission", None
                    )
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
                    if (
                        perm == PermissionLevel.AUTO
                        and permission_hook.permission_decision != "ask"
                    ):
                        permission_allowed_by_hook = True
                if permission_hook.has_permission_decision:
                    yield AgentEvent.permission_decision(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        decision=permission_hook.permission_decision,
                        permission_level=perm.value,
                        message=permission_hook.permission_decision_reason
                        or permission_hook.message,
                        capability={
                            "allowed": True,
                            "reason": "Capability boundary allows this tool call.",
                        },
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
                    and permission_decision.matched_rule_source
                    in hook_overridable_sources
                ):
                    permission_allowed_by_hook = True
            except Exception:
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
                source_agent=str(
                    tool_ctx.metadata.get("run_id")
                    or tool_ctx.metadata.get("agent_role")
                    or ""
                ).strip(),
                source_thread=str(
                    tool_ctx.conversation_id
                    or tool_ctx.metadata.get("conversation_id")
                    or tool_ctx.session_id
                    or ""
                ).strip(),
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
                if approval.get("action") == "reject":
                    guidance = approval.get("guidance", "user rejected this action")
                    yield final_tool_call_event()
                    tool_call_emitted = True
                    result = ToolResult(
                        content=f"Operation rejected: {guidance}", is_error=True
                    )
                    # Preserve the proposed diff on rejection so the UI can show what
                    # would have been applied so the UI can show the rejected edit.
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
                    rejected = [
                        str(path)
                        for path, decision in decisions.items()
                        if decision == "rejected"
                    ]
                    if rejected:
                        approved = [
                            str(path)
                            for path, decision in decisions.items()
                            if decision == "approved"
                        ]
                        guidance = str(approval.get("guidance") or "").strip()
                        result = ToolResult(
                            content=(
                                "Operation rejected because tool approvals are atomic. "
                                "No files were changed. "
                                + (
                                    f"Approved files may be proposed again separately: {', '.join(approved)}. "
                                    if approved
                                    else ""
                                )
                                + f"Rejected files: {', '.join(rejected)}."
                                + (f" User guidance: {guidance}" if guidance else "")
                            ),
                            is_error=True,
                        )
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
                        _permission_decision_denial(
                            edited_authorization.permission_decision
                        )
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
                        prompt_setter = (
                            tool_ctx.run_context.command_prompt_allow_rules_setter
                            if tool_ctx.run_context is not None
                            else None
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

    if not tool_call_emitted:
        yield final_tool_call_event()
        tool_call_emitted = True

    if diff is None and _tool_supports_diff(tc.name, tool_registry):
        diff = _detached_tool_diff(
            tc, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx
        )

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
        hook_manager=_tool_hook_manager(tool_ctx),
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
    else:
        result = await run_tool_with_timeout(
            tc, tool_registry, tool_ctx, iteration_id=iteration_id
        )

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
