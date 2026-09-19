"""Provider streaming lifetime."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from functools import partial
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from backend.agent.first_byte_waiter import (
    ProviderStreamFailure,
)
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.agent.model_execution import refresh_request_auth
from backend.agent.loop_runtime_helpers import (
    epoch_ms,
    is_max_output_finish_reason,
    format_llm_error,
)
from backend.agent.message import AgentEvent
from backend.agent.provider_stream_event_dispatch import (
    ProviderDispatchResult,
    dispatch_provider_event,
)
from backend.agent.provider_stream_error_event import (
    ProviderErrorEventResult,
    handle_provider_error_event,
    provider_error_details,
    committed_provider_error,
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
    ProviderStreamResult,
    ProviderStreamSettlement,
    settle_provider_stream,
    record_provider_attempt_usage,
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
from backend.llm.errors import classify_llm_error
from backend.llm.base import (
    StreamEventType,
    UsageInfo,
    safe_stream_chat_with_request_metadata,
)


Degrade = Callable[..., AsyncIterator[AgentEvent | TurnTerminalProjection]]
ErrorRecovery = Callable[..., Awaitable[AgentEvent | None]]


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
    tool_stream: Any | None = None,
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
    progress_owner = str(getattr(getattr(turn_kernel, "run_record", None), "run_id", "")
                         or turn_kernel.metadata.get("turn_id", "")).strip()
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
    stream_read_context = None

    def settle_attempt_usage(raw: dict[str, Any] | None = None) -> None:
        record_provider_attempt_usage(llm=llm, provider_attempt=provider_attempt, stream_state=stream_state,
            provider_raw_done=provider_raw_done, budget_runtime=budget_runtime, turn_usage=turn_usage, chain=chain, raw=raw)

    async def _close_stream() -> None:
        nonlocal stream_iter
        stream = stream_iter
        stream_iter = None
        await close_provider_stream(stream, read_context=stream_read_context)

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
            stream_read_context = copy_context()
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
                        read_context=stream_read_context,
                    ):
                        if isinstance(wait_update, ProviderWaitResult):
                            wait_result = wait_update
                        else:
                            yield wait_update
                except PhaseDeadlineExceeded:
                    raise
                except (asyncio.TimeoutError, ProviderStreamFailure) as failure:
                    settle_attempt_usage()
                    if stream_state.committed_tool_ids:
                        cause = failure.cause if isinstance(failure, ProviderStreamFailure) else failure
                        classification = classify_llm_error(cause)
                        if error := committed_provider_error(state, classification, format_llm_error(cause)):
                            yield error
                        break
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
                        switch_transport=getattr(llm, "try_fallback_transport", lambda: False),
                    ):
                        if isinstance(transport_update, ProviderTransportFailureResult):
                            transport_result = transport_update
                        else:
                            yield transport_update
                    if transport_result is None:
                        raise RuntimeError("provider transport handler returned without a result")
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
                if event.usage is not None:
                    usage = event.usage
                    stream_state.usage = usage
                    provider_attempt.usage_reported = True
                if event.type == StreamEventType.USAGE:
                    provider_raw_done.update(event.raw)
                if event.type in {StreamEventType.DONE, StreamEventType.ERROR}:
                    settle_attempt_usage(event.raw)
                run_context = getattr(tool_context, "run_context", None)
                auth_retry = (
                    not stream_state.saw_visible_output
                    and not stream_state.saw_provider_activity
                    and not stream_state.committed_tool_ids
                    and not stream_state.has_non_text_result
                    and callable(getattr(run_context, "refresh_model_auth", None))
                    and getattr(tool_context, "model_execution", None) is not None
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
                    close_stream=_close_stream,
                ):
                    if isinstance(dispatch_update, ProviderDispatchResult):
                        dispatch_result = dispatch_update
                    else:
                        yield dispatch_update
                if dispatch_result is None:
                    raise RuntimeError("provider event dispatcher returned without a result")
                if tool_stream is not None:
                    if event.type == StreamEventType.TOOL_CALL and event.tool_calls_committed:
                        await tool_stream.submit(event.tool_calls, stream_state, stream_text, tool_tracker)
                    elif event.type == StreamEventType.DONE and tool_stream.started and not is_max_output_finish_reason(event.finish_reason) and event.finish_reason not in {"pause_turn", "compaction"}:
                        await tool_stream.submit(stream_state.tool_calls, stream_state, stream_text, tool_tracker)
                usage = dispatch_result.usage
                finish_reason = dispatch_result.finish_reason
                provider_response_phase = dispatch_result.response_phase
                awaiting_trailing_tool_done = dispatch_result.awaiting_trailing_tool_done
                visible_text_sanitizer = dispatch_result.visible_text_sanitizer
                thinking_chars = dispatch_result.thinking_chars
                if dispatch_result.provider_stream_steered:
                    provider_stream_steered = True
                if dispatch_result.action == "break":
                    break
                if dispatch_result.action == "error":
                    if stream_state.committed_tool_ids:
                        _, classification, _ = provider_error_details(event)
                        if error := committed_provider_error(state, classification, event.content):
                            yield error
                        break
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
                        switch_transport=getattr(llm, "try_fallback_transport", lambda: False),
                        refresh_auth=partial(refresh_request_auth, tool_context, context_builder, budget_runtime) if auth_retry else None,
                        connection_retries_enabled=bool(getattr(settings, "stream_connection_retries_enabled", True)),
                    ):
                        if isinstance(error_update, ProviderErrorEventResult):
                            error_result = error_update
                        else:
                            yield error_update
                    if error_result is None:
                        raise RuntimeError("provider error handler returned without a result")
                    stream_attempt = error_result.stream_attempt
                    retry_budget_boundary = error_result.retry_budget_boundary
                    stream_recovery_attempted = error_result.stream_recovery_attempted
                    if error_result.action == "retry":
                        llm = getattr(tool_context, "llm", llm)
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

    except (asyncio.CancelledError, Exception) as exc:
        settle_attempt_usage()
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
        if isinstance(exc, asyncio.CancelledError):
            raise
        retry_budget_boundary = exception_result.retry_budget_boundary
    finally:
        # Consumer closure raises GeneratorExit; it still owns usage and cleanup.
        settle_attempt_usage()
        await _close_stream()

    if stream_state.committed_tool_ids and not state.stopped_reason and (
        not stream_state.provider_done or is_max_output_finish_reason(finish_reason)
    ):
        retry_budget_boundary = budget_runtime.consume_retry("provider_stream_after_tools")
        context_builder.append_user_context(
            "The provider response stopped after completed tool calls were accepted. Their results are retained. "
            "Continue from those results; do not repeat successful operations just to replay the interrupted response."
        )

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
