"""Real admission and execution producers for direct command process tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.tool_execution import (
    _authorize_final_tool_request,
    _bind_final_tool_request,
    run_tool_with_timeout,
)
from backend.config import AppConfig, LLMSettings, permission_settings_from_config
from backend.config_layers import ConfigLayerStack
from backend.llm.base import ToolCallEvent
from backend.llm.provider_contracts import ModelDefinition
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.permissions.exec_policy import ExecPolicyOwner
from backend.sandbox import SandboxPolicy
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry


def configure_command_context(
    tool: RunCommandTool, context: ToolExecutionContext, *, sandbox_policy: SandboxPolicy
) -> ToolExecutionContext:
    workspace = Path(context.workspace_root)
    stack = ConfigLayerStack(())
    config = AppConfig(llm=LLMSettings(api_key="", provider="custom", model="command-process-fixture"),
        permissions=permission_settings_from_config({}), config_layer_stack=stack)
    model = ModelDefinition(provider="custom", id=config.llm.model, name="Command process fixture",
        api="openai-completions", base_url="http://127.0.0.1/unused")
    snapshot = ModelExecutionSnapshot(config=config, llm=SimpleNamespace(), provider=model.provider,
        model=model.id, model_info=model)
    registry = ToolRegistry()
    registry.register(tool)
    context.model_execution = snapshot
    context.run_context = RunContext(model_execution=snapshot, active_tool_registry=registry,
        exec_policy_owner=ExecPolicyOwner(home=workspace / ".command-policy", stack=stack))
    context.permission_checker = PermissionChecker(config.permissions, workspace)
    context.tool_registry = registry
    context.sandbox_policy = sandbox_policy
    return context


async def execute_command(tool: RunCommandTool, arguments: dict, context: ToolExecutionContext):
    call = ToolCallEvent(id=f"command-{uuid4().hex}", name=tool.name, arguments=arguments)
    return await execute_command_call(call, context)


async def authorize_command_call(call: ToolCallEvent, context: ToolExecutionContext):
    return (await _authorize_final_tool_request(call, tool_registry=context.tool_registry,
        permission_checker=context.permission_checker, permission_context=context.permission, tool_ctx=context))


async def execute_command_call(call: ToolCallEvent, context: ToolExecutionContext):
    authorization = (await authorize_command_call(call, context))
    assert not authorization.error, authorization.error
    assert authorization.permission_decision.decision == "allow", authorization.permission_decision
    assert authorization.request.exec_policy.kind == "skip", authorization.request.exec_policy.reason
    _bind_final_tool_request(call, authorization.request)
    return await run_tool_with_timeout(call, context.tool_registry, context)
