"""Complete a model-to-tool transition and apply the post-tool budget fence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.loop_process_events import model_process_text_event
from backend.agent.message import AgentEvent
from backend.agent.tool_transition import (
    ToolTransitionController,
    project_tool_transition,
)


ToolTurnAction = Literal["proceed", "retry", "terminate"]


@dataclass(frozen=True, slots=True)
class ToolTurnResult:
    action: ToolTurnAction
    tool_batch_count: int
    tool_call_count: int


async def execute_tool_turn(
    *,
    pending_tool_calls: list[Any],
    provider_phase: str,
    provider_items: list[dict[str, Any]],
    tool_batch_count: int,
    turn_start_tool_call_count: int,
    state: Any,
    context_builder: Any,
    stream_state: Any,
    stream_text: Any,
    tool_tracker: Any,
    tool_registry: Any,
    permission_checker: Any,
    approval_handler: Any,
    skill_manager: Any,
    tool_context: Any,
    turn_kernel: Any,
    settings: Any,
    turn_budget_controller: Any,
    budget_runtime: Any,
    deadline_controller: Any,
    record_tool_call: Any,
) -> AsyncIterator[AgentEvent | ToolTurnResult]:
    """Execute one ordered tool batch and return the loop-control decision."""

    process_event = stream_text.flush_pending_process_text(
        pending_tool_calls,
        source=stream_text.process_text_source,
        event_factory=model_process_text_event,
    )
    if process_event is not None:
        yield process_event
    transition_controller = ToolTransitionController(
        ctx=context_builder,
        state=state,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        approval_handler=approval_handler,
        skill_manager=skill_manager,
        tool_context=tool_context,
        refresh_permission=turn_kernel.refresh_live_permission_context,
        clear_stream_tracking=tool_tracker.cancel_remaining,
        record_tool_call=record_tool_call,
    )
    transition_execution = transition_controller.start(
        tool_calls=pending_tool_calls,
        content=stream_text.full_text,
        phase=provider_phase or "commentary",
        provider_items=provider_items,
        replace_tool_calls=stream_state.replace_tool_calls,
        execution_limit=(
            max(
                0,
                settings.max_tool_calls
                - (len(state.tool_calls) - turn_start_tool_call_count),
            )
            if settings.max_tool_calls > 0
            else None
        ),
    )
    for tool_call in transition_execution.prepared.tool_calls:
        tool_tracker.mark_yielded(tool_call.id)
    tool_batch_count += 1
    tool_call_count = transition_execution.call_count
    async for event in project_tool_transition(
        transition_execution,
        cancel_remaining=tool_tracker.cancel_remaining,
    ):
        yield event
    boundary = turn_budget_controller.evaluate(
        elapsed_seconds=deadline_controller.elapsed(),
        iterations=state.work_iterations,
        tool_calls=len(state.tool_calls) - turn_start_tool_call_count,
        tokens=budget_runtime.local_tokens_used(),
        cost_usd=budget_runtime.turn_cost_usd(),
        post_tools=True,
    )
    if boundary is None:
        boundary = budget_runtime.rollout_boundary(post_tools=True)
    if boundary is not None:
        _, events = await budget_runtime.apply_boundary(boundary)
        for event in events:
            yield event
        yield ToolTurnResult(
            "terminate",
            tool_batch_count,
            tool_call_count,
        )
        return

    state.mark_transition(
        "next_turn",
        tool_call_count=tool_call_count,
        total_tool_calls=len(state.tool_calls),
    )
    yield ToolTurnResult(
        "proceed",
        tool_batch_count,
        tool_call_count,
    )
