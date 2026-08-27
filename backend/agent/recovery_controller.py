"""Shared recovery ladder for provider failures, timeouts and API errors."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.state import AgentState, TerminalReason
from backend.llm.base import ToolCallEvent, UsageInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveryProfile:
    partial_stopped_reason: TerminalReason
    failed_stopped_reason: TerminalReason
    error_message: str
    error_type: str
    recoverable: bool
    provider_error_type: str = ""
    allow_partial_text_commit: bool = True

    @classmethod
    def stream_interrupted(
        cls,
        *,
        error_message: str,
        error_type: str,
        failed_stopped_reason: TerminalReason,
        recoverable: bool,
        provider_error_type: str = "",
        saw_partial_tool_call: bool = False,
    ) -> "RecoveryProfile":
        return cls(
            partial_stopped_reason="partial_stream_error",
            failed_stopped_reason=failed_stopped_reason,
            error_message=error_message,
            error_type=error_type,
            recoverable=recoverable,
            provider_error_type=provider_error_type,
            allow_partial_text_commit=not saw_partial_tool_call,
        )

    @classmethod
    def timeout(
        cls,
        *,
        saw_partial_tool_call: bool = False,
    ) -> "RecoveryProfile":
        return cls(
            partial_stopped_reason="partial_timeout",
            failed_stopped_reason="timeout",
            error_message=(
                "Model response timed out. Tool results already collected remain in "
                "context; please retry."
            ),
            error_type="timeout",
            recoverable=True,
            allow_partial_text_commit=not saw_partial_tool_call,
        )

    @classmethod
    def provider_failure(
        cls,
        *,
        error_message: str,
        error_type: str,
        failed_stopped_reason: TerminalReason,
        recoverable: bool,
        provider_error_type: str = "",
        saw_partial_tool_call: bool = False,
    ) -> "RecoveryProfile":
        return cls(
            partial_stopped_reason="partial_api_error",
            failed_stopped_reason=failed_stopped_reason,
            error_message=error_message,
            error_type=error_type,
            recoverable=recoverable,
            provider_error_type=provider_error_type,
            allow_partial_text_commit=not saw_partial_tool_call,
        )


@dataclass(frozen=True, slots=True)
class RecoveryDependencies:
    scrub_thinking_tags: Callable[[str], str]
    usage_terminal_projection: Callable[..., TurnTerminalProjection]
    run_stop_failure_hook: Callable[..., Awaitable[None]]


class RecoveryController:
    """Own the three-tier degradation ladder without owning the main loop."""

    def __init__(
        self,
        *,
        state: AgentState,
        ctx: ContextBuilder,
        dependencies: RecoveryDependencies,
    ) -> None:
        self.state = state
        self.ctx = ctx
        self.dependencies = dependencies

    async def finish(
        self,
        *,
        usage: UsageInfo,
        stream_text: Any,
        full_text: str,
        pending_tool_calls: list[ToolCallEvent],
        profile: RecoveryProfile,
    ) -> AsyncIterator[AgentEvent | TurnTerminalProjection]:
        state = self.state
        ctx = self.ctx
        deps = self.dependencies

        if full_text and "<" in full_text:
            full_text = deps.scrub_thinking_tags(full_text)

        await deps.run_stop_failure_hook(
            profile.error_type,
            error_details=profile.error_message,
            last_assistant_message=full_text,
        )

        if (
            profile.allow_partial_text_commit
            and full_text.strip()
            and not pending_tool_calls
        ):
            ctx.append_assistant(full_text)
            state.reply = full_text
            state.stopped_reason = profile.partial_stopped_reason
            state.terminal_status = "partial"
            started = stream_text.start_agent_message()
            if started is not None:
                yield started
            completed = stream_text.complete_active_agent_message(
                full_text,
                source="partial",
                status="partial",
            )
            if completed is not None:
                yield completed
            yield deps.usage_terminal_projection(
                usage,
                status="partial",
                reason=profile.partial_stopped_reason,
            )
            return

        yield AgentEvent.error(
            message=profile.error_message,
            recoverable=profile.recoverable,
            error_type=profile.error_type,
            provider_error_type=profile.provider_error_type,
        )
        state.stopped_reason = profile.failed_stopped_reason
        state.terminal_status = "failed"
