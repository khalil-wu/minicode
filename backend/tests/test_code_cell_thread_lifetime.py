from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from backend.agent.code_execution_store import CodeExecutionStore
from backend.agent.context import ContextBuilder
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.atomic_io import await_task_despite_cancellation
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.registry import ToolRegistry


class ProcessTool(BaseTool):
    name = "held_process"
    description = "Run the fixture-owned process until its release file exists."
    permission = PermissionLevel.AUTO
    read_only = False
    mutates_workspace = True

    def __init__(self):
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = None
        self.finished = asyncio.Event()
        self.execution_task = None
        self.process = None

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}, "additionalProperties": False})

    async def execute(self, args, context=None):
        self.execution_task = asyncio.current_task()
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c",
            "from pathlib import Path; import time; print('process started',flush=True); "
            "\nwhile not Path('release').exists(): time.sleep(.02)\nprint('process completed',flush=True)",
            cwd=context.workspace_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.started.set()
        output_task = asyncio.create_task(self.process.communicate())
        cancelled = asyncio.create_task(context.cancel_event.wait())
        try:
            await asyncio.wait({output_task, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled.done():
                return ToolResult("Process cancellation requested.", is_error=True, status="cancelled")
            output, error = await output_task
            if context.cancel_event.is_set():
                return ToolResult(output.decode(), is_error=True, status="cancelled")
            assert self.process.returncode == 0, error.decode()
            return ToolResult(output.decode())
        finally:
            # The fixture owns its OS process even if Task.cancel arrives
            # after its CancelEvent has already started normal cleanup.
            async def stop_process():
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)
                if self.process.returncode is None:
                    self.cleanup_started.set()
                    if self.cleanup_release is not None:
                        await self.cleanup_release.wait()
                    self.process.terminate()
                    await self.process.wait()
                await asyncio.gather(output_task, return_exceptions=True)
                self.finished.set()
            await await_task_despite_cancellation(asyncio.create_task(stop_process()))


class TurnModel(LLMAdapter):
    def __init__(self):
        self._settings = LLMSettings(api_key="offline-fixture", provider="openai", model="code-lifetime")
        self.call = None
        self.step = 0
        self.state = None
        self.report = None

    async def simple_chat(self, *args, **kwargs):
        raise AssertionError("No auxiliary provider request is expected")

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.step += 1
        if self.step == 1:
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[self.call])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            record = next(record for record in self.state.tool_calls if record.tool_call_id == self.call.id)
            self.report = json.loads(record.result_payload["summary"])
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Turn completed; retain the background handle.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop")


def setup(tmp_path):
    tool, model = ProcessTool(), TurnModel()
    registry = ToolRegistry()
    for entry in (tool, ToolExecTool(), ToolWaitTool()):
        registry.register(entry)
    budget = TokenBudget(total=96000, response_reserve=4096)
    session = AgentSession(llm=model, tool_registry=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), owns_artifact_store=True,
        permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), tmp_path),
        agent_settings=AgentSettings(max_iterations=5, max_turn_seconds=15, stream_max_attempts=0), token_budget=budget,
        context_builder=ContextBuilder(llm=model, token_budget=budget, workspace_root=tmp_path, conversation_id="code-conv"))
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm",
        enable_lease_heartbeat=False)
    return tool, model, session, runtime


async def query_turn(tmp_path, model, session, runtime, call):
    model.call, model.step = call, 0
    state = AgentState(user_message="Use this turn's requested tool once, then finish.", conversation_id="code-conv", workspace_root=tmp_path)
    model.state = state
    owner = RunContext(agent_runtime=runtime)
    async with aclosing(QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
        runtime=AgentLoopSessionContext(session_id="code-lifetime", workspace_root=tmp_path,
            permission_context=PermissionContext(mode="bypass"), run_context=owner)))) as stream:
        async for _ in stream:
            pass
    return state, owner


def test_normal_final_retains_real_process_cell_and_next_turn_consumes_its_output(tmp_path):
    async def scenario():
        tool, model, session, runtime = setup(tmp_path)
        try:
            first, owner = await query_turn(tmp_path, model, session, runtime, ToolCallEvent(id="start", name="tool_exec",
                arguments={"code": "text('cell yielded'); const result=await tools.held_process({}); text(result.content);", "yield_time_ms": 1}))
            await asyncio.wait_for(tool.started.wait(), 5)
            cell_id = model.report["cell_id"]
            assert first.terminal_status == "completed" and model.report["status"] == "running"
            assert tool.process.returncode is None and cell_id in session.code_store.cells
            assert not session.lifecycle_cleanup_tasks
            (tmp_path / "release").write_text("complete", encoding="utf-8")
            second, _ = await query_turn(tmp_path, model, session, runtime, ToolCallEvent(id="join", name="tool_wait",
                arguments={"cell_id": cell_id, "yield_time_ms": 10000}))
            assert second.terminal_status == "completed"
            assert model.report["status"] == "completed" and "process completed" in "".join(model.report["output"])
            assert tool.process.returncode == 0
            await query_turn(tmp_path, model, session, runtime, ToolCallEvent(id="read-again", name="tool_wait",
                arguments={"cell_id": cell_id, "yield_time_ms": 1}))
            assert model.report["output"] == []
        finally:
            await session.aclose()
            runtime.close(release_lease=True)
    asyncio.run(scenario())


def test_explicit_session_close_cancels_a_previous_turns_live_process(tmp_path):
    async def scenario():
        tool, model, session, runtime = setup(tmp_path)
        try:
            await query_turn(tmp_path, model, session, runtime, ToolCallEvent(id="start", name="tool_exec",
                arguments={"code": "text('yielded'); await tools.held_process({});", "yield_time_ms": 1}))
            await asyncio.wait_for(tool.started.wait(), 5)
            cell_id = model.report["cell_id"]
            cell = session.code_store.cells[cell_id]
            assert not cell.cancel.is_set()
            assert tool.process.returncode is None
            await session.aclose()
            assert tool.process.returncode is not None, {
                "cell_status": cell.status, "cell_error": cell.error, "cancelled": cell.cancel.is_set(),
                "cleanup": len(session.lifecycle_cleanup_tasks), "runtimes": len(session.code_store.runtimes),
                "tasks": [task.get_coro().__qualname__ for task in asyncio.all_tasks() if not task.done()],
            }
            assert session.code_store.receipts[cell_id].status == "cancelled"
            assert not session.code_store.pending_tasks()
        finally:
            await session.aclose()
            runtime.close(release_lease=True)
    asyncio.run(scenario())


def test_turn_environment_is_retained_until_its_code_cell_releases_it():
    async def scenario():
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        store = CodeExecutionStore()

        class Snapshot:
            closed = False
            async def aclose(self):
                self.closed = True

        class Runtime:
            tool_context = SimpleNamespace(llm="captured-model")
            cells = {"cell": SimpleNamespace(task=task)}
            def pending_tasks(self):
                return {task} if not task.done() else set()

        snapshot, code = Snapshot(), Runtime()
        store.runtimes.add(code)
        owner = RunContext(turn_environments=snapshot, initial_turn_environments=snapshot, code_store=store, code_execution=code)
        await owner.close_environment_snapshots()
        assert owner.turn_environments is snapshot and not snapshot.closed
        assert store.pending_tasks("captured-model") and not owner.lifecycle_cleanup_tasks
        retirement = store.retirements[code]
        release.set()
        await retirement
        assert snapshot.closed and owner.turn_environments is None
        assert not store.pending_tasks()
    asyncio.run(scenario())


def test_session_close_owns_a_real_process_while_its_cancel_cleanup_is_gated(tmp_path):
    async def scenario():
        tool, model, session, runtime = setup(tmp_path)
        tool.cleanup_release = asyncio.Event()
        closing = None
        try:
            await query_turn(tmp_path, model, session, runtime, ToolCallEvent(id="start-gated", name="tool_exec",
                arguments={"code": "text('yielded'); await tools.held_process({});", "yield_time_ms": 1}))
            await asyncio.wait_for(tool.started.wait(), 5)
            cell_id = model.report["cell_id"]
            cell = session.code_store.cells[cell_id]
            closing = asyncio.create_task(session.aclose())
            await asyncio.wait_for(tool.cleanup_started.wait(), 5)
            assert tool.process.returncode is None and not tool.execution_task.done()
            assert tool.execution_task in session.lifecycle_cleanup_tasks
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(closing), 0.02)
            assert tool.execution_task.cancelling() == 1
            assert tool.execution_task in session.lifecycle_cleanup_tasks
            tool.cleanup_release.set()
            await asyncio.wait_for(closing, 5)
            assert tool.process.returncode is not None and tool.finished.is_set()
            assert session.code_store.receipts[cell_id].status == "cancelled"
            assert cell.error == "Script cancelled; inspect nested tool outcomes before retrying writes."
            assert not session.code_store.pending_tasks() and not session.lifecycle_cleanup_tasks
        finally:
            tool.cleanup_release.set()
            if closing is not None:
                await closing
            if tool.execution_task is not None:
                await asyncio.gather(tool.execution_task, return_exceptions=True)
            await session.aclose()
            runtime.close(release_lease=True)
    asyncio.run(scenario())
