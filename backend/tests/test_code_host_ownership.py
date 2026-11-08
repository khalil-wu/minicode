from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from backend.agent.code_host import _hosts
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.sdk import create_session
from backend.tests.test_code_execution import FixtureTool, ScriptModel, run_model
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.registry import ToolRegistry


def test_host_crash_drains_actual_nested_tools_and_retains_completed_writes(tmp_path):
    async def scenario():
        writer = FixtureTool("write_marker", effect=True)
        waiting = FixtureTool("held_reader", delay=60, count=1)
        connections = []
        def crash():
            connection = _hosts[asyncio.get_running_loop()]._connection
            connections.append(connection)
            connection.process.kill()
        waiting.on_start = crash
        model = ScriptModel("await tools.write_marker({}); await tools.held_reader({}); text('unreachable');")
        state, _, journal, _, owner = await run_model(tmp_path, model, [writer, waiting])
        assert model.reports[-1]["status"] == "failed"
        assert "CodeHostDisconnected" in model.reports[-1]["error"]
        assert writer.executions == waiting.executions == 1
        assert (tmp_path / "marker.txt").read_text() == "one write"
        assert waiting.finished.is_set()
        assert any(record.tool_name == "write_marker" and record.status == "success" for record in state.tool_calls)
        assert not owner.code_store.pending_tasks()
        assert connections[0].process.returncode is not None
    asyncio.run(scenario())


class Once(LLMAdapter):
    def __init__(self, code):
        self.code = code
        self.calls = 0

    async def simple_chat(self, *args, **kwargs):
        raise AssertionError("No auxiliary request")

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id=f"exec-{len(messages)}", name="tool_exec", arguments={"code": self.code, "yield_time_ms": 10000})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="The code finished.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop")


@pytest.mark.asyncio
async def test_sdk_turns_reuse_host_but_forks_do_not_share_json_values(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolExecTool())
    registry.register(ToolWaitTool())
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    config = AppConfig(llm=LLMSettings(api_key=""), agent=AgentSettings(code_mode_only=False, max_iterations=4))
    session = create_session(session_id="host-sdk", workspace_root=tmp_path, config=config,
        llm=Once("store('owner','parent'); text('stored');"), tool_registry=registry, artifact_store=artifacts,
        run_context=RunContext(agent_runtime=runtime))
    child = None
    async def collect(owner, code):
        async with aclosing(owner.query("Execute once.", llm=Once(code))) as stream:
            return [event async for event in stream]
    try:
        await collect(session, "store('owner','parent'); text('stored');")
        store, = session._code_stores.values()
        connection = store._host_session.host._connection
        assert connection.alive
        child = session.fork(session_id="host-sdk-child")
        await collect(child, "text(load('owner')); store('owner','child');")
        child_store, = child._code_stores.values()
        assert child_store.values == {"owner": "child"}
        assert child_store._host_session.host._connection is connection
        await collect(session, "store('copy',load('owner')); text(load('copy')); ")
        assert store.values == {"owner": "parent", "copy": "parent"}
        assert store._host_session.host._connection is connection
        await child.aclose()
        assert connection.alive
        await session.aclose()
        assert connection.process.returncode == 0
    finally:
        if child is not None:
            await child.aclose()
        await session.aclose()
        await artifacts.aclose()
        runtime.close(release_lease=True)
