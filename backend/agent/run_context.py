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
from backend.runtime_env import default_user_shell

if TYPE_CHECKING:
    from backend.agent.environment_selection import EnvironmentManagerLease, ThreadEnvironments, TurnEnvironmentRequest, TurnEnvironmentSnapshot
    from backend.environments.manager import EnvironmentManager
    from backend.mcp.environment_runtime import PreparedEnvironmentCapabilities
    from backend.ws.execution_context import ConversationThreadRuntime
    from backend.agent.context import ContextBuilder
    from backend.agent.extension_actions import ExtensionExecutionActions
    from backend.agent.message import UserCommand
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.agent.model_activation import ModelActivationState
    from backend.agent.code_execution_store import CodeExecutionStore
    from backend.agent.code_execution import CodeExecutionRuntime
    from backend.agent.tool_execution_gate import ToolExecutionGate
    from backend.agent.message import AgentEvent
    from backend.agent.execution_journal import ExecutionJournal
    from backend.agent.provider_lifecycle import ProviderLifecycleRuntime
    from backend.agent.runtime import AgentRuntime
    from backend.agent.turn_input import ScheduledInputCommand, TurnInputQueue
    from backend.conversations.repository import ConversationRepository
    from backend.hooks.manager import HookManager
    from backend.llm.base import LLMTurnContext
    from backend.mcp.manager import MCPServerManager
    from backend.mcp.approval import MCPApprovalStore
    from backend.tools.toolsets import ToolsetPolicy
    from backend.tools.registry import ToolRegistry
    from backend.workspace.context import WorkspaceContext
    from backend.agent.turn_diff_tracker import TurnDiffTracker
    from backend.agent.guardian.service import GuardianReviewService
    from backend.agent.guardian.async_service import GuardianAsyncService
    from backend.agent.guardian.retained import GuardianEvidenceSnapshot


@dataclass(slots=True)
class RunContext:
    """Mutable, turn-owned runtime capabilities shared by one agent run."""

    lifecycle_runtime: ProviderLifecycleRuntime | None = None
    environment_manager: EnvironmentManager | None = None
    environment_manager_lease: EnvironmentManagerLease | None = None
    environment_requests: tuple[TurnEnvironmentRequest, ...] | None = None
    execution_cwd_uri: str = ""
    inherited_turn_environments: TurnEnvironmentSnapshot | None = None
    selected_capability_roots: tuple[dict[str, Any], ...] = ()
    environment_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    environment_capabilities: PreparedEnvironmentCapabilities | None = None
    environment_retirement_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    environment_host_mcp_manager: Any | None = None
    environment_host_skill_manager: Any | None = None
    network_step: Any | None = None
    network_approval_service: Any | None = None
    session_source: str = "sdk"
    isolated: bool = False
    is_subagent: bool = False
    thread_environments: ThreadEnvironments | None = None
    retain_thread_environments: bool = False
    user_shell: dict[str, str] = field(default_factory=default_user_shell)
    environment_thread_runtime: ConversationThreadRuntime | None = None
    node_repl_owner: Any | None = None
    initial_turn_environments: TurnEnvironmentSnapshot | None = None
    turn_environments: TurnEnvironmentSnapshot | None = None
    context_builder: ContextBuilder | None = None
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
    mcp_app_runtime: Any | None = None
    mcp_tool_approvals: MCPApprovalStore | None = None
    skill_manager: Any | None = None
    mcp_owner_session_id: str = ""
    # Selection for the next provider step. Existing calls retain their own
    # ModelExecutionSnapshot when a host publishes a replacement here.
    model_execution: ModelExecutionSnapshot | None = None
    active_model_execution: ModelExecutionSnapshot | None = None
    model_activation: ModelActivationState | None = None
    agent_runtime: AgentRuntime | None = None
    llm_turn_context: LLMTurnContext | None = None
    hook_manager: HookManager | None = None
    guardian_review_service: GuardianReviewService | None = None
    guardian_async_service: GuardianAsyncService | None = None
    guardian_evidence_source: Callable[[], GuardianEvidenceSnapshot] | None = None
    guardian_root_evidence_source: Callable[[], GuardianEvidenceSnapshot] | None = None
    node_repl_review_evidence: Any | None = None
    workspace_context: WorkspaceContext | None = None
    cost_session_id: str = ""
    # Canonical model-protocol rollout identity, independent of a WS window
    # or one run incarnation. The request metadata producer owns this value.
    responses_session_id: str = ""
    requires_explicit_workspace: bool = False
    connected_mcp_servers: tuple[str, ...] = ()
    permission_mode_setter: Callable[..., Any] | None = None
    permission_context_provider: Callable[..., Any] | None = None
    command_prompt_allow_rules_setter: Callable[..., Any] | None = None
    teammate_plan_approval_requester: Callable[..., Any] | None = None
    conversation_repository: ConversationRepository | None = None
    turn_input_queue: TurnInputQueue | None = None
    # These are the real owner and canonical terminal, shared by host input
    # submission. They never come from public command metadata.
    turn_kind: str = "regular"
    turn_owner_task: asyncio.Task | None = None
    accept_scheduled_input: Callable[..., Any] | None = None
    turn_terminal_receipt: asyncio.Future[dict[str, Any]] | None = None
    turn_started_receipt: asyncio.Future[dict[str, Any]] | None = None
    active_final_output_json_schema: dict[str, Any] | None = None
    conversation_revision: int | None = None
    close_turn_inputs: Callable[[], None] | None = None
    initial_scheduled_input: ScheduledInputCommand | None = None
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
    window_binding_token: str = field(default="", repr=False)
    cancel_reason: str = ""
    record_user_cancelled: Callable[[str], None] | None = None
    user_cancel_persistence: asyncio.Task[None] | None = None
    user_input_command: Callable[[], Any | None] | None = None
    user_stop_receipt: dict[str, Any] | None = None
    user_stop_acceptance: asyncio.Future[None] | None = None
    exec_policy_owner: Any | None = None

    async def capture_turn_environments(self) -> TurnEnvironmentSnapshot:
        captured = await self.thread_environments.snapshot()
        previous = self.turn_environments
        self.turn_environments = captured
        if self.initial_turn_environments is None:
            self.initial_turn_environments = captured
        if previous is not None and previous is not self.initial_turn_environments:
            await previous.aclose()
        return captured

    async def close_environment_snapshots(self) -> None:
        current, initial = self.turn_environments, self.initial_turn_environments
        self.turn_environments = self.initial_turn_environments = None
        if current is not None:
            await current.aclose()
        if initial is not None and initial is not current:
            await initial.aclose()
        if self.inherited_turn_environments is not None:
            await self.inherited_turn_environments.aclose()
            self.inherited_turn_environments = None
        if self.environment_capabilities is not None:
            self.environment_retirement_tasks.add(self.environment_capabilities.retire(self))
            self.environment_capabilities = None
        if self.environment_retirement_tasks:
            tasks = tuple(self.environment_retirement_tasks)
            pending_tools = any(not task.done() for task in self.lifecycle_cleanup_tasks)
            pending_cells = self.code_execution is not None and any(
                cell.task is not None and not cell.task.done() for cell in self.code_execution.cells.values())
            if pending_tools or pending_cells:
                for task in tasks:
                    self.retain_lifecycle_task(task, label="environment_capabilities_retirement")
                self.environment_retirement_tasks.difference_update(tasks)
                return
            results = await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
            errors = [result for result in results if isinstance(result, Exception)]
            if errors:
                raise ExceptionGroup("retired environment capabilities did not close", errors)
            self.environment_retirement_tasks.difference_update(tasks)

    def prepare_turn_owner(self) -> None:
        self.turn_owner_task = asyncio.current_task()
        self.turn_terminal_receipt = asyncio.get_running_loop().create_future()
        self.turn_started_receipt = asyncio.get_running_loop().create_future()

    def publish_turn_started(self, turn_id: str) -> None:
        receipt = self.turn_started_receipt
        if receipt is not None and not receipt.done():
            receipt.set_result({"status": "started", "turn_id": turn_id})

    def publish_turn_terminal(self, result: dict[str, Any]) -> None:
        receipt = self.turn_terminal_receipt
        if receipt is not None and not receipt.done():
            receipt.set_result(result)

    async def wait_for_turn_release(self) -> None:
        if self.turn_owner_task is not asyncio.current_task():
            await asyncio.shield(self.turn_owner_task)
        while pending := {task for task in self.lifecycle_cleanup_tasks if not task.done()}:
            await asyncio.wait(pending)

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
