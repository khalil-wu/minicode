"""Mechanical turn resource boundaries shared by every loop phase."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from backend.config import AgentSettings


BudgetReason = Literal[
    "max_turn_seconds",
    "max_iterations",
    "max_tool_calls",
    "max_retries",
    "max_turn_tokens",
    "max_turn_cost_usd",
    "budget_exceeded",
]


@dataclass(slots=True)
class TurnDeadlineController:
    """Own the single absolute clock for one agent turn."""

    max_turn_seconds: float
    turn_started_at: float | None = None
    turn_deadline: float | None = None

    def start_turn(self, *, now: float | None = None) -> float:
        if self.turn_started_at is not None:
            return self.turn_started_at
        started_at = time.monotonic() if now is None else float(now)
        self.turn_started_at = started_at
        limit = max(0.0, float(self.max_turn_seconds or 0.0))
        self.turn_deadline = started_at + limit if limit > 0 else None
        return started_at

    def elapsed(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else float(now)
        return (
            max(0.0, current - self.turn_started_at)
            if self.turn_started_at is not None
            else 0.0
        )

    def active_deadline(self) -> float | None:
        return self.turn_deadline

    def bounded_timeout(
        self,
        requested: float | None,
        *,
        now: float | None = None,
    ) -> tuple[float | None, bool, bool]:
        """Return timeout, whether it was capped, and whether it is expired."""
        deadline = self.active_deadline()
        if deadline is None:
            return (
                None if requested is None else max(0.0, float(requested)),
                False,
                False,
            )
        current = time.monotonic() if now is None else float(now)
        remaining = deadline - current
        if remaining <= 0:
            return 0.0, True, True
        if requested is None:
            return remaining, True, False
        timeout = max(0.0, float(requested))
        return min(timeout, remaining), remaining <= timeout, False

    def cap_with_all_deadlines(self, deadline: float | None) -> float | None:
        """Cap a nested wait by the root turn deadline."""
        if self.turn_deadline is None:
            return deadline
        return (
            min(deadline, self.turn_deadline)
            if deadline is not None
            else self.turn_deadline
        )


@dataclass(frozen=True, slots=True)
class BudgetBoundary:
    reason: BudgetReason
    limit: float
    observed: float
    detail: str
    post_tools: bool = False

    @property
    def user_message(self) -> str:
        if self.reason == "max_turn_seconds":
            return f"已达到本轮执行时间上限（{self.limit:g}秒）。"
        if self.reason == "max_iterations":
            return f"已达到最大迭代次数限制（{int(self.limit)}次）。"
        if self.reason == "max_tool_calls":
            return f"已达到工具调用上限（{int(self.limit)}次）。"
        if self.reason == "max_retries":
            return f"已达到本轮错误恢复上限（{int(self.limit)}次）。"
        if self.reason == "max_turn_tokens":
            return f"已达到本轮 Token 上限（{int(self.limit)}）。"
        if self.reason == "max_turn_cost_usd":
            return f"已达到本轮费用上限（${self.limit:g}）。"
        return "上下文预算耗尽，无法继续调用模型。"

    @property
    def label(self) -> str:
        return {
            "max_turn_seconds": "Wall-clock budget",
            "max_iterations": "Iteration budget",
            "max_tool_calls": "Tool-call budget",
            "max_retries": "Retry budget",
            "max_turn_tokens": "Token budget",
            "max_turn_cost_usd": "Cost budget",
            "budget_exceeded": "Context budget",
        }[self.reason]

    @property
    def event_key(self) -> str:
        return self.reason.replace("_", "-")


@dataclass(frozen=True, slots=True)
class TurnBudgetController:
    max_turn_seconds: float
    max_iterations: int
    max_tool_calls: int
    max_retries: int = 0
    max_turn_tokens: int = 0
    max_turn_cost_usd: float = 0.0

    @classmethod
    def from_settings(
        cls,
        settings: AgentSettings,
        *,
        max_iterations: int,
    ) -> "TurnBudgetController":
        return cls(
            max_turn_seconds=max(0.0, float(settings.max_turn_seconds or 0.0)),
            max_iterations=max(0, int(max_iterations)),
            max_tool_calls=max(0, int(settings.max_tool_calls or 0)),
            max_retries=max(0, int(settings.turn_error_budget or 0)),
            max_turn_tokens=max(0, int(settings.max_turn_tokens or 0)),
            max_turn_cost_usd=max(0.0, float(settings.max_turn_cost_usd or 0.0)),
        )

    def with_iteration_limit(self, value: int) -> "TurnBudgetController":
        return TurnBudgetController(
            max_turn_seconds=self.max_turn_seconds,
            max_iterations=max(0, int(value)),
            max_tool_calls=self.max_tool_calls,
            max_retries=self.max_retries,
            max_turn_tokens=self.max_turn_tokens,
            max_turn_cost_usd=self.max_turn_cost_usd,
        )

    def evaluate(
        self,
        *,
        elapsed_seconds: float,
        iterations: int,
        tool_calls: int,
        retries: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
        post_tools: bool = False,
    ) -> BudgetBoundary | None:
        """Return the first exhausted boundary, or None while the turn may run.

        Order is meaningful. Wall-clock and tokens measure what the turn
        actually costs, so they are checked first: when several budgets are
        exhausted at once the reported reason should be the real one. Iteration
        and tool-call counts are proxies for the same thing and are reported
        only when no direct cost boundary explains the stop.

        Every phase consumes the same root budget. Reaching a boundary is a
        terminal runtime fact; it never creates an extra provider turn.
        """
        if (
            self.max_turn_seconds > 0
            and elapsed_seconds >= self.max_turn_seconds
        ):
            return BudgetBoundary(
                reason="max_turn_seconds",
                limit=self.max_turn_seconds,
                observed=max(0.0, elapsed_seconds),
                detail=(
                    "turn wall-clock budget reached "
                    f"({elapsed_seconds:.1f}s/{self.max_turn_seconds:.1f}s)"
                ),
                post_tools=post_tools,
            )
        if self.max_turn_tokens > 0 and tokens >= self.max_turn_tokens:
            return BudgetBoundary(
                reason="max_turn_tokens",
                limit=float(self.max_turn_tokens),
                observed=float(tokens),
                detail=f"turn token limit reached ({tokens}/{self.max_turn_tokens})",
                post_tools=post_tools,
            )
        if self.max_turn_cost_usd > 0 and cost_usd >= self.max_turn_cost_usd:
            return BudgetBoundary(
                reason="max_turn_cost_usd",
                limit=self.max_turn_cost_usd,
                observed=max(0.0, float(cost_usd)),
                detail=f"turn cost limit reached (${cost_usd:.4f}/${self.max_turn_cost_usd:.4f})",
                post_tools=post_tools,
            )
        if self.max_iterations > 0 and iterations >= self.max_iterations:
            return BudgetBoundary(
                reason="max_iterations",
                limit=float(self.max_iterations),
                observed=float(iterations),
                detail=f"work iteration limit reached ({self.max_iterations})",
                post_tools=post_tools,
            )
        if self.max_tool_calls > 0 and tool_calls >= self.max_tool_calls:
            return BudgetBoundary(
                reason="max_tool_calls",
                limit=float(self.max_tool_calls),
                observed=float(tool_calls),
                detail=f"tool call limit reached ({self.max_tool_calls})",
                post_tools=post_tools,
            )
        if self.max_retries > 0 and retries >= self.max_retries:
            return BudgetBoundary(
                reason="max_retries",
                limit=float(self.max_retries),
                observed=float(retries),
                detail=f"turn recovery limit reached ({retries}/{self.max_retries})",
                post_tools=post_tools,
            )
        return None

    @staticmethod
    def context_exhausted() -> BudgetBoundary:
        return BudgetBoundary(
            reason="budget_exceeded",
            limit=0.0,
            observed=0.0,
            detail="context budget exhausted",
        )
