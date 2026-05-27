"""Normalized run-stream events for the agent/UI boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from backend.agent.message import AgentEvent
from backend.ws.events import ServerEventType


RunEventType = Literal[
    "message.delta",
    "reasoning.delta",
    "tool.started",
    "tool.completed",
    "approval.requested",
    "status",
    "turn.completed",
    "turn.failed",
]


@dataclass(slots=True)
class RunEvent:
    """Internal event shape consumed by the WebSocket agent runner."""

    type: RunEventType
    data: dict[str, Any] = field(default_factory=dict)
    legacy_type: ServerEventType | None = None


def is_tool_lifecycle_progress(event: AgentEvent) -> bool:
    if event.type != "agent.progress":
        return False
    data = event.data
    stage = str(data.get("stage") or "").strip()
    phase = str(data.get("phase") or "").strip()
    event_id = str(data.get("id") or "").strip()
    return (
        stage in {"tool", "approval"}
        or phase in {"tool", "approval"}
        or bool(str(data.get("tool_call_id") or "").strip())
        or event_id.startswith(("tool:", "approval:", "ask:"))
    )


def normalize_agent_event(event: AgentEvent) -> RunEvent | None:
    """Convert legacy AgentEvent values into the normalized run stream.

    Adapter-only events such as tool-call argument deltas and retractable draft
    text are intentionally swallowed here. The LLM adapters may still use them
    internally, but the UI receives only stable run events.
    """

    if event.type in {"tool_call_start", "tool_call_delta"}:
        return None

    if event.type == "text_chunk":
        data = {"content": str(event.data.get("content", ""))}
        return RunEvent("message.delta", data)

    if event.type in {"thinking_delta", "thinking"}:
        return RunEvent("reasoning.delta", {"content": str(event.data.get("content", ""))})

    if event.type == "image_chunk":
        return RunEvent(
            "message.delta",
            {
                "image_data": str(event.data.get("image_data") or ""),
                "media_type": str(event.data.get("media_type") or "image/png") or "image/png",
            },
            legacy_type="image_chunk",
        )

    if event.type == "tool_call":
        return RunEvent("tool.started", dict(event.data))

    if event.type == "tool_result":
        return RunEvent("tool.completed", dict(event.data))

    if event.type == "approval_request":
        return RunEvent("approval.requested", dict(event.data))

    if event.type == "ask_user":
        return RunEvent("approval.requested", dict(event.data), legacy_type="ask_user")

    if event.type == "agent.progress":
        if is_tool_lifecycle_progress(event):
            return None
        return RunEvent("status", dict(event.data))

    if event.type == "done":
        return RunEvent("turn.completed", dict(event.data))

    if event.type == "error":
        return RunEvent("turn.failed", dict(event.data))

    return None


def run_event_to_agent_event(event: RunEvent) -> AgentEvent:
    """Adapt normalized run events back to the current WebSocket protocol."""

    if event.legacy_type:
        return AgentEvent(type=event.legacy_type, data=dict(event.data))

    if event.type == "message.delta":
        return AgentEvent.text_chunk(str(event.data.get("content", "")))
    if event.type == "reasoning.delta":
        return AgentEvent.thinking_chunk(str(event.data.get("content", "")))
    if event.type == "tool.started":
        args = event.data.get("args") if isinstance(event.data.get("args"), dict) else {}
        return AgentEvent.tool_call(
            id=str(event.data.get("id") or ""),
            name=str(event.data.get("name") or ""),
            args=cast(dict[str, Any], args),
            status=str(event.data.get("status") or "running"),
            started_at=(
                int(event.data.get("started_at"))
                if event.data.get("started_at") is not None
                else None
            ),
            display_hint=str(event.data.get("display_hint") or ""),
            input_summary=str(event.data.get("input_summary") or ""),
        )
    if event.type == "tool.completed":
        return AgentEvent.tool_result(
            id=str(event.data.get("id") or ""),
            summary=str(event.data.get("summary") or ""),
            artifact_id=(
                str(event.data.get("artifact_id"))
                if event.data.get("artifact_id") is not None
                else None
            ),
            is_error=bool(event.data.get("is_error")),
            diff=event.data.get("diff"),
            source_url=(
                str(event.data.get("source_url"))
                if event.data.get("source_url") is not None
                else None
            ),
            extraction_status=(
                str(event.data.get("extraction_status"))
                if event.data.get("extraction_status") is not None
                else None
            ),
            content_preview=(
                str(event.data.get("content_preview"))
                if event.data.get("content_preview") is not None
                else None
            ),
            evidence_type=(
                str(event.data.get("evidence_type"))
                if event.data.get("evidence_type") is not None
                else None
            ),
            status=(
                str(event.data.get("status"))
                if event.data.get("status") is not None
                else None
            ),
            duration_ms=(
                int(event.data.get("duration_ms"))
                if event.data.get("duration_ms") is not None
                else None
            ),
            display_summary=str(event.data.get("display_summary") or ""),
            result_kind=str(event.data.get("result_kind") or ""),
            limitation=str(event.data.get("limitation") or ""),
        )
    if event.type == "approval.requested":
        if event.legacy_type:
            return AgentEvent(type=event.legacy_type, data=dict(event.data))
        return AgentEvent.approval_request(
            tool_call_id=str(event.data.get("tool_call_id") or ""),
            tool_name=str(event.data.get("tool_name") or ""),
            args=cast(dict[str, Any], event.data.get("args") if isinstance(event.data.get("args"), dict) else {}),
            diff=event.data.get("diff"),
        )
    if event.type == "status":
        return AgentEvent(type="agent.progress", data=dict(event.data))
    if event.type == "turn.completed":
        return AgentEvent(type="done", data=dict(event.data))
    if event.type == "turn.failed":
        return AgentEvent(type="error", data=dict(event.data))

    return AgentEvent(type=cast(ServerEventType, event.type), data=dict(event.data))
