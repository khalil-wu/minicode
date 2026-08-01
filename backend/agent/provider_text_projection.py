"""Phase-aware projection for provider text chunks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.llm.base import StreamEvent


@dataclass(frozen=True, slots=True)
class ProviderTextProjectionResult:
    """Control facts produced while projecting one provider text chunk."""

    steer_eligible: bool


async def project_provider_text_chunk(
    event: StreamEvent,
    *,
    stream_state: StreamAttemptState,
    stream_text: StreamTextState,
    visible_text_sanitizer: Any,
    provider_raw_final_text: dict[str, Any],
    live_text_streaming: bool,
    awaiting_trailing_done: bool,
    process_event_factory: Callable[..., AgentEvent | None],
) -> AsyncIterator[AgentEvent | ProviderTextProjectionResult]:
    """Route provider text by declared phase and project its visible surface."""

    if not event.content:
        yield ProviderTextProjectionResult(False)
        return

    provider_raw_text = dict(getattr(event, "raw", {}) or {})
    provider_phase = (
        str(provider_raw_text.get("message_phase") or "").strip()
        or str(getattr(event, "phase", "") or "").strip()
    ).lower()
    visible_chunk = visible_text_sanitizer.feed(event.content)
    stream_text.full_text += visible_chunk

    if provider_phase in {"final_answer", "final"}:
        if not stream_text.saw_final_answer_phase:
            process_event = stream_text.flush_pending_process_text(
                None,
                source=None,
                event_factory=process_event_factory,
            )
            if process_event is not None:
                yield process_event
        stream_text.saw_final_answer_phase = True
        stream_text.final_candidate_text += visible_chunk
        if live_text_streaming and visible_chunk:
            delta = event.content if "<" not in event.content else visible_chunk
            for projected in stream_text.project_agent_message_delta(delta):
                yield projected
        provider_raw_final_text.update(provider_raw_text)
    elif provider_phase == "commentary":
        stream_text.pending_process_text += visible_chunk
        if live_text_streaming:
            process_event = stream_text.maybe_stream_process_text(
                source="commentary",
                event_factory=process_event_factory,
            )
            if process_event is not None:
                yield process_event
    else:
        # Chat Completions and Anthropic Messages do not label assistant text
        # as commentary/final.  Stream it immediately as the same typed
        # agent-message item used by Responses.  A later tool-use boundary
        # completes that item as commentary; DONE completes it as the answer.
        stream_text.pending_unphased_text += visible_chunk
        stream_text.pending_unphased_visible_text += visible_chunk
        if live_text_streaming and visible_chunk:
            delta = event.content if "<" not in event.content else visible_chunk
            for projected in stream_text.project_agent_message_delta(delta):
                yield projected

    # Raw provider metadata belongs to the authoritative completed item.  It is
    # accumulated here and attached once by the answer commit projection; delta
    # events carry text only, matching the Codex item lifecycle.
    if provider_raw_text and provider_phase != "commentary":
        provider_raw_final_text.update(provider_raw_text)

    yield ProviderTextProjectionResult(
        steer_eligible=(
            not stream_state.saw_partial_tool_call
            and not stream_state.tool_calls
            and not awaiting_trailing_done
        )
    )
