"""Stream retry policy: protocol, decision dataclass, and default implementation.

Defines StreamRetryDecision, StreamRetryPolicy (Protocol), and DefaultStreamRetryPolicy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from backend.llm.errors import is_fatal_llm_error, is_retryable_llm_error

if TYPE_CHECKING:
    from backend.config import AgentSettings


@dataclass(frozen=True)
class StreamRetryDecision:
    """Immutable decision returned by a stream retry policy."""

    should_retry: bool
    delay_seconds: float
    max_attempts: int


@dataclass
class StreamRetryState:
    """Mutable counters owned by one foreground provider operation."""

    consecutive_529_errors: int = 0


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

    Retry shape: ``baseDelay = min(500ms * 2**(attempt-1), 32s)`` with
    ``+25%`` uniform jitter, ``maxRetries = 10``, and a separate 529 counter.
    The backoff curve is taken from cc (services/api/withRetry.ts:52,55,533,546
    — DEFAULT_MAX_RETRIES=10, BASE_DELAY_MS=500, maxDelayMs=32000, jitter
    ``random()*0.25*base``); the policy object, its settings surface and the
    error classification driving it are MiniCode's own.

    Reads four AgentSettings fields: stream_max_attempts,
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
        is_529 = classification.provider_error_type == "busy" and (
            "529" in error_message.lower()
            or "status=529" in error_message.lower()
            or "http 529" in error_message.lower()
        )
        if retry_state is not None:
            retry_state.consecutive_529_errors = (
                retry_state.consecutive_529_errors + 1 if is_529 else 0
            )

        if is_fatal_llm_error(error_message):
            return StreamRetryDecision(
                should_retry=False,
                delay_seconds=self._settings.stream_retry_delay_seconds,
                max_attempts=self._settings.stream_max_attempts,
            )

        error_lower = error_message.lower()
        source = str(query_source or "").strip().lower()
        foreground_529 = not source or source in {
            "user",
            "main",
            "foreground",
            "sdk",
            "agent:custom",
            "agent:default",
            "agent:builtin",
        } or source.startswith("agent:")
        consecutive_529_errors = (
            retry_state.consecutive_529_errors if retry_state is not None else 0
        )
        if is_529 and (not foreground_529 or consecutive_529_errors >= 3):
            return StreamRetryDecision(
                should_retry=False,
                delay_seconds=self._settings.stream_retry_delay_seconds,
                max_attempts=self._settings.stream_max_attempts,
            )

        has_budget = attempt_index < self._settings.stream_max_attempts
        should_retry = has_budget and (
            any(p in error_lower for p in self._settings.stream_retryable_substrings)
            or is_retryable_llm_error(error_message)
        )
        # base = min(delay * 2**attempt, 32s), then
        # add uniform jitter of up to +25% of the base so retries don't stampede.
        delay_seconds = min(
            float(self._settings.stream_retry_delay_seconds) * (2 ** max(0, int(attempt_index))),
            32.0,
        )
        delay_seconds *= 1.0 + random.random() * 0.25
        if should_retry:
            # A server Retry-After wins over the local backoff
            # (capped so a hostile header still cannot stall the turn).
            from backend.llm.errors import retry_after_seconds

            server_delay = retry_after_seconds(error_message)
            if server_delay > 0:
                delay_seconds = max(delay_seconds, min(server_delay, 60.0))
        return StreamRetryDecision(
            should_retry=should_retry,
            delay_seconds=delay_seconds,
            max_attempts=self._settings.stream_max_attempts,
        )
