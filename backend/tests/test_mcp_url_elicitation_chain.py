from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mcp import types

from backend.agent.message import UserCommand
from backend.bootstrap.app import AppBootstrap
from backend.mcp.client import MCPClient
from backend.tests.test_mcp_client_owner_routing import _OAuthControlSession


class MCPControlSession(_OAuthControlSession):
    async def send_payload(self, payload: dict, *, log_context: str) -> bool:
        if log_context == "mcp:elicitation":
            self.sent.append(dict(payload))
            return self.delivered
        return await super().send_payload(payload, log_context=log_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
async def test_url_sdk_producer_owned_replay_and_response_roundtrip(monkeypatch, action):
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    session = MCPControlSession()
    owner = {"session_id": "session-a", "conversation_id": "conv_oauth_a", "message_id": "message-mcp", "turn_id": "turn-mcp", "task_id": "child-worker"}
    bootstrap = object.__new__(AppBootstrap)
    bootstrap._resolve_mcp_request_session = lambda params: (session, params["_minicode_owner"])
    client = MCPClient("source-server", elicitation_handler=bootstrap._handle_mcp_elicitation)
    client._active_tool_request_owners = {1: owner}
    request = types.ElicitRequestURLParams(mode="url", message="Complete the action on the linked page", url="https://auth.example.test/continue?flow=owned", elicitationId="server-flow", _meta={"source": "original"})
    task = asyncio.create_task(client._sdk_elicitation_callback(SimpleNamespace(request_id=8), request))
    for _ in range(40):
        if session.turn_wait_state.pending_elicitations:
            break
        await asyncio.sleep(0)
    public_id = next(iter(session.turn_wait_state.pending_elicitations))
    payload = session.turn_wait_state.pending_approval_payloads[public_id]
    assert payload["request"] == {
        "subtype": "elicitation", "tool_use_id": public_id,
        "prompt": request.message, "question": request.message,
        "mode": "url", "mcp_server_name": "source-server", "url": request.url,
        "elicitation_id": "server-flow", "_meta": {"source": "original"},
    }
    assert {key: payload[key] for key in ("conversation_id", "message_id", "turn_id", "task_id")} == {key: owner[key] for key in ("conversation_id", "message_id", "turn_id", "task_id")}
    assert public_id != "8" and public_id != "server-flow"
    await client._sdk_message_handler(types.ServerNotification(root=types.ElicitCompleteNotification(
        params=types.ElicitCompleteNotificationParams(elicitationId="server-flow"),
    )))
    assert not task.done()
    await session.reemit_pending_state(owner["conversation_id"])
    assert [value["request"] for value in session.sent if value.get("request_id") == public_id] == [payload["request"], payload["request"]]
    await session.command_dispatcher._handle_control_response(UserCommand(type="control_response", data={
        "request_id": public_id, "conversation_id": "wrong-owner", "message_id": owner["message_id"], "turn_id": owner["turn_id"],
        "response": {"subtype": "success", "response": {"action": action}},
    }))
    assert not task.done()
    await session.command_dispatcher._handle_control_response(UserCommand(type="control_response", data={
        "request_id": public_id, "conversation_id": owner["conversation_id"], "message_id": owner["message_id"], "turn_id": owner["turn_id"],
        "response": {"subtype": "success", "response": {"action": action, "content": {"not_url_input": "discard"}, "_meta": {"reply": "kept"}}},
    }))
    result = await asyncio.wait_for(task, 1)
    assert isinstance(result, types.ElicitResult)
    assert result.action == action
    assert result.content is None
    assert result.meta == {"reply": "kept"}
    assert session.turn_wait_state.pending_elicitations == {}
    assert session.turn_wait_state.pending_approval_payloads == {}


@pytest.mark.asyncio
async def test_url_no_owner_does_not_emit_or_accept(monkeypatch):
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    emitted = []
    async def handler(payload):
        emitted.append(payload)
        return {"action": "accept"}
    client = MCPClient("source-server", elicitation_handler=handler)
    result = await client._sdk_elicitation_callback(SimpleNamespace(request_id=8), types.ElicitRequestURLParams(mode="url", message="Open page", url="https://auth.example.test/", elicitationId="server-flow"))
    assert isinstance(result, types.ErrorData)
    assert result.code == -32002
    assert emitted == []


@pytest.mark.asyncio
async def test_url_owner_cancel_cleans_pending_prompt_without_accept(monkeypatch):
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    session = MCPControlSession()
    cancel = asyncio.Event()
    owner = {"session_id": "session-a", "conversation_id": "conv_oauth_a", "cancel_event": cancel}
    bootstrap = object.__new__(AppBootstrap)
    bootstrap._resolve_mcp_request_session = lambda params: (session, params["_minicode_owner"])
    client = MCPClient("source-server", elicitation_handler=bootstrap._handle_mcp_elicitation)
    client._active_tool_request_owners = {1: owner}
    task = asyncio.create_task(client._sdk_elicitation_callback(SimpleNamespace(request_id=8), types.ElicitRequestURLParams(mode="url", message="Open page", url="https://auth.example.test/", elicitationId="server-flow")))
    for _ in range(40):
        if session.turn_wait_state.pending_elicitations:
            break
        await asyncio.sleep(0)
    assert session.turn_wait_state.pending_elicitations
    cancel.set()
    result = await asyncio.wait_for(task, 1)
    assert result.action == "cancel"
    assert result.content is None
    assert session.turn_wait_state.pending_elicitations == {}
    assert session.turn_wait_state.pending_approval_payloads == {}
    assert session.terminal[-1]["reason"] == "mcp_elicitation_owner_cancelled"


@pytest.mark.asyncio
async def test_url_empty_action_is_not_accepted():
    session = MCPControlSession()
    bootstrap = object.__new__(AppBootstrap)
    bootstrap._resolve_mcp_request_session = lambda _params: (session, {"conversation_id": "conv_oauth_a"})
    task = asyncio.create_task(bootstrap._handle_mcp_elicitation({"mode": "url", "prompt": "Open page", "url": "https://auth.example.test/", "elicitationId": "server-flow"}))
    for _ in range(40):
        if session.turn_wait_state.pending_elicitations:
            break
        await asyncio.sleep(0)
    request_id = next(iter(session.turn_wait_state.pending_elicitations))
    session.turn_wait_state.pending_elicitations[request_id].set_result({"content": {}})
    result = await task
    assert result["action"] == "cancel"
    assert "content" not in result
    assert "explicit action" in result["error"]
