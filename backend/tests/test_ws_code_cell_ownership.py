"""Actual WebSocket core unload/model rotation retain the original code borrower."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace

import pytest

from backend.agent.runtime import AgentRuntime
from backend.config import AgentSettings, LLMSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.tests.test_code_cell_thread_lifetime import ProcessTool
from backend.tests.test_user_input_presentation_chain import session_for
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.ws.agent_runner import _clear_session_llm_cache


class CellModel(LLMAdapter):
    def __init__(self):
        self._settings = LLMSettings(api_key="fixture", provider="custom", model="ws-code-owner", tool_mode="code_mode")
        self.step = 0
        self.close_calls = 0
        self.closed = asyncio.Event()
        self.close_observations = []
        self.tool = None

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.step += 1
        if self.step == 1:
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent("start-cell", "tool_exec", {
                "code": "text('yielded'); const result=await tools.held_process({}); text(result.content);", "yield_time_ms": 1})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Keep the yielded code cell.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)

    async def simple_chat(self, *args, **kwargs):
        return "Controlled summary."

    async def aclose(self):
        self.close_observations.append((self.tool.process.returncode, self.tool.finished.is_set()))
        self.close_calls += 1
        self.closed.set()


@asynccontextmanager
async def ws_cell(tmp_path, monkeypatch, *, gated=False):
    session, conversation, _ = session_for(tmp_path, monkeypatch)
    session.conversation_repo.update_memory_mode(conversation.id, "disabled")
    model, tool = CellModel(), ProcessTool()
    model.tool = tool
    if gated:
        tool.cleanup_release = asyncio.Event()
    config = replace(session.config, llm=model._settings,
        agent=AgentSettings(max_iterations=5, max_turn_seconds=15, stream_max_attempts=0))
    session.config, session.llm = config, model
    session._resolve_available_models = lambda *args: [model._settings.model]
    session._llm_adapter_cache[("controlled-old-model",)] = model
    session.permission_context_for_conversation = lambda record, source="agent.run": session.permission_checker.build_context(
        mode="bypass", sandbox_mode="danger-full-access", source=source)
    for entry in (tool, ToolExecTool(), ToolWaitTool()):
        session.tool_registry.register(entry)
    runtime = AgentRuntime(metrics_file=tmp_path / "code-runtime/metrics.jsonl",
        swarm_store_dir=tmp_path / "code-runtime/swarm", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **kwargs: config)
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *args, **kwargs: model)
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    monkeypatch.setattr("backend.ws.agent_runner.default_runtime", lambda: runtime)
    monkeypatch.setattr("backend.agent.query_engine.default_runtime", lambda: runtime)
    # Independent memory jobs have their own real lease regression suite.
    monkeypatch.setattr("backend.memory.generation.schedule_memory_startup", lambda **kwargs: None)
    store = session.conversation_runtime.code_store_for(conversation.id)
    try:
        await session.start_agent_run("Start one code cell, then finish this turn.", conversation_id=conversation.id)
        task = session.run_manager.run_tasks[conversation.id]
        await asyncio.wait_for(task, 20)
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        await asyncio.wait_for(tool.started.wait(), 5)
        cell = next(iter(store.cells.values()))
        assert tool.process.returncode is None and not session.run_manager.run_tasks
        assert cell.task in session._llm_adapter_leases[id(model)]
        yield session, conversation, model, tool, store, cell
    finally:
        if tool.cleanup_release is not None:
            tool.cleanup_release.set()
        if tool.process is not None:
            (tmp_path / "workspace/release").write_text("complete", encoding="utf-8")
        while pending := store.pending_tasks():
            await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True)
        await session.conversation_runtime.close_code_stores()
        await session.session_lifecycle.shutdown(reason="ws-code-test-cleanup")
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_ws_unload_closes_its_completed_turns_cell_and_preserves_thread_data(tmp_path, monkeypatch):
    async with ws_cell(tmp_path, monkeypatch) as (session, conversation, model, tool, store, cell):
        other = session.conversation_runtime.code_store_for("other-hidden-thread")
        other.put("draft", {"text": "Preserve hidden workspace input"})
        before = session.conversation_repo.get_conversation(conversation.id)
        transcript, snapshot = deepcopy(before.transcript), deepcopy(before.context_snapshot)
        await session.session_lifecycle.unload_conversation(conversation.id, reason="thread_unloaded")
        assert tool.process.returncode is not None and tool.finished.is_set()
        assert conversation.id not in session.conversation_runtime._code_stores
        assert session.conversation_runtime._code_stores["other-hidden-thread"] is other
        assert other.values["draft"]["text"] == "Preserve hidden workspace input"
        assert store.receipts[cell.id].status == "cancelled" and not store.pending_tasks()
        after = session.conversation_repo.get_conversation(conversation.id)
        assert after.transcript == transcript and after.context_snapshot == snapshot


@pytest.mark.asyncio
async def test_ws_shutdown_waits_for_retained_cell_before_closing_its_model(tmp_path, monkeypatch):
    async with ws_cell(tmp_path, monkeypatch) as (session, _, model, tool, store, _):
        await session.session_lifecycle.shutdown(reason="actual_session_shutdown")
        assert tool.process.returncode is not None and tool.finished.is_set()
        assert not session.conversation_runtime._code_stores and not store.pending_tasks()
        assert model.close_calls == 1 and model.close_observations[0][0] is not None
        assert model.close_observations[0][1] is True


@pytest.mark.asyncio
async def test_ws_model_rotation_keeps_old_adapter_until_cell_and_actual_tool_finish(tmp_path, monkeypatch):
    async with ws_cell(tmp_path, monkeypatch) as (session, _, model, tool, store, cell):
        assert tool.execution_task in session._llm_adapter_leases[id(model)]
        _clear_session_llm_cache(session)
        assert model.close_calls == 0 and session._retired_llm_adapters[id(model)] is model
        assert not cell.task.done() and tool.process.returncode is None
        (tmp_path / "workspace/release").write_text("complete", encoding="utf-8")
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in store.pending_tasks())), 5)
        await asyncio.wait_for(model.closed.wait(), 5)
        assert tool.process.returncode == 0 and model.close_calls == 1
        assert model.close_observations == [(0, True)]
        assert not store.pending_tasks() and id(model) not in session._retired_llm_adapters


@pytest.mark.asyncio
async def test_ws_pending_code_cleanup_keeps_store_source_and_original_adapter(tmp_path, monkeypatch):
    async with ws_cell(tmp_path, monkeypatch, gated=True) as (session, conversation, model, tool, store, cell):
        monkeypatch.setattr("backend.agent.code_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr("backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr("backend.tools.registry.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.02)
        closing = asyncio.create_task(session.session_lifecycle.unload_conversation(conversation.id, reason="gated_unload"))
        await asyncio.wait_for(tool.cleanup_started.wait(), 5)
        with pytest.raises(RuntimeError, match="pending tool cleanup"):
            await closing
        assert session.conversation_runtime._code_stores[conversation.id] is store
        assert tool.process.returncode is None and not tool.execution_task.done()
        assert tool.execution_task in store.pending_tasks() and tool.execution_task in session._llm_adapter_leases[id(model)]
        assert cell in store.live_tool_calls().values()
        with pytest.raises(RuntimeError, match="not finished shutting down"):
            session.conversation_runtime.forget_code_store(conversation.id)
        _clear_session_llm_cache(session)
        assert model.close_calls == 0
        tool.cleanup_release.set()
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in store.pending_tasks()), return_exceptions=True), 5)
        await asyncio.wait_for(model.closed.wait(), 5)
        await session.session_lifecycle.unload_conversation(conversation.id, reason="actual_cleanup_finished")
        assert tool.process.returncode is not None and tool.finished.is_set()
        assert not store.pending_tasks() and not store.live_tool_calls()
        assert conversation.id not in session.conversation_runtime._code_stores
        assert model.close_calls == 1
