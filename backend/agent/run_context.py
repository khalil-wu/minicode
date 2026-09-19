"""Explicit owners for one live agent run.

``metadata`` is still used for transport and durable projection fields.  The
objects in this container are process-local capabilities and must not be
looked up by string keys after the turn boundary has been assembled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.agent.extension_actions import ExtensionExecutionActions
    from backend.agent.message import UserCommand
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.agent.code_execution_store import CodeExecutionStore
    from backend.agent.code_execution import CodeExecutionRuntime
    from backend.agent.tool_execution_gate import ToolExecutionGate
    from backend.agent.message import AgentEvent
    from backend.agent.execution_journal import ExecutionJournal
    from backend.agent.provider_lifecycle import ProviderLifecycleRuntime
    from backend.agent.runtime import AgentRuntime
    from backend.agent.turn_input import TurnInputQueue
    from backend.conversations.repository import ConversationRepository
    from backend.hooks.manager import HookManager
    from backend.llm.base import LLMTurnContext
    from backend.mcp.manager import MCPServerManager
    from backend.tools.toolsets import ToolsetPolicy
    from backend.tools.registry import ToolRegistry
    from backend.workspace.context import WorkspaceContext


@dataclass(slots=True)
class RunContext:
    """Mutable, turn-owned runtime capabilities shared by one agent run."""

    lifecycle_runtime: ProviderLifecycleRuntime | None = None
    extension_actions: ExtensionExecutionActions | None = None
    enqueue_extension_followup: Callable[[UserCommand], Any] | None = None
    extension_name_setter: Callable[[str], Any] | None = None
    extension_tool_selection_setter: Callable[[list[str]], None] | None = None
    cancel_event: asyncio.Event | None = None
    retain_model: Callable[[Any, asyncio.Task], None] | None = None
    refresh_model_auth: Callable[[ModelExecutionSnapshot, bool, asyncio.Task], Awaitable[ModelExecutionSnapshot]] | None = None
    model_owner_task: asyncio.Task | None = None
    execution_journal: ExecutionJournal | None = None
    mcp_manager: MCPServerManager | None = None
    mcp_owner_session_id: str = ""
    # Selection for the next provider step. Existing calls retain their own
    # ModelExecutionSnapshot when a host publishes a replacement here.
    model_execution: ModelExecutionSnapshot | None = None
    active_model_execution: ModelExecutionSnapshot | None = None
    agent_runtime: AgentRuntime | None = None
    llm_turn_context: LLMTurnContext | None = None
    hook_manager: HookManager | None = None
    workspace_context: WorkspaceContext | None = None
    cost_session_id: str = ""
    requires_explicit_workspace: bool = False
    connected_mcp_servers: tuple[str, ...] = ()
    permission_mode_setter: Callable[..., Any] | None = None
    permission_context_provider: Callable[..., Any] | None = None
    command_prompt_allow_rules_setter: Callable[..., Any] | None = None
    teammate_plan_approval_requester: Callable[..., Any] | None = None
    conversation_repository: ConversationRepository | None = None
    turn_input_queue: TurnInputQueue | None = None
    persist_consumed_turn_input: Callable[..., Any] | None = None
    acknowledge_consumed_turn_input: Callable[..., Any] | None = None
    previous_turn_aborted: bool = False
    toolset_policy: ToolsetPolicy | None = None
    session_toolset_policy: ToolsetPolicy | None = None
    # Captured capability surface for the currently admitted provider step.
    # The host-owned registry remains available through AgentSession for the
    # next step's refresh.
    active_tool_registry: ToolRegistry | None = None
    code_execution: CodeExecutionRuntime | None = None
    tool_execution_gate: ToolExecutionGate | None = None
    publish_nested_event: Callable[[AgentEvent], Awaitable[None]] | None = None
    code_store: CodeExecutionStore | None = None

__all__ = ["RunContext"]
