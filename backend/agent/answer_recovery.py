"""Final-answer recovery decisions that may redirect or terminate a turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import usage_terminal_projection
from backend.agent.response_utils import (
    append_assistant_history,
    provider_items_for_final_answer,
)
from backend.agent.turn_kernel import _set_terminal_reason
from backend.agent.mailbox_delivery import inject_parent_notifications, inject_subagent_mailbox_updates
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
    has_non_text_result: bool = False,
) -> AnswerRecoveryResult:
    """Reject an empty provider answer without fabricating assistant text."""

    if stream_text.final_candidate_text.strip() or has_non_text_result:
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

    events = _retain_completed_answer(
        context_builder=context_builder, stream_text=stream_text,
        candidate_text=candidate_text, provider_phase=provider_phase,
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


def _retain_completed_answer(*, context_builder, stream_text, candidate_text,
                             provider_phase, provider_items) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    if candidate_text.strip():
        # Seal the completed model item before appending pending user or
        # coordination input. The current turn continues from both records.
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
            provider_items=provider_items_for_final_answer(provider_items),
        )
    return events


async def accept_completed_coordination_input(
    *, state, context_builder, stream_text, turn_kernel, candidate_text,
    provider_phase, provider_items,
) -> AnswerRecoveryResult:
    """Consume pending child/mailbox input before accepting turn completion.

    Preserve the completed model item before the first new input is appended,
    then request another sampling step. Notifications are acknowledged only
    after that next request consumes them, just like iteration admission.
    """
    events: list[AgentEvent] = []
    retained = False

    def retain_answer() -> None:
        nonlocal retained
        if retained:
            return
        events.extend(_retain_completed_answer(
            context_builder=context_builder, stream_text=stream_text,
            candidate_text=candidate_text, provider_phase=provider_phase,
            provider_items=provider_items,
        ))
        retained = True

    mailbox_count = await inject_subagent_mailbox_updates(
        ctx=context_builder, state=state, metadata=turn_kernel.metadata,
        conversation_id=turn_kernel.run_record.conversation_id,
        emit_event=turn_kernel.emit_event, run_context=turn_kernel.run_context,
        before_inject=retain_answer,
    )
    notification_count = await inject_parent_notifications(
        ctx=context_builder, state=state, metadata=turn_kernel.metadata,
        runtime=turn_kernel.runtime, run_context=turn_kernel.run_context,
        parent_run_id=turn_kernel.run_record.run_id,
        conversation_id=turn_kernel.run_record.conversation_id,
        emit_event=turn_kernel.emit_event, before_inject=retain_answer,
    )
    if not mailbox_count and not notification_count:
        return AnswerRecoveryResult("accept")
    state.mark_transition("coordination_follow_up", mailbox_count=mailbox_count,
        notification_count=notification_count)
    stream_text.reset_for_retry()
    return AnswerRecoveryResult("retry", tuple(events))
