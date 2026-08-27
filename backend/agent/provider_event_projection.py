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


_DISPLAYABLE_PROVIDER_REASONING_TYPES = frozenset(
    {
        "reasoning_summary_text",  # OpenAI Responses reasoning summaries
        "thinking_delta",          # Anthropic extended-thinking deltas
        "thinking",                # extension transport thinking deltas
    }
)


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
        reasoning_type = str(
            (getattr(event, "raw", {}) or {}).get("provider_reasoning_type")
            or ""
        )
        # Only surfaces the provider itself intends for display. Raw
        # chain-of-thought stays internal (OpenAI's response.reasoning_text.* is
        # already dropped at the adapter, and MiniCode has no opt-in for it).
        # The excluded thinking frames are not content either: signature_delta
        # carries a cryptographic signature and redacted thinking is an opaque
        # blob. Anthropic's thinking_delta *is* the displayable surface, and
        # dropping it meant a user paying for a thinking budget saw nothing
        # even though ThinkingCell renders exactly this shape.
        displayable = reasoning_type in _DISPLAYABLE_PROVIDER_REASONING_TYPES
        # Untagged block boundaries open/close the cell for the tagged deltas.
        boundary_only = (
            not reasoning_type
            and event.lifecycle in {"start", "end"}
            and str(getattr(event, "content_kind", "") or "") == "thinking"
        )
        if not displayable and not boundary_only:
            yield ProviderProjectionResult(True)
            return
        # Provider streams may split reasoning into whitespace-only deltas.
        # They carry no visible content and must not cross the strict event
        # constructor, which rejects empty delta bodies. Lifecycle boundaries
        # remain representable with an empty body.
        if event.content.strip() or event.lifecycle in {"start", "end"}:
            yield AgentEvent.thinking_chunk(
                event.content,
                source="provider",
                visibility="timeline",
                phase="model",
                item_id=event.item_id,
                content_index=event.content_index,
                lifecycle=event.lifecycle,
            )
        yield ProviderProjectionResult(True)
        return
    if event.type == StreamEventType.PROVIDER_ACTIVITY:
        activity = stream_state.accept_provider_activity(
            event.provider_activity
        )
        if activity is not None and activity.message:
            status = str(activity.status or "info").strip().lower()
            if status not in {"running", "completed", "failed", "info"}:
                status = "info"
            image_activity = str(activity.kind or "").strip().lower() == "image_generation"
            yield AgentEvent.progress(
                activity.message,
                stage="image_generation" if image_activity else "tool",
                status=status,
                id=f"provider:{activity.id or activity.kind or activity.name}",
                phase="image_generation" if image_activity else "tool",
                label=activity.name or activity.kind,
                summary=activity.message,
                detail=activity.detail,
                visibility="timeline",
                count=activity.count,
                ephemeral=status == "running",
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
            tool = (
                tool_executor.tool_registry.get_tool(tool_name) if tool_name else None
            )
            if tool is not None:
                try:
                    parsed = from_json(
                        delta.partial_arguments.encode("utf-8"),
                        allow_partial=True,
                    )
                except ValueError:
                    parsed = {}
                if isinstance(parsed, dict):
                    preview = tool.streamed_input_preview(
                        parsed,
                        context=getattr(tool_executor, "tool_ctx", None),
                        prior=stream_state.partial_tool_args.get(delta.id),
                    )
                    if preview and preview != stream_state.partial_tool_args.get(
                        delta.id
                    ):
                        stream_state.partial_tool_args[delta.id] = preview
                        # ``diff`` and underscore-prefixed keys are projection
                        # channels, not public tool arguments: the live line
                        # counts travel beside args, and the private state is
                        # handed back to the tool on the next delta only.
                        public_preview = {
                            key: value
                            for key, value in preview.items()
                            if key != "diff" and not key.startswith("_")
                        }
                        live_diff = preview.get("diff")
                        yield AgentEvent.tool_call(
                            id=delta.id,
                            name=tool_name,
                            args=public_preview,
                            status="pending",
                            group_id=stream_text.iteration_id,
                            step_id=delta.id,
                            iteration_id=stream_text.iteration_id,
                            phase="tool",
                            diff=live_diff if isinstance(live_diff, dict) else None,
                        )
        yield ProviderProjectionResult(True)
        return
    if event.type != StreamEventType.TOOL_CALL:
        yield ProviderProjectionResult(True)
        return

    # The first typed tool frame proves any preceding unphased assistant text
    # was narration. Move it into the process buffer immediately so text that
    # arrives between non-final and final tool frames is appended in order.
    stream_text.reclassify_unphased_as_process()

    tool_only_batch = bool(
        event.tool_calls_final and not stream_text.agent_message_started
    )

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
            completed.data["tool_calls"] = [
                {
                    "id": str(tool_call.id or ""),
                    "name": str(tool_call.name or ""),
                    "arguments": dict(tool_call.arguments or {}),
                }
                for tool_call in stream_state.tool_calls
                if str(tool_call.id or "").strip()
            ]
            yield completed
    elif tool_only_batch:
        # A tool-only provider response is an assistant message in Pi, but it
        # is not a visible MiniCode answer item. Reuse the existing hidden raw
        # stream envelope for the bridge-only message metadata; this avoids
        # duplicating public tool_call events or fabricating an empty answer.
        yield AgentEvent.stream_event(
            provider="agent-loop",
            event_type="tool_only_assistant",
            data={
                "tool_calls": [
                    {
                        "id": str(tool_call.id or ""),
                        "name": str(tool_call.name or ""),
                        "arguments": dict(tool_call.arguments or {}),
                    }
                    for tool_call in stream_state.tool_calls
                    if str(tool_call.id or "").strip()
                ]
            },
            sdk_only=True,
        )

    tool_executor.add_tools(list(outcome.complete_tool_calls))
    await asyncio.sleep(0)
    pending = stream_state.tool_calls
    if event.tool_calls_final and pending:
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
