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
from backend.agent.lifecycle_observer import (
    install_lifecycle_runtime,
    resolve_lifecycle_runtime,
)
from backend.agent.loop_preflight import prepare_turn_input
from backend.agent.loop_runtime_helpers import epoch_ms
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
    hook_manager_token: Any | None
    provider_hook_runner_token: Any | None
    agent_session: Any | None


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

    external_metadata = metadata if isinstance(metadata, dict) else None
    resolved_metadata = dict(metadata or {})
    lifecycle_runtime = resolve_lifecycle_runtime(
        resolved_metadata,
        session_context=session_context,
    )
    install_lifecycle_runtime(resolved_metadata, lifecycle_runtime)
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
        rollout_budget = RolloutBudget(
            token_limit=max(0, int(settings.max_turn_tokens or 0))
        )
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
    effective_permission_context = permission_context or PermissionContext(mode="confirm")
    from backend.config import load_config_layer_stack

    turn_config_stack = load_config_layer_stack(cwd=workspace_root)
    managed_requirements = turn_config_stack.requirements
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
    if isinstance(turn_permission_checker, PermissionChecker) and workspace_root is not None:
        turn_permission_checker = turn_permission_checker.with_workspace_root(workspace_root)
    resolved_mode, requirement_violation = managed_requirements.resolve_permission_mode(
        effective_permission_context.mode
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
        approval_policy=managed_requirements.approval_policy_for_mode(resolved_mode),
        sandbox_mode=managed_requirements.sandbox_mode_for_permission_mode(resolved_mode),
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
    skill_read_roots: list[str] = []
    if skill_manager is not None:
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
        configured_allowlist = merged.get("allowlist")
        if configured_allowlist is None and turn_permission_checker is not None:
            policy_snapshot = getattr(turn_permission_checker, "policy_snapshot", None)
            if callable(policy_snapshot):
                configured_allowlist = list(
                    policy_snapshot().get("path_allowlist", [])
                )
        merged["allowlist"] = list(dict.fromkeys([
            *(configured_allowlist or []),
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
        allow_unsandboxed_commands=turn_sandbox_policy.allow_unsandboxed_commands,
        sandbox_fail_if_unavailable=turn_sandbox_policy.fail_if_unavailable,
        sandbox_auto_allow_commands=turn_sandbox_policy.auto_allow_commands_if_sandboxed,
        sandbox_excluded_commands=turn_sandbox_policy.excluded_commands,
        conversation_id=conversation_id,
        workspace_root=Path(workspace_root).expanduser().resolve() if workspace_root else None,
    )

    def _normalize_live_permission(current: PermissionContext) -> PermissionContext:
        live_mode, live_violation = managed_requirements.resolve_permission_mode(current.mode)
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
            mode=live_mode,
            approval_policy=managed_requirements.approval_policy_for_mode(live_mode),
            sandbox_mode=managed_requirements.sandbox_mode_for_permission_mode(live_mode),
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
            allow_unsandboxed_commands=live_policy.allow_unsandboxed_commands,
            sandbox_fail_if_unavailable=live_policy.fail_if_unavailable,
            sandbox_auto_allow_commands=live_policy.auto_allow_commands_if_sandboxed,
            sandbox_excluded_commands=live_policy.excluded_commands,
            conversation_id=conversation_id,
            workspace_root=Path(workspace_root).expanduser().resolve() if workspace_root else None,
        )

    hook_scope_id = conversation_id or str(session_id or "").strip()
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
        allow_network=turn_sandbox_policy.allow_network,
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
        turn_diff_tracker=TurnDiffTracker(),
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
    tool_context.metadata["_agent_state"] = state
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
    tool_context.metadata["_tool_registry"] = request.tool_registry
    if hook_scope_id:
        tool_context.metadata["hook_session_id"] = hook_scope_id
    lifecycle_runtime = resolve_lifecycle_runtime(resolved_metadata)
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
        elif bound_registry is not request.tool_registry:
            # Rebinding would detach adapters from a registry that an in-flight
            # turn may still own. MiniCode swaps the whole lifecycle generation
            # on reload, so require the same ownership discipline here.
            raise RuntimeError(
                "lifecycle runtime is already bound to a different ToolRegistry; "
                "publish a fresh generation for the replacement session/registry"
            )
    tool_context.metadata["_permission_context_normalizer"] = _normalize_live_permission
    tool_context.metadata["_sandbox_policy_factory"] = _sandbox_policy_for
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

    hook_manager = load_hook_manager_for_workspace(
        workspace_root,
        requirements=managed_requirements,
        config_layer_stack=turn_config_stack,
        session_id=hook_scope_id,
    )
    hook_manager.bind_runtime(
        llm=request.llm,
        tool_registry=request.tool_registry,
        tool_context=tool_context,
    )
    hook_manager_token = bind_hook_manager(hook_manager)
    provider_hook_runner_token = LLMAdapter.bind_provider_lifecycle_runtime(
        lifecycle_runtime
    )
    try:
        preflight = await prepare_turn_input(
            user_message,
            state=state,
            turn_kernel=turn_kernel,
            session_id=hook_scope_id,
            deadline=deadline_controller.turn_deadline,
            cancel_event=cancel_event,
            resume_from_checkpoint=bool(
                resolved_metadata.get("_query_engine_recovery_restored")
            ),
        )
        user_message = preflight.user_message
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
                resolved_metadata.get("cost_session_id") or session_id or ""
            ).strip(),
            hook_manager_token=hook_manager_token,
            provider_hook_runner_token=provider_hook_runner_token,
            agent_session=agent_session,
        )
    except BaseException:
        LLMAdapter.unbind_provider_lifecycle_runtime(provider_hook_runner_token)
        unbind_hook_manager(hook_manager_token)
        raise
    yield bootstrap_result
