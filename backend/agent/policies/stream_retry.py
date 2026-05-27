"""Stream retry policy: protocol, decision dataclass, and default implementation.

Defines StreamRetryDecision, StreamRetryPolicy (Protocol), and DefaultStreamRetryPolicy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from backend.llm.errors import is_fatal_llm_error

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

        should_retry is True when:
        - attempt_index < settings.stream_max_attempts, AND
        - any retryable substring appears in error_message (case-insensitive)
        """
        if is_fatal_llm_error(error_message):
            return StreamRetryDecision(
                should_retry=False,
                delay_seconds=self._settings.stream_retry_delay_seconds,
                max_attempts=self._settings.stream_max_attempts,
            )

        error_lower = error_message.lower()
        should_retry = (
            attempt_index < self._settings.stream_max_attempts
            and any(
                p in error_lower
                for p in self._settings.stream_retryable_substrings
            )
        )
        return StreamRetryDecision(
            should_retry=should_retry,
            delay_seconds=self._settings.stream_retry_delay_seconds,
            max_attempts=self._settings.stream_max_attempts,
        )
