from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
from starlette.websockets import WebSocket

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.mcp.app_runtime import MCPAppRuntime
from backend.mcp.app_ui import MCPAppError, parse_app_resource, registered_app_declaration, resource_csp
from backend.mcp.manager import MCPServerConfig, MCPServerManager
from backend.mcp.registry import MCPToolRegistry
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import SandboxPolicy
from backend.tests.fixtures.mcp_apps_peer import URI
from backend.tools.registry import ToolRegistry

PEER = Path(__file__).parent / "fixtures/mcp_apps_peer.py"

async def owned_invocation(tmp_path, *, read_only=False, elicitation_handler=None, session_id="", conversation_id="conversation", store_id="owned-store"):
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path, elicitation_handler=elicitation_handler)
    config = MCPServerConfig("owned", command=sys.executable, args=["-u", str(PEER), str(tmp_path / "peer.jsonl")])
    await manager.start_server(config)
    registry = ToolRegistry()
    adapter = MCPToolRegistry(registry, mcp_manager=manager)
    adapter.register_server_tools("owned", manager.get_all_tools()["owned"], manager.get_client("owned"))
    runtime = MCPAppRuntime()
    run = RunContext(mcp_app_runtime=runtime, mcp_manager=manager, is_subagent=read_only)
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path, session_id=session_id,
        conversation_id=conversation_id, tool_call_id="actual-call", metadata={"conversation_store_id": store_id}, run_context=run,
        sandbox_policy=SandboxPolicy(disable_os_sandbox=True, workspace_root=tmp_path))
    result = await registry.get_tool("mcp__owned__counter").execute({"value": 7}, context=context)
    assert not result.is_error, result.content
    return manager, runtime, context, result

@pytest.mark.asyncio
async def test_real_stdio_app_outlives_turn_catalog_and_preserves_private_ui_boundary(tmp_path):
    manager, runtime, context, result = await owned_invocation(tmp_path)
    receipt = result.runtime_metadata["mcp_app_receipt"]
    original_client = manager.get_client("owned")
    assert receipt.public_fields()["mcp_app"]["resourceUri"] == URI
    assert "UI_ONLY_SECRET" not in json.dumps(result.mcp_result)
    assert "app_only" in [tool.name for tool in manager.get_all_tools()["owned"]]
    # The model turn's manager is gone. The App owns its captured server lease.
    await manager.stop_all()
    assert original_client.connected
    try:
        view = await runtime.open(receipt.invocation_id, "conversation")
        second = await runtime.open(receipt.invocation_id, "conversation")
        assert view["toolInput"] == {"value": 7}
        assert view["toolResult"]["_meta"]["privateUi"] == "UI_ONLY_SECRET"
        assert second["viewId"] != view["viewId"]
        request = lambda method, params: runtime.request(receipt.invocation_id, "conversation", view["viewId"], method, params)
        listed = await request("tools/list", {})
        assert {tool["name"] for tool in listed["tools"]} == {"counter", "app_only"}
        actual = await request("tools/call", {"name": "app_only", "arguments": {"value": 11}, "_meta": {"threadId": "spoofed"}})
        assert actual["structuredContent"]["value"] == 11
        assert actual["structuredContent"]["nonce"] == view["toolResult"]["structuredContent"]["nonce"]
        assert actual["structuredContent"]["callCount"] == 2
        for tool in ("model_only", "mcp__foreign__tool"):
            with pytest.raises(MCPAppError, match="permitted server catalog"):
                await request("tools/call", {"name": tool})
        await request("ui/update-model-context", {"content": [{"type": "text", "text": "external selection"}]})
        assert runtime.model_context("another-store", "conversation") == []
        builder = ContextBuilder(conversation_id="conversation", workspace_root=tmp_path)
        builder.bind_tool_context(context)
        builder.append_user("Actual human request")
        before = deepcopy(builder.guardian_evidence.snapshot())
        state = AgentState(user_message="Actual human request", conversation_id="conversation", workspace_root=tmp_path)
        messages = await builder.build(state)
        app_message = next(message for message in messages if "<mcp_app_context>" in message.content)
        assert "not a human instruction or authorization" in app_message.content and "external selection" in app_message.content
        assert not any("external selection" in message.content for message in builder._history)
        assert builder.guardian_evidence.snapshot().transcript == before.transcript
        await runtime.close_view(receipt.invocation_id, "conversation", view["viewId"])
        with pytest.raises(MCPAppError, match="closed"):
            await request("tools/list", {})
    finally:
        await runtime.aclose()
    assert not original_client.connected
    rows = [json.loads(line) for line in (tmp_path / "peer.jsonl").read_text(encoding="utf-8").splitlines()]
    initializes = [row for row in rows if row.get("method") == "initialize"]
    assert len(initializes) == 1
    assert len({row["_peerNonce"] for row in rows}) == len({row["_peerPid"] for row in rows}) == 1
    assert all(row["params"]["capabilities"]["extensions"]["io.modelcontextprotocol/ui"]["mimeTypes"] == ["text/html;profile=mcp-app", "text/html+skybridge"] for row in initializes)
    actual = next(row for row in rows if row.get("method") == "tools/call" and row["params"]["name"] == "app_only")
    assert actual["params"]["_meta"]["threadId"] == "conversation"

@pytest.mark.asyncio
async def test_child_receipt_is_read_only_and_resource_capabilities_are_exact(tmp_path, monkeypatch):
    from backend import main
    from backend.api import _state

    monkeypatch.delenv("MINICODE_RUNTIME_TOKEN", raising=False)
    manager, runtime, _, result = await owned_invocation(tmp_path, read_only=True)
    monkeypatch.setattr(_state, "bootstrap", SimpleNamespace(mcp_apps=runtime))
    receipt = result.runtime_metadata["mcp_app_receipt"]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=("127.0.0.1", 34567)), base_url="http://127.0.0.1:8787") as client:
            opened = await client.post(f"/api/mcp/apps/{receipt.invocation_id}/open", json={"conversation_id": "conversation"})
            assert opened.status_code == 200, opened.text
            view = opened.json()
            assert view["readOnly"]
            assert view["frameOrigin"].endswith(".localhost:8787")
            frame = await client.get(view["frameUrl"])
            assert frame.status_code == 200 and "Owned MCP counter" in frame.text
            assert "connect-src 'none'" in frame.headers["content-security-policy"]
            wrong = await client.get(view["frameUrl"].replace("mcp-app-", "mcp-app-wrong"))
            assert wrong.status_code == 403
            for method in ("tools/call", "ui/update-model-context", "ui/legacy/set-widget-state"):
                response = await client.post(f"/api/mcp/apps/{receipt.invocation_id}/request", json={"conversation_id": "conversation", "view_id": view["viewId"], "method": method, "params": {"name": "app_only"}})
                assert response.status_code == 403
            for path in ("/api/health", "/api/v1/health", "/api/runtime/shutdown", "/health"):
                by_host = await client.get(view["frameOrigin"] + path)
                by_origin = await client.get(path, headers={"Origin": view["frameOrigin"]})
                assert by_host.status_code == by_origin.status_code == 403
            assert (await client.get("/openapi.json")).status_code == 200
            for host, origin in (("127.0.0.1:8787", view["frameOrigin"]), (view["frameOrigin"].removeprefix("http://"), None)):
                headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])
                websocket = WebSocket({"type": "websocket", "scheme": "ws", "path": "/ws", "query_string": b"", "headers": headers, "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 1)}, None, None)
                assert not main._websocket_origin_allowed(websocket)
    finally:
        await manager.stop_all()
        await runtime.aclose()

def test_only_registered_metadata_selects_resources_and_resource_csp_is_enforced():
    assert registered_app_declaration({"ui": {"resourceUri": "ui://nested"}, "ui/resourceUri": "ui://flat", "openai/outputTemplate": "ui://legacy"}).resource_uri == "ui://nested"
    assert registered_app_declaration({"content": "<html>untrusted</html>"}) is None
    with pytest.raises(MCPAppError, match="unsupported MIME"):
        parse_app_resource({"contents": [{"uri": URI, "mimeType": "text/html", "text": "<script>1</script>"}]}, URI)
    with pytest.raises(MCPAppError, match="metadata"):
        parse_app_resource({"contents": [{"uri": URI, "mimeType": "text/html+skybridge", "text": "html", "_meta": {"ui": [1]}}]}, URI)
    with pytest.raises(MCPAppError, match="CSP origin"):
        parse_app_resource({"contents": [{"uri": URI, "mimeType": "text/html+skybridge", "text": "html", "_meta": {"ui": {"csp": {"connectDomains": ["https://ok; script-src *"]}}}}]}, URI)

@pytest.mark.asyncio
async def test_committed_real_tool_event_reconnect_and_next_provider_receive_app_context(tmp_path, monkeypatch):
    from backend.llm.base import ToolCallEvent
    from backend.tests import test_code_execution as harness
    from backend.ws.stream_state import _tool_record

    manager, runtime, _, _ = await owned_invocation(tmp_path)
    tools = ToolRegistry()
    adapter = MCPToolRegistry(tools, mcp_manager=manager)
    adapter.register_server_tools("owned", manager.get_all_tools()["owned"], manager.get_client("owned"))
    tool = tools.get_tool("mcp__owned__counter")
    original = harness.RunContext
    monkeypatch.setattr(harness, "RunContext", lambda **kwargs: original(mcp_app_runtime=runtime, **kwargs))
    class ScopedBuilder(ContextBuilder):
        def bind_tool_context(self, context):
            if context is not None:
                context.metadata["conversation_store_id"] = "owned-store"
            super().bind_tool_context(context)
    model = harness.ScriptModel("", first_call=ToolCallEvent("actual", tool.name, {"value": 7}))
    builder = ScopedBuilder(llm=model, conversation_id="code-conv", workspace_root=tmp_path)
    async def observe(event):
        if event.type == "tool_result" and "mcp_app" in event.data:
            view = await runtime.open(event.data["mcp_app"]["id"], "code-conv")
            await runtime.request(event.data["mcp_app"]["id"], "code-conv", view["viewId"], "ui/update-model-context", {"content": [{"type": "text", "text": "PROVIDER_SELECTION"}]})
    try:
        state, builder, journal, events, _ = await harness.run_model(tmp_path, model, [tool], context=builder, observe=observe)
        assert state.terminal_status == "completed"
        final = next(event.data for event in events if event.type == "tool_result" and "mcp_app" in event.data)
        assert final["mcp_app_resource_uri"] == URI
        committed = next(record for record in state.tool_calls if record.tool_call_id == "actual")
        assert committed.result_payload["mcp_app"] == final["mcp_app"]
        assert _tool_record(None, final)["mcpApp"] == final["mcp_app"]
        assert any("PROVIDER_SELECTION" in message.content for message in model.inputs[-1])
        assert "UI_ONLY_SECRET" not in json.dumps([message.content for request in model.inputs for message in request])
        assert not any("PROVIDER_SELECTION" in entry.text for entry in builder.guardian_evidence.transcript)
        assert any(event.payload.get("record", {}).get("mcpApp", {}).get("id") == final["mcp_app"]["id"]
            or event.payload.get("mcp_app", {}).get("id") == final["mcp_app"]["id"] for event in journal.read_events())
    finally:
        await manager.stop_all()
        await runtime.aclose()

@pytest.mark.asyncio
async def test_app_keeps_original_executor_session_after_same_id_replacement_and_retirement(tmp_path):
    import base64
    from backend.mcp.environment_runtime import MCPEnvironmentRuntime, PreparedEnvironmentCapabilities
    from backend.tests.test_environments_http import _environment, _reply, _delta
    from backend.tests.test_remote_mcp_skill_runtime import _capture

    nonce, calls, methods = "original-executor-session", 0, []
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(socket, message):
        nonlocal calls
        params = message["params"]
        if params["method"] in {"GET", "DELETE"}:
            await _reply(socket, message, status=405 if params["method"] == "GET" else 204)
            await _delta(socket, params["requestId"], 1, done=True)
            return
        request = json.loads(base64.b64decode(params["bodyBase64"]))
        methods.append(request["method"])
        if "id" not in request:
            await _reply(socket, message, status=202)
            await _delta(socket, params["requestId"], 1, done=True)
            return
        if request["method"] == "initialize":
            result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "original", "version": "1"}}
        else:
            headers = {header["name"].lower(): header.get("value") for header in params["headers"]}
            assert headers["mcp-session-id"] == nonce
            if request["method"] == "tools/list":
                result = {"tools": [{"name": "counter", "inputSchema": {"type": "object"}, "_meta": {"ui": {"resourceUri": URI}}}]}
            elif request["method"] == "resources/read":
                result = {"contents": [{"uri": URI, "mimeType": "text/html;profile=mcp-app", "text": "<html>original</html>"}]}
            else:
                calls += 1
                if request["params"].get("arguments", {}).get("wait"):
                    entered.set()
                    await release.wait()
                result = {"content": [{"type": "text", "text": nonce}], "structuredContent": {"nonce": nonce, "calls": calls}}
        await _reply(socket, message, headers=[{"name": "content-type", "value": "application/json"}, {"name": "mcp-session-id", "value": nonce}])
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
        await _delta(socket, params["requestId"], 1, body, done=True)

    async with _environment(tmp_path, handler) as (environments, original, requests):
        thread, snapshot = await _capture(environments, "file:///workspace")
        environment_runtime = MCPEnvironmentRuntime(environments, snapshot, local_cwd="file:///workspace")
        source = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path,
            environment_runtime=environment_runtime, runtime_configs=(MCPServerConfig("original", transport="http", url="https://original.invalid/mcp", environment_id="remote"),))
        prepared = PreparedEnvironmentCapabilities(source, None, (), (), environment_runtime, True, "conversation", "turn")
        await source.start_all()
        client = source.get_client("original")
        tools = ToolRegistry()
        MCPToolRegistry(tools, mcp_manager=source).register_server_tools("original", source.get_all_tools()["original"], client)
        apps = MCPAppRuntime()
        context = ToolExecutionContext(PermissionContext(mode="bypass"), conversation_id="conversation", tool_call_id="original-call",
            metadata={"conversation_store_id": "original-store"}, run_context=RunContext(mcp_app_runtime=apps), workspace_root=tmp_path)
        try:
            result = await tools.get_tool("mcp__original__counter").execute({}, context=context)
            assert not result.is_error, result.content
            app = result.runtime_metadata["mcp_app_receipt"]
            view = await apps.open(app.invocation_id, "conversation")
            ui_task = asyncio.create_task(apps.request(app.invocation_id, "conversation", view["viewId"], "tools/call", {"name": "counter", "arguments": {"wait": True}}))
            await asyncio.wait_for(entered.wait(), 2)
            assert not source.pending_requests(), "App work must not extend a model-turn cleanup fence"
            environments.upsert({"id": "remote", "program": sys.executable, "args": ["-c", "pass"]})
            assert environments.get_environment("remote") is not original
            await thread.aclose()
            await snapshot.aclose()
            await asyncio.wait_for(prepared.aclose(), 0.5)
            assert client.connected and not original.closed and environment_runtime.snapshot is not None
            release.set()
            observed = await ui_task
            assert observed["structuredContent"] == {"nonce": nonce, "calls": 2}
            resource = await apps.request(app.invocation_id, "conversation", view["viewId"], "resources/read", {"uri": URI})
            assert resource["contents"][0]["text"] == "<html>original</html>"
            assert methods.count("initialize") == 1
        finally:
            release.set()
            await source.stop_all()
            await environment_runtime.aclose()
            await apps.aclose()
            await thread.aclose()
            await snapshot.aclose()
        assert not client.connected and original.closed and environment_runtime.snapshot is None

@pytest.mark.asyncio
async def test_last_app_close_ack_failure_retains_original_client_for_retry(tmp_path):
    manager, apps, _, result = await owned_invocation(tmp_path)
    app = result.runtime_metadata["mcp_app_receipt"]
    client = manager.get_client("owned")
    class CloseAck:
        interrupted, calls = True, 0
        async def close(self):
            self.calls += 1
            if self.interrupted:
                raise ConnectionError("owned close ACK interrupted")
    callback = CloseAck()
    client._oauth_callback = callback
    await manager.stop_all()
    with pytest.raises(ConnectionError, match="cleanup is still pending"):
        await apps.close_conversation("owned-store", "conversation")
    retained = apps.invocations[app.invocation_id]
    assert retained.client is client and retained.connection_lease.client is client
    assert client._app_borrowers == 0 and client._oauth_callback is callback
    attempts = callback.calls
    callback.interrupted = False
    await apps.close_conversation("owned-store", "conversation")
    assert callback.calls == attempts + 1 and client._app_borrowers == 0 and not client.connected
    assert client._oauth_callback is None
    assert not apps.invocations

@pytest.mark.asyncio
async def test_app_server_elicitation_uses_registered_thread_owner_after_model_turn_ends(tmp_path):
    from backend.bootstrap.app import AppBootstrap
    from backend.conversations.repository import ConversationRepository
    from backend.tests.test_full_source_audit_context_repairs import _ElicitationSession

    repository = ConversationRepository(tmp_path / "conversation-repository")
    conversation = repository.create_conversation(workspace_root=str(tmp_path))
    session = _ElicitationSession(repository, {"action": "accept", "content": {"selected": "yes"}})
    async def status(*args):
        pass
    bootstrap = AppBootstrap(build_tool_registry=lambda *args: ToolRegistry(), create_session_llm=lambda *args: None,
        ws_manager=SimpleNamespace(iter_sessions=lambda: [session]), on_mcp_status_change=status)
    manager, apps, context, result = await owned_invocation(tmp_path, elicitation_handler=bootstrap._handle_mcp_elicitation,
        session_id=session.session_id, conversation_id=conversation.id, store_id=repository.store_instance_id())
    bootstrap.mcp_apps = apps
    context.permission = PermissionContext(mode="confirm", approval_policy="on-request")
    app = result.runtime_metadata["mcp_app_receipt"]
    await manager.stop_all()
    try:
        view = await apps.open(app.invocation_id, conversation.id)
        observed = await apps.request(app.invocation_id, conversation.id, view["viewId"], "tools/call", {"name": "app_only", "arguments": {"elicit": True}})
        response = observed["structuredContent"]["elicitationResponse"]["result"]
        assert response == {"action": "accept", "content": {"selected": "yes"}}
        assert session.prompts[0]["conversation_id"] == conversation.id
        session.conversation_repo = ConversationRepository(tmp_path / "another-store")
        other = await apps.request(app.invocation_id, conversation.id, view["viewId"], "tools/call", {"name": "app_only", "arguments": {"elicit": True}})
        assert other["structuredContent"]["elicitationResponse"]["result"]["action"] == "cancel"
        assert len(session.prompts) == 1
    finally:
        await apps.aclose()
