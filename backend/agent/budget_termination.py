"""One mechanical terminal protocol for turn resource boundaries.

Budget boundaries are runtime facts, not inferred intent.  Keeping the
transition here makes every time/iteration/tool/context limit produce the same
history, timeline and terminal record while leaving provider and tool
orchestration in the main loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from backend.agent.message import AgentEvent
from backend.agent.turn_budget import BudgetBoundary


@dataclass(frozen=True, slots=True)
class BudgetTerminationDependencies:
    state: Any
    context: Any
    set_terminal_reason: Callable[..., Any]
    run_stop_failure_hook: Callable[..., Awaitable[None]]
    terminal_event: Callable[[str, str], AgentEvent]


class BudgetTerminationCoordinator:
    """Apply a resource boundary without fabricating model-authored history."""

    def __init__(self, dependencies: BudgetTerminationDependencies) -> None:
        self._deps = dependencies

    async def apply(
        self,
        boundary: BudgetBoundary,
    ) -> tuple[bool, tuple[AgentEvent, ...]]:
        deps = self._deps
        state = deps.state
        events: list[AgentEvent] = []
        state.mark_transition(
            "budget_boundary",
            budget_reason=boundary.reason,
            detail=boundary.detail,
            observed=boundary.observed,
            limit=boundary.limit,
            post_tools=boundary.post_tools,
        )

        reconcile = getattr(deps.context, "reconcile_dangling_tool_calls", None)
        if callable(reconcile):
            reconcile()

        # max_retries means the provider kept erroring past the recovery budget —
        # a genuine failure, so it stays failed+error. Every other boundary
        # (max turns/tool-calls/tokens/cost/time/context) is a normal resource
        # limit: Claude Code returns {reason:'max_turns'} with an informational
        # attachment instead of erroring, so terminate as partial and surface the
        # limit as a progress notice.
        is_failure_boundary = boundary.reason == "max_retries"
        terminal_status = "failed" if is_failure_boundary else "partial"
        deps.set_terminal_reason(state, boundary.reason, status=terminal_status)
        if is_failure_boundary:
            events.append(
                AgentEvent.error(
                    message=boundary.user_message,
                    recoverable=True,
                    error_type="budget",
                )
            )
        else:
            events.append(
                AgentEvent.progress(
                    boundary.user_message,
                    stage="budget",
                    status="info",
                )
            )
        await deps.run_stop_failure_hook(
            boundary.reason,
            error_details=boundary.detail,
            last_assistant_message=state.reply,
        )

        events.append(deps.terminal_event(terminal_status, boundary.reason))
        return False, tuple(events)
