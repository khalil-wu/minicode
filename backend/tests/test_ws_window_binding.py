from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import WebSocketDisconnect

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.registry import ToolRegistry
from backend.ws import manager as manager_module


class Socket:
    def __init__(self, *, instance="window-a-instance", owner="window-a", proof="a" * 64):
        self.headers = {"sec-websocket-protocol": "minicode, minicode-window." + proof}
        self.query_params = {"session_id": "window-session", "window_instance_id": instance, "window_owner_id": owner}
        self.closed = []
        self.accepted = []

    async def accept(self, **kwargs):
        self.accepted.append(kwargs)

    async def close(self, **kwargs):
        self.closed.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_window_binding_is_verified_against_native_authority_before_adoption(monkeypatch, valid):
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:9911")
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "fixture-broker-token")
    requests = []
    def native(request):
        requests.append(request)
        return httpx.Response(200 if valid else 403, json={"ok": valid, "window_instance_id": "window-a-instance", "window_owner_id": "window-a"})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(manager_module.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(native), **kwargs))
    socket = Socket()
    if valid:
        assert await manager_module._verified_window_binding(socket, "window-a-instance", "window-a") == "a" * 64
        assert socket.closed == []
    else:
        with pytest.raises(WebSocketDisconnect):
            await manager_module._verified_window_binding(socket, "window-a-instance", "window-a")
        assert socket.closed[-1]["code"] == 1008
    assert str(requests[0].url).endswith("/v1/window-binding")
    assert requests[0].headers["authorization"] == "Bearer fixture-broker-token"
    assert json.loads(requests[0].content) == {"window_instance_id": "window-a-instance", "window_owner_id": "window-a", "window_binding_token": "a" * 64}


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [False, True])
async def test_other_window_or_headless_cannot_take_over_an_existing_desktop_session(tmp_path, monkeypatch, headless):
    websocket = Socket(instance="" if headless else "window-a-instance", owner="" if headless else "window-a",
                       proof="" if headless else "a" * 64)
    manager = manager_module.WebSocketManager()
    original_socket = SimpleNamespace()
    original = SimpleNamespace(window_instance_id="window-b-instance", window_owner_id="window-b", ws=original_socket, session_id="window-session")
    manager._sessions["window-session"] = original
    if not headless:
        async def verified(*args):
            return "a" * 64
        monkeypatch.setattr(manager_module, "_verified_window_binding", verified)
    with pytest.raises(WebSocketDisconnect):
        await manager.connect(websocket, llm=None, artifact_store=None, tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path), config=AppConfig(llm=LLMSettings(api_key="")), mcp_manager=None)
    assert manager._sessions["window-session"] is original and original.ws is original_socket
    assert manager._connecting_websockets == {}
    assert websocket.closed[-1] == {"code": 1008, "reason": "session belongs to another window"}


def test_window_proof_is_host_only_and_is_excluded_from_prompt_snapshot_and_repr():
    proof = "fixture-private-window-proof"
    run_context = RunContext(window_binding_token=proof)
    context = ToolExecutionContext(permission=PermissionContext(), run_context=run_context,
        window_binding_token=run_context.window_binding_token,
        metadata={"window_instance_id": "window-a-instance", "window_owner_id": "window-a"})
    builder = ContextBuilder()
    builder.bind_tool_context(context)
    assert proof not in repr(run_context) and proof not in repr(context)
    assert proof not in json.dumps(context.metadata)
    assert proof not in json.dumps(builder.export_snapshot())
