"""
Agent Loop - Single-loop + Recovery-ladder architecture.

The loop has one authoritative lifecycle:
  1. Context Pipeline  (before the call)
  2. Streaming Execution (during the call)
  3. Recovery Paths    (after the call)
  4. Termination Conditions (when to stop)
  5. State Threading   (across iterations)

The model decides: tool_calls -> execute -> loop; no tool_calls -> done.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.error_withholding import ErrorWithholdingController
from backend.agent.message import AgentEvent
from backend.agent.loop_bootstrap import (
    AgentLoopBootstrap,
    AgentLoopBootstrapRequest,
    bootstrap_agent_loop,
)
from backend.agent.loop_components import build_agent_loop_components
from backend.agent.loop_hook_projection import apply_hook_results
from backend.agent.state import AgentState
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.stream_sanitizer import scrub_thinking_tags as _scrub_thinking_tags
from backend.agent.turn_kernel import (
    TurnKernel,
    _set_terminal_reason,
)
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.turn_iteration_admission import (
    IterationAdmissionResult,
    TurnIterationAdmission,
)
from backend.agent.turn_iteration_execution import (
    IterationExecutionResult,
    IterationExecutionState,
    TurnIterationExecutor,
)
from backend.agent.stream_attempt import StreamTextState
from backend.agent.terminal_projection import (
    TurnTerminalProjection,
    terminal_boundary_events,
    terminal_status_and_reason,
)
from backend.agent.terminal_validation import validate_terminal_outcome
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter, LLMTurnContext
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = ["AgentLoopSessionContext", "run_agent_loop"]


# Main loop


async def run_agent_loop(
    user_message: str,
    llm: LLMAdapter,
    tool_registry: ToolRegistry,
    artifact_store: ArtifactStore,
    permission_checker: PermissionChecker,
    agent_settings: AgentSettings | None = None,
    token_budget: TokenBudget | None = None,
    context_builder: ContextBuilder | None = None,
    state: AgentState | None = None,
    approval_handler: Callable | None = None,
    skill_manager: Any | None = None,
    permission_context: PermissionContext | None = None,
    session_id: str = "",
    task_id: str = "",
    task_manager: Any | None = None,
    background_manager: Any | None = None,
    stream_callback: Any | None = None,
    emit_event: Any | None = None,
    metadata: dict[str, Any] | None = None,
    run_context: RunContext | None = None,
    session_context: AgentLoopSessionContext | None = None,
    turn_kernel: TurnKernel | None = None,
    state_prepared: bool = False,
    initial_max_iterations_limit: int | None = None,
    turn_budget_controller: TurnBudgetController | None = None,
) -> AsyncIterator[AgentEvent]:
    """
    Agent Loop - single while-true with recovery ladder.

    The model decides: has tool_calls -> execute -> loop; no tool_calls -> done.
    """
    # Preserve host metadata and establish the single durable runtime owner.
    if metadata is None and session_context is not None and not state_prepared:
        metadata = session_context.metadata
    metadata = metadata if metadata is not None else {}
    run_context = run_context or (
        session_context.run_context
        if session_context is not None
        else None
    ) or RunContext()
    runtime_value = run_context.agent_runtime
    if runtime_value is None:
        runtime_value = AgentRuntime()
    elif not isinstance(runtime_value, AgentRuntime):
        raise TypeError("RunContext agent_runtime must be an AgentRuntime")
    run_context.agent_runtime = runtime_value
    if run_context.llm_turn_context is None:
        side_call_records = metadata.setdefault("_side_calls", [])
        run_context.llm_turn_context = LLMTurnContext(
            side_call_records=side_call_records,
            cost_session_id=str(run_context.cost_session_id or session_id or ""),
            lifecycle_runtime=run_context.lifecycle_runtime,
        )
    bootstrap = None
    async for bootstrap_update in bootstrap_agent_loop(
        AgentLoopBootstrapRequest(
            user_message=user_message,
            llm=llm,
            tool_registry=tool_registry,
            artifact_store=artifact_store,
            permission_checker=permission_checker,
            agent_settings=agent_settings,
            token_budget=token_budget,
            context_builder=context_builder,
            state=state,
            approval_handler=approval_handler,
            skill_manager=skill_manager,
            permission_context=permission_context,
            session_id=session_id,
            task_id=task_id,
            task_manager=task_manager,
            background_manager=background_manager,
            stream_callback=stream_callback,
            emit_event=emit_event,
            metadata=metadata,
            run_context=run_context,
            session_context=session_context,
            turn_kernel=turn_kernel,
            state_prepared=state_prepared,
            initial_max_iterations_limit=initial_max_iterations_limit,
            turn_budget_controller=turn_budget_controller,
        )
    ):
        if isinstance(bootstrap_update, AgentLoopBootstrap):
            bootstrap = bootstrap_update
        else:
            yield bootstrap_update
    if bootstrap is None:
        raise RuntimeError("agent loop bootstrap returned without a result")
    user_message = bootstrap.user_message
    skill_manager = bootstrap.skill_manager
    emit_event = bootstrap.emit_event
    metadata = bootstrap.metadata
    run_context = bootstrap.run_context
    external_metadata = bootstrap.external_metadata
    cancel_event = bootstrap.cancel_event
    settings = bootstrap.settings
    budget = bootstrap.budget
    ctx = bootstrap.context
    state = bootstrap.state
    deadline_controller = bootstrap.deadline_controller
    turn_kernel = bootstrap.turn_kernel
    runtime = turn_kernel.runtime
    run_record = turn_kernel.run_record
    turn_started_at = bootstrap.turn_started_at
    preflight_deadline_reached = bootstrap.preflight_deadline_reached
    chain = bootstrap.chain
    stream_retry_policy = bootstrap.stream_retry_policy
    effective_permission_context = bootstrap.effective_permission_context
    tool_ctx = bootstrap.tool_context
    permission_checker = bootstrap.permission_checker
    session_id = bootstrap.session_id
    task_id = bootstrap.task_id
    stream_text = StreamTextState()
    initial_user_turn_pending = not bool(
        metadata.get("_query_engine_recovery_restored")
        or metadata.get("_turn_admission_restored")
    )
    llm_turn_context = run_context.llm_turn_context
    if not isinstance(llm_turn_context, LLMTurnContext):
        raise TypeError("RunContext llm_turn_context must be an LLMTurnContext")
    turn_usage = llm_turn_context.usage
    terminal_projection: TurnTerminalProjection | None = None
    saw_non_text_result = False

    try:
        for event in apply_hook_results(
            context=ctx,
            user_message=user_message,
            hook_results=(
                bootstrap.session_hook_result,
                bootstrap.prompt_hook_result,
            ),
        ):
            yield event
        if bootstrap.preflight_blocked:
            message = bootstrap.preflight_block_message or "User prompt blocked by hook"
            _set_terminal_reason(state, "blocked", status="failed")
            yield AgentEvent.error(
                message,
                recoverable=True,
                error_type="hook",
                error_code="user_prompt_blocked",
            )
            for event in terminal_boundary_events(
                turn_kernel=turn_kernel,
                session_id=session_id,
                user_message=user_message,
                state=state,
                context_builder=ctx,
                usage=turn_usage,
                terminal_projection=terminal_projection,
                status="failed",
                reason="blocked",
            ):
                yield event
            return

        turn_start_tool_call_count = 0
        tool_batch_count = 0

        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError

        # Do not start more preflight work after references or hooks consumed
        # the absolute turn allowance. The loop boundary below owns the normal
        # terminal fallback decision for that exhausted turn.
        components = build_agent_loop_components(
            bootstrap=bootstrap,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            usage=lambda: turn_usage,
            skill_manager=(skill_manager if not preflight_deadline_reached else None),
        )
        pending_turn_context = components.pending_turn_context
        turn_tool_schema_state = components.turn_tool_schema_state
        iteration_runtime = components.iteration_runtime
        turn_start_tool_call_count = components.turn_start_tool_call_count
        turn_budget_controller = components.turn_budget_controller
        budget_runtime = components.budget_runtime
        answer_committer = components.answer_committer
        llm_request_metadata = components.llm_request_metadata
        provider_completion = components.provider_completion
        iteration_admission = TurnIterationAdmission(
            context=ctx,
            state=state,
            llm=llm,
            iteration_runtime=iteration_runtime,
            deadline_controller=deadline_controller,
            turn_budget_controller=turn_budget_controller,
            budget_runtime=budget_runtime,
            turn_start_tool_call_count=turn_start_tool_call_count,
            chain=chain,
            tool_context=tool_ctx,
            token_budget=budget,
            metadata=metadata,
            external_metadata=external_metadata,
            emit_event=emit_event,
            runtime=runtime,
            run_record=run_record,
            llm_request_metadata=llm_request_metadata,
            turn_kernel=turn_kernel,
        )

        # Phase 2: Main loop (the kernel)
        error_controller = ErrorWithholdingController()
        degraded_reason = ""
        iteration_executor = TurnIterationExecutor(
            llm=llm,
            llm_request_metadata=llm_request_metadata,
            provider_completion=provider_completion,
            state=state,
            context_builder=ctx,
            turn_kernel=turn_kernel,
            budget_runtime=budget_runtime,
            settings=settings,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            effective_permission_context=effective_permission_context,
            tool_context=tool_ctx,
            turn_start_tool_call_count=turn_start_tool_call_count,
            turn_started_at=turn_started_at,
            stream_retry_policy=stream_retry_policy,
            error_controller=error_controller,
            user_message=user_message,
            chain=chain,
            approval_handler=approval_handler,
            skill_manager=skill_manager,
            runtime=runtime,
            run_record=run_record,
            metadata=metadata,
            deadline_controller=deadline_controller,
            answer_committer=answer_committer,
            emit_event=emit_event,
            turn_budget_controller=turn_budget_controller,
        )
        iteration_execution_state = IterationExecutionState(
            turn_usage=turn_usage,
            tool_batch_count=tool_batch_count,
            degraded_reason=degraded_reason,
            stream_text=stream_text,
        )
        deferred_cancel: asyncio.CancelledError | None = None
        try:
            while True:
                # Admission awaits hooks, compaction, mailbox delivery and
                # context construction. Cancelling there must not reuse the
                # previous iteration's already-committed stream state.
                stream_text = StreamTextState()
                iteration_execution_state.stream_text = stream_text
                iteration_admission_result = None
                async for iteration_admission_update in iteration_admission.admit(
                    previous_tool_schema_state=turn_tool_schema_state,
                    initial_turn_pending=initial_user_turn_pending,
                    pending_turn_context=pending_turn_context,
                ):
                    if isinstance(iteration_admission_update, IterationAdmissionResult):
                        iteration_admission_result = iteration_admission_update
                    elif isinstance(iteration_admission_update, TurnTerminalProjection):
                        terminal_projection = iteration_admission_update
                    else:
                        if iteration_admission_update.type == "image_chunk":
                            saw_non_text_result = True
                        yield iteration_admission_update
                if iteration_admission_result is None:
                    raise RuntimeError(
                        "iteration admission returned without a result"
                    )
                active_llm = iteration_runtime.llm
                iteration_executor.llm = active_llm
                iteration_admission.llm = active_llm
                iteration_executor.tool_registry = iteration_runtime.tool_registry
                turn_tool_schema_state = iteration_admission_result.tool_schema_state
                initial_user_turn_pending = (
                    iteration_admission_result.initial_turn_pending
                )
                if iteration_admission_result.action == "retry":
                    continue
                if iteration_admission_result.action == "terminate":
                    break
                tool_schemas = iteration_admission_result.tool_schemas
                messages = iteration_admission_result.messages
                prompt_cache_safe_params = (
                    iteration_admission_result.prompt_cache_safe_params
                )
                if (
                    tool_schemas is None
                    or messages is None
                    or prompt_cache_safe_params is None
                ):
                    raise RuntimeError(
                        "iteration admission proceeded without provider inputs"
                    )
                iteration_id = iteration_admission_result.iteration_id

                iteration_execution_result = None
                async for iteration_execution_update in iteration_executor.execute(
                    messages=messages,
                    tool_schemas=tool_schemas,
                    prompt_cache_safe_params=prompt_cache_safe_params,
                    iteration_id=iteration_id,
                    execution_state=iteration_execution_state,
                ):
                    if isinstance(
                        iteration_execution_update,
                        IterationExecutionResult,
                    ):
                        iteration_execution_result = iteration_execution_update
                    elif isinstance(iteration_execution_update, TurnTerminalProjection):
                        terminal_projection = iteration_execution_update
                    else:
                        if iteration_execution_update.type == "image_chunk":
                            saw_non_text_result = True
                        yield iteration_execution_update
                if iteration_execution_result is None:
                    raise RuntimeError(
                        "iteration executor returned without a result"
                    )
                iteration_execution_state = iteration_execution_result.state
                turn_usage = iteration_execution_state.turn_usage
                tool_batch_count = iteration_execution_state.tool_batch_count
                degraded_reason = (
                    iteration_execution_state.degraded_reason
                )
                stream_text = iteration_execution_result.stream_text
                if iteration_execution_result.action == "retry":
                    continue
                break

        except asyncio.CancelledError as exc:
            interrupt_events = turn_kernel.interrupt(
                context_builder=ctx,
                stream_text=iteration_execution_state.stream_text,
                scrub_text=_scrub_thinking_tags,
            )
            for interrupt_event in interrupt_events:
                yield interrupt_event
            # Defer propagation until the single terminal boundary below has
            # persisted the checkpoint and emitted its receipt.
            deferred_cancel = exc

        validation = validate_terminal_outcome(
            status=str(state.terminal_status or "completed"),
            reason=str(state.stopped_reason or ""),
            reply=state.reply,
            tool_statuses=(record.status for record in state.tool_calls),
            has_non_text_result=saw_non_text_result,
        )
        if validation.changed:
            _set_terminal_reason(
                state,
                validation.reason,
                status=validation.status,
            )
            state.mark_transition(validation.reason)
            yield AgentEvent.error(
                validation.message,
                recoverable=validation.recoverable,
                error_type="missing_final_answer",
                error_code="agent.missing_final_answer",
            )

        # The loop exit is the sole normal terminal transition. Inner provider,
        # admission, and budget paths only set terminal state and report evidence.
        # QueryEngine owns the only durable terminal CAS. The provider/tool loop
        # publishes intent and evidence, then drains completely so checkpoint
        # cleanup cannot race the outer transaction coordinator.
        terminal_status, terminal_reason = terminal_status_and_reason(
            state=state,
            terminal_projection=terminal_projection,
        )
        for event in terminal_boundary_events(
            turn_kernel=turn_kernel,
            session_id=session_id,
            user_message=user_message,
            state=state,
            context_builder=ctx,
            usage=turn_usage,
            terminal_projection=terminal_projection,
            status=terminal_status,
            reason=terminal_reason,
        ):
            yield event

        if deferred_cancel is not None:
            raise deferred_cancel
    except asyncio.CancelledError:
        raise
    except Exception:
        # Every run crosses the durable ``running`` boundary in TurnKernel.create.
        # An unexpected admission, provider, tool, or final-answer exception must
        # therefore become one canonical failed terminal transition instead of
        # escaping with a permanently running record and no public done event.
        logger.exception("Unhandled MiniCode agent-loop failure")
        if not turn_kernel.completion_emitted:
            _set_terminal_reason(state, "runtime_error", status="failed")
            yield AgentEvent.error(
                "MiniCode agent loop failed unexpectedly.",
                recoverable=False,
                error_type="agent_loop",
                error_code="agent_loop.runtime_error",
            )
            for event in terminal_boundary_events(
                turn_kernel=turn_kernel,
                session_id=session_id,
                user_message=user_message,
                state=state,
                context_builder=ctx,
                usage=turn_usage,
                terminal_projection=terminal_projection,
                status="failed",
                reason="runtime_error",
            ):
                yield event
