"""Final-answer recovery decisions that may redirect or terminate a turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import usage_terminal_projection
from backend.agent.response_utils import append_assistant_history
from backend.agent.turn_kernel import _set_terminal_reason
from backend.agent.terminal_projection import TurnTerminalProjection


AnswerRecoveryAction = Literal["accept", "retry", "terminate"]


@dataclass(frozen=True, slots=True)
class AnswerRecoveryResult:
    action: AnswerRecoveryAction
    events: tuple[AgentEvent | TurnTerminalProjection, ...] = ()


async def recover_empty_answer(
    *,
    state: Any,
    stream_text: Any,
    turn_usage: Any,
    finish_reason: str = "",
    provider_raw_done: dict[str, Any] | None = None,
) -> AnswerRecoveryResult:
    """Reject an empty provider answer without fabricating assistant text."""

    if stream_text.final_candidate_text.strip():
        return AnswerRecoveryResult("accept")

    # A provider refusal is an empty reply with a specific cause: the model
    # declined on policy grounds. "Please retry" is
    # the wrong advice, so surface a dedicated refusal message instead of the
    # generic empty-reply text.
    if str(finish_reason or "").strip().lower() == "refusal":
        raw_refusal = (
            provider_raw_done.get("refusal")
            if isinstance(provider_raw_done, dict)
            and isinstance(provider_raw_done.get("refusal"), dict)
            else {}
        )
        explanation = " ".join(
            str(raw_refusal.get("explanation") or "").split()
        )[:4_096]
        category = str(raw_refusal.get("category") or "").strip().lower()[:80]
        refusal_message = (
            f"The model declined to respond. Provider explanation: {explanation}"
            if explanation
            else (
                "The provider declined to respond to this request. Edit your last "
                "message or start a new session for a different task."
            )
        )
        state.mark_transition("refusal")
        events: list[AgentEvent | TurnTerminalProjection] = [
            AgentEvent.error(
                message=refusal_message,
                recoverable=False,
                error_type="refusal",
                error_code=(
                    f"provider_refusal_{category}"
                    if category
                    else "provider_refusal"
                ),
                provider_error_type="refusal",
            )
        ]
        _set_terminal_reason(state, "refusal", status="failed")
        events.append(usage_terminal_projection(turn_usage, status="failed"))
        return AnswerRecoveryResult("terminate", tuple(events))

    state.mark_transition("empty_reply")
    events: list[AgentEvent | TurnTerminalProjection] = [
        AgentEvent.error(
            message="模型返回了空回复，未能生成答案。请重试。",
            recoverable=True,
            error_type="empty_reply",
        )
    ]
    _set_terminal_reason(state, "empty_reply", status="failed")
    events.append(usage_terminal_projection(turn_usage, status="failed"))
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
        # The completed message is sealed before pending messages are injected;
        # a completed answer is never retracted as "cancelled".
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
