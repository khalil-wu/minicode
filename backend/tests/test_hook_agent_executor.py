from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.hooks.runners import HookRuntimeBindings, _execute_agent
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext


class _TaskMustNotRun:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("agent hook must use execute_tool_batch")


class _Registry:
    def get_tool(self, name: str):
        return _TaskMustNotRun() if name == "task" else None


@pytest.mark.asyncio
async def test_agent_hook_routes_task_through_canonical_executor(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_execute_tool_batch(tool_calls, **kwargs):
        calls.append({"tool_calls": tool_calls, **kwargs})
        yield AgentEvent.tool_result(
            id=tool_calls[0].id,
            summary='{"ok": true}',
            result_kind="subagent",
        )

    monkeypatch.setattr(
        "backend.agent.tool_batch_execution.execute_tool_batch", fake_execute_tool_batch
    )

    context_builder = ContextBuilder(
        token_budget=TokenBudget(),
        agent_settings=AgentSettings(),
    )
    state = AgentState(user_message="parent", max_iterations=7)
    tool_context = ToolExecutionContext(
        permission=PermissionContext(),
        session_id="session",
        task_id="turn",
        permission_checker=PermissionChecker(PermissionSettings()),
        metadata={"_context_builder": context_builder, "_agent_state": state},
        run_context=RunContext(),
    )
    runtime = HookRuntimeBindings(
        workspace_root=None,
        tool_registry=_Registry(),
        tool_context=tool_context,
    )
    entry = SimpleNamespace(prompt="Check the workspace", model="", async_timeout=1)

    stdout, stderr, code = await _execute_agent(
        entry,
        event=SimpleNamespace(value="pre_tool_use"),
        json_input="{}",
        runtime=runtime,
        substitute_arguments=lambda value, _json: value,
        parse_verdict=lambda value: (True, "") if '"ok": true' in value else None,
    )

    assert (stdout, stderr, code) == ('{"ok": true}', "", 0)
    assert len(calls) == 1
    tool_call = calls[0]["tool_calls"][0]
    assert isinstance(tool_call, ToolCallEvent)
    assert tool_call.name == "task"
    assert tool_call.arguments["agent_type"] == "explore"
    assert tool_call.arguments["read_only"] is True
    assert calls[0]["approval_handler"] is None
    assert calls[0]["skill_manager"] is None
