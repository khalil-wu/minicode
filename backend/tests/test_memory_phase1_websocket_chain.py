"""Real Phase 1 HTTP/WS requests own transport independently of the main turn."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import socket
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from starlette.responses import Response, StreamingResponse

from backend.config import LLMSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.cost_tracker import CostTracker
from backend.llm.openai_adapter import OpenAIAdapter, ResponsesStreamError
from backend.memory.file_memory import FileMemory
from backend.memory.generation import MemoryGenerationCoordinator, MemoryStartupSource, schedule_memory_startup
from backend.memory.job_store import MEMORY_DB_NAME, MemoryJobStore
from backend.tests.guardian_http_fixture import until
from backend.tests.test_internal_request_provenance_chain import foreground_scene, response_events
from backend.tests.test_memory_phase1_source_request_chain import (
    conversation, observe_side_calls, only_phase1, phase1_result, response_body,
)


def is_memory(headers):
    return json.loads(headers.get("x-codex-turn-metadata", "{}")).get("request_kind") == "memory"


@asynccontextmanager
async def responses_provider(respond, *, reject_memory=0):
    """Serve real HTTP and WS at the same URL, including upgrade rejection."""
    app = FastAPI()
    records, handshakes, connections = [], [], []

    def record(transport, headers, body, connection=None):
        item = SimpleNamespace(transport=transport, headers=dict(headers), body=body,
            connection=connection, index=len(records) + 1)
        records.append(item)
        return item

    @app.post("/v1/responses")
    async def http(request: Request):
        item = record("http", request.headers, await request.json())
        async def stream():
            async for event in respond(item):
                yield "data: " + json.dumps(event) + "\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket("/v1/responses")
    async def websocket(ws: WebSocket):
        headers = dict(ws.headers)
        status = reject_memory if is_memory(headers) else 0
        handshakes.append({"headers": headers, "status": status or 101})
        if status:
            await ws.send_denial_response(Response(status_code=status))
            return
        await ws.accept()
        connection = SimpleNamespace(socket=ws, closed=False)
        connections.append(connection)
        try:
            while True:
                body = await ws.receive_json()
                item = record("websocket", headers, body, connection)
                async for event in respond(item):
                    await ws.send_json(event)
                if connection.closed:
                    return
        except WebSocketDisconnect:
            pass
        finally:
            connection.closed = True

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, loop="asyncio", ws="websockets",
        lifespan="off", log_config=None, access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async def ready():
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("Actual HTTP/WS provider exited before listening")
                await asyncio.sleep(.001)
        await asyncio.wait_for(ready(), 10)
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}/v1", records=records,
            handshakes=handshakes, connections=connections)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


def adapter_for(server, *, enabled=True):
    return OpenAIAdapter(LLMSettings(api_key="local-phase1-ws-key", provider="custom",
        model="phase1-fixture", base_url=server.url, wire_api="responses",
        proxy_mode="direct", responses_websocket=enabled))


def outputs(workspace):
    store = MemoryJobStore(FileMemory.for_workspace(workspace).memory_dir / MEMORY_DB_NAME)
    return store.list_stage1_outputs(limit=None, max_unused_days=30)


def extraction_events(item):
    prompt = str(item.body["input"])
    label = next(label for label in ("ROLLOUT_B_FACT", "ROLLOUT_C_FACT") if label in prompt)
    return response_body(phase1_result(label), f"phase1-{item.index}")


def assert_source(item, startup_id):
    client = item.body["client_metadata"]
    assert client["session_id"] == client["thread_id"] == startup_id
    assert UUID(client["turn_id"]).version == 7
    assert client["root_turn_id"] == client["turn_id"]
    assert client["x-codex-window-id"] == item.headers["x-codex-window-id"] == f"{startup_id}:0"
    assert client["x-codex-turn-metadata"] == item.headers["x-codex-turn-metadata"]
    assert json.loads(client["x-codex-turn-metadata"]) == {
        "turn_id": client["turn_id"], "root_turn_id": client["turn_id"],
        "request_kind": "memory", "thread_source": "memory_consolidation",
        "turn_trigger": "memory_consolidation", "sandbox_mode": "workspace-write",
    }
    assert not {"x-openai-memgen-request", "x-codex-guardian", "x-openai-subagent"} & item.headers.keys()
    assert "previous_response_id" not in item.body


@pytest.mark.asyncio
async def test_actual_startup_phase1_ws_preserves_idle_main_history_identity_and_accounting(tmp_path, monkeypatch):
    only_phase1(monkeypatch)
    repository = ConversationRepository(tmp_path / "conversations")
    startup = conversation(repository, tmp_path, "FOREGROUND_ACTUAL_INPUT")
    previous = [conversation(repository, tmp_path, name) for name in ("ROLLOUT_B_FACT", "ROLLOUT_C_FACT")]
    tracker = CostTracker()
    monkeypatch.setattr(CostTracker, "_instance", tracker)

    async def respond(item):
        for event in extraction_events(item) if is_memory(item.headers) else response_events(item.index):
            yield event

    async with responses_provider(respond) as server:
        adapter = adapter_for(server)
        calls = observe_side_calls(monkeypatch, adapter)
        try:
            async with foreground_scene(tmp_path, adapter, conversation_id=startup.id) as foreground:
                before = deepcopy(foreground.builder.export_snapshot())
                admitted, = [message for message in foreground.builder._history if message.is_user_input]
                native_input, = [item for item in server.records[0].body["input"]
                    if item.get("id") == admitted.accepted_input["model_item_id"]]
                assert native_input["content"] == [{"type": "input_text", "text": "FOREGROUND_ACTUAL_INPUT"}]
                assert native_input["internal_chat_message_metadata_passthrough"] == {
                    "turn_id": admitted.accepted_input["turn_id"], "content_item_kinds": ["user.text"],
                }
                records = list(foreground.owner.llm_turn_context.side_call_records)
                idle = adapter._responses_websocket.idle
                assert idle is not None
                baseline, response_id = deepcopy(idle.baseline), idle.response_id
                task = schedule_memory_startup(repository=repository, llm=adapter, workspace_root=tmp_path,
                    current_conversation_id=startup.id, startup_sandbox_mode="workspace-write")
                await asyncio.wait_for(task, 10)
                assert foreground.builder.export_snapshot() == before
                assert foreground.owner.llm_turn_context.side_call_records == records
                assert adapter._responses_websocket.idle is idle
                assert idle.baseline == baseline and idle.response_id == response_id
                assert not adapter._responses_websocket.active and not adapter._responses_side_pools
                assert not server.connections[0].closed
                assert (tracker.get_summary("foreground-session")["input_tokens"],
                    tracker.get_summary("foreground-session")["output_tokens"]) == (7, 3)
                assert (tracker.get_summary()["input_tokens"], tracker.get_summary()["output_tokens"]) == (21, 9)
            memory = [item for item in server.records if is_memory(item.headers)]
            assert len(memory) == len(calls) == 2
            assert all(item.transport == "websocket" for item in memory)
            assert len({id(item.connection) for item in memory}) == 2
            assert len({item.body["client_metadata"]["turn_id"] for item in memory}) == 2
            for item in memory:
                assert_source(item, startup.id)
            assert all(call.turn is None and call.record["transport"]["mode"] == "websocket" for call in calls)
            assert all(call.record["status"] == "completed" and call.record["usage"]["input_tokens"] == 7 for call in calls)
            await until(lambda: all(item.connection.closed for item in memory))
            actual = outputs(tmp_path)
            assert {item.thread_id for item in actual} == {item.id for item in previous}
            assert {item.raw_memory for item in actual} == {"Remembered ROLLOUT_B_FACT", "Remembered ROLLOUT_C_FACT"}
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,reject", [(False, 0), (True, 405)])
async def test_phase1_disabled_or_unsupported_ws_uses_actual_http_without_changing_main_pool(tmp_path, monkeypatch, enabled, reject):
    only_phase1(monkeypatch)
    repository = ConversationRepository(tmp_path / "conversations")
    startup = conversation(repository, tmp_path, "startup-A")
    old = conversation(repository, tmp_path, "ROLLOUT_B_FACT")
    async def respond(item):
        for event in extraction_events(item):
            yield event
    async with responses_provider(respond, reject_memory=reject) as server:
        adapter = adapter_for(server, enabled=enabled)
        calls = observe_side_calls(monkeypatch, adapter)
        try:
            await MemoryGenerationCoordinator(repository=repository, llm=adapter, workspace_root=tmp_path).run_startup(
                current_conversation_id=startup.id, startup_sandbox_mode="workspace-write")
            assert len(server.records) == 1 and server.records[0].transport == "http"
            assert_source(server.records[0], startup.id)
            assert len(server.handshakes) == int(enabled)
            assert not adapter._responses_websocket.http_only
            assert adapter._responses_websocket.unavailable_status is None
            assert adapter._responses_websocket.idle is None and not adapter._responses_websocket.active
            assert not adapter._responses_side_pools
            assert outputs(tmp_path)[0].thread_id == old.id
            if enabled:
                assert calls[0].record["transport"]["fallback_status"] == 405
                assert calls[0].record["transport"]["mode"] == "http"
                assert server.handshakes[0]["headers"]["x-codex-turn-metadata"] == server.records[0].headers["x-codex-turn-metadata"]
            else:
                assert "transport" not in calls[0].record
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
async def test_phase1_does_not_inherit_main_session_http_fallback(tmp_path, monkeypatch):
    only_phase1(monkeypatch)
    repository = ConversationRepository(tmp_path / "conversations")
    startup = conversation(repository, tmp_path, "startup-A")
    conversation(repository, tmp_path, "ROLLOUT_B_FACT")
    async def respond(item):
        for event in extraction_events(item):
            yield event
    async with responses_provider(respond) as server:
        adapter = adapter_for(server)
        try:
            assert adapter.try_fallback_transport()
            await MemoryGenerationCoordinator(repository=repository, llm=adapter, workspace_root=tmp_path).run_startup(
                current_conversation_id=startup.id, startup_sandbox_mode="workspace-write")
            assert len(server.records) == 1 and server.records[0].transport == "websocket"
            assert adapter._responses_websocket.fallback_to_http
            assert not adapter._responses_side_pools
            assert_source(server.records[0], startup.id)
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
async def test_concurrent_phase1_requests_leave_active_main_lease_and_continuation_intact(tmp_path, monkeypatch):
    only_phase1(monkeypatch)
    repository = ConversationRepository(tmp_path / "conversations")
    startup = conversation(repository, tmp_path, "startup-A")
    for name in ("ROLLOUT_B_FACT", "ROLLOUT_C_FACT"):
        conversation(repository, tmp_path, name)
    main_started, memory_started, release_main, release_memory = (asyncio.Event() for _ in range(4))
    memory_seen = []
    async def respond(item):
        if is_memory(item.headers):
            memory_seen.append(item)
            if len(memory_seen) == 2:
                memory_started.set()
            await release_memory.wait()
            events = extraction_events(item)
        else:
            main_started.set()
            await release_main.wait()
            events = response_body("MAIN_ACTIVE_DONE", f"main-{item.index}")
        for event in events:
            yield event
    async with responses_provider(respond) as server:
        adapter = adapter_for(server)
        metadata = {"session_id": "main-session", "thread_id": "main-thread", "turn_id": "main-turn"}
        messages = [LLMMessage(role="user", content="MAIN_ACTIVE_INPUT")]
        async def main_call(inputs):
            return [event async for event in adapter.stream_chat(inputs, metadata=metadata)]
        main_task = asyncio.create_task(main_call(messages))
        memory_task = None
        try:
            await asyncio.wait_for(main_started.wait(), 5)
            main_lease, = adapter._responses_websocket.active
            original_connection = main_lease.connection
            memory_task = schedule_memory_startup(repository=repository, llm=adapter, workspace_root=tmp_path,
                current_conversation_id=startup.id, startup_sandbox_mode="workspace-write")
            await asyncio.wait_for(memory_started.wait(), 5)
            assert len(adapter._responses_side_pools) == 2
            assert adapter._responses_websocket.active == {main_lease}
            assert main_lease.connection is original_connection
            release_memory.set()
            await asyncio.wait_for(memory_task, 10)
            assert not adapter._responses_side_pools and not main_task.done()
            assert main_lease.connection is original_connection
            assert_source(memory_seen[0], startup.id)
            assert_source(memory_seen[1], startup.id)
            release_main.set()
            events = await asyncio.wait_for(main_task, 5)
            assert events[-1].type == StreamEventType.DONE
            assert adapter._responses_websocket.idle is original_connection
            await main_call([*messages, LLMMessage(role="assistant", content="MAIN_ACTIVE_DONE"),
                LLMMessage(role="user", content="CONTINUE_MAIN")])
            main_records = [item for item in server.records if not is_memory(item.headers)]
            assert len(main_records) == 2
            assert main_records[0].connection is main_records[1].connection
            assert main_records[1].body["previous_response_id"] == f"main-{main_records[0].index}"
            assert main_records[1].body["input"] == [{"role": "user", "content": "CONTINUE_MAIN"}]
        finally:
            release_main.set()
            release_memory.set()
            for task in (main_task, memory_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (main_task, memory_task) if task is not None), return_exceptions=True)
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "adapter_close", "provider_failure"])
async def test_phase1_partial_usage_and_actual_socket_owner_close_on_every_terminal(tmp_path, monkeypatch, stop):
    repository = ConversationRepository(tmp_path / "conversations")
    startup = conversation(repository, tmp_path, "startup-A")
    old = conversation(repository, tmp_path, "ROLLOUT_B_FACT")
    entered = asyncio.Event()
    tracker = CostTracker()
    monkeypatch.setattr(CostTracker, "_instance", tracker)
    async def respond(item):
        usage = {"input_tokens": 4, "output_tokens": 2}
        yield {"type": "response.created", "response": {"id": "partial-phase1", "usage": usage}}
        yield {"type": "response.output_text.delta", "delta": "unfinished"}
        entered.set()
        if stop == "provider_failure":
            yield {"type": "response.failed", "response": {"id": "partial-phase1", "status": "failed",
                "usage": usage, "error": {"code": "invalid_request_error", "message": "controlled phase1 rejection"}}}
        else:
            closed = await item.connection.socket.receive()
            assert closed["type"] == "websocket.disconnect"
            item.connection.closed = True
    async with responses_provider(respond) as server:
        adapter = adapter_for(server)
        calls = observe_side_calls(monkeypatch, adapter)
        coordinator = MemoryGenerationCoordinator(repository=repository, llm=adapter, workspace_root=tmp_path)
        task = asyncio.create_task(coordinator._extract_phase1(old, "ROLLOUT_B_FACT",
            startup_source=MemoryStartupSource(startup.id, "workspace-write")))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if stop == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            elif stop == "adapter_close":
                await adapter.aclose()
                with pytest.raises((httpx.ReadError, RuntimeError)):
                    await asyncio.wait_for(task, 5)
            else:
                with pytest.raises(ResponsesStreamError):
                    await asyncio.wait_for(task, 5)
            assert len(server.records) == 1
            assert_source(server.records[0], startup.id)
            await until(lambda: server.records[0].connection.closed)
            assert not adapter._responses_side_pools and not adapter._responses_websocket.active
            assert adapter._responses_websocket.idle is None
            assert len({id(call) for call in calls}) == 1 and calls[0].turn is None
            assert calls[0].record["usage"]["input_tokens"] == 4
            assert calls[0].record["usage"]["output_tokens"] == 2
            assert calls[0].record["status"] == ("cancelled" if stop == "cancel" else "failed")
            assert (tracker.get_summary()["input_tokens"], tracker.get_summary()["output_tokens"]) == (4, 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_phase1_auth_upgrade_rejection_keeps_original_error_and_never_generates_http(tmp_path, monkeypatch, status):
    repository = ConversationRepository(tmp_path / "conversations")
    startup = conversation(repository, tmp_path, "startup-A")
    old = conversation(repository, tmp_path, "ROLLOUT_B_FACT")
    async def respond(item):
        pytest.fail("Rejected upgrade must not generate a model request")
        yield {}
    async with responses_provider(respond, reject_memory=status) as server:
        adapter = adapter_for(server)
        try:
            coordinator = MemoryGenerationCoordinator(repository=repository, llm=adapter, workspace_root=tmp_path)
            with pytest.raises(httpx.HTTPStatusError) as failure:
                await coordinator._extract_phase1(old, "ROLLOUT_B_FACT",
                    startup_source=MemoryStartupSource(startup.id, "workspace-write"))
            assert failure.value.response.status_code == status
            assert len(server.handshakes) == 1 and server.records == []
            assert not adapter._responses_websocket.http_only and not adapter._responses_side_pools
        finally:
            await adapter.aclose()
