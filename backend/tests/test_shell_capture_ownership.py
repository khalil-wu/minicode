from __future__ import annotations

import asyncio
import threading

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.sdk import create_session
from backend.tools.registry import ToolRegistry
from backend.ws.execution_context import ConversationThreadRuntime


def test_run_metadata_does_not_scan_shell_and_explicit_selection_never_uses_host_default(monkeypatch):
    def forbidden():
        raise AssertionError("Metadata-only context must not discover shell executables")
    monkeypatch.setattr("backend.agent.run_context.default_user_shell", forbidden)
    assert RunContext().user_shell is None
    shell = {"name": "bash", "path": "/captured/bash"}
    assert RunContext(user_shell=shell).capture_user_shell() is shell


class Model(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Actual final result.", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages):
        return ""


def fixture(tmp_path):
    settings, budget = AgentSettings(max_iterations=2), TokenBudget()
    config = AppConfig(llm=LLMSettings(api_key="fixture"), agent=settings, token_budget=budget,
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), {}),)))
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    return config, artifacts, runtime, Model()


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_owned", [False, True])
async def test_actual_turns_retain_shell_selection_and_do_not_block_the_loop(tmp_path, monkeypatch, thread_owned):
    config, artifacts, runtime, model = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    captures = []
    shell = {"name": "bash", "path": "/owned/bash"}

    def discover():
        assert threading.get_ident() != loop_thread
        captures.append(True)
        entered.set()
        assert release.wait(4)
        return dict(shell)

    monkeypatch.setattr("backend.agent.run_context.default_user_shell", discover)
    thread = ConversationThreadRuntime("store", "shell-owner") if thread_owned else None
    session = AgentSession(llm=model, tool_registry=ToolRegistry(), artifact_store=artifacts,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), agent_settings=config.agent,
        token_budget=config.token_budget, context_builder=ContextBuilder(workspace_root=tmp_path))

    async def query(run):
        return [event async for event in QueryEngine().submit(QuerySubmission(user_message="Complete the requested turn", session=session,
            runtime=AgentLoopSessionContext(workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass"),
                run_context=run, metadata={"conversation_id": "shell-owner"})))]

    task = None
    try:
        for index in range(2):
            run = RunContext(agent_runtime=runtime, environment_thread_runtime=thread,
                model_execution=ModelExecutionSnapshot.capture(config, model))
            task = asyncio.create_task(query(run))
            if index == 0:
                assert await asyncio.to_thread(entered.wait, 4)
                assert not task.done()
                release.set()
            events = await task
            assert any(event.type == "done" and event.data.get("status") == "completed" for event in events)
            assert run.user_shell == {"name": "bash", "path": "/owned/bash"}
            run.user_shell["path"] = "/turn-local-edit"
            shell["path"] = "/later-system-default"
        assert len(captures) == 1
        assert (thread or session).user_shell["path"] == "/owned/bash"
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await session.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_sdk_session_and_fork_inherit_captured_shell_without_rescanning(tmp_path, monkeypatch):
    config, artifacts, runtime, model = fixture(tmp_path)
    captures = []

    def discover():
        captures.append(True)
        return {"name": "bash", "path": "/first/bash"}

    monkeypatch.setattr("backend.agent.run_context.default_user_shell", discover)
    parent = create_session(session_id="shell-sdk", workspace_root=tmp_path, config=config, llm=model,
        artifact_store=artifacts, tool_registry=ToolRegistry(), run_context=RunContext(agent_runtime=runtime),
        permission_context=PermissionContext(mode="bypass"))
    child = None
    try:
        for _ in range(2):
            events = [event async for event in parent.query("Finish this turn")]
            assert any(event.type == "done" and event.data.get("status") == "completed" for event in events)
        child = parent.fork(session_id="shell-child")
        events = [event async for event in child.query("Finish the child turn")]
        assert any(event.type == "done" and event.data.get("status") == "completed" for event in events)
        assert captures == [True]
        assert parent._user_shell == child._user_shell == {"name": "bash", "path": "/first/bash"}
        assert parent._user_shell is not child._user_shell
    finally:
        if child is not None:
            await child.aclose()
        await parent.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)
