"""
Error Withholding pattern -- hold recoverable errors before yielding to frontend.

Inspired by Claude Code: when the LLM returns a context-overflow error (e.g., an
HTTP 413 prompt-too-long response), the error is NOT immediately yielded to the user.
Instead, recovery strategies are tried in order. Only if all fail is the error
surfaced.

This prevents "error flicker" where the user sees an error that gets immediately
resolved by a successful recovery.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend.agent.message import AgentEvent

logger = logging.getLogger(__name__)


# Markers for context-overflow errors (Anthropic "prompt is too long", OpenAI
# "context_length_exceeded", DeepSeek "maximum context length", Vertex 413).
# Mirrors the PTL markers used by ContextBuilder.full_compact's retry.
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "prompt too long",
    "context_length",
    "maximum context",
    # Bare "context window" deliberately excluded (cc matches only the exact
    # 'prompt is too long' prefix): a random 500 mentioning the context window
    # must not consume the single reactive-compaction budget.
    "request_too_large",
    "request entity too large",
    "request body too large",
    "payload too large",
    "content too large",
)
_HTTP_413_RE = re.compile(
    r"\b(?:http(?:\s+status)?|status(?:\s+code)?|error(?:\s+code)?|code)\s*[:=]?\s*413\b",
    re.IGNORECASE,
)


def is_context_overflow_error(error: Any) -> bool:
    """True when the error text reports a context/prompt-size overflow."""
    text = str(error or "").lower()
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    return (
        status_code == 413
        or any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)
        or _HTTP_413_RE.search(text) is not None
    )


_MEDIA_SIZE_PDF_PAGES_RE = re.compile(r"maximum of \d+ PDF pages")


def is_media_size_error(error: Any) -> bool:
    """True when the provider rejected oversized image/PDF media in context.

    cc's predicate is deliberately conjunctive (errors.ts isMediaSizeError):
    a bare ``request too large`` 413 must NOT match, or stripping would delete
    the user's current-turn attachments for an unrelated payload rejection.
    """
    text = str(error or "")
    if not text:
        return False
    lowered = text.lower()
    return (
        ("image exceeds" in lowered and "maximum" in lowered)
        or ("image dimensions exceed" in lowered and "many-image" in lowered)
        or _MEDIA_SIZE_PDF_PAGES_RE.search(lowered) is not None
    )


@dataclass
class RecoveryStrategy:
    """A single recovery strategy to try when an error is withheld."""
    name: str
    description: str
    try_recover: Callable[..., Awaitable[bool]]
    # If True, the loop should continue after this recovery succeeds
    continue_loop: bool = True


@dataclass
class WithheldError:
    """An error that is being withheld from the frontend while recovery is attempted."""
    original_error: Any
    error_type: str
    recoverable: bool = True
    recovery_strategies: list[RecoveryStrategy] = field(default_factory=list)
    recovery_attempts: int = 0
    max_recovery_attempts: int = 3

    @property
    def is_exhausted(self) -> bool:
        return self.recovery_attempts >= self.max_recovery_attempts

    def to_agent_event(self) -> AgentEvent:
        """Convert to an AgentEvent for yielding to frontend (only after recovery fails)."""
        return AgentEvent.error(
            message=str(self.original_error),
            recoverable=self.recoverable,
            error_type=self.error_type,
        )

class ErrorWithholdingController:
    """Manages error withholding and recovery across the agent loop.

    Usage in the agent loop:

        controller = ErrorWithholdingController()

        # When an error occurs:
        if controller.is_withholdable(error_type, error):
            withheld = controller.withhold(error, error_type, [
                RecoveryStrategy("context_drain", "Drain staged context collapses", ...),
                RecoveryStrategy("reactive_compact", "Emergency compaction", ...),
            ])
            for strategy in withheld.recovery_strategies:
                if await strategy.try_recover():
                    # Recovery succeeded. The raw provider body remains withheld.
                    state.mark_transition(f"recovered_{strategy.name}")
                    break
            else:
                # All strategies failed -- surface the error
                yield withheld.to_agent_event()
        else:
            # Non-recoverable -- yield immediately
            yield error_event
    """

    # Only context-overflow errors are withheld: the sole recovery strategy is
    # emergency compaction, which cannot help rate limits or overloads — those
    # go through the stream retry/backoff ladder instead (cc only reactive-
    # compacts prompt-too-long / media-size errors).
    WITHHOLDABLE_ERROR_TYPES = {
        "content_filter",
        "prompt_too_long",
        "context_overflow",
        "media_size",
    }

    # Error types that should never be withheld
    FATAL_ERROR_TYPES = {
        "auth_failed",
        "invalid_api_key",
        "model_not_found",
    }

    def __init__(self) -> None:
        self._withheld: WithheldError | None = None
        self._recovery_log: list[dict[str, Any]] = []

    def is_withholdable(self, error_type: str, error: Any = None) -> bool:
        """Determine if an error should be withheld for recovery."""
        if error_type in self.FATAL_ERROR_TYPES:
            return False
        if error_type in self.WITHHOLDABLE_ERROR_TYPES:
            return True
        return is_context_overflow_error(error) or is_media_size_error(error)

    def withhold(
        self,
        error: Any,
        error_type: str,
        strategies: list[RecoveryStrategy] | None = None,
    ) -> WithheldError:
        """Start withholding an error."""
        self._withheld = WithheldError(
            original_error=error,
            error_type=error_type,
            recovery_strategies=strategies or [],
        )
        logger.info(
            "[ErrorWithholding] Withholding %s error, %d strategies available",
            error_type,
            len(self._withheld.recovery_strategies),
        )
        return self._withheld

    def record_recovery(self, strategy_name: str, success: bool, detail: str = "") -> None:
        """Log a recovery attempt."""
        self._recovery_log.append({
            "strategy": strategy_name,
            "success": success,
            "detail": detail,
            "attempt": self._withheld.recovery_attempts if self._withheld else 0,
        })
        if self._withheld:
            self._withheld.recovery_attempts += 1

    def clear(self) -> None:
        """Clear the current withheld error (after successful recovery or surfacing)."""
        self._withheld = None

    @property
    def current(self) -> WithheldError | None:
        return self._withheld

    @property
    def recovery_log(self) -> list[dict[str, Any]]:
        return list(self._recovery_log)
