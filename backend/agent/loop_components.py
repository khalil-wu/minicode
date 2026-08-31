"""Wire the stable collaborators used by the Agent iteration loop."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from backend.agent.budget_termination import (
    BudgetTerminationCoordinator,
    BudgetTerminationDependencies,
)
from backend.agent.answer_committer import (
    AnswerCommitDependencies,
    AnswerCommitter,
)
from backend.agent.loop_bootstrap import AgentLoopBootstrap
from backend.agent.loop_preflight import run_stop_failure_hook
from backend.agent.loop_session import (
    active_toolset_policy_for_context,
    collect_mcp_instructions,
    mcp_registry_version,
    populate_prompt_context,
)
from backend.agent.provider_completion import ProviderCompletionCoordinator
from backend.agent.provider_protocol import (
    build_llm_request_metadata,
    prompt_cache_tracking_source,
    usage_terminal_projection,
)
from backend.agent.response_utils import append_assistant_history
from backend.agent.tool_schema_derivation import (
    TurnToolSchemaDerivation,
    derive_turn_tool_schema_state,
    effective_toolset_policy,
)
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.turn_budget_runtime import TurnBudgetRuntime
from backend.agent.turn_iteration_runtime import TurnIterationRuntime
from backend.agent.turn_kernel import _set_terminal_reason
from backend.llm.base import UsageInfo
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import (
    ACTIVE_TOOLSET_POLICY_METADATA_KEY,
    SESSION_TOOLSET_POLICY_METADATA_KEY,
    ToolsetPolicy,
)
from backend.tools.toolset_runtime import restore_toolset_policy


@dataclass(slots=True)
class AgentLoopComponents:
    pending_turn_context: list[str]
    turn_tool_schema_state: TurnToolSchemaDerivation
    iteration_runtime: TurnIterationRuntime
    turn_start_tool_call_count: int
    turn_budget_controller: TurnBudgetController
    budget_runtime: TurnBudgetRuntime
    answer_committer: AnswerCommitter
    llm_request_metadata: dict[str, Any]
    provider_completion: ProviderCompletionCoordinator


def build_agent_loop_components(
    *,
    bootstrap: AgentLoopBootstrap,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    usage: Callable[[], UsageInfo],
    skill_manager: Any | None = None,
) -> AgentLoopComponents:
    """Build the dependency graph after preflight."""

    state = bootstrap.state
    context = bootstrap.context
    settings = bootstrap.settings
    metadata = bootstrap.metadata
    tool_context = bootstrap.tool_context
    turn_kernel = bootstrap.turn_kernel
    run_record = turn_kernel.run_record

    pending_turn_context: list[str] = []
    for hook_result in (
        bootstrap.session_hook_result,
        bootstrap.prompt_hook_result,
    ):
        if hook_result is None:
            continue
        if hook_result.has_feedback:
            pending_turn_context.append(hook_result.feedback)
        if hook_result.has_additional_context:
            pending_turn_context.append(hook_result.additional_context)

    configured_session_policy: ToolsetPolicy | None = None
    if SESSION_TOOLSET_POLICY_METADATA_KEY in metadata:
        configured_session_policy = restore_toolset_policy(
            metadata[SESSION_TOOLSET_POLICY_METADATA_KEY],
            label="session toolset policy",
        )
    elif ACTIVE_TOOLSET_POLICY_METADATA_KEY in metadata:
        # Older sessions only persisted the turn-effective policy. It is a safe,
        # narrower first-turn ceiling; new sessions immediately split the keys.
        configured_session_policy = restore_toolset_policy(
            metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY],
            label="legacy active toolset policy",
        )
    if configured_session_policy is not None:
        metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = configured_session_policy
    bootstrap.run_context.session_toolset_policy = configured_session_policy

    def current_session_toolset_policy() -> ToolsetPolicy | None:
        owner = bootstrap.agent_session
        active_names = getattr(owner, "active_tool_names", None)
        if active_names is not None:
            # Pi's active-tool list is a per-session selection, not a new
            # capability grant.  Intersect it with the durable ceiling so a
            # UI/extension update cannot erase parent denies, child profiles,
            # or availability filters.  Keep the immutable ceiling in its own
            # metadata slot; only the turn-effective policy is published as
            # ``ACTIVE_TOOLSET_POLICY_METADATA_KEY`` below.
            ceiling = configured_session_policy or ToolsetPolicy.default()
            policy = ceiling.with_active_tool_selection(active_names)
            # Delegation/resume must inherit the current session selection, not
            # fall back to the root default.  The closure retains ``ceiling``
            # separately so a later additive setActiveTools update in this
            # turn can still select another tool the parent was allowed to use.
            metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = policy
            # Tool execution and delegated child sessions receive the
            # bootstrap context's metadata object, while the loop's public
            # metadata may be a separate transport projection. Keep the
            # durable session selection on the execution context as well so
            # child creation cannot mistake a transient turn policy for its
            # inherited ceiling.
            tool_context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = policy
            return policy
        return configured_session_policy

    session_toolset_policy = current_session_toolset_policy()
    base_toolset_policy = active_toolset_policy_for_context(
        permission_context=tool_context.permission,
        session_policy=session_toolset_policy,
        metadata=metadata,
    )
    active_toolset_policy = effective_toolset_policy(
        base_policy=base_toolset_policy,
        tool_registry=tool_registry,
        disabled_tools=state.disabled_tools,
        requires_explicit_workspace=bootstrap.run_context.requires_explicit_workspace,
        workspace_root=bootstrap.workspace_root,
        permission_mode=str(tool_context.permission.mode or ""),
    )
    tool_context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY] = active_toolset_policy
    bootstrap.run_context.toolset_policy = active_toolset_policy
    mcp_manager = bootstrap.run_context.mcp_manager
    base_tool_schemas = tool_registry.get_schemas(
        permission_checker=permission_checker,
        permission_context=tool_context.permission,
        toolset_policy=active_toolset_policy,
        mcp_registry_version=mcp_registry_version(mcp_manager),
    )
    mcp_instructions = collect_mcp_instructions(mcp_manager)
    turn_tool_schema_state = derive_turn_tool_schema_state(
        base_tool_schemas=base_tool_schemas,
        mcp_instructions=mcp_instructions,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        permission_context=tool_context.permission,
        toolset_policy=active_toolset_policy,
    )
    state.prompt_context["tool_names"] = turn_tool_schema_state.tool_names
    state.tool_runtime_guidance = turn_tool_schema_state.runtime_guidance
    state.prompt_context["deferred_tools_prompt_block"] = (
        turn_tool_schema_state.deferred_tools_prompt_block
    )

    iteration_runtime = TurnIterationRuntime(
        context=context,
        state=state,
        llm=bootstrap.tool_context.llm,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        tool_context=tool_context,
        turn_kernel=turn_kernel,
        metadata=metadata,
        workspace_root=bootstrap.workspace_root,
        mcp_instructions=mcp_instructions,
        mcp_registry_version=lambda: mcp_registry_version(mcp_manager),
        active_toolset_policy_factory=lambda *, permission_context: (
            active_toolset_policy_for_context(
                permission_context=permission_context,
                session_policy=current_session_toolset_policy(),
                metadata=metadata,
            )
        ),
        populate_prompt_context=populate_prompt_context,
        run_record=run_record,
        skill_manager=skill_manager,
        agent_session=bootstrap.agent_session,
    )

    turn_start_tool_call_count = len(state.tool_calls)
    prepared_budget_controller = bootstrap.turn_budget_controller
    turn_budget_controller = (
        prepared_budget_controller
        if isinstance(prepared_budget_controller, TurnBudgetController)
        else TurnBudgetController.from_settings(
            settings,
            max_iterations=bootstrap.initial_max_iterations_limit,
        )
    )
    budget_termination = BudgetTerminationCoordinator(
        BudgetTerminationDependencies(
            state=state,
            context=context,
            set_terminal_reason=_set_terminal_reason,
            run_stop_failure_hook=partial(
                run_stop_failure_hook,
                hook_manager=bootstrap.run_context.hook_manager,
            ),
            terminal_projection=lambda status, reason: usage_terminal_projection(
                usage(),
                status=status,
                reason=reason,
            ),
        )
    )
    answer_committer = AnswerCommitter(
        AnswerCommitDependencies(
            context=context,
            state=state,
            append_assistant_history=append_assistant_history,
            set_terminal_reason=_set_terminal_reason,
        )
    )
    budget_runtime = TurnBudgetRuntime(
        state=state,
        tool_context=tool_context,
        usage=usage,
        rollout_budget=bootstrap.rollout_budget,
        deadlines=bootstrap.deadline_controller,
        controller=turn_budget_controller,
        termination=budget_termination,
        cost_session_id=bootstrap.cost_session_id,
    )

    llm_request_metadata = build_llm_request_metadata(
        metadata=metadata,
        session_id=bootstrap.session_id,
        task_id=bootstrap.task_id,
        workspace_root=bootstrap.workspace_root,
        run_id=run_record.run_id,
        conversation_id=run_record.conversation_id,
        provider_lifecycle_runtime=bootstrap.run_context.lifecycle_runtime,
    )
    tracking_source = prompt_cache_tracking_source(
        run_record=run_record,
        session_id=bootstrap.session_id,
        task_id=bootstrap.task_id,
    )
    provider_completion = ProviderCompletionCoordinator(
        settings=settings,
        state=state,
        turn_kernel=turn_kernel,
        prompt_cache_tracking_source=tracking_source,
        turn_started_at=bootstrap.turn_started_at,
        turn_start_tool_call_count=turn_start_tool_call_count,
    )
    return AgentLoopComponents(
        pending_turn_context=pending_turn_context,
        turn_tool_schema_state=turn_tool_schema_state,
        iteration_runtime=iteration_runtime,
        turn_start_tool_call_count=turn_start_tool_call_count,
        turn_budget_controller=turn_budget_controller,
        budget_runtime=budget_runtime,
        answer_committer=answer_committer,
        llm_request_metadata=llm_request_metadata,
        provider_completion=provider_completion,
    )
