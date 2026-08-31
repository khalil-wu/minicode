"""An approval wait must not outlive the turn's wall-clock deadline."""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.agent.tool_batch_execution import _await_approval_within_turn_deadline
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext


def _tool_call() -> ToolCallEvent:
    return ToolCallEvent(
        id="call_1", name="write_file", arguments={"file_path": "a.txt"}
    )


def _context(deadline_monotonic: float | None) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(),
        deadline_monotonic=deadline_monotonic,
    )


def test_approval_without_deadline_waits_for_the_handler() -> None:
    async def handler(tool_call_id: str) -> dict[str, str]:
        return {"action": "approve", "tool_call_id": tool_call_id}

    result = asyncio.run(
        _await_approval_within_turn_deadline(handler, _tool_call(), _context(None))
    )

    assert result["action"] == "approve"


def test_approval_is_rejected_when_the_turn_deadline_passes() -> None:
    async def never_answers(_tool_call_id: str) -> dict[str, str]:
        await asyncio.sleep(30)
        return {"action": "approve"}

    async def run() -> dict[str, str]:
        # A deadline just far enough ahead to prove the wait is bounded by it
        # rather than by the handler's own (much longer) timeout.
        context = _context(time.monotonic() + 0.05)
        return await _await_approval_within_turn_deadline(
            never_answers, _tool_call(), context
        )

    started = time.monotonic()
    result = asyncio.run(run())
    elapsed = time.monotonic() - started

    assert result["action"] == "reject"
    assert "time budget" in result["guidance"]
    assert elapsed < 5, "the wait must end at the deadline, not at the handler timeout"


def test_approval_is_rejected_without_waiting_once_the_deadline_is_gone() -> None:
    calls: list[str] = []

    async def handler(tool_call_id: str) -> dict[str, str]:
        calls.append(tool_call_id)
        return {"action": "approve"}

    result = asyncio.run(
        _await_approval_within_turn_deadline(
            handler, _tool_call(), _context(time.monotonic() - 1)
        )
    )

    assert result["action"] == "reject"
    assert calls == [], "an expired turn must not open a new approval prompt"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_approval_decision_passes_through_before_the_deadline(action: str) -> None:
    async def handler(_tool_call_id: str) -> dict[str, str]:
        return {"action": action}

    result = asyncio.run(
        _await_approval_within_turn_deadline(
            handler, _tool_call(), _context(time.monotonic() + 30)
        )
    )

    assert result["action"] == action
