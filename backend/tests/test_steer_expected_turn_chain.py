"""Captured turn identity survives transport delay and queued-message controls."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.message import UserCommand
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.turn_input import ScheduledInputCommand, TurnInputQueue
from backend.tests.test_conversation_window_follower import windows
from backend.ws.stream_state import create_stream_state


def queued(owner, conversation, name="queued"):
    command = UserCommand("user_message", {"conversation_id": conversation.id, "content": "Original queued input",
        "assistant_message_id": name, "user_message_id": name + "-user", "client_command_id": name + "-input",
        "_client_queue_key": owner.run_manager.durable_client_commands.path.stem})
    owner.run_manager.enqueue_user_message(conversation.id, command)
    return command


async def next_turn(owner, conversation, old_task):
    cancel = owner.run_manager.cancel_events[conversation.id]
    old_task.cancel()
    await asyncio.gather(old_task, return_exceptions=True)
    owner._cleanup_agent_run(conversation_id=conversation.id, task=old_task, task_id="actual-task", cancel_event=cancel)
    task = asyncio.create_task(asyncio.Event().wait())
    owner._register_agent_run(conversation_id=conversation.id, task=task, task_id="next-task", cancel_event=asyncio.Event())
    owner._conversation_streams[conversation.id] = create_stream_state(conversation.id, "next-assistant", "next-turn")
    owner.run_manager.turn_input_queue(conversation.id).begin_turn("next-turn")
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,reason", [("actual-turn", "steer_target_mismatch"), ("", "steer_target_required")])
async def test_delayed_direct_steer_cannot_target_the_newer_turn(windows, expected, reason):
    _, owner, _, conversation, _, start, drain = windows
    task = await next_turn(owner, conversation, start())
    await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
        "client_command_id": "late-steer", "conversation_id": conversation.id, "content": "Retain this draft",
        "user_message_id": "late-user", "assistant_message_id": "late-assistant",
        "streaming_behavior": "steer", "expected_turn_id": expected}), owner.connection_generation)
    await drain()
    result = next(item for item in owner.ws.sent if item["type"] == "command.result" and item.get("client_command_id") == "late-steer")
    assert result["level"] == "error" and result["data"]["reason"] == reason
    assert result["data"]["admitted"] is result["data"]["queued"] is False
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert owner.run_manager.turn_input_queue(conversation.id).snapshot() == ()
    assert owner.run_manager.queued_user_messages(conversation.id) == []
    assert not any(item["type"] == "done" and item.get("message_id") == "next-assistant" for item in owner.ws.sent)


@pytest.mark.asyncio
async def test_valid_direct_steer_keeps_atomic_completion_when_secondary_log_fails(windows, monkeypatch):
    _, owner, _, conversation, _, start, drain = windows
    start()
    data = {"client_command_id": "valid-steer", "conversation_id": conversation.id, "content": "One redirect",
        "user_message_id": "redirect-user", "assistant_message_id": "redirect-assistant",
        "streaming_behavior": "steer", "expected_turn_id": "actual-turn"}
    dispatcher = owner.command_dispatcher
    assert owner.run_manager.durable_client_commands.persist_client_command(UserCommand("user_message", dict(data)))
    reject_approvals = AsyncMock(wraps=owner._reject_pending_approvals)
    monkeypatch.setattr(owner, "_reject_pending_approvals", reject_approvals)

    def fail_completion_record(command, **options):
        raise OSError("completion record write failed")

    with monkeypatch.context() as patch:
        patch.setattr(dispatcher, "_mark_client_command_seen", fail_completion_record)
        with pytest.raises(OSError, match="completion record write failed"):
            await dispatcher._run_durable_client_command("valid-steer", owner.connection_generation)
    assert dispatcher._client_command_seen(UserCommand("user_message", dict(data)))
    assert len(owner.run_manager.turn_input_queue(conversation.id).snapshot()) == 1
    await dispatcher._run_durable_client_command("valid-steer", owner.connection_generation)
    await drain()
    items = owner.run_manager.turn_input_queue(conversation.id).snapshot()
    assert len(items) == 1
    assert items[0].original_command.data["expected_turn_id"] == "actual-turn"
    assert owner.run_manager.queued_user_messages(conversation.id) == []
    assert dispatcher._client_command_seen(UserCommand("user_message", dict(data)))
    assert not owner.run_manager.durable_client_commands.has_client_command("valid-steer")
    assert reject_approvals.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["agent_mode", "permission_mode", "workspace_root"])
async def test_steer_cannot_silently_become_follow_up_when_execution_settings_change(windows, field, tmp_path):
    _, owner, _, conversation, _, start, drain = windows
    task = start()
    owner._conversation_streams[conversation.id]["agent_mode"] = "build"
    different_workspace = tmp_path / "different-workspace"
    different_workspace.mkdir()
    values = {"agent_mode": "plan", "permission_mode": "bypass", "workspace_root": str(different_workspace)}
    await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
        "client_command_id": "changed-" + field, "conversation_id": conversation.id, "content": "Keep this input",
        "user_message_id": "changed-user", "assistant_message_id": "changed-assistant",
        "streaming_behavior": "steer", "expected_turn_id": "actual-turn", field: values[field]}), owner.connection_generation)
    await drain()
    result = next(item for item in owner.ws.sent if item["type"] == "command.result" and item.get("client_command_id") == "changed-" + field)
    assert result["level"] == "error"
    assert result["data"]["reason"] == "steer_configuration_mismatch" and result["data"]["field"] == field
    assert result["data"]["admitted"] is result["data"]["queued"] is False
    assert owner.run_manager.turn_input_queue(conversation.id).snapshot() == ()
    assert owner.run_manager.queued_user_messages(conversation.id) == []
    assert owner.run_manager.running_task_for(conversation.id) is task
    saved = owner.conversation_repo.get_conversation(conversation.id)
    assert saved.workspace_root == conversation.workspace_root and saved.permission_mode == conversation.permission_mode
    assert saved.agent_mode == conversation.agent_mode
    assert not any(item["type"] == "done" for item in owner.ws.sent)


@pytest.mark.asyncio
async def test_stale_queued_steer_preserves_original_queue_bytes_and_pause(windows):
    _, owner, _, conversation, _, start, drain = windows
    task = await next_turn(owner, conversation, start())
    original = queued(owner, conversation)
    owner.run_manager.set_user_queue_paused(conversation.id, "previous-stop")
    before = owner.run_manager.durable_queue.path.read_bytes()
    await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.steer", {
        "client_command_id": "stale-promotion", "conversation_id": conversation.id,
        "message_id": "queued", "expected_turn_id": "actual-turn"}), owner.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_messages(conversation.id) == [original]
    assert owner.run_manager.durable_queue.path.read_bytes() == before
    assert owner.run_manager.user_queue_paused(conversation.id)
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert any(item.get("data", {}).get("reason") == "steer_target_mismatch" for item in owner.ws.sent)
    assert not any(item["type"] == "user_message.queue.updated" and item.get("status") == "dequeued" for item in owner.ws.sent)


@pytest.mark.asyncio
async def test_valid_queued_steer_removes_only_the_selected_item_once(windows):
    _, owner, _, conversation, _, start, drain = windows
    task = start()
    original, tail = queued(owner, conversation), queued(owner, conversation, "tail")
    data = {"client_command_id": "valid-promotion", "conversation_id": conversation.id,
        "message_id": "queued", "expected_turn_id": "actual-turn"}
    for _ in range(2):
        await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.steer", dict(data)), owner.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_messages(conversation.id) == [tail]
    items = owner.run_manager.turn_input_queue(conversation.id).snapshot()
    assert len(items) == 1 and items[0].original_command is original
    assert original.data["expected_turn_id"] == "actual-turn"
    assert owner.run_manager.running_task_for(conversation.id) is task


@pytest.mark.asyncio
async def test_idle_queue_start_that_arrives_after_a_turn_starts_keeps_the_queue(windows):
    _, owner, _, conversation, _, start, drain = windows
    original = queued(owner, conversation)
    owner.run_manager.set_user_queue_paused(conversation.id, "stopped")
    task = start()
    await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.start", {
        "client_command_id": "late-start", "conversation_id": conversation.id, "message_id": "queued"}), owner.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_messages(conversation.id) == [original]
    assert owner.run_manager.user_queue_paused(conversation.id)
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert any(item.get("data", {}).get("reason") == "queue_start_busy" for item in owner.ws.sent)


@pytest.mark.asyncio
async def test_idle_queue_start_rejects_a_pending_dispatch_before_any_run_exists(windows):
    _, owner, _, conversation, _, _, drain = windows
    original = queued(owner, conversation)
    owner.run_manager.set_user_queue_paused(conversation.id, "stopped")
    assert owner.run_manager.begin_queue_dispatch(conversation.id)
    try:
        await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.start", {
            "client_command_id": "pending-start", "conversation_id": conversation.id, "message_id": "queued"}), owner.connection_generation)
        await drain()
        assert owner.run_manager.queued_user_messages(conversation.id) == [original]
        assert owner.run_manager.user_queue_paused(conversation.id)
        assert owner.run_manager.running_task_for(conversation.id) is None
        assert any(item.get("data", {}).get("reason") == "queue_start_busy" for item in owner.ws.sent)
    finally:
        owner.run_manager.finish_queue_dispatch(conversation.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["start", "steer"])
@pytest.mark.parametrize("sender", ["owner", "follower"])
async def test_queue_controls_require_their_original_conversation(windows, control, sender):
    _, owner, follower, conversation, _, start, drain = windows
    if control == "steer":
        start()
    original = queued(owner, conversation)
    owner.run_manager.set_user_queue_paused(conversation.id, "stopped")
    before = owner.run_manager.durable_queue.path.read_bytes()
    submitting = owner if sender == "owner" else follower
    await submitting.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue." + control, {
        "client_command_id": "ownerless-" + control, "message_id": "queued", "expected_turn_id": "actual-turn"}), submitting.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_messages(conversation.id) == [original]
    assert owner.run_manager.durable_queue.path.read_bytes() == before
    assert owner.run_manager.user_queue_paused(conversation.id)
    assert any(item["type"] == "command.result" and item.get("level") == "error" for item in submitting.ws.sent)


@pytest.mark.asyncio
async def test_queue_start_cannot_start_a_turn_beneath_an_existing_detached_query(windows):
    _, owner, _, conversation, _, _, drain = windows
    original = queued(owner, conversation)
    owner.run_manager.set_user_queue_paused(conversation.id, "stopped")
    guards = conversation_query_guards()
    claim = guards.try_start(conversation.id, owner_id="actual-detached-query")
    assert claim is not None
    try:
        await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.start", {
            "client_command_id": "detached-start", "conversation_id": conversation.id, "message_id": "queued"}), owner.connection_generation)
        await drain()
        assert owner.run_manager.queued_user_messages(conversation.id) == [original]
        assert owner.run_manager.user_queue_paused(conversation.id)
        assert owner.run_manager.running_task_for(conversation.id) is None
        assert any(item.get("data", {}).get("reason") == "queue_start_busy" for item in owner.ws.sent)
    finally:
        guards.end(claim)


@pytest.mark.asyncio
async def test_steer_after_its_turn_ends_does_not_start_the_queued_message(windows):
    _, owner, _, conversation, _, start, drain = windows
    task = start()
    cancel = owner.run_manager.cancel_events[conversation.id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    owner._cleanup_agent_run(conversation_id=conversation.id, task=task, task_id="actual-task", cancel_event=cancel)
    original = queued(owner, conversation)
    owner.run_manager.set_user_queue_paused(conversation.id, "stopped")
    await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.steer", {
        "client_command_id": "ended-turn", "conversation_id": conversation.id,
        "message_id": "queued", "expected_turn_id": "actual-turn"}), owner.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_messages(conversation.id) == [original]
    assert owner.run_manager.user_queue_paused(conversation.id)
    assert owner.run_manager.running_task_for(conversation.id) is None
    assert any(item.get("data", {}).get("reason") == "steer_target_unavailable" for item in owner.ws.sent)


@pytest.mark.asyncio
async def test_explicit_idle_start_uses_the_original_dispatch_once(windows, monkeypatch):
    _, owner, _, conversation, _, _, drain = windows
    original = queued(owner, conversation)
    owner.run_manager.set_user_queue_paused(conversation.id, "stopped")
    dispatch = AsyncMock(return_value=True)
    handle = owner.command_dispatcher._handle_command

    async def handle_control(command, *args, **kwargs):
        if command.type == "user_message":
            return await dispatch(command)
        return await handle(command, *args, **kwargs)

    monkeypatch.setattr(owner.command_dispatcher, "_handle_command", handle_control)
    data = {"client_command_id": "idle-start", "conversation_id": conversation.id, "message_id": "queued"}
    # Use the real durable command dispatcher; only the eventual model request
    # is held outside this queue-control and duplicate-request test.
    for _ in range(2):
        await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.start", dict(data)), owner.connection_generation)
    await asyncio.gather(*list(owner.command_dispatcher.execution_command_tasks))
    await drain()
    assert dispatch.await_count == 1 and dispatch.await_args.args[0] is original
    assert not owner.run_manager.user_queue_paused(conversation.id)
    assert owner.run_manager.queued_user_messages(conversation.id) == []


def test_queue_compares_captured_turn_before_publishing_durable_input():
    queue = TurnInputQueue()
    queue.begin_turn("first")
    queue.begin_turn("second")
    publish = Mock()
    assert queue.enqueue_command(UserCommand("user_message", {"content": "late"}),
        expected_turn_id="first", before_publish=publish) is None
    publish.assert_not_called()
    assert queue.snapshot() == ()


@pytest.mark.asyncio
async def test_old_scheduled_producer_cannot_rebind_to_the_new_turn(windows):
    _, owner, _, conversation, _, start, _ = windows
    await next_turn(owner, conversation, start())
    command = ScheduledInputCommand(type="user_message", data={"content": "old scheduled input"}, turn_trigger="automation_heartbeat_scheduled")
    with pytest.raises(ValueError, match="steer_target_mismatch"):
        owner.run_manager.enqueue_scheduled_input(conversation.id, command, expected_turn_id="actual-turn")
    assert owner.run_manager.turn_input_queue(conversation.id).snapshot() == ()
