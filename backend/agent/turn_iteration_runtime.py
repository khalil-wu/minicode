"""Prepare one model iteration before context budgeting and provider I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.skill_activation import activate_turn_skills
from backend.agent.tool_schema_derivation import (
    TurnToolSchemaDerivation,
    derive_turn_tool_schema_state,
    effective_toolset_policy,
)
from backend.llm.capabilities import (
    capabilities_for_adapter,
    is_gpt_image_model,
    require_tool_calling,
)
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy


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
        tool_registry: Any,
        permission_checker: Any,
        tool_context: Any,
        turn_kernel: Any,
        metadata: dict[str, Any],
        workspace_root: Any,
        mcp_instructions: str,
        mcp_registry_version: Any,
        active_toolset_policy_factory: Any,
        populate_prompt_context: Any,
        run_record: Any,
        skill_manager: Any | None = None,
        agent_session: Any | None = None,
    ) -> None:
        self.context = context
        self.state = state
        self.llm = llm
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.tool_context = tool_context
        self.turn_kernel = turn_kernel
        self.metadata = metadata
        self.workspace_root = workspace_root
        self.mcp_instructions = mcp_instructions
        self.mcp_registry_version = mcp_registry_version
        self.active_toolset_policy_factory = active_toolset_policy_factory
        self.populate_prompt_context = populate_prompt_context
        self.run_record = run_record
        self.skill_manager = skill_manager
        self.agent_session = agent_session

    def sync_active_session_model(self) -> Any:
        """Project Pi's mutable AgentSession model into this iteration."""

        owner = self.agent_session
        active_llm = getattr(owner, "llm", None) if owner is not None else None
        if active_llm is not None:
            self.llm = active_llm
            self.tool_context.llm = active_llm
            self.context.bind_llm(active_llm)
        active_budget = getattr(owner, "token_budget", None)
        if active_budget is not None:
            self.context.bind_budget(active_budget)
        active_registry = (
            getattr(owner, "tool_registry", None) if owner is not None else None
        )
        if active_registry is not None:
            self.tool_registry = active_registry
            self.tool_context.tool_registry = active_registry
        return self.llm

    async def prepare(
        self,
        *,
        previous_tool_schema_state: TurnToolSchemaDerivation,
        initial_turn_pending: bool,
        pending_turn_context: list[str],
    ) -> TurnIterationPreparation:
        self.sync_active_session_model()
        self.turn_kernel.refresh_live_permission_context()
        base_policy = self.active_toolset_policy_factory(
            permission_context=self.tool_context.permission,
        )
        active_policy = effective_toolset_policy(
            base_policy=base_policy,
            tool_registry=self.tool_registry,
            disabled_tools=self.state.disabled_tools,
            requires_explicit_workspace=bool(
                self.tool_context.run_context
                and self.tool_context.run_context.requires_explicit_workspace
            ),
            workspace_root=self.workspace_root,
            permission_mode=str(self.tool_context.permission.mode or ""),
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
                enabled_tools=frozenset(base_policy.enabled_tools) | loaded_deferred_tools,
            )
        # The execution boundary reads the policy from metadata as well as
        # the schema builder.  Publish the *final* policy after deferred tools
        # have been activated; writing the pre-activation value here would let
        # a valid tool_search result render in the next schema while a direct
        # model tool call is still rejected by the runtime guard.
        self.tool_context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY] = active_policy
        base_schemas = self.tool_registry.get_schemas(
            permission_checker=self.permission_checker,
            permission_context=self.tool_context.permission,
            toolset_policy=active_policy,
            mcp_registry_version=self.mcp_registry_version(),
        )
        schema_state = derive_turn_tool_schema_state(
            base_tool_schemas=base_schemas,
            mcp_instructions=self.mcp_instructions,
            tool_registry=self.tool_registry,
            permission_checker=self.permission_checker,
            permission_context=self.tool_context.permission,
            toolset_policy=active_policy,
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
        self.state.prompt_context["tool_names"] = schema_state.tool_names
        self.state.tool_runtime_guidance = schema_state.runtime_guidance
        self.state.prompt_context["deferred_tools_prompt_block"] = (
            schema_state.deferred_tools_prompt_block
        )
        events: list[AgentEvent] = []
        boundary_input = await self.turn_kernel.take_boundary_input(
            initial_turn_pending=initial_turn_pending,
        )

        if boundary_input.should_start_turn:
            history_start = self.context.history_length
            admission_snapshot = self.context.export_snapshot()
            try:
                if self.skill_manager is not None:
                    async for skill_event in activate_turn_skills(
                        self.skill_manager,
                        boundary_input.content,
                        self.state,
                    ):
                        events.append(skill_event)
                original_attachments = self.state.attachments
                if boundary_input.attachments is not None:
                    self.state.attachments = [
                        dict(item) for item in boundary_input.attachments
                    ]
                try:
                    await self.context.start_turn(boundary_input.content, self.state)
                finally:
                    self.state.attachments = original_attachments
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
            await self.turn_kernel.acknowledge_boundary_input(boundary_input)
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
