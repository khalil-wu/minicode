"""Public thread observations, independent of the window executing the turn."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from backend.conversations.public_projection import (
    project_public_tool_call,
    project_public_transcript_message,
)
from backend.ws.event_log import is_raw_provider_reasoning_event, sanitize_ws_live_payload
from backend.ws.stream_resume_pages import stream_resume_pages
from backend.ws.stream_state import get_stream_content_blocks


# These are the public thread timeline, never a session's runtime capabilities,
# browser/terminal ownership, provider authentication or private Inspector data.
THREAD_OBSERVATION_TYPES = frozenset({
    "thread/realtime/item/started", "thread/realtime/item/transcript/delta", "thread/realtime/item/completed",
    "item.started", "agent_message.delta", "item.completed", "agent.run.started",
    "agent.run.completed", "agent.item", "agent.item.delta", "thinking_delta", "thinking",
    "tool_call", "tool_output_delta", "tool_result", "command_output_chunk", "runtime.span",
    "guardian_assessment", "guardian_warning", "agent.progress", "task.update",
    "user_message.queue.updated", "control_request", "approval.cancelled", "done", "error",
    "system_notice", "context_usage", "context_compacted", "budget_update", "budget.warning",
    "rate_limit", "session.state_changed", "conversation.compaction.updated", "goal.updated",
    "turn.plan.updated", "turn.diff.updated", "subagent.start", "subagent.event",
    "subagent.progress", "subagent.done", "citation.add", "artifact.preview", "file.changed",
    "workspace.project_index.invalidated", "checkpoint.created",
})
THREAD_CONTROL_SUBTYPES = frozenset({
    "can_use_tool", "request_permissions", "write_stdin", "request_user_input", "elicitation",
})

# A routing identity captured by the server, never read from wire data. Child
# tasks inherit it so delayed admission responses still reach the submitting
# window; execution resources continue to belong to their original session.
_response_origin: ContextVar[tuple[str | None, str] | None] = ContextVar("thread_response_origin", default=None)


@contextmanager
def bind_response_origin(session_id: str | None, command_id: str) -> Iterator[None]:
    token = _response_origin.set((session_id, command_id))
    try:
        yield
    finally:
        _response_origin.reset(token)


def response_origin(command_id: str) -> str | None:
    origin = _response_origin.get()
    return origin[0] if origin is not None and origin[1] == command_id else ""


def public_thread_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    event_type = payload.get("type")
    if event_type not in THREAD_OBSERVATION_TYPES or is_raw_provider_reasoning_event(payload):
        return None
    if payload.get("private_model_only") is True:
        return None
    if event_type == "task.update" and "session" in payload:
        return None
    if event_type == "control_request" and payload["request"]["subtype"] not in THREAD_CONTROL_SUBTYPES:
        return None
    if payload.get("visibility") in {"debug", "hidden", "internal", "redacted"}:
        return None
    # Never copy the original connection envelope or command acknowledgment.
    return sanitize_ws_live_payload({key: value for key, value in payload.items()
        if key not in {"seq", "previous_replay_seq", "event_id", "client_command_id", "client_command_type"}})


def frozen_stream_pages(conversation_id: str, state: dict[str, Any]) -> list[Any]:
    """Freeze the same public tool/content state used by persisted history."""
    tool_states = [project_public_tool_call(record) for record in state.get("tool_calls", {}).values()]
    tool_states = [record for record in tool_states if record.get("id") and record.get("name")]
    blocks = project_public_transcript_message({
        "id": state.get("message_id"), "role": "assistant", "content": "",
        "blocks": get_stream_content_blocks(state),
    }).get("blocks", [])
    return stream_resume_pages(conversation_id, {
        "message_id": state.get("message_id") or None,
        "tool_calls_pending": [record for record in tool_states if record.get("status", "running")
            not in {"success", "completed", "failed", "error", "blocked", "cancelled", "timeout", "timed_out", "partial"}],
        "tool_states": tool_states, "content_blocks": blocks,
        "turn_id": str(state.get("turn_id") or ""), "phase": str(state.get("phase") or ""),
        "stream_status": str(state.get("status") or "running"),
        "event_seq": int(state.get("event_seq") or 0),
        "started_at": state.get("started_at"),
        "last_event_type": str(state.get("last_event_type") or ""),
    })
