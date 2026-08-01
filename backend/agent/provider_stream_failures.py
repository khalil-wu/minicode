"""Typed terminal handling for failures raised while consuming provider output."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Callable

from backend.agent.loop_runtime_helpers import (
    format_llm_error,
    terminal_reason_from_error_type,
)
from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import add_usage
from backend.agent.recovery_controller import RecoveryProfile
from backend.agent.stream_sanitizer import scrub_thinking_tags
from backend.agent.turn_kernel import _set_terminal_reason
from backend.llm.base import UsageInfo
from backend.llm.errors import classify_llm_error


logger = logging.getLogger(__name__)


async def recover_stream_timeout(
    *,
    turn_kernel: Any,
    provider_attempt: Any,
    state: Any,
    context_builder: Any,
    turn_usage: UsageInfo,
    usage: UsageInfo,
    stream_state: Any,
    stream_text: Any,
    pending_tool_calls: list[Any],
    degrade_and_finish: Callable[..., AsyncIterator[AgentEvent]],
) -> AsyncIterator[AgentEvent]:
    await turn_kernel.close_provider_attempt(
        provider_attempt,
        status="failed",
        summary="Provider request timed out",
        data={"error_type": "timeout"},
    )
    async for recovery_event in degrade_and_finish(
        state=state,
        ctx=context_builder,
        usage=add_usage(turn_usage, usage),
        stream_text=stream_text,
        full_text=stream_text.pending_recovery_text(scrub_thinking_tags),
        pending_tool_calls=pending_tool_calls,
        profile=RecoveryProfile.timeout(
            saw_partial_tool_call=stream_state.saw_partial_tool_call,
        ),
    ):
        yield recovery_event


async def recover_provider_failure(
    exc: BaseException,
    *,
    turn_kernel: Any,
    provider_attempt: Any,
    state: Any,
    context_builder: Any,
    turn_usage: UsageInfo,
    usage: UsageInfo,
    stream_state: Any,
    stream_text: Any,
    pending_tool_calls: list[Any],
    degrade_and_finish: Callable[..., AsyncIterator[AgentEvent]],
) -> AsyncIterator[AgentEvent]:
    logger.error("Provider stream failed: %s", exc, exc_info=True)
    classification = classify_llm_error(exc)
    await turn_kernel.close_provider_attempt(
        provider_attempt,
        status="failed",
        summary="Provider request failed",
        data={
            "error_type": classification.error_type,
            "provider_error_type": classification.provider_error_type,
        },
    )
    async for recovery_event in degrade_and_finish(
        state=state,
        ctx=context_builder,
        usage=add_usage(turn_usage, usage),
        stream_text=stream_text,
        full_text=stream_text.pending_recovery_text(scrub_thinking_tags),
        pending_tool_calls=pending_tool_calls,
        profile=RecoveryProfile.provider_failure(
            failed_stopped_reason=(
                terminal_reason_from_error_type(classification.error_type)
                if classification.fatal
                else "api_error"
            ),
            error_message=format_llm_error(f"LLM API request failed: {exc}"),
            error_type=classification.error_type,
            recoverable=not classification.fatal,
            provider_error_type=classification.provider_error_type,
            saw_partial_tool_call=stream_state.saw_partial_tool_call,
        ),
    ):
        yield recovery_event


async def fail_provider_runtime(
    exc: Exception,
    *,
    turn_kernel: Any,
    provider_attempt: Any,
    state: Any,
) -> AsyncIterator[AgentEvent]:
    logger.error(
        "Agent runtime failed while processing provider output",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    await turn_kernel.close_provider_attempt(
        provider_attempt,
        status="failed",
        summary="Agent runtime failed while processing provider output",
        data={
            "error_type": "runtime",
            "exception_type": type(exc).__name__,
        },
    )
    _set_terminal_reason(state, "runtime_error", status="failed")
    yield AgentEvent.error(
        message=(
            "MiniCode 内部执行状态处理失败。本轮已停止，"
            "未按模型 API 错误自动重试。"
        ),
        recoverable=False,
        error_type="runtime",
    )
