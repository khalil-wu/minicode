"""Small answer-acceptance policies with explicit loop actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.loop_preflight import hook_manager_has_hooks
from backend.agent.message import AgentEvent
from backend.agent.response_utils import append_assistant_history
from backend.hooks import get_hook_manager
from backend.hooks.manager import HookEvent


AcceptanceAction = Literal["accept", "retry", "terminate"]


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    action: AcceptanceAction
    events: tuple[AgentEvent, ...] = ()


async def apply_stop_hook_policy(
    *,
    user_message: str,
    candidate_text: str,
    state: Any,
    context_builder: Any,
    stream_text: Any,
    budget_runtime: Any,
    provider_phase: str,
    provider_items: list[dict[str, Any]],
    feedback_limit: int,
) -> AcceptanceResult:
    hook_manager = get_hook_manager()
    if not hook_manager or not hook_manager_has_hooks(hook_manager, HookEvent.STOP):
        return AcceptanceResult("accept")
    hook_result = await hook_manager.run_stop(
        user_message,
        candidate_text,
        tool_results=state.tool_calls,
    )
    notice_events = (
        (AgentEvent(type="system_notice", data={"content": hook_result.system_message}),)
        if hook_result.system_message
        else ()
    )
    # CC's common `continue:false` means the Stop hook explicitly prevents
    # another model continuation. The candidate answer is accepted as the
    # terminal response; it is not fed back as another retry prompt.
    if hook_result.prevent_continuation:
        return AcceptanceResult("accept", notice_events)
    if not hook_result.has_feedback or state.stop_hook_feedback_count >= feedback_limit:
        return AcceptanceResult("accept", notice_events)

    boundary = budget_runtime.consume_retry("stop_hook_feedback")
    if boundary is not None:
        _, events = await budget_runtime.apply_boundary(boundary)
        return AcceptanceResult(
            "terminate",
            tuple(events),
        )
    state.stop_hook_feedback_count += 1
    state.mark_transition(
        "stop_hook_feedback",
        attempt=state.stop_hook_feedback_count,
    )
    completed = stream_text.cancel_active_agent_message()
    events = notice_events + ((completed,) if completed is not None else ())
    append_assistant_history(
        context_builder,
        candidate_text,
        phase=provider_phase or "final_answer",
        provider_items=provider_items,
    )
    context_builder.append_user(hook_result.feedback)
    stream_text.reset_for_retry()
    return AcceptanceResult("retry", events)
