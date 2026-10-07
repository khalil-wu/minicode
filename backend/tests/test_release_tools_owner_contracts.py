from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from types import SimpleNamespace

import anyio
import pytest
from mcp import types
from mcp.shared.message import ServerMessageMetadata, SessionMessage

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_execution import run_tool_with_timeout, store_result_events
from backend.agent.tool_schema_derivation import derive_turn_tool_schema_state
from backend.artifact.store import ArtifactStore
from backend.bootstrap.app import AppBootstrap
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.extensions.types import ToolResultPatch
from backend.hooks.manager import HookEvent, HookManager, _HookEntry
from backend.hooks.runners import HookExecutionResult
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef, _LifecycleClientSession
from backend.mcp.registry import MCPToolProxy
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tasks.manager import TaskManager
from backend.terminal.manager import BackgroundCommandManager
from backend.terminal.task_persistence import process_identity_matches
from backend.tools.agent_tools import TaskTool
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import ToolSearchTool
from backend.tools.toolsets import ToolsetPolicy
from backend.ws.agent_runner import _clear_session_llm_cache, _lease_session_llm_for_task
from backend.ws.turn_wait_state import TurnWaitState


class _DelayedWrite(BaseTool):
    name = "release_delayed_write"
    permission = PermissionLevel.AUTO
    read_only = False
    mutates_workspace = True
    timeout_seconds = .03

    def __init__(self, marker):
        self.marker = marker
        self.cancelled = asyncio.Event()

    def get_schema(self):
        return ToolSchema(self.name, "Delayed mutation", {"type": "object"})

    async def execute(self, args, context=None):
        try:
            await asyncio.sleep(.15)
            self.marker.write_text("late write", encoding="utf-8")
            return ToolResult("written", status="success")
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_tool_timeout_cancels_real_write_before_cleanup_is_reported(tmp_path, managed):
    marker = tmp_path / "late.txt"
    tool = _DelayedWrite(marker)
    registry = ToolRegistry()
    registry.register(tool)
    manager = TaskManager() if managed else None
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path, conversation_id="release-timeout", task_manager=manager,
        cancel_event=asyncio.Event())
    try:
        result = await run_tool_with_timeout(ToolCallEvent("write", tool.name, {}), registry, context)
        assert result.status == "timeout"
        assert result.cleanup_receipt["completed"] and result.cleanup_receipt["pending"] == 0
        assert tool.cancelled.is_set()
        await asyncio.sleep(.17)
        assert not marker.exists()
        if manager:
            assert all(item.status == "cancelled" for item in manager.list())
    finally:
        if manager:
            await manager.cancel_all_and_wait()


@pytest.mark.asyncio
async def test_managed_timeout_retains_cancellation_resistant_source_until_real_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr("backend.tools.registry.CANCELLATION_DRAIN_TIMEOUT_SECONDS", .01)
    cancelled, release = asyncio.Event(), asyncio.Event()
    class Tool(_DelayedWrite):
        async def execute(self, args, context=None):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                self.marker.write_text("actual late mutation", encoding="utf-8")
                return ToolResult("late result", status="success")
    manager, registry = TaskManager(), ToolRegistry()
    tool = Tool(tmp_path / "late.txt")
    registry.register(tool)
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path, task_manager=manager, cancel_event=asyncio.Event())
    try:
        result = await run_tool_with_timeout(ToolCallEvent("resistant", tool.name, {}), registry, context)
        assert cancelled.is_set()
        assert result.status == "timeout" and result.cleanup_receipt["pending"] > 0
        assert not result.cleanup_receipt["completed"]
        assert context.pending_cleanup_tasks and not tool.marker.exists()
        release.set()
        await asyncio.wait(context.pending_cleanup_tasks)
        await asyncio.sleep(0)
        assert tool.marker.read_text(encoding="utf-8") == "actual late mutation"
        assert result.status == "timeout"
    finally:
        release.set()
        await manager.cancel_all_and_wait()


@pytest.mark.asyncio
async def test_distinct_parallel_mcp_scopes_are_never_merged(monkeypatch):
    calls = []
    async def elicit(payload):
        calls.append(payload)
        return {"action": "accept", "content": {}}
    client = MCPClient("parallel", elicitation_handler=elicit)
    cancel_a, cancel_b = asyncio.Event(), asyncio.Event()
    owner = {"session_id": "session", "conversation_id": "conversation", "task_id": "task", "run_id": "run"}
    client._active_tool_request_owners = {1: {**owner, "cancel_event": cancel_a}, 2: {**owner, "cancel_event": cancel_b}}
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    response = await client._sdk_elicitation_callback(SimpleNamespace(request_id=9),
        types.ElicitRequestFormParams(message="A asks", requestedSchema={"type": "object"}))
    cancel_b.set()
    assert isinstance(response, types.ErrorData) and response.code == -32002
    assert calls == [] and not cancel_a.is_set()
    client._active_tool_request_owners[2] = dict(client._active_tool_request_owners[1])
    assert client._active_callback_owner()["cancel_event"] is cancel_a
    client._active_tool_request_owners[3] = {}
    assert client._active_callback_owner() is None


@pytest.mark.asyncio
async def test_sdk_related_request_metadata_selects_original_parallel_owner(monkeypatch):
    server_write, client_read = anyio.create_memory_object_stream(10)
    client_write, server_read = anyio.create_memory_object_stream(10)
    cancel_a, cancel_b = asyncio.Event(), asyncio.Event()
    seen, started, release = [], asyncio.Event(), asyncio.Event()
    async def elicit(payload):
        seen.append(payload["_minicode_owner"])
        started.set()
        await release.wait()
        return {"action": "accept", "content": {"selected": True}}
    client = MCPClient("parallel", elicitation_handler=elicit)
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    async with _LifecycleClientSession(client_read, client_write, transport_closed=asyncio.Event(),
        elicitation_callback=client._sdk_elicitation_callback) as sdk:
        sdk._tool_output_schemas.update(cell_a=None, cell_b=None)
        client._session, client._connected = sdk, True
        owner = {"session_id": "session", "conversation_id": "conversation", "task_id": "task", "run_id": "run"}
        a = asyncio.create_task(client.call_tool("cell_a", request_owner={**owner, "cancel_event": cancel_a}))
        request_a = await server_read.receive()
        b = asyncio.create_task(client.call_tool("cell_b", request_owner={**owner, "cancel_event": cancel_b}))
        await server_read.receive()
        try:
            await server_write.send(SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
                jsonrpc="2.0", id="elicitation-A", method="elicitation/create",
                params={"message": "A asks", "requestedSchema": {"type": "object"}})),
                metadata=ServerMessageMetadata(related_request_id=request_a.message.root.id)))
            await asyncio.wait_for(started.wait(), 1)
            cancel_b.set()
            await asyncio.gather(b, return_exceptions=True)
            assert seen[0]["cancel_event"] is cancel_a
            assert not cancel_a.is_set() and not a.done()
            release.set()
            elicitation_response = await server_read.receive()
            assert elicitation_response.message.root.result == {"action": "accept", "content": {"selected": True}}
            await server_write.send(SessionMessage(types.JSONRPCMessage(types.JSONRPCResponse(
                jsonrpc="2.0", id=request_a.message.root.id,
                result={"content": [{"type": "text", "text": "A finished"}]}))))
            assert (await a).text == "A finished"
            assert client._active_callback_owner() is None
        finally:
            release.set()
            for task in (a, b):
                if not task.done():
                    task.cancel()
            await asyncio.gather(a, b, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after", [("accept", None), ("cancel", None), (None, "accept"), (None, "cancel")])
async def test_elicitation_hook_decisions_and_typed_content_reach_sdk(before, after):
    observed = []
    typed = {"text": "value", "count": 0, "enabled": False}
    schema = {"type": "object", "properties": {"text": {"type": "string"}, "count": {"type": "integer"},
        "enabled": {"type": "boolean"}}, "required": ["text", "count", "enabled"]}
    manager = HookManager(hooks={event: [_HookEntry(matcher=re.compile(".*"), command="fixture")]
        for event in (HookEvent.ELICITATION, HookEvent.ELICITATION_RESULT)})
    async def execute(entry, event, fields, **kwargs):
        observed.append((event, fields))
        action = before if event == HookEvent.ELICITATION else after
        return HookExecutionResult(json.dumps({"action": action, "content": typed} if action else {}), "", 0)
    manager._execute_entry = execute
    class Session:
        turn_wait_state = TurnWaitState()
        sent = []
        async def send_payload(self, payload, **kwargs):
            self.sent.append(payload)
            self.turn_wait_state.pending_elicitations[payload["request_id"]].set_result({"action": "accept", "content": typed})
            return True
        async def emit_approval_cancelled_once(self, *args, **kwargs):
            pass
    session = Session()
    bootstrap = object.__new__(AppBootstrap)
    bootstrap._resolve_mcp_request_session = lambda payload: (session, {"conversation_id": "hook", "hook_manager": manager})
    result = await bootstrap._handle_mcp_elicitation({"prompt": "Fill form", "schema": schema, "_mcp_server_name": "server"})
    expected = after or before or "accept"
    assert result["action"] == expected
    assert result.get("content") == (typed if expected == "accept" else None)
    assert len(session.sent) == int(before is None)
    assert [event for event, _ in observed] == [HookEvent.ELICITATION, HookEvent.ELICITATION_RESULT]
    sdk = types.ElicitResult.model_validate(result)
    assert sdk.action == expected and sdk.content == result.get("content")
    assert session.turn_wait_state.pending_elicitations == {}


@pytest.mark.asyncio
async def test_async_prompt_hook_keeps_model_lease_after_parent_exit():
    class Adapter:
        def __init__(self):
            self.started, self.release, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def side_query(self, *args, **kwargs):
            self.started.set()
            await self.release.wait()
            return '{"ok":true}'
        async def aclose(self):
            self.closed.set()
    adapter = Adapter()
    session = SimpleNamespace(_llm_adapter_cache={"old": adapter})
    end = asyncio.Event()
    parent = asyncio.create_task(end.wait())
    _lease_session_llm_for_task(session, adapter, parent)
    owner = RunContext(retain_model=lambda llm, task: _lease_session_llm_for_task(session, llm, task))
    manager = HookManager(hooks={HookEvent.USER_PROMPT_SUBMIT: [_HookEntry(matcher=re.compile(".*"),
        hook_type="prompt", prompt="Check condition", run_async=True)]})
    manager.bind_runtime(llm=adapter, tool_registry=ToolRegistry(),
        tool_context=SimpleNamespace(run_context=owner, metadata={}, emit_event=None))
    try:
        await manager.run_user_prompt_submit("Start")
        await asyncio.wait_for(adapter.started.wait(), 1)
        _clear_session_llm_cache(session)
        end.set()
        await parent
        await asyncio.sleep(0)
        assert not adapter.closed.is_set()
        assert manager.pending_async_hooks == 1 and len(owner.lifecycle_cleanup_tasks) == 1
        adapter.release.set()
        await manager.drain_async_hooks()
        await asyncio.wait_for(adapter.closed.wait(), 1)
    finally:
        end.set()
        adapter.release.set()
        await asyncio.gather(parent, return_exceptions=True)
        await manager.finalize_async_hooks()


def test_code_mode_guidance_uses_reachable_mcp_server_instructions():
    registry = ToolRegistry()
    for tool in (ToolExecTool(), ToolWaitTool(), ToolSearchTool(registry),
        MCPToolProxy("allowed", MCPToolDef("read", "Read", annotations={"readOnlyHint": True}), None),
        MCPToolProxy("denied", MCPToolDef("write", "Write"), None)):
        registry.register(tool)
    policy = ToolsetPolicy(code_mode_only=True).with_disabled_tools({"mcp__denied__write"})
    schemas = registry.get_schemas(toolset_policy=policy)
    derived = derive_turn_tool_schema_state(base_tool_schemas=schemas, mcp_instructions={
        "allowed": "ACTUAL_SERVER_INSTRUCTIONS", "denied": "HIDDEN_SERVER_INSTRUCTIONS"},
        tool_registry=registry, toolset_policy=policy)
    assert not any(schema["function"]["name"].startswith("mcp__") for schema in schemas)
    assert "ACTUAL_SERVER_INSTRUCTIONS" in derived.runtime_guidance
    assert "HIDDEN_SERVER_INSTRUCTIONS" not in derived.runtime_guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_direct_mcp_images_publish_owner_artifact_without_changing_model_blocks(tmp_path, nested):
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVZkAAAAASUVORK5CYII="
    class Client:
        connected = True
        async def call_tool(self, *args, **kwargs):
            return MCPCallResult(content=[{"type": "image", "mimeType": "image/png", "data": png}])
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = MCPToolProxy("picture", MCPToolDef("render", "Image", annotations={"readOnlyHint": True}), None, artifacts)
    tool._static_client = Client()
    registry = ToolRegistry()
    registry.register(tool)
    owner = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        conversation_id="image-owner", artifact_store=artifacts, metadata={"assistant_message_id": "assistant",
            "_toolset_policy": ToolsetPolicy(include_deferred_directly=True)},
        result_sink=(lambda *_args: None) if nested else None)
    try:
        call = ToolCallEvent("picture", tool.name, {})
        result = await run_tool_with_timeout(call, registry, owner)
        assert not result.is_error and result.images == [{"media_type": "image/png", "data": png}]
        builder, state = ContextBuilder(), AgentState(user_message="Render image")
        events = store_result_events(call, result, builder, state, tool_ctx=owner, tool_registry=registry)
        previews = [event for event in events if event.type == "artifact.preview"]
        assert len(previews) == int(not nested)
        assert sum(len(message.images) for message in builder._history) == int(not nested)
        if not nested:
            artifact_id = previews[0].data["artifact_id"]
            assert artifacts.get_meta(artifact_id, conversation_id="image-owner", workspace_root=tmp_path).media_type == "image/png"
            assert artifacts.get_meta(artifact_id, conversation_id="other-owner", workspace_root=tmp_path) is None
            assert events[-1].data["artifact_id"] == artifact_id
    finally:
        artifacts.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_error,new_error", [(False, True), (True, False)])
async def test_extension_error_patch_normalizes_model_state_and_public_status(tmp_path, old_error, new_error):
    class Tool(_DelayedWrite):
        async def execute(self, args, context=None):
            return ToolResult("original", is_error=old_error, status="failed" if old_error else "success",
                error_kind="fixture_failure" if old_error else None,
                projection="error" if old_error else None)
    async def after(*args, **kwargs):
        return ToolResultPatch(is_error=new_error)
    registry = ToolRegistry()
    tool = Tool(tmp_path / "unused")
    registry.register(tool)
    owner = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        run_context=RunContext(lifecycle_runtime=SimpleNamespace(after_tool_call=after)))
    call = ToolCallEvent("patched", tool.name, {})
    result = await run_tool_with_timeout(call, registry, owner)
    builder, state = ContextBuilder(), AgentState(user_message="Patch tool result")
    events = store_result_events(call, result, builder, state, tool_ctx=owner, tool_registry=registry)
    status = "failed" if new_error else "success"
    assert result.is_error == new_error and result.status == status
    assert events[-1].data["status"] == status and events[-1].data["is_error"] == new_error
    assert state.tool_calls[-1].status == status and state.tool_calls[-1].result_payload["is_error"] == new_error
    if not new_error:
        assert not events[-1].data.get("error_info") and not state.tool_calls[-1].error_kind


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_child", [False, True])
async def test_child_private_commands_exit_before_child_terminal(tmp_path, monkeypatch, cancel_child):
    captures = []
    class Manager(BackgroundCommandManager):
        async def shutdown(self):
            captures.extend(self._commands.values())
            await super().shutdown()
    class Model(LLMAdapter):
        def __init__(self):
            self.calls, self.waiting = 0, asyncio.Event()
        async def simple_chat(self, messages, **kwargs):
            return "Done"
        async def stream_chat(self, messages, tools=None, metadata=None):
            self.calls += 1
            if self.calls == 1:
                command = f'"{sys.executable}" -c "import time; print(123, flush=True); time.sleep(8)"'
                if os.name == "nt":
                    command = "& " + command
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent("command", "run_command", {
                    "command": command, "run_in_background": True, "yield_time_ms": 1000})], tool_calls_committed=True)
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            else:
                self.waiting.set()
                if cancel_child:
                    await asyncio.Event().wait()
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Watcher started.", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
    monkeypatch.setattr("backend.agent.query_engine.BackgroundCommandManager", Manager)
    model, registry = Model(), ToolRegistry()
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", conversation_id="child-command", session_id="parent-session")
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    task_tool = TaskTool(llm_provider=model, tool_registry_provider=registry, artifact_store=artifacts,
        permission_checker_provider=checker, agent_settings_provider=AgentSettings(max_iterations=4, code_mode_only=False),
        token_budget_provider=TokenBudget())
    registry.register(RunCommandTool(artifacts))
    registry.register(task_tool)
    owner = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        session_id="parent-session", conversation_id="child-command", task_id="parent-task", metadata={"run_id": "parent"},
        run_context=RunContext(agent_runtime=runtime), tool_registry=registry, permission_checker=checker)
    job = asyncio.create_task(task_tool.execute({"description": "Start watcher", "prompt": "Run watcher and finish."}, context=owner))
    try:
        await asyncio.wait_for(model.waiting.wait(), 8)
        if cancel_child:
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        assert captures
        assert all(command.pid is not None for command in captures)
        assert all(command.status == "cancelled" and not command.cleanup_pending for command in captures)
        assert all(process_identity_matches(command.pid, command.process_start_time) is False for command in captures)
        child = next(iter(runtime._subagents))
        facts = [event.payload for event in runtime.execution_journal(child).read_events() if event.event_type == "cleanup"]
        assert any(fact.get("reason") == "child_session_closed" and fact.get("status") == "cancelled" for fact in facts)
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        runtime.close(release_lease=True)
        artifacts.shutdown()
