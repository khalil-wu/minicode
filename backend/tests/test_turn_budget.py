from __future__ import annotations

from backend.agent.turn_budget import TurnBudgetController
from backend.config import AgentSettings


def _controller() -> TurnBudgetController:
    return TurnBudgetController.from_settings(
        AgentSettings(
            max_turn_seconds=30,
            max_iterations=8,
            max_tool_calls=12,
            turn_error_budget=3,
            max_turn_tokens=500,
        ),
        max_iterations=8,
    )


def test_turn_budget_uses_one_precedence_order_in_every_phase() -> None:
    controller = _controller()

    boundary = controller.evaluate(
        elapsed_seconds=31,
        iterations=8,
        tool_calls=12,
        post_tools=True,
    )

    assert boundary is not None
    assert boundary.reason == "max_turn_seconds"
    assert boundary.post_tools is True


def test_turn_budget_checks_iterations_and_tools_without_task_heuristics() -> None:
    controller = _controller()

    iteration = controller.evaluate(elapsed_seconds=1, iterations=8, tool_calls=0)
    tools = controller.evaluate(elapsed_seconds=1, iterations=2, tool_calls=12)

    assert iteration is not None and iteration.reason == "max_iterations"
    assert tools is not None and tools.reason == "max_tool_calls"
    assert controller.evaluate(elapsed_seconds=1, iterations=2, tool_calls=2) is None


def test_context_exhaustion_uses_the_same_boundary_contract() -> None:
    boundary = TurnBudgetController.context_exhausted()

    assert boundary.reason == "budget_exceeded"
    assert "上下文预算" in boundary.user_message


def test_turn_budget_accounts_for_retries_and_cumulative_tokens() -> None:
    controller = _controller()

    retry = controller.evaluate(
        elapsed_seconds=1,
        iterations=1,
        tool_calls=0,
        retries=3,
    )
    tokens = controller.evaluate(
        elapsed_seconds=1,
        iterations=1,
        tool_calls=0,
        retries=0,
        tokens=500,
    )

    assert retry is not None and retry.reason == "max_retries"
    assert tokens is not None and tokens.reason == "max_turn_tokens"
