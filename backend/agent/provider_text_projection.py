"""Phase-aware projection for provider text chunks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
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
    """Sanitize one provider item, preserving late classification of that item."""
    phase = _text_phase(event)
    owner = stream_text.sanitizer_owner
    item_changed = bool(owner and event.item_id and owner.item_id and event.item_id != owner.item_id)
    provisional_continues = bool(
        owner and not owner.phase and not phase
    )
    anonymous_phase_changed = bool(
        owner and not owner.item_id and not event.item_id
        and ((owner.phase == "commentary" and phase in {"final_answer", "final"})
             or (owner.phase in {"final_answer", "final"} and phase == "commentary"))
    )
    if (item_changed and not provisional_continues) or anonymous_phase_changed:
        async for projected in finish_provider_text_item(
            stream_state=stream_state, stream_text=stream_text,
            visible_text_sanitizer=visible_text_sanitizer,
            live_text_streaming=live_text_streaming, awaiting_trailing_done=awaiting_trailing_done,
            process_event_factory=process_event_factory,
        ):
            yield projected
        owner = None
    phase = phase or (owner.phase if owner else "")
    # A content-part end can carry the first authoritative phase. It does not
    # close the message's Markdown parser or split a control tag in that item.
    normalized = replace(event, phase=phase, item_id=owner.item_id if provisional_continues and owner.item_id else event.item_id)
    stream_text.sanitizer_owner = replace(normalized, content="", raw={})
    visible_chunk = visible_text_sanitizer.feed(event.content) if event.content else ""
    async for projected in _project_visible_provider_text(
        normalized, visible_chunk,
        stream_state=stream_state, stream_text=stream_text,
        provider_raw_final_text=provider_raw_final_text,
        live_text_streaming=live_text_streaming, awaiting_trailing_done=awaiting_trailing_done,
        process_event_factory=process_event_factory,
    ):
        yield projected


def _text_phase(event: StreamEvent) -> str:
    raw = dict(getattr(event, "raw", {}) or {})
    return (str(raw.get("message_phase") or "").strip() or str(event.phase or "").strip()).lower()


async def finish_provider_text_item(
    *,
    stream_state: StreamAttemptState,
    stream_text: StreamTextState,
    visible_text_sanitizer: Any,
    live_text_streaming: bool,
    awaiting_trailing_done: bool,
    process_event_factory: Callable[..., AgentEvent | None],
    terminal_phase: str = "",
) -> AsyncIterator[AgentEvent]:
    """Release this parser's held literal suffix through its original item."""
    owner = stream_text.sanitizer_owner
    if owner is None:
        return
    stream_text.sanitizer_owner = None
    tail = visible_text_sanitizer.finish()
    if not tail:
        return
    owner = replace(owner, content=tail, phase=owner.phase or terminal_phase)
    async for projected in _project_visible_provider_text(
        owner, tail,
        stream_state=stream_state, stream_text=stream_text,
        provider_raw_final_text=stream_state.raw_final_text,
        live_text_streaming=live_text_streaming, awaiting_trailing_done=awaiting_trailing_done,
        process_event_factory=process_event_factory,
    ):
        if isinstance(projected, AgentEvent):
            yield projected


async def _project_visible_provider_text(
    event: StreamEvent,
    visible_chunk: str,
    *,
    stream_state: StreamAttemptState,
    stream_text: StreamTextState,
    provider_raw_final_text: dict[str, Any],
    live_text_streaming: bool,
    awaiting_trailing_done: bool,
    process_event_factory: Callable[..., AgentEvent | None],
) -> AsyncIterator[AgentEvent | ProviderTextProjectionResult]:
    """Use the same phase/item emission for streamed and terminal visible text."""
    provider_raw_text = dict(getattr(event, "raw", {}) or {})
    provider_phase = (
        str(provider_raw_text.get("message_phase") or "").strip()
        or str(getattr(event, "phase", "") or "").strip()
    ).lower()
    if not event.content:
        # Content-part completion may carry the first authoritative phase.
        # It updates the already-projected item without inventing a text delta
        # or treating the part boundary as whole-response completion.
        same_pending_item = (
            not event.item_id or not stream_text.pending_unphased_item_id
            or event.item_id == stream_text.pending_unphased_item_id
        )
        if stream_text.pending_unphased_text and same_pending_item:
            if provider_phase in {"final_answer", "final"}:
                stream_text.accept_unphased_answer()
                stream_text.saw_final_answer_phase = True
                stream_text.final_candidate_item_id = event.item_id or stream_text.final_candidate_item_id
                if stream_text.agent_message_started:
                    stream_text.active_agent_message_source = "model_final"
                stream_state.response_phase = "final_answer"
            elif provider_phase == "commentary":
                stream_text.reclassify_unphased_as_process()
                if stream_text.agent_message_started:
                    stream_text.active_agent_message_source = "commentary"
                stream_state.response_phase = "commentary"
        if provider_raw_text and provider_phase != "commentary":
            provider_raw_final_text.update(provider_raw_text)
        yield ProviderTextProjectionResult(False)
        return
    stream_text.full_text += visible_chunk
    if visible_chunk:
        stream_state.saw_visible_output = True

    if provider_phase in {"final_answer", "final"}:
        stream_state.response_phase = "final_answer"
        if not stream_text.saw_final_answer_phase:
            # Relabeling the same provisional item also relabels its prefix.
            # A distinct item can still leave the earlier text as commentary.
            if (
                not event.item_id
                or not stream_text.pending_unphased_item_id
                or event.item_id == stream_text.pending_unphased_item_id
            ):
                stream_text.accept_unphased_answer()
            elif stream_text.has_live_provisional_item:
                completed = stream_text.complete_active_agent_message(
                    stream_text.active_agent_message_text,
                    source="commentary", status="completed",
                )
                if completed is not None:
                    yield completed
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
    elif provider_phase == "commentary" or (not provider_phase and stream_state.committed_tool_ids):
        if provider_phase == "commentary":
            stream_state.response_phase = "commentary"
        if stream_state.committed_tool_ids or (
            stream_text.agent_message_started and stream_text.active_agent_message_source == "commentary"
        ):
            # Narration after a committed tool owns a new ordered item. Reusing
            # the iteration's process buffer would replace earlier commentary.
            for projected in stream_text.project_agent_message_delta(
                visible_chunk, source="commentary", item_id=event.item_id,
            ):
                if live_text_streaming or projected.type == "item.started":
                    yield projected
        else:
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
