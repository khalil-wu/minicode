from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys

import psutil
import pytest
import pytest_asyncio

from backend.agent.query_engine import AgentSession
from backend.config import AgentSettings, AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.sandbox import SandboxPolicy
from backend.tests.test_subagent_startup_lifecycle import _launch, _prepare_git, _RecordingLLM, startup_case
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool
from backend.tools.monitor_tool import MonitorTool
from backend.tools.swarm_tools import FollowupTaskTool
from backend.tools.toolsets import SESSION_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy


class _CommandProbe(BaseTool):
    name = "thread_command_probe"
    description = "Start or poll the isolated fixture's real Python command."
    permission = PermissionLevel.AUTO
    mutates_workspace = True

    def __init__(self):
        self.commands = {}
        self.contexts = {}
        self.reads = {}

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "poll"]}}, "required": ["action"]})

    async def execute(self, args, context=None):
        child_id = context.session_id
        self.contexts[child_id] = context
        manager = context.background_manager
        if args["action"] == "start":
            command = await manager.run_background(
                "isolated Python thread lifetime probe", argv=[sys.executable, "-u", "-c",
                    "from pathlib import Path; import time; print('READY',flush=True)\n"
                    "while not Path('followup').exists(): time.sleep(.02)\n"
                    "print('FOLLOWUP',flush=True)\n"
                    "while not Path('release').exists(): time.sleep(.02)\n"
                    "print('EXIT',flush=True)"],
                cwd=str(context.workspace_root), conversation_id=context.command_scope_id,
                task_id=child_id, sandbox_policy=SandboxPolicy.bypass(timeout=60),
            )
            self.commands[child_id] = command
            await manager.wait_for_startup(command.command_id, conversation_id=context.command_scope_id)
            await manager.wait(command.command_id, conversation_id=context.command_scope_id, wait_ms=5000, cursor=0)
            assert command.status == "running" and command.pid is not None
            assert "READY" in manager.get_output_chunk(command.command_id,
                conversation_id=context.command_scope_id, cursor=0, max_chars=1000)[0]
            return ToolResult(content=command.command_id)
        command = self.commands[child_id]
        result = await MonitorTool().execute({"command_id": command.command_id,
            "action": "status", "cursor": 0, "yield_time_ms": 5000}, context)
        self.reads[child_id] = result
        return result


class _CommandModel(LLMAdapter):
    def __init__(self):
        self.calls = 0

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        if self.calls % 2:
            action = "poll" if any("Poll the retained command" in str(message.content) for message in messages) else "start"
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id=f"command-{self.calls}", name="thread_command_probe", arguments={"action": action})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="The child turn finished.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages):
        raise AssertionError("No extra model request is expected")


@pytest_asyncio.fixture
async def lifetime_case(startup_case):
    case = startup_case
    probe = _CommandProbe()
    case.tool._llm_provider = _CommandModel()
    case.context.tool_registry.register(probe)
    case.context.tool_registry.register(MonitorTool())
    case.context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = ToolsetPolicy.from_iterables(
        enabled_toolsets=(), enabled_tools=["task", "thread_command_probe", "monitor"],
    )
    case.probe = probe
    try:
        yield case
    finally:
        await case.runtime.aclose(release_lease=True)
        await case.hooks.finalize_async_hooks()


@pytest.mark.asyncio
async def test_completed_parent_and_child_keep_the_same_process_service_for_followup(lifetime_case):
    case = lifetime_case
    child_id, first = await _launch(case, "foreground")
    assert first.status == "completed"
    session = case.runtime.subagent_session(child_id)
    command = case.probe.commands[child_id]
    manager, pid = session._owned_commands, command.pid
    old = case.runtime.get_subagent(child_id)
    assert not session.closed and psutil.pid_exists(pid)
    assert case.runtime.active_subagent_snapshot() == [{"id": child_id, "status": "completed",
        "cleanup_pending": False, "background_commands": 1, "background_code": False}]

    case.runtime.commit_terminal("parent", "completed")
    case.runtime.start_run(run_id="next-parent", conversation_id="conversation")
    case.context.metadata["run_id"] = "next-parent"
    Path(command.cwd, "followup").write_text("continue", encoding="utf-8")
    reply = await FollowupTaskTool().execute(
        {"target": child_id, "message": "Poll the retained command and finish this turn."}, case.context)
    assert not reply.is_error, reply.content
    await asyncio.wait_for(case.runtime.wait_for_subagent_execution(child_id), 15)
    assert case.runtime.get_subagent(child_id).status == "completed"
    assert case.runtime.get_subagent(child_id).mailbox_epoch == old.mailbox_epoch + 1
    assert case.runtime.subagent_session(child_id) is session and session._owned_commands is manager
    assert command.pid == pid and psutil.pid_exists(pid) and command.status == "running"
    assert "FOLLOWUP" in case.probe.reads[child_id].content
    assert await case.runtime.close_subagent_session(child_id)
    assert case.runtime.subagent_session(child_id) is None and session.closed
    assert command.status == "cancelled" and not command.cleanup_pending
    assert not psutil.pid_exists(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "conversation", "runtime"])
async def test_shutdown_closes_an_idle_child_service_and_exact_process(lifetime_case, scope):
    case = lifetime_case
    child_id, result = await _launch(case, "foreground")
    assert result.status == "completed"
    command = case.probe.commands[child_id]
    session = case.runtime.subagent_session(child_id)
    if scope == "session":
        assert await case.runtime.stop_subagent_tasks_for_session("session")
    elif scope == "conversation":
        assert await case.runtime.stop_subagent_tasks_for_conversation("conversation")
    else:
        assert await case.runtime.aclose(release_lease=False)
        assert not case.runtime._lease_lost
    assert case.runtime.subagent_session(child_id) is None and session.closed
    assert command.status == "cancelled" and not command.cleanup_pending
    assert not psutil.pid_exists(command.pid)


@pytest.mark.asyncio
async def test_capacity_eviction_closes_the_old_service_before_admitting_another(lifetime_case):
    case = lifetime_case
    child_id, result = await _launch(case, "foreground")
    assert result.status == "completed"
    old_session = case.runtime.subagent_session(child_id)
    command = case.probe.commands[child_id]
    case.runtime._subagent_limits["conversation"] = 1
    case.runtime.start_subagent(subagent_id="next-child", parent_run_id="parent", agent_type="worker")
    new_session = await case.runtime.acquire_subagent_session("next-child", lambda: AgentSession(
        llm=case.tool._llm_provider, tool_registry=case.context.tool_registry,
        artifact_store=case.tool._artifact_store, permission_checker=old_session.permission_checker,
        agent_settings=AgentSettings(max_iterations=2), token_budget=old_session.token_budget),
        conversation_id="conversation")
    assert old_session.closed and not psutil.pid_exists(command.pid)
    assert not command.cleanup_pending and case.runtime.subagent_session(child_id) is None
    assert case.runtime.subagent_session("next-child") is new_session
    case.runtime.complete_subagent("next-child", "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True], ids=["clean", "user-edits"])
async def test_worktree_outlives_turn_and_process_then_retires_without_losing_edits(lifetime_case, changed):
    case = lifetime_case
    _prepare_git(case.workspace)
    child_id, result = await _launch(case, "foreground", isolation="worktree")
    assert result.status == "completed", result.content
    session = case.runtime.subagent_session(child_id)
    worktree, command = session.worktree, case.probe.commands[child_id]
    assert worktree.worktree_path.exists() and Path(command.cwd) == worktree.worktree_path
    if changed:
        (worktree.worktree_path / "draft.txt").write_text("user work\n", encoding="utf-8")
    assert await case.runtime.close_subagent_session(child_id)
    assert not psutil.pid_exists(command.pid) and not command.cleanup_pending
    assert worktree.worktree_path.exists() is changed
    if changed:
        assert (worktree.worktree_path / "draft.txt").read_text(encoding="utf-8") == "user work\n"
    resource, = case.runtime.get_subagent(child_id).cleanup_resources
    assert resource["state"] == ("retained" if changed else "released")


@pytest.mark.asyncio
async def test_scope_shutdown_leaves_another_conversations_process_untouched(lifetime_case):
    case = lifetime_case
    first_id, result = await _launch(case, "foreground")
    assert result.status == "completed"
    case.runtime.start_run(run_id="peer-parent", conversation_id="peer-conversation")
    peer_context = replace(case.context, conversation_id="peer-conversation", session_id="peer-session",
        task_id="peer-task", metadata={**case.context.metadata, "run_id": "peer-parent"})
    peer = await case.tool.execute({"description": "Peer command", "prompt": "Start the peer command."}, peer_context)
    peer_id = peer.runtime_metadata["subagent_id"]
    peer_session, peer_command = case.runtime.subagent_session(peer_id), case.probe.commands[peer_id]
    assert peer.status == "completed" and psutil.pid_exists(peer_command.pid)
    assert await case.runtime.stop_subagent_tasks_for_conversation("conversation")
    assert case.runtime.subagent_session(first_id) is None
    assert case.runtime.subagent_session(peer_id) is peer_session
    assert not peer_session.closed and psutil.pid_exists(peer_command.pid)
    assert peer_command.status == "running" and not peer_command.cleanup_pending


class _ChildCellModel(LLMAdapter):
    def __init__(self, code):
        self._settings = LLMSettings(api_key="offline-fixture", provider="openai", model="gpt-5.4", reasoning_effort="off")
        self.calls = 0
        self.code = code
        self.new_turn, self.finish_turn = asyncio.Event(), asyncio.Event()

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="start-child-cell", name="tool_exec", arguments={"code": self.code, "yield_time_ms": 1})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
            return
        if self.calls > 2:
            self.new_turn.set()
            await self.finish_turn.wait()
        yield StreamEvent(StreamEventType.TEXT_CHUNK, content="The child turn finished.", phase="final_answer")
        yield StreamEvent(StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages):
        raise AssertionError("No extra model request is expected")

    def supported_reasoning_efforts(self):
        return ("off",)


@pytest.mark.asyncio
async def test_old_cell_evidence_keeps_its_turn_and_does_not_change_the_resumed_worker(lifetime_case):
    from backend.tests.test_code_cell_thread_lifetime import ProcessTool

    case = lifetime_case
    process_tool = ProcessTool()
    model = _ChildCellModel("const result=await tools.held_process({}); text(result.content);")
    case.tool._llm_provider = model
    case.context.tool_registry.register(process_tool)
    case.context.tool_registry.register(ToolExecTool())
    case.context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = ToolsetPolicy.from_iterables(
        enabled_toolsets=(), enabled_tools=["task", "tool_exec", "held_process"],
    )
    try:
        child_id, first = await _launch(case, "foreground")
        assert first.status == "completed", first.content
        await asyncio.wait_for(process_tool.started.wait(), 5)
        session = case.runtime.subagent_session(child_id)
        cell, = session.code_store.cells.values()
        old = case.runtime.get_subagent(child_id)
        old_turn = cell.parent.turn_id
        facts = [event.to_dict() for event in case.runtime.execution_journal(child_id).read_events()]
        assert not any(event["event_type"] == "tool_result"
            and event["payload"]["tool_call_id"] in cell.pending_tools for event in facts)
        terminal = next(event["payload"] for event in reversed(facts) if event["event_type"] == "terminal")
        assert any(use["tool_call_id"] in cell.pending_tools for use in terminal["unresolved_tool_uses"])
        assert terminal["uncertain_tool_uses"] == [] and not terminal["manual_recovery_required"]
        followup = await FollowupTaskTool().execute(
            {"target": child_id, "message": "Continue this same child thread."}, case.context)
        assert not followup.is_error, followup.content
        await asyncio.wait_for(model.new_turn.wait(), 10)
        interrupted = case.workers[-1]
        assert case.runtime.cancel_subagent_task(child_id, reason="interrupt only the new turn") == "cancelled"
        await asyncio.gather(interrupted, return_exceptions=True)
        assert case.runtime.get_subagent(child_id).status == "cancelled"
        assert process_tool.process.returncode is None and not cell.cancel.is_set()
        facts = [event.to_dict() for event in case.runtime.execution_journal(child_id).read_events()]
        assert not any(event["event_type"] == "tool_result"
            and event["payload"]["tool_call_id"] in cell.pending_tools for event in facts)
        model.new_turn = asyncio.Event()
        followup = await FollowupTaskTool().execute(
            {"target": child_id, "message": "Continue after interrupting only the intervening turn."}, case.context)
        assert not followup.is_error, followup.content
        await asyncio.wait_for(model.new_turn.wait(), 10)
        current = case.runtime.get_subagent(child_id)
        assert current.mailbox_epoch == old.mailbox_epoch + 2 and current.status == "running"
        before = (current.current_tool, current.waiting_on, current.last_progress_at, current.model)
        (case.workspace / "release").write_text("finish original cell", encoding="utf-8")
        await asyncio.wait_for(asyncio.shield(cell.task), 10)
        assert process_tool.process.returncode == 0 and cell.status == "completed", cell.error
        late = next(payload for kind, payload in reversed(case.events)
            if kind == "subagent.progress" and payload.get("call_source", {}).get("cell_id") == cell.id
            and payload.get("source_event_type") == "tool_result")
        assert late["mailbox_epoch"] == old.mailbox_epoch and late["agent_path"] == old.agent_path
        assert (late["model"], late["provider"], late["reasoning_effort"]) == (
            cell.parent.model_execution.model, cell.parent.model_execution.provider,
            cell.parent.model_execution.thinking_level)
        assert any(event["event_type"] == "tool_result" for event in late["transcript_delta"]["events"])
        facts = [event.to_dict() for event in case.runtime.execution_journal(child_id).read_events()]
        result = next(event for event in facts if event["event_type"] == "tool_result"
            and event["payload"].get("call_source", {}).get("cell_id") == cell.id)
        assert result["payload"]["turn_id"] == old_turn
        current = case.runtime.get_subagent(child_id)
        assert current.status == "running" and current.mailbox_epoch == old.mailbox_epoch + 2
        assert (current.current_tool, current.waiting_on, current.last_progress_at, current.model) == before
    finally:
        model.finish_turn.set()
        await asyncio.gather(*case.workers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["foreground", "background"])
async def test_residency_does_not_evict_a_real_active_sleep_turn(lifetime_case, monkeypatch, delivery):
    from backend.tools.sleep_tool import SleepTool

    case = lifetime_case
    case.context.run_context.multi_agent_version = "v2"
    entered, settled = asyncio.Event(), asyncio.Event()
    sleep = SleepTool()
    original = sleep.execute
    async def real_sleep(args, context=None):
        entered.set()
        try:
            return await original(args, context)
        finally:
            settled.set()
    monkeypatch.setattr(sleep, "execute", real_sleep)
    class SleepModel(_RecordingLLM):
        async def stream_chat(self, messages, tools=None, metadata=None):
            self.calls += 1
            assert "sleep" in {tool["function"]["name"] for tool in tools}
            if self.calls > 1:
                yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Sleep turn ended.", phase="final_answer")
                yield StreamEvent(StreamEventType.DONE, finish_reason="stop")
                return
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="active-sleep", name="sleep", arguments={"duration_ms": 30000})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
    model = SleepModel()
    # This lifecycle fixture intentionally exercises an active sleep. The
    # generic fixture model does not advertise Source's clock capability.
    # Select Source's explicit always_on mode instead of waiting for a gated
    # request to finish without ever entering the real sleep tool.
    config = AppConfig(llm=LLMSettings(api_key="offline-fixture"), config_layer_stack=ConfigLayerStack((ConfigLayer(
        ConfigLayerSource("user"), {"features": {"sleep_tool": {"mode": "always_on"}}},
    ),)))
    case.context.model_execution = ModelExecutionSnapshot.capture(config, model)
    case.context.run_context.model_execution = case.context.model_execution
    case.tool._llm_provider = model
    case.context.tool_registry.register(sleep)
    case.context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = ToolsetPolicy.from_iterables(
        enabled_toolsets=(), enabled_tools=["task", "sleep"],
    )
    parent = asyncio.current_task()
    launching = asyncio.create_task(_launch(case, delivery))
    if delivery == "background":
        child_id, result = await launching
        assert not result.is_error, result.content
    await asyncio.wait_for(entered.wait(), 10)
    child_id = next(reversed(case.runtime._subagents))
    session = case.runtime.subagent_session(child_id)
    worker = case.runtime._subagent_tasks[child_id]
    assert worker is not parent and worker is not launching
    assert session.active_turn and not settled.is_set()
    case.runtime._subagent_limits["conversation"] = 1
    case.runtime.start_subagent(subagent_id="after-sleep", parent_run_id="parent", agent_type="worker", pending=True)
    with pytest.raises(RuntimeError, match="no idle thread"):
        await case.runtime.acquire_subagent_session("after-sleep", lambda: session, conversation_id="conversation")
    assert not session.closed and not settled.is_set()
    assert await case.runtime.close_subagent_session(child_id)
    assert session.closed and settled.is_set()
    assert not parent.cancelling()
    if delivery == "foreground":
        with pytest.raises(asyncio.CancelledError):
            await launching
    case.runtime.complete_subagent("after-sleep", "completed")


@pytest.mark.asyncio
async def test_parent_interrupt_closes_linked_idle_services_and_preserves_detached_peer(lifetime_case):
    case = lifetime_case
    case.context.run_context.multi_agent_version = "v2"
    linked_id, result = await _launch(case, "foreground")
    assert result.status == "completed", result.content
    detached_id, result = await _launch(case, "background")
    assert not result.is_error, result.content
    await asyncio.gather(*case.workers)
    linked = case.probe.commands[linked_id]
    detached = case.probe.commands[detached_id]
    detached_session = case.runtime.subagent_session(detached_id)
    assert await case.runtime.stop_subagent_tasks_for_task("parent-task")
    assert case.runtime.subagent_session(linked_id) is None and not psutil.pid_exists(linked.pid)
    assert case.runtime.subagent_session(detached_id) is detached_session
    assert not detached_session.closed and psutil.pid_exists(detached.pid)
    assert detached.status == "running" and not detached.cleanup_pending


@pytest.mark.asyncio
async def test_residency_preserves_unread_trigger_turn_mail(lifetime_case):
    case = lifetime_case
    child_id, result = await _launch(case, "foreground")
    assert result.status == "completed"
    case.runtime._subagent_mailbox_resumers.pop(child_id)
    record = case.runtime.send_swarm_message(sender_id="parent", recipient_id=child_id,
        content="The accepted next turn must retain its loaded thread.", conversation_id="conversation",
        delivery_mode="trigger_turn")
    old_session = case.runtime.subagent_session(child_id)
    case.runtime._subagent_limits["conversation"] = 1
    case.runtime.start_subagent(subagent_id="mail-contender", parent_run_id="parent", agent_type="worker")
    with pytest.raises(RuntimeError, match="no idle thread"):
        await case.runtime.acquire_subagent_session("mail-contender", lambda: old_session, conversation_id="conversation")
    assert not old_session.closed and case.probe.commands[child_id].status == "running"
    assert case.runtime._swarm_store.has_unacknowledged_trigger_turn_mail(child_id, record.recipient_mailbox_epoch)
    case.runtime.complete_subagent("mail-contender", "completed")
