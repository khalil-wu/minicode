"""Terminal handling for provider output-length truncation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import usage_done_event
from backend.agent.response_utils import append_assistant_history
from backend.agent.turn_kernel import _set_terminal_reason


RecoveryAction = Literal["retry", "terminate"]
_MAX_OUTPUT_RECOVERY_LIMIT = 3
_CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly - no apology, no recap of what you were doing. "
    "Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces."
)
_MAX_OUTPUT_ERROR = (
    "The provider stopped because the output limit was reached and continuation "
    "recovery was exhausted."
)


@dataclass(frozen=True, slots=True)
class MaxOutputRecoveryResult:
    action: RecoveryAction
    events: tuple[AgentEvent, ...]


async def recover_max_output(
    *,
    state: Any,
    stream_text: Any,
    tool_executor: Any,
    context_builder: Any,
    budget_runtime: Any,
    provider_items: list[dict[str, Any]],
    turn_usage: Any,
    finish_reason: str,
    scrub_text: Any,
    run_stop_failure_hook: Callable[..., Awaitable[None]],
) -> MaxOutputRecoveryResult:
    """Continue a truncated answer with CC's bounded multi-turn recovery."""

    tool_executor.cancel_remaining()
    partial_text = stream_text.accepted_answer_text(scrub_text)
    events: list[AgentEvent] = []
    if partial_text.strip():
        state.max_output_partial_text = (
            str(getattr(state, "max_output_partial_text", "") or "") + partial_text
        )
        started = stream_text.start_agent_message()
        if started is not None:
            events.append(started)
        completed = stream_text.complete_active_agent_message(
            partial_text,
            source="partial",
            status="partial",
            finish_reason=finish_reason,
        )
        if completed is not None:
            events.append(completed)

    recovery_count = int(getattr(state, "max_output_recovery_count", 0) or 0)
    if recovery_count < _MAX_OUTPUT_RECOVERY_LIMIT:
        boundary = budget_runtime.consume_retry("max_output_tokens_recovery")
        if boundary is None:
            replay_items = [
                item
                for item in provider_items
                if str(item.get("type") or "") not in {"function_call", "tool_call", "tool_use"}
            ]
            append_assistant_history(
                context_builder,
                partial_text,
                phase="final_answer",
                provider_items=replay_items,
            )
            context_builder.append_user(_CONTINUATION_PROMPT)
            state.max_output_recovery_count = recovery_count + 1
            state.mark_transition(
                "max_output_tokens_recovery",
                attempt=state.max_output_recovery_count,
            )
            stream_text.reset_for_retry()
            return MaxOutputRecoveryResult("retry", tuple(events))

    accumulated_partial = str(
        getattr(state, "max_output_partial_text", "") or ""
    )
    if accumulated_partial.strip():
        state.reply = accumulated_partial
    await run_stop_failure_hook(
        "max_output_tokens",
        error_details=_MAX_OUTPUT_ERROR,
        last_assistant_message=accumulated_partial,
    )
    events.append(
        AgentEvent.error(
            message=_MAX_OUTPUT_ERROR,
            recoverable=True,
            error_type="max_output",
        )
    )
    status = "partial" if accumulated_partial.strip() else "failed"
    _set_terminal_reason(state, "max_output", status=status)
    events.append(usage_done_event(turn_usage, status=status, reason="max_output"))
    return MaxOutputRecoveryResult("terminate", tuple(events))
