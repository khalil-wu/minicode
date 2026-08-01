"""Final-answer recovery decisions that may redirect or terminate a turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import usage_done_event
from backend.agent.response_utils import append_assistant_history
from backend.agent.turn_kernel import _set_terminal_reason


AnswerRecoveryAction = Literal["accept", "retry", "terminate"]


@dataclass(frozen=True, slots=True)
class AnswerRecoveryResult:
    action: AnswerRecoveryAction
    events: tuple[AgentEvent, ...] = ()


async def recover_empty_answer(
    *,
    state: Any,
    stream_text: Any,
    turn_usage: Any,
    finish_reason: str = "",
) -> AnswerRecoveryResult:
    """Reject an empty provider answer without fabricating assistant text."""

    if stream_text.final_candidate_text.strip():
        return AnswerRecoveryResult("accept")

    # A refusal (Anthropic stop_reason="refusal") is an empty reply with a
    # specific cause: the model declined on policy grounds. "Please retry" is
    # the wrong advice, so surface a dedicated message like Claude Code
    # (getErrorMessageIfRefusal) instead of the generic empty-reply text.
    if str(finish_reason or "").strip().lower() == "refusal":
        state.mark_transition("refusal")
        events = [
            AgentEvent.error(
                message=(
                    "The model declined to respond to this request, which may "
                    "violate the Anthropic Usage Policy "
                    "(https://www.anthropic.com/legal/aup). Edit your last "
                    "message or start a new session for a different task."
                ),
                recoverable=False,
                error_type="refusal",
            )
        ]
        _set_terminal_reason(state, "refusal", status="failed")
        events.append(usage_done_event(turn_usage, status="failed"))
        return AnswerRecoveryResult("terminate", tuple(events))

    state.mark_transition("empty_reply")
    events = [
        AgentEvent.error(
            message="模型返回了空回复，未能生成答案。请重试。",
            recoverable=True,
            error_type="empty_reply",
        )
    ]
    _set_terminal_reason(state, "empty_reply", status="failed")
    events.append(usage_done_event(turn_usage, status="failed"))
    return AnswerRecoveryResult("terminate", tuple(events))


async def accept_completed_stream_steer(
    *,
    state: Any,
    context_builder: Any,
    stream_text: Any,
    turn_kernel: Any,
    candidate_text: str,
    provider_phase: str,
    provider_items: list[dict[str, Any]],
) -> AnswerRecoveryResult:
    """Redirect an otherwise complete answer when a steer is waiting."""

    queued_steer = turn_kernel.pop_turn_steer()
    if queued_steer is None:
        return AnswerRecoveryResult("accept")

    events: list[AgentEvent] = []
    if candidate_text.strip():
        # The answer is complete, so commit it (model_final/completed) so it
        # stays visible and persisted, then start a fresh turn for the steer.
        # Claude Code emits turn_end for the completed message before injecting
        # pending messages — it never retracts a completed answer as "cancelled".
        started = stream_text.start_agent_message()
        if started is not None:
            events.append(started)
        completed = stream_text.complete_active_agent_message(
            candidate_text,
            source="model_final",
            status="completed",
        )
        if completed is not None:
            events.append(completed)
        append_assistant_history(
            context_builder,
            candidate_text,
            phase=provider_phase or "final_answer",
            provider_items=provider_items,
        )
    await turn_kernel.accept_turn_steer(queued_steer)
    state.mark_transition(
        "user_steer",
        message_id=queued_steer.message_id,
        user_message_id=queued_steer.user_message_id,
    )
    stream_text.reset_for_retry()
    return AnswerRecoveryResult("retry", tuple(events))
