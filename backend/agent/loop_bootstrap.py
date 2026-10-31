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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.agent_identity import AgentPath
from backend.agent.iteration_budget import resolve_turn_max_iterations
from backend.agent.lifecycle_observer import (
    resolve_lifecycle_runtime,
)
from backend.agent.loop_preflight import prepare_turn_input
from backend.agent.loop_session import (
    AgentLoopSessionContext,
    populate_prompt_context,
    prepare_turn_state,
)
from backend.agent.message import AgentEvent
from backend.agent.policies import (
    DefaultStreamRetryPolicy,
)
from backend.agent.query_chain import QueryChainTracking
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.rollout_budget import RolloutBudget
from backend.agent.turn_budget import TurnBudgetController, TurnDeadlineController
from backend.agent.turn_kernel import TurnKernel
from backend.agent.turn_diff_tracker import TurnDiffTracker
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.grants import PermissionGrantStore
from backend.tools.registry import ToolRegistry


@dataclass(slots=True)
class AgentLoopBootstrapRequest:
    user_message: str
    llm: LLMAdapter
    tool_registry: ToolRegistry
    artifact_store: ArtifactStore
    permission_checker: Any
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
    run_context: RunContext | None = None
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
    permission_checker: Any
    session_id: str
    task_id: str
    cost_session_id: str
    agent_session: Any | None
    run_context: RunContext = field(default_factory=RunContext)


def _resolve_checkpoint_manager(state: AgentState) -> Any:
    """Return the turn's rewind manager, creating one when none was injected.

    ``snapshot_before_write`` refuses every write tool without a manager, so a
    caller that builds its own ``AgentState`` (the evaluation driver, the SDK
    entry point) would silently lose ``write_file``/``edit_file``/
    ``apply_patch``. The manager owns no session state, so defaulting it here
    keeps the rewind guarantee instead of disabling the tools that need it.
    """

    manager = getattr(state, "checkpoint_manager", None)
    if manager is not None:
        return manager
    from backend.checkpoint.manager import CheckpointManager

    manager = CheckpointManager()
    state.checkpoint_manager = manager
    return manager


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
    run_context = request.run_context
    session_context = request.session_context
    agent_session = (
        getattr(session_context, "agent_session", None)
        if session_context is not None
        else None
    )

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
        run_context = run_context or session_context.run_context

    external_metadata = metadata if isinstance(metadata, dict) else None
    resolved_metadata = dict(metadata or {})
    run_context = run_context or RunContext()
    run_context.skill_manager = skill_manager
    lifecycle_runtime = resolve_lifecycle_runtime(
        session_context=session_context,
        run_context=run_context,
    )
    run_context.lifecycle_runtime = lifecycle_runtime
    cancel_event = (
        session_context.cancel_event
        if session_context is not None and session_context.cancel_event is not None
        else run_context.cancel_event or resolved_metadata.get("cancel_event")
    )
    if not isinstance(cancel_event, asyncio.Event):
        cancel_event = None

    settings = request.agent_settings or AgentSettings()
    rollout_budget = resolved_metadata.get("_rollout_budget")
    if not isinstance(rollout_budget, RolloutBudget):
        rollout_budget = RolloutBudget(
            token_limit=max(0, int(settings.max_turn_tokens or 0))
        )
    resolved_metadata["_rollout_budget"] = rollout_budget

    budget = request.token_budget or TokenBudget()
    context = request.context_builder or ContextBuilder(
        token_budget=budget,
        agent_settings=settings,
        llm=request.llm,
    )
    run_context.context_builder = context
    context_tool = request.tool_registry.get_tool("new_context")
    model_snapshot = run_context.model_execution
    if model_snapshot is not None:
        from backend.agent.token_budget_activation import register_context_window_tools
        if (context_tool is not None
                and request.tool_registry.get_tool_owner("new_context") != "codex-token-budget"
                and model_snapshot.config.context_window_config is None
                and model_snapshot.config.token_budget_startup_config is None):
            from backend.feature_flags import FeatureFlags
            source_config = replace(model_snapshot.config,
                feature_flags=FeatureFlags({**model_snapshot.config.feature_flags.flags, "token_budget": True}),
                context_window_config=context_tool.config)
            model_snapshot = replace(model_snapshot, config=source_config)
            run_context.model_execution = model_snapshot
        context.bind_context_window_model(model_snapshot,
            startup=context._context_window_source_config is None,
            inherit_activation=resolved_metadata.get("fork_turns", resolved_metadata.get("_fork_turns")) == "all")
        register_context_window_tools(request.tool_registry, context._context_window_config)
    else:
        context.configure_context_window(context_tool.config if context_tool is not None else None)
    if run_context.llm_turn_context is not None:
        run_context.llm_turn_context.lifecycle_runtime = lifecycle_runtime
        run_context.llm_turn_context.cost_session_id = str(
            run_context.cost_session_id or session_id or ""
        )
        context.bind_llm_turn_context(run_context.llm_turn_context)
    read_file_hashes = getattr(context, "read_file_hashes", None)
    if callable(read_file_hashes):
        # Share the context-owned map rather than copying it. Reads and writes
        # during this turn then advance the same state exported in the next
        # conversation snapshot, matching CC's cumulative readFileState.
        resolved_metadata["_read_file_hashes"] = read_file_hashes()
    resolved_metadata["_executor_read_file_hashes"] = context.executor_read_file_hashes()
    resolved_metadata["_executor_observed_files"] = context.executor_observed_files()
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
            user_message=user_message,
            max_iterations=initial_max_iterations_limit,
        )

    deadline_controller = (
        session_context.deadline_controller if session_context is not None else None
    ) or TurnDeadlineController(
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
        run_context=run_context,
    )
    for event in turn_kernel.start_events():
        yield event
    context.bind_history_notes(
        request.tool_registry, model_snapshot,
        session_id=session_id, agent_name=AgentPath.parse(turn_kernel.run_record.agent_path).logical_name,
    )

    # The absolute turn fence includes hooks and skill preflight, rather than
    # starting only at the first provider request.
    turn_started_at = turn_kernel.run_record.started_at
    deadline_controller.start_turn()
    workspace_root = session_context.workspace_root if session_context is not None else None
    if workspace_root is None:
        raw_workspace_root = resolved_metadata.get("workspace_root")
        if str(raw_workspace_root or "").strip():
            workspace_root = Path(str(raw_workspace_root)).expanduser().resolve()
    if (
        workspace_root is None
        and state.workspace_context
        and hasattr(state.workspace_context, "root_path")
    ):
        workspace_root = state.workspace_context.root_path

    stream_retry_policy = (
        settings.stream_retry_policy or DefaultStreamRetryPolicy(settings)
    )
    if permission_context is not None:
        effective_permission_context = permission_context
    elif isinstance(request.permission_checker, PermissionChecker):
        effective_permission_context = request.permission_checker.build_context(mode="confirm")
    else:
        effective_permission_context = PermissionContext(mode="confirm")
    from backend.security.sensitive_files import application_state_roots

    protected_state_roots = application_state_roots(run_context)
    effective_permission_context = replace(
        effective_permission_context,
        protected_state_roots=tuple(dict.fromkeys((
            *effective_permission_context.protected_state_roots,
            *protected_state_roots,
        ))),
    )
    from backend.config import load_config_layer_stack

    turn_config_stack = (model_snapshot.config.config_layer_stack
        if model_snapshot is not None and model_snapshot.config.config_layer_stack is not None
        else load_config_layer_stack(cwd=workspace_root))
    if run_context.model_execution is None:
        from backend.agent.model_execution import ModelExecutionSnapshot
        from backend.config import load_config
        run_context.model_execution = ModelExecutionSnapshot.capture(load_config(cwd=workspace_root), request.llm)
        model_snapshot = run_context.model_execution
    elif model_snapshot.config.config_layer_stack is None:
        model_snapshot = replace(model_snapshot, config=replace(model_snapshot.config, config_layer_stack=turn_config_stack))
        run_context.model_execution = model_snapshot
    resolved_metadata["_concurrent_reasoning_summaries"] = model_snapshot.config.feature_flags.enabled(
        "concurrent_reasoning_summaries", False
    )
    from backend.skills.manager import SkillManager

    if isinstance(skill_manager, SkillManager):
        skill_turn_id = str(turn_kernel.metadata["turn_id"])
        context.bind_skill_manager(skill_manager, workspace_root, refresh=True, config_stack=turn_config_stack,
            force_reload=context._skill_snapshot_turn_id != skill_turn_id, turn_id=skill_turn_id)
        skill_manager = context.skill_manager
        run_context.skill_manager = skill_manager
    managed_requirements = turn_config_stack.requirements
    if permission_context is None and not effective_permission_context.sandbox_mode:
        configured_sandbox = turn_config_stack.effective_config().get("sandbox_mode", "")
        effective_permission_context = replace(effective_permission_context,
            sandbox_mode=configured_sandbox if effective_permission_context.mode not in {"plan", "bypass"} else "")
    configure_project_instructions = getattr(
        context,
        "configure_project_instructions",
        None,
    )
    if callable(configure_project_instructions):
        configure_project_instructions(turn_config_stack.project_instruction_config())
    resolved_metadata["_config_fingerprint"] = turn_config_stack.fingerprint
    # The caller owns the permission evaluator.  In particular, SDK hosts and
    # tests may inject a duck-typed checker carrying stricter capability rules;
    # replacing it with a settings-derived checker silently discards that host
    # boundary.  Native checkers can safely be rebound to the turn workspace
    # while retaining their supplied policy snapshot.  Managed requirements
    # are layered below through PermissionContext and SandboxPolicy, so they
    # still constrain both native and external evaluators.
    turn_permission_checker = request.permission_checker
    if isinstance(turn_permission_checker, PermissionChecker):
        turn_permission_checker = turn_permission_checker.with_workspace_root(workspace_root)
    resolved_mode, requirement_violation = managed_requirements.resolve_permission_mode(
        effective_permission_context.mode,
        sandbox_mode=effective_permission_context.sandbox_mode,
        approval_policy=effective_permission_context.approval_policy_value(),
    )
    if requirement_violation is not None:
        raise requirement_violation
    requirement_source = ""
    if requirement_violation is not None and requirement_violation.source is not None:
        requirement_source = str(requirement_violation.source)
    approval_source = managed_requirements.source_for("allowed_approval_policies")
    sandbox_source = managed_requirements.source_for("allowed_sandbox_modes")
    effective_permission_context = replace(
        effective_permission_context,
        mode=resolved_mode,
        approval_policy=effective_permission_context.approval_policy,
        granular_approval=effective_permission_context.granular_approval,
        sandbox_mode=managed_requirements.sandbox_mode_for_permission_mode(
            resolved_mode, sandbox_mode=effective_permission_context.sandbox_mode,
        ),
        requirements_source=(
            requirement_source
            or str(approval_source or sandbox_source or "")
        ),
        source=(
            effective_permission_context.source
            if requirement_violation is None
            else f"managed_requirements:{requirement_source or 'system'}"
        ),
    )
    if managed_requirements.filesystem_deny_read:
        managed_constraints = {
            key: list(value)
            for key, value in effective_permission_context.filesystem_constraints.items()
        }
        managed_constraints["denylist"] = list(
            dict.fromkeys(
                [
                    *managed_requirements.filesystem_deny_read,
                    *managed_constraints.get("denylist", []),
                ]
            )
        )
        effective_permission_context = replace(
            effective_permission_context,
            filesystem_constraints=managed_constraints,
        )
    conversation_id = str(getattr(state, "conversation_id", "") or "").strip()
    from backend.agent.turn_trace import turn_trace_shared

    trace = turn_trace_shared(f"turn[{conversation_id}]")
    trace.mark("bootstrap.capabilities.start")
    if run_context.environment_manager is not None:
        from backend.agent.environment_runtime import refresh_run_environment_capabilities

        if not run_context.environment_host_captured:
            if run_context.environment_host_mcp_manager is None:
                run_context.environment_host_mcp_manager = run_context.mcp_manager
            if run_context.environment_host_skill_manager is None:
                run_context.environment_host_skill_manager = skill_manager
            run_context.environment_host_captured = True
        prepared_capabilities = await refresh_run_environment_capabilities(run_context,
            agent_session=agent_session, permission_context=effective_permission_context,
            config_stack=turn_config_stack, feature_flags=model_snapshot.config.feature_flags,
            registry=request.tool_registry, artifact_store=request.artifact_store,
            workspace_root=workspace_root, conversation_id=conversation_id,
            turn_id=str(turn_kernel.metadata["turn_id"]),
            mcp_optional_startup_grace_ms=model_snapshot.config.mcp_optional_startup_grace_ms)
        skill_manager = prepared_capabilities.skill_manager
        context.bind_skill_manager(skill_manager, workspace_root, config_stack=turn_config_stack)
        trace.mark("bootstrap.capabilities.done")
        for warning in prepared_capabilities.warnings:
            if emit_event is not None:
                await emit_event("system_notice", {"conversation_id": conversation_id,
                    "message": warning, "level": "warning"})
    skill_read_roots: list[str] = []
    if skill_manager is not None and run_context.environment_manager is None:
        readable_roots = getattr(skill_manager, "readable_roots", None)
        if callable(readable_roots):
            skill_read_roots = [str(path) for path in readable_roots()]

    def _with_skill_read_roots(
        constraints: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        """Grant the discovered Skill directories as read-only roots.

        Skills are program-owned resources, not workspace files: the catalog
        advertises absolute SKILL.md locations, so the model must be able to
        read the one it selected even when the workspace lives on another
        drive. This is applied to the turn snapshot *and* re-applied on every
        live permission refresh, because the session context that feeds the
        refresh never carries these turn-owned roots.
        """

        if not skill_read_roots:
            return constraints
        merged = {key: list(value) for key, value in constraints.items()}
        merged["readable_roots"] = list(dict.fromkeys([
            *merged.get("readable_roots", []),
            *skill_read_roots,
        ]))
        return merged

    if skill_read_roots:
        effective_permission_context = replace(
            effective_permission_context,
            filesystem_constraints=_with_skill_read_roots({
                key: list(value)
                for key, value in effective_permission_context.filesystem_constraints.items()
            }),
        )

    from backend.sandbox import (
        SandboxRunner,
        sandbox_policy_for_permission_context,
    )

    sandbox_workspace = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd()

    def _sandbox_policy_for(current: PermissionContext):
        if run_context.environment_manager is not None:
            primary = run_context.turn_environments.primary()
            if primary is None or primary.is_remote:
                return None
            from backend.workspace.executor_local_policy import local_sandbox_policy

            return local_sandbox_policy(primary.permission_scope.sandbox_context())
        policy = sandbox_policy_for_permission_context(
            sandbox_workspace,
            current,
            config_stack=turn_config_stack,
        )
        if policy.preflight_required:
            capability = SandboxRunner(policy).capability(cwd=sandbox_workspace)
            if not capability.available:
                raise RuntimeError(
                    "Managed sandbox is required but unavailable: "
                    f"{capability.reason}"
                )
        return policy

    turn_sandbox_policy = _sandbox_policy_for(effective_permission_context)
    conversation_id = str(getattr(state, "conversation_id", "") or "").strip()
    effective_permission_context = replace(
        effective_permission_context,
        **({"allow_unsandboxed_commands": turn_sandbox_policy.allow_unsandboxed_commands,
            "sandbox_fail_if_unavailable": turn_sandbox_policy.fail_if_unavailable,
            "sandbox_auto_allow_commands": turn_sandbox_policy.auto_allow_commands_if_sandboxed,
            "sandbox_excluded_commands": turn_sandbox_policy.excluded_commands}
            if turn_sandbox_policy is not None else {}),
        conversation_id=conversation_id,
        workspace_root=Path(workspace_root).expanduser().resolve() if workspace_root else None,
    )

    def _normalize_live_permission(current: PermissionContext) -> PermissionContext:
        live_mode, live_violation = managed_requirements.resolve_permission_mode(
            current.mode, sandbox_mode=current.sandbox_mode, approval_policy=current.approval_policy_value(),
        )
        if live_violation is not None:
            raise live_violation
        live_constraints = {
            key: list(value)
            for key, value in current.filesystem_constraints.items()
        }
        if managed_requirements.filesystem_deny_read:
            live_constraints["denylist"] = list(
                dict.fromkeys(
                    [
                        *managed_requirements.filesystem_deny_read,
                        *live_constraints.get("denylist", []),
                    ]
                )
            )
        normalized = replace(
            current,
            protected_state_roots=tuple(dict.fromkeys((
                *current.protected_state_roots,
                *protected_state_roots,
            ))),
            mode=live_mode,
            approval_policy=current.approval_policy,
            granular_approval=current.granular_approval,
            sandbox_mode=managed_requirements.sandbox_mode_for_permission_mode(
                live_mode, sandbox_mode=current.sandbox_mode,
            ),
            filesystem_constraints=_with_skill_read_roots(live_constraints),
            requirements_source=(
                str(live_violation.source)
                if live_violation is not None and live_violation.source is not None
                else effective_permission_context.requirements_source
            ),
        )
        live_policy = _sandbox_policy_for(normalized)
        return replace(
            normalized,
            **({"allow_unsandboxed_commands": live_policy.allow_unsandboxed_commands,
                "sandbox_fail_if_unavailable": live_policy.fail_if_unavailable,
                "sandbox_auto_allow_commands": live_policy.auto_allow_commands_if_sandboxed,
                "sandbox_excluded_commands": live_policy.excluded_commands}
                if live_policy is not None else {}),
            conversation_id=conversation_id,
            workspace_root=Path(workspace_root).expanduser().resolve() if workspace_root else None,
        )

    hook_scope_id = str(session_id or "").strip() if run_context.is_subagent else conversation_id or str(session_id or "").strip()
    context.bind_background_commands(background_manager)
    run_context.turn_diff_tracker = TurnDiffTracker()
    tool_context = ToolExecutionContext(
        permission=effective_permission_context,
        session_id=session_id,
        task_id=task_id,
        metadata=dict(resolved_metadata),
        run_context=run_context,
        cancel_event=cancel_event,
        pending_cleanup_tasks=run_context.lifecycle_cleanup_tasks,
        emit_event=emit_event,
        approval_handler=request.approval_handler,
        stream_callback=stream_callback,
        workspace_root=workspace_root,
        allow_network=turn_sandbox_policy.allow_network if turn_sandbox_policy is not None else False,
        sandbox_policy=turn_sandbox_policy,
        task_manager=task_manager,
        background_manager=background_manager,
        terminal_manager=(
            getattr(session_context, "terminal_manager", None)
            if session_context is not None
            else None
        ),
        checkpoint_manager=_resolve_checkpoint_manager(state),
        permission_checker=turn_permission_checker,
        conversation_id=conversation_id,
        llm=request.llm,
        artifact_store=request.artifact_store,
        turn_diff_tracker=run_context.turn_diff_tracker,
        window_binding_token=run_context.window_binding_token,
        permission_grants=(
            session_context.permission_grants
            if session_context is not None and session_context.permission_grants is not None
            else PermissionGrantStore()
        ),
        turn_id=str(turn_kernel.metadata["turn_id"]),
        environment_id=(run_context.turn_environments.primary().selection.environment_id
            if run_context.turn_environments is not None and run_context.turn_environments.primary() is not None
            else "" if run_context.turn_environments is not None else "local"),
    )
    tool_context.permission_grants.begin_turn(tool_context.turn_id)
    from backend.permissions.exec_policy import ExecPolicyOwner, config_folders
    from backend.async_cleanup import to_thread_cancel_safe
    owner = context._exec_policy_owner
    if (owner is None or owner.folders != config_folders(turn_config_stack)
            or owner._requirements_rules != turn_config_stack.requirements.value_for("rules")):
        home = Path(next(layer.source.file for layer in reversed(turn_config_stack.get_layers()) if layer.source.kind == "user")).parent
        owner = await to_thread_cancel_safe(ExecPolicyOwner, home=home, stack=turn_config_stack)
        context._exec_policy_owner = owner
        if owner.load_warning is not None and emit_event is not None:
            await emit_event("system_notice", {"conversation_id": conversation_id,
                "message": owner.load_warning.message, "level": "warning"})
    run_context.exec_policy_owner = owner
    if run_context.turn_environments is not None:
        from backend.network.runtime import prepare_environment_network_step

        trace.mark("bootstrap.companions.start")
        run_context.network_step = await prepare_environment_network_step(
            environment_manager=run_context.environment_manager, snapshot=run_context.turn_environments,
            config_stack=turn_config_stack, exec_policy_sources=owner.sources)
        from backend.mcp.node_repl import register_node_companion

        await register_node_companion(run_context, request.tool_registry, agent_session, execution=run_context.model_execution)
        from backend.mcp.cua_repl import register_cua_companion

        await register_cua_companion(run_context, agent_session, execution=run_context.model_execution)
        trace.mark("bootstrap.companions.done")
        # Capabilities, network step and companions are ready for this turn;
        # the iteration boundary reuses them instead of rebuilding the surface.
        run_context.environment_prepared_turn_id = str(turn_kernel.metadata["turn_id"])
    context.bind_tool_context(tool_context)
    from backend.agent.model_activation import admit_model_activation

    admit_model_activation(run_context, task=run_context.model_owner_task, cancel_event=cancel_event,
        permission=effective_permission_context, turn_id=tool_context.turn_id)

    def _commit_live_permission_context(
        current: PermissionContext,
    ) -> tuple[PermissionContext, Any]:
        normalized = _normalize_live_permission(current)
        policy = _sandbox_policy_for(normalized)
        tool_context.permission = normalized
        tool_context.sandbox_policy = policy
        tool_context.allow_network = bool(policy.allow_network) if policy is not None else False
        return normalized, policy

    if workspace_root is not None and "cwd" not in tool_context.metadata:
        tool_context.metadata["cwd"] = str(workspace_root)
    populate_prompt_context(
        state=state,
        metadata=resolved_metadata,
        workspace_root=workspace_root,
        permission_context=effective_permission_context,
        run_context=run_context,
    )
    tool_context.metadata["prompt_context"] = state.prompt_context
    tool_context.metadata["_context_builder"] = context
    tool_context.metadata["_agent_state"] = state
    if run_context.extension_actions is not None:
        run_context.extension_actions.tool_context = tool_context
    # Extension host actions resolve the live canonical execution context from
    # the turn metadata.  Keeping the object here avoids a second command
    # runtime while still allowing a generation-bound extension to execute
    # through the same permission, sandbox, cancellation, and artifact path
    # as a model-issued tool call.
    tool_context.metadata["_tool_execution_context"] = tool_context
    # Expose lifecycle evidence without leaking it to provider prompts.  The
    # QueryEngine/host can now account for cancellation-resistant tool cleanup
    # after the user-visible turn terminal has been committed.
    tool_context.metadata["_pending_tool_cleanup_tasks"] = tool_context.pending_cleanup_tasks
    tool_context.metadata["_tool_cleanup_receipts"] = tool_context.cleanup_receipts
    if isinstance(external_metadata, dict):
        external_metadata["_tool_execution_context"] = tool_context
    # Internal coordination tools need the live registry to resume a stopped
    # TaskTool agent from its durable sidechain transcript. Keep this runtime
    # handle out of model-visible prompt context.
    tool_context.tool_registry = request.tool_registry
    if hook_scope_id:
        tool_context.metadata["hook_session_id"] = hook_scope_id
    lifecycle_runtime = resolve_lifecycle_runtime(
        run_context=run_context,
    )
    if lifecycle_runtime is not None:
        assert_active = getattr(lifecycle_runtime, "assert_active", None)
        if callable(assert_active):
            assert_active()
        bound_registry = getattr(lifecycle_runtime, "_tool_registry", None)
        if bound_registry is None:
            bind_tool_registry = getattr(lifecycle_runtime, "bind_tool_registry", None)
            if not callable(bind_tool_registry):
                raise TypeError(
                    "lifecycle runtime does not expose bind_tool_registry(registry)"
                )
            bind_tool_registry(request.tool_registry)
        elif (
            bound_registry is not request.tool_registry
            and bound_registry.schema_source is not request.tool_registry.schema_source
        ):
            # Child registries fork the generation's capability maps and retain
            # its schema source and extension adapters. They use the shared
            # lifecycle through their own execution scope without rebinding it.
            # An independently rebuilt registry still needs a fresh generation.
            raise RuntimeError(
                "lifecycle runtime is already bound to a different ToolRegistry; "
                "publish a fresh generation for the replacement session/registry"
            )
    tool_context.metadata["_permission_context_normalizer"] = _normalize_live_permission
    tool_context.metadata["_sandbox_policy_factory"] = _sandbox_policy_for
    tool_context.permission_context_committer = _commit_live_permission_context
    tool_context.deadline_monotonic = deadline_controller.turn_deadline
    turn_kernel.bind_tool_context(tool_context)
    if run_context.guardian_async_service is not None:
        run_context.guardian_async_service.schedule_prewarm(tool_context)

    # Resolve hooks once for this turn and keep the exact manager on RunContext.
    from backend.hooks.manager import (
        load_hook_manager_for_workspace,
        register_hook_manager_for_session,
    )

    hook_manager = run_context.hook_manager
    if run_context.is_subagent and agent_session is not None:
        if agent_session.hook_manager is not None:
            hook_manager = agent_session.hook_manager
        elif hook_manager is not None:
            hook_manager = hook_manager.fork_for_subagent(workspace_root=workspace_root, session_id=hook_scope_id)
            agent_session.hook_manager = hook_manager
    if hook_manager is None:
        hook_manager = load_hook_manager_for_workspace(
            workspace_root,
            requirements=managed_requirements,
            config_layer_stack=turn_config_stack,
            session_id=hook_scope_id,
            owner_session_id=session_id,
        )
        if run_context.is_subagent and agent_session is not None:
            agent_session.hook_manager = hook_manager
    if run_context.is_subagent and resolved_metadata.get("_subagent_hook_start_source") == "resume":
        hook_manager._session_started = True
    hook_manager.bind_runtime(
        llm=request.llm,
        tool_registry=request.tool_registry,
        tool_context=tool_context,
    )
    run_context.hook_manager = hook_manager
    context.bind_hook_manager(hook_manager)
    from backend.network.approval import NetworkApprovalService

    network_owner = run_context.environment_thread_runtime or agent_session or run_context
    network_service = network_owner.network_approval_service
    if network_service is None or network_service.conversation_id != tool_context.command_scope_id:
        network_service = NetworkApprovalService(conversation_id=tool_context.command_scope_id)
        network_owner.network_approval_service = network_service
    run_context.network_approval_service = network_service
    if agent_session is not None:
        network_service.bind_review_source(lambda: agent_session.context_builder._tool_execution_context
            if agent_session.active_turn else None)
    trace.mark("bootstrap.preflight.start")
    preflight = await prepare_turn_input(
        user_message,
        state=state,
        turn_kernel=turn_kernel,
        session_id=hook_scope_id,
        deadline=deadline_controller.turn_deadline,
        cancel_event=cancel_event,
        hook_manager=hook_manager,
        run_context=run_context,
        llm=request.llm,
        input_restored=bool(
            resolved_metadata.get("_query_engine_recovery_restored")
            or resolved_metadata.get("_turn_admission_restored")
        ),
    )
    user_message = preflight.user_message
    trace.mark("bootstrap.preflight.done")
    # Preserve SessionStart side-channel values in the turn metadata so
    # downstream prompt/watcher integrations cannot accidentally lose them
    # when the bootstrap object is handed to the main loop.
    # Keep bootstrap compatible with older/custom preflight providers that
    # return a small namespace instead of the current dataclass.  Session
    # side-channel fields are additive and must never make an otherwise
    # valid turn fail during migration.
    initial_user_message = str(
        getattr(preflight, "initial_user_message", "") or ""
    ).strip()
    watch_paths = getattr(preflight, "watch_paths", ())
    if initial_user_message:
        resolved_metadata["hook_initial_user_message"] = initial_user_message
    if watch_paths:
        resolved_metadata["hook_watch_paths"] = list(watch_paths)
    if not run_context.is_subagent:
        register_hook_manager_for_session(
            hook_scope_id,
            hook_manager,
            owner_session_id=session_id,
        )
    chain = QueryChainTracking(
        user_message_preview=user_message[:100],
        source=str(resolved_metadata.get("query_source") or "user"),
    )
    bootstrap_result = AgentLoopBootstrap(
        user_message=user_message,
        skill_manager=skill_manager,
        emit_event=emit_event,
        metadata=resolved_metadata,
        run_context=run_context,
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
        permission_checker=turn_permission_checker,
        session_id=session_id,
        task_id=task_id,
        cost_session_id=str(
            run_context.cost_session_id
            or session_id
            or ""
        ).strip(),
        agent_session=agent_session,
    )
    if run_context.coordination_root_context is None:
        run_context.coordination_root_context = tool_context
    if not run_context.multi_agent_version and tool_context.model_execution is not None:
        from backend.agents.source_roles import source_spawn_version
        snapshot = tool_context.model_execution
        run_context.multi_agent_version = source_spawn_version(snapshot.config, model=snapshot.model, definition=snapshot.model_info)
    if turn_kernel.runtime is not None:
        tool_context.metadata["_agent_wait_cursor"] = turn_kernel.runtime.agent_activity_cursor()
    from backend.agent.message_board_remote import start_remote_board_notifications

    await start_remote_board_notifications(tool_context)
    yield bootstrap_result
