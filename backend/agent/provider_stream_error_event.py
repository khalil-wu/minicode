"""Handle provider ERROR events without conflating them with runtime failures."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.loop_runtime_helpers import (
    format_llm_error,
    plan_stream_retry,
    sleep_or_cancel,
    terminal_reason_from_error_type,
)
from backend.agent.policies.stream_retry import plan_connection_retry
from backend.agent.message import AgentEvent
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.agent.provider_attempt import provider_progress_id
from backend.agent.recovery_controller import RecoveryProfile
from backend.agent.stream_sanitizer import scrub_thinking_tags
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.turn_kernel import _set_terminal_reason
from backend.llm.errors import (
    LLMErrorClassification,
    LLMErrorType,
    ProviderErrorType,
    classify_llm_error,
)


logger = logging.getLogger(__name__)

Degrade = Callable[..., AsyncIterator[AgentEvent | TurnTerminalProjection]]
ErrorRecovery = Callable[..., Awaitable[bool]]


def committed_provider_error(state, classification, message):
    """Keep fatal errors terminal after committed tools have been retained."""
    if not classification.fatal:
        return None
    _set_terminal_reason(state, terminal_reason_from_error_type(classification.error_type), status="failed")
    return AgentEvent.error(message, recoverable=True, error_type=classification.error_type)


@dataclass(frozen=True, slots=True)
class ProviderErrorEventResult:
    action: Literal["retry", "rebuild_context", "finish"]
    stream_attempt: int
    retry_budget_boundary: Any | None
    stream_recovery_attempted: bool


def provider_error_details(event: Any):
    """Classify the complete provider error envelope at one protocol boundary."""
    raw = getattr(event, "raw", {}) or {}
    classification_parts = [str(getattr(event, "content", "") or "")]
    provider_error_type = str(raw.get("provider_error_type") or "").strip()
    if provider_error_type:
        classification_parts.append(
            f"provider_error_type={provider_error_type}"
        )
    status_code = raw.get("status_code")
    if status_code is not None:
        classification_parts.append(f"status={status_code}")
    provider_error_code = str(raw.get("provider_error_code") or "").strip()
    if provider_error_code:
        classification_parts.append(
            f"provider_error_code={provider_error_code}"
        )
    provider_error_schema_type = str(
        raw.get("provider_error_schema_type") or ""
    ).strip()
    if provider_error_schema_type:
        classification_parts.append(
            f"provider_error_schema_type={provider_error_schema_type}"
        )
    classification_input = " ".join(classification_parts)
    classification = classify_llm_error(classification_input)
    wire_provider_error_type = str(raw.get("provider_error_type") or "").strip()
    wire_error_type = str(raw.get("error_type") or "").strip()
    if wire_provider_error_type:
        try:
            provider_kind = ProviderErrorType(wire_provider_error_type)
        except ValueError:
            provider_kind = None
        # A gateway 5xx page often reads as "blocked"; the status is the
        # stronger evidence and keeps the failure retryable. Structured kinds
        # (model, auth, billing, media, prompt size) still win over the status.
        text_derived = provider_kind in {ProviderErrorType.BLOCKED, ProviderErrorType.UNKNOWN}
        server_status = isinstance(status_code, int) and 500 <= status_code <= 599
        if provider_kind is not None and not (text_derived and server_status):
            wire_signal = (
                "media size"
                if provider_kind is ProviderErrorType.MEDIA_SIZE
                else "prompt too long"
                if provider_kind is ProviderErrorType.PROMPT_TOO_LONG
                else f"provider_error_type={provider_kind.value}"
            )
            wire_classification = classify_llm_error(wire_signal)
            try:
                error_kind = LLMErrorType(wire_error_type)
            except ValueError:
                error_kind = wire_classification.error_type
            classification = LLMErrorClassification(
                fatal=wire_classification.fatal,
                retryable=wire_classification.retryable,
                error_type=error_kind,
                provider_error_type=provider_kind,
            )
    provider_failure_data = {
        "error_type": classification.error_type,
        "provider_error_type": classification.provider_error_type,
        **({"status_code": status_code} if status_code is not None else {}),
        **({"provider_error_code": provider_error_code} if provider_error_code else {}),
        **(
            {"provider_error_schema_type": provider_error_schema_type}
            if provider_error_schema_type
            else {}
        ),
    }
    return classification_input, classification, provider_failure_data


async def handle_provider_error_event(
    event: Any,
    *,
    state: Any,
    context_builder: Any,
    turn_kernel: Any,
    provider_attempt: Any,
    stream_state: Any,
    stream_text: Any,
    pending_tool_calls: list[Any],
    usage: Any,
    turn_usage: Any,
    stream_retry_policy: Any,
    stream_attempt: int,
    stream_recovery_attempted: bool,
    budget_runtime: Any,
    error_controller: Any,
    iteration_id_value: str,
    cancel_event: Any,
    degrade_and_finish: Degrade,
    recover_withheld_error: ErrorRecovery,
    total_attempts: int = 0,
    max_retries: int | None = None,
    query_source: str | None = None,
    retry_state: Any | None = None,
    progress_id: str = "",
    close_stream: Callable[[], Awaitable[None]] | None = None,
    switch_transport: Callable[[], bool] | None = None,
    refresh_auth: Callable[[], Awaitable[bool]] | None = None,
    connection_retries_enabled: bool = True,
) -> AsyncIterator[AgentEvent | TurnTerminalProjection | ProviderErrorEventResult]:
    """Resolve a provider-declared stream error through the bounded ladder."""

    classification_input, classification, provider_failure_data = provider_error_details(event)
    raw = getattr(event, "raw", {}) or {}
    incomplete_tool_stream = stream_state.incomplete_tool_stream
    if (
        raw.get("status_code") == 401 and refresh_auth is not None
        and not retry_state.auth_recovery_attempted
        and not stream_state.saw_visible_output
        and not stream_state.saw_provider_activity
        and not stream_state.committed_tool_ids
        and not stream_state.has_non_text_result
        and stream_attempt < (max_retries if max_retries is not None else total_attempts)
    ):
        retry_state.auth_recovery_attempted = True
        boundary = budget_runtime.consume_retry("provider_auth_refresh")
        if boundary is not None:
            yield ProviderErrorEventResult("finish", stream_attempt, boundary, stream_recovery_attempted)
            return
        await turn_kernel.close_provider_attempt(
            provider_attempt, status="failed", summary="Provider rejected request credentials",
            data=provider_failure_data, project_progress=False,
        )
        if close_stream is not None:
            await close_stream()
        if await refresh_auth():
            yield AgentEvent.progress(
                "认证已刷新，正在重试请求。", stage="status", status="running",
                phase="recover", label="provider", provider_state="reconnecting",
                id=progress_id or provider_progress_id(iteration_id_value),
                retry_attempt=stream_attempt + 1, max_retries=max_retries,
            )
            yield ProviderErrorEventResult("retry", stream_attempt + 1, None, stream_recovery_attempted)
            return
    if (
        not stream_state.committed_tool_ids
        and not stream_state.has_non_text_result
        and not classification.fatal
        and classification.error_type not in {"prompt_too_long", "media_size"}
    ):
        # Adapters convert transport exceptions into ERROR events, so the
        # connect-phase verdict arrives as a flag on the event rather than as
        # an exception type. Nothing reached the provider: wait for the
        # network on its own schedule instead of spending the request budget.
        connection_retry = bool(
            raw.get("connect_phase")
            and retry_state is not None
            and connection_retries_enabled
            and not stream_state.saw_provider_activity
        )
        if connection_retry:
            new_attempt = stream_attempt
            retry_delay = plan_connection_retry(retry_state)
        else:
            new_attempt, retry_delay = plan_stream_retry(
                stream_retry_policy,
                classification_input,
                stream_attempt,
                query_source=query_source,
                retry_state=retry_state,
            )
        if retry_delay is not None:
            retry_limit = max_retries if max_retries is not None else total_attempts
            if not connection_retry and new_attempt > 1 and new_attempt == retry_limit and switch_transport is not None and switch_transport():
                yield AgentEvent.progress(
                    "WebSocket 持续中断，改用 HTTPS 重试。",
                    stage="status", status="running", phase="recover", label="provider",
                    provider_state="reconnecting", id=progress_id or provider_progress_id(iteration_id_value),
                )
            try:
                provider_retry_after = max(
                    0.0,
                    float(raw.get("retry_after_seconds") or 0.0),
                )
            except (TypeError, ValueError):
                provider_retry_after = 0.0
            if provider_retry_after > 0:
                retry_delay = max(retry_delay, provider_retry_after)
            await turn_kernel.close_provider_attempt(
                provider_attempt,
                status="failed",
                summary="Provider stream returned an error; retrying",
                data=provider_failure_data,
                project_progress=False,
            )
            effective_max_retries = max(
                0,
                int(
                    max_retries
                    if max_retries is not None
                    else total_attempts
                ),
            )
            retry_marker = (
                f"net{retry_state.connection_retries}"
                if connection_retry
                else str(new_attempt)
            )
            span_data = {
                "stream_attempt": new_attempt,
                "retry_attempt": None if connection_retry else new_attempt,
                "max_retries": None if connection_retry else effective_max_retries,
                "provider_error_type": classification.provider_error_type,
                "error_type": classification.error_type,
            }
            if connection_retry:
                span_data["connection_retry"] = True
                span_data["connection_retries"] = retry_state.connection_retries
            emit_runtime_span = getattr(turn_kernel, "emit_runtime_span", None)
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
                    summary=(
                        "Model transport unreachable; waiting for network"
                        if connection_retry
                        else "Model stream interrupted; retrying"
                    ),
                    data=span_data,
                )
            if connection_retry:
                yield AgentEvent.progress(
                    f"无法连接到模型服务，等待网络恢复（第 {retry_state.connection_retries} 次）",
                    stage="status",
                    status="running",
                    id=progress_id or provider_progress_id(iteration_id_value),
                    phase="recover",
                    label="provider",
                    count=retry_state.connection_retries,
                    detail=f"{classification_input[:320]} · {retry_delay:.1f} 秒后重试",
                    summary="Provider unreachable; waiting for network",
                    retry_after_ms=max(0, int(round(retry_delay * 1000))),
                    error_message=classification_input[:320],
                    operation_id=progress_id,
                    provider_state="reconnecting",
                    visibility="debug",
                )
            else:
                attempt_label = (
                    f"第 {new_attempt}/{effective_max_retries} 次"
                    if effective_max_retries > 0
                    else f"第 {new_attempt} 次"
                )
                yield AgentEvent.progress(
                    f"连接失败，正在重连（{attempt_label}）",
                    stage="status",
                    status="running",
                    id=progress_id or provider_progress_id(iteration_id_value),
                    phase="recover",
                    label="provider",
                    count=new_attempt,
                    detail=f"{classification_input[:320]} · {retry_delay:.1f} 秒后重试",
                    summary="Provider stream interrupted; retrying",
                    retry_attempt=new_attempt,
                    max_retries=effective_max_retries,
                    retry_after_ms=max(0, int(round(retry_delay * 1000))),
                    error_message=classification_input[:320],
                    operation_id=progress_id,
                    provider_state="reconnecting",
                    visibility="debug",
                )
            if classification.provider_error_type == "rate_limit":
                yield AgentEvent.rate_limit(
                    provider=str(raw.get("provider") or ""),
                    retry_after_seconds=retry_delay,
                    message="Provider rate limit reached; retrying after the requested delay.",
                )
            if close_stream is not None:
                await close_stream()
            # The retry policy/provider owns the delay. Do not add a local
            # random jitter layer: it makes the advertised Retry-After value
            # false and stacks a MiniCode-specific policy on top of provider
            # SDK behavior.
            wait_seconds, deadline_capped = budget_runtime.bounded_provider_timeout(retry_delay)
            await sleep_or_cancel(wait_seconds, cancel_event)
            if deadline_capped:
                raise PhaseDeadlineExceeded
            yield ProviderErrorEventResult(
                action="retry",
                stream_attempt=new_attempt,
                retry_budget_boundary=None,
                stream_recovery_attempted=stream_recovery_attempted,
            )
            return

    if not stream_recovery_attempted and not incomplete_tool_stream:
        recovered = await recover_withheld_error(
            error_controller=error_controller,
            classification=classification,
            error_content=event.content,
            state=state,
            ctx=context_builder,
        )
        if recovered:
            retry_boundary = budget_runtime.consume_retry(
                "error_withholding_recovery"
            )
            if retry_boundary is not None:
                logger.warning("%s", retry_boundary.detail)
                await turn_kernel.close_provider_attempt(
                    provider_attempt,
                    status="failed",
                    summary="Provider recovery budget exhausted",
                    data=provider_failure_data,
                )
                yield ProviderErrorEventResult(
                    action="finish",
                    stream_attempt=stream_attempt,
                    retry_budget_boundary=retry_boundary,
                    stream_recovery_attempted=stream_recovery_attempted,
                )
                return
            await turn_kernel.close_provider_attempt(
                provider_attempt,
                status="failed",
                summary="Provider request will be rebuilt for recovery",
                data=provider_failure_data,
                project_progress=False,
            )
            yield AgentEvent.progress(
                "正在调整请求上下文后重试",
                stage="status",
                status="running",
                id=progress_id or provider_progress_id(iteration_id_value),
                phase="recover",
                label="provider",
                detail=classification_input[:400],
                summary="Provider error recovery is rebuilding model context",
                retry_attempt=max(0, int(stream_attempt)),
                max_retries=max(
                    0,
                    int(max_retries if max_retries is not None else total_attempts),
                ),
                error_message=classification_input[:400],
                operation_id=progress_id,
                provider_state="reconnecting",
                visibility="debug",
            )
            yield ProviderErrorEventResult(
                action="rebuild_context",
                stream_attempt=stream_attempt,
                retry_budget_boundary=None,
                stream_recovery_attempted=True,
            )
            return

    await turn_kernel.close_provider_attempt(
        provider_attempt,
        status="failed",
        summary="Provider stream returned an error",
        data=provider_failure_data,
    )
    async for recovery_event in degrade_and_finish(
        state=state,
        ctx=context_builder,
        usage=turn_usage,
        stream_text=stream_text,
        full_text=stream_text.pending_recovery_text(scrub_thinking_tags),
        pending_tool_calls=pending_tool_calls,
        profile=RecoveryProfile.stream_interrupted(
            failed_stopped_reason=(
                "incomplete_tool_stream"
                if incomplete_tool_stream
                else terminal_reason_from_error_type(classification.error_type)
                if classification.fatal
                else "api_error"
            ),
            error_message=(
                "模型开始生成工具调用，但工具参数流没有完整结束。请重试本轮请求。"
                if incomplete_tool_stream
                else format_llm_error(event.content)
            ),
            error_type=(
                "incomplete_tool_stream"
                if incomplete_tool_stream
                else classification.error_type
            ),
            recoverable=True if incomplete_tool_stream else not classification.fatal,
            provider_error_type=classification.provider_error_type,
            saw_partial_tool_call=stream_state.saw_partial_tool_call,
        ),
    ):
        yield recovery_event
    yield ProviderErrorEventResult(
        action="finish",
        stream_attempt=stream_attempt,
        retry_budget_boundary=None,
        stream_recovery_attempted=stream_recovery_attempted,
    )
