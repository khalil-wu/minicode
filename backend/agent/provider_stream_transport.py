"""Retry and terminal recovery for provider transport failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.first_byte_waiter import ProviderStreamFailure
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
from backend.llm.base import UsageInfo
from backend.llm.errors import (
    classify_llm_error,
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
    safe_to_replay = (
        not stream_text.full_text
        and not pending_tool_calls
        and not stream_state.saw_partial_tool_call
    )
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
        if emit_runtime_span is not None:
            await emit_runtime_span(
                "recovery.retry.started",
                span_id=(
                    f"recovery:{provider_attempt.span_id}:{new_attempt}"
                    if getattr(provider_attempt, "span_id", "")
                    else f"recovery:{iteration_id_value}:{new_attempt}"
                ),
                iteration_id=iteration_id_value,
                phase="recovery",
                status="running",
                label="recovery",
                summary=(
                    "Model stream timed out; reconnecting"
                    if is_timeout
                    else "Model stream disconnected; reconnecting"
                ),
                data={
                    "stream_attempt": new_attempt,
                    "retry_attempt": new_attempt,
                    "max_retries": max(
                        0,
                        int(
                            max_retries
                            if max_retries is not None
                            else getattr(settings, "stream_max_attempts", 0) or 0
                        ),
                    ),
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
        retry_label = (
            f"第 {new_attempt}/{effective_max_retries} 次"
            if effective_max_retries > 0
            else f"第 {new_attempt} 次"
        )
        yield AgentEvent.progress(
            f"连接中断，正在重连（{retry_label}）",
            stage="status",
            status="running",
            id=progress_id or provider_progress_id(iteration_id_value),
            phase="recover",
            label="provider",
            count=new_attempt,
            detail=f"{error_message[:320]} · {retry_delay:.1f} 秒后重试",
            summary=(
                "Model stream timed out; reconnecting"
                if is_timeout
                else "Model stream disconnected; reconnecting"
            ),
            retry_attempt=new_attempt,
            max_retries=effective_max_retries,
            retry_after_ms=max(0, int(round(retry_delay * 1000))),
            error_message=error_message[:320],
            operation_id=progress_id,
            provider_state="reconnecting",
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
        await sleep_or_cancel(retry_delay, cancel_event)
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
