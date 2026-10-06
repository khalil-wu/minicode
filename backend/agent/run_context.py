"""Explicit owners for one live agent run.

``metadata`` is still used for transport and durable projection fields.  The
objects in this container are process-local capabilities and must not be
looked up by string keys after the turn boundary has been assembled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.async_cleanup import cancel_and_drain_receipt, retain_cleanup_task
from backend.llm.errors import sanitize_llm_error_message

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
    from backend.agent.turn_diff_tracker import TurnDiffTracker
    from backend.agent.workspace_turn_changes import WorkspaceTurnChanges


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
    lifecycle_cleanup_tasks: set[asyncio.Task] = field(default_factory=set)
    lifecycle_cleanup_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_model_auth: Callable[[ModelExecutionSnapshot, bool, asyncio.Task], Awaitable[ModelExecutionSnapshot]] | None = None
    model_owner_task: asyncio.Task | None = None
    execution_journal: ExecutionJournal | None = None
    mcp_manager: MCPServerManager | None = None
    skill_manager: Any | None = None
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
    turn_diff_tracker: TurnDiffTracker | None = None
    workspace_turn_changes: WorkspaceTurnChanges | None = None

    def retain_lifecycle_task(
        self, task: asyncio.Task, *, label: str, llm: Any = None,
        cancellation_requested: bool = False,
    ) -> dict[str, Any]:
        """Retain the real borrower, not just its terminal cleanup receipt."""
        key = f"{label}:{id(task)}"
        receipt = {
            "resource_kind": "lifecycle", "resource_id": key, "reason": label,
            "requested": cancellation_requested, "acknowledged": False,
            "completed": task.done(), "timed_out": False, "pending": int(not task.done()),
        }
        self.lifecycle_cleanup_receipts[key] = receipt
        retain_cleanup_task(task, self.lifecycle_cleanup_tasks)
        snapshot = self.active_model_execution or self.model_execution
        adapter = llm if llm is not None else snapshot.llm if snapshot is not None else None
        if self.retain_model is not None and adapter is not None:
            self.retain_model(adapter, task)

        def settled(done: asyncio.Task) -> None:
            receipt.update(completed=True, pending=0, acknowledged=receipt["requested"])
            if not done.cancelled() and (error := done.exception()) is not None:
                receipt["error"] = str(error)

        task.add_done_callback(settled)
        return receipt

    async def drain_lifecycle_task(
        self, task: asyncio.Task, *, timeout: float, label: str, llm: Any = None,
    ) -> None:
        if task.done():
            return
        evidence = self.retain_lifecycle_task(
            task, label=label, llm=llm, cancellation_requested=True,
        )
        receipt = await cancel_and_drain_receipt(
            [task], timeout=timeout, label=label, owner=self.lifecycle_cleanup_tasks,
        )
        evidence.update(
            requested=receipt.requested, acknowledged=receipt.acknowledged,
            completed=receipt.completed, timed_out=receipt.timed_out, pending=receipt.pending,
        )

    def lifecycle_cleanup_evidence(self) -> dict[str, Any]:
        receipts = {key: dict(receipt) for key, receipt in self.lifecycle_cleanup_receipts.items()}
        for receipt in receipts.values():
            if "error" in receipt:
                receipt["error"] = sanitize_llm_error_message(receipt["error"])
        return {
            "lifecycle_cleanup_receipts": receipts,
            "lifecycle_cleanup_pending_count": sum(
                not task.done() for task in self.lifecycle_cleanup_tasks
            ),
        }

__all__ = ["RunContext"]
