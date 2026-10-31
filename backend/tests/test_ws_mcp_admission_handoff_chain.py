"""Real WS scheduler/prefix/durable-command owners with a controlled run gate.

The gate models unfinished MCP preparation without making provider or desktop
calls. Production run capture, QueryGuard, TaskManager, prefix leases, the
durable command journal and conversation repository remain in this chain.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.permissions.checker import PermissionChecker
from backend.tasks.manager import TaskManager
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.lifecycle_scope import bind_command_lifecycle_prefix, handoff_command_lifecycle_prefix
from backend.ws.manager import WebSocketManager


def session_for(tmp_path, monkeypatch, *, manager=None):
    from backend.workspace.trust import trust_created_task_directory

    trust_created_task_directory(tmp_path)
    config = AppConfig(llm=LLMSettings(provider="custom", model="captured-model", api_key=""))
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **kwargs: config)
    registry = ToolRegistry()
    session = WebSocketSession("b5-window", SimpleNamespace(), None,
        ArtifactStore(storage_dir=tmp_path / "artifacts"), registry,
        PermissionChecker(PermissionSettings(), tmp_path), config, ws_manager=manager,
        mcp_manager=None)
    session.conversation_repo = ConversationRepository(tmp_path / "conversations")
    conversation = session.conversation_repo.create_conversation(workspace_root=str(tmp_path))
    session.active_conversation_id = conversation.id
    session.provider, session.selected_model = "custom", "captured-model"
    session._model_override_active = session._provider_override_active = True
    session.task_manager = TaskManager()
    monkeypatch.setattr(session, "_conversation_tool_registry", lambda *args, **kwargs: registry)
    monkeypatch.setattr(session, "_mcp_manager_for_workspace", lambda root: None)
    monkeypatch.setattr(session.session_lifecycle, "schedule_task_runtime_update", lambda: None)
    events = []
    async def send_event(event):
        events.append(event.to_ws_message())
    async def send_payload(payload, **kwargs):
        events.append(payload)
    monkeypatch.setattr(session, "send_event", send_event)
    monkeypatch.setattr(session, "send_payload", send_payload)
    if manager is not None:
        manager._sessions[session.session_id] = session
        # This suite owns scheduler/prefix admission, not idle unload watches.
        monkeypatch.setattr(manager, "load_conversation_execution", lambda *args: None)
        monkeypatch.setattr(manager, "register_conversation_execution_owner", lambda *args: None)
        monkeypatch.setattr(manager, "track_execution_cleanup", lambda *args: None)
        monkeypatch.setattr(manager, "note_execution_activity", lambda *args: None)
    return session, conversation, config, events


async def close_session(session):
    tasks = tuple(session.run_manager.run_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(*tuple(session.execution_cleanup_tasks), return_exceptions=True)
    await session.run_manager.shutdown_notification_wakes()
    session.run_manager.close_durable_queue()
    await session.artifact_store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred", [True, False])
async def test_pending_admission_releases_window_for_create_switch_but_preserves_durable_suffix(tmp_path, monkeypatch, preferred):
    manager = WebSocketManager()
    session, conversation, config, _events = session_for(tmp_path, monkeypatch, manager=manager)
    entered, admit, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []

    async def run(content, *, conversation_id, metadata, run_context, **kwargs):
        requests.append(run_context.start_request)
        entered.set()
        await admit.wait()
        session.conversation_repo.commit_turn_admission(conversation_id,
            user_message={"id": metadata["user_message_id"], "role": "user", "content": content, "timestamp": 1},
            context_snapshot={"turn_admissions": {metadata["user_message_id"]: {
                "client_command_id": metadata["client_command_id"], "run_id": "controlled-admission"}}})
        metadata["_turn_admitted"] = True
        metadata["_turn_admission_future"].set_result(None)
        try:
            await finish.wait()
        finally:
            session.run_manager.mark_terminal_status(conversation_id, "cancelled")
            session.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])

    monkeypatch.setattr(session, "_run_agent", run)
    created = []
    async def handle(command):
        if command.type == "user_message":
            await session.start_agent_run(command.data["content"], conversation_id=command.data["conversation_id"],
                metadata=command.data)
        elif command.type == "conversation.create":
            options = {"workspace_root": str(tmp_path)}
            if command.data.get("preferred_conversation_id"):
                options["conversation_id"] = command.data["preferred_conversation_id"]
            record = session.conversation_repo.create_conversation(**options)
            created.append(record.id)
            session.active_conversation_id = record.id
        elif command.type == "conversation.switch":
            session.active_conversation_id = command.data["conversation_id"]

    dispatcher = session.command_dispatcher
    monkeypatch.setattr(dispatcher, "_handle_command_inner", handle)
    command = UserCommand(type="user_message", data={"conversation_id": conversation.id,
        "content": "original owner", "client_command_id": "cmd_b5_pending"})
    queue = session.run_manager.durable_client_commands
    assert queue.persist_client_command(command)
    sending = asyncio.create_task(dispatcher._run_durable_client_command("cmd_b5_pending", session.connection_generation))
    writer = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not sending.done() and queue.has_client_command("cmd_b5_pending")
        assert not dispatcher._client_command_seen(command)
        assert session.conversation_repo.get_conversation(conversation.id).transcript == []
        data = {"preferred_conversation_id": "conv_b5_created"} if preferred else {}
        await asyncio.wait_for(dispatcher._handle_command(UserCommand(type="conversation.create", data=data)), 1)
        assert created and created[0] != conversation.id
        await asyncio.wait_for(dispatcher._handle_command(UserCommand(type="conversation.switch", data={
            "conversation_id": conversation.id})), 1)
        assert session.active_conversation_id == conversation.id
        # Window changes after the prefix cannot re-interpret the captured run.
        session.provider, session.selected_model = "other", "other-model"
        config.permissions.auto_allow.append("later-change")
        assert requests[0].selected_provider == "custom" and requests[0].selected_model == "captured-model"
        assert "later-change" not in requests[0].config.permissions.auto_allow
        async def maintenance():
            async with manager.conversation_maintenance_scope(exclusive=True):
                return "exclusive-after-admission"
        writer = asyncio.create_task(maintenance())
        await asyncio.sleep(0)
        assert not writer.done(), "The durable suffix must retain maintenance participation"
        admit.set()
        await asyncio.wait_for(sending, 1)
        assert await asyncio.wait_for(writer, 1) == "exclusive-after-admission"
        assert dispatcher._client_command_seen(command) and not queue.has_client_command("cmd_b5_pending")
        assert [item["content"] for item in session.conversation_repo.get_conversation(conversation.id).transcript] == ["original owner"]
    finally:
        admit.set()
        finish.set()
        await asyncio.gather(sending, return_exceptions=True)
        if writer is not None:
            await asyncio.gather(writer, return_exceptions=True)
        await close_session(session)
        manager._sessions.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{"_parent_notification_only": True}, {"_goal_continuation": True}])
async def test_internal_runs_return_real_registered_task_id_under_existing_window_lock(tmp_path, monkeypatch, metadata):
    session, conversation, _, _ = session_for(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(*args, **kwargs):
        entered.set()
        await release.wait()
    monkeypatch.setattr(session, "_run_agent", run)
    try:
        async with session.conversation_lifecycle_lock():
            task_id = await asyncio.wait_for(session.start_agent_run("internal", conversation_id=conversation.id,
                metadata={**metadata, "code_review": {"scope": "working-tree"}}), 1)
        assert isinstance(task_id, str) and session.run_manager.run_task_ids[conversation.id] == task_id
        owner = conversation_query_guards().active_owner(conversation.id)
        assert owner is not None and owner.turn_kind == "review"
        assert owner.turn_owner_task is session.run_manager.run_tasks[conversation.id]
        assert not owner.turn_started_receipt.done()
    finally:
        release.set()
        await close_session(session)
    assert conversation_query_guards().active_claim(conversation.id) is None


@pytest.mark.asyncio
async def test_pre_first_step_cancel_releases_reserved_query_claim_and_registered_run(tmp_path, monkeypatch):
    session, conversation, _, events = session_for(tmp_path, monkeypatch)
    entered = []
    async def run(*args, **kwargs):
        entered.append(True)
    monkeypatch.setattr(session, "_run_agent", run)
    original_create = session.task_manager.create
    def cancelled_create(kind, awaitable, **kwargs):
        task = original_create(kind, awaitable, **kwargs)
        task.task.cancel()
        return task
    monkeypatch.setattr(session.task_manager, "create", cancelled_create)
    try:
        with pytest.raises(RuntimeError, match="cancelled before the user turn was durably admitted"):
            await session.start_agent_run("cancel before body", conversation_id=conversation.id)
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        assert not entered and conversation_query_guards().active_claim(conversation.id) is None
        assert session.running_agent_task_for(conversation.id) is None
        assert session.conversation_repo.get_conversation(conversation.id).transcript == []
        done = [event for event in events if event.get("type") == "done"]
        assert len(done) == 1 and done[0]["status"] == "cancelled"
    finally:
        await close_session(session)


@pytest.mark.asyncio
async def test_child_task_cannot_release_an_inherited_parent_command_prefix():
    lock = asyncio.Lock()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(lock)
        with bind_command_lifecycle_prefix(stack):
            await asyncio.create_task(handoff_command_lifecycle_prefix())
            assert lock.locked()
            await handoff_command_lifecycle_prefix()
            assert not lock.locked()
