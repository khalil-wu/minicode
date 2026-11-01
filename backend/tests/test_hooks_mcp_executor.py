"""Configured MCP hooks use the captured MCP owner after its admitted startup."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.agent.run_context import RunContext
from backend.hooks.manager import HookEvent, HookManager
from backend.hooks.mcp_executor import HookMcpExecutor, expand_mcp_input
from backend.hooks.runners import HookExecutionError
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.manager import MCPServerConfig
from backend.permissions.context import PermissionContext, ToolExecutionContext


class _Client:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or MCPCallResult(content=[{"type": "text", "text": "{}"}])

    async def call_tool(self, tool, arguments, *, request_owner, request_meta):
        self.calls.append((tool, arguments, request_owner, request_meta))
        return self.result


class _Manager:
    """The production prepare/get_client contract: a dormant cached server is
    started by prepare_server_call, an unconnectable one raises ConnectionError."""

    def __init__(self, client, *, enabled=True, connected=True, tools=("inspect",), environment_id="local", dormant=False):
        self.client = client
        self.config = MCPServerConfig(name="docs", enabled=enabled, environment_id=environment_id)
        self.connected = connected
        self.dormant = dormant
        self.tools = tools
        self.prepared = []

    def get_server_config(self, name):
        assert name == "docs"
        return self.config

    def get_client(self, name):
        assert name == "docs"
        return self.client if self.connected and not self.dormant else None

    def get_all_tools(self):
        return {"docs": [MCPToolDef(name=name, description="fixture") for name in self.tools]}

    async def prepare_server_call(self, name):
        assert name == "docs"
        self.prepared.append(name)
        self.dormant = False
        if not self.connected:
            raise ConnectionError(f"MCP server '{name}' is not connected")


def _context(tmp_path, manager, **run_fields):
    run = RunContext(mcp_manager=manager, **run_fields)
    return ToolExecutionContext(
        permission=PermissionContext(), session_id="window", task_id="task",
        conversation_id="thread", turn_id="turn", workspace_root=tmp_path,
        metadata={"run_id": "run", "conversation_run_generation": 4},
        run_context=run,
    )


def test_mcp_input_expands_nested_values_without_losing_json_types():
    hook_input = {"session_id": "线程", "tool_input": {"count": 2, "items": [True, None]}, "empty": None}
    template = {
        "thread": "${session_id}", "args": "${tool_input}",
        "nested": ["${tool_input.count}", {"empty": "${empty}"}],
        "text": "items=${tool_input.items}; thread=${session_id}", "literal": False,
    }
    result = expand_mcp_input(template, hook_input)
    assert result == {
        "thread": "线程", "args": {"count": 2, "items": [True, None]},
        "nested": [2, {"empty": None}], "text": 'items=[true,null]; thread=线程', "literal": False,
    }
    result["args"]["items"].append("new")
    assert hook_input["tool_input"]["items"] == [True, None]


def test_mcp_input_missing_placeholder_is_a_real_hook_failure():
    with pytest.raises(HookExecutionError, match="tool_input.missing.*was not found"):
        expand_mcp_input({"value": "prefix ${tool_input.missing}"}, {"tool_input": {}})


@pytest.mark.asyncio
async def test_configured_mcp_hook_reaches_current_step_manager_with_owner_and_typed_input(tmp_path):
    old_client, current_client = _Client(), _Client()
    context = _context(tmp_path, _Manager(old_client), mcp_owner_session_id="owner-window")
    manager = HookManager.from_settings({"hooks": {"pre_tool_use": [{"matcher": "read_file", "hooks": [{
        "type": "mcp_tool", "server": "docs", "tool": "inspect", "async": True,
        "statusMessage": "Inspecting", "timeout": 0.5,
        "input": {"event": "${hook_event_name}", "arguments": "${tool_input}", "id": "${tool_use_id}"},
    }]}]}}, workspace_root=tmp_path)
    context.run_context.hook_manager = manager
    manager.bind_runtime(llm=None, tool_registry=None, tool_context=context)
    current_manager = _Manager(current_client)
    context.run_context.mcp_manager = current_manager

    result = await manager.run_pre_tool("read_file", {"path": "a.py", "limit": 3}, tool_call_id="call")

    assert old_client.calls == []
    assert len(current_client.calls) == 1
    tool, arguments, owner, metadata = current_client.calls[0]
    assert (tool, arguments) == ("inspect", {"event": "PreToolUse", "arguments": {"path": "a.py", "limit": 3}, "id": "call"})
    assert metadata == {"threadId": "thread"}
    assert owner["session_id"] == "owner-window"
    assert (owner["conversation_id"], owner["task_id"], owner["turn_id"], owner["run_id"]) == ("thread", "task", "turn", "run")
    assert owner["conversation_run_generation"] == 4
    assert owner["mcp_manager"] is current_manager and owner["hook_manager"] is manager
    assert owner["permission_context_provider"]() is context.permission
    assert 0 < owner["deadline_monotonic"] - time.monotonic() <= 0.5
    assert result.run_summaries[0]["status_message"] == "Inspecting"
    assert manager.pending_async_hooks == 0
    assert manager.hooks[HookEvent.PRE_TOOL_USE][0].run_async is False


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,connected,tools,message", [
    (False, True, ("inspect",), "not enabled"),
    (True, False, ("inspect",), "not connected"),
    (True, True, (), "tool is not enabled"),
])
async def test_mcp_hook_reports_disabled_unconnectable_and_unlisted_targets(tmp_path, enabled, connected, tools, message):
    client = _Client()
    manager = _Manager(client, enabled=enabled, connected=connected, tools=tools)
    with pytest.raises(HookExecutionError, match=message):
        await HookMcpExecutor(manager).execute(
            server="docs", tool="inspect", input={}, timeout=0.5,
            tool_context=_context(tmp_path, manager),
        )
    assert client.calls == []
    assert manager.prepared == ([] if not enabled else ["docs"])


@pytest.mark.asyncio
async def test_mcp_hook_waits_for_a_dormant_cached_server_like_ordinary_tools(tmp_path):
    # Source hook_mcp_executor calls the server with wait_for_server=true; a
    # cached lazily-started server inherited by a child is dormant until the
    # first call admits its startup.
    client = _Client()
    manager = _Manager(client, dormant=True)
    assert manager.get_client("docs") is None
    assert await HookMcpExecutor(manager).execute(
        server="docs", tool="inspect", input={}, timeout=0.5, tool_context=_context(tmp_path, manager),
    ) == "{}"
    assert manager.prepared == ["docs"]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_mcp_hook_preserves_remote_permission_scope_and_only_returns_text(tmp_path):
    scope = object()
    environments = []

    def permission_scope(environment_id):
        environments.append(environment_id)
        return scope

    client = _Client(MCPCallResult(content=[
        {"type": "text", "text": "first"}, {"type": "image", "data": "image"},
        {"type": "text", "text": "second"}, {"type": "text"},
    ]))
    manager = _Manager(client, environment_id="remote")
    context = _context(tmp_path, manager, environment_capabilities=SimpleNamespace(
        runtime=SimpleNamespace(permission_scope=permission_scope),
    ))
    assert await HookMcpExecutor(manager).execute(
        server="docs", tool="inspect", input={}, timeout=0.5, tool_context=context,
    ) == "first\nsecond"
    assert environments == ["remote"]
    owner = client.calls[0][2]
    assert owner["mcp_permission_scope"] is scope and owner["mcp_permission_profile"] is None


@pytest.mark.asyncio
async def test_mcp_hook_error_and_timeout_remain_failures(tmp_path):
    client = _Client(MCPCallResult(content=[{"type": "text", "text": "server rejected"}], is_error=True))
    manager = _Manager(client)
    context = _context(tmp_path, manager)
    with pytest.raises(HookExecutionError, match="MCP tool returned an error: server rejected"):
        await HookMcpExecutor(manager).execute(server="docs", tool="inspect", input={}, timeout=0.5, tool_context=context)

    cancelled = asyncio.Event()

    async def waiting_call(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.call_tool = waiting_call
    with pytest.raises(HookExecutionError, match="MCP hook timed out"):
        await HookMcpExecutor(manager).execute(server="docs", tool="inspect", input={}, timeout=0.01, tool_context=context)
    assert cancelled.is_set()
