"""Retry and terminal recovery for provider transport failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.first_byte_waiter import ProviderStreamFailure
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.agent.loop_runtime_helpers import plan_stream_retry, sleep_or_cancel
from backend.agent.message import AgentEvent
from backend.agent.provider_attempt import provider_progress_id
from backend.agent.provider_stream_control import (
    ProviderRetryReset,
    reset_for_provider_retry,
)
from backend.agent.provider_stream_failures import (
    recover_provider_failure,
    recover_stream_timeout,
)
from backend.agent.policies.stream_retry import (
    StreamRetryState,
    plan_connection_retry,
)
from backend.llm.base import UsageInfo
from backend.llm.errors import (
    classify_llm_error,
    is_connection_not_established,
    llm_error_status_code,
    retry_after_seconds,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderTransportFailureResult:
    action: Literal["retry", "finish"]
    stream_attempt: int
    retry_budget_boundary: Any | None
    usage: UsageInfo


async def handle_provider_transport_failure(
    failure: asyncio.TimeoutError | ProviderStreamFailure,
    *,
    settings: Any,
    stream_text: Any,
    pending_tool_calls: list[Any],
    stream_state: Any,
    stream_retry_policy: Any,
    stream_attempt: int,
    turn_kernel: Any,
    provider_attempt: Any,
    budget_runtime: Any,
    iteration_id_value: str,
    stream_iter: Any,
    cancel_event: Any,
    tool_tracker: Any,
    state: Any,
    context_builder: Any,
    turn_usage: UsageInfo,
    usage: UsageInfo,
    degrade_and_finish: Callable[..., AsyncIterator[AgentEvent]],
    query_source: str | None = None,
    retry_state: Any | None = None,
    progress_id: str = "",
    max_retries: int | None = None,
    close_stream: Callable[[], Awaitable[None]] | None = None,
    switch_transport: Callable[[], bool] | None = None,
) -> AsyncIterator[AgentEvent | ProviderTransportFailureResult]:
    """Retry a replay-safe transport failure or finish through typed recovery."""

    is_timeout = isinstance(failure, asyncio.TimeoutError)
    cause = failure if is_timeout else failure.cause
    classification = classify_llm_error(cause)
    error_parts = [
        f"stream timeout after {settings.stream_timeout_seconds}s"
        if is_timeout
        else str(cause)
    ]
    if classification.provider_error_type != "unknown":
        error_parts.append(f"provider_error_type={classification.provider_error_type}")
    status_code = llm_error_status_code(cause)
    if status_code is not None:
        error_parts.append(f"status={status_code}")
    error_message = " ".join(error_parts)
    # Text is speculative until the attempt is accepted. Only committed tool
    # effects and non-text results prevent replay; visible text is retracted by
    # reset_for_provider_retry before the next attempt is exposed.
    safe_to_replay = (
        not stream_state.committed_tool_ids
        and not stream_state.has_non_text_result
    )
    connection_retry_state = retry_state if retry_state is not None else StreamRetryState()
    # A connect-phase failure is waited out instead of budgeted. Nothing reached
    # the provider, so reissuing the same attempt cannot replay a poisoned
    # request, and the cause is almost always the user's network coming back.
    # This is decided before the request retry policy so the two budgets stay
    # independent: only the unconnectable case is unbounded.
    connection_retry = bool(
        safe_to_replay
        and not is_timeout
        and getattr(settings, "stream_connection_retries_enabled", True)
        and is_connection_not_established(cause)
    )
    if connection_retry:
        retry_delay = plan_connection_retry(connection_retry_state)
        # The request's retry ordinal does not advance: no request-level retry
        # was spent, so the UI must not advertise a count it did not consume.
        new_attempt = stream_attempt
    else:
        new_attempt, retry_delay = (
            plan_stream_retry(
                stream_retry_policy,
                error_message,
                stream_attempt,
                query_source=query_source,
                retry_state=retry_state,
            )
            if safe_to_replay
            else (stream_attempt, None)
        )
    if safe_to_replay and retry_delay is not None:
        # Reserve the last existing retry for HTTP when WS repeatedly fails.
        # This does not extend the retry budget or replay committed effects.
        retry_limit = max_retries if max_retries is not None else settings.stream_max_attempts
        if (
            not connection_retry
            and new_attempt > 1
            and new_attempt == retry_limit
            and switch_transport is not None
            and switch_transport()
        ):
            yield AgentEvent.progress(
                "WebSocket 持续中断，改用 HTTPS 重试。",
                stage="status", status="running", phase="recover", label="provider",
                provider_state="reconnecting", id=progress_id or provider_progress_id(iteration_id_value),
            )
        if not is_timeout:
            provider_retry_after = retry_after_seconds(cause)
            if provider_retry_after > 0:
                retry_delay = max(retry_delay, provider_retry_after)
        error_type = "timeout" if is_timeout else classification.error_type
        provider_error_type = (
            "network" if is_timeout else classification.provider_error_type
        )
        # Provider retries are owned exclusively by StreamRetryPolicy.  The
        # turn recovery fuse handles context/loop repairs, so it must not
        # impose a second, smaller provider budget that would make the UI's
        # advertised N inaccurate.
        await turn_kernel.close_provider_attempt(
            provider_attempt,
            status="failed",
            summary=(
                "Provider request timed out; retrying"
                if is_timeout
                else "Provider stream transport failed; retrying"
            ),
            data={
                "error_type": error_type,
                "provider_error_type": provider_error_type,
                **({"status_code": status_code} if status_code is not None else {}),
            },
            project_progress=False,
        )
        emit_runtime_span = getattr(turn_kernel, "emit_runtime_span", None)
        # A connect-phase wait does not advance the request retry ordinal, so
        # its own counter keeps each attempt's span and row distinct instead of
        # collapsing them onto the unchanged ordinal.
        retry_marker = (
            f"net{connection_retry_state.connection_retries}"
            if connection_retry
            else str(new_attempt)
        )
        span_summary = (
            "Model transport unreachable; waiting for network"
            if connection_retry
            else "Model stream timed out; reconnecting"
            if is_timeout
            else "Model stream disconnected; reconnecting"
        )
        if emit_runtime_span is not None:
            await emit_runtime_span(
                "recovery.retry.started",
                span_id=(
                    f"recovery:{provider_attempt.span_id}:{retry_marker}"
                    if getattr(provider_attempt, "span_id", "")
                    else f"recovery:{iteration_id_value}:{retry_marker}"
                ),
                iteration_id=iteration_id_value,
                phase="recovery",
                status="running",
                label="recovery",
                summary=span_summary,
                data={
                    "stream_attempt": stream_attempt,
                    "retry_attempt": None if connection_retry else new_attempt,
                    "max_retries": (
                        None
                        if connection_retry
                        else max(
                            0,
                            int(
                                max_retries
                                if max_retries is not None
                                else getattr(settings, "stream_max_attempts", 0) or 0
                            ),
                        )
                    ),
                    "connection_retry": connection_retry,
                    "connection_retries": connection_retry_state.connection_retries,
                    "provider_error_type": provider_error_type,
                    "error_type": error_type,
                },
            )
        effective_max_retries = max(
            0,
            int(
                max_retries
                if max_retries is not None
                else getattr(settings, "stream_max_attempts", 0) or 0
            ),
        )
        if connection_retry:
            # No ordinal: the wait is unbounded, so advertising N/M would claim
            # a budget that is not being spent.
            progress_message = "无法连接提供商，正在等待网络恢复"
            progress_summary = span_summary
            progress_retry_attempt = None
            progress_max_retries = None
        else:
            retry_label = (
                f"第 {new_attempt}/{effective_max_retries} 次"
                if effective_max_retries > 0
                else f"第 {new_attempt} 次"
            )
            progress_message = f"连接中断，正在重连（{retry_label}）"
            progress_summary = span_summary
            progress_retry_attempt = new_attempt
            progress_max_retries = effective_max_retries
        yield AgentEvent.progress(
            progress_message,
            stage="status",
            status="running",
            id=progress_id or provider_progress_id(iteration_id_value),
            phase="recover",
            label="provider",
            count=None if connection_retry else new_attempt,
            detail=f"{error_message[:320]} · {retry_delay:.1f} 秒后重试",
            summary=progress_summary,
            retry_attempt=progress_retry_attempt,
            max_retries=progress_max_retries,
            retry_after_ms=max(0, int(round(retry_delay * 1000))),
            error_message=error_message[:320],
            operation_id=progress_id,
            provider_state="reconnecting",
            visibility="debug",
        )
        if close_stream is not None:
            await close_stream()
        else:
            close_iterator = getattr(stream_iter, "aclose", None)
            if callable(close_iterator):
                with suppress(Exception):
                    await close_iterator()
        if classification.provider_error_type == "rate_limit":
            yield AgentEvent.rate_limit(
                retry_after_seconds=retry_delay,
                message="Provider rate limit reached; retrying after the requested delay.",
            )
        wait_seconds, deadline_capped = budget_runtime.bounded_provider_timeout(retry_delay)
        await sleep_or_cancel(wait_seconds, cancel_event)
        if deadline_capped:
            raise PhaseDeadlineExceeded
        retry_reset = None
        async for reset_update in reset_for_provider_retry(
            stream_text=stream_text,
            stream_state=stream_state,
            tool_tracker=tool_tracker,
        ):
            if isinstance(reset_update, ProviderRetryReset):
                retry_reset = reset_update
            else:
                yield reset_update
        if retry_reset is None:
            raise RuntimeError("provider retry reset returned without a result")
        yield ProviderTransportFailureResult(
            action="retry",
            stream_attempt=new_attempt,
            retry_budget_boundary=None,
            usage=retry_reset.usage,
        )
        return

    if is_timeout:
        logger.warning("LLM stream timeout: %ss", settings.stream_timeout_seconds)
        recovery_events = recover_stream_timeout(
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
        )
    else:
        recovery_events = recover_provider_failure(
            cause,
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
        )
    async for recovery_event in recovery_events:
        yield recovery_event
    yield ProviderTransportFailureResult(
        action="finish",
        stream_attempt=stream_attempt,
        retry_budget_boundary=None,
        usage=usage,
    )
