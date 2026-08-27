"""Admit one Agent iteration and prepare its provider request.

This is the boundary between turn orchestration and provider I/O.  It owns the
work that must happen exactly once before a provider attempt: phase transition,
budget admission, pacing, mutable tool-schema refresh, context budgeting,
mailbox delivery, context rendering, and the model-phase timeline events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.loop_runtime_helpers import iteration_id, sleep_or_cancel
from backend.agent.mailbox_delivery import (
    inject_parent_notifications,
    inject_subagent_mailbox_updates,
)
from backend.agent.message import AgentEvent
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.tool_schema_derivation import TurnToolSchemaDerivation
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.turn_context_runtime import prepare_turn_context
from backend.agent.turn_kernel import _set_terminal_reason
from backend.agent.loop_preflight import PhaseDeadlineExceeded
from backend.llm.base import LLMMessage
from backend.services.context_budget import manage_context_budget


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IterationAdmissionResult:
    action: Literal["proceed", "retry", "terminate"]
    tool_schema_state: TurnToolSchemaDerivation
    initial_turn_pending: bool
    tool_schemas: list[dict[str, Any]] | None = None
    messages: list[LLMMessage] | None = None
    prompt_cache_safe_params: dict[str, Any] | None = None
    iteration_id: str = ""


class TurnIterationAdmission:
    """Own the complete pre-provider boundary for each loop iteration."""

    def __init__(
        self,
        *,
        context: Any,
        state: Any,
        llm: Any,
        iteration_runtime: Any,
        deadline_controller: Any,
        turn_budget_controller: TurnBudgetController,
        budget_runtime: Any,
        turn_start_tool_call_count: int,
        chain: Any,
        tool_context: Any,
        token_budget: Any,
        metadata: dict[str, Any],
        external_metadata: dict[str, Any] | None,
        emit_event: Any,
        runtime: Any,
        run_record: Any,
        llm_request_metadata: dict[str, Any],
        turn_kernel: Any,
    ) -> None:
        self.context = context
        self.state = state
        self.llm = llm
        self.iteration_runtime = iteration_runtime
        self.deadline_controller = deadline_controller
        self.turn_budget_controller = turn_budget_controller
        self.budget_runtime = budget_runtime
        self.turn_start_tool_call_count = turn_start_tool_call_count
        self.chain = chain
        self.tool_context = tool_context
        self.token_budget = token_budget
        self.metadata = metadata
        self.external_metadata = external_metadata
        self.emit_event = emit_event
        self.runtime = runtime
        self.run_record = run_record
        self.llm_request_metadata = llm_request_metadata
        self.turn_kernel = turn_kernel

    async def admit(
        self,
        *,
        previous_tool_schema_state: TurnToolSchemaDerivation,
        initial_turn_pending: bool,
        pending_turn_context: list[str],
    ) -> AsyncIterator[AgentEvent | TurnTerminalProjection | IterationAdmissionResult]:
        """Yield admission events and finish with exactly one result sentinel."""

        turn_elapsed_seconds = self.deadline_controller.elapsed()
        budget_boundary = self.turn_budget_controller.evaluate(
            elapsed_seconds=turn_elapsed_seconds,
            iterations=self.state.work_iterations,
            tool_calls=len(self.state.tool_calls) - self.turn_start_tool_call_count,
            tokens=self.budget_runtime.local_tokens_used(),
            cost_usd=self.budget_runtime.turn_cost_usd(),
        )
        if budget_boundary is None:
            budget_boundary = self.budget_runtime.rollout_boundary()
        if budget_boundary is not None:
            logger.warning("%s: %s", budget_boundary.label, budget_boundary.detail)
            _, budget_events = await self.budget_runtime.apply_boundary(budget_boundary)
            for event in budget_events:
                yield event
            yield self._result(
                action="terminate",
                tool_schema_state=previous_tool_schema_state,
                initial_turn_pending=initial_turn_pending,
            )
            return

        if self.state.iterations > 0:
            await sleep_or_cancel(0.15, self.tool_context.cancel_event)
        depth = self.chain.next_iteration()
        logger.info("%s Iteration %d", self.chain.to_log_context(), depth)
        iteration_preparation = await self.iteration_runtime.prepare(
            previous_tool_schema_state=previous_tool_schema_state,
            initial_turn_pending=initial_turn_pending,
            pending_turn_context=pending_turn_context,
        )
        self.llm = self.iteration_runtime.llm
        owner = getattr(self.iteration_runtime, "agent_session", None)
        active_budget = getattr(owner, "token_budget", None)
        if active_budget is not None:
            self.token_budget = active_budget
        initial_turn_pending = False
        tool_schema_state = iteration_preparation.tool_schema_state
        tool_schemas = iteration_preparation.tool_schemas
        for event in iteration_preparation.events:
            yield event
        if iteration_preparation.terminal:
            logger.error(
                "Provider capability check failed run_id=%s",
                self.run_record.run_id,
            )
            _set_terminal_reason(self.state, "provider_capability", status="failed")
            yield self._result(
                action="terminate",
                tool_schema_state=tool_schema_state,
                initial_turn_pending=initial_turn_pending,
            )
            return

        try:
            self.budget_runtime.bounded_provider_timeout(0.0)
        except PhaseDeadlineExceeded:
            _, deadline_events = await self.budget_runtime.apply_boundary(
                self.budget_runtime.phase_deadline_boundary(),
            )
            for event in deadline_events:
                yield event
            yield self._result(
                action="terminate",
                tool_schema_state=tool_schema_state,
                initial_turn_pending=initial_turn_pending,
            )
            return

        active_deadline = self.budget_runtime.active_phase_deadline()
        try:
            if active_deadline is None:
                async for event in manage_context_budget(
                    self.context,
                    self.state,
                    self.token_budget,
                    tool_schemas,
                ):
                    yield event
            else:
                remaining = max(0.0, active_deadline - time.monotonic())
                async with asyncio.timeout(remaining):
                    async for event in manage_context_budget(
                        self.context,
                        self.state,
                        self.token_budget,
                        tool_schemas,
                    ):
                        yield event
        except TimeoutError:
            _, deadline_events = await self.budget_runtime.apply_boundary(
                self.budget_runtime.phase_deadline_boundary(),
            )
            for event in deadline_events:
                yield event
            yield self._result(
                action="terminate",
                tool_schema_state=tool_schema_state,
                initial_turn_pending=initial_turn_pending,
            )
            return
        if self.state.stopped_reason:
            if self.state.stopped_reason == "budget_exceeded":
                _, budget_events = await self.budget_runtime.apply_boundary(
                    TurnBudgetController.context_exhausted(),
                )
                for event in budget_events:
                    yield event
            yield self._result(
                action="terminate",
                tool_schema_state=tool_schema_state,
                initial_turn_pending=initial_turn_pending,
            )
            return

        await inject_subagent_mailbox_updates(
            ctx=self.context,
            state=self.state,
            metadata=self.metadata,
            conversation_id=self.run_record.conversation_id,
            emit_event=self.emit_event,
        )
        await inject_parent_notifications(
            ctx=self.context,
            state=self.state,
            metadata=self.metadata,
            runtime=self.runtime,
            parent_run_id=self.run_record.run_id,
            conversation_id=(
                self.run_record.conversation_id
                or str(self.metadata.get("conversation_id") or "")
            ),
            emit_event=self.emit_event,
        )

        prepared_context = await prepare_turn_context(
            context=self.context,
            state=self.state,
            llm=self.llm,
            tool_schemas=tool_schemas,
            request_metadata=self.llm_request_metadata,
            metadata=self.metadata,
            external_metadata=self.external_metadata,
            tool_context=self.tool_context,
            turn_kernel=self.turn_kernel,
            run_id=self.run_record.run_id,
        )

        self.state.iterations += 1
        current_iteration_id = iteration_id(self.state)
        model_phase = "execute"
        model_phase_summary = "Model deciding next action"
        self.runtime.update_phase(
            self.run_record.run_id,
            model_phase,
            summary=model_phase_summary,
        )
        yield self._result(
            action="proceed",
            tool_schema_state=tool_schema_state,
            initial_turn_pending=initial_turn_pending,
            tool_schemas=tool_schemas,
            messages=prepared_context.messages,
            prompt_cache_safe_params=prepared_context.prompt_cache_safe_params,
            iteration_id_value=current_iteration_id,
        )

    @staticmethod
    def _result(
        *,
        action: Literal["proceed", "retry", "terminate"],
        tool_schema_state: TurnToolSchemaDerivation,
        initial_turn_pending: bool,
        tool_schemas: list[dict[str, Any]] | None = None,
        messages: list[LLMMessage] | None = None,
        prompt_cache_safe_params: dict[str, Any] | None = None,
        iteration_id_value: str = "",
    ) -> IterationAdmissionResult:
        return IterationAdmissionResult(
            action=action,
            tool_schema_state=tool_schema_state,
            initial_turn_pending=initial_turn_pending,
            tool_schemas=tool_schemas,
            messages=messages,
            prompt_cache_safe_params=prompt_cache_safe_params,
            iteration_id=iteration_id_value,
        )
