"""Bound recovery dependencies shared by provider-stream failure paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.loop_recovery import (
    degrade_and_finish as run_degrade_and_finish,
    emergency_compact,
    quarantine_external_web_results,
    strip_historical_media,
    try_error_withholding_recovery,
)
from backend.agent.message import AgentEvent
from backend.agent.loop_preflight import run_stop_failure_hook
from backend.agent.provider_protocol import usage_done_event
from backend.agent.recovery_controller import RecoveryDependencies, RecoveryProfile
from backend.agent.state import AgentState
from backend.agent.stream_sanitizer import scrub_thinking_tags
from backend.llm.base import ToolCallEvent, UsageInfo


async def recover_withheld_error(
    *,
    error_controller: Any,
    classification: Any,
    error_content: str,
    state: AgentState,
    ctx: ContextBuilder,
    compact: Any = emergency_compact,
) -> bool:
    return await try_error_withholding_recovery(
        error_controller=error_controller,
        classification=classification,
        error_content=error_content,
        state=state,
        context=ctx,
        compact=compact,
        strip_media=strip_historical_media,
        quarantine_web=quarantine_external_web_results,
    )


async def degrade_and_finish(
    *,
    state: AgentState,
    ctx: ContextBuilder,
    usage: UsageInfo,
    stream_text: Any,
    full_text: str,
    pending_tool_calls: list[ToolCallEvent],
    profile: RecoveryProfile,
) -> AsyncIterator[AgentEvent]:
    async for event in run_degrade_and_finish(
        state=state,
        context=ctx,
        usage=usage,
        stream_text=stream_text,
        full_text=full_text,
        pending_tool_calls=pending_tool_calls,
        profile=profile,
        dependencies=RecoveryDependencies(
            scrub_thinking_tags=scrub_thinking_tags,
            usage_done_event=usage_done_event,
            run_stop_failure_hook=run_stop_failure_hook,
        ),
    ):
        yield event
