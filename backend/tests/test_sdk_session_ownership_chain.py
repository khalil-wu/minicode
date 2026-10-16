import asyncio
from contextlib import aclosing

import pytest

from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.sdk import create_session, create_tool_registry, tool


class ToolModel(LLMAdapter):
    def __init__(self, name, arguments=None, entered=None, release=None):
        self.name = name
        self.arguments = arguments or {}
        self.entered = entered
        self.release = release

    async def simple_chat(self, messages, **kwargs):
        raise AssertionError("No auxiliary model request expected")

    async def stream_chat(self, messages, tools=None, metadata=None):
        if not any(message.role == "tool" for message in messages):
            if self.entered is not None:
                self.entered.set()
                await self.release.wait()
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                ToolCallEvent(id="call-" + self.name, name=self.name, arguments=self.arguments),
            ])
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="The requested observation is complete.", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE)


@pytest.fixture
def sdk_owner(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    config = AppConfig(llm=LLMSettings(api_key=""), agent=AgentSettings(code_mode_only=False, max_iterations=3))
    yield workspace, runtime, artifacts, config
    artifacts.shutdown()
    runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_fork_keeps_host_runtime_and_isolates_code_values(sdk_owner):
    workspace, runtime, artifacts, config = sdk_owner
    parent_entered, child_entered = asyncio.Event(), asyncio.Event()
    observed = {}

    @tool
    async def branch_value(label: str, context):
        assert context.window_binding_token == "fork-private-window-proof"
        assert context.run_context.window_binding_token == "fork-private-window-proof"
        context.run_context.code_store.put("branch", label)
        if label == "PARENT":
            parent_entered.set()
            await child_entered.wait()
        else:
            child_entered.set()
        observed[label] = context.run_context.code_store.values["branch"]
        return observed[label]

    parent = create_session(session_id="parent", workspace_root=workspace, config=config,
        llm=ToolModel("branch_value", {"label": "PARENT"}), artifact_store=artifacts,
        tool_registry=create_tool_registry(branch_value),
        run_context=RunContext(agent_runtime=runtime, window_binding_token="fork-private-window-proof"))
    child = parent.fork(session_id="child")

    async def collect(stream):
        async with aclosing(stream):
            return [event async for event in stream]

    parent_task = asyncio.create_task(collect(parent.query("Observe the parent value")))
    try:
        await asyncio.wait_for(parent_entered.wait(), 10)
        await asyncio.wait_for(collect(child.query("Observe the child value", llm=ToolModel("branch_value", {"label": "CHILD"}))), 10)
        await asyncio.wait_for(parent_task, 10)
        assert observed == {"PARENT": "PARENT", "CHILD": "CHILD"}
        assert child._query_kwargs["run_context"].agent_runtime is runtime
        assert child._query_kwargs["run_context"].window_binding_token == "fork-private-window-proof"
    finally:
        child_entered.set()
        await asyncio.gather(parent_task, return_exceptions=True)
        await child.aclose()
        await parent.aclose()


@pytest.mark.asyncio
async def test_close_stops_active_query_before_tools_execute(sdk_owner):
    workspace, runtime, artifacts, config = sdk_owner
    entered, release = asyncio.Event(), asyncio.Event()
    executed = []

    @tool
    async def after_close():
        executed.append(True)
        return "Executed"

    session = create_session(session_id="closing", workspace_root=workspace, config=config,
        llm=ToolModel("after_close", entered=entered, release=release), artifact_store=artifacts,
        tool_registry=create_tool_registry(after_close), run_context=RunContext(agent_runtime=runtime))

    async def collect():
        async with aclosing(session.query("Close this query")) as stream:
            return [event async for event in stream]

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await asyncio.wait_for(session.aclose(), 10)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.done()
        assert executed == []
        with pytest.raises(RuntimeError, match="closed"):
            await anext(session.query("A later prompt"))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await session.aclose()


@pytest.mark.asyncio
async def test_close_suspended_stream_does_not_cancel_its_consumer(sdk_owner):
    workspace, runtime, artifacts, config = sdk_owner
    session = create_session(workspace_root=workspace, config=config, llm=ToolModel("unused"),
        tool_registry=create_tool_registry(), artifact_store=artifacts, run_context=RunContext(agent_runtime=runtime))
    async with aclosing(session.query("Observe")) as stream:
        await anext(stream)
        await session.aclose()
        assert asyncio.current_task().cancelling() == 0
    with pytest.raises(RuntimeError, match="closed"):
        session.fork()


@pytest.mark.asyncio
async def test_sdk_node_kernel_persists_between_prompts_and_closes_before_environment(sdk_owner, monkeypatch, request):
    import json
    import shutil
    from backend.permissions.context import PermissionContext
    from backend.environments import EnvironmentManager
    from backend.workspace.executor_local import LocalExecutionEnvironmentBackend

    node = shutil.which("node")
    if node is None:
        pytest.skip("requires the actual installed Node runtime")
    monkeypatch.setenv("MINICODE_EDITOR_NODE", node)
    workspace, runtime, artifacts, config = sdk_owner
    backend = LocalExecutionEnvironmentBackend()
    environments = EnvironmentManager(workspace.parent / "sdk-node-environments", local_backend=backend)

    class PromptModel(ToolModel):
        invoked = False

        async def stream_chat(self, messages, tools=None, metadata=None):
            if not self.invoked:
                self.invoked = True
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                    ToolCallEvent(id="sdk-node-" + str(len(messages)), name="mcp__node_repl__js", arguments=self.arguments)])
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Node observation complete.", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE)

    run = RunContext(agent_runtime=runtime)
    approvals = []

    async def approve(call_id):
        marker = workspace / ("first-executed" if not approvals else "second-executed")
        assert not marker.exists()
        approvals.append(call_id)
        return {"action": "approve"}

    session = create_session(session_id="persistent-sdk-node", workspace_root=workspace, config=config,
        llm=PromptModel("node", {"code": "const fs=await import('node:fs');const sdkSaved=41;fs.writeFileSync('first-executed','41');nodeRepl.write(sdkSaved)"}),
        artifact_store=artifacts, tool_registry=create_tool_registry(), run_context=run,
        environment_manager=environments, permission_context=PermissionContext(mode="bypass"), approval_handler=approve)
    try:
        first = [event async for event in session.query("Remember the SDK value")]
        result, = [event for event in first if event.type == "tool_result"]
        assert result.data["summary"].splitlines()[-1] == "41"
        assert len(approvals) == 1 and (workspace / "first-executed").read_text() == "41"
        companion = session.node_repl_companions
        assert run.node_repl_owner is session and companion is not None
        client = companion.instances[companion.active_key].client
        original = client.process_handle
        pid = original.process.pid
        assert client.connected and original.accepted and not original.closed and not original.released
        second = [event async for event in session.query("Read the same SDK value", llm=PromptModel("node", {"code": "fs.writeFileSync('second-executed',String(sdkSaved+1));nodeRepl.write(sdkSaved+1)"}))]
        result, = [event for event in second if event.type == "tool_result"]
        assert result.data["summary"].splitlines()[-1] == "42"
        assert len(approvals) == 2 and (workspace / "second-executed").read_text() == "42"
        assert session.node_repl_companions is companion and client.process_handle is original
        assert original.process.pid == pid and client.connected and len(backend.process.processes) == 1
        thread = session._thread_environments
        close_environment = thread.aclose
        order = []

        async def close_original_environment():
            assert original.closed and original.released and client.thread_lease.released
            assert not backend.process.processes
            order.append("environment_after_original_node_ack")
            await close_environment()

        monkeypatch.setattr(thread, "aclose", close_original_environment)
        await session.aclose()
        assert order == ["environment_after_original_node_ack"]
        assert not companion.instances and not client.connected
        request.node.user_properties.append(("actual_sdk_node_owner", json.dumps({
            "pid": pid, "same_process_between_prompts": True, "bindings_preserved": True,
            "closed_ack_before_environment": True, "original_thread_lease_released": client.thread_lease.released,
            "approval_count": len(approvals), "execution_markers_absent_before_approval": True})))
    finally:
        await session.aclose()
        await environments.close()
