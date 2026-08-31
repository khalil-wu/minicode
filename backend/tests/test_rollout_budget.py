from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.provider_stream_settlement import (
    ProviderStreamSettlement,
    settle_provider_stream,
)
from backend.agent.rollout_budget import RolloutBudget
from backend.agent.turn_budget_runtime import TurnBudgetRuntime
from backend.agent.turn_budget import TurnBudgetController
from backend.llm.base import UsageInfo


def test_rollout_budget_records_contributor_totals_idempotently() -> None:
    budget = RolloutBudget()

    budget.record_total("child-a", 120)
    budget.record_total("child-a", 160)
    budget.record_total("child-a", 90)
    budget.record_total("child-b", 40)

    assert budget.tokens_used() == 200


def test_rollout_runtime_does_not_count_live_usage_twice() -> None:
    budget = RolloutBudget()
    budget.record_total("child-a", 120)
    budget.record_total("child-b", 40)
    runtime = object.__new__(TurnBudgetRuntime)
    runtime.tool_context = SimpleNamespace(
        metadata={"agent_mode": "subagent", "run_id": "child-a"},
        task_id="child-a",
    )
    runtime.usage = lambda: UsageInfo(input_tokens=80, output_tokens=40)
    runtime.rollout_budget = budget

    assert runtime.rollout_tokens_used() == 160


def _runtime(
    budget: RolloutBudget,
    *,
    run_id: str,
    usage: UsageInfo,
    agent_path: str = "",
    mailbox_epoch: int = 0,
    reservation_id: str = "",
) -> TurnBudgetRuntime:
    runtime = object.__new__(TurnBudgetRuntime)
    runtime.tool_context = SimpleNamespace(
        metadata={
            "run_id": run_id,
            "agent_path": agent_path,
            "mailbox_epoch": mailbox_epoch,
            "_rollout_reservation_id": reservation_id,
        },
        task_id=run_id,
    )
    runtime.usage = lambda: usage
    runtime.rollout_budget = budget
    return runtime


def test_parent_admission_counts_outstanding_child_reservations() -> None:
    budget = RolloutBudget(token_limit=1_000)
    assert budget.reserve("child-a", 400) == 400
    assert budget.reserve("child-b", 400) == 400
    parent = _runtime(
        budget,
        run_id="root",
        usage=UsageInfo(input_tokens=150, output_tokens=50),
    )

    boundary = parent.rollout_boundary()

    assert boundary is not None
    assert boundary.limit == 1_000
    assert boundary.observed == 1_000
    assert "800 reserved" in boundary.detail


def test_child_admission_uses_local_quota_and_excludes_own_reservation() -> None:
    budget = RolloutBudget(token_limit=1_000)
    assert budget.reserve("child-a", 400) == 400
    assert budget.reserve("child-b", 400) == 400
    child_usage = UsageInfo(input_tokens=100, output_tokens=50)
    child = _runtime(
        budget,
        run_id="child-a",
        usage=child_usage,
        agent_path="main/root/child-a",
        mailbox_epoch=3,
        reservation_id="child-a",
    )

    assert child.local_tokens_used() == 150
    assert child.rollout_boundary() is None
    local_controller = TurnBudgetController(
        max_turn_seconds=0,
        max_iterations=0,
        max_tool_calls=0,
        max_turn_tokens=400,
    )
    assert local_controller.evaluate(
        elapsed_seconds=0,
        iterations=0,
        tool_calls=0,
        tokens=child.local_tokens_used(),
    ) is None
    snapshot = budget.snapshot(excluding_reservation="child-a")
    assert snapshot.tokens_used == 150
    assert snapshot.reserved_tokens == 400
    assert snapshot.available_tokens == 450


def test_reused_subagent_id_records_each_incarnation_once() -> None:
    budget = RolloutBudget(token_limit=1_000)
    first = _runtime(
        budget,
        run_id="child-a",
        usage=UsageInfo(input_tokens=80, output_tokens=20),
        agent_path="main/root/child-a",
        mailbox_epoch=1,
    )
    second = _runtime(
        budget,
        run_id="child-a",
        usage=UsageInfo(input_tokens=40, output_tokens=10),
        agent_path="main/root/child-a",
        mailbox_epoch=2,
    )

    assert first.local_tokens_used() == 100
    assert second.local_tokens_used() == 50
    assert budget.tokens_used() == 150


def test_incarnation_usage_consumes_pre_registration_reservation() -> None:
    budget = RolloutBudget(token_limit=500)
    assert budget.reserve("child-a", 300) == 300
    child = _runtime(
        budget,
        run_id="child-a",
        usage=UsageInfo(input_tokens=75, output_tokens=25),
        agent_path="main/root/child-a",
        mailbox_epoch=4,
        reservation_id="child-a",
    )

    child.record_provider_usage_total(UsageInfo(input_tokens=75, output_tokens=25))

    snapshot = budget.snapshot()
    assert snapshot.tokens_used == 100
    assert snapshot.reserved_tokens == 200
    assert snapshot.available_tokens == 200


def test_retried_provider_usage_still_counts_toward_rollout() -> None:
    budget = RolloutBudget()

    class _BudgetRuntime:
        async def apply_boundary(self, _boundary):
            return False, ()

        def record_provider_usage_total(self, usage: UsageInfo) -> None:
            budget.record_usage_total("root-run", usage)

    class _TurnKernel:
        async def close_provider_attempt(self, *_args, **_kwargs) -> None:
            return None

    async def run() -> ProviderStreamSettlement:
        updates = [
            update
            async for update in settle_provider_stream(
                retry_budget_boundary=None,
                budget_runtime=_BudgetRuntime(),
                turn_kernel=_TurnKernel(),
                provider_attempt=object(),
                finish_reason="stop",
                provider_stream_steered=True,
                rebuild_context_and_retry=False,
                state=SimpleNamespace(stopped_reason="", iterations=1, recovery_iterations=0),
                pending_tool_calls=[],
                provider_raw_done={},
                provider_done=True,
                stream_state=SimpleNamespace(finish_reason=""),
                stream_text=SimpleNamespace(sanitize=lambda _scrub: None),
                context_builder=SimpleNamespace(),
                usage=UsageInfo(input_tokens=11, output_tokens=7),
                turn_usage=UsageInfo(),
                chain=SimpleNamespace(record_usage=lambda **_kwargs: None),
            )
        ]
        return updates[-1]

    settlement = asyncio.run(run())

    assert isinstance(settlement, ProviderStreamSettlement)
    assert settlement.action == "retry"
    assert settlement.turn_usage.billable_tokens == 18
    assert budget.tokens_used() == 18
