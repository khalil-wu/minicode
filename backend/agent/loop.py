"""
Agent Loop - Single-loop + Recovery-ladder architecture.

Inspired by Claude Code's single-query-loop pattern:
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
from backend.agent.skill_activation import activate_turn_skills
from backend.agent.loop_bootstrap import (
    AgentLoopBootstrap,
    AgentLoopBootstrapRequest,
    bootstrap_agent_loop,
)
from backend.agent.loop_components import build_agent_loop_components
from backend.agent.state import AgentState
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.stream_sanitizer import scrub_thinking_tags as _scrub_thinking_tags
from backend.agent.turn_kernel import (
    TurnKernel,
    _set_terminal_reason,
)
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
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter, UsageInfo
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = ["AgentLoopSessionContext", "run_agent_loop"]


# Constants

# Stop hook feedback may inject more than once (cc stopHookActive allows
# multiple rounds); a small count cap guards against a prompt-too-long death
# loop when a hook keeps blocking.
_STOP_HOOK_FEEDBACK_LIMIT = 3


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
    session_id = bootstrap.session_id
    task_id = bootstrap.task_id
    stream_text = StreamTextState()
    initial_user_turn_pending = not bool(
        metadata.get("_query_engine_recovery_restored")
    )
    turn_usage = UsageInfo()
    from backend.llm.cost_tracker import CostTracker
    _cost_session_token = CostTracker.bind_session(bootstrap.cost_session_id)
    # Bind side-call usage (for example compaction) to the current turn.
    _turn_usage_token = LLMAdapter.bind_turn_usage(turn_usage)

    try:
        for hook_result in (
            bootstrap.session_hook_result,
            bootstrap.prompt_hook_result,
        ):
            system_message = str(
                getattr(hook_result, "system_message", "") or ""
            ).strip()
            if system_message:
                yield AgentEvent(
                    type="system_notice",
                    data={"content": system_message},
                )
        if bootstrap.preflight_blocked:
            message = bootstrap.preflight_block_message or "User prompt blocked by hook"
            _set_terminal_reason(state, "blocked", status="failed")
            yield AgentEvent.error(
                message,
                recoverable=True,
                error_type="hook",
                error_code="user_prompt_blocked",
            )
            completed_event = turn_kernel.complete_for_terminal_reason("blocked")
            if completed_event is not None:
                yield completed_event
            yield AgentEvent.done(status="failed", reason="blocked")
            turn_kernel.finalize_checkpoint(
                session_id=session_id,
                user_message=user_message,
                state=state,
                context_builder=ctx,
            )
            return

        turn_start_tool_call_count = 0
        tool_batch_count = 0

        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError

        # Do not start more preflight work after references or hooks consumed
        # the absolute turn allowance. The loop boundary below owns the normal
        # terminal fallback decision for that exhausted turn.
        if skill_manager and not preflight_deadline_reached:
            try:
                async for skill_event in activate_turn_skills(skill_manager, user_message, state):
                    yield skill_event
            except asyncio.CancelledError:
                _set_terminal_reason(state, "interrupted", status="cancelled")
                raise

        components = build_agent_loop_components(
            bootstrap=bootstrap,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            usage=lambda: turn_usage,
        )
        pending_turn_context = components.pending_turn_context
        turn_tool_schema_state = components.turn_tool_schema_state
        iteration_runtime = components.iteration_runtime
        turn_start_tool_call_count = components.turn_start_tool_call_count
        turn_budget_controller = components.turn_budget_controller
        budget_runtime = components.budget_runtime
        answer_committer = components.answer_committer
        llm_request_metadata = components.llm_request_metadata
        prefer_stateful_history = components.prefer_stateful_history
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
            stop_hook_feedback_limit=_STOP_HOOK_FEEDBACK_LIMIT,
        )
        iteration_execution_state = IterationExecutionState(
            turn_usage=turn_usage,
            tool_batch_count=tool_batch_count,
            prefer_stateful_history=prefer_stateful_history,
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
                    else:
                        yield iteration_admission_update
                if iteration_admission_result is None:
                    raise RuntimeError(
                        "iteration admission returned without a result"
                    )
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
                    else:
                        yield iteration_execution_update
                if iteration_execution_result is None:
                    raise RuntimeError(
                        "iteration executor returned without a result"
                    )
                iteration_execution_state = iteration_execution_result.state
                turn_usage = iteration_execution_state.turn_usage
                tool_batch_count = iteration_execution_state.tool_batch_count
                prefer_stateful_history = (
                    iteration_execution_state.prefer_stateful_history
                )
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
            turn_kernel.finalize_checkpoint(
                session_id=session_id,
                user_message=user_message,
                state=state,
                context_builder=ctx,
            )
            for interrupt_event in interrupt_events:
                yield interrupt_event
            # Defer propagation until checkpoint persistence below has flushed the
            # current messages/tool evidence. This makes deadline resume real.
            deferred_cancel = exc

        # The loop exit is the sole normal terminal transition. Inner provider,
        # admission, and budget paths only set terminal state and report evidence.
        completed_event = turn_kernel.complete_for_terminal_reason(state.stopped_reason)
        if completed_event is not None:
            yield completed_event

        if deferred_cancel is None:
            turn_kernel.finalize_checkpoint(
                session_id=session_id,
                user_message=user_message,
                state=state,
                context_builder=ctx,
            )
        if deferred_cancel is not None:
            raise deferred_cancel
    finally:
        if bootstrap.hook_manager_token is not None:
            from backend.hooks.manager import unbind_hook_manager

            unbind_hook_manager(bootstrap.hook_manager_token)
        LLMAdapter.unbind_turn_usage(_turn_usage_token)
        CostTracker.unbind_session(_cost_session_token)
