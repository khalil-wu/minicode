"""Deterministic projection of an accepted provider answer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast, get_args

from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import provider_raw_for_projection
from backend.agent.state import TerminalReason, TerminalStatus
from backend.agent.stream_attempt import StreamTextState


@dataclass(frozen=True, slots=True)
class AnswerCommitProjection:
    """Accepted answer events and terminal classification."""

    text_events: tuple[AgentEvent, ...]
    terminal_reason: TerminalReason
    terminal_status: TerminalStatus


def build_answer_commit_projection(
    *,
    stream_text: StreamTextState,
    final_text: str,
    finish_reason: str,
    provider_raw: dict[str, Any],
    degraded_reason: str,
) -> AnswerCommitProjection:
    """Build accepted-answer events without reading mutable loop state."""

    text_events: list[AgentEvent] = []
    if final_text:
        active_item_text = (
            stream_text.active_agent_message_text
            if stream_text.agent_message_started
            else final_text
        )
        started = stream_text.start_agent_message(
            source="model_final",
            item_id=stream_text.final_candidate_item_id,
        )
        if started is not None:
            text_events.append(started)
        completed = stream_text.complete_active_agent_message(
            active_item_text,
            source="model_final",
            status="completed" if not degraded_reason else "partial",
            finish_reason=finish_reason,
            provider_raw=provider_raw_for_projection(provider_raw),
        )
        if completed is not None:
            text_events.append(completed)

    known_reasons = set(get_args(TerminalReason))
    terminal_reason: TerminalReason = (
        cast(TerminalReason, degraded_reason)
        if degraded_reason in known_reasons
        else "invalid_model_action"
        if degraded_reason
        else "completed"
    )
    terminal_status: TerminalStatus = (
        "completed" if terminal_reason == "completed" else "partial"
    )
    return AnswerCommitProjection(
        text_events=tuple(text_events),
        terminal_reason=terminal_reason,
        terminal_status=terminal_status,
    )
