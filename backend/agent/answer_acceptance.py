"""Small answer-acceptance policies with explicit loop actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.loop_preflight import hook_manager_has_hooks
from backend.agent.message import AgentEvent
from backend.agent.response_utils import append_assistant_history
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
    provider_phase: str,
    provider_items: list[dict[str, Any]],
) -> AcceptanceResult:
    hook_manager = context_builder.hook_manager
    prompt_context = (
        state.prompt_context if isinstance(state.prompt_context, dict) else {}
    )
    subagent_id = str(prompt_context.get("subagent_id") or "").strip()
    agent_type = str(prompt_context.get("subagent") or "").strip()
    hook_event = HookEvent.SUBAGENT_STOP if subagent_id else HookEvent.STOP
    if not hook_manager or not hook_manager_has_hooks(hook_manager, hook_event):
        return AcceptanceResult("accept")
    if subagent_id:
        prompt_context.pop("subagent_stop_prevented_continuation", None)
        hook_result = await hook_manager.run_subagent_stop(
            subagent_id=subagent_id,
            agent_type=agent_type,
            status="completed",
            summary=candidate_text,
        )
    else:
        hook_result = await hook_manager.run_stop(
            user_message,
            candidate_text,
            tool_results=state.tool_calls,
            stop_hook_active=state.stop_hook_feedback_count > 0,
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
        if subagent_id:
            prompt_context["subagent_stop_prevented_continuation"] = True
        return AcceptanceResult("accept", notice_events)
    feedback = str(hook_result.feedback or hook_result.message or "").strip()
    if not hook_result.blocked and not feedback:
        return AcceptanceResult("accept", notice_events)

    # A blocking Stop hook starts another model turn in Claude Code. It is not
    # a provider failure and therefore must not consume the API retry budget.
    # Explicit turn limits, when configured, remain the loop's outer guard.
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
    context_builder.append_user(feedback)
    stream_text.reset_for_retry()
    return AcceptanceResult("retry", events)
