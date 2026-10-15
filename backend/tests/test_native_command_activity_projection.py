from __future__ import annotations

import asyncio
import time

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_serial
from backend.agent.tool_events import tool_call_pending_event, tool_call_start_event
from backend.conversations.public_projection import project_public_tool_call
from backend.llm.base import ToolCallStartEvent
from backend.tests.test_native_exec_policy_chain import authorize, setup
from backend.ws.stream_state import apply_stream_event, create_stream_state


def runtime(host, state, call, auth, *, approval_handler):
    builder = ContextBuilder()
    builder.bind_tool_context(host.context)
    return execute_serial(call, perm=auth.permission_decision.permission_level,
        permission_decision=auth.permission_decision, permission_checker=host.checker,
        permission_context=host.context.permission, ctx=builder, state=state,
        tool_registry=host.registry, tool_ctx=host.context,
        approval_handler=approval_handler, skill_manager=None, iteration_id="iter:1")


def snapshot(events):
    streams = {"source-owner": create_stream_state("source-owner", "source-answer", "source-turn")}
    for event in events:
        apply_stream_event(streams, "source-owner", event.type,
            {**event.data, "conversation_id": "source-owner", "message_id": "source-answer", "turn_id": "source-turn"})
    return streams["source-owner"]


@pytest.mark.asyncio
async def test_frozen_native_planned_item_approval_execution_result_and_recovery_share_one_identity(tmp_path):
    host = setup(tmp_path)
    call, auth = authorize(host, command='Write-Output "NATIVE_ACTIVITY_RESULT"')
    state = AgentState(user_message="native activity lifecycle")
    state.ui_tool_started_at[call.id] = time.time() - 30
    events = [tool_call_pending_event(ToolCallStartEvent(id=call.id, name=call.name),
        started_at=int(state.ui_tool_started_at[call.id] * 1000), iteration_id="iter:1", tool_registry=host.registry)]
    async def emit(kind, payload):
        events.append(AgentEvent(type=kind, data=payload))
    host.context.emit_event = emit
    async def approve(call_id):
        planned = next(event for event in events if event.type == "tool_call" and event.data.get("command_presentation"))
        request = next(event for event in events if event.type == "approval_request")
        assert events.index(planned) < events.index(request)
        assert call_id == planned.data["id"] == request.data["tool_call_id"]
        assert planned.data["status"] == "pending"
        assert planned.data["command_presentation"]["command"] == auth.request.arguments["command"]
        assert planned.data["command_presentation"]["cwd"] == str(auth.request.exec_policy.command.cwd)
        assert "execution_started_at" not in planned.data
        pending = snapshot(events)["tool_calls"][call_id]
        assert pending["transition"] == "waiting_approval"
        assert pending["streamingAnnouncement"] is False
        assert "executionStartedAt" not in pending and "durationMs" not in pending
        return {"action": "approve", "decision": "approved", "request_digest": auth.request.digest}
    async for event in runtime(host, state, call, auth, approval_handler=approve):
        events.append(event)
    result = next(event for event in events if event.type == "tool_result")
    starts = [event for event in events if event.type == "runtime.span" and event.data["event"] == "tool.started"]
    assert len(starts) == 1
    assert result.data["id"] == starts[0].data["tool_call_id"] == call.id
    assert result.data["exit_code"] == 0
    assert "NATIVE_ACTIVITY_RESULT" in result.data["stdout_preview"]
    assert result.data["execution_started_at"] == starts[0].data["started_at"]
    presentations = [event.data["command_presentation"] for event in events if "command_presentation" in event.data]
    assert presentations and all(presentation == presentations[0] for presentation in presentations)
    assert tool_call_start_event(call, started_epoch=time.time(), iteration_id="iter:1",
        tool_registry=host.registry).data["command_presentation"] == presentations[0]
    complete = next(event for event in events if event.type == "runtime.span" and event.data["event"] == "tool.completed")
    assert result.data["duration_ms"] == complete.data["data"]["service_duration_ms"]
    restored = project_public_tool_call(snapshot(events)["tool_calls"][call.id])
    assert restored["commandPresentation"] == presentations[0]
    assert restored["executionStartedAt"] == result.data["execution_started_at"]
    assert restored["exitCode"] == 0
    assert restored["status"] == "success"
    assert restored["streamingAnnouncement"] is False
    assert state.tool_calls[-1].result_payload["command_presentation"] == presentations[0]


@pytest.mark.asyncio
async def test_native_abort_commits_real_declined_item_without_execution_measurement_then_cancels_original_owner(tmp_path):
    host = setup(tmp_path)
    host.context.cancel_event = asyncio.Event()
    cancelled = []
    host.context.run_context.record_user_cancelled = cancelled.append
    call, auth = authorize(host, command='Set-Content -LiteralPath native-projection-never-ran.txt -Value "not approved"')
    state = AgentState(user_message="native activity abort")
    state.ui_tool_started_at[call.id] = time.time() - 30
    events = []
    async def emit(kind, payload):
        events.append(AgentEvent(type=kind, data=payload))
    host.context.emit_event = emit
    async def abort(call_id):
        return {"action": "reject", "decision": "abort", "request_digest": auth.request.digest}
    with pytest.raises(asyncio.CancelledError):
        async for event in runtime(host, state, call, auth, approval_handler=abort):
            events.append(event)
    result = next(event for event in events if event.type == "tool_result")
    assert result.data["id"] == call.id
    assert result.data["status"] == "cancelled"
    assert result.data["error_kind"] == "approval_aborted"
    assert "not executed" in result.data["summary"]
    assert not {"exit_code", "duration_ms", "execution_started_at"}.intersection(result.data)
    assert not any(event.type == "runtime.span" and event.data["event"] == "tool.started" for event in events)
    complete = next(event for event in events if event.type == "runtime.span" and event.data["event"] == "tool.completed")
    assert not {"started_at", "duration_ms"}.intersection(complete.data)
    assert state.tool_calls[-1].result_payload["command_presentation"] == result.data["command_presentation"]
    assert snapshot(events)["tool_calls"][call.id]["status"] == "cancelled"
    assert cancelled == ["user_cancelled"] and host.context.cancel_event.is_set()
    assert not (host.workspace / "native-projection-never-ran.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("guardian_status", ["denied", "timed_out"])
async def test_native_guardian_rejection_retains_its_receipt_without_claiming_a_user_abort(tmp_path, monkeypatch, guardian_status):
    host = setup(tmp_path)
    host.context.cancel_event = asyncio.Event()
    cancelled = []
    host.context.run_context.record_user_cancelled = cancelled.append
    call, auth = authorize(host, command='Set-Content -LiteralPath guardian-projection-never-ran.txt -Value "not approved"')
    state = AgentState(user_message="native automatic refusal")
    events = []
    async def review(request, context, registry):
        return {"action": "reject", "request_digest": request.digest, "guardian_review_id": "real-host-review",
            "guardian_status": guardian_status, "guidance": "Original automatic reviewer refusal"}
    monkeypatch.setattr("backend.agent.guardian.tool_approval.review_tool_request", review)
    async for event in runtime(host, state, call, auth, approval_handler=None):
        events.append(event)
    result = next(event for event in events if event.type == "tool_result")
    assert result.data["status"] == "blocked" and result.data["error_kind"] == "approval_rejected"
    assert "Original automatic reviewer refusal" in result.data["summary"]
    assert result.data["command_presentation"]["command"] == auth.request.arguments["command"]
    assert not {"exit_code", "duration_ms", "execution_started_at"}.intersection(result.data)
    assert not any(event.type == "approval_request" for event in events)
    assert state.tool_calls[-1].result_payload["id"] == call.id
    assert cancelled == [] and not host.context.cancel_event.is_set()
    assert not (host.workspace / "guardian-projection-never-ran.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["expired", "timeout", "invalid_response", "digest_mismatch"])
async def test_native_host_approval_rejections_do_not_become_user_abort(tmp_path, reason):
    host = setup(tmp_path)
    host.context.cancel_event = asyncio.Event()
    cancelled = []
    host.context.run_context.record_user_cancelled = cancelled.append
    call, auth = authorize(host, command='Set-Content -LiteralPath host-projection-never-ran.txt -Value "not approved"')
    state = AgentState(user_message="native host approval rejection")
    if reason in {"expired", "timeout"}:
        host.context.deadline_monotonic = time.monotonic() + (-1 if reason == "expired" else 0.01)
    async def respond(call_id):
        if reason == "timeout":
            await asyncio.Event().wait()
        if reason == "invalid_response":
            return "not an approval response"
        return {"action": "approve", "decision": "approved", "request_digest": "mismatched-digest"}
    events = [event async for event in runtime(host, state, call, auth, approval_handler=respond)]
    result = next(event for event in events if event.type == "tool_result")
    assert result.data["status"] == "blocked" and result.data["error_kind"] == "approval_rejected"
    assert not {"exit_code", "duration_ms", "execution_started_at"}.intersection(result.data)
    assert result.data["command_presentation"]["command"] == auth.request.arguments["command"]
    assert cancelled == [] and not host.context.cancel_event.is_set()
    assert not (host.workspace / "host-projection-never-ran.txt").exists()
