from __future__ import annotations

import asyncio

from backend.agent.loop_runtime_helpers import (
    sleep_or_cancel as _sleep_or_cancel,
    terminal_reason_from_error_type as _terminal_reason_from_error_type,
)
from backend.agent.turn_kernel import (
    _terminal_run_error,
    _terminal_run_status,
    _terminal_run_summary,
)
from backend.agent.state import AgentState, TerminalReason


def test_terminal_reason_completed_maps_to_successful_run() -> None:
    assert _terminal_run_status("completed") == "completed"
    assert _terminal_run_summary("completed") == "Final answer committed"
    assert _terminal_run_error("completed") == ""


def test_terminal_reason_interrupted_maps_to_cancelled_run() -> None:
    assert _terminal_run_status("interrupted") == "cancelled"
    assert _terminal_run_summary("interrupted") == "Interrupted"
    assert _terminal_run_error("interrupted") == "cancelled"


def test_partial_reasons_map_to_partial_run() -> None:
    for reason in ("partial_timeout", "partial_api_error"):
        assert _terminal_run_status(reason) == "partial"
        assert _terminal_run_error(reason) == ""


def test_terminal_reason_other_maps_to_failed_run() -> None:
    assert _terminal_run_status("empty_reply") == "failed"
    assert _terminal_run_summary("empty_reply") == "Run ended: empty_reply"
    assert _terminal_run_error("empty_reply") == "empty_reply"


def test_missing_terminal_reason_preserves_unknown_result_as_partial() -> None:
    assert _terminal_run_status(None) == "partial"
    assert _terminal_run_summary(None) == "Partial result retained: unknown"
    assert _terminal_run_error(None) == ""


def test_agent_state_uses_shared_terminal_reason_vocabulary() -> None:
    state = AgentState(user_message="test")

    state.stopped_reason = "max_tool_calls"
    assert state.stopped_reason == "max_tool_calls"
    assert "empty_reply" in TerminalReason.__args__
    assert "max_retries" in TerminalReason.__args__
    assert "max_turn_seconds" in TerminalReason.__args__


def test_provider_error_types_map_to_terminal_reasons() -> None:
    assert _terminal_reason_from_error_type("billing") == "billing"
    assert _terminal_reason_from_error_type("auth") == "auth"
    assert _terminal_reason_from_error_type("unknown-provider-kind") == "api_error"


def test_sleep_or_cancel_returns_after_delay_without_cancel() -> None:
    asyncio.run(_sleep_or_cancel(0.001, asyncio.Event()))


def test_sleep_or_cancel_raises_when_cancel_event_is_set() -> None:
    async def run() -> str:
        event = asyncio.Event()
        event.set()
        try:
            await _sleep_or_cancel(5.0, event)
        except asyncio.CancelledError:
            return "cancelled"
        return "not-cancelled"

    assert asyncio.run(run()) == "cancelled"
