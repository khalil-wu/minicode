from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from backend.agent.run_context import RunContext
from backend.extensions.loader import ExtensionLoader
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tests.test_model_execution_ownership import setup, execute, Model


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))


async def bind(fixture, root, factory):
    loaded = await ExtensionLoader(cwd=root).load_factory(factory)
    runner = loaded.runner
    fixture.host._extension_runtime_states["model-conv"]["runtime"] = runner
    fixture.host._bind_lifecycle_runtime_host_actions(runner,
        conversation=type("Conversation", (), {"id": "model-conv"})(),
        tool_registry=fixture.session.tool_registry, run_metadata={},
        run_context_builder=fixture.builder, run_llm=fixture.model, cancel_event=None,
        model_runtime=fixture.owner.model_execution.model_runtime,
        agent_session=fixture.session, run_context=fixture.owner)
    return runner


@pytest.mark.asyncio
async def test_query_hooks_select_child_model_without_writing_parent_preferences(tmp_path, monkeypatch):
    seen = []
    captured = []

    def factory(api):
        async def on_context(event, ctx):
            seen.append(ctx.model.id)
            captured.append(ctx)
            if len(seen) == 1:
                api.set_thinking_level("high")
        api.on("context", on_context)

    async def behavior(model, messages):
        if model._settings.reasoning_effort == "low":
            return ToolCallEvent(id="child-low-call", name="inspect_model", arguments={})
        return None

    fixture = setup(tmp_path, monkeypatch, behavior)
    parent = fixture.owner
    runner = await bind(fixture, tmp_path, factory)
    child_model = Model(replace(fixture.model._settings, model="model-b"), behavior)
    child_snapshot = replace(parent.model_execution, model="model-b", llm=child_model,
        config=replace(fixture.config, llm=child_model._settings,
            token_budget=replace(fixture.config.token_budget, total=32768)))
    child = RunContext(agent_runtime=parent.agent_runtime, execution_journal=parent.execution_journal,
        model_execution=child_snapshot, lifecycle_runtime=runner)
    fixture.owner = child
    fixture.session.llm = child_model
    fixture.builder.bind_llm(child_model)
    try:
        await execute(fixture, tmp_path)
        assert seen == ["model-b", "model-b"]
        assert fixture.inspector.observations == [("model-b", "model-b", "low", 32768)]
        assert parent.model_execution.thinking_level == "low"
        assert child.model_execution.thinking_level == "high"
        assert captured[0].model.id == "model-b"
        fixture.host.conversation_repo.update_model_selection.assert_not_called()
    finally:
        await asyncio.sleep(0)
        runner.invalidate()


@pytest.mark.asyncio
async def test_concurrent_tool_and_provider_callbacks_keep_separate_owners(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    def factory(api):
        async def callback(event, ctx):
            if ctx.model.id == "model-b":
                entered.set()
                await release.wait()
                api.set_thinking_level("high")
            else:
                await entered.wait()
                release.set()
            seen.append((ctx.model.id, ctx.thinking_level))
        api.on("before_provider_request", callback)
        api.on("tool_call", callback)

    fixture = setup(tmp_path, monkeypatch, None)
    runner = await bind(fixture, tmp_path, factory)
    child = RunContext(model_execution=replace(fixture.owner.model_execution, model="model-b"))
    tool_context = ToolExecutionContext(permission=PermissionContext(), run_context=child)
    try:
        await asyncio.gather(
            runner.emit_tool_call({"tool_name": "inspect_model", "tool_call_id": "child", "input": {}},
                context=runner.create_context(tool_context=tool_context)),
            runner.for_execution(fixture.owner).emit_before_provider_request({"model": "model-a"}),
        )
        assert sorted(seen) == [("model-a", "low"), ("model-b", "high")]
        assert fixture.owner.model_execution.thinking_level == "low"
        assert child.model_execution.thinking_level == "high"
        assert runner.runtime.execution_run_context is None
        assert not runner.errors
    finally:
        await asyncio.sleep(0)
        fixture.runtime.close(release_lease=True)
        runner.invalidate()
