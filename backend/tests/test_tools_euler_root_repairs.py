"""Finite verification for preview ownership and CDP navigation results."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.browser_control_tool import BrowserControlTool
from backend.tools.preview_tool import PreviewServerTool
from backend.tools.registry import ToolRegistry


def context(root=None):
    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass", conversation_id="euler-owner-A"),
        session_id="euler-session-A", conversation_id="euler-owner-A", workspace_root=root,
    )


@pytest.mark.parametrize("root", [None, ""])
def test_context_workspace_is_authoritative_even_when_missing(root):
    tool = PreviewServerTool("C:/Workspace/B")
    assert tool._workspace(context(root)) is None
    assert tool._workspace(None) == "C:/Workspace/B"


def test_registry_start_rejects_missing_turn_workspace_without_launching(monkeypatch):
    calls = []

    async def launch(root, **kwargs):
        calls.append((root, kwargs))

    monkeypatch.setattr("backend.preview.launcher.start_preview_launch", launch)
    registry = ToolRegistry()
    registry.register(PreviewServerTool("C:/Workspace/B"))
    result = asyncio.run(registry.execute("preview_server", {"action": "start"}, context=context()))
    assert result.is_error
    assert "requires an open workspace" in result.content
    assert calls == []


def test_registry_start_uses_the_exact_turn_workspace(monkeypatch):
    calls = []

    async def launch(root, **kwargs):
        calls.append((root, kwargs))
        return SimpleNamespace(effective_url="", effective_port=None, status="starting", stderr_tail=[],
                               process=SimpleNamespace(returncode=None, pid=101))

    monkeypatch.setattr("backend.preview.launcher.start_preview_launch", launch)
    registry = ToolRegistry()
    registry.register(PreviewServerTool("C:/Workspace/B"))
    owner = context(Path("C:/Workspace/A"))
    result = asyncio.run(registry.execute("preview_server", {"action": "start"}, context=owner))
    assert not result.is_error
    assert calls[0][0] == str(owner.workspace_root)
    assert calls[0][1]["session_id"] == owner.session_id
    assert calls[0][1]["conversation_id"] == owner.conversation_id


@pytest.mark.parametrize("action", ["status", "stop", "verify"])
def test_other_preview_consumers_keep_exact_owner_without_borrowing_registry_root(monkeypatch, action):
    calls = []

    def running(**kwargs):
        calls.append(kwargs)
        return []

    async def stop(name=None, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("backend.preview.launcher.running_preview_processes", running)
    monkeypatch.setattr("backend.preview.launcher.stop_preview_launch", stop)
    registry = ToolRegistry()
    registry.register(PreviewServerTool("C:/Workspace/B"))
    asyncio.run(registry.execute("preview_server", {"action": action}, context=context()))
    assert calls == [{"session_id": "euler-session-A", "conversation_id": "euler-owner-A", "workspace_root": None}]


def test_explicit_private_verify_cannot_find_a_registry_root_preview(monkeypatch):
    calls = []

    def find(url, **kwargs):
        calls.append((url, kwargs))
        return None

    monkeypatch.setattr("backend.preview.launcher.find_preview_process", find)
    result = asyncio.run(PreviewServerTool("C:/Workspace/B").execute(
        {"action": "verify", "url": "http://127.0.0.1:9876/"}, context=context(),
    ))
    assert result.is_error
    assert calls[0][1] == {"session_id": "euler-session-A", "conversation_id": "euler-owner-A", "workspace_root": None}


def test_navigation_error_survives_real_http_and_cdp_wire():
    async def verify():
        wire_calls = []
        wire_closed = asyncio.Event()

        async def cdp(ws, *unused):
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    wire_calls.append(message["method"])
                    result = {"errorText": "net::ERR_CONNECTION_REFUSED"} if message["method"] == "Page.navigate" else {}
                    await ws.send(json.dumps({"id": message["id"], "result": result}))
            finally:
                wire_closed.set()

        async with websockets.serve(cdp, "127.0.0.1", 0) as ws_server:
            ws_port = ws_server.sockets[0].getsockname()[1]
            targets = [{"id": "fixture-page", "type": "page", "title": "Fixture", "url": "about:blank",
                        "webSocketDebuggerUrl": f"ws://127.0.0.1:{ws_port}/page"}]

            async def http(reader, writer):
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), .5)
                body = json.dumps(targets).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(http, "127.0.0.1", 0)
            async with server:
                port = server.sockets[0].getsockname()[1]
                owner = context()
                # One explicit fixture-owned preview origin admits only this
                # deterministic navigation intent; there is no real browser.
                owner.metadata["preview_origin"] = "http://127.0.0.1:9876"
                registry = ToolRegistry()
                registry.register(BrowserControlTool())
                result = await asyncio.wait_for(registry.execute("browser_control", {
                    "action": "navigate", "url": "http://127.0.0.1:9876/",
                    "cdp_endpoint": f"http://127.0.0.1:{port}", "target_id": "fixture-page", "wait_ms": 5000,
                }, context=owner), 2)
                await asyncio.wait_for(wire_closed.wait(), .5)
            assert result.is_error
            assert result.content == "Error: net::ERR_CONNECTION_REFUSED"
            assert result.display_summary != "Browser navigated"
            assert wire_calls == ["Page.enable", "Page.navigate"]

    asyncio.run(verify())
