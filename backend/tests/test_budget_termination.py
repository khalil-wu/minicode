from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.budget_termination import (
    BudgetTerminationCoordinator,
    BudgetTerminationDependencies,
)
from backend.agent.message import AgentEvent
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.turn_budget import BudgetBoundary, TurnBudgetController
from backend.llm.base import UsageInfo


class _Context:
    def __init__(self) -> None:
        self.reconciled = 0

    def reconcile_dangling_tool_calls(self) -> None:
        self.reconciled += 1


def _coordinator(state, context):
    async def stop_failure(*args, **kwargs) -> None:
        state.stop_failure = (args, kwargs)

    return BudgetTerminationCoordinator(
        BudgetTerminationDependencies(
            state=state,
            context=context,
            set_terminal_reason=lambda current, reason, *, status: setattr(
                current, "terminal", (reason, status)
            ),
            run_stop_failure_hook=stop_failure,
            terminal_projection=lambda status, reason: TurnTerminalProjection.from_usage(
                UsageInfo(),
                status=status,
                reason=reason,
            ),
        )
    )


def test_context_budget_boundary_is_a_failure_without_fabricating_answer() -> None:
    state = SimpleNamespace(iterations=3, tool_calls=["tool-1"], reply="", transitions=[])
    state.mark_transition = lambda *args, **kwargs: state.transitions.append((args, kwargs))
    context = _Context()
    coordinator = _coordinator(state, context)

    boundary = TurnBudgetController.context_exhausted()
    should_continue, events = asyncio.run(
        coordinator.apply(boundary)
    )
    public_events = [event for event in events if isinstance(event, AgentEvent)]

    assert should_continue is False
    assert context.reconciled == 1
    assert state.terminal == ("budget_exceeded", "failed")
    assert not any(event.type == "item.completed" for event in public_events)
    assert any(event.type == "error" for event in public_events)
    assert not any(event.type == "agent.progress" for event in public_events)
    assert state.stop_failure[0] == ("budget_exceeded",)
    terminal = [event for event in events if isinstance(event, TurnTerminalProjection)]
    assert len(terminal) == 1
    assert terminal[0].status == "failed"
    assert terminal[0].reason == "budget_exceeded"


def test_claude_code_max_turn_and_cost_boundaries_are_errors() -> None:
    for reason in ("max_iterations", "max_turn_cost_usd"):
        state = SimpleNamespace(tool_calls=[], reply="", transitions=[])
        state.mark_transition = lambda *args, **kwargs: state.transitions.append(
            (args, kwargs)
        )
        coordinator = _coordinator(state, _Context())
        boundary = BudgetBoundary(
            reason=reason,
            limit=2,
            observed=2,
            detail="limit reached",
        )

        _, events = asyncio.run(coordinator.apply(boundary))
        public_events = [event for event in events if isinstance(event, AgentEvent)]

        assert state.terminal == (reason, "failed")
        assert any(event.type == "error" for event in public_events)
        assert not hasattr(state, "stop_failure")


def test_host_tool_limit_remains_a_partial_result() -> None:
    state = SimpleNamespace(tool_calls=[], reply="", transitions=[])
    state.mark_transition = lambda *args, **kwargs: state.transitions.append(
        (args, kwargs)
    )
    coordinator = _coordinator(state, _Context())
    boundary = BudgetBoundary(
        reason="max_tool_calls",
        limit=5,
        observed=5,
        detail="host tool limit reached",
    )

    _, events = asyncio.run(coordinator.apply(boundary))
    public_events = [event for event in events if isinstance(event, AgentEvent)]

    assert state.terminal == ("max_tool_calls", "partial")
    progress = [event for event in public_events if event.type == "agent.progress"]
    assert len(progress) == 1
    assert progress[0].data == {
        "id": "budget:max_tool_calls",
        "stage": "status",
        "status": "info",
        "message": boundary.user_message,
        "phase": "status",
        "summary": "host tool limit reached",
        "visibility": "timeline",
        "label": boundary.label,
    }
    assert not any(event.type == "error" for event in public_events)
    assert not hasattr(state, "stop_failure")
