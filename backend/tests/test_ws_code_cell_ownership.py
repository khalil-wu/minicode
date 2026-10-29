"""Actual WebSocket core unload/model rotation retain the original code borrower."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import sys

import pytest

from backend.agent.runtime import AgentRuntime
from backend.config import AgentSettings, LLMSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.tests.test_code_cell_thread_lifetime import ProcessTool
from backend.tests.test_user_input_presentation_chain import session_for
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.base import ToolResult
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
async def ws_cell(tmp_path, monkeypatch, *, gated=False, complete_parent=True):
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
        if complete_parent:
            await asyncio.wait_for(task, 20)
            await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        await asyncio.wait_for(tool.started.wait(), 5)
        cell = next(iter(store.cells.values()))
        assert tool.process.returncode is None
        if complete_parent:
            assert not session.run_manager.run_tasks
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


class StreamingProcess(ProcessTool):
    async def execute(self, args, context=None):
        self.execution_task = asyncio.current_task()
        self.output_emitted = asyncio.Event()
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c",
            "from pathlib import Path; import time\n"
            "for gate, message in [('emit-output','ACTUAL_RETAINED_STDOUT'),('emit-output-2','ACTUAL_TRAILING_STDOUT')]:\n"
            " while not Path(gate).exists() and not Path('release').exists(): time.sleep(.01)\n"
            " if Path(gate).exists(): print(message, flush=True)\n"
            "while not Path('release').exists(): time.sleep(.01)\n",
            cwd=context.workspace_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.started.set()
        output = []

        async def read():
            while chunk := await self.process.stdout.readline():
                text = chunk.decode("utf-8")
                output.append(text)
                await context.emit_event("tool_output_delta", {"id": context.tool_call_id, "output": text, "stream": "stdout"})
                self.output_emitted.set()

        reading = asyncio.create_task(read())
        exited = asyncio.create_task(self.process.wait())
        cancelled = asyncio.create_task(context.cancel_event.wait())
        try:
            await asyncio.wait({exited, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled.done():
                return ToolResult("Cancelled", is_error=True, status="cancelled")
            await reading
            assert self.process.returncode == 0, (await self.process.stderr.read()).decode()
            return ToolResult("".join(output))
        finally:
            cancelled.cancel()
            if self.process.returncode is None:
                self.process.terminate()
            await self.process.wait()
            await asyncio.gather(reading, exited, cancelled, return_exceptions=True)
            self.finished.set()

@pytest.mark.asyncio
@pytest.mark.parametrize("start_next_turn", [False, True])
async def test_retained_cell_stdout_is_in_cold_history_before_result(tmp_path, monkeypatch, start_next_turn):
    from backend.conversations.repository import ConversationRepository
    from backend.tests import test_ws_code_cell_ownership as ownership

    monkeypatch.setattr(ownership, "ProcessTool", StreamingProcess)
    async with ownership.ws_cell(tmp_path, monkeypatch) as (session, conversation, model, tool, store, cell):
        original = session.conversation_repo.get_conversation(conversation.id)
        answer = original.transcript[-1]
        assert answer["terminal_status"] == "completed" and not cell.task.done()
        next_entered, next_release = asyncio.Event(), asyncio.Event()
        next_task = None

        async def later_turn(messages, tools=None, metadata=None):
            next_entered.set()
            await next_release.wait()
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="The later answer.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)

        try:
            if start_next_turn:
                monkeypatch.setattr(model, "stream_chat", later_turn)
                await session.start_agent_run("Start another independent turn.", conversation_id=conversation.id,
                    metadata={"user_message_id": "later-user", "assistant_message_id": "later-answer"})
                next_task = session.run_manager.run_tasks[conversation.id]
                await asyncio.wait_for(next_entered.wait(), 5)
            before_output = session.conversation_repo.get_conversation(conversation.id)
            (tmp_path / "workspace/emit-output").write_text("emit", encoding="utf-8")
            async with asyncio.timeout(5):
                while not any(event.get("type") == "tool_output_delta" and "ACTUAL_RETAINED_STDOUT" in event.get("output", "")
                    for event in session.ws.messages):
                    await asyncio.sleep(.01)
            cold = ConversationRepository(tmp_path / "conversations").get_conversation(conversation.id)
            retained = next(message for message in cold.transcript if message["id"] == answer["id"])
            leaf = next(record for record in retained["tool_calls"] if record["name"] == "held_process")
            assert "ACTUAL_RETAINED_STDOUT" in leaf["stdoutPreview"]
            assert leaf["status"] == "running" and not cell.task.done() and tool.process.returncode is None
            assert retained["content"] == answer["content"] and retained["terminal_status"] == "completed"
            assert cold.context_snapshot == before_output.context_snapshot
            assert not any(event.get("type") == "tool_result" and event.get("id") == leaf["id"] for event in session.ws.messages)
            if start_next_turn:
                assert session.run_manager.run_tasks[conversation.id] is next_task and not next_task.done()
                assert session._conversation_streams[conversation.id]["message_id"] == "later-answer"
        finally:
            (tmp_path / "workspace/release").write_text("complete", encoding="utf-8")
            await asyncio.wait_for(asyncio.shield(cell.task), 5)
            next_release.set()
            if next_task is not None:
                await asyncio.wait_for(next_task, 5)


@pytest.mark.asyncio
async def test_active_cell_trailing_stdout_flushes_while_process_and_parent_wait(tmp_path, monkeypatch):
    from backend.conversations.repository import ConversationRepository
    from backend.tests import test_ws_code_cell_ownership as ownership

    parent_waiting, parent_release = asyncio.Event(), asyncio.Event()
    original = ownership.CellModel.stream_chat

    async def hold_final(self, messages, tools=None, metadata=None):
        if self.step > 0:
            parent_waiting.set()
            await parent_release.wait()
        async for event in original(self, messages, tools=tools, metadata=metadata):
            yield event

    monkeypatch.setattr(ownership, "ProcessTool", StreamingProcess)
    monkeypatch.setattr(ownership.CellModel, "stream_chat", hold_final)
    async with ownership.ws_cell(tmp_path, monkeypatch, complete_parent=False) as (session, conversation, model, tool, store, cell):
        task = session.run_manager.run_tasks[conversation.id]
        try:
            await asyncio.wait_for(parent_waiting.wait(), 5)
            (tmp_path / "workspace/emit-output").write_text("emit", encoding="utf-8")
            await asyncio.wait_for(tool.output_emitted.wait(), 5)

            def persisted_preview():
                fresh = ConversationRepository(tmp_path / "conversations").get_conversation(conversation.id)
                for message in fresh.transcript:
                    for record in message.get("tool_calls", []):
                        if record.get("name") == "held_process":
                            return record.get("stdoutPreview", "")
                return ""

            async with asyncio.timeout(5):
                while "ACTUAL_RETAINED_STDOUT" not in persisted_preview():
                    await asyncio.sleep(.02)
            tool.output_emitted.clear()
            (tmp_path / "workspace/emit-output-2").write_text("emit", encoding="utf-8")
            await asyncio.wait_for(tool.output_emitted.wait(), 5)
            async with asyncio.timeout(3):
                while "ACTUAL_TRAILING_STDOUT" not in persisted_preview():
                    await asyncio.sleep(.02)
            assert not task.done() and not cell.task.done() and tool.process.returncode is None
            assert not any(event.get("type") == "tool_result" and event.get("call_source", {}).get("kind") == "code_mode"
                for event in session.ws.messages)
        finally:
            (tmp_path / "workspace/release").write_text("complete", encoding="utf-8")
            await asyncio.wait_for(asyncio.shield(cell.task), 5)
            parent_release.set()
            await asyncio.wait_for(task, 5)
