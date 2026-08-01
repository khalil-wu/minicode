"""Stateless timing, classification, and retry helpers for the agent loop."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast, get_args

from backend.agent.state import AgentState, TerminalReason
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message


logger = logging.getLogger(__name__)

MAX_OUTPUT_FINISH_REASONS = frozenset(
    {
        "length",
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "incomplete",
    }
)
TERMINAL_REASON_VALUES = frozenset(str(value) for value in get_args(TerminalReason))


def terminal_reason_from_error_type(
    error_type: str | None,
    *,
    fallback: TerminalReason = "api_error",
) -> TerminalReason:
    reason = str(error_type or "").strip()
    if reason in TERMINAL_REASON_VALUES:
        return cast(TerminalReason, reason)
    return fallback


def format_llm_error(message: str) -> str:
    return sanitize_llm_error_message(
        message,
        classify_llm_error(message),
        include_provider_details=False,
    )


def is_max_output_finish_reason(reason: str) -> bool:
    return str(reason or "").strip().lower() in MAX_OUTPUT_FINISH_REASONS


def iteration_id(state: AgentState) -> str:
    return f"iter:{max(1, state.iterations)}"


def epoch_ms() -> int:
    return int(time.time() * 1000)


async def sleep_or_cancel(
    delay_seconds: float,
    cancel_event: asyncio.Event | None = None,
) -> None:
    if delay_seconds <= 0:
        return
    if cancel_event is None:
        await asyncio.sleep(delay_seconds)
        return
    if cancel_event.is_set():
        raise asyncio.CancelledError
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay_seconds)
    except asyncio.TimeoutError:
        return
    raise asyncio.CancelledError


def plan_stream_retry(
    stream_retry_policy: Any,
    error_content: str,
    stream_attempt: int,
) -> tuple[int, float | None]:
    """Plan one policy-controlled transient stream retry."""
    decision = stream_retry_policy.decide_retry(error_content, stream_attempt)
    if not decision.should_retry:
        return stream_attempt, None
    new_attempt = stream_attempt + 1
    logger.warning("Retrying stream (%d): %s", new_attempt, error_content)
    return new_attempt, decision.delay_seconds
