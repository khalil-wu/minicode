"""Commit an accepted final answer to history and terminal runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from backend.agent.answer_commit_projection import AnswerCommitProjection
from backend.agent.provider_protocol import usage_terminal_projection
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.llm.base import UsageInfo


@dataclass(frozen=True, slots=True)
class AnswerCommitDependencies:
    context: Any
    state: Any
    append_assistant_history: Callable[..., None]
    set_terminal_reason: Callable[..., Any]


class AnswerCommitter:
    """Commit accepted answer history and return an internal terminal intent."""

    def __init__(self, dependencies: AnswerCommitDependencies) -> None:
        self._deps = dependencies

    def commit_answer(
        self,
        *,
        projection: AnswerCommitProjection,
        final_text: str,
        provider_phase: str,
        provider_items: list[dict[str, Any]],
        usage: UsageInfo,
        provider_raw: dict[str, Any],
        finish_reason: str,
    ) -> TurnTerminalProjection:
        deps = self._deps
        state = deps.state
        if final_text:
            deps.append_assistant_history(
                deps.context,
                final_text,
                phase=provider_phase or "final_answer",
                provider_items=provider_items,
            )
            state.reply = (
                str(getattr(state, "max_output_partial_text", "") or "")
                + final_text
            )

        terminal_reason = projection.terminal_reason
        deps.set_terminal_reason(
            state,
            terminal_reason,
            status=projection.terminal_status,
        )
        projected_provider_raw = dict(provider_raw)
        if finish_reason:
            projected_provider_raw["finish_reason"] = str(finish_reason)
        return usage_terminal_projection(
            usage,
            provider_raw=projected_provider_raw,
            status=projection.terminal_status,
            reason="" if terminal_reason == "completed" else terminal_reason,
        )
