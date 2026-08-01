"""Commit an accepted final answer to history and terminal runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from backend.agent.answer_commit_projection import AnswerCommitProjection
from backend.agent.message import AgentEvent
from backend.llm.base import UsageInfo


@dataclass(frozen=True, slots=True)
class AnswerCommitDependencies:
    context: Any
    state: Any
    turn_kernel: Any
    append_assistant_history: Callable[..., None]
    set_terminal_reason: Callable[..., Any]


class AnswerCommitter:
    """Own the single durable commit point for a provider final answer."""

    def __init__(self, dependencies: AnswerCommitDependencies) -> None:
        self._deps = dependencies

    def commit_history_and_terminal(
        self,
        *,
        projection: AnswerCommitProjection,
        final_text: str,
        provider_phase: str,
        provider_items: list[dict[str, Any]],
        usage: UsageInfo,
        provider_raw: dict[str, Any],
    ) -> tuple[AgentEvent, ...]:
        deps = self._deps
        state = deps.state
        # This is the visible-answer commit boundary. Close current-turn
        # mailbox delivery first so late child results remain queued for the
        # next user turn instead of reopening this answer.
        defer_mailbox = getattr(deps.turn_kernel, "defer_mailbox_to_next_turn", None)
        if callable(defer_mailbox):
            defer_mailbox()
        if final_text:
            deps.append_assistant_history(
                deps.context,
                final_text,
                phase=provider_phase or "final_answer",
                provider_items=provider_items,
            )
            state.reply = (
                str(getattr(state, "max_output_partial_text", "") or "")
                + final_text
            )

        events: list[AgentEvent] = []
        terminal_reason = projection.terminal_reason
        deps.set_terminal_reason(
            state,
            terminal_reason,
            status=projection.terminal_status,
        )
        completed_event = deps.turn_kernel.complete_for_terminal_reason(
            state.stopped_reason
        )
        if completed_event is not None:
            events.append(completed_event)
        events.append(
            AgentEvent.done(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens,
                cache_deleted_input_tokens=usage.cache_deleted_input_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                input_includes_cache_read=usage.input_includes_cache_read,
                provider_raw=provider_raw,
                status=projection.terminal_status,
                reason="" if terminal_reason == "completed" else terminal_reason,
            )
        )
        return tuple(events)
