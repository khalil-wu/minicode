"""Interrupt is a bounded terminal observer, independent of Stop/model control."""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.event_envelope import EventEnvelope
from backend.agent.execution_journal import execution_journal_owner
from backend.agent.lifecycle_observer import LifecycleObserverOwner
from backend.agent.message import AgentEvent
from backend.agent.query_engine import QueryEngine
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.query_terminal import QueryTerminalTransaction
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.turn_kernel import TurnKernel
from backend.hooks.discovery import snapshot_from_settings
from backend.hooks.dispatcher import HookExecution
from backend.hooks.manager import HookEvent, HookManager, _HookEntry
from backend.hooks.reducer import execution_succeeded_for_once, reduce_hook_executions
from backend.hooks.runners import HookExecutionError, HookExecutionResult, _entry_timeout_seconds
from backend.permissions.context import PermissionContext, ToolExecutionContext


def _settings(*handlers, event="interrupt", matcher="does-not-match"):
    return {"hooks": {event: [{"matcher": matcher, "hooks": list(handlers)}]}}


def _bound_manager(tmp_path, handlers, *, cancelled=True, emit_event=None):
    manager = HookManager.from_settings(_settings(*handlers), workspace_root=tmp_path)
    manager.scope_id = "thread"
    run = RunContext(hook_manager=manager, cancel_reason="user_interrupted")
    cancel_event = asyncio.Event()
    if cancelled:
        cancel_event.set()
    context = ToolExecutionContext(
        permission=PermissionContext(approval_policy="never"), run_context=run,
        session_id="window", conversation_id="thread", turn_id="turn",
        metadata={"run_id": "run"}, cancel_event=cancel_event,
        deadline_monotonic=time.monotonic() - 1, workspace_root=tmp_path,
        emit_event=emit_event,
    )
    manager.bind_runtime(llm=None, tool_registry=None, tool_context=context)
    return manager, run, context


def test_interrupt_discovery_has_short_timeout_no_matcher_and_sync_mcp():
    snapshot = snapshot_from_settings(_settings(
        {"type": "command", "command": "observe"},
        {"type": "command", "command": "bounded", "timeout": 99},
        {"type": "mcp_tool", "server": "docs", "tool": "inspect", "input": {}, "async": True},
        {"type": "prompt", "prompt": "not supported"},
    ), workspace_root=None)
    manager = HookManager.from_snapshot(snapshot)
    entries = manager.hooks[HookEvent.INTERRUPT]
    assert len(entries) == 3
    assert [_entry_timeout_seconds(entry, HookEvent.INTERRUPT) for entry in entries] == [1, 3, 1]
    assert entries[2].run_async is False
    assert len(manager.preview(HookEvent.INTERRUPT, match_target="anything")) == 3
    assert any("Clamping interrupt hook timeout" in warning for warning in snapshot.warnings)
    assert any("supports command and MCP" in warning for warning in snapshot.warnings)
    session_end = snapshot_from_settings(_settings(
        {"type": "mcp_tool", "server": "docs", "tool": "inspect", "input": {}}, event="session_end",
    ), workspace_root=None)
    assert not session_end.entries
    assert any("SessionEnd MCP hooks are not supported" in warning for warning in session_end.warnings)


@pytest.mark.parametrize("stdout,exit_code,valid,message", [
    ("", 0, True, ""),
    ('{"systemMessage":"Observer warning"}', 0, True, "Observer warning"),
    ('{"systemMessage":null}', 0, True, ""),
    ('{"system_message":"wrong spelling"}', 0, False, ""),
    ('{"systemMessage":3}', 0, False, ""),
    ('{"continue":false,"additional_context":"resume","decision":"block"}', 0, False, ""),
    ("plain diagnostic", 0, False, ""),
    ('{"systemMessage":"must not apply"}', 2, False, ""),
])
def test_interrupt_output_is_strict_and_never_controls_continuation(stdout, exit_code, valid, message):
    entry = _HookEntry(matcher=None, entry_id="observer", source="user")
    execution = HookExecution(entry=entry, stdout=stdout, stderr="", exit_code=exit_code,
        configured_order=0, completion_order=0, duration_ms=1)
    result = reduce_hook_executions(HookEvent.INTERRUPT, [execution], expected_event_name="interrupt")
    assert result.failed is not valid
    assert result.system_message == message
    assert not result.blocked and not result.prevent_continuation and not result.retry
    assert result.additional_context == result.custom_instructions == result.feedback == ""
    assert result.permission_decision == "" and result.updated_input is None
    assert result.run_summaries[0]["stdout"] == stdout
    assert execution_succeeded_for_once(HookEvent.INTERRUPT, execution, expected_event_name="interrupt") is valid


@pytest.mark.asyncio
async def test_real_interrupt_command_receives_source_input_after_turn_was_cancelled(tmp_path):
    capture = tmp_path / "input.json"
    script = tmp_path / "observer.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(capture)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n"
        "print(json.dumps({'systemMessage': 'Observed interruption'}))\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command, shell = f"& {quote(sys.executable)} {quote(script)}", "powershell"
    else:
        command, shell = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", "bash"
    manager, _run, original = _bound_manager(tmp_path, [{
        "type": "command", "command": command, "shell": shell, "timeout": 3,
    }])
    result = await manager.run_interrupt(turn_id="turn", model="active-model", permission_mode="bypassPermissions")
    assert not result.failed and result.system_message == "Observed interruption"
    assert json.loads(capture.read_text(encoding="utf-8")) == {
        "session_id": "thread", "turn_id": "turn", "transcript_path": None,
        "cwd": str(tmp_path), "hook_event_name": "Interrupt", "model": "active-model",
        "permission_mode": "bypassPermissions",
    }
    assert original.cancel_event.is_set() and original.deadline_monotonic < time.monotonic()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    '{"systemMessage":"warning"}',
    '{"system_message":"must not reach next model","additional_context":"never inject"}',
])
async def test_configured_async_interrupt_does_not_delay_terminal_or_feed_next_model(tmp_path, monkeypatch, output):
    emitted, started, release = [], asyncio.Event(), asyncio.Event()

    async def emit(event_type, data):
        emitted.append((event_type, data))

    async def execute(_entry, *, runtime, **_kwargs):
        assert runtime.tool_context.cancel_event is None
        assert runtime.tool_context.deadline_monotonic is None
        started.set()
        await release.wait()
        return HookExecutionResult(output, "", 0)

    monkeypatch.setattr("backend.hooks.runners._execute_command", execute)
    manager, _run, _context = _bound_manager(tmp_path, [{
        "type": "command", "command": "observer", "async": True, "async_rewake": True,
    }], emit_event=emit)
    result = await asyncio.wait_for(manager.run_interrupt(
        turn_id="turn", model="model", permission_mode="default",
    ), 1)
    await started.wait()
    assert not result.failed and manager.pending_async_hooks == 1
    assert not release.is_set() and not emitted
    release.set()
    await manager.drain_async_hooks()
    assert manager.take_async_context() == ()
    assert len(emitted) == 1 and emitted[0][0] == "agent.item"
    assert emitted[0][1]["status"] == ("completed" if "systemMessage" in output else "failed")


@pytest.fixture
def terminal_owner(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)
    record = runtime.start_run(run_id="interrupt-run", conversation_id="thread", session_id="window")
    manager, run, tool_context = _bound_manager(tmp_path, [{
        "type": "mcp_tool", "server": "docs", "tool": "observe",
        "input": {"path": "${transcript_path}", "model": "${model}", "permission": "${permission_mode}"},
    }])
    run.agent_runtime = runtime
    run.active_model_execution = SimpleNamespace(model="last-step-model")
    state, builder = AgentState(user_message="request"), ContextBuilder()
    metadata = {"run_id": record.run_id, "turn_id": "turn", "conversation_id": "thread", "assistant_message_id": "answer"}
    kernel = TurnKernel(runtime=runtime, run_record=record, metadata=metadata,
        emit_event=None, initial_user_message=state.user_message, state=state, run_context=run)
    journal = runtime.execution_journal(execution_journal_owner("conversation", "interrupt-test", "thread"))
    recorder = QueryJournalRecorder(journal, metadata, state, builder, kernel, "thread")
    ctx = SimpleNamespace(state=state, context_builder=builder, run_context=run, turn_kernel=kernel,
        metadata=metadata, session_id="window", user_message=state.user_message)
    transaction = QueryTerminalTransaction(ctx, recorder)
    yield SimpleNamespace(runtime=runtime, manager=manager, run=run, context=tool_context,
        journal=journal, transaction=transaction, ctx=ctx)
    runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_actual_terminal_commits_transcript_before_interrupt_and_publishes_hook_evidence_before_done(terminal_owner):
    owner, calls, observed = terminal_owner, [], []

    class Executor:
        async def execute(self, **kwargs):
            calls.append(kwargs)
            records = [json.loads(line) for line in owner.journal.path.read_text(encoding="utf-8").splitlines()]
            assert any(row["payload"].get("lifecycle") == "runtime_terminal_committed" for row in records)
            assert owner.runtime.get_run("interrupt-run").status == "cancelled"
            assert kwargs["input"] == {"path": str(owner.journal.path), "model": "last-step-model", "permission": "bypassPermissions"}
            assert kwargs["tool_context"].cancel_event is None
            return '{"systemMessage":"observer completed"}'

    class Observer:
        async def finish(self, **_kwargs):
            observed.append(len(calls))

    owner.manager.bind_runtime(llm=None, tool_registry=None, tool_context=owner.context, mcp_executor=Executor())
    events = [event async for event in QueryEngine._terminal_events(
        owner.transaction, LifecycleObserverOwner(observer=Observer(), run_context=owner.run),
        EventEnvelope(task_id="task", conversation_id="thread"),
        AgentEvent.done(status="cancelled", reason="interrupted"), validate=True,
    )]
    assert len(calls) == 1 and observed == [1]
    assert [event.type for event in events] == ["system_notice", "agent.item", "agent.run.completed", "done"]
    assert events[-1].data["status"] == "cancelled"
    again = await owner.transaction.commit(AgentEvent.done(status="cancelled", reason="interrupted"), validate=True)
    assert len(calls) == 1 and again.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason,cancel_reason,subagent,expected", [
    ("cancelled", "interrupted", "user_interrupted", False, 1),
    ("cancelled", "interrupted", "app_shutdown", False, 0),
    ("cancelled", "max_turn_seconds", "", False, 0),
    ("cancelled", "interrupted", "user_interrupted", True, 0),
    ("completed", "completed", "", False, 0),
])
async def test_interrupt_only_runs_for_root_turn_interruption_and_cannot_veto_terminal(
    terminal_owner, status, reason, cancel_reason, subagent, expected,
):
    owner, calls = terminal_owner, []

    class FailedExecutor:
        async def execute(self, **_kwargs):
            calls.append("actual attempt")
            raise HookExecutionError("MCP hook timed out after 1s")

    owner.run.cancel_reason, owner.run.is_subagent = cancel_reason, subagent
    owner.ctx.state.reply = "final answer" if status == "completed" else ""
    owner.manager.bind_runtime(llm=None, tool_registry=None, tool_context=owner.context, mcp_executor=FailedExecutor())
    result = await owner.transaction.commit(AgentEvent.done(status=status, reason=reason), validate=True)
    assert len(calls) == expected
    assert (result.status, result.reason) == (status, reason)
    if expected:
        assert any("MCP hook timed out" in event.data.get("content", "") for event in result.evidence_events)
        item = next(event.data for event in result.evidence_events if event.type == "agent.item")
        assert item["status"] == "failed" and "runtime_error" in item["content"]
