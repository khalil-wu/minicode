from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import UserCommand
from backend.agent.thread_sample import SampleError, SampleParams
from backend.config import AgentSettings, LLMSettings
from backend.llm.base import LLMMessage, StreamEvent, StreamEventType as E, ToolCallEvent
from backend.tests.test_conversation_window_follower import windows, Socket
from backend.tests.test_thread_sample import Model, done
from backend.tests.test_code_execution import FixtureTool
from backend.tools.registry import ToolRegistry
from backend.ws.thread_sample import generate_sample


def loaded_source(windows, monkeypatch, model):
    manager, owner, follower, record, _, _, drain = windows
    model_settings = LLMSettings(provider="custom", model="sample-fixture", wire_api="responses", api_key="test")
    owner.config = replace(owner.config, llm=model_settings, agent=AgentSettings(code_mode_only=False))
    record.model_selection = {"provider": "custom", "model": "sample-fixture", "reasoning_effort": "low"}
    record.execution_cwd = record.workspace_root
    record.transcript = [{"id": "user", "role": "user", "content": "original prompt", "turn_id": "turn-one"},
        {"id": "assistant", "turn_id": "turn-one", "role": "assistant", "content": "original answer", "terminal_status": "completed"}]
    builder = ContextBuilder(conversation_id=record.id)
    builder._history = [LLMMessage("user", "original prompt"), LLMMessage("assistant", "original answer")]
    record.context_snapshot = builder.export_snapshot()
    record = owner.conversation_repo.save_conversation(record)
    loaded = manager.execution_contexts.load(record.id, owner, has_subscribers=True, is_active=False,
        now=asyncio.get_running_loop().time())
    tool = FixtureTool("sample_write", effect=True)
    registry = ToolRegistry()
    registry.register(tool)
    monkeypatch.setattr(owner, "_conversation_tool_registry", lambda *args, **kwargs: registry.fork())
    monkeypatch.setattr(owner, "_model_runtime_for_conversation", lambda *_: None)
    def create(config, **kwargs):
        model.captured_config = config
        return model
    monkeypatch.setattr("backend.ws.thread_sample.create_session_llm", create)
    directories = []
    def temporary(**kwargs):
        directory = TemporaryDirectory(**kwargs)
        directories.append(Path(directory.name))
        return directory
    monkeypatch.setattr("backend.ws.thread_sample.TemporaryDirectory", temporary)
    return loaded, record, tool, directories


@pytest.mark.asyncio
async def test_second_window_sample_inherits_history_without_publishing_or_persisting_a_thread(windows, monkeypatch):
    manager, owner, follower, _, other, _, drain = windows
    model = Model([StreamEvent(E.TEXT_CHUNK, "sample answer"), done()])
    loaded, record, tool, directories = loaded_source(windows, monkeypatch, model)
    follower.active_conversation_id = other.id
    before = owner.conversation_repo.get_conversation(record.id).to_dict()
    # The loaded source configuration wins over a later host config edit.
    owner.config = replace(owner.config, llm=replace(owner.config.llm, model="unrelated-model", max_tokens=42))
    await follower.command_dispatcher._dispatch_client_command(UserCommand("thread/sample", {
        "threadId": record.id, "expectedTurnId": "turn-one", "prompt": "sample prompt", "requestId": 7,
        "client_command_id": "sample-receipt"}), follower.connection_generation)
    await drain()
    result = next(payload for payload in follower.ws.sent if payload.get("command") == "thread/sample")
    assert result["level"] == "info", result
    assert result["data"]["text"] == "sample answer"
    assert result["data"]["requestId"] == 7 and result["data"]["sourceTurnId"] == "turn-one"
    assert model.captured_config.llm.max_tokens == loaded.thread_runtime.model_config.llm.max_tokens != 42
    assert owner.conversation_repo.get_conversation(record.id).to_dict() == before
    assert model.closed.is_set() and all(not directory.exists() for directory in directories)
    assert any("original answer" in message.content for message in model.inputs)
    assert tool.executions == 0 and loaded.host is owner and follower.active_conversation_id == other.id
    assert not [payload for payload in owner.ws.sent if payload.get("command") == "thread/sample"]
    assert not [payload for payload in follower.event_outbox._events if payload.get("command") == "thread/sample"]
    assert not follower.command_dispatcher.thread_samples.requests


@pytest.mark.asyncio
async def test_changed_source_rejects_result_after_private_runtime_cleanup(windows, monkeypatch):
    _, owner, follower, _, _, _, _ = windows
    model = Model([StreamEvent(E.TEXT_CHUNK, "stale answer"), done()], hold=True)
    _, record, _, directories = loaded_source(windows, monkeypatch, model)
    task = asyncio.create_task(generate_sample(follower, SampleParams(record.id, "turn-one", "sample"),
        asyncio.Event(), asyncio.get_running_loop().time() + 15))
    try:
        await asyncio.wait_for(model.started.wait(), 8)
        current = owner.conversation_repo.get_conversation(record.id)
        current.context_snapshot["history"].append({"role": "user", "content": "new history"})
        owner.conversation_repo.save_conversation(current)
        model.release.set()
        with pytest.raises(SampleError, match="source changed"):
            await task
        assert model.closed.is_set() and all(not directory.exists() for directory in directories)
    finally:
        model.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale", "active", "archived", "subagent"])
async def test_invalid_source_never_opens_provider_or_private_storage(windows, monkeypatch, change):
    _, owner, follower, _, _, start, _ = windows
    model = Model([done()])
    _, record, _, directories = loaded_source(windows, monkeypatch, model)
    if change == "active":
        start()
    elif change in {"archived", "subagent"}:
        setattr(record, "archived" if change == "archived" else "thread_source", True if change == "archived" else "subagent")
        owner.conversation_repo.save_conversation(record)
    with pytest.raises(SampleError):
        await generate_sample(follower, SampleParams(record.id, "stale" if change == "stale" else "turn-one", "sample"),
            asyncio.Event(), asyncio.get_running_loop().time() + 15)
    assert model.calls == 0 and not directories


@pytest.mark.asyncio
async def test_reconnect_cancels_original_request_without_delivering_its_result(windows, monkeypatch):
    _, owner, follower, _, _, _, _ = windows
    model = Model([done()], hold=True)
    _, record, _, directories = loaded_source(windows, monkeypatch, model)
    old_socket, old_generation = follower.ws, follower.connection_generation
    task = asyncio.create_task(follower.command_dispatcher._dispatch_client_command(UserCommand("thread/sample", {
        "threadId": record.id, "expectedTurnId": "turn-one", "prompt": "sample", "requestId": "same"}), old_generation))
    try:
        await asyncio.wait_for(model.started.wait(), 8)
        follower.attach_websocket(Socket(), window_instance_id=follower.window_instance_id, window_owner_id=follower.window_owner_id)
        await asyncio.wait_for(task, 8)
        await follower.event_outbox.drain_delivery()
        assert model.closed.is_set()
        assert not [payload for payload in follower.ws.sent if payload.get("command") == "thread/sample"]
        assert all(not directory.exists() for directory in directories)
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_startup_keeps_actual_worker_owned_until_cleanup(windows, monkeypatch):
    import threading
    _, _, follower, _, _, _, _ = windows
    model = Model([done()])
    _, record, _, directories = loaded_source(windows, monkeypatch, model)
    started, release = threading.Event(), threading.Event()
    def create(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return model
    monkeypatch.setattr("backend.ws.thread_sample.create_session_llm", create)
    requests = follower.command_dispatcher.thread_samples
    task = asyncio.create_task(requests.sample(7, SampleParams(record.id, "turn-one", "sample"), follower.connection_generation))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        requests.cancel(7, follower.connection_generation)
        assert not task.done() and any(directory.exists() for directory in directories)
        release.set()
        with pytest.raises(SampleError, match="canceled"):
            await task
        assert model.calls == 0 and model.closed.is_set()
        assert all(not directory.exists() for directory in directories)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_sample_cancellation_keeps_its_host_error_and_does_not_report_a_provider_failure(windows, monkeypatch):
    _, _, follower, _, _, _, drain = windows
    model = Model([done()], hold=True)
    _, record, _, _ = loaded_source(windows, monkeypatch, model)
    task = asyncio.create_task(follower.command_dispatcher._dispatch_client_command(UserCommand("thread/sample", {
        "threadId": record.id, "expectedTurnId": "turn-one", "prompt": "sample", "requestId": 8}), follower.connection_generation))
    try:
        await asyncio.wait_for(model.started.wait(), 8)
        await follower.command_dispatcher._dispatch_client_command(UserCommand("thread/sample/cancel", {"requestId": 8}),
            follower.connection_generation)
        await task
        await drain()
        result = next(row for row in follower.ws.sent if row.get("command") == "thread/sample")
        assert result["level"] == "error" and result["message"] == "sample canceled"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_rpc_deadline_retains_unfinished_startup_owner_until_it_can_release_resources(windows, monkeypatch):
    import threading
    _, _, follower, _, _, _, _ = windows
    model = Model([done()])
    _, record, _, directories = loaded_source(windows, monkeypatch, model)
    started, release = threading.Event(), threading.Event()
    def create(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return model
    monkeypatch.setattr("backend.ws.thread_sample.create_session_llm", create)
    monkeypatch.setattr("backend.ws.thread_sample.SAMPLE_TIMEOUT", .2)
    requests = follower.command_dispatcher.thread_samples
    task = asyncio.create_task(requests.sample("slow", SampleParams(record.id, "turn-one", "sample"), follower.connection_generation))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        with pytest.raises(SampleError, match="timed out"):
            await task
        retained, = requests.requests.values()
        assert not retained.task.done() and retained.task in follower.cleanup_tasks
        release.set()
        await asyncio.gather(retained.task, return_exceptions=True)
        assert model.closed.is_set() and model.calls == 0
        assert not requests.requests and all(not directory.exists() for directory in directories)
    finally:
        release.set()
        await asyncio.gather(task, *(entry.task for entry in requests.requests.values()), return_exceptions=True)
