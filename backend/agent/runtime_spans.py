from __future__ import annotations

import time
from typing import Any

from backend.agent.message import AgentEvent


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
    requires_attention: bool = False,
    data: dict[str, Any] | None = None,
) -> AgentEvent:
    payload: dict[str, Any] = {
        "event": event,
        "span_id": span_id,
        "status": status,
        "ui_visible": bool(ui_visible),
        "debug_only": bool(debug_only),
        "requires_attention": bool(requires_attention),
    }
    if run_id:
        payload["run_id"] = run_id
    if turn_id:
        payload["turn_id"] = turn_id
    if iteration_id:
        payload["iteration_id"] = iteration_id
    if phase:
        payload["phase"] = phase
    if label:
        payload["label"] = label
    if summary:
        payload["summary"] = summary
    if started_at is not None:
        payload["started_at"] = started_at
    if ended_at is not None:
        payload["ended_at"] = ended_at
    if duration_ms is not None:
        payload["duration_ms"] = max(0, int(duration_ms))
    if parent_span_id:
        payload["parent_span_id"] = parent_span_id
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    if tool_name:
        payload["tool_name"] = tool_name
    if agent_id:
        payload["agent_id"] = agent_id
    if waiting_on:
        payload["waiting_on"] = waiting_on
    if blocking_reason:
        payload["blocking_reason"] = blocking_reason
    if data:
        payload["data"] = dict(data)
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
    requires_attention: bool = False,
    data: dict[str, Any] | None = None,
) -> AgentEvent:
    metadata = getattr(tool_ctx, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return runtime_span(
        event,
        span_id=span_id,
        run_id=str(metadata.get("run_id") or metadata.get("task_id") or ""),
        turn_id=str(metadata.get("turn_id") or metadata.get("assistant_message_id") or ""),
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
        requires_attention=requires_attention,
        data=data,
    )

