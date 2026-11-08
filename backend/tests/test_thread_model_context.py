"""A completed thread transfers its model context only while durable state agrees."""
import asyncio
from copy import deepcopy

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.tests.test_user_input_presentation_chain import session_for
from backend.ws.execution_context import ConversationThreadRuntime
from backend.ws.manager import WebSocketManager
from backend.ws.model_prewarm import prepare_thread_model_context, release_prepared_model_context, schedule_model_prewarm
from pathlib import Path
import threading


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["history", "metadata", "workspace", "local_history", "borrower", "presentation"])
async def test_retained_context_checks_durable_content_workspace_and_pending_borrowers(change):
    builder = ContextBuilder()
    builder.append_user("original")
    snapshot = builder.export_snapshot()
    thread = ConversationThreadRuntime("store", "thread")
    thread.retain_model_context(builder, snapshot, "workspace")
    incoming = deepcopy(snapshot)
    workspace = "workspace"
    pending = None
    if change == "history":
        incoming["history"][0]["content"] = "external replacement"
    elif change == "metadata":
        incoming["extension_state"] = {"changed": True}
    elif change == "workspace":
        workspace = "other"
    elif change == "local_history":
        builder.append_user("late local mutation")
    elif change == "borrower":
        pending = asyncio.create_task(asyncio.Event().wait())
        thread.retain_turn(pending, set())
    else:
        incoming["ui_agent_state"] = {"expanded": True}
        incoming["_ui_agent_state_revision"] = 9
    try:
        taken = thread.take_model_context(incoming, workspace)
        assert (taken is builder) == (change == "presentation")
        assert thread.model_context is None
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await thread.close_environments()


@pytest.mark.asyncio
async def test_real_ws_turns_reuse_context_and_external_replacement_forces_reload(tmp_path, monkeypatch):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    thread = ConversationThreadRuntime(session.conversation_repo.store_instance_id(), conversation.id)
    snapshots = []
    load = ContextBuilder.load_snapshot

    def observed(builder, snapshot):
        if builder._conversation_id == conversation.id:
            snapshots.append(builder)
        return load(builder, snapshot)

    monkeypatch.setattr(ContextBuilder, "load_snapshot", observed)
    owners = []

    async def turn(number):
        run = RunContext(environment_thread_runtime=thread)
        await asyncio.create_task(session._run_agent(f"input {number}", conversation_id=conversation.id,
            run_context=run, metadata={"user_message_id": f"user-{number}", "assistant_message_id": f"answer-{number}"}))
        await asyncio.gather(*tuple(session.run_manager.lifecycle_cleanup_tasks_for(conversation.id)))
        assert thread.model_context is not None
        owners.append(thread.model_context[0])

    try:
        await turn(1)
        await turn(2)
        assert owners[0] is owners[1]
        assert snapshots == [owners[0]]
        assert any("input 1" in str(message.content) for message in adapter.requests[1])
        assert any("fixture answer" in str(message.content) for message in adapter.requests[1])
        saved = session.conversation_repo.get_execution_context(conversation.id)
        replaced = deepcopy(saved.context_snapshot)
        replaced["history"][0]["content"] = "external history replacement"
        session.conversation_repo.save_context_snapshot(conversation.id, replaced)
        await turn(3)
        assert owners[2] is not owners[1]
        assert any("external history replacement" in str(message.content) for message in adapter.requests[2])
        assert len(snapshots) == 2
    finally:
        await thread.close_environments()
        await session.session_lifecycle.shutdown(reason="thread-model-context")


@pytest.mark.asyncio
async def test_idle_context_is_taken_by_the_first_real_turn_without_loading_it_again(tmp_path, monkeypatch):
    session, record, adapter = session_for(tmp_path, monkeypatch)
    manager = WebSocketManager()
    session.ws_manager = manager
    session.conversation_repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "Restored original input"}]})
    loads = []
    load = ContextBuilder.load_snapshot
    def observe(builder, snapshot):
        if builder._conversation_id == record.id:
            loads.append(builder)
        return load(builder, snapshot)
    monkeypatch.setattr(ContextBuilder, "load_snapshot", observe)
    workspace = Path(record.execution_cwd)
    try:
        await prepare_thread_model_context(session, record.id, workspace, session.config)
        thread = manager.execution_contexts.thread_runtime_for(session.conversation_repo.store_instance_id(), record.id)
        prepared = thread.model_context[0]
        assert loads == [prepared] and prepared is not session.context_builder
        session.context_builder.append_user("Reader-only mutation must not enter model context")
        await asyncio.create_task(session._run_agent("First real input", conversation_id=record.id,
            run_context=RunContext(environment_thread_runtime=thread),
            metadata={"user_message_id": "first-user", "assistant_message_id": "first-answer"}))
        await asyncio.gather(*tuple(session.run_manager.lifecycle_cleanup_tasks_for(record.id)))
        assert loads == [prepared]
        assert thread.model_context[0] is prepared
        assert any(message.content == "Restored original input" for message in adapter.requests[0])
        assert not any("Reader-only mutation" in str(message.content) for message in adapter.requests[0])
        release_prepared_model_context(session)
    finally:
        session.ws_manager = None
        await session.session_lifecycle.shutdown(reason="idle-context-first-turn")
        for thread in manager.execution_contexts.thread_runtimes():
            await thread.close_environments()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["history", "workspace", "admission", "navigation", "shutdown"])
async def test_idle_preparation_cannot_publish_across_an_owner_transition(tmp_path, monkeypatch, change):
    session, record, _ = session_for(tmp_path, monkeypatch)
    manager = WebSocketManager()
    session.ws_manager = manager
    workspace = Path(record.execution_cwd)
    entered, release = threading.Event(), threading.Event()
    load = ContextBuilder.load_snapshot
    def delayed(builder, snapshot):
        if builder._conversation_id == record.id:
            entered.set()
            assert release.wait(5)
        return load(builder, snapshot)
    monkeypatch.setattr(ContextBuilder, "load_snapshot", delayed)
    task = asyncio.create_task(prepare_thread_model_context(session, record.id, workspace, session.config))
    assert await asyncio.to_thread(entered.wait, 3)
    try:
        if change == "history":
            session.conversation_repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "newer history"}]})
        elif change == "workspace":
            workspace = tmp_path / "another-workspace"
        elif change == "admission":
            session.run_manager.run_tasks[record.id] = asyncio.current_task()
        elif change == "navigation":
            session.active_conversation_id = "another-thread"
        else:
            session.session_lifecycle.is_shutting_down = True
        release.set()
        await task
        thread = manager.execution_contexts.thread_runtime_for(session.conversation_repo.store_instance_id(), record.id)
        if change in {"history", "workspace"}:
            current = session.conversation_repo.get_execution_context(record.id)
            assert thread.take_model_context(current.context_snapshot, workspace) is None
        elif change == "admission":
            assert thread.model_context is None and thread.model_context_preparation.done()
            current = session.conversation_repo.get_execution_context(record.id)
            assert await thread.acquire_model_context(current.context_snapshot, workspace) is not None
        else:
            assert thread is None
        release_prepared_model_context(session)
    finally:
        release.set()
        session.run_manager.run_tasks.pop(record.id, None)
        session.ws_manager = None
        await session.session_lifecycle.shutdown(reason="idle-context-transition")


@pytest.mark.asyncio
async def test_prewarm_shutdown_drains_the_history_worker_and_does_not_retain_a_speculative_thread(tmp_path, monkeypatch):
    session, record, _ = session_for(tmp_path, monkeypatch)
    manager = WebSocketManager()
    session.ws_manager = manager
    session.provider, session.selected_model = "custom", session.config.llm.model
    monkeypatch.setattr("backend.config.load_config", lambda **_: session.config)
    entered, release = threading.Event(), threading.Event()
    load = ContextBuilder.load_snapshot
    def delayed(builder, snapshot):
        if builder._conversation_id == record.id:
            entered.set()
            assert release.wait(5)
        return load(builder, snapshot)
    monkeypatch.setattr(ContextBuilder, "load_snapshot", delayed)
    schedule_model_prewarm(session, record.id)
    assert await asyncio.to_thread(entered.wait, 3)
    shutdown = asyncio.create_task(session.session_lifecycle.shutdown(reason="prewarm-worker-drain"))
    await asyncio.sleep(.03)
    assert not shutdown.done()
    release.set()
    await shutdown
    assert session._model_context_prewarm is None
    assert not manager.execution_contexts.thread_runtimes()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "history", "navigation", "cancel"])
async def test_first_turn_takes_the_inflight_history_preparation_instead_of_decoding_twice(tmp_path, monkeypatch, change):
    session, record, adapter = session_for(tmp_path, monkeypatch)
    manager = WebSocketManager()
    session.ws_manager = manager
    workspace = Path(record.execution_cwd)
    session.conversation_repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "Original retained input"}]})
    entered, release = threading.Event(), threading.Event()
    acquired = asyncio.Event()
    load, acquire = ContextBuilder.load_snapshot, ConversationThreadRuntime.acquire_model_context
    loads = []
    def held(builder, snapshot):
        if builder._conversation_id == record.id:
            loads.append(builder)
            if len(loads) == 1:
                entered.set()
                assert release.wait(6)
        return load(builder, snapshot)
    async def observed(thread, snapshot, cwd):
        acquired.set()
        return await acquire(thread, snapshot, cwd)
    monkeypatch.setattr(ContextBuilder, "load_snapshot", held)
    monkeypatch.setattr(ConversationThreadRuntime, "acquire_model_context", observed)
    idle = asyncio.create_task(prepare_thread_model_context(session, record.id, workspace, session.config))
    assert await asyncio.to_thread(entered.wait, 3)
    thread = manager.execution_contexts.thread_runtime_for(session.conversation_repo.store_instance_id(), record.id)
    if change == "history":
        session.conversation_repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "New durable history"}]})
    turn = asyncio.create_task(session._run_agent("Actual input", conversation_id=record.id,
        run_context=RunContext(environment_thread_runtime=thread),
        metadata={"user_message_id": "real-user", "assistant_message_id": "real-answer"}))
    try:
        await asyncio.wait_for(acquired.wait(), 3)
        assert thread.model_context_preparation is None
        assert len(loads) == 1 and not adapter.requests
        if change == "navigation":
            session.active_conversation_id = "new-reader"
            release_prepared_model_context(session)
            idle.cancel()
            await asyncio.gather(idle, return_exceptions=True)
        elif change == "cancel":
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            assert thread.pending_tasks()
        release.set()
        await asyncio.gather(turn, idle, return_exceptions=True)
        await asyncio.gather(*tuple(session.run_manager.lifecycle_cleanup_tasks_for(record.id)))
        if change == "cancel":
            assert not adapter.requests
        else:
            assert turn.exception() is None
            assert len(loads) == (2 if change == "history" else 1)
            expected = "New durable history" if change == "history" else "Original retained input"
            assert any(message.content == expected for message in adapter.requests[0])
    finally:
        release.set()
        await asyncio.gather(turn, idle, return_exceptions=True)
        release_prepared_model_context(session)
        await thread.close_environments()
        session.ws_manager = None
        await session.session_lifecycle.shutdown(reason="inflight-model-context")
