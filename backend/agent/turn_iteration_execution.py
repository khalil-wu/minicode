"""Execute the provider/recovery/decision half of one Agent iteration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.final_answer_orchestrator import (
    FinalAnswerOutcome,
    orchestrate_final_answer,
)
from backend.agent.message import AgentEvent
from backend.agent.provider_response_recovery import (
    PostStreamRecoveryResult,
    recover_provider_response,
)
from backend.agent.provider_stream_runtime import (
    ProviderStreamResult,
    stream_provider_response,
)
from backend.agent.stream_attempt import StreamTextState
from backend.agent.stream_sanitizer import scrub_thinking_tags
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.swarm_store import MAILBOX_MESSAGE_LEASE_MS
from backend.agent.tool_turn_runtime import ToolTurnResult, execute_tool_turn
from backend.agent.turn_recovery_runtime import (
    degrade_and_finish,
    recover_withheld_error,
)


def _exposed_tool_names(tool_schemas: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for schema in tool_schemas:
        if not isinstance(schema, dict):
            continue
        function = schema.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if name:
            names.add(name)
    return names


@dataclass(slots=True)
class IterationExecutionState:
    turn_usage: Any
    tool_batch_count: int
    degraded_reason: str
    stream_text: StreamTextState


@dataclass(slots=True)
class IterationExecutionResult:
    action: Literal["retry", "terminate"]
    state: IterationExecutionState
    stream_text: StreamTextState


class TurnIterationExecutor:
    """Compose the already-isolated provider, recovery, answer and tool runtimes.

    The outer loop owns turn lifetime.  This object owns one admitted model
    iteration and returns only the state needed by the next iteration.
    """

    def __init__(
        self,
        *,
        llm: Any,
        llm_request_metadata: dict[str, Any],
        provider_completion: Any,
        state: Any,
        context_builder: Any,
        turn_kernel: Any,
        budget_runtime: Any,
        settings: Any,
        tool_registry: Any,
        permission_checker: Any,
        effective_permission_context: Any,
        tool_context: Any,
        turn_start_tool_call_count: int,
        turn_started_at: float,
        stream_retry_policy: Any,
        error_controller: Any,
        user_message: str,
        chain: Any,
        approval_handler: Any,
        skill_manager: Any,
        runtime: Any,
        run_record: Any,
        metadata: dict[str, Any],
        deadline_controller: Any,
        answer_committer: Any,
        emit_event: Any,
        turn_budget_controller: Any,
    ) -> None:
        self.llm = llm
        self.llm_request_metadata = llm_request_metadata
        self.provider_completion = provider_completion
        self.agent_state = state
        self.context_builder = context_builder
        self.turn_kernel = turn_kernel
        self.budget_runtime = budget_runtime
        self.settings = settings
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.effective_permission_context = effective_permission_context
        self.tool_context = tool_context
        self.turn_start_tool_call_count = turn_start_tool_call_count
        self.turn_started_at = turn_started_at
        self.stream_retry_policy = stream_retry_policy
        self.error_controller = error_controller
        self.user_message = user_message
        self.chain = chain
        self.approval_handler = approval_handler
        self.skill_manager = skill_manager
        self.runtime = runtime
        self.run_record = run_record
        self.metadata = metadata
        self.deadline_controller = deadline_controller
        self.answer_committer = answer_committer
        self.emit_event = emit_event
        self.turn_budget_controller = turn_budget_controller

    def _ack_consumed_parent_notifications(self) -> None:
        prompt_context = (
            self.agent_state.prompt_context
            if isinstance(self.agent_state.prompt_context, dict)
            else {}
        )
        raw_ids = prompt_context.get("delivered_parent_notification_ids", [])
        notification_ids = [
            str(value or "").strip() for value in raw_ids if str(value or "").strip()
        ]
        ack = getattr(self.runtime, "ack_parent_notification", None)
        if not notification_ids or not callable(ack):
            return
        parent_run_id = str(getattr(self.run_record, "run_id", "") or "")
        conversation_id = str(getattr(self.run_record, "conversation_id", "") or "")
        for notification_id in notification_ids:
            ack(
                notification_id,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
            )
        prompt_context.pop("delivered_parent_notification_ids", None)
        self.agent_state.prompt_context = prompt_context

    def _ack_consumed_swarm_messages(self) -> None:
        prompt_context = (
            self.agent_state.prompt_context
            if isinstance(self.agent_state.prompt_context, dict)
            else {}
        )
        claims = prompt_context.get("delivered_swarm_message_claims")
        ack = getattr(self.runtime, "ack_swarm_message_claims", None)
        if not isinstance(claims, list) or not claims or not callable(ack):
            return
        acked = int(ack(claims) or 0)
        if acked == len(claims):
            prompt_context.pop("delivered_swarm_message_claims", None)
            self.agent_state.prompt_context = prompt_context
            return
        self.agent_state.mark_transition(
            "subagent_mailbox_ack_fenced",
            claimed=len(claims),
            acked=acked,
        )

    async def _renew_swarm_message_claims(self, stop: asyncio.Event) -> None:
        """Keep claimed mailbox input fenced for the whole provider request."""

        prompt_context = (
            self.agent_state.prompt_context
            if isinstance(self.agent_state.prompt_context, dict)
            else {}
        )
        claims = prompt_context.get("delivered_swarm_message_claims")
        renew = getattr(self.runtime, "renew_swarm_message_claims", None)
        if not isinstance(claims, list) or not claims or not callable(renew):
            return
        lease_ms = MAILBOX_MESSAGE_LEASE_MS
        heartbeat_seconds = lease_ms / 3 / 1000
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_seconds)
                return
            except asyncio.TimeoutError:
                renewed = int(renew(claims, lease_ms=lease_ms) or 0)
                if renewed != len(claims):
                    self.agent_state.mark_transition(
                        "subagent_mailbox_lease_fenced",
                        claimed=len(claims),
                        renewed=renewed,
                    )
                    return

    async def execute(
        self,
        *,
        messages: list[Any],
        tool_schemas: list[dict[str, Any]],
        prompt_cache_safe_params: dict[str, Any],
        iteration_id: str,
        execution_state: IterationExecutionState,
    ) -> AsyncIterator[AgentEvent | TurnTerminalProjection | IterationExecutionResult]:
        stream_text = StreamTextState(iteration_id=iteration_id)
        execution_state.stream_text = stream_text
        provider_stream_result = None
        lease_stop = asyncio.Event()
        lease_task = asyncio.create_task(
            self._renew_swarm_message_claims(lease_stop),
            name=f"mailbox-claim-heartbeat-{iteration_id}",
        )
        try:
            async for update in stream_provider_response(
                llm=self.llm,
                messages=messages,
                tool_schemas=tool_schemas,
                llm_request_metadata=self.llm_request_metadata,
                prompt_cache_safe_params=prompt_cache_safe_params,
                provider_completion=self.provider_completion,
                state=self.agent_state,
                context_builder=self.context_builder,
                turn_kernel=self.turn_kernel,
                budget_runtime=self.budget_runtime,
                turn_usage=execution_state.turn_usage,
                settings=self.settings,
                tool_registry=self.tool_registry,
                permission_checker=self.permission_checker,
                effective_permission_context=self.effective_permission_context,
                tool_context=self.tool_context,
                turn_start_tool_call_count=self.turn_start_tool_call_count,
                turn_started_at=self.turn_started_at,
                iteration_limit=self.turn_budget_controller.max_iterations,
                tool_batch_count=execution_state.tool_batch_count,
                iteration_id_value=iteration_id,
                stream_retry_policy=self.stream_retry_policy,
                error_controller=self.error_controller,
                chain=self.chain,
                stream_text=stream_text,
                degrade_and_finish=degrade_and_finish,
                recover_withheld_error=recover_withheld_error,
            ):
                if isinstance(update, ProviderStreamResult):
                    provider_stream_result = update
                else:
                    yield update
        finally:
            lease_stop.set()
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
        if provider_stream_result is None:
            raise RuntimeError("provider stream runtime returned without a result")

        execution_state.turn_usage = provider_stream_result.turn_usage
        stream_state = provider_stream_result.stream_state
        stream_text = provider_stream_result.stream_text
        execution_state.stream_text = stream_text
        if provider_stream_result.action != "retry":
            self._ack_consumed_swarm_messages()
            self._ack_consumed_parent_notifications()
        if provider_stream_result.action != "proceed":
            yield IterationExecutionResult(
                action=provider_stream_result.action,
                state=execution_state,
                stream_text=stream_text,
            )
            return

        post_stream_result = None
        async for update in recover_provider_response(
            state=self.agent_state,
            stream_state=stream_state,
            stream_text=stream_text,
            tool_tracker=provider_stream_result.tool_tracker,
            context_builder=self.context_builder,
            budget_runtime=self.budget_runtime,
            turn_usage=execution_state.turn_usage,
            finish_reason=provider_stream_result.finish_reason,
            scrub_text=scrub_thinking_tags,
            tool_batch_count=execution_state.tool_batch_count,
            degraded_reason=execution_state.degraded_reason,
            exposed_tool_names=_exposed_tool_names(tool_schemas),
        ):
            if isinstance(update, PostStreamRecoveryResult):
                post_stream_result = update
            else:
                yield update
        if post_stream_result is None:
            raise RuntimeError("post-stream recovery returned without a result")
        execution_state.tool_batch_count = post_stream_result.tool_batch_count
        execution_state.degraded_reason = post_stream_result.degraded_reason
        if post_stream_result.action != "proceed":
            yield IterationExecutionResult(
                action=post_stream_result.action,
                state=execution_state,
                stream_text=stream_text,
            )
            return

        pending_tool_calls = stream_state.tool_calls
        if not pending_tool_calls:
            side_calls = self.metadata.get("_side_calls")
            if isinstance(side_calls, list) and side_calls:
                stream_state.raw_done["side_calls"] = [
                    dict(item) for item in side_calls if isinstance(item, dict)
                ]
            final_answer_outcome = None
            async for update in orchestrate_final_answer(
                user_message=self.user_message,
                state=self.agent_state,
                context_builder=self.context_builder,
                stream_text=stream_text,
                turn_kernel=self.turn_kernel,
                turn_usage=execution_state.turn_usage,
                provider_phase=provider_stream_result.response_phase,
                provider_items=stream_state.response_items,
                runtime=self.runtime,
                run_record=self.run_record,
                answer_committer=self.answer_committer,
                finish_reason=provider_stream_result.finish_reason,
                provider_raw_final_text=stream_state.raw_final_text,
                provider_raw_done=stream_state.raw_done,
                degraded_reason=execution_state.degraded_reason,
            ):
                if isinstance(update, FinalAnswerOutcome):
                    final_answer_outcome = update
                else:
                    yield update
            if final_answer_outcome is None:
                raise RuntimeError(
                    "final-answer orchestrator returned without an outcome"
                )
            execution_state.degraded_reason = final_answer_outcome.degraded_reason
            yield IterationExecutionResult(
                action=(
                    "retry" if final_answer_outcome.action == "retry" else "terminate"
                ),
                state=execution_state,
                stream_text=stream_text,
            )
            return

        tool_turn_result = None
        async for update in execute_tool_turn(
            pending_tool_calls=pending_tool_calls,
            provider_phase=provider_stream_result.response_phase,
            provider_items=stream_state.response_items,
            tool_batch_count=execution_state.tool_batch_count,
            turn_start_tool_call_count=self.turn_start_tool_call_count,
            state=self.agent_state,
            context_builder=self.context_builder,
            stream_state=stream_state,
            stream_text=stream_text,
            tool_tracker=provider_stream_result.tool_tracker,
            tool_registry=self.tool_registry,
            permission_checker=self.permission_checker,
            approval_handler=self.approval_handler,
            skill_manager=self.skill_manager,
            tool_context=self.tool_context,
            turn_kernel=self.turn_kernel,
            settings=self.settings,
            turn_budget_controller=self.turn_budget_controller,
            budget_runtime=self.budget_runtime,
            deadline_controller=self.deadline_controller,
            record_tool_call=self.chain.record_tool_call,
        ):
            if isinstance(update, ToolTurnResult):
                tool_turn_result = update
            else:
                yield update
        if tool_turn_result is None:
            raise RuntimeError("tool turn runtime returned without a result")
        execution_state.tool_batch_count = tool_turn_result.tool_batch_count
        yield IterationExecutionResult(
            action=("terminate" if tool_turn_result.action == "terminate" else "retry"),
            state=execution_state,
            stream_text=stream_text,
        )
