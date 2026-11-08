from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.agent.message import UserCommand
from backend.agent.code_execution import CodeExecutionRuntime
from backend.agent.code_execution_store import CodeExecutionStore
from backend.agent.elicitation import ElicitationService
from backend.llm.base import ToolCallEvent
from backend.tests.test_code_cell_thread_lifetime import setup, query_turn
from backend.tests.test_code_execution import FixtureTool, ScriptModel, run_model
from backend.tests.test_conversation_window_follower import windows
from backend.ws.handlers.elicitation import handle_counter


@pytest.mark.asyncio
async def test_actual_code_output_waits_for_all_external_interactions_before_model_delivery(tmp_path, monkeypatch):
    _, model, session, runtime = setup(tmp_path)
    service = session.code_store.elicitations
    service.increment()
    service.increment()
    settled = asyncio.Event()
    run_cell = CodeExecutionRuntime._run
    async def observe(self, cell):
        try:
            return await run_cell(self, cell)
        finally:
            settled.set()
    monkeypatch.setattr(CodeExecutionRuntime, "_run", observe)
    task = asyncio.create_task(query_turn(tmp_path, model, session, runtime, ToolCallEvent("cell", "tool_exec",
        {"code": "text('Actual V8 result')", "yield_time_ms": 10000})))
    try:
        await asyncio.wait_for(settled.wait(), 5)
        assert model.step == 1 and not task.done()
        receipt, = session.code_store.receipts.values()
        assert receipt.output[0]["text"] == "Actual V8 result"
        service.decrement()
        await asyncio.sleep(0)
        assert not task.done()
        service.decrement()
        await asyncio.wait_for(task, 5)
        assert model.report["output"] == ["Actual V8 result"]
        assert receipt.output == []
    finally:
        service.reset_external()
        await asyncio.gather(task, return_exceptions=True)
        await session.aclose()
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_counter_targets_the_loaded_execution_owner_and_is_not_replayed(windows):
    manager, owner, follower, conversation, other, start, drain = windows
    start()
    follower.active_conversation_id = other.id
    store = owner.conversation_runtime.code_store_for(conversation.id)
    unrelated = follower.conversation_runtime.code_store_for(other.id)
    for index in range(2):
        command = UserCommand("thread/increment_elicitation", {"threadId": conversation.id, "client_command_id": f"increment-{index}"})
        await follower.command_dispatcher._dispatch_client_command(command, follower.connection_generation)
    await drain()
    receipts = [payload for payload in follower.ws.sent if payload["type"] == "command.result"]
    assert [payload["data"]["count"] for payload in receipts] == [1, 2]
    assert all(payload["conversation_id"] == conversation.id for payload in receipts)
    assert store.elicitations.paused and not unrelated.elicitations.paused
    assert not any(payload["type"] == "command.result" for payload in follower.event_outbox._events)
    await follower.command_dispatcher._dispatch_client_command(UserCommand("thread/decrement_elicitation", {"threadId": conversation.id}),
        follower.connection_generation)
    assert store.elicitations.external_count == 1
    await store.aclose()
    assert store.elicitations.external_count == 0
    await follower.command_dispatcher._dispatch_client_command(UserCommand("thread/decrement_elicitation", {"threadId": conversation.id}),
        follower.connection_generation)
    await drain()
    error = follower.ws.sent[-1]
    assert error["level"] == "error" and error["data"]["error_code"] == "elicitation_count_invalid"
    assert follower.active_conversation_id == other.id


@pytest.mark.asyncio
async def test_missing_or_closed_thread_cannot_create_an_implicit_counter(monkeypatch):
    events = []
    async def send(event):
        events.append(event)
    session = SimpleNamespace(ws_manager=None, send_event=send)
    monkeypatch.setattr("backend.ws.handlers.elicitation.default_runtime_if_initialized", lambda: None)
    await handle_counter(session, {"threadId": "not-loaded"}, increment=True)
    assert events[0].data["data"]["error_code"] == "thread_not_loaded"


@pytest.mark.asyncio
async def test_loaded_child_uses_its_own_session_counter(monkeypatch):
    child = SimpleNamespace(closed=False, code_store=CodeExecutionStore())
    runtime = SimpleNamespace(subagent_session=lambda identity: child if identity == "actual-child" else None)
    monkeypatch.setattr("backend.ws.handlers.elicitation.default_runtime_if_initialized", lambda: runtime)
    results = []
    async def emit(command, message, **kwargs):
        results.append(kwargs["data"])
    session = SimpleNamespace(ws_manager=None, emit_command_result=emit)
    await handle_counter(session, {"threadId": "actual-child"}, increment=True)
    assert results == [{"conversation_id": "actual-child", "count": 1, "paused": True}]
    assert child.code_store.elicitations.paused
    await child.code_store.aclose()
    assert not child.code_store.elicitations.paused


@pytest.mark.asyncio
async def test_canonical_approval_and_external_counter_share_the_code_delivery_gate(tmp_path, monkeypatch):
    services = []
    original = CodeExecutionRuntime.execute
    async def capture(self, *args, **kwargs):
        services.append(self.store.elicitations)
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(CodeExecutionRuntime, "execute", capture)
    entered, release = asyncio.Event(), asyncio.Event()
    async def approve(_call):
        assert services[0].paused
        entered.set()
        await release.wait()
        return {"action": "approve"}
    writer = FixtureTool("write_marker", effect=True)
    model = ScriptModel("const result=await tools.write_marker({});text(result.content);", yield_time_ms=10000)
    task = asyncio.create_task(run_model(tmp_path, model, [writer], mode="confirm", approval=approve))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        service = services[0]
        assert service.external_count == 0 and model.calls == 1
        service.increment()
        release.set()
        await asyncio.wait_for(writer.finished.wait(), 5)
        assert not task.done() and model.calls == 1
        service.decrement()
        state, *_ = await asyncio.wait_for(task, 5)
        assert state.terminal_status == "completed"
        assert (tmp_path / "marker.txt").read_text() == "one write"
        assert not service.paused
    finally:
        release.set()
        for service in services:
            service.reset_external()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_counter_commands_bypass_busy_dispatch_and_never_enter_the_durable_queue(windows, monkeypatch):
    manager, owner, follower, conversation, _, start, drain = windows
    start()
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    follower.ws.receive_text = incoming.get
    original = follower.ws.send_json
    async def send(payload):
        await original(payload)
        outgoing.put_nowait(payload)
    follower.ws.send_json = send
    follower.command_dispatcher._command_semaphore = asyncio.Semaphore(0)
    follower.command_dispatcher._max_command_tasks = 0
    run = asyncio.create_task(follower.command_dispatcher.run(follower.connection_generation))
    async def request(kind, identity):
        await incoming.put(json.dumps({"type": kind, "threadId": conversation.id, "client_command_id": identity}))
        while True:
            payload = await asyncio.wait_for(outgoing.get(), 3)
            if payload["type"] == "command.result" and payload.get("client_command_id") == identity:
                return payload
    try:
        first = await request("thread/increment_elicitation", "counter-up")
        assert first["data"]["count"] == 1 and first["data"]["paused"]
        second = await request("thread/decrement_elicitation", "counter-down")
        assert second["data"]["count"] == 0 and not second["data"]["paused"]
        assert follower.run_manager.durable_client_commands.pending_client_commands() == []
        assert not follower.run_manager.durable_client_commands.has_client_command("counter-up")
        assert not owner.conversation_runtime.code_store_for(conversation.id).elicitations.paused
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await asyncio.gather(*tuple(follower.command_dispatcher.command_tasks), return_exceptions=True)
        await drain()
