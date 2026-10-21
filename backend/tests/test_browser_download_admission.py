from __future__ import annotations

import asyncio
from contextlib import aclosing
import json

import httpx
import pytest

from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tests.test_code_cell_thread_lifetime import setup
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.browser_control_tool import BrowserControlTool


def context_for(tmp_path, owner):
    return ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        conversation_id="original-conversation", session_id="original-session", turn_id="original-turn",
        metadata={"window_instance_id": "original-window", "window_owner_id": "original-owner"},
        window_binding_token="original-binding", run_context=owner, pending_cleanup_tasks=owner.lifecycle_cleanup_tasks)


def bridge_transport(monkeypatch, receive):
    client = httpx.AsyncClient
    monkeypatch.setattr("backend.tools.browser_control_tool.httpx.AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(receive)))
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:43123")
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "captured-backend-token")


def test_download_authorization_is_an_explicit_mutating_capability_and_requires_an_exact_target():
    tool = BrowserControlTool()
    assert tool.check_permission({"action": "allow_download"}) == PermissionLevel.CONFIRM
    assert tool.check_permission({"action": "set_blob_download_origin"}) == PermissionLevel.CONFIRM
    assert not tool.is_read_only({"action": "allow_download"})
    assert tool.is_read_only({"action": "list_downloads"})
    assert tool.check_permission({"action": "list_downloads"}) == PermissionLevel.CONFIRM
    assert "original embedded target_id" in tool.validate_input({"action": "allow_download", "url": "https://example.com/file"})
    assert "exact HTTP" in tool.validate_input({"action": "allow_download", "target_id": "page", "url": "file:///private"})
    assert tool.validate_input({"action": "allow_download", "target_id": "page", "url": "blob:https://example.com/exact"}) == ""
    assert "original embedded target_id" in tool.validate_input({"action": "set_blob_download_origin", "origin": "https://example.com"})
    assert "HTTP or HTTPS origin" in tool.validate_input({"action": "set_blob_download_origin", "target_id": "page", "origin": "file:///private"})
    assert tool.validate_input({"action": "set_blob_download_origin", "target_id": "page", "origin": None}) == ""


@pytest.mark.asyncio
async def test_blob_origin_registration_and_clear_use_the_captured_agent_scope(tmp_path, monkeypatch):
    requests = []

    async def receive(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/v1/session-control":
            return httpx.Response(200, json={"ok": True, "released": 1})
        return httpx.Response(200, json={"ok": True, "target": {"id": "original-page"}, "value": payload["origin"]})

    bridge_transport(monkeypatch, receive)
    owner = RunContext()
    context = context_for(tmp_path, owner)
    tool = BrowserControlTool()
    result = await tool.execute({"action": "set_blob_download_origin", "target_id": "original-page", "origin": "https://example.com"}, context)
    assert not result.is_error and "registered: https://example.com" in result.content
    cleared = await tool.execute({"action": "set_blob_download_origin", "target_id": "original-page", "origin": None}, context)
    assert not cleared.is_error and "cleared" in cleared.content
    await owner.close_environment_snapshots()
    assert requests[0][1]["agent_control_id"] == requests[1][1]["agent_control_id"] == requests[2][1]["agent_control_id"]
    assert requests[0][1]["agent_turn_id"] == "original-turn"


@pytest.mark.asyncio
async def test_download_control_identity_comes_from_the_original_turn_and_releases_the_captured_scope(tmp_path, monkeypatch):
    requests = []

    async def receive(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, request.headers["authorization"], payload))
        if request.url.path == "/v1/session-control":
            return httpx.Response(200, json={"ok": True, "released": 1})
        return httpx.Response(200, json={"ok": True, "target": {"id": "original-page", "url": "https://example.com/"},
            "value": payload.get("url") if payload["action"] == "allow_download" else [{"id": "actual", "path": "/download/original.txt", "state": "completed"}]})

    bridge_transport(monkeypatch, receive)
    owner = RunContext()
    context = context_for(tmp_path, owner)
    tool = BrowserControlTool()
    result = await tool.execute({"action": "allow_download", "target_id": "original-page", "url": "https://example.com/file?exact=1",
        "agent_control_id": "forged-control", "agent_turn_id": "forged-turn", "conversation_id": "forged-conversation",
        "window_instance_id": "forged-window", "window_binding_token": "forged-binding"}, context)
    assert not result.is_error and "10 seconds" in result.content
    first = requests[0][2]
    assert first["agent_control_id"].startswith("browser_control_") and first["agent_control_id"] != "forged-control"
    assert first["agent_turn_id"] == "original-turn" and first["conversation_id"] == "original-conversation"
    assert first["window_instance_id"] == "original-window" and first["window_binding_token"] == "original-binding"
    listed = await tool.execute({"action": "list_downloads"}, context)
    assert not listed.is_error and "original.txt" in listed.content
    assert requests[1][2]["agent_control_id"] == first["agent_control_id"]
    assert requests[1][2]["operation_id"] != first["operation_id"]
    await owner.close_environment_snapshots()
    assert owner.browser_execution is None
    assert requests[-1] == ("/v1/session-control", "Bearer captured-backend-token", {
        "action": "release", "agent_control_id": first["agent_control_id"], "agent_turn_id": "original-turn",
        "conversation_id": "original-conversation", "window_instance_id": "original-window", "window_binding_token": "original-binding",
        "operation_id": requests[1][2]["operation_id"]})


@pytest.mark.asyncio
async def test_actual_pending_native_operation_keeps_download_control_until_its_original_completion(tmp_path, monkeypatch):
    finish = asyncio.Event()
    requests = []
    pending = {"completed": False, "pending": 1, "resource_kind": "browser", "resource_id": "original-call", "execution_outcome": "uncertain"}

    async def receive(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/v1/session-control":
            return httpx.Response(200, json={"ok": True, "released": 1})
        if request.url.path == "/v1/operation":
            await finish.wait()
            return httpx.Response(200, json={"ok": True, "cleanup_receipt": {**pending, "pending": 0, "completed": True}})
        return httpx.Response(400, json={"ok": False, "status": "timeout", "error": "Original native operation is still settling", "cleanup_receipt": dict(pending)})

    bridge_transport(monkeypatch, receive)
    owner = RunContext()
    context = context_for(tmp_path, owner)
    context.tool_call_id = "original-call"
    result = await BrowserControlTool().execute({"action": "evaluate", "target_id": "original-page", "expression": "document.body.textContent"}, context)
    assert result.is_error and result.cleanup_receipt["pending"] == 1
    execution = owner.browser_execution
    assert execution.pending and context.pending_cleanup_tasks
    await owner.close_environment_snapshots()
    assert owner.browser_execution is None and owner.lifecycle_cleanup_tasks
    await asyncio.sleep(0)
    assert all(path != "/v1/session-control" for path, _payload in requests)
    finish.set()
    await asyncio.wait_for(asyncio.gather(*tuple(owner.lifecycle_cleanup_tasks)), 3)
    assert result.cleanup_receipt["completed"] and not result.cleanup_receipt["pending"]
    assert requests[-1][0] == "/v1/session-control"
    assert requests[-1][1]["agent_control_id"] == execution.control_id


@pytest.mark.asyncio
async def test_download_control_release_failure_remains_an_actual_lifecycle_failure(tmp_path, monkeypatch):
    async def receive(request):
        payload = json.loads(request.content)
        if request.url.path == "/v1/session-control":
            return httpx.Response(503, json={"ok": False, "error": "Original browser host is unavailable"})
        return httpx.Response(200, json={"ok": True, "target": {"id": "page"}, "value": payload["url"]})

    bridge_transport(monkeypatch, receive)
    owner = RunContext()
    result = await BrowserControlTool().execute({"action": "allow_download", "target_id": "page", "url": "https://example.com/file"}, context_for(tmp_path, owner))
    assert not result.is_error
    with pytest.raises(ExceptionGroup) as failure:
        await owner.close_environment_snapshots()
    assert any(isinstance(error, httpx.HTTPStatusError) for error in failure.value.exceptions)


@pytest.mark.asyncio
async def test_a_failed_native_completion_observation_does_not_release_unknown_running_browser_control(tmp_path, monkeypatch):
    requests = []
    pending = {"completed": False, "pending": 1, "resource_kind": "browser", "resource_id": "original-call", "execution_outcome": "uncertain"}

    async def receive(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/operation":
            raise httpx.ConnectError("Original native host connection was lost", request=request)
        return httpx.Response(400, json={"ok": False, "status": "timeout", "error": "Original operation is pending", "cleanup_receipt": dict(pending)})

    bridge_transport(monkeypatch, receive)
    owner = RunContext()
    context = context_for(tmp_path, owner)
    context.tool_call_id = "original-call"
    result = await BrowserControlTool().execute({"action": "evaluate", "target_id": "original-page", "expression": "document.body.textContent"}, context)
    await asyncio.gather(*tuple(context.pending_cleanup_tasks))
    assert result.cleanup_receipt["pending"] == 1 and not result.cleanup_receipt["completed"]
    assert "connection was lost" in result.cleanup_receipt["observation_error"]
    with pytest.raises(ExceptionGroup) as failure:
        await owner.close_environment_snapshots()
    assert any("cleanup is not confirmed" in str(error) for error in failure.value.exceptions)
    assert "/v1/session-control" not in requests


@pytest.mark.asyncio
async def test_a_real_retained_code_cell_can_first_claim_browser_control_after_final_and_release_it_after_the_leaf(tmp_path, monkeypatch):
    gate = asyncio.Event()
    started = asyncio.Event()
    requests = []

    class Gate(BaseTool):
        name = "browser_gate"
        read_only = True

        def get_schema(self):
            return ToolSchema(self.name, "Wait for the fixture's original cell gate", {"type": "object", "properties": {}})

        async def execute(self, args, context=None):
            started.set()
            await gate.wait()
            return ToolResult(content="Original cell gate released")

    async def receive(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/v1/session-control":
            return httpx.Response(200, json={"ok": True, "released": 1})
        return httpx.Response(200, json={"ok": True, "target": {"id": "original-page"}, "value": payload["url"]})

    bridge_transport(monkeypatch, receive)
    _unused, model, session, runtime = setup(tmp_path)
    for tool in (Gate(), BrowserControlTool()):
        session.tool_registry.register(tool)
    owner = RunContext(agent_runtime=runtime, window_binding_token="original-binding")
    state = AgentState(user_message="Start the requested background cell and finish this turn.", conversation_id="code-conv", workspace_root=tmp_path)
    model.call = ToolCallEvent(id="original-cell", name="tool_exec", arguments={"code": "text('cell yielded'); await tools.browser_gate({}); const result=await tools.browser_control({action:'allow_download',target_id:'original-page',url:'https://example.com/original.txt'}); text(result.content);", "yield_time_ms": 1})
    model.step, model.state = 0, state
    events = []
    try:
        async with aclosing(QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
            runtime=AgentLoopSessionContext(session_id="browser-late", workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass"),
                metadata={"window_instance_id": "original-window"}, run_context=owner)))) as stream:
            async for event in stream:
                events.append(event)
        await asyncio.wait_for(started.wait(), 3)
        assert state.terminal_status == "completed" and model.report["status"] == "running"
        assert owner.browser_execution is None and not requests
        cell_id = model.report["cell_id"]
        cell = session.code_store.cells[cell_id]
        retirement = next(iter(session.code_store.retirements.values()))
        assert cell.task is not None and not cell.task.done() and not retirement.done()
        gate.set()
        await asyncio.wait_for(cell.task, 5)
        await asyncio.wait_for(retirement, 5)
        assert cell.status == "completed"
        assert [path for path, _payload in requests] == ["/v1/command", "/v1/session-control"]
        started_turn = next(event.data["turn_id"] for event in events if event.type == "agent.run.started")
        assert requests[0][1]["agent_turn_id"] == requests[1][1]["agent_turn_id"] == started_turn
        assert requests[0][1]["agent_control_id"] == requests[1][1]["agent_control_id"]
        assert owner.browser_execution is None
    finally:
        gate.set()
        await session.aclose()
        await runtime.aclose(release_lease=True)
