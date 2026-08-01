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
from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import add_usage
from backend.agent.recovery_controller import RecoveryProfile
from backend.agent.stream_sanitizer import scrub_thinking_tags
from backend.llm.errors import classify_llm_error


logger = logging.getLogger(__name__)

Degrade = Callable[..., AsyncIterator[AgentEvent]]
ErrorRecovery = Callable[..., Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class ProviderErrorEventResult:
    action: Literal["retry", "rebuild_context", "finish"]
    stream_attempt: int
    retry_budget_boundary: Any | None
    stream_recovery_attempted: bool


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
) -> AsyncIterator[AgentEvent | ProviderErrorEventResult]:
    """Resolve a provider-declared stream error through the bounded ladder."""

    classification = classify_llm_error(event.content)
    await turn_kernel.close_provider_attempt(
        provider_attempt,
        status="failed",
        summary="Provider stream returned an error",
        data={
            "error_type": classification.error_type,
            "provider_error_type": classification.provider_error_type,
        },
    )
    incomplete_tool_stream = stream_state.incomplete_tool_stream
    if (
        not stream_text.full_text
        and not pending_tool_calls
        and not classification.fatal
        and classification.error_type not in {"prompt_too_long", "media_size"}
    ):
        new_attempt, retry_delay = plan_stream_retry(
            stream_retry_policy,
            event.content,
            stream_attempt,
        )
        if retry_delay is not None:
            provider_retry_after = float(
                (getattr(event, "raw", {}) or {}).get("retry_after_seconds") or 0.0
            )
            if provider_retry_after > 0:
                retry_delay = max(retry_delay, provider_retry_after)
            retry_boundary = budget_runtime.consume_retry("stream_error")
            if retry_boundary is not None:
                logger.warning("%s", retry_boundary.detail)
                yield ProviderErrorEventResult(
                    action="finish",
                    stream_attempt=stream_attempt,
                    retry_budget_boundary=retry_boundary,
                    stream_recovery_attempted=stream_recovery_attempted,
                )
                return
            await turn_kernel.emit_runtime_span(
                "recovery.retry.started",
                span_id=f"recovery:{iteration_id_value}:{new_attempt}",
                iteration_id=iteration_id_value,
                phase="recovery",
                status="running",
                label="recovery",
                summary="Model stream interrupted; retrying",
                data={
                    "stream_attempt": new_attempt,
                    "provider_error_type": classification.provider_error_type,
                    "error_type": classification.error_type,
                },
            )
            if classification.provider_error_type == "rate_limit":
                yield AgentEvent.rate_limit(
                    provider=str((getattr(event, "raw", {}) or {}).get("provider") or ""),
                    retry_after_seconds=retry_delay,
                    message="Provider rate limit reached; retrying after the requested delay.",
                )
            # The retry policy/provider owns the delay. Do not add a local
            # random jitter layer: it makes the advertised Retry-After value
            # false and stacks a MiniCode-specific policy on top of provider
            # SDK behavior.
            await sleep_or_cancel(retry_delay, cancel_event)
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
                yield ProviderErrorEventResult(
                    action="finish",
                    stream_attempt=stream_attempt,
                    retry_budget_boundary=retry_boundary,
                    stream_recovery_attempted=stream_recovery_attempted,
                )
                return
            yield ProviderErrorEventResult(
                action="rebuild_context",
                stream_attempt=stream_attempt,
                retry_budget_boundary=None,
                stream_recovery_attempted=True,
            )
            return

    async for recovery_event in degrade_and_finish(
        state=state,
        ctx=context_builder,
        usage=add_usage(turn_usage, usage),
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
