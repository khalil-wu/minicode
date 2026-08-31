"""The turn token budget must measure cost, not resent traffic.

An agent turn resends its whole context on every provider request, so summing
``input_tokens`` across iterations counts the same prompt over and over. A 50k
context over 60 iterations reads as 3M tokens while the real context never grew
past 50k, which makes any ceiling expressed in tokens meaningless.
"""

from __future__ import annotations

from backend.agent.turn_budget import TurnBudgetController
from backend.llm.base import UsageInfo


def test_billable_tokens_excludes_the_cached_prefix() -> None:
    # Shape taken from a real long agent turn: most of the input on each
    # request is the cached prefix being resent.
    usage = UsageInfo(
        input_tokens=3_342_568,
        output_tokens=95_437,
        cache_read_input_tokens=1_987_456,
    )

    assert usage.total_tokens == 3_438_005
    assert usage.billable_tokens == 3_342_568 - 1_987_456 + 95_437


def test_billable_tokens_without_cache_reporting_matches_total() -> None:
    usage = UsageInfo(input_tokens=1_000, output_tokens=250)

    assert usage.billable_tokens == usage.total_tokens == 1_250


def test_billable_tokens_never_goes_negative_on_odd_provider_reports() -> None:
    # Some gateways report a cache read larger than the prompt they billed.
    usage = UsageInfo(input_tokens=500, output_tokens=100, cache_read_input_tokens=900)

    assert usage.billable_tokens == 100


def test_direct_cost_boundaries_are_reported_before_proxy_ones() -> None:
    """When several budgets are exhausted, the reported reason must be the real one."""
    controller = TurnBudgetController(
        max_turn_seconds=0.0,
        max_iterations=5,
        max_tool_calls=10,
        max_retries=0,
        max_turn_tokens=1_000,
    )

    boundary = controller.evaluate(
        elapsed_seconds=0.0,
        iterations=99,
        tool_calls=99,
        tokens=5_000,
    )

    assert boundary is not None
    assert boundary.reason == "max_turn_tokens", (
        "an exhausted token budget explains the stop better than the iteration proxy"
    )


def test_wall_clock_outranks_every_other_boundary() -> None:
    controller = TurnBudgetController(
        max_turn_seconds=10.0,
        max_iterations=5,
        max_tool_calls=10,
        max_retries=0,
        max_turn_tokens=1_000,
    )

    boundary = controller.evaluate(
        elapsed_seconds=99.0,
        iterations=99,
        tool_calls=99,
        tokens=5_000,
    )

    assert boundary is not None
    assert boundary.reason == "max_turn_seconds"


def test_iteration_proxy_still_reported_when_it_is_the_only_boundary() -> None:
    controller = TurnBudgetController(
        max_turn_seconds=0.0,
        max_iterations=5,
        max_tool_calls=0,
        max_retries=0,
        max_turn_tokens=1_000_000,
    )

    boundary = controller.evaluate(
        elapsed_seconds=0.0,
        iterations=5,
        tool_calls=0,
        tokens=1_000,
    )

    assert boundary is not None
    assert boundary.reason == "max_iterations"
