"""Admission, pending-child and public-journal ownership regression checks."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from backend.agent.answer_acceptance import apply_stop_hook_policy
from backend.agent.context import ContextBuilder
from backend.agent.conversation_query_guard import ConversationQueryGuardRegistry
from backend.agent.execution_journal import ExecutionJournal, turn_input_consumption_id
from backend.agent.final_tool_request import FinalExecutableToolRequest
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_events import status_for_result
from backend.agent.turn_state import AgentTurnState
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.conversations.context_delta import context_snapshot_delta
from backend.hooks.dispatcher import HookExecution, select_handlers
from backend.hooks.manager import HookEvent, HookResult, _prepare_hook_condition_matcher
from backend.hooks.reducer import reduce_hook_executions
from backend.llm.base import LLMAdapter, LLMMessage, ToolCallEvent
from backend.mcp.client import MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.subagent_service import build_subagent_transcript_messages, project_subagent_transcript_events
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_support import _SubagentLLMResolution
from backend.ws.conversation_runtime import ConversationRuntime


def _runtime(root: Path) -> AgentRuntime:
    return AgentRuntime(metrics_file=root / "metrics.jsonl", swarm_store_dir=root / "swarm", enable_lease_heartbeat=False)


def test_transition_reserves_the_entire_cancel_to_query_handoff():
    guards = ConversationQueryGuardRegistry()
    old = guards.try_start("conversation", owner_id="ws:owner:old")
    assert guards.try_transition("conversation", owner_id="foreign", replacing_owner_id="ws:other:old") is None
    transition = guards.try_transition("conversation", owner_id="retry", replacing_owner_id=old.owner_id)
    assert not guards.transition_ready(transition)
    assert guards.owns(old)
    assert guards.try_start("conversation", owner_id="scheduler") is None
    assert guards.end(old)
    assert guards.transition_ready(transition)
    assert guards.try_start("conversation", owner_id="scheduler") is None
    new = guards.handoff(transition, owner_id="ws:owner:new")
    assert new.generation == old.generation + 1
    assert not guards.release_transition(transition)
    assert not guards.end(old)
    assert guards.owns(new)
    assert guards.end(new)


def test_retry_is_private_until_one_durable_user_and_rewind_commit(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "conversations")
    snapshot = {"history": [{"role": "user", "content": "old request"}, {"role": "assistant", "content": "old answer"}],
                "turn_admissions": {"old-user": {"history_start": 0, "history_end": 1}}, "summary": "old summary"}
    conversation = repo.create_conversation(transcript=[
        {"id": "old-user", "role": "user", "content": "old request"},
        {"id": "old-answer", "role": "assistant", "content": "old answer"},
    ], context_snapshot=snapshot)
    context = ContextBuilder()
    context.load_snapshot(snapshot)
    before_history = context.export_snapshot()["history"]
    owner = ConversationRuntime(conversation_repo=repo, context_builder=context,
                                build_summary_from_transcript=lambda *args, **kwargs: "")
    prepared = owner.prepare_retry_from_message(conversation=conversation, retry_from_message_id="old-user")
    assert [item["id"] for item in repo.get_conversation(conversation.id).transcript] == ["old-user", "old-answer"]
    assert context.export_snapshot()["history"] == before_history
    commit_record = repo._commit_record
    monkeypatch.setattr(repo, "_commit_record", lambda _: (_ for _ in ()).throw(OSError("admission write failed")))
    with pytest.raises(OSError, match="admission write failed"):
        repo.commit_turn_admission(conversation.id, user_message={"id": "new-user", "role": "user", "content": "redo"},
            context_snapshot=prepared["context_snapshot"], rewind_projection=prepared, expected_revision=conversation.revision)
    assert [item["id"] for item in repo.get_conversation(conversation.id).transcript] == ["old-user", "old-answer"]
    monkeypatch.setattr(repo, "_commit_record", commit_record)
    committed = repo.commit_turn_admission(conversation.id, user_message={"id": "new-user", "role": "user", "content": "redo"},
        context_snapshot=prepared["context_snapshot"], rewind_projection=prepared, expected_revision=conversation.revision)
    assert [item["id"] for item in committed.transcript] == ["new-user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
async def test_whitespace_blocking_stop_hook_accepts_without_inventing_continuation(child):
    context = ContextBuilder()
    hook = SimpleNamespace(has_hooks=lambda _: True, run_stop=AsyncMock(return_value=HookResult(blocked=True, feedback=" \n\t ", message=" ")),
                           run_subagent_stop=AsyncMock(return_value=HookResult(blocked=True, feedback=" \n\t ", message=" ")))
    context.bind_hook_manager(hook)
    state = AgentState(user_message="request", max_iterations=2)
    if child:
        state.prompt_context.update(subagent_id="child", subagent="explore")
    stream = SimpleNamespace(cancel_active_agent_message=lambda: pytest.fail("candidate must remain accepted"),
                             reset_for_retry=lambda: pytest.fail("must not retry an empty instruction"))
    result = await apply_stop_hook_policy(user_message="request", candidate_text="answer", state=state,
        context_builder=context, stream_text=stream, provider_phase="final_answer", provider_items=[])
    assert result.action == "accept"
    assert any(event.type == "system_notice" and event.data["level"] == "warning" for event in result.events)
    assert state.stop_hook_feedback_count == 0
    assert context.export_snapshot()["history"] == []


class _Provider(LLMAdapter):
    async def simple_chat(self, messages, *, max_tokens=None):
        return ""

    async def stream_chat(self, messages, tools=None, metadata=None):
        if False:
            yield


@pytest.mark.asyncio
async def test_task_acceptance_persists_pending_identity_and_detach_before_a_slot(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    parent = runtime.start_run(conversation_id="conversation", session_id="session")
    for index in range(4):
        runtime.start_subagent(subagent_id=f"busy-{index}", parent_run_id=parent.run_id, agent_type="explore")
    registry = ToolRegistry()
    llm = _Provider()
    config = AppConfig(llm=LLMSettings(api_key=""))
    tool = TaskTool(llm_provider=llm, tool_registry_provider=registry,
                    artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(tool, "_prepare_subagent_llm_resolution", AsyncMock(return_value=_SubagentLLMResolution(
        llm=llm, config=config, provider="fixture", model="fixture-model", effort="low")))
    emitted = []
    async def emit(kind, data):
        emitted.append((kind, data))
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"),
        session_id="session", task_id="parent-task", tool_call_id="spawn-call", workspace_root=tmp_path,
        metadata={"run_id": parent.run_id}, run_context=RunContext(agent_runtime=runtime), emit_event=emit)
    result = await tool._start_background_subtask(description="Queued work", prompt="The complete original instruction.",
        agent_type="general-purpose", context=context, subagent_metadata={"detach_from_parent": True, "_task_name": "queued_worker"})
    ident = result.runtime_metadata["subagent_id"]
    record = runtime.get_subagent(ident)
    assert result.status == "accepted" and status_for_result(result) == "success"
    assert record.status == "pending" and record.started_at == 0
    assert record.input_prompt == "The complete original instruction."
    assert record.parent_tool_call_id == "spawn-call"
    assert record.detach_from_parent and not record.cancel_with_parent
    assert record.resume_config["model"] == "fixture-model"
    assert "api_key" not in json.dumps(record.resume_config)
    assert result.runtime_metadata["delegations"] == [{"agent_id": ident, "name": "queued_worker", "status": "pending",
                                                       "started_at": 0, "accepted_at": record.accepted_at}]
    assert runtime.cancel_child_subagent_tasks(parent.run_id) == []
    assert runtime.has_live_subagent_task(ident)
    assert not [event for event in emitted if event[0] == "subagent.start"]
    worker = runtime._subagent_tasks[ident]
    runtime.cancel_subagent_task(ident, reason="user_stopped_pending")
    await asyncio.gather(worker, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.gather(*context.run_context.lifecycle_cleanup_tasks, return_exceptions=False)
    terminal = runtime.get_subagent(ident)
    assert terminal.status == "cancelled" and terminal.started_at == 0
    assert runtime.get_subagent_snapshot(ident)["result"]["terminal_reason"] == "user_stopped_pending"
    assert not [event for event in emitted if event[0] == "subagent.start"]
    assert [event for event in emitted if event[0] == "subagent.done"][0][1]["child_started"] is False
    runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_pending_promotion_keeps_the_same_path_and_mailbox_incarnation(tmp_path):
    runtime = _runtime(tmp_path)
    parent = runtime.start_run(conversation_id="conversation")
    worker = asyncio.create_task(asyncio.Event().wait())
    runtime.register_subagent_task("queued", worker, parent_run_id=parent.run_id, agent_type="explore", pending=True,
        input_prompt="original prompt", canonical_task_name="worker", resume_config={"model": "fixture"})
    pending = runtime.get_subagent("queued")
    assert await runtime.acquire_subagent_slot("queued")
    started = runtime.start_subagent(subagent_id="queued", parent_run_id=parent.run_id, agent_type="explore", input_prompt="original prompt")
    assert started.agent_path == pending.agent_path
    assert started.mailbox_epoch == pending.mailbox_epoch
    assert started.accepted_at == pending.accepted_at
    assert started.started_at > 0 and started.status == "running"
    assert started.resume_config["model"] == "fixture"
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    runtime.release_subagent_task("queued", expected_task=worker)
    runtime.close(release_lease=True)


async def _crash_with_pending_child(root: Path):
    runtime = _runtime(root)
    parent = runtime.start_run(conversation_id="conversation", session_id="session")
    task = asyncio.create_task(asyncio.Event().wait())
    runtime.register_subagent_task("queued-crash", task, parent_run_id=parent.run_id, agent_type="explore", pending=True,
        input_prompt="full durable input", background=True, detach_from_parent=True,
        canonical_task_name="original_name", resume_config={"provider": "fixture", "model": "fixture-model"})
    (root / "pending.json").write_text(json.dumps(runtime.get_subagent("queued-crash").to_dict()), encoding="utf-8")
    os._exit(77)


def test_cold_pending_child_has_a_collectable_never_started_terminal(tmp_path):
    process = subprocess.run([sys.executable, __file__, "crash-pending", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert process.returncode == 77, process.stdout + process.stderr
    previous = json.loads((tmp_path / "pending.json").read_text(encoding="utf-8"))
    runtime = _runtime(tmp_path)
    recovered = runtime.get_subagent("queued-crash")
    assert recovered.status == "interrupted" and recovered.started_at == 0
    assert recovered.agent_path == previous["agent_path"] and recovered.mailbox_epoch == previous["mailbox_epoch"]
    assert recovered.input_prompt == "full durable input" and recovered.detach_from_parent
    assert not runtime.has_live_subagent_task("queued-crash")
    result = runtime.get_subagent_snapshot("queued-crash")["result"]
    assert result["terminal_reason"] == "interrupted_before_start"
    assert result["duration_ms"] == result["tool_call_count"] == 0
    assert "before" in result["content"]
    runtime.close(release_lease=True)


def test_evicted_parent_run_is_resolved_from_its_authoritative_record(tmp_path):
    runtime = _runtime(tmp_path)
    parent = runtime.start_run(conversation_id="conversation")
    child = runtime.start_subagent(subagent_id="child", parent_run_id=parent.run_id, agent_type="explore", background=True)
    runtime.complete_subagent("child", summary="finished", agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
    runtime.complete_run(parent.run_id)
    current = runtime.start_run(conversation_id="conversation")
    runtime._runs.pop(parent.run_id)
    assert runtime.accepts_parent_notification(subagent_id="child", mailbox_epoch=child.mailbox_epoch, parent_run_id=current.run_id)
    assert parent.run_id not in runtime._runs
    assert not runtime.accepts_parent_notification(subagent_id="child", mailbox_epoch=child.mailbox_epoch + 1, parent_run_id=current.run_id)
    runtime.close(release_lease=True)


def test_public_journal_cursor_carries_only_new_records_and_no_private_context(tmp_path):
    journal = ExecutionJournal("child", base_dir=tmp_path)
    journal.append_lifecycle("turn_started", {"turn_id": "turn", "context_snapshot": {"secret_runtime_field": "private"}})
    journal.append("user_prompt", {"content": "request", "run_id": "turn", "context_snapshot": {"private": "context"}})
    cursor = journal.sequence
    for index in range(60):
        journal.append_tool_use({"id": f"tool-{index}", "name": "read_file", "args": {"path": f"{index}.py"}})
        journal.append_tool_result({"id": f"tool-{index}", "status": "success", "content": "x" * 1024}, tool_name="read_file")
    tail = journal.read_events_since(cursor)
    public = project_subagent_transcript_events([event.to_dict() for event in tail])
    assert len(tail) == len(public) == 120
    assert public[0]["seq"] == cursor + 1
    assert public[-1]["seq"] == journal.sequence
    assert len(json.dumps(public)) < 200_000
    assert public[0]["payload"]["tool_call"]["args"] == {"path": "0.py"}
    assert public[1]["payload"]["record"]["outputPreview"] == "x" * 1024
    assert "args" not in public[1]["payload"]["record"]
    headers = project_subagent_transcript_events([event.to_dict() for event in journal.read_events()[:2]])
    assert "context_snapshot" not in json.dumps(headers)
    snapshot = build_subagent_transcript_messages({"events": [event.to_dict() for event in journal.read_events()]})
    assert snapshot[-1]["id"] == "subagent-turn-turn"
    assert len(snapshot[-1]["blocks"]) == 60


def test_retained_answer_source_survives_context_and_child_projection():
    context = ContextBuilder()
    context.append_user("question")
    context.append_assistant("candidate", phase="final_answer", answer_source="retained")
    context._history_store.append(LLMMessage(role="developer", content="late instruction"))
    restored = ContextBuilder()
    restored.load_snapshot(context.export_snapshot())
    history = restored.export_snapshot()["history"]
    assert history[1]["answer_source"] == "retained" and history[1]["phase"] == "final_answer"
    assert history[-1]["role"] == "developer"
    messages = build_subagent_transcript_messages({"events": [
        {"event_type": "system", "event_id": "start", "ts_ms": 1, "payload": {"lifecycle": "turn_started", "turn_id": "turn"}},
        {"event_type": "user_prompt", "event_id": "user", "ts_ms": 2, "payload": {"content": "question"}},
        {"event_type": "assistant", "event_id": "candidate", "ts_ms": 3, "payload": {"content": "candidate", "answer_source": "retained"}},
        {"event_type": "terminal", "event_id": "done", "ts_ms": 4, "payload": {"status": "failed", "summary": "candidate"}},
    ]})
    assert messages[-1]["blocks"][-1]["source"] == "retained"


def test_ambiguous_hook_alias_observes_but_cannot_grant_the_mcp_request(tmp_path):
    tool = MCPToolProxy("a_b", MCPToolDef(name="c", description="fixture", input_schema={"type": "object"}),
                        SimpleNamespace(connected=True), ArtifactStore(storage_dir=tmp_path / "artifacts"))
    registry = ToolRegistry()
    registry.register(tool)
    entry = SimpleNamespace(raw_matcher="mcp__a_b__c", condition="", entry_id="legacy", source="project", source_path="hooks.json",
                            display_order=0, status_message="", additional_context_limit=None)
    condition, aliases, grant = _prepare_hook_condition_matcher(event=HookEvent.PRE_TOOL_USE, match_target=tool.name,
        env_extras={"TOOL_NAME": tool.name, "TOOL_ARGS_JSON": "{}"}, tool_registry=registry, has_conditions=False)
    assert select_handlers([entry], event=HookEvent.PRE_TOOL_USE, match_target=tool.name,
                           condition_matches=condition, match_aliases=aliases) == [entry]
    assert not grant(entry)
    result = reduce_hook_executions(HookEvent.PRE_TOOL_USE, [HookExecution(entry=entry,
        stdout=json.dumps({"event": "pre_tool_use", "permission_decision": "allow", "updated_input": {"x": 1}, "additional_context": "observed"}),
        stderr="", exit_code=0, configured_order=0, completion_order=0, duration_ms=1, permission_grant_allowed=False)],
        expected_event_name="pre_tool_use")
    assert result.permission_decision == "" and not result.blocked
    assert result.updated_input == {"x": 1} and result.additional_context == "observed"


@pytest.mark.asyncio
async def test_approved_digest_and_execution_remain_bound_to_the_captured_mcp_identity(tmp_path):
    calls = []
    class Client:
        connected = True
        async def call_tool(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("replacement tool must not execute")
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    first = MCPToolProxy("a_b", MCPToolDef(name="c", description="fixture"), Client(), store)
    replacement = MCPToolProxy("a", MCPToolDef(name="b_c", description="fixture"), Client(), store)
    first.name = replacement.name = "shared-wire"
    request = FinalExecutableToolRequest.freeze(ToolCallEvent(id="call", name="shared-wire", arguments={}), policy_identity=first.policy_identity)
    other = FinalExecutableToolRequest.freeze(ToolCallEvent(id="call", name="shared-wire", arguments={}), policy_identity=replacement.policy_identity)
    assert request.digest != other.digest
    registry = ToolRegistry()
    registry.register(replacement)
    result = await registry.execute("shared-wire", {}, expected_policy_identity=request.policy_identity)
    assert result.is_error and result.error_kind == "approval_stale"
    assert calls == []


def test_public_parent_record_keeps_pending_delegation_separate_from_tool_success():
    turn = AgentTurnState(now_ms=lambda: 100)
    turn.record_tool_call({"id": "spawn", "name": "task", "args": {}, "status": "running"})
    event = AgentEvent.tool_result(id="spawn", summary="Accepted background child", status="success",
        delegations=[{"agent_id": "child", "name": "worker", "status": "pending", "started_at": 0, "accepted_at": 20}])
    turn.record_tool_result(event.data)
    record = turn.finalize(terminal_status="completed").tool_calls[0]
    assert record["status"] == "success"
    assert record["delegations"][0]["status"] == "pending" and record["delegations"][0]["started_at"] == 0


def test_late_actual_start_is_retained_across_stale_parent_terminal_projection(tmp_path):
    pending = {"agent_id": "child", "name": "worker", "status": "pending", "started_at": 0, "accepted_at": 20}
    started = {**pending, "status": "running", "started_at": 80}
    turn = AgentTurnState(now_ms=lambda: 100)
    turn.record_tool_call({"id": "spawn", "name": "task", "args": {}, "status": "running"})
    turn.record_subagent_lifecycle({"parent_tool_call_id": "spawn", "delegation": started})
    turn.record_tool_result(AgentEvent.tool_result(id="spawn", summary="Accepted child", status="success", delegations=[pending]).data)
    assert turn.finalize(terminal_status="completed").tool_calls[0]["delegations"] == [started]
    repo = ConversationRepository(tmp_path / "conversations")
    snapshot = {"turn_admissions": {"user": {"run_id": "parent"}}, "history": [{"role": "user", "content": "request"}]}
    message = {"id": "answer", "role": "assistant", "content": "accepted final", "terminal_status": "completed",
        "tool_calls": [{"id": "spawn", "name": "task", "args": {}, "status": "success", "delegations": [pending]}]}
    conversation = repo.create_conversation(context_snapshot=snapshot, transcript=[{"id": "user", "role": "user", "content": "request"}, message])
    assert repo.update_tool_delegation(conversation.id, message_id="answer", tool_call_id="spawn", delegation=started)
    committed = repo.commit_turn_projection(conversation.id, assistant_message=message,
        context_delta=context_snapshot_delta(snapshot, snapshot), expected_revision=conversation.revision,
        source_user_message_ids=["user"], source_run_id="parent")
    assert committed.transcript[-1]["content"] == "accepted final"
    assert committed.transcript[-1]["tool_calls"][0]["delegations"] == [started]
    assert committed.context_snapshot["history"] == snapshot["history"]


def test_child_transcript_replays_actual_grandchild_start_after_its_parent_finished():
    pending = {"agent_id": "nested", "name": "nested worker", "status": "pending", "started_at": 0, "accepted_at": 20}
    started = {**pending, "status": "running", "started_at": 80}
    events = [
        {"event_type": "system", "event_id": "start", "seq": 1, "ts_ms": 1, "payload": {"lifecycle": "turn_started", "turn_id": "parent-turn"}},
        {"event_type": "user_prompt", "event_id": "user", "seq": 2, "ts_ms": 2, "payload": {"content": "request"}},
        {"event_type": "tool_use", "event_id": "tool", "seq": 3, "ts_ms": 3, "payload": {"tool_call": {"id": "spawn", "name": "task", "arguments": {}}}},
        {"event_type": "tool_result", "event_id": "result", "seq": 4, "ts_ms": 4, "payload": {"tool_call_id": "spawn", "tool_name": "task", "status": "success", "content": "Accepted child", "delegations": [pending]}},
        {"event_type": "terminal", "event_id": "terminal", "seq": 5, "ts_ms": 5, "payload": {"status": "completed"}},
        {"event_type": "system", "event_id": "nested-start", "seq": 6, "ts_ms": 80, "payload": {"lifecycle": "subagent_delegation_updated", "parent_message_id": "subagent-turn-parent-turn", "parent_tool_call_id": "spawn", "delegation": started}},
    ]
    transcript = build_subagent_transcript_messages({"events": events})
    assert transcript[-1]["blocks"][0]["record"]["delegations"] == [started]
    delta = project_subagent_transcript_events(events[-1:])
    assert delta[0]["payload"]["delegation"] == started


if __name__ == "__main__":
    asyncio.run(_crash_with_pending_child(Path(sys.argv[2])))
