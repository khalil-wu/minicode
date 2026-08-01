"""Runtime ownership for turn budgets, deadlines, and recovery attempts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.agent.budget_termination import BudgetTerminationCoordinator
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.agent.turn_budget import (
    BudgetBoundary,
    TurnBudgetController,
    TurnDeadlineController,
)
from backend.agent.rollout_budget import RolloutBudget
from backend.llm.base import UsageInfo


@dataclass(slots=True)
class TurnBudgetRuntime:
    """Coordinate mutable budget state without leaking it into the agent loop."""

    state: Any
    tool_context: Any
    usage: Callable[[], UsageInfo]
    rollout_budget: RolloutBudget
    deadlines: TurnDeadlineController
    controller: TurnBudgetController
    termination: BudgetTerminationCoordinator
    cost_session_id: str = ""
    _cost_baseline_usd: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        from backend.llm.cost_tracker import CostTracker

        self._cost_baseline_usd = float(
            CostTracker.get_instance()
            .get_summary(self.cost_session_id)
            .get("total_cost_usd")
            or 0.0
        )

    async def apply_boundary(
        self,
        boundary: BudgetBoundary,
    ) -> tuple[bool, tuple[Any, ...]]:
        return await self.termination.apply(boundary)

    def ensure_started(self) -> None:
        self.deadlines.start_turn()
        self.tool_context.deadline_monotonic = self.deadlines.turn_deadline

    def rollout_tokens_used(self) -> int:
        self.rollout_budget.record_usage_total(
            self._usage_contributor_id(),
            self.usage(),
        )
        return self.rollout_budget.tokens_used()

    def _usage_contributor_id(self) -> str:
        metadata = getattr(self.tool_context, "metadata", {}) or {}
        return str(
            metadata.get("run_id")
            or metadata.get("task_id")
            or getattr(self.tool_context, "task_id", "")
            or f"run:{id(self)}"
        ).strip()

    def record_provider_usage_total(self, total_usage: UsageInfo) -> None:
        self.rollout_budget.record_usage_total(
            self._usage_contributor_id(),
            total_usage,
        )

    def turn_cost_usd(self) -> float:
        from backend.llm.cost_tracker import CostTracker

        current = float(
            CostTracker.get_instance()
            .get_summary(self.cost_session_id)
            .get("total_cost_usd")
            or 0.0
        )
        return max(0.0, current - self._cost_baseline_usd)

    def active_phase_deadline(self) -> float | None:
        return self.deadlines.active_deadline()

    def bounded_provider_timeout(self, requested: float | None) -> tuple[float | None, bool]:
        """Cap one provider wait to the active phase's absolute deadline."""
        timeout, capped, expired = self.deadlines.bounded_timeout(
            requested,
        )
        if expired:
            raise PhaseDeadlineExceeded
        return timeout, capped

    def phase_deadline_boundary(self) -> BudgetBoundary:
        limit = self.controller.max_turn_seconds
        observed = self.deadlines.elapsed()
        return BudgetBoundary(
            reason="max_turn_seconds",
            limit=limit,
            observed=observed,
            detail=(
                "turn wall-clock budget reached during provider wait "
                f"({observed:.1f}s/{limit:.1f}s)"
            ),
        )

    def consume_retry(self, reason: str) -> BudgetBoundary | None:
        """Reserve one loop-owned recovery attempt or return its boundary."""
        limit = self.controller.max_retries
        if limit > 0 and self.state.total_retries >= limit:
            return BudgetBoundary(
                reason="max_retries",
                limit=float(limit),
                observed=float(self.state.total_retries),
                detail=(
                    f"turn recovery limit reached before {reason} "
                    f"({self.state.total_retries}/{limit})"
                ),
            )
        self.state.total_retries += 1
        self.state.recovery_iterations += 1
        self.state.mark_transition(
            "retry_budget_consumed",
            retry_reason=reason,
            retry_count=self.state.total_retries,
            retry_limit=limit,
        )
        return None
