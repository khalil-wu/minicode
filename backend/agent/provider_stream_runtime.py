"""Provider stream lifecycle, projection, retry, and typed failure handling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agent.first_byte_waiter import (
    ProviderStreamFailure,
)
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.agent.loop_runtime_helpers import (
    epoch_ms,
)
from backend.agent.message import AgentEvent
from backend.agent.provider_stream_event_dispatch import (
    ProviderDispatchResult,
    dispatch_provider_event,
)
from backend.agent.provider_stream_error_event import (
    ProviderErrorEventResult,
    handle_provider_error_event,
)
from backend.agent.provider_stream_control import (
    ProviderFallbackReset,
    reset_for_provider_fallback,
)
from backend.agent.provider_stream_wait import (
    ProviderWaitResult,
    wait_for_next_provider_event,
)
from backend.agent.provider_stream_settlement import (
    ProviderStreamAction,
    ProviderStreamSettlement,
    settle_provider_stream,
)
from backend.agent.provider_stream_failures import (
    fail_provider_runtime,
    recover_provider_failure,
    recover_stream_timeout,
)
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.stream_sanitizer import ThinkingStreamSanitizer
from backend.agent.tool_execution import StreamingToolExecutor
from backend.llm.base import (
    UsageInfo,
    safe_stream_chat_with_request_metadata,
)


logger = logging.getLogger(__name__)

Degrade = Callable[..., AsyncIterator[AgentEvent]]
ErrorRecovery = Callable[..., Awaitable[AgentEvent | None]]


@dataclass(frozen=True, slots=True)
class ProviderStreamResult:
    action: ProviderStreamAction
    stream_state: StreamAttemptState
    stream_text: StreamTextState
    tool_executor: StreamingToolExecutor
    turn_usage: UsageInfo
    usage: UsageInfo
    finish_reason: str
    response_phase: str
    thinking_chars: int
    prefer_stateful_history: bool


async def stream_provider_response(
    *,
    llm: Any,
    messages: list[Any],
    tool_schemas: list[dict[str, Any]],
    llm_request_metadata: dict[str, Any],
    prompt_cache_safe_params: dict[str, Any],
    prefer_stateful_history: bool,
    provider_completion: Any,
    state: Any,
    context_builder: Any,
    turn_kernel: Any,
    budget_runtime: Any,
    turn_usage: UsageInfo,
    settings: Any,
    tool_registry: Any,
    permission_checker: Any,
    effective_permission_context: Any,
    tool_context: Any,
    turn_start_tool_call_count: int,
    turn_started_at: float,
    iteration_limit: int,
    tool_batch_count: int,
    iteration_id_value: str,
    stream_retry_policy: Any,
    error_controller: Any,
    chain: Any,
    stream_text: StreamTextState,
    degrade_and_finish: Degrade,
    recover_withheld_error: ErrorRecovery,
) -> AsyncIterator[AgentEvent | ProviderStreamResult]:
    """Consume one provider response, including bounded retries and recovery."""

    stream_state = StreamAttemptState()
    pending_tool_calls = stream_state.tool_calls
    usage = stream_state.usage
    finish_reason = ""
    provider_raw_done = stream_state.raw_done
    stream_attempt = 0
    stream_recovery_attempted = False
    retry_budget_boundary = None
    rebuild_context_and_retry = False
    provider_response_phase = ""
    provider_stream_steered = False
    awaiting_trailing_tool_done = False
    tool_executor = StreamingToolExecutor(
        state=state,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        permission_context=effective_permission_context,
        tool_ctx=tool_context,
        execution_limit=(
            max(
                0,
                settings.max_tool_calls
                - (len(state.tool_calls) - turn_start_tool_call_count),
            )
            if settings.max_tool_calls > 0
            else None
        ),
    )
    thinking_chars = 0
    provider_attempt = None

    try:
        while True:
            should_retry = False
            thinking_chars = 0
            visible_text_sanitizer = ThinkingStreamSanitizer()
            budget_runtime.ensure_started()
            provider_attempt = await turn_kernel.start_provider_attempt(
                iteration_id=iteration_id_value,
                retry_index=stream_attempt,
                started_at=epoch_ms(),
            )
            stream_iter = safe_stream_chat_with_request_metadata(
                llm,
                messages,
                tools=tool_schemas,
                metadata=llm_request_metadata,
            ).__aiter__()
            first_event = True
            while True:
                wait_result = None
                async for wait_update in wait_for_next_provider_event(
                    stream_iter=stream_iter,
                    first_event=first_event,
                    settings=settings,
                    budget_runtime=budget_runtime,
                    tool_context=tool_context,
                    stream_state=stream_state,
                    pending_tool_calls=pending_tool_calls,
                    awaiting_trailing_tool_done=awaiting_trailing_tool_done,
                ):
                    if isinstance(wait_update, ProviderWaitResult):
                        wait_result = wait_update
                    else:
                        yield wait_update
                if wait_result is None:
                    raise RuntimeError(
                        "provider wait returned without a result"
                    )
                if wait_result.finish_reason:
                    finish_reason = wait_result.finish_reason
                if wait_result.response_phase:
                    provider_response_phase = wait_result.response_phase
                if wait_result.action == "finish":
                    break
                event = wait_result.event
                if event is None:
                    raise RuntimeError(
                        "provider wait produced an empty event"
                    )
                first_event = False
                dispatch_result = None
                async for dispatch_update in dispatch_provider_event(
                    event,
                    llm=llm,
                    provider_attempt=provider_attempt,
                    iteration_id_value=iteration_id_value,
                    state=state,
                    context_builder=context_builder,
                    turn_kernel=turn_kernel,
                    tool_context=tool_context,
                    stream_iter=stream_iter,
                    stream_state=stream_state,
                    stream_text=stream_text,
                    tool_executor=tool_executor,
                    settings=settings,
                    provider_completion=provider_completion,
                    prompt_cache_safe_params=prompt_cache_safe_params,
                    tool_batch_count=tool_batch_count,
                    iteration_limit=iteration_limit,
                    usage=usage,
                    finish_reason=finish_reason,
                    response_phase=provider_response_phase,
                    awaiting_trailing_tool_done=awaiting_trailing_tool_done,
                    visible_text_sanitizer=visible_text_sanitizer,
                    thinking_chars=thinking_chars,
                    prefer_stateful_history=prefer_stateful_history,
                ):
                    if isinstance(dispatch_update, ProviderDispatchResult):
                        dispatch_result = dispatch_update
                    else:
                        yield dispatch_update
                if dispatch_result is None:
                    raise RuntimeError(
                        "provider event dispatcher returned without a result"
                    )
                usage = dispatch_result.usage
                finish_reason = dispatch_result.finish_reason
                provider_response_phase = dispatch_result.response_phase
                awaiting_trailing_tool_done = (
                    dispatch_result.awaiting_trailing_tool_done
                )
                visible_text_sanitizer = (
                    dispatch_result.visible_text_sanitizer
                )
                thinking_chars = dispatch_result.thinking_chars
                prefer_stateful_history = (
                    dispatch_result.prefer_stateful_history
                )
                if dispatch_result.provider_stream_steered:
                    provider_stream_steered = True
                if dispatch_result.action == "break":
                    break
                if dispatch_result.action == "error":
                    error_result = None
                    async for error_update in handle_provider_error_event(
                        event,
                        state=state,
                        context_builder=context_builder,
                        turn_kernel=turn_kernel,
                        provider_attempt=provider_attempt,
                        stream_state=stream_state,
                        stream_text=stream_text,
                        pending_tool_calls=pending_tool_calls,
                        usage=usage,
                        turn_usage=turn_usage,
                        stream_retry_policy=stream_retry_policy,
                        stream_attempt=stream_attempt,
                        stream_recovery_attempted=stream_recovery_attempted,
                        budget_runtime=budget_runtime,
                        error_controller=error_controller,
                        iteration_id_value=iteration_id_value,
                        cancel_event=tool_context.cancel_event,
                        degrade_and_finish=degrade_and_finish,
                        recover_withheld_error=recover_withheld_error,
                    ):
                        if isinstance(error_update, ProviderErrorEventResult):
                            error_result = error_update
                        else:
                            yield error_update
                    if error_result is None:
                        raise RuntimeError(
                            "provider error handler returned without a result"
                        )
                    stream_attempt = error_result.stream_attempt
                    retry_budget_boundary = error_result.retry_budget_boundary
                    stream_recovery_attempted = (
                        error_result.stream_recovery_attempted
                    )
                    if error_result.action == "retry":
                        # A same-provider retry replaces the interrupted
                        # response just like a fallback restart. Discard the
                        # abandoned tool/text payload before opening the next
                        # stream; otherwise a partial input_json_delta leaves
                        # saw_partial_tool_call set and can make a later
                        # successful response fail as incomplete.
                        retry_reset = None
                        async for reset_update in reset_for_provider_fallback(
                            stream_text=stream_text,
                            stream_state=stream_state,
                            tool_executor=tool_executor,
                        ):
                            if isinstance(reset_update, ProviderFallbackReset):
                                retry_reset = reset_update
                            else:
                                yield reset_update
                        if retry_reset is None:
                            raise RuntimeError(
                                "provider retry reset returned without a result"
                            )
                        usage = retry_reset.usage
                        finish_reason = ""
                        provider_response_phase = ""
                        awaiting_trailing_tool_done = False
                        should_retry = True
                    elif error_result.action == "rebuild_context":
                        rebuild_context_and_retry = True
                    break

            if rebuild_context_and_retry:
                break
            if should_retry:
                continue
            break

    except asyncio.CancelledError:
        await turn_kernel.close_provider_attempt(
            provider_attempt,
            status="cancelled",
            summary="Provider request cancelled",
            data={"reason": "turn_cancelled"},
        )
        raise
    except PhaseDeadlineExceeded:
        boundary = budget_runtime.phase_deadline_boundary()
        logger.warning("%s: %s", boundary.label, boundary.detail)
        await turn_kernel.close_provider_attempt(
            provider_attempt,
            status="failed",
            summary="Turn wall-clock deadline reached",
            data={"error_type": "max_turn_seconds"},
        )
        retry_budget_boundary = boundary
    except asyncio.TimeoutError:
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
    except ProviderStreamFailure as stream_failure:
        async for recovery_event in recover_provider_failure(
            stream_failure.cause,
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
    except Exception as exc:
        async for failure_event in fail_provider_runtime(
            exc,
            turn_kernel=turn_kernel,
            provider_attempt=provider_attempt,
            state=state,
        ):
            yield failure_event

    settlement = None
    async for settlement_update in settle_provider_stream(
        retry_budget_boundary=retry_budget_boundary,
        budget_runtime=budget_runtime,
        turn_kernel=turn_kernel,
        provider_attempt=provider_attempt,
        finish_reason=finish_reason,
        provider_stream_steered=provider_stream_steered,
        rebuild_context_and_retry=rebuild_context_and_retry,
        state=state,
        pending_tool_calls=pending_tool_calls,
        provider_raw_done=provider_raw_done,
        stream_state=stream_state,
        stream_text=stream_text,
        context_builder=context_builder,
        usage=usage,
        turn_usage=turn_usage,
        chain=chain,
    ):
        if isinstance(settlement_update, ProviderStreamSettlement):
            settlement = settlement_update
        else:
            yield settlement_update
    if settlement is None:
        raise RuntimeError("provider stream settlement returned without a result")
    action = settlement.action
    turn_usage = settlement.turn_usage
    finish_reason = settlement.finish_reason

    yield ProviderStreamResult(
        action=action,
        stream_state=stream_state,
        stream_text=stream_text,
        tool_executor=tool_executor,
        turn_usage=turn_usage,
        usage=usage,
        finish_reason=finish_reason,
        response_phase=provider_response_phase,
        thinking_chars=thinking_chars,
        prefer_stateful_history=prefer_stateful_history,
    )
