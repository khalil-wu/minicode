"""Source final-item deferral keeps already-pending explicit input in its turn."""
from __future__ import annotations

import asyncio

import pytest

from backend.agent.message import UserCommand
from backend.agent.runtime import AgentRuntime
from backend.agent.turn_input import TurnInputQueue
from backend.llm.base import StreamEvent, StreamEventType
from backend.tests.test_provider_text_boundary_chain import (
    _done, _text, collect_boundary_query, make_boundary_query,
)


def _final_item():
    return StreamEvent(type=StreamEventType.TEXT_CHUNK, item_id="candidate", phase="final_answer",
        lifecycle="item_completed", provider_items=[{"type": "message", "id": "candidate", "role": "assistant",
            "phase": "final_answer", "content": [{"type": "output_text", "text": "Candidate answer"}]}])


def test_user_steer_queued_before_final_item_reaches_the_same_actual_query(tmp_path):
    queue = TurnInputQueue()
    command = UserCommand(type="user_message", data={"content": "Use the new requirement",
        "user_message_id": "steered-user", "assistant_message_id": "steered-answer"})

    async def first_response():
        yield _text("Candidate answer", item="candidate")
        assert queue.enqueue_command(command, expected_turn_id=queue.turn_id) is not None
        yield _final_item()
        yield _done()

    query = make_boundary_query(tmp_path, [first_response, [_text("Answer to the new requirement"), _done()]])
    query.submission.runtime.run_context.turn_input_queue = queue
    events = asyncio.run(collect_boundary_query(query))
    assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
    assert len(query.adapter.request_messages) == 2
    assert any(message.is_user_input and "Use the new requirement" in message.content
        for message in query.adapter.request_messages[1])
    assert query.state.reply == "Answer to the new requirement"
    assert queue.pending_count() == 0


@pytest.mark.parametrize("delivery_mode", ["queue_only", "trigger_turn"])
def test_actual_final_item_keeps_trigger_mail_current_and_defers_queue_only_mail(tmp_path, delivery_mode):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)
    queue = TurnInputQueue()

    async def first_response():
        yield _text("Candidate answer", item="candidate")
        child = runtime.start_subagent(subagent_id="worker", parent_run_id=queue.turn_id,
            agent_type="default", agent_path_segment="worker")
        runtime.send_swarm_message(sender_id=child.subagent_id, recipient_id="parent", content="Actual explicit input",
            author_agent_path="/root/worker", recipient_agent_path="/root", conversation_id="text-boundary",
            sender_mailbox_epoch=child.mailbox_epoch, delivery_mode=delivery_mode, notify_parent=False)
        yield _final_item()
        yield _done()

    responses = [first_response]
    if delivery_mode == "trigger_turn":
        responses.append([_text("Answer after explicit agent input"), _done()])
    query = make_boundary_query(tmp_path, responses)
    query.submission.runtime.run_context.agent_runtime = runtime
    query.submission.runtime.run_context.turn_input_queue = queue

    async def scenario():
        try:
            events = await collect_boundary_query(query)
            assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
            assert len(query.adapter.request_messages) == (2 if delivery_mode == "trigger_turn" else 1)
            if delivery_mode == "trigger_turn":
                native = [item for message in query.adapter.request_messages[1]
                    for item in message.provider_items if item["type"] == "agent_message"]
                assert len(native) == 1 and native[0]["author"] == "/root/worker"
                assert "Actual explicit input" in native[0]["content"][0]["text"]
                assert query.state.reply == "Answer after explicit agent input"
            else:
                assert query.state.reply == "Candidate answer"
            assert runtime.has_pending_agent_input(agent_id=queue.turn_id,
                conversation_id="text-boundary") is (delivery_mode == "queue_only")
        finally:
            await query.session.aclose()
            await runtime.aclose(release_lease=True)

    asyncio.run(scenario())


def test_trigger_mail_probe_uses_the_actual_conversation_and_child_epoch(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)
    runtime.start_run(run_id="root", conversation_id="conversation")
    child = runtime.start_subagent(subagent_id="worker", parent_run_id="root", agent_type="default")
    try:
        assert runtime.complete_subagent(child.subagent_id, "completed",
            agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch) is not None
        runtime.send_swarm_message(sender_id="root", recipient_id=child.subagent_id, content="Next incarnation",
            conversation_id="conversation", recipient_mailbox_epoch=child.mailbox_epoch + 1,
            delivery_mode="trigger_turn")
        assert not runtime.has_pending_agent_input(agent_id=child.subagent_id,
            conversation_id="conversation", trigger_turn_only=True)
        current = runtime.start_subagent(subagent_id="worker", parent_run_id="root", agent_type="default")
        assert current.mailbox_epoch == child.mailbox_epoch + 1
        assert not runtime.has_pending_agent_input(agent_id=child.subagent_id,
            conversation_id="another-conversation", trigger_turn_only=True)
        assert runtime.has_pending_agent_input(agent_id=child.subagent_id,
            conversation_id="conversation", trigger_turn_only=True)
    finally:
        runtime.close(release_lease=True)
