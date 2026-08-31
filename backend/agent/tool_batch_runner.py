"""Turn-scoped adapter around the tool execution engine.

The historical loop wired ten execution arguments directly at the call site.
This object is intentionally thin: it creates a stable seam for the next
extraction without changing tool ordering, permission checks, or cancellation
semantics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.agent.message import AgentEvent
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.registry import ToolRegistry


class ToolBatchRunner:
    """Execute one model tool batch with immutable turn dependencies."""

    def __init__(
        self,
        *,
        ctx: ContextBuilder,
        state: AgentState,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        approval_handler: Callable | None,
        skill_manager: Any | None,
        permission_context: PermissionContext | None,
        tool_ctx: ToolExecutionContext,
    ) -> None:
        self.ctx = ctx
        self.state = state
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.approval_handler = approval_handler
        self.skill_manager = skill_manager
        self.permission_context = permission_context
        self.tool_ctx = tool_ctx

    def run(
        self,
        tool_calls: list[ToolCallEvent],
        *,
        prepared_tool_calls: list[ToolCallEvent] | None = None,
        execution_limit: int | None = None,
        execution_limit_reason: str = "",
    ) -> AsyncIterator[AgentEvent]:
        return execute_tool_batch(
            tool_calls,
            ctx=self.ctx,
            state=self.state,
            tool_registry=self.tool_registry,
            permission_checker=self.permission_checker,
            approval_handler=self.approval_handler,
            skill_manager=self.skill_manager,
            permission_context=self.permission_context,
            tool_ctx=self.tool_ctx,
            prepared_tool_calls=prepared_tool_calls,
            execution_limit=execution_limit,
            execution_limit_reason=execution_limit_reason,
        )
