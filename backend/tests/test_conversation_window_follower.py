"""Shared public thread notifications retain the actual window execution owner.

These exercise real session/outbox/dispatcher/queue/approval implementations;
held asyncio tasks delimit a turn without an external model request. Desktop
HTTP/tool/UI coverage is recorded separately, not inferred from these tests.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from starlette.websockets import WebSocketState

from backend.agent.message import AgentEvent, UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.manager import WebSocketManager
from backend.ws.payload_contracts import MAX_STREAM_RESUME_BLOCKS
from backend.ws.stream_state import create_stream_state


class Socket:
    def __init__(self):
        self.sent = []
        self.client_state = self.application_state = WebSocketState.CONNECTED

    async def send_json(self, payload):
        self.sent.append(deepcopy(payload))

    async def close(self, **_kwargs):
        self.client_state = self.application_state = WebSocketState.DISCONNECTED


@pytest_asyncio.fixture
async def windows(tmp_path):
    manager = WebSocketManager()
    sessions = []
    held = []

    def new(name):
        session = WebSocketSession(name, Socket(), None,
            ArtifactStore(storage_dir=tmp_path / "artifacts"), ToolRegistry(),
            PermissionChecker(PermissionSettings()), AppConfig(llm=LLMSettings(api_key="")),
            ws_manager=manager, mcp_manager=None, window_instance_id=name + "-instance",
            window_owner_id=name + "-owner", window_binding_token=name + "-binding")
        # Unit scope has no model/tool producer or workspace probe to refresh.
        # The explicit runtime-payload tests still call the real producer.
        session.session_lifecycle.schedule_task_runtime_update = lambda: None
        manager._sessions[name] = session
        sessions.append(session)
        return session

    owner, follower = new("owner-window"), new("follower-window")
    root = tmp_path / "workspace-a"
    root.mkdir()
    conversation = owner.conversation_repo.create_conversation(conversation_id="conv_window_a", workspace_root=str(root))
    root_b = tmp_path / "workspace-b"
    root_b.mkdir()
    other = owner.conversation_repo.create_conversation(conversation_id="conv_window_b", workspace_root=str(root_b))
    owner.active_conversation_id = follower.active_conversation_id = conversation.id

    def start():
        task = asyncio.create_task(asyncio.Event().wait())
        held.append(task)
        owner._register_agent_run(conversation_id=conversation.id, task=task,
            task_id="actual-task", cancel_event=asyncio.Event())
        owner._conversation_streams[conversation.id] = create_stream_state(conversation.id, "actual-assistant", "actual-turn")
        owner.run_manager.turn_input_queue(conversation.id)
        return task

    async def drain():
        for session in sessions:
            await session.event_outbox.drain_delivery()
            await session.event_outbox.drain_persistence()

    yield manager, owner, follower, conversation, other, start, drain
    for task in held:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await manager.shutdown(reason="follower-unit-complete")
    assert not manager._execution_watch_tasks
    assert not manager._execution_host_shutdown_tasks
    manager.reset_for_tests()


async def text(owner, conversation, content):
    await owner.send_event(AgentEvent(type="agent_message.delta", data={
        "conversation_id": conversation.id, "message_id": "actual-assistant", "turn_id": "actual-turn",
        "item_id": "answer-item", "delta": content, "phase": "final_answer",
    }))


@pytest.mark.asyncio
async def test_resume_observes_accumulated_and_later_events_without_a_second_execution(windows):
    manager, owner, follower, conversation, _, start, drain = windows
    task = start()
    await text(owner, conversation, "before resume")
    await follower.reemit_pending_state(conversation.id)
    await text(owner, conversation, " after resume")
    await drain()
    snapshot = next(item for item in follower.ws.sent if item["type"] == "stream_resume")
    delta = next(item for item in follower.ws.sent if item["type"] == "agent_message.delta")
    assert snapshot["content_blocks"][0]["content"] == "before resume"
    assert delta["source_event_seq"] == snapshot["event_seq"] + 1
    assert snapshot["seq"] < delta["seq"]
    assert delta["event_id"].startswith(follower.session_id + ":")
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert follower.run_manager.run_tasks == follower._conversation_streams == {}
    runtime = follower.runtime_snapshot()
    assert runtime["session_id"] == follower.session_id
    assert runtime["active_task_id"] == "actual-task"
    assert runtime["active_stream_conversation_ids"] == [conversation.id]


@pytest.mark.asyncio
async def test_resume_pages_and_subscription_are_serialized_against_new_deltas(windows, monkeypatch):
    manager, owner, follower, conversation, _, start, drain = windows
    start()
    owner._conversation_streams[conversation.id]["content_blocks"] = [
        {"type": "text", "itemId": f"item-{number}", "content": str(number)}
        for number in range(MAX_STREAM_RESUME_BLOCKS + 1)]
    entered, release = asyncio.Event(), asyncio.Event()
    send = follower.event_outbox.send_payload

    async def held_snapshot(payload, **options):
        result = await send(payload, **options)
        if payload["type"] == "stream_resume" and payload.get("snapshot_part") == 0:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(follower.event_outbox, "send_payload", held_snapshot)
    observing = asyncio.create_task(manager.observe_conversation_stream(follower, conversation.id))
    await asyncio.wait_for(entered.wait(), 5)
    publishing = asyncio.create_task(text(owner, conversation, "ordered delta"))
    await asyncio.sleep(0)
    assert not publishing.done()
    release.set()
    await asyncio.gather(observing, publishing)
    await drain()
    events = [item for item in follower.ws.sent if item["type"] in {"stream_resume", "agent_message.delta"}]
    assert [item["type"] for item in events] == ["stream_resume", "stream_resume", "agent_message.delta"]
    assert events[1]["snapshot_complete"] is True


@pytest.mark.asyncio
async def test_resume_after_terminal_uses_the_latest_durable_answer(windows):
    manager, owner, follower, conversation, _, start, drain = windows
    task = start()
    owner.conversation_repo.append_transcript_message(conversation.id, {
        "id": "actual-assistant", "role": "assistant", "content": "durable final", "terminal_status": "completed"})
    await owner.send_event(AgentEvent(type="done", data={"conversation_id": conversation.id,
        "message_id": "actual-assistant", "turn_id": "actual-turn", "status": "completed"}))
    owner.run_manager.mark_delivery_complete(conversation.id, "actual-task")
    await follower.reemit_pending_state(conversation.id)
    await drain()
    synced = next(item for item in follower.ws.sent if item["type"] == "session.synced")
    assert synced["active_conversation"]["transcript"][-1]["content"] == "durable final"
    assert synced["session"]["active_stream_conversation_ids"] == []
    assert synced["session"]["active_task_id"] is None
    assert not any(item["type"] == "stream_resume" for item in follower.ws.sent)
    assert not task.done()


@pytest.mark.asyncio
async def test_observer_disconnect_replays_its_own_chain_and_resumes_original_task(windows):
    manager, owner, follower, conversation, _, start, drain = windows
    task = start()
    await follower.reemit_pending_state(conversation.id)
    await owner.send_event(AgentEvent(type="agent.item", data={"conversation_id": conversation.id,
        "item_id": "durable-process", "kind": "process_text", "content": "process started", "status": "running"}))
    await drain()
    cursor = follower.event_outbox.current_replay_seq
    follower.mark_disconnected()
    await text(owner, conversation, "while follower disconnected")
    await owner.send_event(AgentEvent(type="agent.item", data={"conversation_id": conversation.id,
        "item_id": "durable-process", "kind": "process_text", "content": "real process completion", "status": "completed"}))
    await drain()
    missed, gap = follower.event_outbox.replay_window_after(cursor)
    assert gap is False
    assert missed[-1]["content"] == "real process completion"
    assert not any(item["type"] == "agent_message.delta" for item in missed)
    assert missed[-1]["event_id"].startswith(follower.session_id + ":")
    socket = Socket()
    follower.attach_websocket(socket, window_instance_id=follower.window_instance_id,
        window_owner_id=follower.window_owner_id, window_binding_token=follower._window_binding_token)
    await follower.reemit_pending_state(conversation.id)
    await drain()
    assert any(item["type"] == "stream_resume" for item in socket.sent)
    assert any("while follower disconnected" in str(item.get("content_blocks")) for item in socket.sent)
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert follower.run_manager.run_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["other", None])
async def test_background_observation_cannot_change_the_readers_workspace_or_permission(windows, selection):
    manager, owner, follower, conversation, other, start, drain = windows
    start()
    await follower.reemit_pending_state(conversation.id)
    selected = other.id if selection == "other" else None
    follower.active_conversation_id = selected
    permission = follower.permission_context = PermissionContext(mode="bypass")
    await text(owner, conversation, "background update")
    await owner.session_lifecycle.send_task_runtime_update(include_capabilities=False)
    await follower.session_lifecycle.send_task_runtime_update(include_capabilities=False)
    await drain()
    assert follower.active_conversation_id == selected
    assert follower.permission_context is permission
    assert follower.ws.sent[-1]["type"] == "task.update"
    assert follower.ws.sent[-1]["session"]["active_conversation_id"] == selected
    assert any(item.get("delta") == "background update" and item["conversation_id"] == conversation.id for item in follower.ws.sent)
    assert follower.window_instance_id == "follower-window-instance"
    assert owner.window_instance_id == "owner-window-instance"


@pytest.mark.asyncio
async def test_private_raw_and_window_capabilities_do_not_become_follower_notifications(windows):
    manager, owner, follower, conversation, _, start, drain = windows
    start()
    await follower.reemit_pending_state(conversation.id)
    await drain()
    before = len(follower.ws.sent)
    await owner.send_event(AgentEvent(type="stream_event", data={"conversation_id": conversation.id, "sdk_only": True, "raw": "private"}))
    await owner.send_event(AgentEvent(type="thinking_delta", data={"conversation_id": conversation.id,
        "content": "private raw reasoning", "source": "provider", "is_raw_provider_reasoning": True}))
    await owner.send_payload({"type": "terminal.output", "conversation_id": conversation.id, "session_id": "terminal-original",
        "data": "original terminal", "start_cursor": 0, "end_cursor": 17}, log_context="unit-terminal")
    await owner.session_lifecycle.send_task_runtime_update()
    await drain()
    assert len(follower.ws.sent) == before
    assert follower.turn_wait_state.pending_approval_payloads == {}
    assert follower.turn_wait_state.pending_approvals == {}


def control(conversation, request_id="real-request", command_id="follower-decision", **payload):
    return UserCommand("control_response", {"client_command_id": command_id, "request_id": request_id,
        "conversation_id": conversation.id, "turn_id": "actual-turn", "message_id": "actual-assistant",
        "response": {"subtype": "success", "response": payload}})


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype,lane,response", [
    ("can_use_tool", "approval", {"action": "approve", "request_digest": "original-digest"}),
    ("request_user_input", "user_input", {"answers": {"question": {"answers": ["original answer"]}}}),
    ("elicitation", "elicitation", {"action": "accept", "content": {"accepted": True}}),
])
async def test_control_decision_settles_only_original_waiter_and_replies_only_to_submitter(windows, subtype, lane, response):
    manager, owner, follower, conversation, _, start, drain = windows
    start()
    await owner.send_event(AgentEvent(type="approval_request", data={"conversation_id": conversation.id,
        "tool_call_id": "real-request", "tool_name": "run_command", "args": {"command": "read-only fixture"},
        "turn_id": "actual-turn", "message_id": "actual-assistant", "request_digest": "original-digest"}))
    pending = owner.turn_wait_state.pending_approval_payloads["real-request"]
    pending["request"]["subtype"] = subtype
    waiter = asyncio.get_running_loop().create_future()
    owner.turn_wait_state.register_waiter("real-request", waiter, kind=lane)
    await follower.reemit_pending_state(conversation.id)
    follower.permission_context = PermissionContext(mode="bypass")
    await follower.command_dispatcher._dispatch_client_command(control(conversation, **response), follower.connection_generation)
    await drain()
    resolved = waiter.result()
    assert {key: resolved[key] for key in response} == response
    assert resolved["request_digest"] == "original-digest"
    assert resolved["conversation_id"] == conversation.id
    assert resolved["turn_id"] == "actual-turn"
    assert follower.turn_wait_state.pending_approval_payloads == {}
    assert follower.turn_wait_state.pending_approvals == {}
    assert owner.turn_wait_state.pending_approval_payloads["real-request"] is pending
    assert any(item["type"] == "control_request" for item in follower.ws.sent)
    assert any(item.get("client_command_id") == "follower-decision" and item.get("level") == "success" for item in follower.ws.sent)
    assert not any(item.get("client_command_id") == "follower-decision" for item in owner.ws.sent)
    owner.turn_wait_state.remove_waiter("real-request")
    owner.turn_wait_state.pending_approval_payloads.pop("real-request")
    await follower.command_dispatcher._dispatch_client_command(control(conversation, command_id="late-decision", **response), follower.connection_generation)
    await drain()
    assert waiter.result() == resolved
    assert any(item.get("client_command_id") == "late-decision" and item.get("level") == "error" for item in follower.ws.sent)


@pytest.mark.asyncio
async def test_stale_stop_targets_cannot_cancel_the_actual_new_turn(windows):
    manager, owner, follower, conversation, _, start, drain = windows
    task = start()
    await follower.reemit_pending_state(conversation.id)
    owner.cancel_agent_runs = AsyncMock()
    for turn_id in ["older-turn", "actual-turn"]:
        await follower.command_dispatcher._dispatch_client_command(UserCommand("interrupt", {
            "conversation_id": conversation.id, "turn_id": turn_id, "message_id": "actual-assistant",
        }), follower.connection_generation)
    owner.cancel_agent_runs.assert_awaited_once_with(conversation_id=conversation.id, reason="user_interrupted")
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert follower.run_manager.run_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["steer", "follow_up"])
async def test_busy_input_keeps_original_upload_origin_and_background_execution_owner(windows, behavior):
    manager, owner, follower, conversation, other, start, drain = windows
    task = start()
    await follower.reemit_pending_state(conversation.id)
    owner.active_conversation_id = other.id
    attachment = {"artifact_id": "follower-upload", "file_name": "follower.txt", "kind": "document"}
    follower.attachment_store.save(artifact_id="follower-upload", content="actual uploaded text",
        metadata={"conversation_id": conversation.id, "workspace_root": conversation.workspace_root, "attachment": attachment})
    command = UserCommand("user_message", {"client_command_id": "same-command-id", "conversation_id": conversation.id,
        "content": "use the uploaded file", "attachments": [attachment], "streaming_behavior": behavior,
        "user_message_id": "follower-user", "assistant_message_id": "follower-assistant"})
    # The same client ID in the original window is a different durable input.
    other_command = UserCommand("user_message", {"client_command_id": "same-command-id", "conversation_id": other.id,
        "content": "untouched original-window pending input"})
    assert owner.run_manager.durable_client_commands.persist_client_command(other_command)
    assert follower.run_manager.durable_client_commands.persist_client_command(command)
    await follower.command_dispatcher._run_durable_client_command("same-command-id", follower.connection_generation)
    await drain()
    if behavior == "steer":
        queued = owner.run_manager.turn_input_queue(conversation.id).pop_steer()
        assert queued is not None and queued.target_message_id == "actual-assistant"
        accepted = queued.original_command
        owner.run_manager.acknowledge_turn_input(conversation.id, accepted)
    else:
        accepted = owner.run_manager.queued_user_messages(conversation.id)[0]
    assert accepted.data["_client_queue_key"] == follower.run_manager.durable_client_commands.path.stem
    assert accepted.data["attachments"][0]["artifact_id"] == "follower-upload"
    assert owner.attachment_store.get("follower-upload", conversation_id=conversation.id,
        workspace_root=conversation.workspace_root) == "actual uploaded text"
    assert owner.active_conversation_id == other.id
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert follower.run_manager.run_tasks == {}
    assert owner.run_manager.durable_client_commands.claim_client_command("same-command-id").data["content"] == other_command.data["content"]
    assert not follower.run_manager.durable_client_commands.has_client_command("same-command-id")
    assert any(item.get("client_command_id") == "same-command-id" and item.get("data", {}).get("queued") for item in follower.ws.sent)
    assert not any(item.get("client_command_id") == "same-command-id" for item in owner.ws.sent)


@pytest.mark.asyncio
async def test_a_different_workspace_upload_cannot_be_smuggled_into_the_owner_queue(windows):
    manager, owner, follower, conversation, other, start, drain = windows
    start()
    follower.attachment_store.save(artifact_id="other-upload", content="private other workspace",
        metadata={"conversation_id": other.id, "workspace_root": other.workspace_root})
    await follower.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
        "client_command_id": "invalid-upload", "conversation_id": conversation.id, "content": "wrong owner",
        "streaming_behavior": "steer", "attachments": [{"artifact_id": "other-upload", "file_name": "other.txt"}],
    }), follower.connection_generation)
    await drain()
    assert owner.run_manager.turn_input_queue(conversation.id).snapshot() == ()
    assert owner.run_manager.queued_user_messages(conversation.id) == []
    assert any(item.get("client_command_id") == "invalid-upload" and item.get("level") == "error" for item in follower.ws.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "steer"])
async def test_queued_message_controls_address_original_turn_even_when_original_window_reads_another_thread(windows, action):
    manager, owner, follower, conversation, other, start, drain = windows
    task = start()
    await follower.reemit_pending_state(conversation.id)
    owner.active_conversation_id = other.id
    owner.run_manager.enqueue_user_message(conversation.id, UserCommand("user_message", {
        "conversation_id": conversation.id, "content": "real queued input", "client_command_id": "original-queued",
        "user_message_id": "queued-user", "assistant_message_id": "queued-assistant",
        "_client_queue_key": follower.run_manager.durable_client_commands.path.stem,
    }))
    await follower.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue." + action, {
        "client_command_id": "queue-control", "conversation_id": conversation.id, "message_id": "queued-assistant",
    }), follower.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_messages(conversation.id) == []
    steered = owner.run_manager.turn_input_queue(conversation.id).snapshot()
    assert len(steered) == (1 if action == "steer" else 0)
    if steered:
        assert steered[0].target_message_id == "actual-assistant"
    assert owner.active_conversation_id == other.id
    assert owner.run_manager.running_task_for(conversation.id) is task
    assert follower.run_manager.run_tasks == {}


@pytest.mark.asyncio
async def test_public_tool_results_preserve_original_error_evidence_and_sequence(windows):
    manager, owner, follower, conversation, _, start, drain = windows
    start()
    await follower.reemit_pending_state(conversation.id)
    for event_type, extra in [
        ("tool_call", {"args": {"command": "read-only fixture"}}),
        ("command_output_chunk", {"output": "actual output", "stream": "stdout"}),
        ("tool_result", {"status": "failed", "is_error": True, "output": "actual command error"}),
    ]:
        await owner.send_event(AgentEvent(type=event_type, data={"conversation_id": conversation.id,
            "message_id": "actual-assistant", "turn_id": "actual-turn", "tool_call_id": "actual-tool",
            "tool_name": "run_command", **extra}))
    await drain()
    events = [item for item in follower.ws.sent if item["type"] in {"tool_call", "command_output_chunk", "tool_result"}]
    assert len(events) == 3
    assert [item["source_event_seq"] for item in events] == sorted(item["source_event_seq"] for item in events)
    assert events[-1]["status"] == "failed"
    assert events[-1]["output"] == "actual command error"
    assert follower._conversation_streams == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_online", [True, False])
async def test_idle_follower_start_preserves_origin_through_actual_scheduler_and_durable_replay(windows, monkeypatch, origin_online):
    from backend.agent.execution_journal import execution_journal_owner, turn_input_consumption_id
    from backend.agent.runtime import default_runtime

    manager, owner, follower, conversation, other, _, drain = windows
    manager.register_conversation_execution_owner(conversation.id, owner)
    owner.active_conversation_id = other.id
    follower_origin = follower.run_manager.durable_client_commands.path.stem
    if not origin_online:
        manager._sessions.pop(follower.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def controlled_run(content, *, conversation_id, attachments, metadata, cancel_event, run_context):
        observed.append((dict(metadata), run_context))
        assert metadata["_client_queue_key"] == follower_origin
        assert metadata["window_instance_id"] == owner.window_instance_id
        user_id = metadata["user_message_id"]
        command_id = metadata["client_command_id"]
        run_id = metadata["_run_task_id"]
        owner.conversation_repo.commit_turn_admission(conversation_id,
            user_message={"id": user_id, "role": "user", "content": content, "attachments": attachments},
            context_snapshot={"turn_admissions": {user_id: {"client_command_id": command_id,
                "client_queue_key": metadata["_client_queue_key"], "assistant_message_id": metadata["assistant_message_id"],
                "run_id": run_id, "history_start": 0, "history_end": 1}}})
        journal = default_runtime().execution_journal(execution_journal_owner("conversation",
            owner.conversation_repo.store_instance_id(), conversation_id))
        journal.append_once("system", {"lifecycle": "turn_input_consumed", "run_id": run_id},
            event_id=turn_input_consumption_id(user_id, command_id))
        metadata["_turn_admitted"] = True
        await owner.send_event(AgentEvent(type="command.result", data={"conversation_id": conversation_id,
            "command": "user_message", "level": "success", "message": "", "client_command_id": command_id,
            "data": {"admitted": True, "conversation_id": conversation_id, "user_message_id": user_id,
                "assistant_message_id": metadata["assistant_message_id"], "run_id": run_id}}))
        metadata["_turn_admission_future"].set_result(None)
        entered.set()
        await release.wait()
        owner.run_manager.mark_terminal_status(conversation_id, "completed")
        await owner.send_event(AgentEvent(type="done", data={"conversation_id": conversation_id,
            "message_id": metadata["assistant_message_id"], "status": "completed"}))
        owner.run_manager.mark_delivery_complete(conversation_id, run_id)

    monkeypatch.setattr(owner, "_run_agent", controlled_run)
    other_pending = UserCommand("user_message", {"client_command_id": "idle-same-id", "conversation_id": other.id,
        "content": "original-window pending text"})
    owner.run_manager.durable_client_commands.persist_client_command(other_pending)
    command = UserCommand("user_message", {"client_command_id": "idle-same-id", "conversation_id": conversation.id,
        "user_message_id": "idle-follower-user", "assistant_message_id": "idle-follower-answer", "content": "idle follower input"})
    try:
        await follower.command_dispatcher._dispatch_client_command(command, follower.connection_generation)
        await asyncio.wait_for(entered.wait(), 5)
        await drain()
        assert len(observed) == 1
        assert observed[0][1].window_binding_token == owner._window_binding_token
        assert owner.active_conversation_id == other.id
        assert follower.run_manager.run_tasks == {}
        assert not any(item.get("client_command_id") == "idle-same-id" for item in owner.ws.sent)
        assert any(item.get("client_command_id") == "idle-same-id" for item in follower.ws.sent) is origin_online
        saved = owner.conversation_repo.get_conversation(conversation.id)
        assert saved.context_snapshot["turn_admissions"]["idle-follower-user"]["client_queue_key"] == follower_origin
    finally:
        release.set()
        if owner.command_dispatcher.command_tasks:
            await asyncio.gather(*tuple(owner.command_dispatcher.command_tasks))
    manager._sessions[follower.session_id] = follower
    # A lost completion reconciles from the original admission and consumption
    # journal; neither a new source ID nor a second sampling task is created.
    await follower.command_dispatcher._dispatch_client_command(command, follower.connection_generation)
    await drain()
    assert len(observed) == 1
    assert any(item.get("client_command_id") == "idle-same-id" and item.get("data", {}).get("replayed") for item in follower.ws.sent)
    assert owner.run_manager.durable_client_commands.claim_client_command("idle-same-id").data["content"] == other_pending.data["content"]


@pytest.mark.asyncio
async def test_stop_cancels_the_actual_submitting_admission_before_a_delegated_slash_finishes(windows, monkeypatch):
    manager, owner, follower, conversation, other, _, drain = windows
    manager.register_conversation_execution_owner(conversation.id, owner)
    owner.active_conversation_id = other.id
    entered, release = asyncio.Queue(), asyncio.Event()
    monkeypatch.setattr(owner, "_ensure_extension_commands_for_conversation", AsyncMock())
    monkeypatch.setattr(owner.command_registry, "get_extension_slash", lambda *_args, **_kwargs: object())
    async def held_slash(*_args, **_kwargs):
        entered.put_nowait(True)
        await release.wait()
        return False, "/held"
    monkeypatch.setattr(owner.command_registry, "dispatch_slash", held_slash)
    monkeypatch.setattr(owner, "start_agent_run", AsyncMock())
    command = UserCommand("user_message", {"client_command_id": "pending-slash", "conversation_id": conversation.id,
        "content": "/held", "assistant_message_id": "pending-answer", "user_message_id": "pending-user"})
    follower.run_manager.durable_client_commands.persist_client_command(command)
    submitting = asyncio.create_task(follower.command_dispatcher._run_durable_client_command("pending-slash", follower.connection_generation))
    await asyncio.wait_for(entered.get(), 5)
    await follower.command_dispatcher._dispatch_client_command(UserCommand("interrupt", {
        "conversation_id": conversation.id, "message_id": "pending-answer"}), follower.connection_generation)
    await submitting
    await drain()
    owner.start_agent_run.assert_not_awaited()
    cancellation = follower.run_manager.durable_client_commands.cancelled_client_command("pending-slash")
    assert cancellation["reason"] == "user_interrupted"
    assert owner.active_conversation_id == other.id
    replacement = UserCommand("user_message", {"client_command_id": "replacement-slash", "conversation_id": conversation.id,
        "content": "/held", "assistant_message_id": "replacement-answer", "user_message_id": "replacement-user"})
    replacing = asyncio.create_task(follower.command_dispatcher._dispatch_client_command(replacement, follower.connection_generation))
    await asyncio.wait_for(entered.get(), 5)
    await follower.command_dispatcher._dispatch_client_command(UserCommand("interrupt", {
        "conversation_id": conversation.id, "message_id": "pending-answer"}), follower.connection_generation)
    assert not replacing.done()
    release.set()
    await replacing
    owner.start_agent_run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["not-ready", "error", "cancelled", "ready"])
async def test_forwarded_regeneration_releases_execution_dispatcher_transition_on_every_exit(windows, monkeypatch, outcome):
    from backend.agent.conversation_query_guard import conversation_query_guards
    manager, owner, follower, conversation, _, start, drain = windows
    start()
    owner.conversation_repo.append_transcript_message(conversation.id,
        {"id": "retry-user", "role": "user", "content": "original input"})
    guards = conversation_query_guards()
    claim = guards.try_start(conversation.id, owner_id=f"ws:{owner.session_id}:actual-task")
    assert claim is not None
    entered, release = asyncio.Event(), asyncio.Event()
    original_cancel_runs = owner.cancel_agent_runs
    async def cancel_run(**_kwargs):
        entered.set()
        if outcome == "error":
            raise RuntimeError("controlled cleanup error")
        if outcome == "cancelled":
            await release.wait()
        if outcome == "ready":
            guards.end(claim)
            owner.run_manager.run_tasks.pop(conversation.id)
            owner.run_manager.run_task_ids.pop(conversation.id)
    monkeypatch.setattr(owner, "cancel_agent_runs", cancel_run)
    monkeypatch.setattr(owner, "_prepare_retry_from_message", lambda **_kwargs: {"snapshot": {}})
    monkeypatch.setattr(owner, "start_agent_run", AsyncMock())
    command = UserCommand("user_message", {"client_command_id": "regenerate-follower", "conversation_id": conversation.id,
        "content": "replacement input", "retry_from_message_id": "retry-user", "assistant_message_id": "regenerated-answer"})
    executing = asyncio.create_task(follower.command_dispatcher._dispatch_client_command(command, follower.connection_generation))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if outcome == "cancelled":
            executing.cancel()
            with suppress(asyncio.CancelledError):
                await executing
        else:
            await executing
        assert owner.command_dispatcher._regeneration_transitions == {}
        assert not guards.has_transition(conversation.id)
        guards.end(claim)
        next_claim = guards.try_start(conversation.id, owner_id="next-real-admission")
        assert next_claim is not None
        guards.end(next_claim)
        assert owner.start_agent_run.await_count == (1 if outcome == "ready" else 0)
    finally:
        monkeypatch.setattr(owner, "cancel_agent_runs", original_cancel_runs)
        release.set()
        if not executing.done():
            executing.cancel()
            with suppress(asyncio.CancelledError):
                await executing
        guards.end(claim)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["owner_conversation_id", "ownerConversationId"])
async def test_persisted_owner_alias_is_normalized_before_locks_and_delegated_scope(windows, monkeypatch, alias):
    from backend.ws.command_dispatcher import _command_lifecycle_conversations
    from backend.ws.command_scope import resolve_command_scope
    manager, owner, follower, conversation, other, _, drain = windows
    manager.register_conversation_execution_owner(conversation.id, owner)
    owner.active_conversation_id = follower.active_conversation_id = other.id
    captured = []
    async def record(command_type, data):
        captured.append((resolve_command_scope(owner, data), _command_lifecycle_conversations(follower, UserCommand(command_type, data))))
        return True
    monkeypatch.setattr(owner.command_registry, "dispatch", record)
    await follower.command_dispatcher._dispatch_client_command(UserCommand("context.compact", {
        alias: conversation.id, "conversation_id": other.id, "client_command_id": "aliased-owner",
    }), follower.connection_generation)
    assert captured[0][0].conversation_id == conversation.id
    assert captured[0][0].workspace_root == conversation.workspace_root
    assert captured[0][1] == (conversation.id,)
    assert owner.active_conversation_id == follower.active_conversation_id == other.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_message", "followup_task"])
async def test_follower_subagent_mailbox_uses_original_parent_scope_while_owner_reads_another_thread(windows, monkeypatch, tmp_path, kind):
    from backend.agent.runtime import AgentRuntime
    manager, owner, follower, conversation, other, _, drain = windows
    runtime = AgentRuntime(metrics_file=tmp_path / "mailbox/metrics.jsonl", swarm_store_dir=tmp_path / "mailbox/swarm", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    manager.register_conversation_execution_owner(conversation.id, owner)
    owner.active_conversation_id = other.id
    parent = runtime.start_run(conversation_id=conversation.id, session_id=owner.session_id)
    child = runtime.start_subagent(subagent_id="follower-child", parent_run_id=parent.run_id, agent_type="explore", session_id=owner.session_id)
    try:
        await follower.command_dispatcher._dispatch_client_command(UserCommand(kind, {
            "client_command_id": "child-message", "conversation_id": conversation.id,
            "recipient": child.subagent_id, "message": "actual directed input", "message_id": "actual-mailbox-message",
        }), follower.connection_generation)
        await drain()
        messages = runtime.list_swarm_messages(participant_id=child.subagent_id, conversation_id=conversation.id)
        assert len(messages) == 1
        assert messages[0].delivery_mode == ("queue_only" if kind == "send_message" else "trigger_turn")
        assert messages[0].content == "actual directed input"
        assert owner.active_conversation_id == other.id
        assert runtime.get_subagent(child.subagent_id).session_id == owner.session_id
        assert not any(item.get("client_command_id") == "child-message" for item in owner.ws.sent)
    finally:
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_terminal_child_resume_receives_target_permission_and_registry_without_changing_the_reader(windows, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from backend.agent.runtime import AgentRuntime
    manager, owner, follower, conversation, other, _, drain = windows
    runtime = AgentRuntime(metrics_file=tmp_path / "resume/metrics.jsonl", swarm_store_dir=tmp_path / "resume/swarm", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    manager.register_conversation_execution_owner(conversation.id, owner)
    owner.active_conversation_id = other.id
    original_permission = owner.permission_context = PermissionContext(mode="bypass")
    parent = runtime.start_run(conversation_id=conversation.id, session_id=owner.session_id)
    child = runtime.start_subagent(subagent_id="follower-resume-child", parent_run_id=parent.run_id, agent_type="explore", session_id=owner.session_id)
    runtime.complete_subagent(child.subagent_id, agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
    resuming = AsyncMock()
    registry = SimpleNamespace(get_tool=lambda name: SimpleNamespace(resume_background_subtask=resuming) if name == "task" else None)
    def registry_for(target):
        assert target == conversation.id
        return registry
    monkeypatch.setattr(owner, "_conversation_tool_registry", registry_for)
    try:
        await follower.command_dispatcher._dispatch_client_command(UserCommand("followup_task", {
            "client_command_id": "child-resume", "conversation_id": conversation.id,
            "recipient": child.subagent_id, "message": "resume from original parent",
        }), follower.connection_generation)
        context = resuming.await_args.kwargs["context"]
        assert context.session_id == owner.session_id
        assert context.conversation_id == conversation.id
        assert context.workspace_root.as_posix() == Path(conversation.workspace_root).as_posix()
        assert context.permission.mode == conversation.permission_mode
        assert context.permission is not original_permission
        assert context.tool_registry is registry
        assert owner.active_conversation_id == other.id
        assert owner.permission_context is original_permission
    finally:
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_retiring_original_producer_can_deliver_terminal_but_not_after_a_new_execution_replaces_it(windows):
    manager, owner, follower, conversation, _, start, drain = windows
    original = start()
    await follower.reemit_pending_state(conversation.id)
    manager._sessions.pop(owner.session_id)
    await owner.send_event(AgentEvent(type="done", data={"conversation_id": conversation.id,
        "message_id": "actual-assistant", "turn_id": "actual-turn", "status": "cancelled"}))
    await drain()
    assert any(item["type"] == "done" and item["message_id"] == "actual-assistant" for item in follower.ws.sent)
    original.cancel()
    await asyncio.gather(original, return_exceptions=True)
    owner._cleanup_agent_run(conversation_id=conversation.id, task=original,
        task_id="actual-task", cancel_event=owner.run_manager.cancel_events[conversation.id])
    assert await manager.unload_conversation_execution(conversation.id, reason="thread_removed_for_replacement")
    replacement = asyncio.create_task(asyncio.Event().wait())
    try:
        follower._register_agent_run(conversation_id=conversation.id, task=replacement,
            task_id="replacement-task", cancel_event=asyncio.Event())
        follower._conversation_streams[conversation.id] = create_stream_state(conversation.id, "replacement-answer", "replacement-turn")
        before = len(follower.ws.sent)
        await owner.send_event(AgentEvent(type="done", data={"conversation_id": conversation.id,
            "message_id": "actual-assistant", "turn_id": "actual-turn", "status": "cancelled"}))
        await drain()
        assert not any(item["type"] == "done" for item in follower.ws.sent[before:])
        assert not follower._conversation_streams[conversation.id]["terminal_fenced"]
        assert follower.runtime_snapshot()["active_task_id"] == "replacement-task"
    finally:
        replacement.cancel()
        with suppress(asyncio.CancelledError):
            await replacement
