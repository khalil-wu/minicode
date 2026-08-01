"""Project one acquired provider event and return its stream-state transition."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.loop_process_events import model_process_text_event
from backend.agent.loop_runtime_helpers import epoch_ms
from backend.agent.message import AgentEvent
from backend.agent.provider_event_projection import (
    ProviderProjectionResult,
    project_non_text_provider_event,
)
from backend.agent.provider_stream_control import (
    ProviderFallbackReset,
    ProviderSteerResult,
    apply_provider_chunk_steer,
    reset_for_provider_fallback,
)
from backend.agent.provider_text_projection import (
    ProviderTextProjectionResult,
    project_provider_text_chunk,
)
from backend.llm.base import StreamEventType


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderDispatchResult:
    action: Literal["continue", "break", "error"]
    usage: Any
    finish_reason: str
    response_phase: str
    awaiting_trailing_tool_done: bool
    visible_text_sanitizer: Any
    thinking_chars: int
    prefer_stateful_history: bool
    provider_stream_steered: bool = False


async def dispatch_provider_event(
    event: Any,
    *,
    llm: Any,
    provider_attempt: Any,
    iteration_id_value: str,
    state: Any,
    context_builder: Any,
    turn_kernel: Any,
    tool_context: Any,
    stream_iter: Any,
    stream_state: Any,
    stream_text: Any,
    tool_executor: Any,
    settings: Any,
    provider_completion: Any,
    prompt_cache_safe_params: dict[str, Any],
    tool_batch_count: int,
    iteration_limit: int,
    usage: Any,
    finish_reason: str,
    response_phase: str,
    awaiting_trailing_tool_done: bool,
    visible_text_sanitizer: Any,
    thinking_chars: int,
    prefer_stateful_history: bool,
) -> AsyncIterator[AgentEvent | ProviderDispatchResult]:
    """Observe, project and transition one provider event."""

    await turn_kernel.observe_provider_first_event(
        provider_attempt,
        progress_origin_ms=provider_attempt.started_at,
        observed_at=epoch_ms(),
    )
    raw_event = getattr(event, "raw", None)
    if raw_event and isinstance(raw_event, dict):
        provider_name = type(llm).__name__.replace("Adapter", "").lower()
        yield AgentEvent.stream_event(
            provider=provider_name,
            event_type=(
                event.type.value
                if hasattr(event.type, "value")
                else str(event.type)
            ),
            data=dict(raw_event),
            sdk_only=True,
        )
    if tool_context.cancel_event is not None and tool_context.cancel_event.is_set():
        raise asyncio.CancelledError

    if event.type == StreamEventType.ERROR:
        yield _result(
            action="error",
            usage=usage,
            finish_reason=finish_reason,
            response_phase=response_phase,
            awaiting_trailing_tool_done=awaiting_trailing_tool_done,
            visible_text_sanitizer=visible_text_sanitizer,
            thinking_chars=thinking_chars,
            prefer_stateful_history=prefer_stateful_history,
        )
        return

    if event.type == StreamEventType.TEXT_CHUNK and event.content:
        text_projection = None
        async for projected in project_provider_text_chunk(
            event,
            stream_state=stream_state,
            stream_text=stream_text,
            visible_text_sanitizer=visible_text_sanitizer,
            provider_raw_final_text=stream_state.raw_final_text,
            live_text_streaming=settings.live_text_streaming,
            awaiting_trailing_done=awaiting_trailing_tool_done,
            process_event_factory=model_process_text_event,
        ):
            if isinstance(projected, ProviderTextProjectionResult):
                text_projection = projected
            else:
                yield projected
        steer_result = None
        async for steer_update in apply_provider_chunk_steer(
            steer_eligible=bool(
                text_projection is not None and text_projection.steer_eligible
            ),
            turn_kernel=turn_kernel,
            context_builder=context_builder,
            stream_text=stream_text,
            state=state,
            tool_executor=tool_executor,
            stream_iter=stream_iter,
            provider_attempt=provider_attempt,
        ):
            if isinstance(steer_update, ProviderSteerResult):
                steer_result = steer_update
            else:
                yield steer_update
        if steer_result is None:
            raise RuntimeError("provider steer handler returned without a result")
        yield _result(
            action="break" if steer_result.steered else "continue",
            usage=usage,
            finish_reason=finish_reason,
            response_phase=response_phase,
            awaiting_trailing_tool_done=awaiting_trailing_tool_done,
            visible_text_sanitizer=visible_text_sanitizer,
            thinking_chars=thinking_chars,
            prefer_stateful_history=prefer_stateful_history,
            provider_stream_steered=steer_result.steered,
        )
        return

    if event.type == StreamEventType.FALLBACK_RESTART:
        fallback_reset = None
        async for fallback_update in reset_for_provider_fallback(
            stream_text=stream_text,
            stream_state=stream_state,
            tool_executor=tool_executor,
        ):
            if isinstance(fallback_update, ProviderFallbackReset):
                fallback_reset = fallback_update
            else:
                yield fallback_update
        if fallback_reset is None:
            raise RuntimeError("provider fallback handler returned without a result")
        yield _result(
            action="continue",
            usage=fallback_reset.usage,
            finish_reason="",
            response_phase="",
            awaiting_trailing_tool_done=False,
            visible_text_sanitizer=fallback_reset.visible_text_sanitizer,
            thinking_chars=thinking_chars,
            prefer_stateful_history=prefer_stateful_history,
        )
        return

    if event.type in {
        StreamEventType.THINKING_CHUNK,
        StreamEventType.IMAGE_CHUNK,
        StreamEventType.TOOL_CALL_START,
        StreamEventType.TOOL_CALL_DELTA,
        StreamEventType.TOOL_CALL,
    }:
        if event.type == StreamEventType.THINKING_CHUNK:
            previous_thinking_chars = thinking_chars
            thinking_chars += len(event.content or "")
            if thinking_chars > 8000 and previous_thinking_chars <= 8000:
                logger.info(
                    "Provider reasoning exceeded 8000 chars; continuing stream"
                )
        projection_result = ProviderProjectionResult(False)
        async for projected in project_non_text_provider_event(
            event,
            stream_state=stream_state,
            stream_text=stream_text,
            live_text_streaming=settings.live_text_streaming,
            tool_executor=tool_executor,
            process_event_factory=model_process_text_event,
        ):
            if isinstance(projected, ProviderProjectionResult):
                projection_result = projected
            else:
                yield projected
        if projection_result.awaiting_trailing_done:
            awaiting_trailing_tool_done = True
        yield _result(
            action="continue",
            usage=usage,
            finish_reason=finish_reason,
            response_phase=response_phase,
            awaiting_trailing_tool_done=awaiting_trailing_tool_done,
            visible_text_sanitizer=visible_text_sanitizer,
            thinking_chars=thinking_chars,
            prefer_stateful_history=prefer_stateful_history,
        )
        return

    if event.type == StreamEventType.DONE:
        completion = await provider_completion.settle(
            event,
            stream_state=stream_state,
            provider_attempt=provider_attempt,
            prompt_cache_safe_params=prompt_cache_safe_params,
            iteration_id=iteration_id_value,
            iteration_limit=iteration_limit,
            tool_batch_count=tool_batch_count,
            prefer_stateful_history=prefer_stateful_history,
        )
        for completion_event in completion.events:
            yield completion_event
        yield _result(
            action="continue",
            usage=completion.usage,
            finish_reason=completion.finish_reason,
            response_phase=completion.response_phase,
            awaiting_trailing_tool_done=False,
            visible_text_sanitizer=visible_text_sanitizer,
            thinking_chars=thinking_chars,
            prefer_stateful_history=completion.prefer_stateful_history,
        )
        return

    yield _result(
        action="continue",
        usage=usage,
        finish_reason=finish_reason,
        response_phase=response_phase,
        awaiting_trailing_tool_done=awaiting_trailing_tool_done,
        visible_text_sanitizer=visible_text_sanitizer,
        thinking_chars=thinking_chars,
        prefer_stateful_history=prefer_stateful_history,
    )


def _result(
    *,
    action: Literal["continue", "break", "error"],
    usage: Any,
    finish_reason: str,
    response_phase: str,
    awaiting_trailing_tool_done: bool,
    visible_text_sanitizer: Any,
    thinking_chars: int,
    prefer_stateful_history: bool,
    provider_stream_steered: bool = False,
) -> ProviderDispatchResult:
    return ProviderDispatchResult(
        action=action,
        usage=usage,
        finish_reason=finish_reason,
        response_phase=response_phase,
        awaiting_trailing_tool_done=awaiting_trailing_tool_done,
        visible_text_sanitizer=visible_text_sanitizer,
        thinking_chars=thinking_chars,
        prefer_stateful_history=prefer_stateful_history,
        provider_stream_steered=provider_stream_steered,
    )
