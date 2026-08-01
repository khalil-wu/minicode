"""Model-tool transition preparation and history commit."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.agent.tool_execution import (
    prepare_tool_call_sequence,
)
from backend.llm.base import ToolCallEvent
from backend.permissions.context import ToolExecutionContext
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class PreparedToolTransition:
    tool_calls: list[ToolCallEvent]
    history_tool_calls: list[ToolCallEvent]


@dataclass(slots=True)
class ToolTransitionExecution:
    prepared: PreparedToolTransition
    events: AsyncIterator[AgentEvent]
    call_count: int


class ToolTransitionController:
    """Own preparation, history commit, permission refresh, and batch start.

    The provider loop only consumes the resulting event iterator. This keeps
    model-history safety and live permission changes in one transition
    boundary. Provider streaming never executes tools; the batch runner below
    is the single post-settlement execution path.
    """

    def __init__(
        self,
        *,
        ctx: ContextBuilder,
        state: AgentState,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        approval_handler: Callable | None,
        skill_manager: Any | None,
        tool_context: ToolExecutionContext,
        refresh_permission: Callable[[], bool],
        cancel_prefetch: Callable[[], None],
        prefetched_results: dict[str, Any],
        record_tool_call: Callable[[], None],
    ) -> None:
        self.ctx = ctx
        self.state = state
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.approval_handler = approval_handler
        self.skill_manager = skill_manager
        self.tool_context = tool_context
        self.refresh_permission = refresh_permission
        # Kept as a compatibility callback for recovery owners. It only clears
        # stream diagnostics now; it must not start or replay tool work.
        self.cancel_prefetch = cancel_prefetch
        self.prefetched_results = prefetched_results
        self.record_tool_call = record_tool_call

    def start(
        self,
        *,
        tool_calls: list[ToolCallEvent],
        content: str,
        phase: str,
        provider_items: list[dict[str, Any]],
        replace_tool_calls: Callable[[list[ToolCallEvent]], None],
        execution_limit: int | None = None,
        execution_limit_reason: str = "",
    ) -> ToolTransitionExecution:
        prepared = prepare_tool_transition(
            state=self.state,
            tool_calls=tool_calls,
            tool_registry=self.tool_registry,
            tool_context=self.tool_context,
        )
        replace_tool_calls(prepared.tool_calls)
        for _tool_call in prepared.tool_calls:
            self.record_tool_call()
        append_tool_transition_history(
            ctx=self.ctx,
            transition=prepared,
            content=content,
            phase=phase,
            provider_items=provider_items,
        )

        if self.refresh_permission():
            self.cancel_prefetch()

        runner = ToolBatchRunner(
            ctx=self.ctx,
            state=self.state,
            tool_registry=self.tool_registry,
            permission_checker=self.permission_checker,
            approval_handler=self.approval_handler,
            skill_manager=self.skill_manager,
            permission_context=self.tool_context.permission,
            tool_ctx=self.tool_context,
        )
        return ToolTransitionExecution(
            prepared=prepared,
            events=runner.run(
                prepared.tool_calls,
                prefetched_results=self.prefetched_results,
                prepared_tool_calls=prepared.tool_calls,
                execution_limit=execution_limit,
                execution_limit_reason=execution_limit_reason,
            ),
            call_count=len(prepared.tool_calls),
        )


async def project_tool_transition(
    execution: ToolTransitionExecution,
    *,
    cancel_remaining: Callable[[], None],
) -> AsyncIterator[AgentEvent]:
    """Consume a tool batch and guarantee its execution scope is closed.

    Tool runners are async generators so interruption can happen while an
    event is suspended at ``yield``. The owner of that generator closes it
    and releases stream diagnostics in one place.
    """

    events = execution.events
    try:
        async for event in events:
            yield event
    finally:
        await events.aclose()
        cancel_remaining()


def prepare_tool_transition(
    *,
    state: AgentState,
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
    tool_context: ToolExecutionContext,
) -> PreparedToolTransition:
    prepared = prepare_tool_call_sequence(
        state,
        tool_calls,
        tool_registry,
        tool_context,
    )
    return PreparedToolTransition(
        tool_calls=prepared,
        history_tool_calls=prepared,
    )


def append_tool_transition_history(
    *,
    ctx: ContextBuilder,
    transition: PreparedToolTransition,
    content: str,
    phase: str,
    provider_items: list[dict[str, Any]],
) -> None:
    if transition.history_tool_calls:
        append_tool_calls = ctx.append_assistant_tool_calls
        try:
            parameters = inspect.signature(append_tool_calls).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "content" in parameters:
            kwargs["content"] = content
        if accepts_kwargs or "phase" in parameters:
            kwargs["phase"] = phase
        if accepts_kwargs or "provider_items" in parameters:
            kwargs["provider_items"] = provider_items
        append_tool_calls(transition.history_tool_calls, **kwargs)
        return
    if content.strip():
        append_assistant = ctx.append_assistant
        try:
            parameters = inspect.signature(append_assistant).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {}
        if accepts_kwargs or "phase" in parameters:
            kwargs["phase"] = phase
        if accepts_kwargs or "provider_items" in parameters:
            kwargs["provider_items"] = provider_items
        append_assistant(content, **kwargs)
