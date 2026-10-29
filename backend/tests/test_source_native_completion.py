"""Source completion identity, durable routing and provider-consumption receipts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import partial
import json

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.mailbox_delivery import inject_parent_notifications
from backend.agent.parent_notification_outbox import enqueue_parent_notification
from backend.agent.query_engine import QueryEngine
from backend.agent.run_context import RunContext
from backend.agent import runtime as runtime_module
from backend.agent.runtime import AgentRuntime
from backend.agent.runtime_records import SubagentResultRecord
from backend.agent.state import AgentState
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tests.test_harness_turn_boundaries import query_factory
from backend.tools.collaboration_tools import InterruptAgentTool, ListAgentsTool, wait_for_coordination


def _runtime(tmp_path):
    return AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)


def _child(runtime, *, parent="root", name="worker", version="v2"):
    return runtime.start_subagent(subagent_id=name, parent_run_id=parent, agent_type="default",
        background=True, agent_path_segment=name, resume_config={"source_spawn_version": version})


def _finish(runtime, child, *, status="completed", content="Actual child answer", error="", reason="completed"):
    fence = {"agent_path": child.agent_path, "mailbox_epoch": child.mailbox_epoch}
    result = runtime.store_subagent_result(child.subagent_id, status=status, content=content,
        error=error, terminal_reason=reason, **fence)
    assert result is not None
    assert runtime.complete_subagent(child.subagent_id, status, **fence) is not None
    return result


def _native(author, recipient, payload):
    return {"type": "agent_message", "author": author, "recipient": recipient,
        "content": [{"type": "input_text", "text":
            f"Message Type: FINAL_ANSWER\nTask name: {recipient}\nSender: {author}\nPayload:\n{payload}"}]}


def _native_items(messages):
    return [item for message in messages for item in message.provider_items if item["type"] == "agent_message"]


def _old_outbox(runtime, conversation_id):
    path = runtime.parent_outbox(conversation_id=conversation_id).path
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["notifications"]:
        for key in ("source_spawn_version", "author_agent_path", "recipient_agent_path", "terminal_reason"):
            item["payload"].pop(key)
    path.write_text(json.dumps(data), encoding="utf-8")


async def _inject(runtime, builder, state, *, parent="root", metadata=None):
    return await inject_parent_notifications(ctx=builder, state=state, metadata=metadata or {},
        runtime=runtime, run_context=RunContext(agent_runtime=runtime), parent_run_id=parent,
        conversation_id="conversation")


@pytest.mark.parametrize("cold,old_payload", [(False, False), (True, False), (True, True)])
def test_next_real_user_turn_consumes_native_completion_once_then_acks(query_factory, tmp_path, cold, old_payload):
    make_session, make_submission = query_factory
    runtime = None

    class Provider(LLMAdapter):
        def __init__(self):
            self.inputs = []

        async def stream_chat(self, messages, tools=None):
            self.inputs.append(deepcopy(messages))
            if len(self.inputs) == 1:
                item, = runtime.list_parent_notifications(conversation_id="boundary-conversation")
                assert item["status"] == "delivered" and item["acked_at_ms"] is None
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Parent answer", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    provider = Provider()
    owner = make_session(provider)
    first = make_submission(owner, "Use the child's answer")
    runtime = first.runtime.run_context.agent_runtime
    runtime.start_run(run_id="root", conversation_id="boundary-conversation")
    child = _child(runtime)
    _finish(runtime, child, content="  exact final text\n")
    notification, = runtime.list_parent_notifications(conversation_id="boundary-conversation")
    assert notification["payload"]["source_spawn_version"] == "v2"
    assert notification["payload"]["author_agent_path"] == "/root/worker"
    assert notification["payload"]["recipient_agent_path"] == "/root"
    assert notification["payload"]["terminal_reason"] == "completed"
    assert not runtime.parent_outbox(conversation_id="boundary-conversation").replayable()[0].starts_turn
    runtime.complete_run("root")
    if old_payload:
        _old_outbox(runtime, "boundary-conversation")

    async def scenario():
        nonlocal runtime
        try:
            if cold:
                assert await runtime.aclose(release_lease=True)
                runtime = _runtime(tmp_path)
            first.runtime.run_context.agent_runtime = runtime
            events = [event async for event in QueryEngine().submit(first)]
            assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
            expected = _native("/root/worker", "/root", "  exact final text\n")
            assert _native_items(provider.inputs[0]) == [expected]
            item, = runtime.list_parent_notifications(conversation_id="boundary-conversation")
            assert item["status"] == "acked" and item["attempts"] == 1
            assert runtime.parent_outbox(conversation_id="boundary-conversation").replayable() == []
            second = make_submission(owner, "Continue with another user request")
            second.runtime.run_context.agent_runtime = runtime
            events = [event async for event in QueryEngine().submit(second)]
            assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
            assert len(provider.inputs) == 2
            assert _native_items(provider.inputs[1]) == [expected]
            carriers = [message for message in owner.context_builder._history if message.provider_items
                and message.input_origin == "agent"]
            assert len(carriers) == 1 and carriers[0].content == "" and not carriers[0].is_user_input
            assert carriers[0].accepted_input is None
            assert len([message for message in owner.context_builder._history if message.is_user_input]) == 2
        finally:
            await owner.aclose()
            await runtime.aclose(release_lease=True)

    asyncio.run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
async def test_nested_completion_keeps_its_parent_after_cold_recovery(tmp_path, cold):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    parent = _child(runtime, name="planner")
    child = _child(runtime, parent=parent.subagent_id, name="reviewer")
    _finish(runtime, child)
    assert runtime.complete_subagent(parent.subagent_id, agent_path=parent.agent_path,
        mailbox_epoch=parent.mailbox_epoch) is not None
    runtime.complete_run("root")
    try:
        if cold:
            assert await runtime.aclose(release_lease=True)
            runtime = _runtime(tmp_path)
        runtime.start_run(run_id="root-next", conversation_id="conversation")
        builder = ContextBuilder()
        assert await _inject(runtime, builder, AgentState(user_message="Next root request"), parent="root-next") == 0
        pending, = runtime.list_parent_notifications(conversation_id="conversation", status="pending")
        assert pending["parent_run_id"] == "planner"
        state = AgentState(user_message="Next planner request")
        assert await _inject(runtime, builder, state, metadata={"agent_mode": "subagent",
            "agent_id": "planner", "run_id": "planner-turn"}) == 1
        assert _native_items(builder._history) == [_native("/root/planner/reviewer", "/root/planner", "Actual child answer")]
        assert runtime.list_parent_notifications(conversation_id="conversation")[0]["status"] == "delivered"
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_completion_does_not_cross_reused_child_mailbox_epoch(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(runtime_module, "SubagentResultRecord", partial(SubagentResultRecord, completed_at=42))
    runtime.start_run(run_id="root", conversation_id="conversation")
    old = _child(runtime)
    _finish(runtime, old, content="Old incarnation")
    current = _child(runtime)
    assert current.mailbox_epoch == old.mailbox_epoch + 1
    _finish(runtime, current, content="Current incarnation")
    try:
        builder = ContextBuilder()
        assert await _inject(runtime, builder, AgentState(user_message="Actual request")) == 1
        assert _native_items(builder._history) == [_native("/root/worker", "/root", "Current incarnation")]
        items = runtime.list_parent_notifications(conversation_id="conversation")
        assert {item["payload"]["content"]: item["status"] for item in items} == {
            "Old incarnation": "acked", "Current incarnation": "delivered"}
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,old_payload", [("failed", False), ("partial", False), ("partial", True)])
async def test_failed_and_partial_native_results_keep_terminal_evidence(tmp_path, status, old_payload):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = _child(runtime)
    _finish(runtime, child, status=status, content="Captured output", error="Stream connection closed",
        reason="stream_ended_before_done")
    runtime.complete_run("root")
    if old_payload:
        _old_outbox(runtime, "conversation")
    assert await runtime.aclose(release_lease=True)
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root-next", conversation_id="conversation")
    try:
        builder = ContextBuilder()
        assert await _inject(runtime, builder, AgentState(user_message="Inspect result"), parent="root-next") == 1
        error = "Stream connection closed"
        if status == "partial":
            error = ("status: partial\nterminal_reason: stream_ended_before_done\n"
                "error: Stream connection closed\npartial_output:\nCaptured output")
        assert _native_items(builder._history) == [_native("/root/worker", "/root",
            f"Agent errored: {error}\n\nThis agent's turn failed. If you still need this agent, "
            "use the available collaboration tools to give it another task.")]
        payload = runtime.list_parent_notifications(conversation_id="conversation")[0]["payload"]
        assert payload["status"] == status and payload["content"] == "Captured output"
        assert payload["error"] == "Stream connection closed"
        assert runtime.get_subagent_snapshot(child.subagent_id)["result"]["terminal_reason"] == "stream_ended_before_done"
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [("cancelled", "interrupted"), ("interrupted", "interrupted"),
    ("partial", "goal_budget_exhausted")])
async def test_interrupted_source_result_records_evidence_without_completion_mail(tmp_path, status, reason):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = _child(runtime)
    _finish(runtime, child, status=status, content="Unfinished output", error="Actual stop evidence", reason=reason)
    try:
        assert runtime.list_parent_notifications(conversation_id="conversation") == []
        result = runtime.get_subagent_snapshot(child.subagent_id)["result"]
        assert result["status"] == status and result["content"] == "Unfinished output"
        old = enqueue_parent_notification(parent_run_id="root", conversation_id="conversation", subagent_id=child.subagent_id,
            payload={"status": status, "content": "Unfinished output", "error": "Actual stop evidence"},
            mailbox_epoch=child.mailbox_epoch, base_dir=runtime._outbox_root)
        builder = ContextBuilder()
        assert await _inject(runtime, builder, AgentState(user_message="Continue")) == 0
        assert builder._history == []
        item, = runtime.list_parent_notifications(conversation_id="conversation")
        assert item["notification_id"] == old.notification_id and item["status"] == "acked"
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
async def test_guardian_stop_notifies_active_or_cold_parent_without_changing_child_status(tmp_path, cold):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = _child(runtime)
    warning = "Automatic approval review rejected too many approval requests for this turn (3 consecutive, 3 in the last 50 reviews); interrupting the turn."
    _finish(runtime, child, status="cancelled", content="Original partial output", error=warning, reason="guardian_denials")
    try:
        parent = "root"
        if cold:
            runtime.complete_run(parent)
            assert await runtime.aclose(release_lease=True)
            runtime = _runtime(tmp_path)
            parent = "root-next"
            runtime.start_run(run_id=parent, conversation_id="conversation")
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="conversation",
            metadata={"run_id": parent}, run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
        item, = runtime.parent_outbox(conversation_id="conversation").replayable()
        assert not item.starts_turn and item.payload["codex_error_info"] == "too_many_denials"
        assert item.payload["content"] == "Original partial output" and item.payload["error"] == warning
        listed = await ListAgentsTool().execute({}, context)
        child_status = next(agent["agent_status"] for agent in json.loads(listed.content)["agents"]
            if agent["agent_name"] == "/root/worker")
        assert child_status == "interrupted"
        assert await wait_for_coordination(context, timeout_seconds=0) == {"kind": "message"}
        builder, state = ContextBuilder(), AgentState(user_message="Read the actual worker result")
        assert await _inject(runtime, builder, state, parent=parent) == 1
        assert _native_items(builder._history) == [_native("/root/worker", "/root",
            f"Agent interrupted by Guardian: {warning}\n\nTell the user this agent stopped after repeated Guardian denials. "
            "Do not resume this agent or retry its blocked work until the user explicitly confirms that it should continue.")]
        assert await _inject(runtime, builder, state, parent=parent) == 0
        delivered, = runtime.list_parent_notifications(conversation_id="conversation")
        assert delivered["status"] == "delivered" and delivered["acked_at_ms"] is None
        assert await wait_for_coordination(context, timeout_seconds=0) is None
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error,reason,expected", [
    ("completed", "", "completed", {"completed": "Captured output"}),
    ("completed", "Terminal error", "runtime_error", {"errored": "Terminal error"}),
    ("failed", "Terminal error", "runtime_error", {"errored": "Terminal error"}),
    ("partial", "", "max_turn_seconds", {"errored": "status: partial\nterminal_reason: max_turn_seconds\n"
        "error: \npartial_output:\nCaptured output"}),
    ("partial", "Terminal error", "stream_ended_before_done", {"errored": "status: partial\n"
        "terminal_reason: stream_ended_before_done\nerror: Terminal error\npartial_output:\nCaptured output"}),
    ("partial", "", "goal_budget_exhausted", "interrupted"),
    ("cancelled", "", "interrupted", "interrupted"),
    ("interrupted", "", "interrupted", "interrupted"),
])
async def test_list_and_interrupt_use_the_same_actual_source_terminal_status(tmp_path, status, error, reason, expected):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = _child(runtime)
    _finish(runtime, child, status=status, content="Captured output", error=error, reason=reason)
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="conversation",
        metadata={"run_id": "root"}, run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
    try:
        listed = await ListAgentsTool().execute({}, context)
        assert not listed.is_error, listed.content
        statuses = {item["agent_name"]: item["agent_status"] for item in json.loads(listed.content)["agents"]}
        assert statuses == {"/root": "running", "/root/worker": expected}
        interrupted = await InterruptAgentTool().execute({"target": "/root/worker"}, context)
        assert not interrupted.is_error, interrupted.content
        assert json.loads(interrupted.content)["previous_status"] == expected
        builder = ContextBuilder()
        state = AgentState(user_message="Inspect result")
        if expected == "interrupted":
            assert await _inject(runtime, builder, state) == 0
        else:
            assert await _inject(runtime, builder, state) == 1
            payload = (_native_items(builder._history)[0]["content"][0]["text"].split("Payload:\n", 1)[1])
            if "completed" in expected:
                assert payload == expected["completed"]
            else:
                assert payload.startswith(f"Agent errored: {expected['errored']}\n\n")
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_legacy_completion_and_async_hook_keep_existing_text_inputs(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = _child(runtime, version="v1")
    _finish(runtime, child)
    enqueue_parent_notification(parent_run_id="root", conversation_id="conversation", subagent_id="hook-id",
        kind="async_hook", payload={"hook_id": "hook-id", "event": "Stop", "content": "Hook blocked reason"},
        base_dir=runtime._outbox_root)
    try:
        builder = ContextBuilder()
        assert await _inject(runtime, builder, AgentState(user_message="Continue")) == 2
        assert _native_items(builder._history) == []
        assert "<task-notification>" in builder._history[0].content
        assert "Actual child answer" in builder._history[0].content
        assert "<hook-notification>" in builder._history[1].content
        assert "Hook blocked reason" in builder._history[1].content
        assert all(item["status"] == "delivered" for item in runtime.list_parent_notifications(conversation_id="conversation"))
    finally:
        await runtime.aclose(release_lease=True)
