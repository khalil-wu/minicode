"""Shared token accounting for one root run and its delegated agents."""

from __future__ import annotations

from threading import RLock
from dataclasses import dataclass
from typing import Any


def billable_tokens_from_usage(usage: Any) -> int:
    """Return billable input plus output tokens for a UsageInfo-like value.

    OpenAI-compatible usage normally reports ``input_tokens`` including the
    cached prefix, while Anthropic/Pi report cache reads as a separate field.
    The adapter-owned ``input_includes_cache_read`` flag is therefore part of
    the accounting contract; subtracting cache reads unconditionally silently
    under-counts separate-cache providers and makes the turn token boundary
    provider-order dependent during fallback.
    """
    if isinstance(usage, dict):
        get = usage.get
    else:
        get = lambda key, default=0: getattr(usage, key, default)
    input_tokens = max(0, int(get("input_tokens", 0) or 0))
    cache_read = max(0, int(get("cache_read_input_tokens", 0) or 0))
    output_tokens = max(0, int(get("output_tokens", 0) or 0))
    includes_cache = bool(get("input_includes_cache_read", True))
    billable_input = (
        max(0, input_tokens - cache_read)
        if includes_cache
        else input_tokens
    )
    return billable_input + output_tokens


@dataclass(frozen=True, slots=True)
class RolloutBudgetSnapshot:
    token_limit: int
    tokens_used: int
    reserved_tokens: int
    available_tokens: int | None


class RolloutBudget:
    """Atomic usage ledger shared by the root run and delegated agents.

    Provider usage is cumulative within each run, so contributors publish their
    latest total. Re-publishing the same total is idempotent and a later total
    contributes only its delta to the shared rollout.
    """

    def __init__(self, *, token_limit: int = 0) -> None:
        self._lock = RLock()
        self._token_limit = max(0, int(token_limit or 0))
        self._tokens_used = 0
        self._contributor_totals: dict[str, int] = {}
        self._reservations: dict[str, int] = {}

    @property
    def token_limit(self) -> int:
        return self._token_limit

    def reserve(self, contributor_id: str, requested_tokens: int = 0) -> int:
        """Atomically reserve a child quota from the root rollout budget.

        A zero root limit means unlimited accounting and therefore requires no
        reservation. For a limited rollout, a zero requested quota means "all
        currently available tokens"; callers then clamp the child boundary to
        the returned amount.
        """

        contributor = str(contributor_id or "").strip()
        if not contributor:
            raise ValueError("rollout budget contributor_id must not be empty")
        requested = max(0, int(requested_tokens or 0))
        with self._lock:
            existing = self._reservations.get(contributor)
            if existing is not None:
                return existing
            if self._token_limit <= 0:
                return requested
            available = max(
                0,
                self._token_limit
                - self._tokens_used
                - sum(self._reservations.values()),
            )
            quota = min(requested, available) if requested > 0 else available
            if quota > 0:
                self._reservations[contributor] = quota
            return quota

    def release_reservation(self, contributor_id: str) -> int:
        contributor = str(contributor_id or "").strip()
        with self._lock:
            self._reservations.pop(contributor, None)
            return self._remaining_locked()

    def remaining_tokens(self) -> int | None:
        with self._lock:
            if self._token_limit <= 0:
                return None
            return self._remaining_locked()

    def snapshot(self, *, excluding_reservation: str = "") -> RolloutBudgetSnapshot:
        """Return one lock-consistent view used by rollout admission.

        A child excludes its own remaining reservation because its local turn
        controller enforces that quota. Every other outstanding reservation is
        treated as committed capacity, so a parent or sibling cannot spend it.
        """

        excluded = str(excluding_reservation or "").strip()
        with self._lock:
            reserved = sum(
                quota
                for contributor, quota in self._reservations.items()
                if contributor != excluded
            )
            available = (
                None
                if self._token_limit <= 0
                else max(0, self._token_limit - self._tokens_used - reserved)
            )
            return RolloutBudgetSnapshot(
                token_limit=self._token_limit,
                tokens_used=self._tokens_used,
                reserved_tokens=reserved,
                available_tokens=available,
            )

    def _remaining_locked(self) -> int:
        return max(
            0,
            self._token_limit
            - self._tokens_used
            - sum(self._reservations.values()),
        )

    def record_usage_total(
        self,
        contributor_id: str,
        usage: Any,
        *,
        reservation_id: str = "",
    ) -> int:
        return self.record_total(
            contributor_id,
            billable_tokens_from_usage(usage),
            reservation_id=reservation_id,
        )

    def record_total(
        self,
        contributor_id: str,
        total_tokens: int,
        *,
        reservation_id: str = "",
    ) -> int:
        contributor = str(contributor_id or "").strip()
        if not contributor:
            raise ValueError("rollout budget contributor_id must not be empty")
        total = max(0, int(total_tokens or 0))
        with self._lock:
            previous = self._contributor_totals.get(contributor, 0)
            if total <= previous:
                return self._tokens_used
            self._contributor_totals[contributor] = total
            delta = total - previous
            self._tokens_used += delta
            reservation_owner = str(reservation_id or contributor).strip()
            if reservation_owner in self._reservations:
                remaining_reservation = max(
                    0,
                    self._reservations[reservation_owner] - delta,
                )
                if remaining_reservation:
                    self._reservations[reservation_owner] = remaining_reservation
                else:
                    self._reservations.pop(reservation_owner, None)
            return self._tokens_used

    def tokens_used(self) -> int:
        with self._lock:
            return self._tokens_used
