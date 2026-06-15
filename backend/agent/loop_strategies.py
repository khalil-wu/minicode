"""
Stream Processing Strategies for Agent Loop.

Extracts stream event handling, text accumulation, and recovery logic
from the main agent loop into reusable, testable components.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.llm.base import StreamEventType, ToolCallEvent

# Hermes-style: strip internal reasoning tags from user-visible text.
# Some models (especially via proxies) leak <thinking>/<reasoning> blocks
# into the text stream. Strip them before storing or emitting.
_THINKING_TAG_RE = re.compile(
    r"<(?:thinking|reasoning|internal)[^>]*>.*?</(?:thinking|reasoning|internal)>",
    re.DOTALL | re.IGNORECASE,
)
_PRIVATE_REASONING_TAG_OPEN_RE = re.compile(
    r"<(?:thinking|reasoning|internal)\b",
    re.IGNORECASE,
)


def scrub_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> and similar tags from model output."""
    if not text or "<" not in text:
        return text
    return _THINKING_TAG_RE.sub("", text).strip()


@dataclass
class FinalAnswerController:
    """
    Tracks final-answer streaming and allows draft retraction.

    Manages the lifecycle of a final answer:
    - Accumulates draft text as it arrives
    - Emits final_answer_started/delta events
    - Supports retraction when tool calls appear mid-stream
    - Handles preamble (reasoning text before tool calls)
    """

    draft_text: str = ""
    preamble_emitted: bool = False
    final_started: bool = False
    streamed_text: str = ""
    hold_for_privacy: bool = False

    def append(self, content: str) -> None:
        """Append content to the draft buffer."""
        if content:
            self.draft_text += content

    def stream_delta(self, content: str) -> list[AgentEvent]:
        """
        Stream a text delta as part of the final answer.

        Returns events to emit (final_answer_started + final_answer_delta).
        Holds content if privacy tags detected.
        """
        if not content:
            return []
        if self.hold_for_privacy or _PRIVATE_REASONING_TAG_OPEN_RE.search(content):
            self.hold_for_privacy = True
            return []
        events: list[AgentEvent] = []
        if not self.final_started:
            self.final_started = True
            events.append(AgentEvent.final_answer_started())
        self.streamed_text += content
        events.append(AgentEvent.final_answer_delta(content))
        return events

    def retract(self, reason: str = "") -> AgentEvent | None:
        """
        Retract a previously started final answer stream.

        Used when:
        - Tool calls appear after text started streaming
        - Draft needs to be replaced with corrected text
        - Privacy tags require content hold

        Returns final_answer_retracted event or None if nothing to retract.
        """
        if not self.final_started and not self.streamed_text:
            return None
        self.final_started = False
        self.streamed_text = ""
        return AgentEvent.final_answer_retracted(reason)

    def emit_final(self, final_text: str) -> list[AgentEvent]:
        """
        Emit the complete final answer, handling draft replacement if needed.

        If streamed text differs from final text, retracts and re-emits.
        Returns events to emit.
        """
        if not final_text:
            return []
        events: list[AgentEvent] = []
        if (
            self.streamed_text
            and self.streamed_text != final_text
            and not final_text.startswith(self.streamed_text)
        ):
            retracted = self.retract("replace_draft")
            if retracted is not None:
                events.append(retracted)
        if not self.final_started:
            self.final_started = True
            events.append(AgentEvent.final_answer_started())
        if self.streamed_text != final_text:
            suffix = final_text[len(self.streamed_text):] if final_text.startswith(self.streamed_text) else final_text
            if suffix:
                self.streamed_text += suffix
                events.append(AgentEvent.final_answer_delta(suffix))
        return events

    def emit_preamble(self) -> AgentEvent | None:
        """
        Emit accumulated draft text as a preamble (model reasoning before tool calls).

        Returns thinking_chunk event or None if already emitted or no draft.
        """
        if self.preamble_emitted or not self.draft_text.strip():
            return None
        self.preamble_emitted = True
        return AgentEvent.thinking_chunk(
            self.draft_text,
            source="model_preamble",
            visibility="timeline",
        )

    def reset(self) -> None:
        """Reset controller state for next iteration."""
        self.draft_text = ""
        self.preamble_emitted = False
        self.final_started = False
        self.streamed_text = ""
        self.hold_for_privacy = False


@dataclass
class StreamProcessor:
    """
    Processes LLM stream events into agent events and state updates.

    Coordinates:
    - Text accumulation and streaming
    - Tool call detection
    - Thinking/reasoning display
    - Final answer lifecycle management

    Example:
        processor = StreamProcessor()
        async for event in llm.stream_chat(messages):
            for agent_event in processor.process_event(event):
                yield agent_event
    """

    full_text: str = ""
    text_buffer: str = ""
    pending_tool_calls: list[ToolCallEvent] | None = None
    streamed_text: bool = False
    process_text_emitted: bool = False
    thinking_chars: int = 0
    final_answer: FinalAnswerController | None = None

    def __post_init__(self) -> None:
        if self.final_answer is None:
            self.final_answer = FinalAnswerController()
        if self.pending_tool_calls is None:
            self.pending_tool_calls = []

    def reset(self) -> None:
        """Reset processor state for next stream iteration."""
        self.full_text = ""
        self.text_buffer = ""
        self.pending_tool_calls = []
        self.streamed_text = False
        self.process_text_emitted = False
        self.thinking_chars = 0
        if self.final_answer:
            self.final_answer.reset()

    def process_event(self, event: Any) -> list[AgentEvent]:
        """
        Process a single stream event and return corresponding agent events.

        Args:
            event: Stream event from LLM adapter

        Returns:
            List of AgentEvent to emit (may be empty)
        """
        events: list[AgentEvent] = []

        if event.type == StreamEventType.TEXT_CHUNK:
            self.full_text += event.content
            self.text_buffer += event.content
            if self.final_answer:
                self.final_answer.append(event.content)
            self.streamed_text = True
            if self.final_answer:
                events.extend(self.final_answer.stream_delta(event.content))

        elif event.type == StreamEventType.THINKING_CHUNK:
            self.thinking_chars += len(event.content or "")
            events.append(
                AgentEvent.thinking_chunk(
                    "模型推理已隐藏",
                    source="provider",
                    visibility="debug",
                    is_raw_provider_reasoning=True,
                )
            )

        elif event.type == StreamEventType.IMAGE_CHUNK:
            events.append(AgentEvent.image_chunk(event.image_data, event.image_media_type))

        elif event.type == StreamEventType.TOOL_CALL_START:
            # Text before tool calls is model reasoning - emit as preamble
            if self.full_text and not self.process_text_emitted:
                if self.final_answer:
                    retracted = self.final_answer.retract("tool_call_started")
                    if retracted is not None:
                        events.append(retracted)
                    preamble_event = self.final_answer.emit_preamble()
                    if preamble_event is not None:
                        events.append(preamble_event)
                self.process_text_emitted = True
                self.text_buffer = ""

        elif event.type == StreamEventType.TOOL_CALL_DELTA:
            pass  # Deltas are accumulated internally by the adapter

        elif event.type == StreamEventType.TOOL_CALL:
            self.pending_tool_calls = event.tool_calls
            if self.pending_tool_calls and self.full_text and not self.process_text_emitted:
                if self.final_answer:
                    retracted = self.final_answer.retract("tool_call_started")
                    if retracted is not None:
                        events.append(retracted)
                    preamble_event = self.final_answer.emit_preamble()
                    if preamble_event is not None:
                        events.append(preamble_event)
                self.process_text_emitted = True
                self.text_buffer = ""

        return events

    def should_break_thinking_loop(self) -> bool:
        """
        Check if thinking loop should be interrupted.

        Safety mechanism: if model has been thinking for >8000 chars
        without emitting text or tool calls, the stream should break
        to prevent infinite thinking loops (e.g., DeepSeek thinking mode).
        """
        return (
            self.thinking_chars > 8000
            and not self.full_text
            and not self.pending_tool_calls
        )

    def scrub_final_text(self) -> tuple[str, bool]:
        """
        Scrub thinking tags from full_text and return (scrubbed_text, was_modified).

        Some models leak <thinking> blocks into output. Remove them before
        storing or emitting as final answer.
        """
        if not self.full_text or "<" not in self.full_text:
            return self.full_text, False
        scrubbed = scrub_thinking_tags(self.full_text)
        return scrubbed, scrubbed != self.full_text
