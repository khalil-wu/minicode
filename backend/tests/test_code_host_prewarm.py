from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from backend.agent.code_execution_store import CodeExecutionStore
from backend.agent.code_host import _Connection
from backend.feature_flags import FeatureFlags
from backend.tests.test_code_host import start
from backend.tests.test_user_input_presentation_chain import session_for
from backend.ws.model_prewarm import schedule_model_prewarm


def configured_session(tmp_path, monkeypatch, *, enabled):
    session, record, model = session_for(tmp_path, monkeypatch)
    session.provider, session.selected_model = "custom", session.config.llm.model
    session.config = replace(session.config, feature_flags=FeatureFlags({"code_mode_prewarm": enabled}))
    monkeypatch.setattr("backend.config.load_config", lambda **_: session.config)
    return session, record, model


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_idle_thread_prewarms_only_its_host_without_running_code_or_publishing_output(tmp_path, monkeypatch, enabled):
    session, record, model = configured_session(tmp_path, monkeypatch, enabled=enabled)
    before = session.conversation_repo.get_conversation(record.id)
    connection = None
    try:
        schedule_model_prewarm(session, record.id)
        await session._model_prewarm_tasks[record.id][1]
        if not enabled:
            assert not session.conversation_runtime._code_stores
        else:
            store = session.conversation_runtime.code_store_for(record.id)
            connection, binding_id = store._host_session._binding
            assert connection.alive and not store.cells and not store.receipts and not store.values
            vm = store.create_vm()
            try:
                result = await start(vm, "text('actual cell');")
                assert result["output"][0]["text"] == "actual cell"
                assert vm._binding == (connection, binding_id)
            finally:
                await vm.aclose()
        assert not model.requests and not session.ws.messages
        assert session.conversation_repo.get_conversation(record.id).to_dict() == before.to_dict()
    finally:
        await session.session_lifecycle.shutdown(reason="code-prewarm-test")
    if connection is not None:
        assert connection.process.returncode == 0


@pytest.mark.asyncio
async def test_prewarmed_stores_share_process_and_close_after_last_owner():
    left, right = CodeExecutionStore(), CodeExecutionStore()
    try:
        await asyncio.gather(left.prewarm(), right.prewarm(), left.prewarm())
        connection, left_id = left._host_session._binding
        other_connection, right_id = right._host_session._binding
        assert connection is other_connection and left_id != right_id
        assert len(connection._pending) == 0
        await left.aclose()
        assert connection.alive
        vm = right.create_vm()
        try:
            assert (await start(vm, "text(typeof globalThis.leftValue);"))["output"][0]["text"] == "undefined"
        finally:
            await vm.aclose()
        await right.aclose()
        assert connection.process.returncode == 0
    finally:
        await left.aclose()
        await right.aclose()


@pytest.mark.asyncio
async def test_shutdown_drains_inflight_prewarm_and_reaps_the_original_process(tmp_path, monkeypatch):
    session, record, _ = configured_session(tmp_path, monkeypatch, enabled=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = _Connection.start
    connections = []

    async def held():
        connection = await original()
        connections.append(connection)
        entered.set()
        await release.wait()
        return connection

    monkeypatch.setattr(_Connection, "start", held)
    schedule_model_prewarm(session, record.id)
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(session.session_lifecycle.shutdown(reason="code-prewarm-shutdown"))
    try:
        await asyncio.sleep(.02)
        assert not closing.done() and connections[0].alive
        release.set()
        await asyncio.wait_for(closing, 5)
        assert connections[0].process.returncode == 0
        assert not session.conversation_runtime._code_stores
    finally:
        release.set()
        await closing


@pytest.mark.asyncio
async def test_optional_host_start_failure_preserves_foreground_recovery(tmp_path, monkeypatch, caplog):
    session, record, _ = configured_session(tmp_path, monkeypatch, enabled=True)
    original = _Connection.start

    async def failed():
        raise OSError("owned host launch failed")

    monkeypatch.setattr(_Connection, "start", failed)
    try:
        schedule_model_prewarm(session, record.id)
        await session._model_prewarm_tasks[record.id][1]
        assert "Code host startup prewarm failed" in caplog.text
        assert not session.ws.messages
        monkeypatch.setattr(_Connection, "start", original)
        store = session.conversation_runtime.code_store_for(record.id)
        vm = store.create_vm()
        try:
            assert (await start(vm, "text('recovered');"))["output"][0]["text"] == "recovered"
        finally:
            await vm.aclose()
    finally:
        await session.session_lifecycle.shutdown(reason="code-prewarm-failure")
