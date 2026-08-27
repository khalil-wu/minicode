"""Final-answer acceptance orchestration with explicit loop-control outcomes.

The agent loop owns iteration and termination. This module owns the policy
pipeline that runs once a provider response contains no tool calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.answer_acceptance import apply_stop_hook_policy
from backend.agent.answer_recovery import (
    accept_completed_stream_steer,
    recover_empty_answer,
)
from backend.agent.final_answer_runtime import commit_accepted_final_answer
from backend.agent.loop_process_events import model_process_text_event
from backend.agent.message import AgentEvent
from backend.agent.terminal_projection import TurnTerminalProjection


FinalAnswerAction = Literal["retry", "terminate"]


@dataclass(frozen=True, slots=True)
class FinalAnswerOutcome:
    action: FinalAnswerAction
    degraded_reason: str = ""


async def orchestrate_final_answer(
    *,
    user_message: str,
    state: Any,
    context_builder: Any,
    stream_text: Any,
    turn_kernel: Any,
    turn_usage: Any,
    provider_phase: str,
    provider_items: list[dict[str, Any]],
    runtime: Any,
    run_record: Any,
    answer_committer: Any,
    finish_reason: str,
    provider_raw_final_text: dict[str, Any],
    provider_raw_done: dict[str, Any],
    degraded_reason: str,
) -> AsyncIterator[AgentEvent | TurnTerminalProjection | FinalAnswerOutcome]:
    """Run the no-tools acceptance pipeline and return an explicit loop action."""

    if stream_text.saw_final_answer_phase:
        process_event = stream_text.flush_pending_process_text(
            None,
            source=None,
            event_factory=model_process_text_event,
        )
        if process_event is not None:
            yield process_event
    else:
        stream_text.accept_unphased_answer()
        if stream_text.pending_process_text:
            process_event = stream_text.flush_pending_process_text(
                None,
                source=None,
                event_factory=model_process_text_event,
            )
            if process_event is not None:
                yield process_event

    empty_recovery = await recover_empty_answer(
        state=state,
        stream_text=stream_text,
        turn_usage=turn_usage,
        finish_reason=finish_reason,
        provider_raw_done=provider_raw_done,
    )
    for event in empty_recovery.events:
        yield event
    if empty_recovery.action in {"retry", "terminate"}:
        yield FinalAnswerOutcome(empty_recovery.action, degraded_reason)
        return

    candidate_text = stream_text.final_candidate_text

    completed_steer = await accept_completed_stream_steer(
        state=state,
        context_builder=context_builder,
        stream_text=stream_text,
        turn_kernel=turn_kernel,
        candidate_text=candidate_text,
        provider_phase=provider_phase,
        provider_items=provider_items,
    )
    for event in completed_steer.events:
        yield event
    if completed_steer.action == "retry":
        yield FinalAnswerOutcome("retry", degraded_reason)
        return

    stop_hook_acceptance = await apply_stop_hook_policy(
        user_message=user_message,
        candidate_text=candidate_text,
        state=state,
        context_builder=context_builder,
        stream_text=stream_text,
        provider_phase=provider_phase,
        provider_items=provider_items,
    )
    for event in stop_hook_acceptance.events:
        yield event
    if stop_hook_acceptance.action in {"retry", "terminate"}:
        yield FinalAnswerOutcome(stop_hook_acceptance.action, degraded_reason)
        return

    async for event in commit_accepted_final_answer(
        candidate_text=candidate_text,
        state=state,
        stream_text=stream_text,
        runtime=runtime,
        run_record=run_record,
        answer_committer=answer_committer,
        degraded_reason=degraded_reason,
        finish_reason=finish_reason,
        provider_raw_final_text=provider_raw_final_text,
        provider_raw_done=provider_raw_done,
        provider_phase=provider_phase,
        provider_items=provider_items,
        usage=turn_usage,
    ):
        yield event
    yield FinalAnswerOutcome("terminate", degraded_reason)
