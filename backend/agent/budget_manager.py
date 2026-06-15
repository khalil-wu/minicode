"""
Budget Manager - Token budget tracking and compaction decisions.

Extracted from ContextBuilder to follow SRP (Single Responsibility Principle).
Manages token counting, budget tracking, and compaction thresholds.
"""
from __future__ import annotations

from typing import Any

from backend.agent.state import AgentState
from backend.config import TokenBudget


class BudgetManager:
    """
    Manages token budget and compaction decisions.

    Responsibilities:
    - Track token usage across prompt components (system, history, tools, etc.)
    - Determine when compaction is needed
    - Provide budget snapshots for UI display
    """

    # Default compaction threshold (85% of total budget)
    DEFAULT_COMPACTION_THRESHOLD = 0.85

    def __init__(self, budget: TokenBudget):
        self._budget = budget
        self._last_actual_prompt_tokens = 0
        self._compaction_threshold = self.DEFAULT_COMPACTION_THRESHOLD

    def record_actual_usage(self, input_tokens: int, cached_tokens: int = 0) -> None:
        """
        Record provider-reported token usage.

        Character estimates are used before first request, but real
        provider counts are more accurate once available.
        """
        observed = input_tokens if input_tokens > 0 else cached_tokens
        if observed > 0:
            self._last_actual_prompt_tokens = observed

    def estimate_tokens(self, content: Any) -> int:
        """Estimate tokens from content (rough approximation: 1 token ≈ 4 chars)."""
        return len(str(content)) // 4

    def needs_compaction(
        self,
        current_usage: int,
        compaction_threshold: float | None = None,
    ) -> bool:
        """
        Determine if context compaction is needed.

        Args:
            current_usage: Estimated current token usage
            compaction_threshold: Override default threshold (0.0-1.0)

        Returns:
            True if compaction should be triggered
        """
        threshold = compaction_threshold if compaction_threshold is not None else self._compaction_threshold
        limit = int(self._budget.total * threshold)
        return current_usage > limit

    def get_budget_breakdown(
        self,
        system_tokens: int,
        notes_tokens: int,
        skills_tokens: int,
        rag_tokens: int,
        history_tokens: int,
        tools_tokens: int,
    ) -> dict[str, Any]:
        """
        Generate budget breakdown snapshot for UI display.

        Returns:
            Dictionary with 'used', 'total', and 'breakdown' fields
        """
        used = (
            system_tokens
            + notes_tokens
            + skills_tokens
            + rag_tokens
            + history_tokens
            + tools_tokens
        )

        # Use observed actual if higher than estimate
        if self._last_actual_prompt_tokens > used:
            used = self._last_actual_prompt_tokens

        return {
            "used": used,
            "total": self._budget.total,
            "breakdown": {
                "system": system_tokens + notes_tokens,
                "skills": skills_tokens,
                "rag": rag_tokens,
                "history": history_tokens,
                "tools": tools_tokens,
                "observed_actual": self._last_actual_prompt_tokens,
            },
        }

    def get_history_token_budget(self) -> int:
        """Calculate how many tokens are available for history."""
        return self._budget.history_budget

    @property
    def total_budget(self) -> int:
        """Total token budget."""
        return self._budget.total

    @property
    def compaction_threshold(self) -> float:
        """Compaction trigger threshold (0.0-1.0)."""
        return self._compaction_threshold
