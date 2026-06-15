"""
Error Withholding pattern -- hold recoverable errors before yielding to frontend.

Inspired by Claude Code: when the LLM returns a recoverable error (e.g., 413 prompt
too long, max output tokens), the error is NOT immediately yielded to the user.
Instead, recovery strategies are tried in order. Only if all fail is the error
surfaced.

This prevents "error flicker" where the user sees an error that gets immediately
resolved by a successful recovery.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Awaitable

from backend.agent.message import AgentEvent

logger = logging.getLogger(__name__)


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
        if controller.is_withholdable(error_type, full_text, pending_tool_calls):
            withheld = controller.withhold(error, error_type, [
                RecoveryStrategy("context_drain", "Drain staged context collapses", ...),
                RecoveryStrategy("reactive_compact", "Emergency compaction", ...),
            ])
            for strategy in withheld.recovery_strategies:
                if await strategy.try_recover(state, ctx):
                    # Recovery succeeded -- continue the loop without yielding the error
                    state.transition = f"recovered_{strategy.name}"
                    break
            else:
                # All strategies failed -- surface the error
                yield withheld.to_agent_event()
        else:
            # Non-recoverable -- yield immediately
            yield error_event
    """

    # Error types that should be withheld for recovery attempts
    WITHHOLDABLE_ERROR_TYPES = {
        "prompt_too_long",
        "context_overflow",
        "max_output_tokens",
        "rate_limit",
        "overloaded",
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

    def is_withholdable(
        self,
        error_type: str,
        has_partial_text: bool = False,
        has_tool_calls: bool = False,
    ) -> bool:
        """Determine if an error should be withheld for recovery."""
        if error_type in self.FATAL_ERROR_TYPES:
            return False
        if error_type in self.WITHHOLDABLE_ERROR_TYPES:
            return True
        # If we have partial content, try recovery even for unknown errors
        return has_partial_text or has_tool_calls

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
