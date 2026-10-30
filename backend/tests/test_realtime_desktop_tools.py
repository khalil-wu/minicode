"""Original desktop claims through live voice discovery and the native HTTP boundary."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from backend.agent.message import UserCommand
from backend.agent.tool_execution import run_tool
from backend.llm.base import ToolCallEvent
from backend.permissions.context import ToolCallSource
from backend.realtime.desktop_client import DesktopVoiceClaim, DesktopVoiceClient
from backend.realtime.protocol import Start
from backend.realtime.session import thread_runtime
from backend.realtime.transport import RealtimeConfig
from backend.tests.test_realtime_source_chain import peer, until
from backend.tests.test_realtime_voice_tool import names, owned_call
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.tools.realtime_tools import CaptureScreenContextTool, TransferVoiceCallTool
from backend.tools.tool_search import ToolSearchTool
from backend.tools.toolsets import ToolsetPolicy
from backend.ws.manager import WebSocketManager


def claim(**patch):
    return DesktopVoiceClaim.model_validate({"claimId": "original-voice", "hostId": "local",
        "captureScreenContext": True, "transferVoiceCall": True, **patch})


@asynccontextmanager
async def native_peer():
    calls = []
    response = {"status": 200, "body": {"ok": True, "value": {"page": "thread", "thread": {"id": "current-page", "title": None},
        "rightSidebarVisible": True, "rightPanelTabs": [{"type": "browser", "focused": True}]}}}
    async def accept(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
        headers = dict(line.split(": ", 1) for line in head.split("\r\n")[1:] if ": " in line)
        body = json.loads(await reader.readexactly(int(headers["Content-Length"])))
        calls.append({"head": head, "body": body})
        if "before_response" in response:
            await response["before_response"](body)
        encoded = json.dumps(response["body"]).encode()
        writer.write(f"HTTP/1.1 {response['status']} Native\r\nContent-Type: application/json\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", calls, response
    finally:
        server.close()
        await server.wait_closed()


def desktop_session():
    return SimpleNamespace(window_instance_id="original-window", window_owner_id="primary", _window_binding_token="original-binding")


@pytest.mark.asyncio
async def test_real_loopback_native_request_preserves_original_claim_owner_and_model_call(monkeypatch):
    async with native_peer() as (endpoint, calls, response):
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", endpoint)
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "native-only-token")
        client = DesktopVoiceClient(desktop_session(), claim())
        value = await client.invoke(conversation_id="voice-owner", name="capture_screen_context", arguments={}, call_id="actual-model-call")
        assert value["thread"]["id"] == "current-page"
        assert calls[0]["head"].startswith("POST /v1/voice-control HTTP/1.1")
        assert "Authorization: Bearer native-only-token" in calls[0]["head"]
        assert calls[0]["body"] == {"window_instance_id": "original-window", "window_owner_id": "primary", "window_binding_token": "original-binding",
            "claim_id": "original-voice", "host_id": "local", "conversation_id": "voice-owner", "tool": "capture_screen_context", "arguments": {}, "call_id": "actual-model-call"}
        response.update(status=403, body={"ok": False, "error": "Original desktop claim expired"})
        with pytest.raises(ValueError, match="claim expired"):
            await client.invoke(conversation_id="voice-owner", name="transfer_voice_call", arguments={"threadId": "target"}, call_id="second-call")


@pytest.mark.asyncio
async def test_all_desktop_voice_tools_share_actual_owner_gates_and_discovery_capabilities():
    async with owned_call() as (service, context, _committed, _emitted):
        registry = context.tool_registry
        registry.register(CaptureScreenContextTool()); registry.register(TransferVoiceCallTool())
        search = ToolSearchTool(registry); registry.register(search)
        policy = replace(ToolsetPolicy.default(), realtime_service=service, supports_search_tool=True)
        context.metadata["_toolset_policy"] = policy
        before = policy.cache_key()
        assert not policy.is_available(registry.get_tool_spec("capture_screen_context"))
        service.desktop_client = SimpleNamespace(supports=lambda name: name == "capture_screen_context")
        assert policy.cache_key() != before
        assert policy.is_available(registry.get_tool_spec("capture_screen_context"))
        assert not policy.is_available(registry.get_tool_spec("transfer_voice_call"))
        found = await search.execute({"query": "capture screen context"}, context)
        assert "capture_screen_context" in {tool["name"] for namespace in found.tool_search_output for tool in namespace["tools"]}
        context.tool_call_id = "nested"; context.call_sources["nested"] = ToolCallSource(kind="code_mode")
        assert json.loads((await search.execute({"query": "capture screen context"}, context)).content) == {"tools": []}
        assert not names(registry.get_schemas(toolset_policy=replace(policy, nested_surface=True))) & {"capture_screen_context", "transfer_voice_call"}
        for tool in (CaptureScreenContextTool(), TransferVoiceCallTool()):
            context.run_context.is_subagent = True
            assert (await tool.execute({}, context)).is_error
            context.run_context.is_subagent = False
            context.metadata["_toolset_policy"] = replace(policy, realtime_service=None)
            assert (await tool.execute({}, context)).is_error
            context.metadata["_toolset_policy"] = policy
        await service.aclose()
        assert not policy.is_available(registry.get_tool_spec("capture_screen_context"))


@pytest.mark.asyncio
async def test_actual_capture_model_tool_uses_original_request_and_rejects_late_service_result(monkeypatch):
    async with native_peer() as (endpoint, calls, _response):
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", endpoint)
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "native-only-token")
        async with owned_call() as (service, context, _committed, _emitted):
            service.desktop_client = DesktopVoiceClient(desktop_session(), claim())
            context.tool_registry.register(CaptureScreenContextTool())
            context.tool_call_id = "actual-capture"
            result = await run_tool(ToolCallEvent(id="actual-capture", name="capture_screen_context", arguments={}), context.tool_registry, context)
            assert not result.is_error, result.content
            assert json.loads(result.content)["page"] == "thread"
            assert calls[0]["body"]["conversation_id"] == "original"
            assert calls[0]["body"]["call_id"] == "actual-capture"
            started, release = asyncio.Event(), asyncio.Event()
            async def delayed(**_kwargs):
                started.set(); await release.wait(); return {"page": "settings"}
            service.desktop_client = SimpleNamespace(supports=lambda _: True, invoke=delayed)
            pending = asyncio.create_task(CaptureScreenContextTool().execute({}, context))
            await started.wait(); await service.aclose(); release.set()
            assert (await pending).is_error


@pytest.mark.asyncio
async def test_transfer_keeps_original_tool_call_result_after_the_actual_original_service_stop(monkeypatch):
    async with native_peer() as (endpoint, calls, response):
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", endpoint)
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "native-only-token")
        async with owned_call() as (service, context, _committed, _emitted):
            service.desktop_client = DesktopVoiceClient(desktop_session(), claim())
            context.tool_registry.register(TransferVoiceCallTool())
            async def original_stop(_body): await service.aclose()
            response.update(body={"ok": True, "value": {"transferred": True, "threadId": "actual-target", "hostId": "ssh-real"}}, before_response=original_stop)
            result = await run_tool(ToolCallEvent(id="actual-transfer", name="transfer_voice_call",
                arguments={"threadId": "actual-target", "hostId": "ssh-real", "context": "Actual handoff"}), context.tool_registry, context)
            assert not result.is_error, result.content
            assert json.loads(result.content) == {"transferred": True, "threadId": "actual-target", "hostId": "ssh-real"}
            assert not service.active
            assert calls[0]["body"]["call_id"] == "actual-transfer"
            assert calls[0]["body"]["arguments"] == {"threadId": "actual-target", "hostId": "ssh-real", "context": "Actual handoff"}


@pytest.mark.asyncio
async def test_source_start_keeps_desktop_claim_private_and_binds_original_service(tmp_path, monkeypatch):
    from backend.realtime import session as module
    async with peer() as remote:
        monkeypatch.setattr(module, "capture_config", lambda *_args, **_kwargs: RealtimeConfig(remote.base_url, {}, configured=True))
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:9")
        monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "native-only-token")
        manager = WebSocketManager()
        connection = _connection(tmp_path, "desktop-voice-binding")
        session, _ = await manager.connect(**connection)
        session.window_instance_id, session.window_owner_id, session._window_binding_token = "original-window", "primary", "original-binding"
        original, other = session.conversation_repo.create_conversation(), session.conversation_repo.create_conversation()
        session.active_conversation_id = original.id
        try:
            await session.command_dispatcher._dispatch_client_command(UserCommand(type="thread/realtime/start", data={
                "threadId": original.id, "conversation_id": original.id, "outputModality": "audio", "client_command_id": "actual-start",
                "desktopVoice": claim().model_dump(by_alias=True)}), session.connection_generation)
            runtime = thread_runtime(session, original.id).realtime_runtime
            await until(lambda: runtime.started)
            assert runtime.desktop_client.claim.claim_id == "original-voice"
            assert runtime.desktop_client.owner["window_instance_id"] == "original-window"
            assert not hasattr(runtime.params, "desktop_voice")
            assert thread_runtime(session, other.id).realtime_runtime is None
            with pytest.raises(ValidationError):
                Start.model_validate({"threadId": original.id, "outputModality": "audio", "desktopVoice": claim().model_dump(by_alias=True)})
        finally: await _release_session(session)


@pytest.mark.asyncio
async def test_voice_target_preflight_uses_the_original_connected_store_and_no_credentials(tmp_path, monkeypatch):
    from backend.api import routes_thread_metadata as routes
    from backend.conversations.repository import ConversationRepository
    from backend.realtime import session as module
    repository, foreign = ConversationRepository(tmp_path / "original"), ConversationRepository(tmp_path / "foreign")
    original, elsewhere = repository.create_conversation(), foreign.create_conversation()
    current = SimpleNamespace(conversation_repo=repository, is_connected=True)
    monkeypatch.setattr(routes._state, "ws_manager", SimpleNamespace(get_session=lambda value: current if value == "original-session" else None))
    captured = []
    def project(session, conversation_id):
        captured.append((session, conversation_id))
        return RealtimeConfig("http://provider.invalid", {}, configured=True).capabilities()
    monkeypatch.setattr(module, "project_capabilities", project)
    monkeypatch.setattr(module, "capture_config", lambda *_args, **_kwargs: pytest.fail("Voice preflight must not capture startup credentials"))
    app = FastAPI(); app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://metadata") as client:
        path = f"/api/thread-metadata/{original.id}/voice"
        query = {"session_id": "original-session"}
        response = await client.get(path, params=query)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["conversationId"] == original.id
        assert captured == [(current, original.id)]
        assert (await client.get(f"/api/thread-metadata/{elsewhere.id}/voice", params=query)).status_code == 404
        repository.set_archived(original.id, True)
        assert (await client.get(path, params=query)).status_code == 404
        repository.set_archived(original.id, False); current.is_connected = False
        assert (await client.get(path, params=query)).status_code == 404
        assert (await client.get(path, params={"session_id": "retired"})).status_code == 404
