"""Control-plane transitions that interrupt or replace a provider stream."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from backend.agent.loop_process_events import model_process_text_event
from backend.agent.loop_runtime_helpers import epoch_ms
from backend.agent.message import AgentEvent
from backend.agent.response_utils import append_assistant_history
from backend.agent.stream_sanitizer import ThinkingStreamSanitizer, scrub_thinking_tags


@dataclass(frozen=True, slots=True)
class ProviderSteerResult:
    steered: bool


@dataclass(frozen=True, slots=True)
class ProviderFallbackReset:
    usage: Any
    visible_text_sanitizer: ThinkingStreamSanitizer


async def apply_provider_chunk_steer(
    *,
    steer_eligible: bool,
    turn_kernel: Any,
    context_builder: Any,
    stream_text: Any,
    state: Any,
    tool_executor: Any,
    stream_iter: Any,
    provider_attempt: Any,
) -> AsyncIterator[AgentEvent | ProviderSteerResult]:
    """Apply a queued steer only at a text projection safe boundary."""

    if not steer_eligible:
        yield ProviderSteerResult(False)
        return
    chunk_steer = turn_kernel.pop_turn_steer()
    if chunk_steer is None:
        yield ProviderSteerResult(False)
        return

    partial_text = scrub_thinking_tags(stream_text.full_text).strip()
    process_event = stream_text.flush_pending_process_text(
        None,
        source="commentary",
        event_factory=model_process_text_event,
    )
    if process_event is not None:
        yield process_event
    completed = stream_text.cancel_active_agent_message()
    if completed is not None:
        yield completed
    if partial_text:
        append_assistant_history(
            context_builder,
            partial_text,
            phase=(
                "final_answer"
                if stream_text.saw_final_answer_phase
                else "commentary"
            ),
        )
    await turn_kernel.accept_turn_steer(chunk_steer)
    state.mark_transition(
        "user_steer_provider_chunk",
        message_id=chunk_steer.message_id,
        user_message_id=chunk_steer.user_message_id,
        target_message_id=chunk_steer.target_message_id,
    )
    tool_executor.cancel_remaining()
    close_stream = getattr(stream_iter, "aclose", None)
    if callable(close_stream):
        with suppress(Exception):
            await close_stream()
    await turn_kernel.close_provider_attempt(
        provider_attempt,
        status="interrupted",
        summary="Provider stream redirected by user steer",
        event="provider.request.interrupted",
        ended_at=epoch_ms(),
        data={"reason": "user_steer"},
    )
    stream_text.reset_for_retry()
    yield ProviderSteerResult(True)


async def reset_for_provider_fallback(
    *,
    stream_text: Any,
    stream_state: Any,
    tool_executor: Any,
) -> AsyncIterator[AgentEvent | ProviderFallbackReset]:
    """Discard speculative output before a backup provider continues."""

    tool_executor.cancel_remaining()
    completed = stream_text.cancel_active_agent_message()
    if completed is not None:
        yield completed
    stream_text.reset_for_provider_fallback()
    stream_state.reset_provider_payload()
    yield ProviderFallbackReset(
        usage=stream_state.usage,
        visible_text_sanitizer=ThinkingStreamSanitizer(),
    )
