"""The hard iteration limit counts every admitted provider turn."""

from __future__ import annotations

from backend.agent.state import AgentState
from backend.agent.turn_budget import TurnBudgetController


def _controller(max_iterations: int = 5) -> TurnBudgetController:
    return TurnBudgetController(
        max_turn_seconds=0.0,
        max_iterations=max_iterations,
        max_tool_calls=0,
        max_retries=3,
        max_turn_tokens=0,
    )


def test_work_iterations_does_not_subtract_retry_telemetry() -> None:
    state = AgentState(user_message="fix the bug")
    state.iterations = 10
    state.recovery_iterations = 4

    assert state.work_iterations == 10


def test_work_iterations_never_goes_negative() -> None:
    state = AgentState(user_message="fix the bug")
    state.iterations = 2
    state.recovery_iterations = 5

    assert state.work_iterations == 2


def test_admitted_turns_exhaust_the_iteration_budget() -> None:
    controller = _controller(max_iterations=5)
    state = AgentState(user_message="fix the bug")

    state.iterations = 8
    state.recovery_iterations = 3

    boundary = controller.evaluate(
        elapsed_seconds=0.0,
        iterations=state.work_iterations,
        tool_calls=0,
    )
    assert boundary is not None
    assert boundary.reason == "max_iterations"

    state.iterations = 4
    boundary = controller.evaluate(
        elapsed_seconds=0.0,
        iterations=state.work_iterations,
        tool_calls=0,
    )
    assert boundary is None


def test_raw_iteration_counter_is_untouched() -> None:
    """Event ids, checkpoints and resume all key off ``iterations``."""
    state = AgentState(user_message="fix the bug")
    state.iterations = 9
    state.recovery_iterations = 4

    assert state.iterations == 9
