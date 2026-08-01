"""Projection of provider events that do not own retry or terminal policy."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from pydantic_core import from_json

from backend.agent.message import AgentEvent
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.tool_events import tool_call_pending_event
from backend.llm.base import StreamEvent, StreamEventType


@dataclass(frozen=True, slots=True)
class ProviderProjectionResult:
    handled: bool
    awaiting_trailing_done: bool = False


async def project_non_text_provider_event(
    event: StreamEvent,
    *,
    stream_state: StreamAttemptState,
    stream_text: StreamTextState,
    live_text_streaming: bool,
    tool_executor: Any,
    process_event_factory: Callable[..., AgentEvent | None],
) -> AsyncIterator[AgentEvent | ProviderProjectionResult]:
    """Reduce tool/media/reasoning frames while leaving policy to the loop.

    ERROR, DONE, fallback, and text frames deliberately remain with their
    lifecycle coordinators because they can retry, terminate, or replace the
    active provider request.
    """

    if event.type == StreamEventType.THINKING_CHUNK:
        if event.content:
            yield AgentEvent.thinking_chunk(
                event.content,
                source="provider",
                visibility="timeline",
                is_raw_provider_reasoning=True,
                provider_reasoning_type=str(
                    (getattr(event, "raw", {}) or {}).get("provider_reasoning_type") or ""
                ),
                phase="model",
            )
        yield ProviderProjectionResult(True)
        return
    if event.type == StreamEventType.IMAGE_CHUNK:
        yield AgentEvent.image_chunk(event.image_data, event.image_media_type)
        yield ProviderProjectionResult(True)
        return
    if event.type not in {
        StreamEventType.TOOL_CALL_START,
        StreamEventType.TOOL_CALL_DELTA,
        StreamEventType.TOOL_CALL,
    }:
        yield ProviderProjectionResult(False)
        return

    outcome = stream_state.accept_provider_event(event)
    if event.type == StreamEventType.TOOL_CALL_START:
        start = event.tool_call_start
        if start is not None and start.id and start.name:
            stream_state.partial_tool_names[start.id] = start.name
            yield tool_call_pending_event(
                start,
                started_at=int(time.time() * 1000),
                iteration_id=stream_text.iteration_id,
                tool_registry=tool_executor.tool_registry,
            )
        yield ProviderProjectionResult(True)
        return
    if event.type == StreamEventType.TOOL_CALL_DELTA:
        delta = event.tool_call_delta
        if delta is not None and delta.id:
            tool_name = stream_state.partial_tool_names.get(delta.id, "")
            tool = tool_executor.tool_registry.get_tool(tool_name) if tool_name else None
            if tool is not None:
                try:
                    parsed = from_json(
                        delta.partial_arguments.encode("utf-8"),
                        allow_partial=True,
                    )
                except ValueError:
                    parsed = {}
                if isinstance(parsed, dict):
                    preview = tool.streamed_input_preview(parsed)
                    if preview and preview != stream_state.partial_tool_args.get(delta.id):
                        stream_state.partial_tool_args[delta.id] = preview
                        yield AgentEvent.tool_call(
                            id=delta.id,
                            name=tool_name,
                            args=preview,
                            status="pending",
                            group_id=stream_text.iteration_id,
                            step_id=delta.id,
                            iteration_id=stream_text.iteration_id,
                            phase="tool",
                        )
        yield ProviderProjectionResult(True)
        return
    if event.type != StreamEventType.TOOL_CALL:
        yield ProviderProjectionResult(True)
        return

    if event.tool_calls_final and stream_text.agent_message_started:
        # Partial tool frames are transport detail, not an assistant-item
        # boundary. Tool execution is intentionally deferred until the final
        # assistant item is settled, so retries and length-truncated responses
        # cannot leak side effects.
        completed = stream_text.complete_active_agent_message(
            stream_text.active_agent_message_text,
            source="commentary",
            status="completed",
        )
        if completed is not None:
            yield completed
        stream_text.pending_unphased_text = ""
        stream_text.pending_unphased_visible_text = ""

    tool_executor.add_tools(list(outcome.complete_tool_calls))
    await asyncio.sleep(0)
    pending = stream_state.tool_calls
    if event.tool_calls_final and pending and not stream_text.process_text_emitted:
        source = stream_text.process_text_source
        if live_text_streaming and source != "model_preamble_retracted":
            projected = stream_text.maybe_stream_process_text(
                source=source,
                event_factory=process_event_factory,
            )
            if projected is not None:
                yield projected
        projected = stream_text.flush_pending_process_text(
            pending,
            source=source,
            event_factory=process_event_factory,
        )
        if projected is not None:
            yield projected
    yield ProviderProjectionResult(
        True,
        awaiting_trailing_done=bool(pending and event.tool_calls_final),
    )
