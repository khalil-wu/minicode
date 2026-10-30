"""Actual network consumer, session projection and TCP WebSocket decisions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import socket
from types import SimpleNamespace

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import pytest
import pytest_asyncio
import uvicorn
from websockets.asyncio.client import connect

from backend.agent.message import AgentEvent
from backend.artifact.store import ArtifactStore
from backend.tests.test_network_approval import network_host, native_request, register
from backend.ws.handler import WebSocketSession
from backend.ws.manager import WebSocketManager


@pytest_asyncio.fixture
async def network_socket(network_host, tmp_path):
    host = network_host
    manager = WebSocketManager()
    app = FastAPI()
    ready = asyncio.Event()
    holder = SimpleNamespace(host=host, upstream_deny=False, captured=[])

    @app.websocket("/network-review")
    async def owned_socket(websocket: WebSocket):
        await websocket.accept()
        owner = WebSocketSession("network-wire-session", websocket, None,
            ArtifactStore(storage_dir=tmp_path / "wire-artifacts"), host.registry, host.checker,
            host.snapshot.config, ws_manager=manager)
        holder.session = owner
        conversation = owner.conversation_repo.create_conversation(
            title="Real network review", workspace_root=str(host.workspace), permission_mode="confirm")
        owner.active_conversation_id = conversation.id
        owner.session_lifecycle.workspace_root = host.workspace
        manager._sessions[owner.session_id] = owner

        async def emit(kind, data):
            if kind == "approval_request" and holder.upstream_deny:
                # A real upstream may explicitly advertise this variant; the
                # default Source producer advertises only the allow amendment.
                data["available_decisions"].insert(-1, {"network_policy_amendment": {
                    "network_policy_amendment": {"host": data["network_approval_context"]["host"], "action": "deny"}}})
            holder.captured.append((kind, data))
            await owner.send_event(AgentEvent(type=kind, data=data))

        host.context = replace(host.context, conversation_id=conversation.id,
            permission=replace(host.context.permission, conversation_id=conversation.id),
            session_id=owner.session_id, emit_event=emit,
            approval_handler=owner.approval_handler,
            metadata={**host.context.metadata, "assistant_message_id": "actual-review-message"})
        ready.set()
        try:
            await owner.command_dispatcher.run(owner.connection_generation)
        except WebSocketDisconnect:
            pass
        finally:
            await owner.session_lifecycle.shutdown(reason="network-wire-test")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                await asyncio.sleep(0.01)
        async with connect(f"ws://127.0.0.1:{port}/network-review") as websocket:
            await asyncio.wait_for(ready.wait(), 10)
            holder.websocket = websocket
            yield holder
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, 15)
        await manager.shutdown(reason="network-wire-fixture")


async def receive(server, kind):
    async with asyncio.timeout(10):
        while True:
            value = json.loads(await server.websocket.recv())
            if value["type"] == kind:
                return value


async def start_review(server, *, protocol="http", persist=None):
    registration = await register(server.host, persist=persist)
    result = asyncio.create_task(registration.decide(native_request(registration, protocol=protocol)))
    wire = await receive(server, "control_request")
    assert wire["request_id"] == wire["request"]["tool_use_id"]
    assert wire["request_id"].startswith("network#local#")
    assert wire["request_id"] != registration.request.tool_call_id
    assert wire["request"]["request_digest"] != registration.request.digest
    assert wire["conversation_id"] == server.host.context.conversation_id
    assert wire["turn_id"] == server.host.context.turn_id
    assert wire["message_id"] == "actual-review-message"
    assert wire["request"]["network_approval_context"]["host"] == "Example.COM"
    assert wire["request"]["proposed_network_policy_amendments"] == [
        {"host": "Example.COM", "action": "allow"}, {"host": "Example.COM", "action": "deny"}]
    return registration, result, wire


def response(wire, decision, *, action="approve"):
    return {"type": "control_response", "request_id": wire["request_id"],
        **{key: wire[key] for key in ("conversation_id", "turn_id", "message_id")},
        "response": {"subtype": "success", "response": {"action": action, "decision": decision,
            "request_digest": wire["request"]["request_digest"]}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["approved", "approved_for_session", "allow", "abort", "deny"])
async def test_actual_consumer_roundtrips_exact_network_decision(network_socket, choice, record_testsuite_property):
    server = network_socket
    server.upstream_deny = choice == "deny"
    writes = []
    async def persist(**fields):
        writes.append(fields)
        fields["on_policy_applied"]()
    registration, pending, wire = await start_review(server, persist=persist)
    record_testsuite_property(f"network_control_request_{choice}", json.dumps(wire, ensure_ascii=False))
    available = wire["request"]["available_decisions"]
    assert any(isinstance(item, dict) and item["network_policy_amendment"]["network_policy_amendment"]["action"] == "deny"
        for item in available) is (choice == "deny")
    decision = ({"network_policy_amendment": {"network_policy_amendment": {"host": "Example.COM", "action": choice}}}
        if choice in {"allow", "deny"} else choice)
    await server.websocket.send(json.dumps(response(wire, decision, action="reject" if choice in {"abort", "deny"} else "approve")))
    ack = await receive(server, "command.result")
    assert ack["level"] == "success", ack
    result = await asyncio.wait_for(pending, 10)
    assert result["type"] == ("deny" if choice in {"abort", "deny"} else "allow")
    assert bool(writes) is (choice in {"allow", "deny"})
    if writes:
        assert (writes[0]["host"], writes[0]["protocol"], writes[0]["action"]) == ("Example.COM", "http", choice)
    assert len(server.host.service._session_approved) == (1 if choice in {"allow", "approved_for_session"} else 0)
    assert await registration.finish() == ("rejected by user" if choice in {"abort", "deny"} else None)
    assert wire["request_id"] not in server.session.turn_wait_state.pending_approval_payloads
    await server.websocket.send(json.dumps(response(wire, "approved")))
    duplicate = await receive(server, "command.result")
    assert duplicate["level"] == "error" and "stale" in duplicate["message"]


@pytest.mark.asyncio
async def test_invalid_decisions_do_not_settle_or_rebind_actual_pending_review(network_socket):
    server = network_socket
    registration, pending, wire = await start_review(server)
    other = server.session.conversation_repo.create_conversation(title="Another actual conversation", workspace_root=str(server.host.workspace))
    patches = [{"conversation_id": other.id}, {"turn_id": "other"}, {"message_id": "other"}]
    commands = [{**response(wire, "approved"), **patch} for patch in patches]
    for patch in ({"request_digest": "b" * 64}, {"decision": None}, {"action": "reject"}, {"remember_for_session": True},
        {"decision": {"network_policy_amendment": {"network_policy_amendment": {"host": "Example.COM", "action": "deny"}}}},
        {"decision": {"network_policy_amendment": {"network_policy_amendment": {"host": "other.test", "action": "allow"}}}}):
        command = response(wire, "approved")
        command["response"]["response"].update(patch)
        commands.append(command)
    for command in commands:
        await server.websocket.send(json.dumps(command))
        ack = await receive(server, "command.result")
        assert ack["level"] == "error", ack
        assert not pending.done()
        assert wire["request_id"] in server.session.turn_wait_state.pending_approval_payloads
    await server.websocket.send(json.dumps(response(wire, "abort", action="reject")))
    assert (await receive(server, "command.result"))["level"] == "success"
    assert (await asyncio.wait_for(pending, 10))["type"] == "deny"
    await registration.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["http", "https_connect", "socks5_tcp", "socks5_udp"])
async def test_actual_source_protocol_projection_and_steer_rejection(network_socket, protocol):
    server = network_socket
    registration, pending, wire = await start_review(server, protocol=protocol)
    expected = "https" if protocol == "https_connect" else protocol
    assert wire["request"]["network_approval_context"]["protocol"] == expected
    assert await server.session._reject_pending_approvals(reason="steer", guidance="actual new user instruction",
        conversation_id=wire["conversation_id"]) == [wire["request_id"]]
    assert (await asyncio.wait_for(pending, 10))["type"] == "deny"
    assert await registration.finish() == "actual new user instruction"
