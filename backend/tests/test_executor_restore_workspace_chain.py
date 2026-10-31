from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from backend.agent.environment_runtime import resolve_environment_config, thread_environment_defaults
from backend.agent.environment_selection import ThreadEnvironments
from backend.agent.message import AgentEvent, UserCommand
from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.ws.stream_state import create_stream_state


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.parametrize("available", [True, False])
async def test_restore_uses_the_original_source_executor_workspace(editor_server, available):
    server = editor_server
    directory = server.host.workspace / "restore-executor-workspace"
    directory.mkdir()
    path = directory.as_uri()
    conversation = server.session.conversation_repo.create_conversation(
        title="Executor restore", workspace_root=path, execution_cwd=path, permission_mode="bypass",
    )
    permission = server.session.permission_context_for_conversation(conversation, source="executor-restore-test")
    owner = ThreadEnvironments.retarget(server.host.manager,
        thread_environment_defaults(permission, server.session.config.config_layer_stack), resolve_config=resolve_environment_config,
        attachment=server.host.environments.primary(), cwd=path, workspace_roots=(path,))
    runtime = server.manager.execution_contexts.thread_runtime(server.session.conversation_repo.store_instance_id(), conversation.id)
    runtime.thread_environments = owner
    if not available:
        directory.rmdir()

    await server.websocket.send(json.dumps({"type": "session.restore", "last_conversation_id": conversation.id,
                                             "client_command_id": "executor-restore"}))
    events = []
    async with asyncio.timeout(20):
        while True:
            event = json.loads(await server.websocket.recv())
            events.append(event)
            if event.get("type") == "session.restored" or (event.get("type") == "command.result"
                    and event.get("command") == "session.restore" and event.get("level") == "error"):
                break
    restored = next((event for event in events if event.get("type") == "session.restored"), None)
    assert restored is not None, events
    assert restored["active_conversation_id"] == conversation.id
    if available:
        assert not restored["error"]
        assert restored["working_directory"] == path
        assert restored["session"]["workspace_root"] == path
    else:
        assert restored["error"]
        assert server.session.session_lifecycle.workspace_activation_failed(conversation.id)
    await server.websocket.send(json.dumps({"type": "session.sync", "client_command_id": "executor-sync"}))
    async with asyncio.timeout(20):
        while True:
            synced = json.loads(await server.websocket.recv())
            if synced.get("type") == "session.synced":
                break
    assert synced["working_directory"] == (path if available else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.parametrize("explicit_owner", [True, False])
async def test_recovered_input_does_not_run_before_source_workspace_is_validated(editor_server, monkeypatch, explicit_owner):
    server = editor_server
    directory = server.host.workspace / "unavailable-executor-workspace"
    directory.mkdir()
    path = directory.as_uri()
    conversation = server.session.conversation_repo.create_conversation(
        title="Executor cold input", workspace_root=path, execution_cwd=path, permission_mode="bypass",
    )
    permission = server.session.permission_context_for_conversation(conversation, source="executor-cold-test")
    runtime = server.manager.execution_contexts.thread_runtime(server.session.conversation_repo.store_instance_id(), conversation.id)
    runtime.thread_environments = ThreadEnvironments.retarget(server.host.manager,
        thread_environment_defaults(permission, server.session.config.config_layer_stack), resolve_config=resolve_environment_config,
        attachment=server.host.environments.primary(), cwd=path, workspace_roots=(path,))
    directory.rmdir()
    if not explicit_owner:
        server.session.active_conversation_id = conversation.id
    calls = []

    async def run(content, *, conversation_id, metadata, **_kwargs):
        calls.append(content)
        metadata["_turn_admitted"] = True
        metadata["_turn_admission_future"].set_result(None)
        done = AgentEvent.done(status="completed", reason="completed")
        done.data.update(conversation_id=conversation_id, message_id=metadata["assistant_message_id"])
        await server.session.send_event(done)
        server.session.run_manager.mark_terminal_status(conversation_id, "completed")
        server.session.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])

    monkeypatch.setattr(server.session, "_run_agent", run)
    monkeypatch.setattr(server.session, "_conversation_tool_registry", lambda *_args, **_kwargs: server.host.registry)
    queue = server.session.run_manager.durable_client_commands
    assert queue.persist_client_command(UserCommand("user_message", {**({"conversation_id": conversation.id} if explicit_owner else {}),
        "content": "preserve remote input", "client_command_id": "source-cold-input",
        "user_message_id": "source-user", "assistant_message_id": "source-answer"}))
    await server.session.command_dispatcher._replay_pending_client_commands(server.session.connection_generation, replay_results=False)
    tasks = tuple(server.session.command_dispatcher.execution_command_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 20)
    assert not calls
    assert queue.has_client_command("source-cold-input")
    assert server.session.session_lifecycle.workspace_activation_failed(conversation.id)
    directory.mkdir()
    await server.websocket.send(json.dumps({"type": "session.restore", "last_conversation_id": conversation.id,
                                             "client_command_id": "source-cold-restore"}))
    async with asyncio.timeout(20):
        while True:
            event = json.loads(await server.websocket.recv())
            if event.get("type") == "done" and event.get("message_id") == "source-answer":
                break
    assert calls == ["preserve remote input"]
    async with asyncio.timeout(5):
        while queue.has_client_command("source-cold-input"):
            await asyncio.sleep(.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.parametrize("available", [True, False])
async def test_normal_message_uses_its_source_workspace_uri(editor_server, monkeypatch, available):
    server = editor_server
    directory = server.host.workspace / "message-executor-workspace"
    directory.mkdir()
    path = directory.as_uri()
    conversation = server.session.conversation_repo.create_conversation(
        title="Executor message", workspace_root=path, execution_cwd=path, permission_mode="confirm",
    )
    permission = server.session.permission_context_for_conversation(conversation, source="executor-message-test")
    runtime = server.manager.execution_contexts.thread_runtime(server.session.conversation_repo.store_instance_id(), conversation.id)
    runtime.thread_environments = ThreadEnvironments.retarget(server.host.manager,
        thread_environment_defaults(permission, server.session.config.config_layer_stack), resolve_config=resolve_environment_config,
        attachment=server.host.environments.primary(), cwd=path, workspace_roots=(path,))
    if not available:
        directory.rmdir()
    calls = []

    async def run(content, *, conversation_id, metadata, **_kwargs):
        calls.append(content)
        metadata["_turn_admitted"] = True
        metadata["_turn_admission_future"].set_result(None)
        done = AgentEvent.done(status="completed", reason="completed")
        done.data.update(conversation_id=conversation_id, message_id=metadata["assistant_message_id"])
        await server.session.send_event(done)
        server.session.run_manager.mark_terminal_status(conversation_id, "completed")
        server.session.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])

    monkeypatch.setattr(server.session, "_run_agent", run)
    monkeypatch.setattr(server.session, "_conversation_tool_registry", lambda *_args, **_kwargs: server.host.registry)
    await server.websocket.send(json.dumps({"type": "user_message", "conversation_id": conversation.id,
        "workspace_root": path, "content": "ordinary remote input", "permission_mode": "bypass",
        "client_command_id": "source-message", "user_message_id": "source-message-user",
        "assistant_message_id": "source-message-answer"}))
    events = []
    async with asyncio.timeout(20):
        while True:
            event = json.loads(await server.websocket.recv())
            events.append(event)
            if event.get("type") == "done" and event.get("message_id") == "source-message-answer":
                break
    if available:
        assert calls == ["ordinary remote input"], events
    else:
        assert not calls
        assert server.session.session_lifecycle.workspace_activation_failed(conversation.id)
        assert server.session.conversation_repo.get_conversation(conversation.id).permission_mode == "confirm"


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
async def test_missing_source_workspace_keeps_followup_queued_until_reopened(editor_server, monkeypatch):
    server = editor_server
    directory = server.host.workspace / "queued-executor-workspace"
    directory.mkdir()
    path = directory.as_uri()
    conversation = server.session.conversation_repo.create_conversation(
        title="Executor followup", workspace_root=path, execution_cwd=path, permission_mode="bypass",
    )
    permission = server.session.permission_context_for_conversation(conversation, source="executor-queue-test")
    runtime = server.manager.execution_contexts.thread_runtime(server.session.conversation_repo.store_instance_id(), conversation.id)
    runtime.thread_environments = ThreadEnvironments.retarget(server.host.manager,
        thread_environment_defaults(permission, server.session.config.config_layer_stack), resolve_config=resolve_environment_config,
        attachment=server.host.environments.primary(), cwd=path, workspace_roots=(path,))
    directory.rmdir()
    calls, emitted = [], []
    send_event = server.session.send_event

    async def capture(event):
        emitted.append((event.type, dict(event.data)))
        await send_event(event)

    async def run(content, *, conversation_id, metadata, **_kwargs):
        calls.append(content)
        metadata["_turn_admitted"] = True
        metadata["_turn_admission_future"].set_result(None)
        done = AgentEvent.done(status="completed", reason="completed")
        done.data.update(conversation_id=conversation_id, message_id=metadata["assistant_message_id"])
        await server.session.send_event(done)
        server.session.run_manager.mark_terminal_status(conversation_id, "completed")
        server.session.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])

    monkeypatch.setattr(server.session, "send_event", capture)
    monkeypatch.setattr(server.session, "_run_agent", run)
    monkeypatch.setattr(server.session, "_conversation_tool_registry", lambda *_args, **_kwargs: server.host.registry)
    manager = server.session.run_manager
    manager.enqueue_user_message(conversation.id, UserCommand("user_message", {"conversation_id": conversation.id,
        "workspace_root": path, "content": "queued remote input",
        "user_message_id": "queued-remote-user", "assistant_message_id": "queued-remote-answer"}))
    server.session.schedule_next_queued_user_message(conversation.id)
    tasks = tuple(server.session.command_dispatcher.execution_command_tasks)
    assert tasks
    await asyncio.wait_for(asyncio.gather(*tasks), 20)
    assert not calls
    assert [item.data["content"] for item in manager.queued_user_messages(conversation.id)] == ["queued remote input"]
    assert not any(kind == "user_message.queue_updated" and data.get("status") == "dequeued" for kind, data in emitted)
    assert server.session.session_lifecycle.workspace_activation_failed(conversation.id)

    directory.mkdir()
    await server.websocket.send(json.dumps({"type": "session.restore", "last_conversation_id": conversation.id,
                                             "client_command_id": "queued-remote-restore"}))
    async with asyncio.timeout(20):
        while True:
            event = json.loads(await server.websocket.recv())
            if event.get("type") == "done" and event.get("message_id") == "queued-remote-answer":
                break
    assert calls == ["queued remote input"]


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.parametrize("matches", [True, False])
async def test_steer_keeps_the_source_executor_workspace_owner(editor_server, matches):
    server = editor_server
    path = server.host.workspace.as_uri()
    conversation = server.session.conversation_repo.create_conversation(
        title="Executor steer", workspace_root=path, execution_cwd=path, permission_mode="bypass",
    )
    server.session.active_conversation_id = conversation.id
    task = asyncio.create_task(asyncio.Event().wait())
    cancel_event = asyncio.Event()
    server.session._register_agent_run(conversation_id=conversation.id, task=task, task_id="remote-run", cancel_event=cancel_event)
    server.session._conversation_streams[conversation.id] = create_stream_state(conversation.id, "remote-answer", "remote-turn")
    server.session.run_manager.turn_input_queue(conversation.id).begin_turn("remote-turn")
    try:
        await server.websocket.send(json.dumps({"type": "user_message", "conversation_id": conversation.id,
            "workspace_root": path if matches else path + "/different", "content": "redirect the current turn",
            "streaming_behavior": "steer", "expected_turn_id": "remote-turn", "client_command_id": "remote-steer",
            "user_message_id": "steer-user", "assistant_message_id": "steer-answer"}))
        async with asyncio.timeout(20):
            while True:
                event = json.loads(await server.websocket.recv())
                if event.get("type") == "command.result" and event.get("client_command_id") == "remote-steer":
                    break
        if matches:
            assert event["level"] == "success" and event["data"]["turn_mode"] == "steer"
            assert len(server.session.run_manager.turn_input_queue(conversation.id).snapshot()) == 1
        else:
            assert event["level"] == "error" and event["data"]["field"] == "workspace_root"
            assert not server.session.run_manager.turn_input_queue(conversation.id).snapshot()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.session._cleanup_agent_run(conversation_id=conversation.id, task=task, task_id="remote-run", cancel_event=cancel_event)


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
async def test_parent_notification_waits_for_the_source_workspace(editor_server, tmp_path, monkeypatch):
    from backend.agent import parent_notification_outbox

    server = editor_server
    monkeypatch.setattr(parent_notification_outbox, "OUTBOX_ROOT", tmp_path / "notification-outbox")
    directory = server.host.workspace / "notification-executor-workspace"
    directory.mkdir()
    path = directory.as_uri()
    conversation = server.session.conversation_repo.create_conversation(
        title="Executor notification", workspace_root=path, execution_cwd=path, permission_mode="bypass",
    )
    permission = server.session.permission_context_for_conversation(conversation, source="executor-notification-test")
    runtime = server.manager.execution_contexts.thread_runtime(server.session.conversation_repo.store_instance_id(), conversation.id)
    runtime.thread_environments = ThreadEnvironments.retarget(server.host.manager,
        thread_environment_defaults(permission, server.session.config.config_layer_stack), resolve_config=resolve_environment_config,
        attachment=server.host.environments.primary(), cwd=path, workspace_roots=(path,))
    directory.rmdir()
    started = asyncio.Event()
    calls = []

    async def start(content, *, conversation_id, metadata, **_kwargs):
        calls.append((content, conversation_id, metadata))
        started.set()
        return "notification-run"

    monkeypatch.setattr(server.session, "start_agent_run", start)
    manager = server.session.run_manager
    manager.watch_conversation_notifications(conversation.id)
    await asyncio.gather(*manager.notification_tasks_for(conversation.id))
    parent_notification_outbox.enqueue_parent_notification(conversation_id=conversation.id, session_id=server.session.session_id,
        subagent_id="worker", kind="mailbox_wake", payload={"message_id": "followup", "delivery_mode": "trigger_turn"})
    await asyncio.gather(*manager.notification_tasks_for(conversation.id))
    assert not calls
    assert server.session.session_lifecycle.workspace_activation_failed(conversation.id)
    assert parent_notification_outbox.load_parent_outbox(conversation_id=conversation.id).replayable()

    directory.mkdir()
    await server.websocket.send(json.dumps({"type": "session.restore", "last_conversation_id": conversation.id,
                                             "client_command_id": "notification-restore"}))
    await asyncio.wait_for(started.wait(), 20)
    assert len(calls) == 1 and calls[0][1] == conversation.id and calls[0][2]["_parent_notification_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.parametrize("matches", [True, False])
async def test_builtin_slash_command_respects_the_source_workspace_owner(editor_server, monkeypatch, matches):
    server = editor_server
    path = server.host.workspace.as_uri()
    conversation = server.session.conversation_repo.create_conversation(
        title="Executor slash", workspace_root=path, execution_cwd=path, permission_mode="bypass",
    )
    monkeypatch.setattr(server.session, "_ensure_extension_commands_for_conversation", AsyncMock())
    await server.websocket.send(json.dumps({"type": "user_message", "conversation_id": conversation.id,
        "workspace_root": path if matches else path + "/other", "content": "/help", "client_command_id": "source-help",
        "user_message_id": "source-help-user", "assistant_message_id": "source-help-answer"}))
    async with asyncio.timeout(20):
        while True:
            event = json.loads(await server.websocket.recv())
            if (event.get("type") == "command.result" and event.get("command") == "user_message"
                    and event.get("client_command_id") == "source-help"):
                break
    if matches:
        assert event["level"] == "info" and event["data"]["native_completion"] == "completed"
    else:
        assert event["level"] == "error" and event["data"]["admitted"] is False
