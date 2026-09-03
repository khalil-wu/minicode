from __future__ import annotations

import copy
import time
from typing import Any

from backend.agent.provider_activity import (
    merge_provider_activity_detail,
    provider_progress_lifecycle_regressed,
)
from backend.agent.turn_state import (
    _append_bounded_output,
    append_agent_message_delta,
    append_thinking_block,
    complete_agent_message_block,
    start_agent_message_block,
)
from backend.ws.event_log import is_raw_provider_reasoning_event


_TERMINAL_TOOL_STATUSES = {
    "success",
    "completed",
    "failed",
    "error",
    "blocked",
    "cancelled",
    "timeout",
    "timed_out",
    "partial",
}


def _int_or(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _tool_status(payload: dict[str, Any], fallback: str = "running") -> str:
    status = str(payload.get("status") or "").strip().lower()
    if status:
        return {
            "completed": "success",
            "error": "failed",
            "timed_out": "timeout",
            "waiting_approval": "pending",
        }.get(status, status)
    if payload.get("is_error"):
        return "failed"
    return fallback


def _tool_transition(status: str, payload: dict[str, Any], fallback: str = "running") -> str:
    explicit = str(
        payload.get("transition")
        or payload.get("tool_transition")
        or payload.get("toolTransition")
        or ""
    ).strip()
    if explicit:
        return explicit
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status in {"cancelled", "timed_out"}:
        return "cancelled" if raw_status == "cancelled" else "timeout"
    if payload.get("waiting_on") or payload.get("blocking_reason"):
        return "waiting"
    if status in _TERMINAL_TOOL_STATUSES:
        return {
            "success": "completed",
            "completed": "completed",
            "failed": "failed",
            "error": "failed",
            "blocked": "blocked",
            "cancelled": "cancelled",
            "timeout": "timeout",
            "partial": "completed",
        }.get(status, status)
    if payload.get("output") is not None:
        return "streaming_output"
    return fallback


def _tool_id(payload: dict[str, Any], prior: dict[str, Any] | None = None) -> str:
    return str(
        payload.get("tool_call_id")
        or payload.get("id")
        or (prior or {}).get("id")
        or ""
    ).strip()


def _runtime_span_lifecycle(payload: dict[str, Any]) -> tuple[str, str]:
    event = str(payload.get("event") or "").strip().lower()
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nested_status = str(nested.get("tool_status") or "").strip().lower()
    status_payload = dict(payload)
    if nested_status:
        status_payload["status"] = nested_status

    if event == "tool.preparing":
        return "pending", "prepared"
    if event == "tool.queued":
        return "pending", "queued"
    if event == "approval.waiting":
        return "pending", "waiting_approval"
    if event == "tool.started":
        return "running", "running"
    if event == "tool.first_output":
        return "running", "streaming_output"
    if event == "tool.completed":
        status = _tool_status(status_payload, "success")
        return status, _tool_transition(status, status_payload, "completed")

    status = _tool_status(payload, "running")
    return status, _tool_transition(status, payload)


def _progress_lifecycle(payload: dict[str, Any]) -> tuple[str, str]:
    if str(payload.get("phase") or "").strip().lower() == "approval":
        return "pending", "waiting_approval"
    status = _tool_status(payload, "running")
    return status, _tool_transition(status, payload, "running")


def _tool_record(
    prior: dict[str, Any] | None,
    payload: dict[str, Any],
    *,
    status: str | None = None,
    transition: str | None = None,
) -> dict[str, Any]:
    record = dict(prior or {})
    record.update(dict(payload))
    tool_id = _tool_id(payload, record)
    if tool_id:
        record["id"] = tool_id
    if payload.get("tool_name") and not record.get("name"):
        record["name"] = str(payload.get("tool_name") or "")
    elif payload.get("name"):
        record["name"] = str(payload.get("name") or "")
    if payload.get("args") is None and isinstance(record.get("arguments"), dict):
        record["args"] = dict(record["arguments"])
    if not isinstance(record.get("args"), dict):
        record["args"] = {}
    resolved_status = status or _tool_status(payload, str(record.get("status") or "running"))
    record["status"] = resolved_status
    record["transition"] = transition or _tool_transition(
        resolved_status,
        payload,
        str(record.get("transition") or "running"),
    )
    for source_key, target_key in (
        ("started_at", "startedAt"),
        ("finished_at", "finishedAt"),
        ("duration_ms", "durationMs"),
        ("waiting_on", "waitingOn"),
        ("blocking_reason", "blockingReason"),
        ("tool_call_id", "toolCallId"),
        ("tool_name", "toolName"),
        ("turn_id", "turnId"),
        ("iteration_id", "iterationId"),
        ("step_id", "stepId"),
        ("group_id", "groupId"),
        ("artifact_id", "artifactId"),
        ("artifact_kind", "artifactKind"),
        ("artifact_media_type", "artifactMediaType"),
        ("artifact_bytes", "artifactBytes"),
        ("source_url", "sourceUrl"),
        ("extraction_status", "extractionStatus"),
        ("content_preview", "contentPreview"),
        ("evidence_type", "evidenceType"),
        ("display_summary", "displaySummary"),
        ("result_kind", "resultKind"),
        ("activity_kind", "activityKind"),
        ("visibility", "visibility"),
        ("provider_error_type", "providerErrorType"),
        ("error_info", "errorInfo"),
        ("error_kind", "errorKind"),
        ("user_summary", "userSummary"),
        ("developer_detail", "developerDetail"),
        ("display_hint", "displayHint"),
        ("input_summary", "inputSummary"),
        ("task_id", "taskId"),
        ("output_preview", "outputPreview"),
        ("stdout_preview", "stdoutPreview"),
        ("stderr_preview", "stderrPreview"),
    ):
        if source_key in payload and payload.get(source_key) is not None:
            record[target_key] = payload[source_key]
    if record["transition"] not in {"waiting", "waiting_approval"}:
        if not payload.get("waiting_on"):
            record.pop("waitingOn", None)
        if not payload.get("blocking_reason"):
            record.pop("blockingReason", None)
    if not record.get("startedAt"):
        record["startedAt"] = int(time.time() * 1000)
    return record


def _stream_content_blocks(state: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = state.get("content_blocks")
    if isinstance(blocks, list):
        return blocks
    blocks: list[dict[str, Any]] = []
    state["content_blocks"] = blocks
    return blocks


def get_stream_content_blocks(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered, renderer-safe snapshot for reconnect."""
    blocks = copy.deepcopy(_stream_content_blocks(state))
    return [
        block
        for block in blocks
        if isinstance(block, dict)
        if not is_raw_provider_reasoning_event(
            {"type": block.get("type"), **block}
        )
    ]


def _replace_tool_block(state: dict[str, Any], record: dict[str, Any]) -> None:
    blocks = _stream_content_blocks(state)
    tool_id = str(record.get("id") or "").strip()
    if not tool_id:
        return
    for index, block in enumerate(blocks):
        if (
            block.get("type") == "tool_call"
            and isinstance(block.get("record"), dict)
            and str(block["record"].get("id") or "").strip() == tool_id
        ):
            blocks[index] = {"type": "tool_call", "record": dict(record)}
            return
    blocks.append({"type": "tool_call", "record": dict(record)})


def _upsert_tool_state(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    status: str | None = None,
    transition: str | None = None,
    terminal_authoritative: bool = False,
) -> dict[str, Any] | None:
    tool_id = _tool_id(payload)
    if not tool_id:
        return None
    tools = state.setdefault("tool_calls", {})
    prior = tools.get(tool_id) if isinstance(tools, dict) else None
    if not isinstance(prior, dict):
        prior = {}
    record = _tool_record(prior, payload, status=status, transition=transition)
    # Runtime/progress events may arrive after the persisted tool result.  A
    # recovery snapshot must not turn a completed/failed tool back into a
    # running card merely because such a late event was projected last.
    prior_status = str(prior.get("status") or "").strip().lower()
    if (
        prior_status in _TERMINAL_TOOL_STATUSES
        and not terminal_authoritative
    ):
        record["status"] = prior_status
        record["transition"] = str(
            prior.get("transition")
            or record.get("transition")
            or "completed"
        )
        for key in (
            "finishedAt",
            "durationMs",
            "errorInfo",
            "errorKind",
            "userSummary",
            "developerDetail",
        ):
            if key in prior:
                record[key] = prior[key]
    tools[tool_id] = record
    _replace_tool_block(state, record)
    return record


def _upsert_activity_block(
    state: dict[str, Any],
    block: dict[str, Any],
) -> None:
    block_id = str(block.get("id") or "").strip()
    if not block_id:
        return
    blocks = _stream_content_blocks(state)
    for index, existing in enumerate(blocks):
        if existing.get("type") == block.get("type") and existing.get("id") == block_id:
            provider_progress = (
                block.get("type") == "progress"
                and str(block_id).startswith("provider:")
            )
            if provider_progress:
                previous_retry_attempt = existing.get("retryAttempt")
                incoming_retry_attempt = block.get("retryAttempt")
                if provider_progress_lifecycle_regressed(
                    previous_status=existing.get("status"),
                    incoming_status=block.get("status"),
                    previous_retry_attempt=(
                        previous_retry_attempt
                        if isinstance(previous_retry_attempt, int)
                        and not isinstance(previous_retry_attempt, bool)
                        else None
                    ),
                    incoming_retry_attempt=(
                        incoming_retry_attempt
                        if isinstance(incoming_retry_attempt, int)
                        and not isinstance(incoming_retry_attempt, bool)
                        else None
                    ),
                    previous_provider_state=existing.get("providerState"),
                    incoming_provider_state=block.get("providerState"),
                ):
                    # A delayed provider frame cannot reopen or downgrade the
                    # reconnect row once a newer lifecycle snapshot won.
                    return

                merged = {**existing, **block}
                merged["timestamp"] = existing.get("timestamp", block.get("timestamp"))
                for key in ("retryAttempt", "maxRetries", "count"):
                    previous_value = existing.get(key)
                    incoming_value = block.get(key)
                    if (
                        isinstance(previous_value, int)
                        and not isinstance(previous_value, bool)
                        and isinstance(incoming_value, int)
                        and not isinstance(incoming_value, bool)
                    ):
                        merged[key] = max(previous_value, incoming_value)
                retry_progress = (
                    "retryAttempt" in existing
                    or "maxRetries" in existing
                    or "providerState" in existing
                    or "retryAttempt" in block
                    or "maxRetries" in block
                    or "providerState" in block
                )
                if retry_progress:
                    # Retry detail describes the current attempt. Retaining
                    # every previous transport error makes the row grow and
                    # hides the retry that is currently pending.
                    if block.get("detail"):
                        merged["detail"] = block.get("detail")
                    else:
                        merged.pop("detail", None)
                else:
                    detail = merge_provider_activity_detail(
                        existing.get("detail"),
                        block.get("detail"),
                    )
                    if detail:
                        merged["detail"] = detail
                if str(merged.get("status") or "").strip().lower() != "running":
                    merged.pop("ephemeral", None)
                blocks[index] = merged
                return

            merged = {**existing, **block}
            if block.get("type") == "progress":
                detail = merge_provider_activity_detail(
                    existing.get("detail"),
                    block.get("detail"),
                )
                if detail:
                    merged["detail"] = detail
                if str(merged.get("status") or "").strip().lower() != "running":
                    merged.pop("ephemeral", None)
            blocks[index] = merged
            return
    blocks.append(block)


def _remove_activity_block(
    state: dict[str, Any],
    *,
    block_type: str,
    block_id: str,
) -> None:
    target_id = str(block_id or "").strip()
    if not target_id:
        return
    blocks = _stream_content_blocks(state)
    blocks[:] = [
        block
        for block in blocks
        if not (
            block.get("type") == block_type
            and str(block.get("id") or "").strip() == target_id
        )
    ]


def create_stream_state(
    conversation_id: str,
    message_id: str,
    turn_id: str = "",
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "turn_id": turn_id,
        "content_blocks": [],
        "phase": "",
        "status": "running",
        "event_seq": 0,
        "last_event_type": "",
        "last_event_at": 0,
        # Retryable errors must say ``recoverable=True`` explicitly. A final
        # error marks the resume snapshot failed, while the canonical ``done``
        # event remains the delivery fence for the live turn.
        "terminal_fenced": False,
        "tool_calls": {},
    }


def apply_stream_event(
    streams: dict[str, dict[str, Any]],
    conversation_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Project a wire event into the reconnectable stream snapshot.

    This is deliberately protocol-shaped and side-effect free outside the
    conversation's stream record.  The snapshot is the authoritative recovery
    state; clients never need to infer a running tool from visible text.
    """
    if not conversation_id:
        return None
    state = streams.get(str(conversation_id))
    if state is None:
        return None

    # A stream slot is owned by one assistant message/turn.  Late events from
    # an older run can arrive after a new turn has reused the conversation key;
    # accepting them would append stale text or reopen old tool cards.
    payload_message_id = str(
        payload.get("message_id") or payload.get("messageId") or ""
    ).strip()
    state_message_id = str(state.get("message_id") or "").strip()
    if payload_message_id and state_message_id and payload_message_id != state_message_id:
        return state
    payload_turn_id = str(
        payload.get("turn_id") or payload.get("turnId") or ""
    ).strip()
    state_turn_id = str(state.get("turn_id") or "").strip()
    if payload_turn_id and state_turn_id and payload_turn_id != state_turn_id:
        return state

    # ``done`` is the sole stream terminal fence.  Recoverable ``error``
    # evidence must continue through the retry ladder, while every event after
    # the durable terminal boundary is stale by definition.
    if bool(state.get("terminal_fenced")):
        return state

    state["event_seq"] = int(state.get("event_seq") or 0) + 1
    state["last_event_type"] = str(event_type or "")
    state["last_event_at"] = int(time.time() * 1000)
    if payload.get("turn_id"):
        state["turn_id"] = str(payload["turn_id"])
    if payload.get("phase") is not None:
        state["phase"] = str(payload.get("phase") or "")
    elif event_type in {"approval_request", "ask_user"}:
        state["phase"] = "approval"
    elif event_type in {
        "tool_call",
        "tool_output_delta",
        "command_output_chunk",
        "tool_result",
        "runtime.span",
    }:
        state["phase"] = "tool"
    if event_type == "item.started":
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if item.get("type") == "agent_message":
            start_agent_message_block(
                _stream_content_blocks(state),
                str(item.get("id") or "agent-message"),
                {
                    key: item[key]
                    for key in ("phase", "visibility")
                    if item.get(key) is not None
                },
            )
            state["phase"] = "model"
    elif event_type == "agent_message.delta":
        append_agent_message_delta(
            _stream_content_blocks(state),
            str(payload.get("item_id") or "agent-message"),
            str(payload.get("delta") or ""),
            {
                key: payload[key]
                for key in ("phase", "visibility")
                if payload.get(key) is not None
            },
        )
        state["phase"] = "model"
    elif event_type == "item.completed":
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if item.get("type") == "agent_message":
            complete_agent_message_block(
                _stream_content_blocks(state),
                item,
                finish_reason=str(payload.get("finish_reason") or ""),
                provider_raw=(
                    payload.get("provider_raw")
                    if isinstance(payload.get("provider_raw"), dict)
                    else None
                ),
            )
            source = str(item.get("source") or "model_final").strip().lower()
            state["phase"] = (
                "final"
                if source in {"model_final", "reply", "partial", "recovery"}
                else "model"
            )
    elif event_type in {"thinking_delta", "thinking"}:
        thinking = str(payload.get("content") or "")
        if thinking:
            append_thinking_block(
                _stream_content_blocks(state),
                thinking,
                {
                    key: payload[key]
                    for key in (
                        "source",
                        "visibility",
                        "phase",
                        "provider_reasoning_type",
                        "item_id",
                        "content_index",
                    )
                    if payload.get(key) is not None
                },
            )
    elif event_type == "tool_call":
        _upsert_tool_state(state, payload, transition="prepared")
    elif event_type in {"approval_request", "ask_user"}:
        _upsert_tool_state(
            state,
            payload,
            status="pending",
            transition="waiting_approval",
        )
    elif event_type in {"tool_output_delta", "command_output_chunk"}:
        _upsert_tool_state(
            state,
            payload,
            status="running",
            transition="streaming_output",
        )
        tool_id = _tool_id(payload)
        if tool_id:
            record = state["tool_calls"][tool_id]
            output = str(payload.get("output") or payload.get("content") or "")
            stream_name = str(payload.get("stream") or "stdout").lower()
            preview_key = "stderrPreview" if stream_name == "stderr" else "stdoutPreview"
            if output:
                current = str(record.get(preview_key) or "")
                record[preview_key] = _append_bounded_output(current, output)
                record["outputPreview"] = _append_bounded_output(
                    str(record.get("outputPreview") or ""),
                    output,
                )
                _replace_tool_block(state, record)
    elif event_type == "tool_result":
        resolved_status = _tool_status(payload, "failed" if payload.get("is_error") else "success")
        record = _upsert_tool_state(
            state,
            payload,
            status=resolved_status,
            transition=_tool_transition(resolved_status, payload),
            terminal_authoritative=True,
        )
        if record is not None:
            record["finishedAt"] = int(time.time() * 1000)
            _replace_tool_block(state, record)
    elif event_type == "runtime.span":
        runtime_status, runtime_transition = _runtime_span_lifecycle(payload)
        _upsert_tool_state(
            state,
            payload,
            status=runtime_status,
            transition=runtime_transition,
        )
    elif event_type == "agent.progress" and payload.get("tool_call_id"):
        progress_status, progress_transition = _progress_lifecycle(payload)
        _upsert_tool_state(
            state,
            payload,
            status=progress_status,
            transition=progress_transition,
        )
    if event_type == "agent.item":
        item_id = str(payload.get("item_id") or payload.get("id") or "").strip()
        if (
            str(payload.get("status") or "").strip().lower() == "retracted"
            or payload.get("visibility") == "debug"
        ):
            _remove_activity_block(
                state,
                block_type="process",
                block_id=item_id,
            )
            return state
        content = str(payload.get("content") or payload.get("summary") or "").strip()
        if item_id and content:
            block: dict[str, Any] = {
                "type": "process",
                "id": item_id,
                "itemKind": str(payload.get("kind") or "process_text"),
                "content": content,
                "timestamp": _int_or(payload.get("created_at"), int(time.time() * 1000)),
            }
            for source_key, target_key in (
                ("title", "title"),
                ("summary", "summary"),
                ("source", "source"),
                ("status", "status"),
                ("role", "role"),
                ("visibility", "visibility"),
                ("loop_id", "loopId"),
                ("iteration_id", "iterationId"),
                ("parent_id", "parentId"),
                ("group_id", "groupId"),
                ("step_id", "stepId"),
                ("skill_name", "skillName"),
                ("trigger_mode", "triggerMode"),
                ("source_level", "sourceLevel"),
                ("reason", "reason"),
            ):
                if payload.get(source_key) is not None:
                    block[target_key] = payload[source_key]
            if isinstance(payload.get("tool_call_ids"), list):
                block["toolCallIds"] = [
                    str(item) for item in payload["tool_call_ids"] if str(item or "").strip()
                ]
            for source_key, target_key in (
                ("default_collapsed", "defaultCollapsed"),
            ):
                if isinstance(payload.get(source_key), bool):
                    block[target_key] = payload[source_key]
            for key in ("seq", "order"):
                if payload.get(key) is not None:
                    block[key] = _int_or(payload.get(key), 0)
            token_estimate = payload.get("token_estimate", payload.get("tokenEstimate"))
            if token_estimate is not None:
                block["tokenEstimate"] = _int_or(token_estimate, 0)
            _upsert_activity_block(state, block)
    elif event_type == "agent.progress" and not payload.get("tool_call_id"):
        progress_id = str(payload.get("id") or "").strip()
        message = str(payload.get("message") or "").strip()
        if progress_id and message and payload.get("visibility") != "debug":
            block = {
                "type": "progress",
                "id": progress_id,
                "stage": str(payload.get("stage") or "status"),
                "status": str(payload.get("status") or "info"),
                "message": message,
                "timestamp": int(time.time() * 1000),
            }
            for source_key, target_key in (
                ("phase", "phase"),
                ("label", "label"),
                ("summary", "summary"),
                ("visibility", "visibility"),
                ("detail", "detail"),
                ("tool_call_id", "toolCallId"),
                ("tool_name", "toolName"),
                ("group_id", "groupId"),
                ("step_id", "stepId"),
                ("iteration_id", "iterationId"),
                ("ephemeral", "ephemeral"),
                ("count", "count"),
                ("retry_attempt", "retryAttempt"),
                ("max_retries", "maxRetries"),
                ("retry_after_ms", "retryAfterMs"),
                ("error_message", "errorMessage"),
                ("operation_id", "operationId"),
                ("provider_state", "providerState"),
            ):
                if payload.get(source_key) is not None:
                    block[target_key] = payload[source_key]
            _upsert_activity_block(state, block)
    elif event_type == "done":
        state["status"] = str(payload.get("status") or "completed")
        state["terminal_reason"] = str(payload.get("reason") or "")
        state["terminal_fenced"] = True
    elif event_type == "error":
        # Errors may be provider retry evidence only when that is explicit.
        # Defaulting a missing field to recoverable leaves reconnect snapshots
        # running forever after a terminal provider failure.
        if payload.get("recoverable") is not True:
            state["status"] = "failed"
    return state


def upsert_pending_tool_call(
    stream_state: dict[str, Any],
    tool_id: str,
    record: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    tool_calls = stream_state.get("tool_calls")
    if not isinstance(tool_calls, dict):
        tool_calls = {}
        stream_state["tool_calls"] = tool_calls
    if tool_id:
        tool_calls[tool_id] = dict(record)
        _replace_tool_block(stream_state, dict(record, id=tool_id))
    return tool_calls
