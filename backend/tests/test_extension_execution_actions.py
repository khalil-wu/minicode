from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path

import pytest

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.tool_execution_gate import ToolExecutionGate
from backend.config import PermissionSettings
from backend.extensions.lifecycle_observer import lifecycle_observer_factory
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tests.test_extension_model_ownership import bind
from backend.tests.test_model_execution_ownership import setup, Model
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))


class Command(BaseTool):
    name = "run_command"
    read_only = False
    mutates_workspace = True
    permission = PermissionLevel.AUTO

    def __init__(self):
        self.calls = []

    def get_schema(self):
        return ToolSchema(self.name, "Write a fixture marker", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})

    async def execute(self, args, context=None):
        self.calls.append(context)
        (context.workspace_root / "marker.txt").write_text(args["command"], encoding="utf-8")
        await context.emit_event("done", {"reason": "not a model terminal"})
        return ToolResult("fixture command completed")


async def run_child(tmp_path, monkeypatch, factory, behavior, *, mode="bypass", history=(), command=None, settings=None, expect_cancel=False, approval=None, on_event=None):
    parent = setup(tmp_path, monkeypatch, behavior)
    parent.builder.append_user("PARENT_HISTORY_MUST_NOT_CHANGE")
    runner = await bind(parent, tmp_path, factory)
    workspace = tmp_path / "child"
    workspace.mkdir()
    registry = ToolRegistry()
    command = command or Command()
    registry.register(command)
    runner.bind_tool_registry(registry)
    builder = ContextBuilder(llm=parent.model, token_budget=parent.config.token_budget,
        agent_settings=settings or parent.config.agent, workspace_root=workspace)
    for role, content in history:
        (builder.append_user if role == "user" else builder.append_assistant)(content)
    journal = ExecutionJournal("child-actions", base_dir=tmp_path / "child-journal")
    owner = RunContext(agent_runtime=parent.runtime, lifecycle_runtime=runner, execution_journal=journal,
        model_execution=replace(parent.owner.model_execution, config=replace(parent.config, agent=settings or parent.config.agent)))
    session = AgentSession(llm=parent.model, tool_registry=registry, artifact_store=parent.session.artifact_store,
        permission_checker=PermissionChecker(PermissionSettings(auto_allow=["wrapper"], require_confirm=["run_command"]), workspace),
        agent_settings=settings or parent.config.agent, token_budget=parent.config.token_budget,
        context_builder=builder, lifecycle_observer_factory=lifecycle_observer_factory, approval_handler=approval)
    state = AgentState(user_message="Check the requested extension behavior", workspace_root=workspace, conversation_id="conv_child_actions")
    events = []
    try:
        async with asyncio.timeout(12), aclosing(QueryEngine().submit(QuerySubmission(session=session, state=state,
            user_message=state.user_message, runtime=AgentLoopSessionContext(session_id="child-actions", task_id="child-actions",
                workspace_root=workspace, permission_context=PermissionContext(mode=mode), run_context=owner,
                metadata={"assistant_message_id": "assistant_actions"})))) as stream:
            try:
                async for event in stream:
                    events.append(event)
                    if on_event is not None:
                        on_event(event)
            except asyncio.CancelledError:
                if not expect_cancel or asyncio.current_task().cancelling():
                    raise
        return parent, builder, journal, owner, state, events, command
    finally:
        await session.aclose()
        parent.runtime.close(release_lease=True)
        runner.invalidate()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["body", "observer"])
@pytest.mark.parametrize("requires_approval", [False, True])
async def test_exec_uses_child_scope_and_canonical_facts_without_model_history_or_deadlock(tmp_path, monkeypatch, origin, requires_approval):
    observations = []
    approval_seen = asyncio.Event()
    approved = []
    async def approval(call_id):
        assert approval_seen.is_set(), "The approval must be visible before its handler waits for a response"
        approved.append(call_id)
        return {"action": "approve"}
    def on_event(event):
        if event.type == "approval_request":
            approval_seen.set()

    def factory(api):
        async def invoke(ctx):
            observations.append(ctx.cwd)
            return await api.exec("fixture-write")

        async def body(params, ctx):
            return await invoke(ctx) if origin == "body" else "wrapper completed"

        async def observe(event, ctx):
            if event.get("tool_name") == "wrapper":
                await invoke(ctx)

        api.register_tool({"name": "wrapper", "description": "Run a nested fixture command",
            "parameters": {"type": "object"}, "execute": body})
        if origin == "observer":
            api.on("tool_execution_start", observe)

    async def behavior(model, messages):
        if len(model.inputs) == 1:
            return ToolCallEvent(id="wrapper-call", name="wrapper", arguments={})
        assert [message.name for message in messages if message.role == "tool"] == ["wrapper"]
        return None

    parent, builder, journal, owner, state, events, command = await run_child(tmp_path, monkeypatch, factory, behavior,
        mode="confirm" if requires_approval else "bypass", approval=approval if requires_approval else None, on_event=on_event)
    assert state.terminal_status == "completed", (state.stopped_reason, events)
    assert (tmp_path / "child" / "marker.txt").read_text() == "fixture-write"
    assert not (tmp_path / "marker.txt").exists()
    assert observations == [str(tmp_path / "child")]
    assert len(command.calls) == 1
    assert len(approved) == int(requires_approval)
    assert command.calls[0].run_context is owner
    nested = [event for event in events if event.type == "tool_result" and event.data.get("call_source", {}).get("kind") == "extension"]
    assert len(nested) == 1
    assert not any(event.type == "done" and event.data.get("reason") == "not a model terminal" for event in events)
    assert [message["name"] for message in journal.reconstruct_history() if message["role"] == "tool"] == ["wrapper"]
    assert [message.content for message in parent.builder._history] == ["PARENT_HISTORY_MUST_NOT_CHANGE"]


@pytest.mark.asyncio
async def test_extension_data_and_labels_are_durable_private_and_fork_independent(tmp_path, monkeypatch):
    data = "PRIVATE_EXTENSION_STATE_" * 300
    retained = []

    def factory(api):
        def before(event, ctx):
            for count in range(8):
                entry = api.append_entry("counter", {"count": count, "private": data})
            api.set_label(entry, "bookmark")
            api.set_session_name("Child extension name")
            retained.append(ctx.session_manager)
            api.send_message({"content": "VISIBLE_CUSTOM_MESSAGE"})
        api.on("before_agent_start", before)
        api.on("agent_end", lambda event, ctx: api.append_entry("end", {"done": True}))

    async def behavior(model, messages):
        sent = json.dumps([message.content for message in messages])
        assert "VISIBLE_CUSTOM_MESSAGE" in sent
        assert "PRIVATE_EXTENSION_STATE" not in sent and "bookmark" not in sent
        return None

    parent, builder, journal, owner, state, events, _ = await run_child(tmp_path, monkeypatch, factory, behavior)
    assert state.terminal_status == "completed", (state.stopped_reason, events)
    entries = builder.extension_state["entries"]
    assert len(entries) == 9 and entries[-1]["custom_type"] == "end"
    assert retained[0].get_label(entries[-2]["id"]) == "bookmark"
    assert retained[0].get_session_name() == "Child extension name"
    assert parent.builder.extension_state == {}
    stored = [event for event in journal.read_events() if event.payload.get("lifecycle") == "extension_state_delta"]
    assert len(stored) == 3  # initial data, message consumption, end-hook data
    restored = ContextBuilder()
    restored.load_snapshot(journal.reconstruct_context_snapshot())
    assert restored.extension_state == builder.extension_state
    fork = clone_context_builder(restored)
    fork.extension_state["labels"][entries[-2]["id"]] = "fork-label"
    assert restored.extension_state["labels"][entries[-2]["id"]] == "bookmark"
    projections = journal.unprojected_terminal_projections()
    assert projections[-1]["context_snapshot"]["extension_state"]["entries"][-1]["custom_type"] == "end"


@pytest.mark.asyncio
async def test_tool_selection_and_abort_stay_on_calling_query(tmp_path, monkeypatch):
    def factory(api):
        def before(event, ctx):
            assert "run_command" in {tool["name"] for tool in api.get_all_tools()}
            api.set_active_tools([])
            assert api.get_active_tools() == []
            ctx.abort()
        api.on("before_agent_start", before)

    async def behavior(model, messages):
        raise AssertionError("Aborted query must not reach the model")

    parent, builder, journal, owner, state, events, _ = await run_child(tmp_path, monkeypatch, factory, behavior, expect_cancel=True)
    assert state.terminal_status == "cancelled", events
    assert parent.session.active_tool_names is None
    assert not parent.model.inputs
    with pytest.raises(RuntimeError, match="ended"):
        owner.extension_actions.abort()


@pytest.mark.asyncio
async def test_gate_resumes_after_nested_parallel_writers_and_cancellation():
    gate = ToolExecutionGate(limit=0, initial_completed=0)
    order = []

    async def nested(label):
        async with gate.suspend():
            async with gate.hold(read_only=False):
                order.append(label)
                await asyncio.sleep(0)

    async with asyncio.timeout(2):
        outside = asyncio.Event()
        async def outsider():
            async with gate.hold(read_only=False):
                outside.set()
        async with gate.hold(read_only=False):
            await asyncio.gather(nested("a"), nested("b"))
            waiting = asyncio.create_task(outsider())
            await asyncio.sleep(0)
            assert not outside.is_set()
        await waiting
        assert outside.is_set()
        assert order == ["a", "b"]
        entered = asyncio.Event()
        async def cancelled():
            async with gate.hold(read_only=False):
                async with gate.suspend():
                    entered.set()
                    await asyncio.Event().wait()
        task = asyncio.create_task(cancelled())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with gate.hold(read_only=False):
            order.append("after-cancel")
        assert order[-1] == "after-cancel"


@pytest.mark.asyncio
async def test_nested_exec_uses_child_plan_permission_instead_of_parent_window(tmp_path, monkeypatch):
    def factory(api):
        async def body(params, ctx):
            return await api.exec("forbidden-write")
        api.register_tool({"name": "wrapper", "description": "Check a denied nested call", "read_only": True,
            "parameters": {"type": "object"}, "execute": body})

    async def behavior(model, messages):
        if len(model.inputs) == 1:
            return ToolCallEvent(id="wrapper-call", name="wrapper", arguments={})
        return None

    _, _, journal, _, _, events, command = await run_child(tmp_path, monkeypatch, factory, behavior, mode="plan")
    assert not command.calls
    assert not (tmp_path / "child" / "marker.txt").exists()
    assert any(event.type == "tool_result" and event.data.get("is_error") for event in events)
    assert not journal.unresolved_tool_uses()


@pytest.mark.asyncio
async def test_in_turn_compaction_uses_child_history_and_emits_commit(tmp_path, monkeypatch):
    summaries = []
    async def summarize(self, messages, **kwargs):
        summaries.append(messages)
        return "Historical assistant work has been summarized."
    monkeypatch.setattr(Model, "simple_chat", summarize)

    def factory(api):
        async def compact(params, ctx):
            usage = ctx.get_context_usage()
            assert usage["contextWindow"] == 96000 and usage["tokens"] > 0
            return await ctx.compact({"customInstructions": "Preserve all user constraints"})
        api.register_tool({"name": "compact_owned", "description": "Compact the calling query", "read_only": True,
            "parameters": {"type": "object"}, "execute": compact})

    async def behavior(model, messages):
        if len(model.inputs) == 1:
            return ToolCallEvent(id="compact-call", name="compact_owned", arguments={})
        return None

    from backend.config import AgentSettings
    settings = AgentSettings(max_iterations=4, max_turn_seconds=20, compaction_keep_recent_tokens=64)
    history = [(role, f"old {index}: " + "details " * 100) for index in range(12) for role in ("user", "assistant")]
    parent, builder, journal, _, state, events, _ = await run_child(tmp_path, monkeypatch, factory, behavior, history=history, settings=settings)
    assert state.terminal_status == "completed", (state.stopped_reason, events)
    assert len(summaries) == 1
    assert builder.export_snapshot()["compaction_count"] == 1
    assert [message.content for message in parent.builder._history] == ["PARENT_HISTORY_MUST_NOT_CHANGE"]
    assert sum(event.type == "context_compacted" for event in events) == 1
    assert any(event.payload.get("lifecycle") == "compaction_committed" for event in journal.read_events())


def test_fork_copies_extension_state_but_not_parent_delivery_queues():
    from backend.tools.subagent_support import _fork_snapshot_for_child
    builder = ContextBuilder()
    builder.append_user("parent")
    builder.extension_state = {"entries": [{"id": "entry", "data": {"value": 1}}],
        "pending_messages": ["pending-parent-context"], "followups": [{"content": "pending-parent-input"}]}
    fork = builder.fork_from(-1)
    child = _fork_snapshot_for_child({"_context_builder": builder}, "all")
    for state in (fork.extension_state, child["extension_state"]):
        assert "pending_messages" not in state and "followups" not in state
        assert state["entries"][0]["id"] == "entry"
    assert builder.extension_state["followups"][0]["content"] == "pending-parent-input"


@pytest.mark.asyncio
async def test_borrowed_adapter_is_retained_by_the_query_after_parent_exit(tmp_path, monkeypatch):
    from backend.ws.agent_runner import _clear_session_llm_cache, _lease_session_llm_for_task
    from backend.tests.test_model_execution_ownership import execute

    entered, release_model, release_parent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def behavior(model, messages):
        entered.set()
        await release_model.wait()
        return None
    fixture = setup(tmp_path, monkeypatch, behavior)
    host = fixture.host
    host._llm_adapter_cache = {"initial": fixture.model}
    parent_task = asyncio.create_task(release_parent.wait())
    _lease_session_llm_for_task(host, fixture.model, parent_task)
    fixture.owner.retain_model = lambda adapter, task: _lease_session_llm_for_task(host, adapter, task)
    child_task = asyncio.create_task(execute(fixture, tmp_path))
    try:
        await entered.wait()
        _clear_session_llm_cache(host)
        release_parent.set()
        await parent_task
        await asyncio.sleep(0)
        assert not fixture.model.closed
        release_model.set()
        await child_task
        await asyncio.sleep(0)
        await asyncio.gather(*getattr(host, "_llm_close_tasks", ()))
        assert fixture.model.closed
    finally:
        release_parent.set()
        release_model.set()
        await asyncio.gather(parent_task, child_task, return_exceptions=True)
