"""Typed terminal handling for failures raised while consuming provider output."""

from __future__ import annotations

import asyncio
from contextvars import Context
import logging
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Any, Callable

from backend.agent.first_byte_waiter import ProviderStreamFailure
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.agent.loop_runtime_helpers import (
    format_llm_error,
    terminal_reason_from_error_type,
)
from backend.agent.message import AgentEvent
from backend.agent.provider_text_projection import finish_provider_text_item
from backend.agent.loop_process_events import model_process_text_event
from backend.agent.progress import provider_failure_detail_progress
from backend.agent.provider_attempt import provider_progress_id
from backend.agent.recovery_controller import RecoveryProfile
from backend.agent.stream_sanitizer import scrub_thinking_tags
from backend.agent.tool_events import (
    abandoned_tool_announcement_events,
    cancelled_pending_tool_events,
)
from backend.agent.turn_kernel import _set_terminal_reason
from backend.llm.base import UsageInfo
from backend.llm.errors import classify_llm_error, llm_error_raw
from backend.secret_redaction import redact_secrets


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderStreamExceptionResult:
    """Outcome metadata returned after an outer stream exception is settled."""

    retry_budget_boundary: Any | None = None
    cancelled: bool = False


async def close_provider_stream(stream: Any | None, *, read_context: Context | None = None) -> None:
    """Detach and close a provider iterator without masking its root failure."""

    close = getattr(stream, "aclose", None) if stream is not None else None
    if callable(close):
        try:
            if read_context is None:
                await close()
            else:
                await asyncio.create_task(close(), context=read_context)
        except Exception:
            logger.debug("Provider stream close failed", exc_info=True)


async def finish_provider_stream(
    close_stream: Callable[[], Awaitable[None]],
    turn_kernel: Any,
    provider_attempt: Any,
) -> None:
    """Settle the active attempt when its consumer closes before completion."""
    try:
        await close_stream()
    finally:
        if provider_attempt is not None and not provider_attempt.closed:
            await turn_kernel.close_provider_attempt(
                provider_attempt,
                status="cancelled",
                summary="Provider stream closed before completion",
                project_progress=False,
            )


async def handle_provider_stream_exception(
    exc: BaseException,
    *,
    close_stream: Callable[[], Awaitable[None]],
    tool_tracker: Any,
    stream_state: Any,
    iteration_id_value: str,
    turn_kernel: Any,
    provider_attempt: Any,
    budget_runtime: Any,
    settings: Any,
    state: Any,
    context_builder: Any,
    turn_usage: UsageInfo,
    usage: UsageInfo,
    stream_text: Any,
    pending_tool_calls: list[Any],
    degrade_and_finish: Callable[..., AsyncIterator[AgentEvent]],
) -> AsyncIterator[AgentEvent | ProviderStreamExceptionResult]:
    """Own cleanup and typed recovery for exceptions escaping stream consumption."""

    if isinstance(exc, asyncio.CancelledError):
        await close_stream()
        for abandoned in abandoned_tool_announcement_events(
            stream_state,
            iteration_id=iteration_id_value,
        ):
            yield abandoned
        for cancelled in cancelled_pending_tool_events(
            stream_state,
            tool_tracker,
            iteration_id=iteration_id_value,
        ):
            yield cancelled
        tool_tracker.cancel_remaining()
        await turn_kernel.close_provider_attempt(
            provider_attempt,
            status="cancelled",
            summary="Provider request cancelled",
            data={"reason": "turn_cancelled"},
        )
        yield ProviderStreamExceptionResult(cancelled=True)
        return

    await close_stream()
    tool_tracker.cancel_remaining()
    if isinstance(exc, PhaseDeadlineExceeded):
        boundary = budget_runtime.phase_deadline_boundary()
        logger.warning("%s: %s", boundary.label, boundary.detail)
        await turn_kernel.close_provider_attempt(
            provider_attempt,
            status="failed",
            summary="Turn wall-clock deadline reached",
            data={"error_type": "max_turn_seconds"},
        )
        yield ProviderStreamExceptionResult(retry_budget_boundary=boundary)
        return

    if isinstance(exc, asyncio.TimeoutError):
        logger.warning(
            "LLM stream timeout: %ss",
            settings.stream_timeout_seconds,
        )
        async for recovery_event in recover_stream_timeout(
            turn_kernel=turn_kernel,
            provider_attempt=provider_attempt,
            state=state,
            context_builder=context_builder,
            turn_usage=turn_usage,
            usage=usage,
            stream_state=stream_state,
            stream_text=stream_text,
            pending_tool_calls=pending_tool_calls,
            degrade_and_finish=degrade_and_finish,
        ):
            yield recovery_event
        yield ProviderStreamExceptionResult()
        return

    if isinstance(exc, ProviderStreamFailure):
        async for recovery_event in recover_provider_failure(
            exc.cause,
            turn_kernel=turn_kernel,
            provider_attempt=provider_attempt,
            state=state,
            context_builder=context_builder,
            turn_usage=turn_usage,
            usage=usage,
            stream_state=stream_state,
            stream_text=stream_text,
            pending_tool_calls=pending_tool_calls,
            degrade_and_finish=degrade_and_finish,
        ):
            yield recovery_event
        yield ProviderStreamExceptionResult()
        return

    if not isinstance(exc, Exception):
        raise exc
    async for failure_event in fail_provider_runtime(
        exc,
        turn_kernel=turn_kernel,
        provider_attempt=provider_attempt,
        state=state,
    ):
        yield failure_event
    yield ProviderStreamExceptionResult()


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
        usage=turn_usage,
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
    provider_message = llm_error_raw(exc, provider="").get("provider_error_message")
    if provider_message:
        yield provider_failure_detail_progress(provider_message, attempt=provider_attempt,
            progress_id=provider_progress_id(provider_attempt.iteration_id))
    async for recovery_event in degrade_and_finish(
        state=state,
        ctx=context_builder,
        usage=turn_usage,
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
    error = AgentEvent.error(
        message=(
            "MiniCode 内部执行状态处理失败。本轮已停止，未按模型 API 错误自动重试。\n"
            + redact_secrets(str(exc))
        ),
        recoverable=False,
        error_type="runtime",
    )
    await turn_kernel.close_provider_attempt(
        provider_attempt,
        status="failed",
        summary="Agent runtime failed while processing provider output",
        data={
            "error_type": "runtime",
            "exception_type": type(exc).__name__,
            "error_message": error.data["message"],
        },
    )
    _set_terminal_reason(state, "runtime_error", status="failed")
    yield error


@dataclass(frozen=True)
class ProviderExceptionBranchResult:
    """Retry-boundary state after one provider attempt settled through its exception path."""

    retry_budget_boundary: Any


async def run_stream_exception_branch(
    exc: BaseException,
    *,
    request_completed: bool,
    model_hooks: Any,
    tool_stream: Any | None,
    settle_attempt_usage: Any,
    stream_state: Any,
    stream_text: Any,
    visible_text_sanitizer: Any,
    settings: Any,
    awaiting_trailing_tool_done: bool,
    close_stream: Any,
    tool_tracker: Any,
    iteration_id_value: str,
    turn_kernel: Any,
    provider_attempt: Any,
    budget_runtime: Any,
    state: Any,
    context_builder: Any,
    turn_usage: Any,
    usage: Any,
    pending_tool_calls: Any,
    degrade_and_finish: Any,
) -> AsyncIterator[Any]:
    """Settle a cancelled or failed attempt and report how the caller continues."""
    await model_hooks.settle_exception(exc, completed=request_completed, usage=usage,
                                       reported=provider_attempt is not None and provider_attempt.usage_reported)
    if isinstance(exc, asyncio.CancelledError):
        if tool_stream is not None:
            await tool_stream.abort_deferred()
        async for projected in finish_provider_text_item(
            stream_state=stream_state, stream_text=stream_text,
            visible_text_sanitizer=visible_text_sanitizer,
            live_text_streaming=settings.live_text_streaming,
            awaiting_trailing_done=awaiting_trailing_tool_done,
            process_event_factory=model_process_text_event,
        ):
            yield projected
    settle_attempt_usage()
    exception_result = None
    async for exception_update in handle_provider_stream_exception(
        exc, close_stream=close_stream, tool_tracker=tool_tracker, stream_state=stream_state,
        iteration_id_value=iteration_id_value, turn_kernel=turn_kernel, provider_attempt=provider_attempt,
        budget_runtime=budget_runtime, settings=settings, state=state, context_builder=context_builder,
        turn_usage=turn_usage, usage=usage, stream_text=stream_text, pending_tool_calls=pending_tool_calls,
        degrade_and_finish=degrade_and_finish,
    ):
        if isinstance(exception_update, ProviderStreamExceptionResult):
            exception_result = exception_update
        else:
            yield exception_update
    if exception_result is None:
        raise RuntimeError("provider exception handler returned without a result")
    if isinstance(exc, asyncio.CancelledError):
        raise
    yield ProviderExceptionBranchResult(retry_budget_boundary=exception_result.retry_budget_boundary)
