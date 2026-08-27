"""Runtime ownership for turn budgets, deadlines, and recovery attempts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.agent.budget_termination import BudgetTerminationCoordinator
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.agent.turn_budget import (
    BudgetBoundary,
    TurnBudgetController,
    TurnDeadlineController,
)
from backend.agent.rollout_budget import RolloutBudget, billable_tokens_from_usage
from backend.llm.cost_tracker import estimate_usage_cost_usd
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

    async def apply_boundary(
        self,
        boundary: BudgetBoundary,
    ) -> tuple[bool, tuple[Any, ...]]:
        return await self.termination.apply(boundary)

    def ensure_started(self) -> None:
        self.deadlines.start_turn()
        self.tool_context.deadline_monotonic = self.deadlines.turn_deadline

    def local_tokens_used(self) -> int:
        current_usage = self.usage()
        self.rollout_budget.record_usage_total(
            self._usage_contributor_id(),
            current_usage,
            reservation_id=self._reservation_id(),
        )
        return billable_tokens_from_usage(current_usage)

    def rollout_boundary(self, *, post_tools: bool = False) -> BudgetBoundary | None:
        """Fence shared capacity without charging a child for its own quota."""

        self.local_tokens_used()
        snapshot = self.rollout_budget.snapshot(
            excluding_reservation=self._reservation_id(),
        )
        observed = snapshot.tokens_used + snapshot.reserved_tokens
        if snapshot.token_limit <= 0 or observed < snapshot.token_limit:
            return None
        return BudgetBoundary(
            reason="max_turn_tokens",
            limit=float(snapshot.token_limit),
            observed=float(observed),
            detail=(
                "shared rollout token capacity exhausted "
                f"({snapshot.tokens_used} used + {snapshot.reserved_tokens} reserved"
                f"/{snapshot.token_limit})"
            ),
            post_tools=post_tools,
        )

    def rollout_tokens_used(self) -> int:
        """Compatibility metric; admission must use the two-level methods."""

        self.local_tokens_used()
        return self.rollout_budget.tokens_used()

    def _usage_contributor_id(self) -> str:
        metadata = getattr(self.tool_context, "metadata", {}) or {}
        agent_path = str(metadata.get("agent_path") or "").strip()
        mailbox_epoch = max(0, int(metadata.get("mailbox_epoch") or 0))
        if agent_path and mailbox_epoch > 0:
            return f"{agent_path}@{mailbox_epoch}"
        return str(
            metadata.get("run_id")
            or metadata.get("task_id")
            or getattr(self.tool_context, "task_id", "")
            or f"run:{id(self)}"
        ).strip()

    def _reservation_id(self) -> str:
        metadata = getattr(self.tool_context, "metadata", {}) or {}
        return str(metadata.get("_rollout_reservation_id") or "").strip()

    def record_provider_usage_total(self, total_usage: UsageInfo) -> None:
        self.rollout_budget.record_usage_total(
            self._usage_contributor_id(),
            total_usage,
            reservation_id=self._reservation_id(),
        )

    def turn_cost_usd(self) -> float | None:
        """Cost of this turn so far, or None when it cannot be priced.

        Budget checks happen before the WS terminal handler commits totals to
        CostTracker, so the turn-owned provider usage is authoritative here.

        ``UsageInfo.cost_usd`` is only ever populated from a provider-reported
        cost, and no provider MiniCode supports reports one, so reading it alone
        made ``max_turn_cost_usd`` unarmed for every provider while presenting a
        confident ``$0``. Fall back to the local price table, and return None —
        not 0.0 — when the active model is unpriced, so the caller can say the
        ceiling cannot be enforced instead of silently never reaching it.
        """
        usage = self.usage()
        reported = getattr(usage, "cost_usd", None)
        if reported is not None:
            return max(0.0, float(reported))
        model_id = str(getattr(self.state, "model", "") or "").strip()
        if not model_id:
            return None
        estimated = estimate_usage_cost_usd(model_id, usage)
        return None if estimated is None else max(0.0, estimated)

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
