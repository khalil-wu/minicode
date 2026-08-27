from __future__ import annotations

import time
from typing import Any

from backend.agent.message import (
    AgentEvent,
    _MAX_EVENT_ID_CHARS,
    _MAX_EVENT_METADATA_CHARS,
    _MAX_EVENT_SUMMARY_CHARS,
    _bounded_event_record,
    _non_negative_event_int,
    _optional_event_text,
    _required_event_text,
)


_RUNTIME_SPAN_STATUSES = frozenset(
    {
        "running",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "superseded",
        "partial",
        "info",
    }
)
_TOOL_RUNTIME_SPAN_EVENTS = frozenset(
    {
        "tool.preparing",
        "tool.queued",
        "approval.waiting",
        "tool.started",
        "tool.first_output",
        "tool.completed",
    }
)


def epoch_ms() -> int:
    return int(time.time() * 1000)


def runtime_span(
    event: str,
    *,
    span_id: str,
    run_id: str = "",
    turn_id: str = "",
    iteration_id: str = "",
    phase: str = "",
    status: str = "running",
    label: str = "",
    summary: str = "",
    started_at: int | None = None,
    ended_at: int | None = None,
    duration_ms: int | None = None,
    parent_span_id: str = "",
    tool_call_id: str = "",
    tool_name: str = "",
    agent_id: str = "",
    waiting_on: str = "",
    blocking_reason: str = "",
    ui_visible: bool = True,
    debug_only: bool = False,
    data: dict[str, Any] | None = None,
) -> AgentEvent:
    clean_event = _required_event_text(
        event,
        field_name="event",
        maximum=256,
    )
    clean_span_id = _required_event_text(
        span_id,
        field_name="span_id",
        maximum=_MAX_EVENT_ID_CHARS,
    )
    clean_status = _required_event_text(
        status,
        field_name="status",
        maximum=32,
    ).lower()
    if clean_status not in _RUNTIME_SPAN_STATUSES:
        raise ValueError(f"Unsupported runtime-span status: {clean_status}")
    clean_phase = _optional_event_text(
        phase,
        field_name="phase",
        maximum=64,
    ).lower()
    clean_tool_call_id = _optional_event_text(
        tool_call_id,
        field_name="tool_call_id",
        maximum=_MAX_EVENT_ID_CHARS,
    )
    clean_tool_name = _optional_event_text(
        tool_name,
        field_name="tool_name",
        maximum=1_024,
    )
    if clean_tool_call_id:
        if not clean_tool_name:
            raise ValueError("tool-owned runtime.span requires tool_name")
        if clean_event not in _TOOL_RUNTIME_SPAN_EVENTS:
            raise ValueError(f"Unsupported tool runtime-span event: {clean_event}")
        if clean_phase not in {"tool", "approval"}:
            raise ValueError("tool-owned runtime.span requires tool/approval phase")
    if not isinstance(ui_visible, bool):
        raise ValueError("ui_visible must be a boolean")
    if not isinstance(debug_only, bool):
        raise ValueError("debug_only must be a boolean")
    payload: dict[str, Any] = {
        "event": clean_event,
        "span_id": clean_span_id,
        "status": clean_status,
        "ui_visible": ui_visible,
        "debug_only": debug_only,
    }
    optional_text_fields = (
        ("run_id", run_id, _MAX_EVENT_ID_CHARS),
        ("turn_id", turn_id, _MAX_EVENT_ID_CHARS),
        ("iteration_id", iteration_id, _MAX_EVENT_ID_CHARS),
        ("parent_span_id", parent_span_id, _MAX_EVENT_ID_CHARS),
        ("label", label, 4_096),
        ("summary", summary, _MAX_EVENT_SUMMARY_CHARS),
        ("agent_id", agent_id, _MAX_EVENT_ID_CHARS),
        ("waiting_on", waiting_on, 1_024),
        ("blocking_reason", blocking_reason, _MAX_EVENT_METADATA_CHARS),
    )
    for field_name, value, maximum in optional_text_fields:
        clean_value = _optional_event_text(
            value,
            field_name=field_name,
            maximum=maximum,
        )
        if clean_value:
            payload[field_name] = clean_value
    if clean_phase:
        payload["phase"] = clean_phase
    if clean_tool_call_id:
        payload["tool_call_id"] = clean_tool_call_id
        payload["tool_name"] = clean_tool_name
    if started_at is not None:
        payload["started_at"] = _non_negative_event_int(
            started_at,
            field_name="started_at",
        )
    if ended_at is not None:
        payload["ended_at"] = _non_negative_event_int(
            ended_at,
            field_name="ended_at",
        )
    if duration_ms is not None:
        payload["duration_ms"] = _non_negative_event_int(
            duration_ms,
            field_name="duration_ms",
        )
    if "started_at" in payload and "ended_at" in payload:
        elapsed = payload["ended_at"] - payload["started_at"]
        if elapsed < 0:
            raise ValueError("ended_at cannot precede started_at")
        if "duration_ms" in payload and payload["duration_ms"] != elapsed:
            raise ValueError("duration_ms must match ended_at - started_at")
    if data is not None:
        payload["data"] = _bounded_event_record(data, field_name="data")
    return AgentEvent(type="runtime.span", data=payload)


def runtime_span_from_tool_context(
    event: str,
    *,
    span_id: str,
    tool_ctx: Any | None = None,
    iteration_id: str = "",
    phase: str = "tool",
    status: str = "running",
    label: str = "",
    summary: str = "",
    started_at: int | None = None,
    ended_at: int | None = None,
    duration_ms: int | None = None,
    tool_call_id: str = "",
    tool_name: str = "",
    waiting_on: str = "",
    blocking_reason: str = "",
    ui_visible: bool = True,
    debug_only: bool = False,
    data: dict[str, Any] | None = None,
) -> AgentEvent:
    metadata = getattr(tool_ctx, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return runtime_span(
        event,
        span_id=span_id,
        run_id=str(metadata.get("run_id") or metadata.get("task_id") or ""),
        turn_id=str(metadata.get("run_id") or metadata.get("turn_id") or ""),
        iteration_id=iteration_id,
        phase=phase,
        status=status,
        label=label,
        summary=summary,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        agent_id=str(metadata.get("agent_id") or metadata.get("subagent_id") or metadata.get("agent_role") or ""),
        waiting_on=waiting_on,
        blocking_reason=blocking_reason,
        ui_visible=ui_visible,
        debug_only=debug_only,
        data=data,
    )
