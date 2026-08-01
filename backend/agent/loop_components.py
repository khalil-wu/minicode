"""Wire the stable collaborators used by the Agent iteration loop."""

from __future__ import annotations

from dataclasses import dataclass
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
from backend.agent.message import AgentEvent
from backend.agent.provider_completion import ProviderCompletionCoordinator
from backend.agent.provider_protocol import (
    build_llm_request_metadata,
    prompt_cache_tracking_source,
    provider_stateful_history_preferred,
    set_context_stateful_history_preference,
)
from backend.agent.response_utils import append_assistant_history
from backend.agent.tool_schema_derivation import (
    TurnToolSchemaDerivation,
    derive_turn_tool_schema_state,
    workspace_bound_tool_names,
)
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.turn_budget_runtime import TurnBudgetRuntime
from backend.agent.turn_iteration_runtime import TurnIterationRuntime
from backend.agent.turn_kernel import _set_terminal_reason
from backend.llm.base import UsageInfo
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


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
    prefer_stateful_history: bool
    provider_completion: ProviderCompletionCoordinator


def build_agent_loop_components(
    *,
    bootstrap: AgentLoopBootstrap,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    usage: Callable[[], UsageInfo],
) -> AgentLoopComponents:
    """Build the dependency graph after preflight and skill activation."""

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

    active_toolset_policy = active_toolset_policy_for_context(
        permission_context=tool_context.permission,
    )
    base_tool_schemas = tool_registry.get_schemas(
        budget=bootstrap.budget.tool_schemas,
        permission_checker=permission_checker,
        permission_context=tool_context.permission,
        toolset_policy=active_toolset_policy,
        mcp_registry_version=mcp_registry_version(),
    )
    if (
        bool(metadata.get("requires_explicit_workspace"))
        and bootstrap.workspace_root is None
        and tool_context.permission.mode != "bypass"
    ):
        workspace_bound_tools = workspace_bound_tool_names(base_tool_schemas)
        if workspace_bound_tools:
            state.disable_tools(workspace_bound_tools)

    mcp_instructions = collect_mcp_instructions()
    turn_tool_schema_state = derive_turn_tool_schema_state(
        base_tool_schemas=base_tool_schemas,
        disabled_tools=state.disabled_tools,
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
        tool_schema_budget=bootstrap.budget.tool_schemas,
        mcp_registry_version=mcp_registry_version,
        active_toolset_policy_factory=active_toolset_policy_for_context,
        populate_prompt_context=populate_prompt_context,
        run_record=run_record,
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
            run_stop_failure_hook=run_stop_failure_hook,
            terminal_event=lambda status, reason: _usage_done_event(
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
            turn_kernel=turn_kernel,
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
    )
    tracking_source = prompt_cache_tracking_source(
        run_record=run_record,
        session_id=bootstrap.session_id,
        task_id=bootstrap.task_id,
    )
    prefer_stateful_history = provider_stateful_history_preferred(
        bootstrap.tool_context.llm
    )
    set_context_stateful_history_preference(context, prefer_stateful_history)
    provider_completion = ProviderCompletionCoordinator(
        settings=settings,
        state=state,
        context_builder=context,
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
        prefer_stateful_history=prefer_stateful_history,
        provider_completion=provider_completion,
    )


def _usage_done_event(
    usage: UsageInfo,
    *,
    status: str,
    reason: str,
) -> AgentEvent:
    return AgentEvent.done(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_deleted_input_tokens=usage.cache_deleted_input_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        input_includes_cache_read=usage.input_includes_cache_read,
        status=status,
        reason=reason,
    )
