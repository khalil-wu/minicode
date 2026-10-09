"""Stream retry policy: protocol, decision dataclass, and default implementation.

Defines StreamRetryDecision, StreamRetryPolicy (Protocol), and DefaultStreamRetryPolicy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from backend.config import AgentSettings


@dataclass(frozen=True)
class StreamRetryDecision:
    """Immutable decision returned by a stream retry policy."""

    should_retry: bool
    delay_seconds: float
    max_attempts: int

    @property
    def max_retries(self) -> int:
        """Public name for the retry budget (initial request excluded)."""

        return self.max_attempts


@dataclass
class StreamRetryState:
    """Mutable counters owned by one foreground provider operation."""

    auth_recovery_attempted: bool = False
    # The connect phase keeps its own schedule. Its delays double from a short
    # initial value to a long cap because the cause is usually the user's
    # network rather than the request, and its attempts are deliberately not
    # counted against the request retry budget.
    connection_retries: int = 0
    connection_retry_delay_seconds: float = 0.0


# Connect-phase schedule. An unreachable provider is a waiting problem, not a
# failing request: the delay starts short so a blip recovers immediately and
# caps so a long outage polls slowly.
CONNECTION_RETRY_INITIAL_DELAY_SECONDS = 5.0
CONNECTION_RETRY_MAX_DELAY_SECONDS = 60.0


def plan_connection_retry(
    state: StreamRetryState,
    *,
    initial_seconds: float = CONNECTION_RETRY_INITIAL_DELAY_SECONDS,
    max_seconds: float = CONNECTION_RETRY_MAX_DELAY_SECONDS,
) -> float:
    """Return the next connect-phase delay and advance the doubling schedule.

    Unlike :meth:`DefaultStreamRetryPolicy.decide_retry` this returns no retry
    budget: the caller reissues the same attempt until the provider becomes
    reachable or the turn's own deadline fires.
    """

    initial = max(0.0, float(initial_seconds))
    ceiling = max(initial, float(max_seconds))
    delay = state.connection_retry_delay_seconds or initial
    state.connection_retries += 1
    state.connection_retry_delay_seconds = min(delay * 2.0, ceiling)
    return delay


class StreamRetryPolicy(Protocol):
    """Protocol for stream retry policies.

    Implementations decide whether to retry a failed stream operation
    based on the error message and the current attempt index.
    """

    def decide_retry(
        self,
        error_message: str,
        attempt_index: int,
        *,
        query_source: str | None = None,
        retry_state: StreamRetryState | None = None,
    ) -> StreamRetryDecision:
        """Return a retry decision given the error and attempt index.

        This must be a pure function — no async, no I/O.
        """
        ...


class DefaultStreamRetryPolicy:
    """Default stream retry policy that classifies errors using AgentSettings.

    Codex's async-utils/backoff.rs uses a 200ms exponential base and ±10%
    jitter; model-provider-info defaults to five stream retries. Explicit
    host settings still own the retry budget and base delay. The request
    owner applies server Retry-After exactly once after this local decision.

    Reads four AgentSettings fields: stream_max_attempts (the maximum number
    of retries; the initial request is excluded),
    stream_retry_delay_seconds, stream_retryable_substrings,
    stream_timeout_seconds.
    """

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def decide_retry(
        self,
        error_message: str,
        attempt_index: int,
        *,
        query_source: str | None = None,
        retry_state: StreamRetryState | None = None,
    ) -> StreamRetryDecision:
        """Return a retry decision based on error message content and attempt budget.

        should_retry is True when attempt budget remains AND either:
        - a configured retryable substring appears (e.g. rate-limit / 429), OR
        - the error classifies as retryable (transient network / stream drop).

        The classifier branch is what lets a DeepSeek streaming cutoff
        (RemoteProtocolError "peer closed connection…") retry instead of
        surfacing immediately as a generic failure.
        """
        from backend.llm.errors import classify_llm_error

        classification = classify_llm_error(error_message)
        if classification.fatal:
            return StreamRetryDecision(
                should_retry=False,
                delay_seconds=self._settings.stream_retry_delay_seconds,
                max_attempts=self._settings.stream_max_attempts,
            )

        error_lower = error_message.lower()
        has_budget = attempt_index < self._settings.stream_max_attempts
        should_retry = has_budget and (
            any(p in error_lower for p in self._settings.stream_retryable_substrings)
            or classification.retryable
        )
        delay_seconds = float(self._settings.stream_retry_delay_seconds) * (2 ** max(0, int(attempt_index)))
        delay_seconds *= 0.9 + random.random() * 0.2
        return StreamRetryDecision(
            should_retry=should_retry,
            delay_seconds=delay_seconds,
            max_attempts=self._settings.stream_max_attempts,
        )
