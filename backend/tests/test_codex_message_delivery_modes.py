from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent import parent_notification_outbox
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.runtime_records import _swarm_message_from_dict
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_control_plane import canonical_agent_operation
from backend.tools.subagent_context import AgentExecutionProfile, subagent_toolset_policy
from backend.tools.swarm_tools import FollowupTaskTool, SendMessageTool
from backend.ws.run_manager import SessionRunManager


def test_new_tools_are_distinct_operations_and_followup_stays_under_child_capability_ceiling():
    assert canonical_agent_operation("send_message").operation == "message"
    assert canonical_agent_operation("followup_task").operation == "followup"
    assert FollowupTaskTool().get_schema().parameters["required"] == ["target", "message"]
    policy = subagent_toolset_policy(execution_profile=AgentExecutionProfile(message_coordination=True, agent_lifecycle=False))
    assert "followup_task" in policy.disabled_tools
    assert "send_message" not in policy.disabled_tools


def test_durable_idle_claims_skip_queue_prefix_and_reopen_without_losing_mail(tmp_path):
    directory = tmp_path / "swarm"
    runtime = AgentRuntime(swarm_store_dir=directory, enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", conversation_id="conv")
    child = runtime.start_subagent(subagent_id="worker", parent_run_id="parent", agent_type="general-purpose")
    for index in range(110):
        runtime.send_swarm_message(sender_id="parent", recipient_id="worker", content=f"queued-{index}", conversation_id="conv", delivery_mode="queue_only")
    turn = runtime.send_swarm_message(sender_id="parent", recipient_id="worker", content="start next turn", conversation_id="conv", delivery_mode="trigger_turn")
    claims = runtime.claim_swarm_messages(participant_id="worker", mailbox_epoch=child.mailbox_epoch, conversation_id="conv", trigger_turn_only=True)
    assert [claim.message.message_id for claim in claims] == [turn.message_id]
    runtime.ack_swarm_message_claims(claims)
    runtime.close(release_lease=True)
    restored = AgentRuntime(swarm_store_dir=directory, enable_lease_heartbeat=False)
    try:
        messages = restored.list_swarm_messages(participant_id="worker", conversation_id="conv", limit=100)
        assert messages[-1].delivery_mode == "trigger_turn"
        claims = restored.claim_swarm_messages(participant_id="worker", mailbox_epoch=child.mailbox_epoch, conversation_id="conv")
        assert len(claims) == 100 and all(claim.message.delivery_mode == "queue_only" for claim in claims)
        restored.ack_swarm_message_claims(claims)
        remaining = restored.claim_swarm_messages(participant_id="worker", mailbox_epoch=child.mailbox_epoch, conversation_id="conv")
        assert len(remaining) == 10
    finally:
        restored.close(release_lease=True)


def test_parent_queue_marker_is_deliverable_but_does_not_start_idle_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(parent_notification_outbox, "OUTBOX_ROOT", tmp_path / "outbox")
    parent_notification_outbox.enqueue_parent_notification(parent_run_id="parent", conversation_id="conv", session_id="session",
        subagent_id="worker", payload={"message_id": "queue-message", "delivery_mode": "queue_only"}, kind="mailbox_wake", idempotency_key="mailbox_wake:queue-message")
    manager = SessionRunManager(SimpleNamespace(session_id="session"))
    assert manager._has_replayable_parent_notification("conv") is False
    assert parent_notification_outbox.load_parent_outbox(conversation_id="conv").replayable()[0].payload["message_id"] == "queue-message"
    parent_notification_outbox.enqueue_parent_notification(parent_run_id="parent", conversation_id="conv", session_id="session",
        subagent_id="worker", payload={"message_id": "followup-message", "delivery_mode": "trigger_turn"}, kind="mailbox_wake", idempotency_key="mailbox_wake:followup-message")
    assert manager._has_replayable_parent_notification("conv") is True


def test_old_messages_keep_their_recorded_wake_behavior():
    record = _swarm_message_from_dict({"message_id": "old", "sender_id": "parent", "recipient_id": "worker", "content": "old accepted work"})
    assert record.delivery_mode == "trigger_turn"
    assert _swarm_message_from_dict(record.to_dict()).delivery_mode == "trigger_turn"


def test_queue_only_completed_target_does_not_require_task_tool_or_claim_new_resources(tmp_path):
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    try:
        runtime.start_run(run_id="parent", conversation_id="conv")
        child = runtime.start_subagent(subagent_id="worker", parent_run_id="parent", agent_type="general-purpose")
        runtime.complete_subagent("worker", status="completed", summary="original result", agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="conv",
            metadata={"run_id": "parent"}, run_context=RunContext(agent_runtime=runtime))
        result = asyncio.run(SendMessageTool().execute({"recipient": "worker", "message": "queue only"}, context=context))
        assert not result.is_error
        assert runtime.get_subagent("worker").status == "completed"
        assert runtime.get_subagent("worker").mailbox_epoch == child.mailbox_epoch
        assert runtime.list_swarm_messages(participant_id="worker", conversation_id="conv")[-1].delivery_mode == "queue_only"
        followup = asyncio.run(FollowupTaskTool().execute({"target": "worker", "message": "start work"}, context=context))
        assert followup.is_error and "resume runtime is unavailable" in followup.content
        assert runtime.get_subagent("worker").status == "completed"
        assert runtime.get_subagent("worker").mailbox_epoch == child.mailbox_epoch
    finally:
        runtime.close(release_lease=True)
