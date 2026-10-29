"""Pending Source activity and bounded mailbox work in long conversations."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.agent.message import UserCommand
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.swarm_store import FileSwarmStore
from backend.agent.turn_input import TurnInputQueue
from backend.config import AppConfig, LLMSettings
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_control_plane import AgentControlPlane
from backend.tools.collaboration_tools import ListAgentsTool, WaitAgentTool, wait_for_coordination


def _runtime(root):
    return AgentRuntime(metrics_file=root / "metrics.jsonl", swarm_store_dir=root / "swarm",
        enable_lease_heartbeat=False)


def _context(runtime, actor="root", *, child=False):
    queue = TurnInputQueue()
    queue.begin_turn(actor)
    return ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="conversation",
        metadata={"run_id": actor, **({"agent_id": actor, "agent_mode": "subagent"} if child else {})},
        run_context=RunContext(agent_runtime=runtime, turn_input_queue=queue, multi_agent_version="v2"))


def _refs(claims):
    return [{"message_id": claim["message"]["message_id"], "participant_id": claim["participant_id"],
        "mailbox_epoch": claim["mailbox_epoch"], "claim_token": claim["claim_token"]} for claim in claims]


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
async def test_wait_reports_pending_mail_until_consumed_and_ignores_old_completion(tmp_path, cold):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = runtime.start_subagent(subagent_id="worker", parent_run_id="root", agent_type="default")
    runtime.send_swarm_message(sender_id="worker", recipient_id="parent", content="already queued",
        conversation_id="conversation", sender_mailbox_epoch=child.mailbox_epoch,
        delivery_mode="queue_only", notify_parent=False)
    runtime.complete_subagent("worker", agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
    if cold:
        assert runtime.close(release_lease=True)
        runtime = _runtime(tmp_path)
        runtime.start_run(run_id="root-next", conversation_id="conversation")
    context = _context(runtime, "root-next" if cold else "root")
    context.metadata["_agent_wait_cursor"] = 0
    try:
        assert await wait_for_coordination(context, timeout_seconds=0) == {"kind": "message"}
        assert await wait_for_coordination(context, timeout_seconds=0) == {"kind": "message"}
        claims = runtime.claim_swarm_messages(participant_id="parent", mailbox_epoch=0, conversation_id="conversation")
        assert [claim.message.content for claim in claims] == ["already queued"]
        assert runtime.ack_swarm_message_claims(claims) == 1
        assert await wait_for_coordination(context, timeout_seconds=0) is None
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_wait_reports_pending_completion_only_until_model_admission(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = runtime.start_subagent(subagent_id="worker", parent_run_id="root", agent_type="default", background=True)
    runtime.store_subagent_result("worker", status="completed", content="Actual answer",
        agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
    runtime.complete_subagent("worker", agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
    context = _context(runtime)
    try:
        assert await wait_for_coordination(context, timeout_seconds=0) == {"kind": "message"}
        notification, = runtime.list_parent_notifications(parent_run_id="root", conversation_id="conversation", status="pending")
        runtime.mark_parent_notification_delivered(notification["notification_id"], parent_run_id="root", conversation_id="conversation")
        assert await wait_for_coordination(context, timeout_seconds=0) is None
        runtime.ack_parent_notification(notification["notification_id"], parent_run_id="root", conversation_id="conversation")
        assert await wait_for_coordination(context, timeout_seconds=0) is None
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_wait_distinguishes_message_board_arrival_from_user_steer(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    context = _context(runtime)
    try:
        waiting = asyncio.create_task(wait_for_coordination(context, timeout_seconds=2))
        await asyncio.sleep(0)
        queue = context.run_context.turn_input_queue
        assert queue.deliver_message_board_notification("channel update", expected_turn_id="root")
        assert await asyncio.wait_for(waiting, timeout=1) == {"kind": "message"}
        assert queue.pending_count() == 0 and queue.take_message_board_notifications() == ("channel update",)
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_wait_does_not_miss_steer_between_pending_check_and_subscription(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    context = _context(runtime)
    queue = context.run_context.turn_input_queue

    def pending_input(**kwargs):
        queue.enqueue_command(UserCommand(type="user_message", data={"content": "Current instruction",
            "user_message_id": "steer", "conversation_id": "conversation"}))
        return False

    monkeypatch.setattr(runtime, "has_pending_agent_input", pending_input)
    try:
        assert await asyncio.wait_for(wait_for_coordination(context, timeout_seconds=2), timeout=1) == {"kind": "steer"}
        assert queue.pending_count() == 1
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_wait_cancellation_does_not_report_completed(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    context = _context(runtime)
    context.cancel_event = asyncio.Event()
    try:
        waiting = asyncio.create_task(wait_for_coordination(context, timeout_seconds=2))
        await asyncio.sleep(0)
        context.cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_child_wait_ignores_sibling_completion_and_receives_its_mail(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    runtime.start_subagent(subagent_id="worker", parent_run_id="root", agent_type="default")
    sibling = runtime.start_subagent(subagent_id="sibling", parent_run_id="root", agent_type="default")
    context = _context(runtime, "worker", child=True)
    entered, unrelated_ignored = asyncio.Event(), asyncio.Event()
    original = runtime.wait_for_agent_activity
    calls = 0

    async def observe(*args, **kwargs):
        nonlocal calls
        calls += 1
        (entered if calls == 1 else unrelated_ignored).set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, "wait_for_agent_activity", observe)
    try:
        waiting = asyncio.create_task(wait_for_coordination(context, timeout_seconds=2))
        await asyncio.wait_for(entered.wait(), timeout=1)
        runtime.complete_subagent("sibling", agent_path=sibling.agent_path, mailbox_epoch=sibling.mailbox_epoch)
        await asyncio.wait_for(unrelated_ignored.wait(), timeout=1)
        assert not waiting.done()
        runtime.send_swarm_message(sender_id="root", recipient_id="worker", content="For this worker",
            conversation_id="conversation", delivery_mode="queue_only")
        assert (await waiting)["kind"] == "message"
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_wait_reports_source_timeout_clamping(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    context = _context(runtime)
    context.run_context.turn_input_queue.deliver_message_board_notification("pending", expected_turn_id="root")
    tool = WaitAgentTool(AppConfig(llm=LLMSettings(api_key="fixture")))
    try:
        result = json.loads((await tool.execute({"timeout_ms": 1}, context)).content)
        assert result == {"message": "Wait completed.\n\nRequested timeout of 1ms was clamped to the minimum of "
            + str(tool.options["min_wait_timeout_ms"]) + "ms.", "timed_out": False}
    finally:
        await runtime.aclose(release_lease=True)


def test_claim_skips_acked_and_live_claimed_history_but_replays_older_expired_mail(tmp_path, monkeypatch):
    from backend.agent import swarm_store

    store = FileSwarmStore(tmp_path / "swarm")

    def append(message_id, *, epoch=4, conversation="conversation"):
        return store.append_message({"message_id": message_id, "sender_id": "root", "recipient_id": "worker",
            "recipient_mailbox_epoch": epoch, "conversation_id": conversation, "content": "payload"})

    append("older-expired")
    for index in range(60):
        append(f"acked-{index}")
    first = store.claim_messages(participant_id="worker", mailbox_epoch=4, claim_owner="old-owner",
        conversation_id="conversation", now_ms=1000, lease_ms=1000)
    assert store.ack_message_claims(_refs(first[1:]), claim_owner="old-owner") == 60
    append("live-claim")
    live = store.claim_messages(participant_id="worker", mailbox_epoch=4, claim_owner="live-owner",
        conversation_id="conversation", now_ms=1500, lease_ms=1000)
    assert [claim["message"]["message_id"] for claim in live] == ["live-claim"]
    append("fresh")
    append("stale-epoch", epoch=3)
    append("foreign-conversation", conversation="other")
    decoded = []
    original_json = swarm_store.json

    def loads(value):
        message = original_json.loads(value)
        if isinstance(message, dict) and "message_id" in message:
            decoded.append(message["message_id"])
        return message

    monkeypatch.setattr(swarm_store, "json", SimpleNamespace(loads=loads, dumps=original_json.dumps))
    claims = store.claim_messages(participant_id="worker", mailbox_epoch=4, claim_owner="new-owner",
        conversation_id="conversation", now_ms=2200)
    assert [claim["message"]["message_id"] for claim in claims] == ["older-expired", "fresh"]
    assert decoded == ["older-expired", "fresh"]
    assert store.ack_message_claims(_refs(claims), claim_owner="old-owner") == 0
    assert store.ack_message_claims(_refs(claims), claim_owner="new-owner") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [None, "/root/worker_00"])
async def test_agent_listing_reuses_paths_and_does_not_read_shared_boards(tmp_path, monkeypatch, prefix):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    runtime.configure_subagent_limit("conversation", 20, source_v2=True)
    for index in reversed(range(12)):
        runtime.start_subagent(subagent_id=f"child-{index}", parent_run_id="root", agent_type="default",
            task_id=f"worker_{index:02}", agent_path_segment=f"worker_{index:02}")
    runtime.start_run(run_id="foreign", conversation_id="other")
    runtime.start_subagent(subagent_id="foreign-child", parent_run_id="foreign", agent_type="default")
    original = runtime.list_runs
    reads = []

    def list_runs(**kwargs):
        reads.append(kwargs)
        return original(**kwargs)

    def unrelated_read(**kwargs):
        pytest.fail("agent control must not load shared board tables")

    monkeypatch.setattr(runtime, "list_runs", list_runs)
    for method in ("list_swarm_messages", "list_swarm_tasks", "list_swarm_teams"):
        monkeypatch.setattr(runtime, method, unrelated_read)
    try:
        args = {} if prefix is None else {"path_prefix": prefix}
        result = json.loads((await ListAgentsTool().execute(args, _context(runtime))).content)
        expected = [f"/root/worker_{index:02}" for index in range(12)]
        assert [agent["agent_name"] for agent in result["agents"]] == ([expected[0]] if prefix else ["/root", *expected])
        assert len(reads) <= 4 and all(read["include_swarm"] is False for read in reads)
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
async def test_agent_listing_resolves_relative_prefix_as_path_segments(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    runtime.configure_subagent_limit("conversation", 20, source_v2=True)
    runtime.start_subagent(subagent_id="branch", parent_run_id="root", agent_type="default",
        task_id="branch", agent_path_segment="branch")
    runtime.start_subagent(subagent_id="worker", parent_run_id="branch", agent_type="default",
        task_id="worker", agent_path_segment="worker")
    runtime.start_subagent(subagent_id="nested", parent_run_id="worker", agent_type="default",
        task_id="nested", agent_path_segment="nested")
    runtime.start_subagent(subagent_id="worker_other", parent_run_id="branch", agent_type="default",
        task_id="worker_other", agent_path_segment="worker_other")
    context = _context(runtime, "branch", child=True)
    context.metadata["_agent_execution_profile"] = {"agent_lifecycle": True}
    try:
        listing = await ListAgentsTool().execute({"path_prefix": "worker"}, context)
        assert [item["agent_name"] for item in json.loads(listing.content)["agents"]] == [
            "/root/branch/worker", "/root/branch/worker/nested"]
        listing = await ListAgentsTool().execute({"path_prefix": "/root/branch/work"}, context)
        assert json.loads(listing.content)["agents"] == []
        invalid = await ListAgentsTool().execute({"path_prefix": ""}, context)
        assert invalid.is_error and "agent path must not be empty" in invalid.content
    finally:
        await runtime.aclose(release_lease=True)


def test_legacy_public_paths_keep_nested_parent_edges(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="root", conversation_id="conversation")
    runtime.start_subagent(subagent_id="branch", parent_run_id="root", agent_type="default",
        task_id="branch", agent_path_segment="branch")
    runtime.start_subagent(subagent_id="nested", parent_run_id="branch", agent_type="default",
        task_id="nested", agent_path_segment="nested")
    context = _context(runtime)
    context.run_context.multi_agent_version = "v1"
    try:
        assert AgentControlPlane(context).public_tree_paths() == {"branch": "/root/branch", "nested": "/root/branch/nested"}
    finally:
        runtime.close(release_lease=True)
