"""Canonical validation of a candidate turn terminal outcome."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


_FAILED_TOOL_STATUSES = {
    "error",
    "failed",
    "blocked",
    "timeout",
    "cancelled",
}


@dataclass(frozen=True, slots=True)
class TerminalValidation:
    status: str
    reason: str
    changed: bool = False
    message: str = ""
    recoverable: bool = False


def validate_terminal_outcome(
    *,
    status: str,
    reason: str,
    reply: str,
    tool_statuses: Iterable[str] = (),
    has_non_text_result: bool = False,
) -> TerminalValidation:
    """Reject a false completed result before the durable terminal commit.

    A completed coding-agent turn needs a model-authored answer or an explicit
    non-text result.  Tool evidence alone is useful but is not a final answer;
    preserve it as ``partial`` unless a tool failed, in which case the turn is
    ``failed``.  This rule belongs to the harness lifecycle, not a UI transport.
    """

    normalized_status = str(status or "completed").strip().lower()
    normalized_reason = str(reason or "").strip()
    if (
        normalized_status != "completed"
        or normalized_reason not in {"", "completed"}
        or str(reply or "").strip()
        or has_non_text_result
    ):
        return TerminalValidation(normalized_status, normalized_reason)

    statuses = [str(value or "").strip().lower() for value in tool_statuses]
    if statuses:
        failed_tools = any(value in _FAILED_TOOL_STATUSES for value in statuses)
        return TerminalValidation(
            status="failed" if failed_tools else "partial",
            reason="missing_final_answer",
            changed=True,
            message=(
                "Tool calls failed before the model produced a final response."
                if failed_tools
                else "The model ended after tool execution without producing a final response."
            ),
            recoverable=False,
        )

    return TerminalValidation(
        status="failed",
        reason="missing_final_answer",
        changed=True,
        message="The model ended without producing a final response.",
        recoverable=True,
    )
