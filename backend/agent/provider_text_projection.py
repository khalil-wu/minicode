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
        if event.item_id:
            stream_text.final_candidate_item_id = event.item_id
        if live_text_streaming and visible_chunk:
            for projected in stream_text.project_agent_message_delta(
                visible_chunk,
                source="model_final",
                item_id=event.item_id,
            ):
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
        # as commentary/final. Once a provider has started a typed tool item,
        # later unphased text is necessarily process narration and must never
        # appear in the copyable answer surface.
        if (
            stream_state.saw_partial_tool_call
            or stream_state.tool_calls
            or awaiting_trailing_done
        ):
            stream_text.pending_process_text += visible_chunk
            if live_text_streaming:
                process_event = stream_text.maybe_stream_process_text(
                    source="commentary",
                    event_factory=process_event_factory,
                )
                if process_event is not None:
                    yield process_event
        else:
            # Before that boundary the item remains provisional: DONE can
            # commit it as the answer, while a later tool batch reclassifies
            # the same item as commentary. Keep this provisional text out of
            # the copyable answer surface until the provider settles the
            # assistant item. Otherwise a pre-tool sentence briefly appears as
            # a final reply and is only retracted after the tool frame arrives.
            stream_text.pending_unphased_text += visible_chunk
            stream_text.pending_unphased_visible_text += visible_chunk
            if event.item_id:
                stream_text.pending_unphased_item_id = event.item_id
            if live_text_streaming and visible_chunk:
                for projected in stream_text.project_agent_message_delta(
                    visible_chunk,
                    source="pending",
                    item_id=event.item_id,
                ):
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
