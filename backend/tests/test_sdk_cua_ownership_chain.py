"""SDK entry points and original CUA process ownership, without desktop actions.

The fixture replaces only the external installed CUA package boundary. SDK,
QueryEngine, capability preparation, MCP wire, captured executor and native
pipe host remain real. These cases do not certify the installed CUA package.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil

import pytest

from backend.agent.run_context import RunContext
from backend.environments import EnvironmentManager
from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent
from backend.mcp.cua_repl import ComputerUseHost
from backend.mcp.manager import MCPServerManager
from backend.permissions.context import PermissionContext
from backend.sdk import create_session, create_tool_registry, query, tool
from backend.tests.test_cua_repl_companion import selected_runtime
from backend.tests.test_sdk_session_ownership_chain import sdk_owner, ToolModel
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.workspace.executor_local import LocalExecutionEnvironmentBackend


class CuaModel(ToolModel):
    def __init__(self, code, run):
        super().__init__("mcp__cua_repl__js", {"code": code})
        self.run = run
        self.invoked = False
        self.clients = []

    async def stream_chat(self, messages, tools=None, metadata=None):
        if not self.invoked:
            self.invoked = True
            self.clients.append(self.run.mcp_manager.get_client("cua_repl"))
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                ToolCallEvent(id="sdk-cua-" + str(len(messages)), name=self.name, arguments=self.arguments)])
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Fixture observation complete.", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE)


@pytest.fixture
def cua_sdk_boundary(sdk_owner, monkeypatch):
    if os.name != "nt":
        pytest.skip("CUA receiving environment is local Windows")
    node = shutil.which("node")
    if node is None:
        pytest.skip("Actual Node is required for the controlled CUA process boundary")
    workspace, runtime, artifacts, config = sdk_owner
    selected = replace(selected_runtime(workspace.parent / "source-package-boundary"), required=True)
    executable = Path(selected.command)
    shutil.copy2(node, executable)
    Path(selected.args[0]).write_bytes((Path(__file__).parent / "fixtures/cua_sdk_companion.mjs").read_bytes())
    modules = executable.parent / "node_modules"
    sky = modules / "@oai/sky"
    (sky / "package.json").write_text('{"name":"@oai/sky","type":"module"}', encoding="utf-8")
    transport = sky / "dist/project/cua/sky_js/src/targets/windows/internal/helper_transport.js"
    transport.write_bytes((Path(__file__).parent / "fixtures/cua_source_transport.mjs").read_bytes())
    (sky / "bin/windows/codex-computer-use.exe").write_text("", encoding="utf-8")
    events = workspace.parent / "cua-wire-events.jsonl"
    selected = replace(selected, env={**selected.env, "MINICODE_CUA_TEST_EVENTS": str(events)})
    monkeypatch.setenv("MINICODE_EDITOR_NODE", node)
    monkeypatch.setattr("backend.mcp.cua_repl.agent_runtime_root", lambda: workspace.parent / "owned-cua-home")
    controls = []
    original_request = ComputerUseHost.request

    async def capture_original_control(host, method, params=None):
        result = await original_request(host, method, params)
        if method == "closeActiveTurn":
            controls.append((host, method, params, result))
        return result

    monkeypatch.setattr(ComputerUseHost, "request", capture_original_control)
    return selected, events, controls


async def collect(stream):
    async with aclosing(stream):
        return [event async for event in stream]


async def approve(_call_id):
    return {"action": "approve"}


def assert_tool_value(events, expected):
    assert not [event.data for event in events if event.type == "error"], events
    result, = [event for event in events if event.type == "tool_result"]
    assert result.data["summary"].splitlines()[-1] == expected, result.data
    assert any(event.type == "done" and event.data["status"] == "completed" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["query", "session", "disabled-cua-session"])
async def test_real_public_sdk_without_cua_reaches_tool_and_final_answer(sdk_owner, entry):
    workspace, runtime, artifacts, config = sdk_owner

    @tool
    def observe_sdk():
        return "ordinary SDK executed"

    host = None
    if entry == "disabled-cua-session":
        selected = replace(selected_runtime(workspace.parent / "disabled-cua"), enabled=False)
        host = MCPServerManager(config_path=workspace.parent / "host-mcp.json", workspace_root=workspace,
            runtime_configs=(selected,))
    run = RunContext(agent_runtime=runtime, mcp_manager=host)
    kwargs = dict(workspace_root=workspace, config=config, llm=ToolModel("observe_sdk"),
        artifact_store=artifacts, tool_registry=create_tool_registry(observe_sdk), run_context=run)
    if entry == "query":
        assert_tool_value(await collect(query("Run the ordinary SDK tool", **kwargs)), "ordinary SDK executed")
    else:
        async with create_session(**kwargs) as session:
            assert session.cua_repl_companions is None
            assert_tool_value(await collect(session.query("Run the ordinary SDK tool")), "ordinary SDK executed")
            assert run.node_repl_owner is session and session.cua_repl_companions is None
        assert not session.lifecycle_cleanup_tasks and not session._lifecycle_cleanup_waiters


@pytest.mark.asyncio
async def test_stateful_sdk_cua_kernel_fork_and_close_keep_original_owners(sdk_owner, cua_sdk_boundary, monkeypatch):
    workspace, runtime, artifacts, config = sdk_owner
    selected, events_path, controls = cua_sdk_boundary
    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(workspace.parent / "environments", local_backend=backend)
    host = MCPServerManager(config_path=workspace.parent / "host-mcp.json", workspace_root=workspace,
        runtime_configs=(selected,))
    run = RunContext(agent_runtime=runtime, mcp_manager=host)
    first_model = CuaModel("const sdkCuaSaved=41;nodeRepl.write(sdkCuaSaved)", run)
    session = create_session(session_id="sdk-cua-parent", workspace_root=workspace, config=config,
        llm=first_model, artifact_store=artifacts, tool_registry=create_tool_registry(), run_context=run,
        environment_manager=environments, permission_context=PermissionContext(mode="bypass"), approval_handler=approve,
        metadata={"conversation_id": "sdk-cua-parent"})
    child = None
    try:
        assert_tool_value(await collect(session.query("Remember the fixture value")), "41")
        companion = session.cua_repl_companions
        client = first_model.clients[0]
        assert run.node_repl_owner is session and companion is not session.node_repl_companions
        assert companion.instances[companion.active_key].client is client
        original = client.process_handle
        original_host = client.host
        assert original.accepted and not original.closed and not client.thread_lease.released
        assert run.cua_repl_turn is None
        second_model = CuaModel("nodeRepl.write(sdkCuaSaved+1)", run)
        assert_tool_value(await collect(session.query("Read the original value", llm=second_model)), "42")
        assert second_model.clients == [client] and session.cua_repl_companions is companion
        assert client.process_handle is original
        wire = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        calls = [row for row in wire if row["event"] == "call"]
        assert len(calls) == 2 and {row["pid"] for row in calls} == {original.process.pid}
        assert all(row["meta"]["x-codex-turn-metadata"]["session_id"] == "sdk-cua-parent" for row in calls)
        assert len({row["meta"]["x-codex-turn-metadata"]["turn_id"] for row in calls}) == 2
        actual_controls = [(method, params) for owner, method, params, _ in controls if owner is original_host]
        assert actual_controls == [("closeActiveTurn", {"sessionId": row["meta"]["x-codex-turn-metadata"]["session_id"],
            "turnId": row["meta"]["x-codex-turn-metadata"]["turn_id"]}) for row in calls]
        child = session.fork(session_id="sdk-cua-child", metadata={"conversation_id": "sdk-cua-child"})
        assert child.cua_repl_companions is None and child.node_repl_companions is None
        child_run = child._query_kwargs["run_context"]
        child_model = CuaModel("nodeRepl.write(typeof sdkCuaSaved)", child_run)
        assert_tool_value(await collect(child.query("Observe an independent child kernel", llm=child_model)), "undefined")
        child_client = child_model.clients[0]
        assert child.cua_repl_companions is not companion and child_client is not client
        assert child_client.process_handle.process.pid != original.process.pid
        assert child_client.host is original_host and original_host.references == 2
        await child.aclose()
        assert child_client.process_handle.closed and child_client.thread_lease.released
        assert client.connected and not original.closed and original_host.references == 1
        thread = session._thread_environments
        original_close = thread.aclose
        order = []

        async def close_environment_after_original_companions():
            assert original.closed and original.released and client.thread_lease.released
            assert original_host.process.returncode == 0
            assert not backend.process.processes
            order.append("original-companions-before-environments")
            await original_close()

        monkeypatch.setattr(thread, "aclose", close_environment_after_original_companions)
        await session.aclose()
        assert order == ["original-companions-before-environments"]
        assert not companion.instances and not session.lifecycle_cleanup_tasks and not session._lifecycle_cleanup_waiters
    finally:
        if child is not None:
            await child.aclose()
        await session.aclose()
        await environments.close()


@pytest.mark.asyncio
async def test_standalone_public_sdk_query_closes_its_cua_process_and_original_host(sdk_owner, cua_sdk_boundary):
    workspace, runtime, artifacts, config = sdk_owner
    selected, _, controls = cua_sdk_boundary
    host = MCPServerManager(config_path=workspace.parent / "host-mcp.json", workspace_root=workspace,
        runtime_configs=(selected,))
    run = RunContext(agent_runtime=runtime, mcp_manager=host)
    model = CuaModel("nodeRepl.write(42)", run)
    events = await collect(query("Observe the standalone fixture", session_id="sdk-cua-standalone",
        workspace_root=workspace, config=config, llm=model, artifact_store=artifacts,
        tool_registry=create_tool_registry(), run_context=run,
        permission_context=PermissionContext(mode="bypass"), approval_handler=approve,
        metadata={"conversation_id": "sdk-cua-standalone"}))
    assert_tool_value(events, "42")
    client, = model.clients
    assert client.process_handle.closed and client.process_handle.released and client.thread_lease.released
    original_host, method, params, _ = controls[0]
    assert method == "closeActiveTurn" and params["sessionId"] == "sdk-cua-standalone"
    assert original_host.process.returncode == 0 and client.host is None
    assert not run.lifecycle_cleanup_tasks


@pytest.mark.asyncio
async def test_conversation_thread_owner_keeps_cua_after_query_and_closes_original_process(sdk_owner, cua_sdk_boundary):
    from backend.ws.execution_context import ConversationThreadRuntime

    workspace, runtime, artifacts, config = sdk_owner
    selected, _, controls = cua_sdk_boundary
    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(workspace.parent / "environments", local_backend=backend)
    thread = ConversationThreadRuntime("sdk-test-store", "sdk-cua-thread")
    host = MCPServerManager(config_path=workspace.parent / "host-mcp.json", workspace_root=workspace,
        runtime_configs=(selected,))
    run = RunContext(agent_runtime=runtime, mcp_manager=host, environment_thread_runtime=thread)
    model = CuaModel("nodeRepl.write(42)", run)
    task = asyncio.create_task(collect(query("Observe the actual thread owner", session_id="sdk-thread-host",
        workspace_root=workspace, config=config, llm=model, artifact_store=artifacts,
        tool_registry=create_tool_registry(), run_context=run, environment_manager=environments,
        permission_context=PermissionContext(mode="bypass"), approval_handler=approve,
        metadata={"conversation_id": thread.conversation_id})))
    try:
        assert_tool_value(await task, "42")
        companion = thread.cua_repl_companions
        client, = model.clients
        original, original_host = client.process_handle, client.host
        assert companion.instances[companion.active_key].client is client
        assert client.connected and not original.closed and not thread.pending_tasks()
        assert len(controls) == 1 and controls[0][2]["sessionId"] == thread.conversation_id
        await thread.close_environments()
        assert original.closed and original.released and client.thread_lease.released
        assert original_host.process.returncode == 0 and not backend.process.processes
        assert thread.thread_environments is None and not companion.instances
    finally:
        await asyncio.gather(task, return_exceptions=True)
        await thread.close_environments()
        await environments.close()


@pytest.mark.asyncio
async def test_sdk_cua_close_waits_for_actual_late_consumer_and_turn_close_before_environment(sdk_owner, cua_sdk_boundary, monkeypatch):
    workspace, runtime, artifacts, config = sdk_owner
    selected, _, controls = cua_sdk_boundary
    monkeypatch.setattr("backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.001)
    release, finished = asyncio.Event(), asyncio.Event()

    class ResistantTool(BaseTool):
        name = "held_cua_consumer"
        permission = PermissionLevel.AUTO
        read_only = True
        requires_execution_environment = True
        timeout_seconds = 0.01
        context = None

        def get_schema(self):
            return ToolSchema(name=self.name, description="Hold the real captured capability owner",
                parameters={"type": "object", "properties": {}})

        async def execute(self, _args, context=None):
            self.context = context
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                return ToolResult(content="original CUA consumer released")
            finally:
                finished.set()

    held = ResistantTool()
    host = MCPServerManager(config_path=workspace.parent / "host-mcp.json", workspace_root=workspace,
        runtime_configs=(selected,))
    run = RunContext(agent_runtime=runtime, mcp_manager=host)
    session = create_session(session_id="sdk-cua-late", workspace_root=workspace, config=config,
        llm=ToolModel(held.name), artifact_store=artifacts, tool_registry=create_tool_registry(held),
        run_context=run, permission_context=PermissionContext(mode="bypass"), approval_handler=approve,
        metadata={"conversation_id": "sdk-cua-late"})
    closing = None
    try:
        events = await collect(session.query("Hold the original CUA capability"))
        assert any(event.type == "tool_result" and "timed out" in event.data["summary"] for event in events)
        companion = session.cua_repl_companions
        client = companion.instances[companion.active_key].client
        original = client.process_handle
        original_host = client.host
        assert held.context is not None and not finished.is_set()
        assert not controls and not original.closed and not client.thread_lease.released
        assert any(task.get_name() == "cua-turn-ended" and not task.done() for task in session.lifecycle_cleanup_tasks)
        with pytest.raises(RuntimeError, match="pending lifecycle cleanup"):
            session.fork()
        thread = session._thread_environments
        original_close = thread.aclose

        async def close_original_environment():
            assert finished.is_set() and original.closed and client.thread_lease.released
            assert original_host.process.returncode == 0
            assert len(controls) == 1 and controls[0][1:] == ("closeActiveTurn", {
                "sessionId": held.context.conversation_id, "turnId": held.context.turn_id}, False)
            await original_close()

        monkeypatch.setattr(thread, "aclose", close_original_environment)
        closing = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        assert not closing.done() and not controls and not original.closed
        release.set()
        await asyncio.wait_for(closing, 10)
        assert not session.lifecycle_cleanup_tasks and not session._lifecycle_cleanup_waiters
        assert not companion.instances and original.closed and original.released
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, 10)
        await session.aclose()


@pytest.mark.asyncio
async def test_closing_active_sdk_cua_query_cancels_provider_before_original_companions(sdk_owner, cua_sdk_boundary):
    workspace, runtime, artifacts, config = sdk_owner
    selected, _, controls = cua_sdk_boundary
    entered, release = asyncio.Event(), asyncio.Event()
    host = MCPServerManager(config_path=workspace.parent / "host-mcp.json", workspace_root=workspace,
        runtime_configs=(selected,))
    run = RunContext(agent_runtime=runtime, mcp_manager=host)
    session = create_session(session_id="sdk-cua-active", workspace_root=workspace, config=config,
        llm=ToolModel("never_execute", entered=entered, release=release), artifact_store=artifacts,
        tool_registry=create_tool_registry(), run_context=run,
        permission_context=PermissionContext(mode="bypass"), approval_handler=approve,
        metadata={"conversation_id": "sdk-cua-active"})
    running = asyncio.create_task(collect(session.query("Wait in the actual provider")))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        companion = session.cua_repl_companions
        client = companion.instances[companion.active_key].client
        original, original_host = client.process_handle, client.host
        assert not controls and not original.closed
        await asyncio.wait_for(session.aclose(), 10)
        await asyncio.gather(running, return_exceptions=True)
        assert running.done() and original.closed and original.released and client.thread_lease.released
        assert original_host.process.returncode == 0 and len(controls) == 1
        assert not session.lifecycle_cleanup_tasks and not session._lifecycle_cleanup_waiters
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await session.aclose()
