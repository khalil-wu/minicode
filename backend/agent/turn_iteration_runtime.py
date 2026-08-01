"""Prepare one model iteration before context budgeting and provider I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.tool_schema_derivation import (
    TurnToolSchemaDerivation,
    derive_turn_tool_schema_state,
)
from backend.llm.capabilities import require_tool_calling


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
        tool_schema_budget: Any,
        mcp_registry_version: Any,
        active_toolset_policy_factory: Any,
        populate_prompt_context: Any,
        run_record: Any,
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
        self.tool_schema_budget = tool_schema_budget
        self.mcp_registry_version = mcp_registry_version
        self.active_toolset_policy_factory = active_toolset_policy_factory
        self.populate_prompt_context = populate_prompt_context
        self.run_record = run_record

    async def prepare(
        self,
        *,
        previous_tool_schema_state: TurnToolSchemaDerivation,
        initial_turn_pending: bool,
        pending_turn_context: list[str],
    ) -> TurnIterationPreparation:
        self.turn_kernel.refresh_live_permission_context()
        active_policy = self.active_toolset_policy_factory(
            permission_context=self.tool_context.permission,
        )
        base_schemas = self.tool_registry.get_schemas(
            budget=self.tool_schema_budget,
            permission_checker=self.permission_checker,
            permission_context=self.tool_context.permission,
            toolset_policy=active_policy,
            mcp_registry_version=self.mcp_registry_version(),
        )
        schema_state = derive_turn_tool_schema_state(
            base_tool_schemas=base_schemas,
            disabled_tools=self.state.disabled_tools,
            mcp_instructions=self.mcp_instructions,
            tool_registry=self.tool_registry,
            permission_checker=self.permission_checker,
            permission_context=self.tool_context.permission,
            toolset_policy=active_policy,
            previous=previous_tool_schema_state,
        )
        self.populate_prompt_context(
            state=self.state,
            metadata=self.metadata,
            workspace_root=self.workspace_root,
            permission_context=self.tool_context.permission,
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
            original_attachments = self.state.attachments
            if boundary_input.attachments is not None:
                self.state.attachments = [dict(item) for item in boundary_input.attachments]
            try:
                await self.context.start_turn(boundary_input.content, self.state)
            finally:
                self.state.attachments = original_attachments
            await self.turn_kernel.acknowledge_boundary_input(boundary_input)
            for content in pending_turn_context:
                self.context.append_user_context(content)
            pending_turn_context.clear()

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
