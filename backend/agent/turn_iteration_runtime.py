"""Prepare one model iteration before context budgeting and provider I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.agent_identity import AgentPath
from backend.agent.skill_activation import activate_turn_skills
from backend.agent.tool_schema_derivation import (
    TurnToolSchemaDerivation,
    derive_turn_tool_schema_state,
    effective_toolset_policy,
    requested_tool_mode,
)
from backend.llm.capabilities import (
    capabilities_for_adapter,
    is_gpt_image_model,
    require_tool_calling,
)
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy
from backend.tools.registry import ToolRegistry
from backend.tools.realtime_tools import active_realtime_service
from backend.services.context_budget import build_context_compacted_event, context_ledger_snapshot


@dataclass(slots=True)
class TurnIterationPreparation:
    tool_schemas: list[dict[str, Any]]
    tool_schema_state: TurnToolSchemaDerivation
    events: list[AgentEvent] = field(default_factory=list)
    terminal: bool = False


class TurnIterationRuntime:
    """Own the mutable capability boundary at the start of every iteration.

    Permissions, visible tools, queued steering, and provider capabilities can
    all change between provider calls. Keeping them together prevents the loop
    from building context with one tool set and sending a different one.
    """

    def __init__(
        self,
        *,
        context: Any,
        state: Any,
        llm: Any,
        tool_registry: ToolRegistry,
        permission_checker: Any,
        tool_context: Any,
        turn_kernel: Any,
        metadata: dict[str, Any],
        workspace_root: Any,
        mcp_catalog: Any,
        active_toolset_policy_factory: Any,
        populate_prompt_context: Any,
        run_record: Any,
        skill_manager: Any | None = None,
        agent_session: Any | None = None,
        code_mode_only: bool = False,
    ) -> None:
        self.context = context
        self.state = state
        self.llm = llm
        self.tool_registry = tool_registry
        self.base_tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.tool_context = tool_context
        self.turn_kernel = turn_kernel
        self.metadata = metadata
        self.workspace_root = workspace_root
        self.mcp_catalog = mcp_catalog
        self.active_toolset_policy_factory = active_toolset_policy_factory
        self.populate_prompt_context = populate_prompt_context
        self.run_record = run_record
        self.skill_manager = skill_manager
        self.agent_session = agent_session
        self.code_mode_only = code_mode_only
        self._base_schema_key: tuple[Any, ...] | None = None
        self._base_schema_permission: Any = None
        self._base_schemas: list[dict[str, Any]] = []

    def sync_active_session_model(self, snapshot: Any) -> Any:
        """Capture the selection before context preparation and provider I/O."""

        owner = self.agent_session
        self.tool_context.run_context.active_model_execution = snapshot
        self.tool_context.model_execution = snapshot
        active_llm = snapshot.llm if snapshot is not None else getattr(owner, "llm", None)
        if snapshot is not None and owner is not None:
            owner.llm = snapshot.llm
            owner.token_budget = snapshot.config.token_budget
        if active_llm is not None:
            self.llm = active_llm
            self.tool_context.llm = active_llm
            self.context.bind_llm(active_llm)
        active_budget = snapshot.config.token_budget if snapshot is not None else getattr(owner, "token_budget", None)
        if active_budget is not None:
            self.context.bind_budget(active_budget)
        active_registry = (
            getattr(owner, "tool_registry", None) if owner is not None else None
        ) or self.base_tool_registry
        self.base_tool_registry = active_registry
        context_tool = active_registry.get_tool("new_context")
        if snapshot is not None and self.context._context_window_source_config is not None:
            from backend.agent.token_budget_activation import register_context_window_tools
            self.context.bind_context_window_model(snapshot)
            register_context_window_tools(active_registry, self.context._context_window_config)
        else:
            self.context.configure_context_window(context_tool.config if context_tool is not None else None)
        self.context.bind_history_notes(
            active_registry, snapshot,
            session_id=self.tool_context.run_context.responses_session_id,
            agent_name=AgentPath.parse(self.turn_kernel.run_record.agent_path).logical_name,
        )
        if self.tool_context.run_context.guardian_async_service is not None:
            self.tool_context.run_context.guardian_async_service.schedule_prewarm(self.tool_context)
        return self.llm

    async def prepare(
        self,
        *,
        previous_tool_schema_state: TurnToolSchemaDerivation,
        initial_turn_pending: bool,
        pending_turn_context: list[str],
    ) -> TurnIterationPreparation:
        run_context = self.tool_context.run_context
        if run_context.refresh_model_auth is not None:
            while (snapshot := run_context.model_execution) is not None:
                refreshed = await run_context.refresh_model_auth(snapshot, False, run_context.model_owner_task)
                if run_context.model_execution is snapshot:
                    run_context.model_execution = refreshed
                    break
                # A host selection published during the await owns the next
                # step. Refresh that choice under the same admission deadline.
        snapshot = run_context.model_execution
        if run_context.environment_manager is not None:
            from backend.agent.environment_runtime import refresh_run_environment_capabilities
            from backend.workspace.executor_local_policy import local_sandbox_policy

            self.turn_kernel.refresh_live_permission_context()
            prepared = await refresh_run_environment_capabilities(run_context,
                agent_session=self.agent_session, permission_context=self.tool_context.permission,
                config_stack=snapshot.config.config_layer_stack,
                feature_flags=snapshot.config.feature_flags, registry=self.base_tool_registry,
                artifact_store=self.tool_context.artifact_store, workspace_root=self.workspace_root,
                conversation_id=self.tool_context.conversation_id, turn_id=self.tool_context.turn_id)
            self.context.bind_skill_manager(prepared.skill_manager, self.workspace_root)
            self.skill_manager = prepared.skill_manager
            primary = run_context.turn_environments.primary()
            policy = local_sandbox_policy(primary.permission_scope.sandbox_context()) if primary is not None and not primary.is_remote else None
            self.tool_context.sandbox_policy = policy
            self.tool_context.allow_network = policy.allow_network if policy is not None else False
            self.tool_context.environment_id = primary.selection.environment_id if primary is not None else ""
            self.context.configure_project_instructions(snapshot.config.config_layer_stack.project_instruction_config())
            from backend.network.runtime import prepare_environment_network_step

            run_context.network_step = await prepare_environment_network_step(
                environment_manager=run_context.environment_manager, snapshot=run_context.turn_environments,
                config_stack=snapshot.config.config_layer_stack, exec_policy_sources=run_context.exec_policy_owner.sources)
            from backend.mcp.node_repl import register_node_companion

            await register_node_companion(run_context, self.base_tool_registry, self.agent_session, execution=snapshot)
            from backend.mcp.cua_repl import register_cua_companion

            await register_cua_companion(run_context, self.agent_session, execution=snapshot)
        model_events: list[AgentEvent] = []
        if snapshot is not None and self.context._llm is not snapshot.llm:
            target_budget = snapshot.config.token_budget
            if (target_budget.total < self.context._budget.total
                and self.context.token_usage > target_budget.total - target_budget.reserved_response_tokens):
                before = context_ledger_snapshot(self.context)
                summary = await self.context.compact(
                    restore_state=self.state, replacement_budget=target_budget,
                    replacement_llm=snapshot.llm,
                )
                model_events.append(build_context_compacted_event(summary, before, context_ledger_snapshot(self.context)))
        self.sync_active_session_model(snapshot)
        mcp_version, mcp_instructions = self.mcp_catalog(self.base_tool_registry)
        self.tool_registry = self.base_tool_registry.fork()
        self.tool_context.tool_registry = self.tool_registry
        self.tool_context.run_context.active_tool_registry = self.tool_registry
        self.state.loaded_deferred_tools.intersection_update(self.tool_registry.list_tools())
        self.turn_kernel.refresh_live_permission_context()
        base_policy = self.active_toolset_policy_factory(
            permission_context=self.tool_context.permission,
        )
        active_policy = effective_toolset_policy(
            realtime_service=active_realtime_service(self.tool_context),
            model_execution=snapshot,
            multi_agent_version=run_context.multi_agent_version,
            base_policy=base_policy,
            tool_registry=self.tool_registry,
            disabled_tools=(
                self.state.disabled_tools | {"read_terminal"}
                if self.tool_context.terminal_manager is None
                else self.state.disabled_tools
            ),
            requires_explicit_workspace=bool(
                self.tool_context.run_context
                and self.tool_context.run_context.requires_explicit_workspace
            ),
            workspace_root=self.workspace_root,
            permission_mode=str(self.tool_context.permission.mode or ""),
            is_subagent=run_context.is_subagent or run_context.session_source == "subagent",
            hosted_web_search=(
                callable(getattr(self.llm, "supports_hosted_web_search", None))
                and self.llm.supports_hosted_web_search()
            ),
            hosted_cached_search=(callable(getattr(self.llm, "hosted_web_search_supports_cached", None))
                and self.llm.hosted_web_search_supports_cached()),
            supports_search_tool=(snapshot.model_info.supports_search_tool if snapshot is not None and snapshot.model_info is not None
                else snapshot.config.llm.supports_search_tool if snapshot is not None
                else getattr(getattr(self.llm, "_settings", None), "supports_search_tool", False)),
            tool_mode=(
                "code_mode"
                if getattr(self.agent_session, "active_tool_names", None) is not None
                else requested_tool_mode(
                    default_code_mode_only=self.code_mode_only,
                    model_execution=run_context.model_execution,
                    llm=self.llm,
                )
            ),
        )
        loaded_deferred_tools = frozenset(
            str(name).strip()
            for name in getattr(self.state, "loaded_deferred_tools", set())
            if str(name).strip()
        )
        if active_policy is not None and loaded_deferred_tools:
            get_tool_spec = getattr(self.tool_registry, "get_tool_spec", None)
            if callable(get_tool_spec):
                loaded_deferred_tools = frozenset(
                    name
                    for name in loaded_deferred_tools
                    if active_policy.is_available(get_tool_spec(name))
                )
        if loaded_deferred_tools:
            base_policy = active_policy or ToolsetPolicy.default()
            active_policy = replace(
                base_policy,
                enabled_tools=frozenset(base_policy.enabled_tools) | frozenset(
                    self.tool_registry.get_tool_spec(name).policy_identity or name
                    for name in loaded_deferred_tools
                ),
            )
        from backend.tools.environment_policy import policy_for_environment_snapshot

        features = run_context.model_execution.config.feature_flags if run_context.model_execution is not None else None
        active_policy = policy_for_environment_snapshot(active_policy, run_context.turn_environments,
            stable_environment_tools=features.enabled("stable_environment_tools", default=False) if features is not None else False)
        active_policy = replace(active_policy, model_messages=(snapshot.model_info.model_messages
            if snapshot is not None and snapshot.model_info is not None else None))
        mcp_manager = run_context.mcp_manager
        if not active_policy.code_mode_only and (mcp_manager is None or not mcp_manager.has_enabled_servers()):
            active_policy = active_policy.with_disabled_tools({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})
        # The execution boundary reads the policy from metadata as well as
        # the schema builder.  Publish the *final* policy after deferred tools
        # have been activated; writing the pre-activation value here would let
        # a valid tool_search result render in the next schema while a direct
        # model tool call is still rejected by the runtime guard.
        self.tool_context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY] = active_policy
        run_context.toolset_policy = active_policy
        schema_key = (
            self.tool_registry.schema_source,
            self.tool_registry.version,
            mcp_version,
            active_policy.cache_key(),
            id(self.permission_checker),
        )
        if (schema_key != self._base_schema_key
                or self.tool_context.permission is not self._base_schema_permission):
            self._base_schemas = self.tool_registry.get_schemas(
                permission_checker=self.permission_checker,
                permission_context=self.tool_context.permission,
                toolset_policy=active_policy,
                mcp_registry_version=mcp_version,
            )
            self._base_schema_key = schema_key
            self._base_schema_permission = self.tool_context.permission
        base_schemas = self._base_schemas
        schema_state = derive_turn_tool_schema_state(
            base_tool_schemas=base_schemas,
            mcp_instructions=mcp_instructions,
            tool_registry=self.tool_registry,
            permission_checker=self.permission_checker,
            permission_context=self.tool_context.permission,
            toolset_policy=active_policy,
            mcp_registry_version=mcp_version,
            previous=previous_tool_schema_state,
        )
        active_capabilities = capabilities_for_adapter(self.llm)
        dedicated_image_model = is_gpt_image_model(active_capabilities.model) or (
            active_capabilities.image_generation is True
            and active_capabilities.tool_calling is False
        )
        if dedicated_image_model:
            # Dedicated Images API models are valid main-turn models, but they
            # never receive local function schemas or tool runtime guidance.
            # Keeping tool_calling=False accurately describes the provider;
            # the empty schema is what makes this image-only turn admissible.
            schema_state = replace(
                schema_state,
                tool_schemas=[],
                tool_names=[],
                runtime_guidance="",
                deferred_tools_prompt_block="",
            )
        self.populate_prompt_context(
            state=self.state,
            metadata=self.metadata,
            workspace_root=self.workspace_root,
            permission_context=self.tool_context.permission,
            run_context=self.tool_context.run_context,
        )

        tool_schemas = schema_state.tool_schemas
        if self.tool_context.run_context.extension_actions is not None:
            self.tool_context.run_context.extension_actions.tool_schemas = tool_schemas
        self.state.prompt_context["tool_names"] = schema_state.tool_names
        self.state.tool_runtime_guidance = schema_state.runtime_guidance
        self.state.prompt_context["deferred_tools_prompt_block"] = (
            schema_state.deferred_tools_prompt_block
        )
        events: list[AgentEvent] = model_events
        if self.context.token_budget_enabled and self.context.needs_compaction(self.state, tool_schemas=tool_schemas):
            # A new user steer is admitted after the previous window rolls
            # over. Its original text must belong to the new active window.
            from backend.agent.token_budget_runtime import run_context_window_compaction
            async for event in run_context_window_compaction(self.context, self.state):
                events.append(event)
            if self.state.stopped_reason:
                return TurnIterationPreparation(tool_schemas, schema_state, events)
        await self.context.prepare_history_notes_thread_context(
            state=self.state, session_id=self.tool_context.run_context.responses_session_id,
            agent_name=AgentPath.parse(self.turn_kernel.run_record.agent_path).logical_name,
        )
        boundary_input = await self.turn_kernel.take_boundary_input(
            initial_turn_pending=initial_turn_pending,
        )

        if boundary_input.should_start_turn:
            history_start = self.context.history_length
            admission_snapshot = self.context.export_snapshot()
            try:
                if self.skill_manager is not None:
                    primary = run_context.turn_environments.primary() if run_context.turn_environments is not None else None
                    async for skill_event in activate_turn_skills(
                        self.skill_manager,
                        boundary_input.content,
                        self.state,
                        self.tool_context.run_context.mcp_manager if self.tool_context.run_context else None,
                        self.tool_context.approval_handler,
                        self.tool_context.run_context.publish_nested_event if self.tool_context.run_context else None,
                        permission_context=self.tool_context.permission,
                        permission_profile=(self.tool_context.sandbox_policy.resolve(cwd=self.tool_context.workspace_root)
                            if self.tool_context.sandbox_policy is not None else None),
                        permission_scope=primary.permission_scope if primary is not None else None,
                        prompted_dependencies=self.context.prompted_mcp_dependencies,
                        feature_flags=self.tool_context.model_execution.config.feature_flags,
                    ):
                        events.append(skill_event)
                original_attachments = self.state.attachments
                if boundary_input.attachments is not None:
                    self.state.attachments = [
                        dict(item) for item in boundary_input.attachments
                    ]
                try:
                    goal_steering = bool(self.metadata.get("_goal_continuation")) and boundary_input.consumed_steer is None
                    if goal_steering:
                        admitted_message = await self.context.start_turn(boundary_input.content, self.state, input_role="developer")
                    else:
                        admitted_message = await self.context.start_turn(boundary_input.content, self.state)
                finally:
                    self.state.attachments = original_attachments
                source = str(self.metadata.get("query_source") or "user")
                if boundary_input.consumed_steer is not None:
                    # A newly admitted input owns its provenance even when it
                    # steers a turn originally started by a heartbeat/worker.
                    origin = boundary_input.consumed_steer.input_origin
                elif "input_origin" in self.metadata:
                    origin = self.metadata["input_origin"]
                else:
                    origin = "agent" if (
                        self.turn_kernel.run_record.parent_run_id
                        or source.startswith(("memory", "side_query", "extension", "guardian"))
                        or source == "auto_followup" or goal_steering
                    ) else "user"
                admitted_message.input_origin = origin
                accepted_id = (
                    boundary_input.consumed_steer.original_command.data.get("user_message_id")
                    if boundary_input.consumed_steer is not None
                    else self.metadata.get("user_message_id")
                )
                evidence = self.context.guardian_evidence
                accepted_input = (boundary_input.consumed_steer.original_command.data
                    if boundary_input.consumed_steer is not None else self.metadata)
                order = accepted_input.get("_guardian_acceptance_order")
                if order is None:
                    order = evidence.reserve_order()
                if not goal_steering:
                    self.context.record_admitted_user_input(admitted_message,
                        text=boundary_input.content, turn_id=str(self.metadata["turn_id"]),
                        message_id=accepted_id, acceptance_order=order)
                commit_turn_admission = self.metadata.get("commit_turn_admission")
                if callable(commit_turn_admission):
                    committed = commit_turn_admission(
                        boundary_input=boundary_input,
                        history_start=history_start,
                        history_end=self.context.history_length,
                    )
                    if hasattr(committed, "__await__"):
                        await committed
            except BaseException:
                self.context.load_snapshot(admission_snapshot)
                raise
            report_turn_admission = self.metadata.get("report_turn_admission")
            if callable(report_turn_admission):
                reported = report_turn_admission(boundary_input=boundary_input)
                if hasattr(reported, "__await__"):
                    await reported
            await self.turn_kernel.acknowledge_boundary_input(boundary_input)
            publish_turn_admission = self.metadata.get("publish_turn_admission")
            if callable(publish_turn_admission):
                published = publish_turn_admission(boundary_input=boundary_input)
                if hasattr(published, "__await__"):
                    await published
            for content in pending_turn_context:
                self.context.append_user_context(content)
            pending_turn_context.clear()

        # Claude collects completed AsyncHookRegistry responses for every
        # model query, not only when the next user turn starts.  Poll the
        # conversation-owned manager at this same iteration boundary so a
        # hook that finishes while tools are running can affect the next
        # provider call without leaking into another conversation.
        hook_manager = self.context.hook_manager
        take_async_context = getattr(hook_manager, "take_async_context", None)
        if callable(take_async_context):
            for content in take_async_context():
                self.context.append_user_context(content)

        capability = require_tool_calling(self.llm, tool_count=len(tool_schemas))
        if capability.ok:
            return TurnIterationPreparation(tool_schemas, schema_state, events)

        capabilities_payload = (
            capability.capabilities.to_dict()
            if capability.capabilities is not None
            else {}
        )
        events.extend(
            [
                AgentEvent.error(
                    message=(
                        "当前模型或 provider 不支持工具调用，因此不能安全执行这轮 agent 任务。"
                        "请切换到支持 tool/function calling 的模型或 provider 后重试。"
                    ),
                    recoverable=True,
                    error_type="provider_capability",
                    provider_error_type="unsupported_capability",
                ),
                AgentEvent.inspector_update(
                    "provider",
                    f"{self.run_record.run_id}:provider-capability:{self.state.iterations + 1}",
                    {
                        "kind": "provider_capability",
                        "capability": capability.capability,
                        "reason": capability.reason,
                        "capabilities": capabilities_payload,
                    },
                ),
            ]
        )
        return TurnIterationPreparation(tool_schemas, schema_state, events, terminal=True)
