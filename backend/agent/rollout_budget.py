"""Shared token accounting for one root run and its delegated agents."""

from __future__ import annotations

from threading import RLock
from typing import Any


def billable_tokens_from_usage(usage: Any) -> int:
    """Return billable input plus output tokens for a UsageInfo-like value."""
    if isinstance(usage, dict):
        get = usage.get
    else:
        get = lambda key, default=0: getattr(usage, key, default)
    input_tokens = max(0, int(get("input_tokens", 0) or 0))
    cache_read = max(0, int(get("cache_read_input_tokens", 0) or 0))
    output_tokens = max(0, int(get("output_tokens", 0) or 0))
    return max(0, input_tokens - cache_read) + output_tokens


class RolloutBudget:
    """Atomic usage ledger shared by the root run and delegated agents.

    Provider usage is cumulative within each run, so contributors publish their
    latest total. Re-publishing the same total is idempotent and a later total
    contributes only its delta to the shared rollout.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._tokens_used = 0
        self._contributor_totals: dict[str, int] = {}

    def record_usage_total(self, contributor_id: str, usage: Any) -> int:
        return self.record_total(
            contributor_id,
            billable_tokens_from_usage(usage),
        )

    def record_total(self, contributor_id: str, total_tokens: int) -> int:
        contributor = str(contributor_id or "").strip()
        if not contributor:
            raise ValueError("rollout budget contributor_id must not be empty")
        total = max(0, int(total_tokens or 0))
        with self._lock:
            previous = self._contributor_totals.get(contributor, 0)
            if total <= previous:
                return self._tokens_used
            self._contributor_totals[contributor] = total
            self._tokens_used += total - previous
            return self._tokens_used

    def tokens_used(self) -> int:
        with self._lock:
            return self._tokens_used
