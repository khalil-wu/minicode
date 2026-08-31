"""Provider stream lifecycle, projection, retry, and typed failure handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agent.first_byte_waiter import (
    ProviderStreamFailure,
)
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
    ProviderRetryReset,
    reset_for_provider_retry,
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
    ProviderStreamExceptionResult,
    close_provider_stream,
    handle_provider_stream_exception,
)
from backend.agent.provider_attempt import provider_progress_id
from backend.agent.provider_stream_transport import (
    ProviderTransportFailureResult,
    handle_provider_transport_failure,
)
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.policies.stream_retry import StreamRetryState
from backend.agent.stream_sanitizer import ThinkingStreamSanitizer
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.tool_stream_tracker import StreamingToolTracker
from backend.llm.base import (
    UsageInfo,
    safe_stream_chat_with_request_metadata,
)


Degrade = Callable[..., AsyncIterator[AgentEvent | TurnTerminalProjection]]
ErrorRecovery = Callable[..., Awaitable[AgentEvent | None]]


@dataclass(frozen=True, slots=True)
class ProviderStreamResult:
    action: ProviderStreamAction
    stream_state: StreamAttemptState
    stream_text: StreamTextState
    tool_tracker: StreamingToolTracker
    turn_usage: UsageInfo
    usage: UsageInfo
    finish_reason: str
    response_phase: str
    thinking_chars: int


async def stream_provider_response(
    *,
    llm: Any,
    messages: list[Any],
    tool_schemas: list[dict[str, Any]],
    llm_request_metadata: dict[str, Any],
    prompt_cache_safe_params: dict[str, Any],
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
) -> AsyncIterator[AgentEvent | TurnTerminalProjection | ProviderStreamResult]:
    """Consume one provider response, including bounded retries and recovery."""

    stream_state = StreamAttemptState()
    pending_tool_calls = stream_state.tool_calls
    usage = stream_state.usage
    finish_reason = ""
    provider_raw_done = stream_state.raw_done
    stream_attempt = 0
    max_retries = max(
        0,
        int(getattr(settings, "stream_max_attempts", 0) or 0),
    )
    # A loop iteration id is local to a turn.  Include the durable run id in
    # the provider operation identity so a later turn cannot overwrite this
    # retry row during reconnect/hydration.
    progress_owner = str(
        getattr(getattr(turn_kernel, "run_record", None), "run_id", "")
        or getattr(turn_kernel, "metadata", {}).get("turn_id", "")
        or ""
    ).strip()
    provider_progress_key = provider_progress_id(
        iteration_id_value,
        progress_owner,
    )
    retry_state = StreamRetryState()
    query_source = str(llm_request_metadata.get("query_source") or "user").strip()
    stream_recovery_attempted = False
    retry_budget_boundary = None
    rebuild_context_and_retry = False
    provider_response_phase = ""
    provider_stream_steered = False
    awaiting_trailing_tool_done = False
    tool_tracker = StreamingToolTracker()
    thinking_chars = 0
    provider_attempt = None
    stream_iter: Any | None = None

    async def _close_stream() -> None:
        nonlocal stream_iter
        stream = stream_iter
        stream_iter = None
        await close_provider_stream(stream)

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
                max_retries=max_retries,
                progress_id=provider_progress_key,
            )
            stream_iter = safe_stream_chat_with_request_metadata(
                llm,
                messages,
                tools=tool_schemas,
                metadata=dict(llm_request_metadata),
            ).__aiter__()
            first_event = True
            while True:
                wait_result = None
                try:
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
                except (asyncio.TimeoutError, ProviderStreamFailure) as failure:
                    transport_result = None
                    async for transport_update in handle_provider_transport_failure(
                        failure,
                        settings=settings,
                        stream_text=stream_text,
                        pending_tool_calls=pending_tool_calls,
                        stream_state=stream_state,
                        stream_retry_policy=stream_retry_policy,
                        stream_attempt=stream_attempt,
                        turn_kernel=turn_kernel,
                        provider_attempt=provider_attempt,
                        budget_runtime=budget_runtime,
                        iteration_id_value=iteration_id_value,
                        stream_iter=stream_iter,
                        cancel_event=tool_context.cancel_event,
                        tool_tracker=tool_tracker,
                        state=state,
                        context_builder=context_builder,
                        turn_usage=turn_usage,
                        usage=usage,
                        degrade_and_finish=degrade_and_finish,
                        query_source=query_source,
                        retry_state=retry_state,
                        progress_id=provider_progress_key,
                        max_retries=max_retries,
                        close_stream=_close_stream,
                    ):
                        if isinstance(
                            transport_update,
                            ProviderTransportFailureResult,
                        ):
                            transport_result = transport_update
                        else:
                            yield transport_update
                    if transport_result is None:
                        raise RuntimeError(
                            "provider transport handler returned without a result"
                        )
                    usage = transport_result.usage
                    stream_attempt = transport_result.stream_attempt
                    retry_budget_boundary = transport_result.retry_budget_boundary
                    if transport_result.action == "retry":
                        finish_reason = ""
                        provider_response_phase = ""
                        awaiting_trailing_tool_done = False
                        should_retry = True
                    break
                if wait_result is None:
                    if (
                        should_retry
                        or retry_budget_boundary is not None
                        or state.stopped_reason
                    ):
                        break
                    raise RuntimeError("provider wait returned without a result")
                if wait_result.finish_reason:
                    finish_reason = wait_result.finish_reason
                if wait_result.response_phase:
                    provider_response_phase = wait_result.response_phase
                if wait_result.action == "finish":
                    break
                event = wait_result.event
                if event is None:
                    raise RuntimeError("provider wait produced an empty event")
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
                    tool_tracker=tool_tracker,
                    tool_registry=tool_registry,
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
                visible_text_sanitizer = dispatch_result.visible_text_sanitizer
                thinking_chars = dispatch_result.thinking_chars
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
                        max_retries=max_retries,
                        cancel_event=tool_context.cancel_event,
                        degrade_and_finish=degrade_and_finish,
                        recover_withheld_error=recover_withheld_error,
                        query_source=query_source,
                        retry_state=retry_state,
                        progress_id=provider_progress_key,
                        close_stream=_close_stream,
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
                    stream_recovery_attempted = error_result.stream_recovery_attempted
                    if error_result.action == "retry":
                        # A same-provider retry replaces the interrupted
                        # response. Discard the
                        # abandoned tool/text payload before opening the next
                        # stream; otherwise a partial input_json_delta leaves
                        # saw_partial_tool_call set and can make a later
                        # successful response fail as incomplete.
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
                await _close_stream()
                break
            if should_retry:
                await _close_stream()
                continue
            await _close_stream()
            break

    except asyncio.CancelledError as exc:
        exception_result = None
        async for exception_update in handle_provider_stream_exception(
            exc,
            close_stream=_close_stream,
            tool_tracker=tool_tracker,
            stream_state=stream_state,
            iteration_id_value=iteration_id_value,
            turn_kernel=turn_kernel,
            provider_attempt=provider_attempt,
            budget_runtime=budget_runtime,
            settings=settings,
            state=state,
            context_builder=context_builder,
            turn_usage=turn_usage,
            usage=usage,
            stream_text=stream_text,
            pending_tool_calls=pending_tool_calls,
            degrade_and_finish=degrade_and_finish,
        ):
            if isinstance(exception_update, ProviderStreamExceptionResult):
                exception_result = exception_update
            else:
                yield exception_update
        if exception_result is None or not exception_result.cancelled:
            raise RuntimeError(
                "provider exception handler returned without cancellation"
            )
        raise
    except Exception as exc:
        exception_result = None
        async for exception_update in handle_provider_stream_exception(
            exc,
            close_stream=_close_stream,
            tool_tracker=tool_tracker,
            stream_state=stream_state,
            iteration_id_value=iteration_id_value,
            turn_kernel=turn_kernel,
            provider_attempt=provider_attempt,
            budget_runtime=budget_runtime,
            settings=settings,
            state=state,
            context_builder=context_builder,
            turn_usage=turn_usage,
            usage=usage,
            stream_text=stream_text,
            pending_tool_calls=pending_tool_calls,
            degrade_and_finish=degrade_and_finish,
        ):
            if isinstance(exception_update, ProviderStreamExceptionResult):
                exception_result = exception_update
            else:
                yield exception_update
        if exception_result is None:
            raise RuntimeError("provider exception handler returned without a result")
        retry_budget_boundary = exception_result.retry_budget_boundary

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
        provider_done=stream_state.provider_done,
        visible_text_sanitizer=visible_text_sanitizer,
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
        tool_tracker=tool_tracker,
        turn_usage=turn_usage,
        usage=usage,
        finish_reason=finish_reason,
        response_phase=provider_response_phase,
        thinking_chars=thinking_chars,
    )
