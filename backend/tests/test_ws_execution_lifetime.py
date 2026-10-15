"""Source thread lifetime through actual scheduler, sessions, outboxes and tasks.

Only provider production and the unit test's 30s clock are controlled. Native
window availability and the unshortened close-over-grace path are desktop QA.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import anyio
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from backend.agent.message import AgentEvent, UserCommand
from backend.agent.runtime import default_runtime
from backend.tests.test_ws_cold_connection import _connection
from backend.ws.execution_context import ThreadUnloadingState
from backend.ws.manager import WebSocketManager


@asynccontextmanager
async def sessions(tmp_path, monkeypatch, *, unload_delay=60.0):
    manager = WebSocketManager(thread_unload_delay_seconds=unload_delay)
    owner, _ = await manager.connect(**_connection(tmp_path / "owner", "execution-owner"))
    reader, _ = await manager.connect(**_connection(tmp_path / "reader", "execution-reader"))
    controls = {}

    async def provider(content, *, conversation_id, metadata, run_context, **_kwargs):
        state = controls[conversation_id]
        state.context = run_context
        state.metadata = metadata
        metadata["_turn_admitted"] = True
        metadata["_turn_admission_future"].set_result(None)
        try:
            await state.release.wait()
        finally:
            owner.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])

    monkeypatch.setattr(owner, "_run_agent", provider)
    sleep = asyncio.sleep

    async def unit_grace(delay, *args, **kwargs):
        return await sleep(0 if delay == 30.0 else delay, *args, **kwargs)

    monkeypatch.setattr("backend.ws.manager.asyncio.sleep", unit_grace)

    async def launch(name, *, conversation=None):
        if conversation is None:
            conversation = owner.conversation_repo.create_conversation(conversation_id="conv_lifetime_" + name)
        state = SimpleNamespace(release=asyncio.Event(), conversation=conversation)
        controls[conversation.id] = state
        owner.active_conversation_id = conversation.id
        await owner.start_agent_run("real accepted input", conversation_id=conversation.id,
            metadata={"assistant_message_id": name + "-answer"})
        state.task = owner.run_manager.run_tasks[conversation.id]
        state.execution = manager.execution_contexts.context_for(conversation.id)
        return state

    async def expire(session):
        manager.disconnect(session.session_id, connection_generation=session.connection_generation)
        await manager._disconnect_tasks[session.session_id]

    try:
        yield SimpleNamespace(manager=manager, owner=owner, reader=reader, launch=launch, expire=expire, sleep=sleep)
    finally:
        for state in controls.values():
            state.release.set()
        await manager.shutdown(reason="execution-lifetime-unit-cleanup")


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def test_source_idle_deadline_tracks_only_true_state_changes():
    state = ThreadUnloadingState.create(has_subscribers=True, is_active=True, now=0)
    state.sync(has_subscribers=False, is_active=True, now=10)
    state.sync(has_subscribers=False, is_active=False, now=30)
    state.sync(has_subscribers=False, is_active=False, now=45)
    assert state.unloading_target() == 90
    assert not state.should_unload_now(now=89.999)
    assert state.should_unload_now(now=90)
    state.note_thread_activity_observed(now=91)
    assert state.unloading_target() == 151
    state.sync(has_subscribers=True, is_active=False, now=100)
    assert state.unloading_target() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_close", [False, True])
async def test_retirement_keeps_real_core_context_permissions_waiter_and_cleanup(tmp_path, monkeypatch, explicit_close):
    async with sessions(tmp_path, monkeypatch) as case:
        state = await case.launch("a")
        owner = case.owner
        permissions, adapter = owner.permission_context, owner.llm
        waiter = asyncio.get_running_loop().create_future()
        owner.turn_wait_state.register_waiter("retained-question", waiter, kind="user_input")
        owner.turn_wait_state.pending_approval_payloads["retained-question"] = {
            "conversation_id": state.conversation.id, "request": {"subtype": "request_user_input"},
        }
        terminal_close = AsyncMock(wraps=owner.terminal_manager.destroy_all)
        background_close = AsyncMock(wraps=owner.background_manager.shutdown)
        monkeypatch.setattr(owner.terminal_manager, "destroy_all", terminal_close)
        monkeypatch.setattr(owner.background_manager, "shutdown", background_close)
        if explicit_close:
            assert await case.manager.shutdown_session(owner.session_id)
        else:
            await case.expire(owner)
        assert case.manager.get_session(owner.session_id) is None
        assert case.manager.execution_session_for_conversation(state.conversation.id) is owner
        assert owner in case.manager.resource_sessions() and owner not in case.manager.iter_sessions()
        assert owner.run_manager.run_tasks[state.conversation.id] is state.task
        assert owner.run_manager.context_for(state.conversation.id) is state.context
        assert state.task.cancelling() == 0 and not state.task.done()
        assert owner.permission_context is permissions and owner.llm is adapter
        assert not waiter.done()
        assert owner.execution_cleanup_tasks and not owner.command_dispatcher.command_tasks
        assert not owner.session_lifecycle.is_shutting_down
        assert adapter.close_calls == 0
        terminal_close.assert_awaited_once()
        background_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_reader_loaded_before_first_execution_receives_actual_run_and_runtime(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch) as case:
        await case.reader.command_dispatcher._dispatch_client_command(UserCommand("conversation.create", {
            "conversation_id": "conv_lifetime_a", "activate": True,
        }), case.reader.connection_generation)
        conversation = case.reader.conversation_repo.get_conversation("conv_lifetime_a")
        assert case.reader.active_conversation_id == conversation.id
        assert conversation.id in case.manager._conversation_observers[case.reader]
        assert case.manager.execution_contexts.context_for(conversation.id) is None
        state = await case.launch("a", conversation=conversation)
        assert state.conversation.id in case.manager._conversation_observers[case.reader]
        await case.owner.send_event(AgentEvent(type="agent.item", data={"conversation_id": state.conversation.id,
            "item_id": "original-work", "kind": "process_text", "content": "actual original work", "status": "running"}))
        await case.owner.session_lifecycle.send_task_runtime_update(include_capabilities=False)
        await until(lambda: any(e.get("type") == "task.update" for e in case.reader.ws.sent))
        await case.reader.event_outbox.drain_delivery()
        assert any(e.get("item_id") == "original-work" for e in case.reader.ws.sent)
        assert any(e.get("type") == "task.update" and e["session"]["active_task_id"] == state.metadata["_run_task_id"]
            for e in case.reader.ws.sent)


@pytest.mark.asyncio
async def test_full_shutdown_does_not_close_a_physically_disconnected_asgi_socket_twice(tmp_path, monkeypatch):
    from starlette.websockets import WebSocketState

    async with sessions(tmp_path, monkeypatch) as case:
        state = await case.launch("a")
        await case.expire(case.owner)
        socket = case.owner.ws
        socket.client_state = socket.application_state = WebSocketState.DISCONNECTED
        close = AsyncMock(side_effect=RuntimeError("ASGI connection already closed"))
        monkeypatch.setattr(socket, "close", close)
        assert await case.manager.shutdown_execution_host(case.owner.session_id)
        close.assert_not_awaited()
        assert case.manager.execution_contexts.context_for(state.conversation.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("retired", [False, True], ids=["live-transport", "retired-transport"])
async def test_full_shutdown_uses_transport_ownership_after_asgi_stream_closes(tmp_path, monkeypatch, retired):
    async with sessions(tmp_path, monkeypatch) as case:
        incoming, incoming_reader = anyio.create_memory_object_stream[dict](float("inf"))
        outgoing, outgoing_reader = anyio.create_memory_object_stream[dict](float("inf"))
        async with incoming, incoming_reader, outgoing, outgoing_reader:
            await incoming.send({"type": "websocket.connect"})
            socket = WebSocket({"type": "websocket", "path": "/ws", "headers": [],
                "query_string": ("session_id=" + case.owner.session_id).encode()},
                incoming_reader.receive, outgoing.send)
            connection = _connection(tmp_path / "asgi-transport", case.owner.session_id)
            connection["websocket"] = socket
            owner, _generation = await case.manager.connect(**connection)
            assert owner is case.owner
            state = await case.launch("asgi-close-owner")
            close = AsyncMock(wraps=socket.close)
            monkeypatch.setattr(socket, "close", close)
            if retired:
                await case.expire(owner)
                # ASGI can finish without consuming websocket.disconnect. Its
                # protocol states remain connected while the actual streams
                # are closed, as in the current Starlette TestClient.
                await outgoing.aclose()
                await outgoing_reader.aclose()
            assert owner.is_connected is (not retired)
            assert socket.client_state is WebSocketState.CONNECTED
            assert socket.application_state is WebSocketState.CONNECTED
            assert await case.manager.shutdown_execution_host(owner.session_id)
            assert case.manager.execution_contexts.context_for(state.conversation.id) is None
            assert case.manager.execution_host_for_session(owner.session_id) is None
            assert owner.llm.close_calls == 1
            if retired:
                close.assert_not_awaited()
            else:
                close.assert_awaited_once_with(code=1000, reason="execution_shutdown")
                messages = [await outgoing_reader.receive()
                    for _ in range(outgoing_reader.statistics().current_buffer_used)]
                assert messages[0]["type"] == "websocket.accept"
                assert messages[-1] == {"type": "websocket.close", "code": 1000, "reason": "execution_shutdown"}


@pytest.mark.asyncio
async def test_original_reconnect_keeps_unique_outbox_and_durable_origin(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch) as case:
        state = await case.launch("a")
        owner = case.owner
        outbox, durable, token = owner.event_outbox, owner.run_manager.durable_client_commands, state.context.window_binding_token
        await owner.event_outbox.send_payload({"type": "tool_result", "conversation_id": state.conversation.id,
            "item_id": "one", "content": "before retirement"}, log_context="test")
        await case.expire(owner)
        await owner.event_outbox.send_payload({"type": "tool_result", "conversation_id": state.conversation.id,
            "item_id": "two", "content": "after retirement"}, log_context="test")
        restored, generation = await case.manager.connect(**_connection(tmp_path / "reconnect", owner.session_id))
        assert restored is owner and generation == 2
        assert restored.event_outbox is outbox and restored.run_manager.durable_client_commands is durable
        assert restored.run_manager.context_for(state.conversation.id) is state.context
        assert state.context.window_binding_token == token
        await owner.event_outbox.send_payload({"type": "tool_result", "conversation_id": state.conversation.id,
            "item_id": "three", "content": "after reconnect"}, log_context="test")
        await outbox.drain_persistence()
        entries = outbox._store.load(limit=100)
        seqs = [entry["seq"] for entry in entries]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        assert [entry["content"] for entry in entries if entry["type"] == "tool_result"] == [
            "before retirement", "after retirement", "after reconnect"]


@pytest.mark.asyncio
async def test_home_durable_ack_retains_actual_task_before_any_conversation_exists(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch) as case:
        owner = case.owner
        entered, release = asyncio.Event(), asyncio.Event()
        submitted = UserCommand("user_message", {"client_command_id": "home-accepted", "content": "Home input"})
        durable = owner.run_manager.durable_client_commands
        assert durable.persist_client_command(submitted)
        await owner.command_dispatcher._send_client_command_ack(submitted)
        captured = []

        async def preparing(command, generation):
            captured.append(asyncio.current_task())
            entered.set()
            await release.wait()
            conversation = owner.conversation_repo.create_conversation(conversation_id="conv_home_lifetime")
            owner.command_dispatcher._bind_user_message_owner(command, conversation.id)
            return True

        monkeypatch.setattr(owner.command_dispatcher, "_dispatch_client_command", preparing)
        owner.command_dispatcher._schedule_durable_client_command("home-accepted", owner.connection_generation, command_type="user_message")
        await entered.wait()
        actual = captured[0]
        admission = case.manager.execution_contexts.admission_for(actual)
        assert admission.owner.session is owner and admission.context is None
        assert case.manager.execution_contexts.contexts() == ()
        await case.expire(owner)
        assert not actual.done() and actual.cancelling() == 0
        assert case.manager.execution_host_for_session(owner.session_id) is owner
        assert durable.has_client_command("home-accepted")
        release.set()
        await actual
        await asyncio.sleep(0)
        assert not durable.has_client_command("home-accepted")
        assert case.manager.execution_contexts.admission_for(actual) is None
        assert case.manager.execution_session_for_conversation("conv_home_lifetime") is owner


@pytest.mark.asyncio
async def test_retirement_preserves_hydration_borrowed_by_actual_notification_core(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch) as case:
        owner = case.owner
        conversation = owner.conversation_repo.create_conversation(conversation_id="conv_hydration_borrower")
        owner.active_conversation_id = conversation.id
        hydrated = asyncio.Event()
        hydration = asyncio.create_task(hydrated.wait())
        owner.conversation_runtime._hydration_task = hydration
        entered = asyncio.Event()

        async def notification_core(content, *, conversation_id, metadata, **_kwargs):
            entered.set()
            try:
                await owner.conversation_runtime.wait_for_hydration(conversation_id)
            finally:
                owner.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])

        monkeypatch.setattr(owner, "_run_agent", notification_core)
        await owner.start_agent_run("", conversation_id=conversation.id, metadata={"_parent_notification_only": True})
        await entered.wait()
        actual = owner.run_manager.run_tasks[conversation.id]
        assert owner.command_dispatcher.execution_command_tasks == set()
        await case.expire(owner)
        assert hydration.cancelling() == 0 and not hydration.done()
        assert actual.cancelling() == 0 and not actual.done()
        hydrated.set()
        await actual


@pytest.mark.asyncio
async def test_explicit_scope_stop_cancels_real_admission_on_another_submitting_host(tmp_path, monkeypatch):
    from backend.ws.handlers.conversation import _stop_conversation_run

    async with sessions(tmp_path, monkeypatch) as case:
        state = await case.launch("a")
        source, owner = case.reader, case.owner
        submitted = asyncio.create_task(asyncio.Event().wait())
        source.command_dispatcher.track_execution_command_task(submitted, conversation_id=state.conversation.id)
        token = case.manager.execution_contexts.admission_for(submitted)
        assert token.owner.session is source and token.context is state.execution
        await case.expire(source)
        assert submitted.cancelling() == 0
        assert await _stop_conversation_run(owner, state.conversation.id, reason="conversation_archived")
        assert submitted.cancelled() and state.task.cancelled()
        assert case.manager.execution_contexts.admission_for(submitted) is None
        assert case.manager.execution_contexts.context_for(state.conversation.id) is state.execution


@pytest.mark.asyncio
async def test_another_window_instance_cannot_claim_original_sid_or_tool_identity(tmp_path, monkeypatch):
    from fastapi import WebSocketDisconnect

    async with sessions(tmp_path, monkeypatch) as case:
        owner = case.owner
        owner.window_instance_id, owner.window_owner_id = "original-instance", "same-owner"
        owner._window_binding_token = "original-token"
        state = await case.launch("a")
        await case.expire(owner)
        incoming = _connection(tmp_path / "intruder", owner.session_id)
        incoming["websocket"].query_params.update(window_instance_id="another-instance", window_owner_id="same-owner")
        monkeypatch.setattr("backend.ws.manager._verified_window_binding", AsyncMock(return_value="another-verified-token"))
        with pytest.raises(WebSocketDisconnect):
            await case.manager.connect(**incoming)
        assert case.manager.execution_session_for_conversation(state.conversation.id) is owner
        assert state.context.window_binding_token == "original-token"
        assert owner.window_instance_id == "original-instance"
        assert owner.event_outbox.connection_generation == 1
        assert state.task.cancelling() == 0


@pytest.mark.asyncio
async def test_reader_approves_original_waiter_after_owner_retirement(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch) as case:
        state = await case.launch("a")
        owner, reader = case.owner, case.reader
        reader.active_conversation_id = state.conversation.id
        await owner.send_event(AgentEvent(type="approval_request", data={
            "conversation_id": state.conversation.id, "tool_call_id": "original-request", "tool_name": "run_command",
            "args": {"command": "held original command"}, "turn_id": "original-turn",
            "message_id": "a-answer", "request_digest": "original-digest",
        }))
        pending = owner.turn_wait_state.pending_approval_payloads["original-request"]
        future = asyncio.get_running_loop().create_future()
        owner.turn_wait_state.register_waiter("original-request", future)
        await reader.reemit_pending_state(state.conversation.id)
        await case.expire(owner)
        original_permission = owner.permission_context
        await reader.command_dispatcher._dispatch_client_command(UserCommand("control_response", {
            "client_command_id": "reader-approval", "request_id": "original-request", "conversation_id": state.conversation.id,
            "turn_id": "original-turn", "message_id": "a-answer", "response": {"subtype": "success",
                "response": {"action": "approve", "request_digest": "original-digest"}},
        }), reader.connection_generation)
        assert future.result()["action"] == "approve"
        assert owner.permission_context is original_permission
        assert owner.turn_wait_state.pending_approval_payloads["original-request"] is pending
        assert reader.turn_wait_state.pending_approvals == {}
        assert owner.run_manager.context_for(state.conversation.id) is state.context


@pytest.mark.asyncio
async def test_idle_unload_uses_both_conditions_and_does_not_stop_another_thread(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch, unload_delay=0.04) as case:
        first, second = await case.launch("a"), await case.launch("c")
        await case.expire(case.owner)
        await case.sleep(0.07)
        assert case.manager.execution_contexts.context_for(first.conversation.id) is first.execution
        first.release.set()
        await first.task
        await until(lambda: case.manager.execution_contexts.context_for(first.conversation.id) is None)
        assert case.manager.execution_contexts.context_for(second.conversation.id) is second.execution
        assert second.task.cancelling() == 0
        assert not case.owner.session_lifecycle.is_shutting_down
        second.release.set()
        await second.task
        await until(lambda: case.manager.execution_contexts.retained_host(case.owner.session_id) is None)
        assert case.owner.llm.close_calls == 1


@pytest.mark.asyncio
async def test_subscriber_keeps_idle_loaded_core_and_app_shutdown_drains_detached_cores(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch, unload_delay=0.03) as case:
        first, second = await case.launch("a"), await case.launch("c")
        case.reader.active_conversation_id = first.conversation.id
        await case.reader.reemit_pending_state(first.conversation.id)
        await case.expire(case.owner)
        first.release.set()
        await first.task
        await case.sleep(0.07)
        assert case.manager.execution_contexts.context_for(first.conversation.id) is first.execution
        assert not second.task.done()
        await case.manager.shutdown()
        assert case.manager.execution_contexts.contexts() == ()
        assert case.manager.execution_contexts.retained_hosts() == ()
        assert second.task.cancelled()
        assert case.owner.llm.close_calls == 1


@pytest.mark.asyncio
async def test_actual_subagent_continuation_keeps_parent_runtime_loaded(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch, unload_delay=0.03) as case:
        state = await case.launch("a")
        runtime = default_runtime()
        parent = runtime.start_run(conversation_id=state.conversation.id, session_id=case.owner.session_id, run_id="borrowed-parent")
        release_child = asyncio.Event()
        child = asyncio.create_task(release_child.wait())
        runtime.register_subagent_task("borrowed-child", child, parent_run_id=parent.run_id,
            session_id=case.owner.session_id, canonical_task_name="borrowed_child")
        continuation = asyncio.create_task(release_child.wait())
        assert runtime.transfer_subagent_task_owner("borrowed-child", expected_task=child, owner_task=continuation)
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        assert runtime.subagent_tasks_for_conversation(state.conversation.id) == {continuation}
        await case.expire(case.owner)
        state.release.set()
        await state.task
        await case.sleep(0.07)
        assert case.manager.execution_contexts.context_for(state.conversation.id) is state.execution
        assert continuation.cancelling() == 0
        release_child.set()
        await continuation
        case.manager.note_execution_activity(state.conversation.id)
        await until(lambda: case.manager.execution_contexts.context_for(state.conversation.id) is None)


@pytest.mark.asyncio
async def test_unload_failure_keeps_original_runtime_and_old_request_cannot_remove_replacement(tmp_path, monkeypatch):
    async with sessions(tmp_path, monkeypatch) as case:
        state = await case.launch("a")
        state.release.set()
        await state.task
        await until(lambda: not case.owner.execution_cleanup_tasks)
        unload = case.owner.session_lifecycle.unload_conversation
        monkeypatch.setattr(case.owner.session_lifecycle, "unload_conversation", AsyncMock(side_effect=RuntimeError("actual shutdown submission failed")))
        with pytest.raises(RuntimeError, match="actual shutdown submission failed"):
            await case.manager.unload_conversation_execution(state.conversation.id, reason="test-unload")
        assert case.manager.execution_contexts.context_for(state.conversation.id) is state.execution
        assert not state.execution.shutting_down
        assert not case.owner.session_lifecycle.is_shutting_down
        monkeypatch.setattr(case.owner.session_lifecycle, "unload_conversation", unload)
        old = case.manager.execution_contexts.begin_unload(state.execution)
        assert await case.manager._unload_execution_context(old, reason="test-unload")
        replacement = case.manager.load_conversation_execution(state.conversation.id, case.owner)
        assert replacement is not state.execution
        assert case.manager.execution_contexts.finish_unload(old) is False
        assert case.manager.execution_contexts.context_for(state.conversation.id) is replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("started_core", [False, True])
async def test_global_shutdown_propagates_actual_cleanup_failure_and_retains_original_host(tmp_path, monkeypatch, started_core):
    from fastapi import WebSocketDisconnect

    async with sessions(tmp_path, monkeypatch) as case:
        state = await case.launch("a") if started_core else None
        if started_core:
            await case.expire(case.owner)
        shutdown = case.owner.session_lifecycle.shutdown
        async def failed_shutdown(*, reason):
            case.owner.session_lifecycle.is_shutting_down = True
            raise RuntimeError("actual core cleanup failed")

        monkeypatch.setattr(case.owner.session_lifecycle, "shutdown", failed_shutdown)
        with pytest.raises(ExceptionGroup, match="Could not drain execution hosts") as error:
            await case.manager.shutdown()
        assert any(str(item) == "actual core cleanup failed" for item in error.value.exceptions)
        assert case.manager.execution_contexts.retained_host(case.owner.session_id).session is case.owner
        if state is not None:
            assert case.manager.execution_contexts.context_for(state.conversation.id) is state.execution
            assert not state.execution.shutting_down
            assert not state.task.done()
        else:
            assert case.manager.execution_contexts.contexts() == ()
        with pytest.raises(WebSocketDisconnect) as refused:
            await case.manager.connect(**_connection(tmp_path / "closing-reconnect", case.owner.session_id))
        assert refused.value.code == 1013
        assert case.manager.execution_host_for_session(case.owner.session_id) is case.owner
        monkeypatch.setattr(case.owner.session_lifecycle, "shutdown", shutdown)
