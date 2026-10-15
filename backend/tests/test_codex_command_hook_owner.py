from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.run_context import RunContext
from backend.agent.tool_execution import _apply_pre_tool_hook, run_tool
from backend.hooks.manager import HookResult
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class CommandResultTool(BaseTool):
    name = "run_command"
    description = "Owned command"
    def __init__(self, result): self.result = result
    def get_schema(self): return ToolSchema(self.name, self.description, {"type": "object"})
    async def execute(self, args, context=None): return self.result


@pytest.mark.asyncio
async def test_bash_hook_rewrite_changes_only_command_before_authorization(tmp_path):
    hook = SimpleNamespace(run_pre_tool=AsyncMock(return_value=HookResult(updated_input={"command": "echo changed"})))
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path, run_context=RunContext(hook_manager=hook))
    call = ToolCallEvent(id="exec", name="run_command", arguments={"command": "echo original", "shell": "pwsh", "login": False, "tty": True, "yield_time_ms": 1000})
    assert await _apply_pre_tool_hook(call, context) is None
    hook.run_pre_tool.assert_awaited_once_with("Bash", {"command": "echo original"}, tool_call_id="exec", session_id="", permission_mode="bypass")
    assert call.arguments == {"command": "echo changed", "shell": "pwsh", "login": False, "tty": True, "yield_time_ms": 1000}


@pytest.mark.asyncio
async def test_running_result_waits_for_original_command_completion_hook(tmp_path):
    hook = SimpleNamespace(run_post_tool=AsyncMock(return_value=HookResult()), run_post_tool_failure=AsyncMock(return_value=HookResult()))
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path, run_context=RunContext(hook_manager=hook))
    registry = ToolRegistry()
    registry.register(CommandResultTool(ToolResult("still running", runtime_metadata={"terminal_hook": {"ready": False}})))
    await run_tool(ToolCallEvent(id="exec", name="run_command", arguments={"command": "echo original"}), registry, context)
    hook.run_post_tool.assert_not_awaited()
    hook.run_post_tool_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_poll_pairs_bash_post_with_original_id_input_and_output_even_on_exit_error(tmp_path):
    hook = SimpleNamespace(run_post_tool=AsyncMock(return_value=HookResult()), run_post_tool_failure=AsyncMock(return_value=HookResult()))
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path, run_context=RunContext(hook_manager=hook))
    terminal = {"ready": True, "command_id": "process", "tool_use_id": "original-exec", "tool_name": "Bash",
                "tool_input": {"command": "echo original"}, "output": "literal output\n"}
    tool = CommandResultTool(ToolResult("wrapped monitor result", is_error=True, runtime_metadata={"terminal_hook": terminal}))
    tool.name = "monitor"
    registry = ToolRegistry(); registry.register(tool)
    await run_tool(ToolCallEvent(id="later-poll", name="monitor", arguments={"action": "status", "command_id": "process"}), registry, context)
    hook.run_post_tool.assert_awaited_once_with("Bash", {"command": "echo original"}, "literal output\n", tool_call_id="original-exec", session_id="", permission_mode="bypass")
    hook.run_post_tool_failure.assert_not_awaited()
