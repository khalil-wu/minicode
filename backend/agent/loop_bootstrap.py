"""Construct the turn-scoped runtime owned by the Agent loop.

This module is the composition root for one user turn.  It resolves session
dependencies, creates the lifecycle/budget objects, performs bounded input
preflight, and builds the tool execution context.  Provider I/O, recovery,
tool execution, and final-answer decisions intentionally stay in their own
runtimes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.iteration_budget import resolve_turn_max_iterations
from backend.agent.loop_preflight import prepare_turn_input
from backend.agent.loop_runtime_helpers import epoch_ms
from backend.agent.loop_session import (
    AgentLoopSessionContext,
    populate_prompt_context,
    prepare_turn_state,
)
from backend.agent.message import AgentEvent
from backend.agent.prompting import build_git_status_context
from backend.agent.policies import (
    DefaultStreamRetryPolicy,
)
from backend.agent.query_chain import QueryChainTracking
from backend.agent.state import AgentState
from backend.agent.rollout_budget import RolloutBudget
from backend.agent.turn_budget import TurnBudgetController, TurnDeadlineController
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.registry import ToolRegistry


@dataclass(slots=True)
class AgentLoopBootstrapRequest:
    user_message: str
    llm: LLMAdapter
    tool_registry: ToolRegistry
    artifact_store: ArtifactStore
    permission_checker: PermissionChecker
    agent_settings: AgentSettings | None
    token_budget: TokenBudget | None
    context_builder: ContextBuilder | None
    state: AgentState | None
    approval_handler: Callable | None
    skill_manager: Any | None
    permission_context: PermissionContext | None
    session_id: str
    task_id: str
    task_manager: Any | None
    background_manager: Any | None
    stream_callback: Any | None
    emit_event: Any | None
    metadata: dict[str, Any] | None
    session_context: AgentLoopSessionContext | None
    turn_kernel: TurnKernel | None = None
    state_prepared: bool = False
    initial_max_iterations_limit: int | None = None
    turn_budget_controller: TurnBudgetController | None = None


@dataclass(slots=True)
class AgentLoopBootstrap:
    user_message: str
    skill_manager: Any | None
    emit_event: Any | None
    metadata: dict[str, Any]
    external_metadata: dict[str, Any] | None
    cancel_event: asyncio.Event | None
    settings: AgentSettings
    rollout_budget: RolloutBudget
    budget: TokenBudget
    context: ContextBuilder
    state: AgentState
    initial_max_iterations_limit: int
    turn_budget_controller: TurnBudgetController | None
    deadline_controller: TurnDeadlineController
    turn_kernel: TurnKernel
    turn_started_at: int
    preflight_deadline_reached: bool
    preflight_blocked: bool
    preflight_block_message: str
    session_hook_result: Any | None
    prompt_hook_result: Any | None
    chain: QueryChainTracking
    stream_retry_policy: Any
    workspace_root: Path | None
    effective_permission_context: PermissionContext
    tool_context: ToolExecutionContext
    session_id: str
    task_id: str
    cost_session_id: str
    hook_manager_token: Any | None


async def bootstrap_agent_loop(
    request: AgentLoopBootstrapRequest,
) -> AsyncIterator[AgentEvent | AgentLoopBootstrap]:
    """Prepare one turn before skill activation and model iterations begin."""

    user_message = request.user_message
    skill_manager = request.skill_manager
    permission_context = request.permission_context
    session_id = request.session_id
    task_id = request.task_id
    task_manager = request.task_manager
    background_manager = request.background_manager
    stream_callback = request.stream_callback
    emit_event = request.emit_event
    metadata = request.metadata
    session_context = request.session_context

    if session_context is not None and not request.state_prepared:
        skill_manager = skill_manager or session_context.skill_manager
        permission_context = permission_context or session_context.permission_context
        session_id = session_id or session_context.session_id
        task_id = task_id or session_context.task_id
        task_manager = task_manager or session_context.task_manager
        background_manager = background_manager or session_context.background_manager
        stream_callback = stream_callback or session_context.stream_callback
        emit_event = emit_event or session_context.emit_event
        metadata = metadata or session_context.metadata

    external_metadata = metadata if isinstance(metadata, dict) else None
    resolved_metadata = dict(metadata or {})
    cancel_event = (
        session_context.cancel_event
        if session_context is not None and session_context.cancel_event is not None
        else resolved_metadata.get("cancel_event")
    )
    if not isinstance(cancel_event, asyncio.Event):
        cancel_event = None

    settings = request.agent_settings or AgentSettings()
    rollout_budget = resolved_metadata.get("_rollout_budget")
    if not isinstance(rollout_budget, RolloutBudget):
        rollout_budget = RolloutBudget()
    resolved_metadata["_rollout_budget"] = rollout_budget

    budget = request.token_budget or TokenBudget()
    context = request.context_builder or ContextBuilder(
        token_budget=budget,
        agent_settings=settings,
    )
    read_file_hashes = getattr(context, "read_file_hashes", None)
    if callable(read_file_hashes):
        # Share the context-owned map rather than copying it. Reads and writes
        # during this turn then advance the same state exported in the next
        # conversation snapshot, matching CC's cumulative readFileState.
        resolved_metadata["_read_file_hashes"] = read_file_hashes()
    initial_max_iterations_limit = int(
        request.initial_max_iterations_limit
        if request.initial_max_iterations_limit is not None
        else resolve_turn_max_iterations(settings)
    )
    state = request.state or AgentState(
        user_message=user_message,
        max_iterations=initial_max_iterations_limit,
    )
    if not request.state_prepared:
        prepare_turn_state(
            state,
            settings=settings,
        )

    deadline_controller = TurnDeadlineController(
        max_turn_seconds=max(0.0, float(settings.max_turn_seconds or 0.0)),
    )
    turn_kernel = request.turn_kernel or TurnKernel.create(
        metadata=resolved_metadata,
        state=state,
        budget=budget,
        task_id=task_id,
        session_id=session_id,
        emit_event=emit_event,
        initial_user_message=user_message,
    )
    for event in turn_kernel.start_events():
        yield event

    # The absolute turn fence includes hooks and skill preflight, rather than
    # starting only at the first provider request.
    turn_started_at = epoch_ms()
    deadline_controller.start_turn()
    workspace_root = session_context.workspace_root if session_context is not None else None
    if (
        workspace_root is None
        and state.workspace_context
        and hasattr(state.workspace_context, "root_path")
    ):
        workspace_root = state.workspace_context.root_path
    await asyncio.to_thread(build_git_status_context, workspace_root)

    stream_retry_policy = (
        settings.stream_retry_policy or DefaultStreamRetryPolicy(settings)
    )
    effective_permission_context = permission_context or PermissionContext()
    skill_read_roots: list[str] = []
    if skill_manager is not None:
        readable_roots = getattr(skill_manager, "readable_roots", None)
        if callable(readable_roots):
            skill_read_roots = [str(path) for path in readable_roots()]
    if skill_read_roots:
        constraints = {
            key: list(value)
            for key, value in effective_permission_context.filesystem_constraints.items()
        }
        constraints["readable_roots"] = skill_read_roots
        configured_allowlist = constraints.get("allowlist")
        if configured_allowlist is None and request.permission_checker is not None:
            configured_allowlist = list(
                request.permission_checker.policy_snapshot().get("path_allowlist", [])
            )
        constraints["allowlist"] = list(dict.fromkeys([
            *(configured_allowlist or []),
            *skill_read_roots,
        ]))
        effective_permission_context = replace(
            effective_permission_context,
            filesystem_constraints=constraints,
        )
    tool_context = ToolExecutionContext(
        permission=effective_permission_context,
        session_id=session_id,
        task_id=task_id,
        metadata=dict(resolved_metadata),
        cancel_event=cancel_event,
        emit_event=emit_event,
        approval_handler=request.approval_handler,
        stream_callback=stream_callback,
        workspace_root=workspace_root,
        allow_network=effective_permission_context.mode == "bypass",
        task_manager=task_manager,
        background_manager=background_manager,
        terminal_manager=(
            getattr(session_context, "terminal_manager", None)
            if session_context is not None
            else None
        ),
        checkpoint_manager=getattr(state, "checkpoint_manager", None),
        permission_checker=request.permission_checker,
        conversation_id=getattr(state, "conversation_id", ""),
        llm=request.llm,
        artifact_store=request.artifact_store,
    )
    if workspace_root is not None and "cwd" not in tool_context.metadata:
        tool_context.metadata["cwd"] = str(workspace_root)
    populate_prompt_context(
        state=state,
        metadata=resolved_metadata,
        workspace_root=workspace_root,
        permission_context=effective_permission_context,
    )
    tool_context.metadata["prompt_context"] = state.prompt_context
    tool_context.metadata["_context_builder"] = context
    tool_context.deadline_monotonic = deadline_controller.turn_deadline
    turn_kernel.bind_tool_context(tool_context)

    # CC resolves hooks per session cwd from user -> project -> local scopes.
    # A ContextVar keeps concurrent desktop conversations from sharing the
    # wrong project's hook set or process cwd. Bind only after the rest of the
    # bootstrap graph is built, then transfer token ownership atomically with
    # the returned bootstrap object.
    from backend.hooks.manager import (
        bind_hook_manager,
        load_hook_manager_for_workspace,
        register_hook_manager_for_session,
        unbind_hook_manager,
    )

    hook_manager = load_hook_manager_for_workspace(workspace_root)
    hook_manager_token = bind_hook_manager(hook_manager)
    try:
        preflight = await prepare_turn_input(
            user_message,
            state=state,
            turn_kernel=turn_kernel,
            session_id=session_id,
            deadline=deadline_controller.turn_deadline,
            cancel_event=cancel_event,
            resume_from_checkpoint=bool(
                resolved_metadata.get("_query_engine_recovery_restored")
            ),
        )
        user_message = preflight.user_message
        register_hook_manager_for_session(session_id, hook_manager)
        chain = QueryChainTracking(
            user_message_preview=user_message[:100],
            source="user",
        )
        bootstrap_result = AgentLoopBootstrap(
            user_message=user_message,
            skill_manager=skill_manager,
            emit_event=emit_event,
            metadata=resolved_metadata,
            external_metadata=external_metadata,
            cancel_event=cancel_event,
            settings=settings,
            rollout_budget=rollout_budget,
            budget=budget,
            context=context,
            state=state,
            initial_max_iterations_limit=initial_max_iterations_limit,
            turn_budget_controller=request.turn_budget_controller,
            deadline_controller=deadline_controller,
            turn_kernel=turn_kernel,
            turn_started_at=turn_started_at,
            preflight_deadline_reached=preflight.deadline_reached,
            preflight_blocked=preflight.blocked,
            preflight_block_message=preflight.block_message,
            session_hook_result=preflight.session_hook_result,
            prompt_hook_result=preflight.prompt_hook_result,
            chain=chain,
            stream_retry_policy=stream_retry_policy,
            workspace_root=workspace_root,
            effective_permission_context=effective_permission_context,
            tool_context=tool_context,
            session_id=session_id,
            task_id=task_id,
            cost_session_id=str(
                resolved_metadata.get("cost_session_id") or session_id or ""
            ).strip(),
            hook_manager_token=hook_manager_token,
        )
    except BaseException:
        unbind_hook_manager(hook_manager_token)
        raise
    yield bootstrap_result
