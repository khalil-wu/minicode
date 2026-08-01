"""Stream retry policy: protocol, decision dataclass, and default implementation.

Defines StreamRetryDecision, StreamRetryPolicy (Protocol), and DefaultStreamRetryPolicy.
"""

from __future__ import annotations

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


class StreamRetryPolicy(Protocol):
    """Protocol for stream retry policies.

    Implementations decide whether to retry a failed stream operation
    based on the error message and the current attempt index.
    """

    def decide_retry(self, error_message: str, attempt_index: int) -> StreamRetryDecision:
        """Return a retry decision given the error and attempt index.

        This must be a pure function — no async, no I/O.
        """
        ...


class DefaultStreamRetryPolicy:
    """Default stream retry policy that classifies errors using AgentSettings.

    Pure function — no I/O, no async. Reads only four AgentSettings fields:
    stream_max_attempts, stream_retry_delay_seconds, stream_retryable_substrings,
    stream_timeout_seconds.
    """

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def decide_retry(self, error_message: str, attempt_index: int) -> StreamRetryDecision:
        """Return a retry decision based on error message content and attempt budget.

        should_retry is True when attempt budget remains AND either:
        - a configured retryable substring appears (e.g. rate-limit / 429), OR
        - the error classifies as retryable (transient network / stream drop).

        The classifier branch is what lets a DeepSeek streaming cutoff
        (RemoteProtocolError "peer closed connection…") retry instead of
        surfacing immediately as a generic failure.
        """
        if is_fatal_llm_error(error_message):
            return StreamRetryDecision(
                should_retry=False,
                delay_seconds=self._settings.stream_retry_delay_seconds,
                max_attempts=self._settings.stream_max_attempts,
            )

        error_lower = error_message.lower()
        has_budget = attempt_index < self._settings.stream_max_attempts
        should_retry = has_budget and (
            any(p in error_lower for p in self._settings.stream_retryable_substrings)
            or is_retryable_llm_error(error_message)
        )
        # A delay is an explicit host/provider setting. The policy must not
        # invent an exponential schedule or a hidden upper bound; provider
        # Retry-After and the configured value are resolved by the caller.
        delay_seconds = max(0.0, float(self._settings.stream_retry_delay_seconds))
        return StreamRetryDecision(
            should_retry=should_retry,
            delay_seconds=delay_seconds,
            max_attempts=self._settings.stream_max_attempts,
        )
