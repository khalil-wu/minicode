"""Provider stream aggregation owned outside the main agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.provider_activity import reduce_provider_activity
from backend.llm.base import (
    ProviderActivityEvent,
    StreamEvent,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
)


ProcessEventFactory = Callable[..., AgentEvent | None]
TextScrubber = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class ProviderEventOutcome:
    """Structured result of accepting one provider protocol event.

    The loop still owns UI projection, but provider bookkeeping and the exact
    point at which a complete tool call becomes executable live here. This
    prevents retry paths from maintaining a second set of flags alongside the
    accepted provider payload.
    """

    complete_tool_calls: tuple[ToolCallEvent, ...] = ()
    final_tool_batch: bool = False
    provider_done: bool = False
    partial_tool_stream: bool = False


@dataclass(slots=True)
class StreamTextState:
    """Own all mutable text routing for one provider iteration.

    The main loop supplies provider events and consumes projected AgentEvents;
    it no longer owns a parallel set of closure variables for narration,
    commentary, agent-message lifecycle, and retry cleanup.
    """

    iteration_id: str = ""
    full_text: str = ""
    final_candidate_text: str = ""
    pending_process_text: str = ""
    pending_unphased_text: str = ""
    pending_unphased_visible_text: str = ""
    saw_final_answer_phase: bool = False
    agent_message_started: bool = False
    agent_message_sequence: int = 0
    active_agent_message_id: str = ""
    active_agent_message_text: str = ""
    active_agent_message_source: str = ""
    # Source already published for the active item. ``item.started`` announces
    # the initial classification, so a delta only needs to carry a source when
    # the provider reclassifies the same item mid-stream. Repeating it on every
    # token would more than double the wire size of short deltas.
    published_agent_message_source: str = ""
    final_candidate_item_id: str = ""
    pending_unphased_item_id: str = ""
    process_text_emitted: bool = False
    process_text_streamed: bool = False
    process_text_last_emitted: str = ""
    process_text_source: str = "model_preamble"

    @property
    def has_live_provisional_item(self) -> bool:
        """A still-unclassified assistant item already owns the unphased text.

        Live projection publishes unphased narration under ``source=pending``
        before the provider reveals whether it was commentary or the answer.
        While that item is open it is the public representation of those exact
        characters, so neither the process buffer nor a transport item-id switch
        may duplicate or reorder them.
        """

        return self.agent_message_started and self.active_agent_message_source == "pending"

    def start_agent_message(
        self,
        *,
        source: str = "",
        item_id: str = "",
    ) -> AgentEvent | None:
        if self.agent_message_started:
            return None
        self.agent_message_sequence += 1
        item_prefix = self.iteration_id or "turn"
        self.active_agent_message_id = item_id or (
            f"{item_prefix}:agent-message:{self.agent_message_sequence}"
        )
        self.active_agent_message_text = ""
        self.active_agent_message_source = source
        self.published_agent_message_source = source
        self.agent_message_started = True
        return AgentEvent.agent_message_started(
            item_id=self.active_agent_message_id,
            source=source,
        )

    def project_agent_message_delta(
        self,
        delta: str,
        *,
        source: str = "",
        item_id: str = "",
    ) -> tuple[AgentEvent, ...]:
        if not delta:
            return ()
        events: list[AgentEvent] = []
        # Anthropic content blocks and a few OpenAI-compatible transports may
        # expose a transport item id on every chunk (or change it when the
        # stream switches from text to a tool block).  Pi keeps one assistant
        # message for the whole provider turn, and Codex only closes an item at
        # an actual lifecycle boundary.  A provisional/unphased message is
        # therefore a logical item, not a new public item for every provider
        # id.  Preserve that id while the text is still being classified; the
        # tool boundary or DONE event will complete it with its final source.
        preserve_provisional_item = (
            self.has_live_provisional_item
            and source in {"", "pending", "model_final"}
        )
        if (
            item_id
            and self.agent_message_started
            and self.active_agent_message_id != item_id
            and not preserve_provisional_item
        ):
            completed = self.complete_active_agent_message(
                self.active_agent_message_text,
                source=self.active_agent_message_source or source or "model_final",
                status="completed",
            )
            if completed is not None:
                events.append(completed)
        effective_item_id = (
            self.active_agent_message_id
            if preserve_provisional_item
            else item_id
        )
        started = self.start_agent_message(source=source, item_id=effective_item_id)
        if started is not None:
            events.append(started)
        if source:
            self.active_agent_message_source = source
        self.active_agent_message_text += delta
        reclassified = (
            self.active_agent_message_source != self.published_agent_message_source
        )
        if reclassified:
            self.published_agent_message_source = self.active_agent_message_source
        events.append(
            AgentEvent.agent_message_delta(
                delta,
                item_id=self.active_agent_message_id,
                source=self.active_agent_message_source if reclassified else "",
            )
        )
        return tuple(events)

    def complete_active_agent_message(
        self,
        text: str,
        *,
        source: str,
        status: str,
        finish_reason: str = "",
        provider_raw: dict[str, Any] | None = None,
    ) -> AgentEvent | None:
        if not self.agent_message_started or not self.active_agent_message_id:
            return None
        item_id = self.active_agent_message_id
        self.agent_message_started = False
        self.active_agent_message_id = ""
        self.active_agent_message_text = ""
        self.active_agent_message_source = ""
        self.published_agent_message_source = ""
        return AgentEvent.agent_message_completed(
            text,
            item_id=item_id,
            source=source,
            status=status,
            finish_reason=finish_reason,
            provider_raw=provider_raw,
        )

    def cancel_active_agent_message(self) -> AgentEvent | None:
        return self.complete_active_agent_message(
            self.active_agent_message_text,
            source="cancelled",
            status="cancelled",
        )

    def pending_recovery_text(self, scrub: TextScrubber) -> str:
        if self.saw_final_answer_phase and self.final_candidate_text.strip():
            return scrub(self.final_candidate_text)
        if self.pending_unphased_text.strip():
            return scrub(self.pending_unphased_text)
        return ""

    def accepted_answer_text(self, scrub: TextScrubber) -> str:
        if self.saw_final_answer_phase:
            return scrub(self.final_candidate_text)
        return scrub(self.pending_unphased_text or self.final_candidate_text)

    def clear_pending(self) -> None:
        self.pending_process_text = ""
        self.pending_unphased_text = ""
        self.pending_unphased_visible_text = ""
        self.pending_unphased_item_id = ""

    def clear_pending_process_text(self) -> None:
        """Clear commentary without discarding an unphased answer candidate."""
        self.pending_process_text = ""

    def process_buffer(self) -> str:
        if self.has_live_provisional_item:
            return self.pending_process_text.strip()
        return (
            f"{self.pending_process_text}{self.pending_unphased_visible_text}".strip()
        )

    def reclassify_unphased_as_process(self) -> None:
        """Move provisional assistant text across the tool-use boundary in order."""
        if self.pending_unphased_visible_text and not self.has_live_provisional_item:
            self.pending_process_text = (
                f"{self.pending_unphased_visible_text}{self.pending_process_text}"
            )
        self.pending_unphased_text = ""
        self.pending_unphased_visible_text = ""
        self.pending_unphased_item_id = ""

    def maybe_stream_process_text(
        self,
        *,
        source: str,
        event_factory: ProcessEventFactory,
    ) -> AgentEvent | None:
        text = self.process_buffer()
        if not text or text == self.process_text_last_emitted:
            return None
        self.process_text_last_emitted = text
        # Mark the process item as having a live projection.  The terminal
        # flush still emits one completed update, while subsequent chunks only
        # emit when the accumulated content actually changed.
        self.process_text_emitted = True
        self.process_text_streamed = True
        self.process_text_source = source
        return event_factory(
            text,
            [],
            iteration_id=self.iteration_id,
            source=source,
            status="running",
        )

    def accept_unphased_answer(self) -> None:
        if not self.pending_unphased_text:
            return
        self.final_candidate_text += self.pending_unphased_text
        if self.pending_unphased_item_id:
            self.final_candidate_item_id = self.pending_unphased_item_id
        self.pending_unphased_text = ""
        self.pending_unphased_visible_text = ""
        self.pending_unphased_item_id = ""

    def flush_pending_process_text(
        self,
        tool_calls: list[ToolCallEvent] | None,
        *,
        source: str | None,
        event_factory: ProcessEventFactory,
    ) -> AgentEvent | None:
        text = self.process_buffer()
        self.clear_pending()
        if not text or (self.process_text_emitted and not self.process_text_streamed):
            return None
        self.process_text_emitted = True
        resolved_source = source or self.process_text_source
        self.process_text_source = resolved_source
        return event_factory(
            text,
            tool_calls or [],
            iteration_id=self.iteration_id,
            source=resolved_source,
            status="completed",
        )

    def reset_for_retry(self) -> None:
        self.full_text = ""
        self.final_candidate_text = ""
        self.final_candidate_item_id = ""
        self.clear_pending()

    def reset_for_provider_retry(self) -> None:
        self.reset_for_retry()
        self.saw_final_answer_phase = False
        self.process_text_emitted = False
        self.process_text_streamed = False
        self.process_text_last_emitted = ""
        self.process_text_source = "model_preamble"

    def sanitize(self, scrub: TextScrubber) -> None:
        for name in (
            "full_text",
            "final_candidate_text",
            "pending_unphased_text",
            "pending_unphased_visible_text",
            "pending_process_text",
        ):
            value = getattr(self, name)
            if value and "<" in value:
                setattr(self, name, scrub(value))


@dataclass(slots=True)
class StreamAttemptState:
    """Mutable provider output accumulated across one turn iteration.

    Lists and dictionaries are mutated in place so retry paths cannot
    accidentally retain aliases to an abandoned provider payload.
    """

    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    usage: UsageInfo = field(default_factory=UsageInfo)
    raw_done: dict[str, Any] = field(default_factory=dict)
    raw_final_text: dict[str, Any] = field(default_factory=dict)
    response_items: list[dict[str, Any]] = field(default_factory=list)
    response_phase: str = ""
    finish_reason: str = ""
    provider_done: bool = False
    saw_partial_tool_call: bool = False
    final_tool_batch_received: bool = False
    partial_tool_names: dict[str, str] = field(default_factory=dict)
    partial_tool_args: dict[str, dict[str, Any]] = field(default_factory=dict)
    provider_activities: dict[str, ProviderActivityEvent] = field(
        default_factory=dict
    )

    def accept_provider_activity(
        self,
        activity: ProviderActivityEvent | None,
    ) -> ProviderActivityEvent | None:
        """Reduce one provider lifecycle frame into an informative snapshot.

        The same provider-managed operation emits several frames under one id.
        Later terminal frames frequently omit safe metadata exposed earlier;
        they are updates, not instructions to erase those fields.
        """

        if activity is None:
            return None
        activity_key = str(
            activity.id or activity.kind or activity.name or ""
        ).strip()
        if not activity_key:
            return activity
        previous = self.provider_activities.get(activity_key)
        merged = reduce_provider_activity(previous, activity)
        if previous == merged:
            return None
        self.provider_activities[activity_key] = merged
        return merged

    def accept_provider_event(self, event: StreamEvent) -> ProviderEventOutcome:
        """Accept protocol state without making presentation decisions."""

        if event.type in {
            StreamEventType.TOOL_CALL_START,
            StreamEventType.TOOL_CALL_DELTA,
        }:
            self.saw_partial_tool_call = True
            return ProviderEventOutcome(partial_tool_stream=True)
        if event.type == StreamEventType.TOOL_CALL:
            complete = tuple(event.tool_calls)
            self.merge_tool_calls(list(complete))
            self.saw_partial_tool_call = self.saw_partial_tool_call or bool(complete)
            self.final_tool_batch_received = self.final_tool_batch_received or bool(
                event.tool_calls_final
            )
            return ProviderEventOutcome(
                complete_tool_calls=complete,
                final_tool_batch=bool(event.tool_calls_final),
                partial_tool_stream=self.saw_partial_tool_call,
            )
        if event.type == StreamEventType.DONE:
            self.provider_done = True
            self.usage = event.usage
            raw = dict(getattr(event, "raw", {}) or {})
            self.accept_done_payload(
                finish_reason=event.finish_reason,
                raw=raw,
                response_items=[
                    dict(item)
                    for item in (getattr(event, "provider_items", []) or [])
                    if isinstance(item, dict)
                ],
                response_phase=(
                    str(getattr(event, "phase", "") or "").strip()
                    or str(raw.get("response_message_phase") or "").strip()
                ),
            )
            return ProviderEventOutcome(provider_done=True)
        return ProviderEventOutcome()

    def merge_tool_calls(self, incoming: list[ToolCallEvent]) -> None:
        if not incoming:
            return
        by_id: dict[str, list[int]] = {}
        for index, tool_call in enumerate(self.tool_calls):
            by_id.setdefault(tool_call.id, []).append(index)
        incoming_occurrences: dict[str, int] = {}
        for tool_call in incoming:
            occurrence = incoming_occurrences.get(tool_call.id, 0)
            incoming_occurrences[tool_call.id] = occurrence + 1
            matching_indexes = by_id.get(tool_call.id, [])
            if occurrence >= len(matching_indexes):
                index = len(self.tool_calls)
                self.tool_calls.append(tool_call)
                by_id.setdefault(tool_call.id, []).append(index)
            else:
                self.tool_calls[matching_indexes[occurrence]] = tool_call

    def replace_tool_calls(self, incoming: list[ToolCallEvent]) -> None:
        self.tool_calls[:] = incoming

    def take_unsettled_tool_announcements(self) -> tuple[tuple[str, str], ...]:
        """Tool blocks the provider announced but never completed.

        A provider that stops mid tool-call (output limit, cut stream) has
        already published a pending block: the client is showing a card whose
        arguments never arrive, so the call is never executed and no result can
        close it.  The abandoning path owns settling those blocks, so hand them
        over exactly once and forget them.
        """

        settled = {
            str(tool_call.id or "")
            for tool_call in self.tool_calls
            if str(tool_call.id or "")
        }
        abandoned = tuple(
            (tool_id, name)
            for tool_id, name in self.partial_tool_names.items()
            if tool_id and tool_id not in settled
        )
        for tool_id, _ in abandoned:
            self.partial_tool_names.pop(tool_id, None)
            self.partial_tool_args.pop(tool_id, None)
        return abandoned

    def reset_provider_payload(self) -> None:
        self.tool_calls.clear()
        self.usage = UsageInfo()
        self.raw_done.clear()
        self.raw_final_text.clear()
        self.response_items.clear()
        self.response_phase = ""
        self.finish_reason = ""
        self.provider_done = False
        self.saw_partial_tool_call = False
        self.final_tool_batch_received = False
        self.partial_tool_names.clear()
        self.partial_tool_args.clear()
        self.provider_activities.clear()

    @property
    def incomplete_tool_stream(self) -> bool:
        return self.saw_partial_tool_call and not self.tool_calls

    def accept_done_payload(
        self,
        *,
        finish_reason: str,
        raw: dict[str, Any] | None,
        response_items: list[dict[str, Any]] | None,
        response_phase: str,
    ) -> None:
        self.finish_reason = str(finish_reason or "")
        self.raw_done.clear()
        self.raw_done.update(dict(raw or {}))
        self.response_items[:] = list(response_items or [])
        self.response_phase = str(response_phase or "")
