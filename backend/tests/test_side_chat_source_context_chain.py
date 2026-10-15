from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, replace

import pytest

from backend.agent.message import UserCommand
from backend.agent.context_window import ContextWindowState
from backend.agent.run_context import RunContext
from backend.conversations.repository import ConversationRepository
from backend.services.conversation_payload_service import parse_conversation_create_request
from backend.tests.test_context_fork_activation_chain import _connected_fork_session
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.tools.registry import ToolRegistry


def _create_side(parent, command_id="side-create", **overrides):
    return UserCommand("conversation.create", {
        "client_command_id": command_id, "conversation_id": "side-source01", "title": "Side chat",
        "conversation_type": "side_chat", "source_conversation_id": parent.id,
        "workspace_root": parent.execution_cwd or parent.workspace_root, "activate": False, **overrides,
    })


def _result(events, command_id):
    return next(event for event in events if event.get("type") == "command.result" and event.get("client_command_id") == command_id)


@pytest.mark.parametrize("title", ["", "  ", "\t\n"])
def test_repository_rejects_blank_name_without_committing_default_or_revision(tmp_path, title):
    repo = ConversationRepository(tmp_path / "conversations")
    source = repo.create_conversation(title="Preserve this name")
    original = source.to_dict()
    with pytest.raises(ValueError, match="must not be empty"):
        repo.rename_conversation(source.id, title)
    assert ConversationRepository(tmp_path / "conversations").get_conversation(source.id).to_dict() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "  ", "\t\n", "  Renamed  ", "  " + "完整标题" * 40 + "  "])
async def test_websocket_rename_returns_real_semantic_result_and_keeps_complete_name(tmp_path, monkeypatch, title):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    original = parent.to_dict()
    try:
        assert await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.rename", {
            "conversation_id": parent.id, "workspace_root": parent.workspace_root,
            "client_command_id": "rename-original", "title": title,
        }), session.connection_generation) is True
        result = _result(connection["websocket"].sent, "rename-original")
        assert result["conversation_id"] == parent.id
        restored = session.conversation_repo.get_conversation(parent.id)
        if title.strip():
            assert result["level"] == "success" and result["data"]["title"] == title.strip()
            assert restored.title == title.strip()
            assert restored.context_snapshot == parent.context_snapshot
        else:
            assert result["level"] == "error" and result["data"]["reason"] == "empty_name"
            assert restored.to_dict() == original
            assert not any(event.get("type") == "conversation.list" for event in connection["websocket"].sent)
    finally:
        await _release_session(session)


@pytest.mark.asyncio
async def test_side_chat_forks_complete_persisted_context_resources_and_window_facts(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch, history_length=30)
    snapshot = copy.deepcopy(parent.context_snapshot)
    long_content = "Actual complete reference material " * 100
    snapshot["history"][0]["content"] = long_content
    snapshot["extension_state"] = {"pending_messages": [{"content": "Parent-only delivery"}], "followups": ["Parent-only follow-up"], "saved_fact": "Keep this fact"}
    snapshot["extension_cursor"] = {"owner": "parent-delivery-owner"}
    snapshot["inactive_context_windows"] = [{"history": [{"role": "user", "content": "Old durable window"}]}]
    snapshot["context_window_state"] = asdict(ContextWindowState(window_number=2, last_response_total_tokens=400))
    session.conversation_repo.save_context_snapshot(parent.id, snapshot)
    session.attachment_store.save(artifact_id="side-upload", content="Full uploaded content",
        metadata={"conversation_id": parent.id, "workspace_root": parent.workspace_root})
    artifact = session.artifact_store.save("Full tool content", "read_file", conversation_id=parent.id, workspace_root=parent.workspace_root)
    session.diagnostic_store.put("message", "message-29", {"usage": {"output_tokens": 30}}, conversation_id=parent.id)
    try:
        assert await session.command_dispatcher._dispatch_client_command(_create_side(parent), session.connection_generation) is True
        result = _result(connection["websocket"].sent, "side-create")
        assert result["level"] == "success" and result["conversation_id"] == "side-source01"
        assert result["data"]["source_conversation_id"] == result["data"]["parent_conversation_id"] == parent.id
        assert result["data"]["activated"] is False
        assert result["data"]["parent_message_index"] == 29 and result["data"]["side_chat_transcript"] == []
        child = session.conversation_repo.get_conversation("side-source01")
        assert child.conversation_type == child.branch_kind == "side_chat"
        assert child.transcript == parent.transcript and len(child.context_snapshot["history"]) == 30
        assert child.context_snapshot["history"][0]["content"] == long_content
        assert child.context_snapshot["inactive_context_windows"] == snapshot["inactive_context_windows"]
        assert child.context_snapshot["context_window_state"] == snapshot["context_window_state"]
        assert child.context_snapshot["extension_state"] == {"saved_fact": "Keep this fact"}
        assert child.context_snapshot["extension_cursor"] == {}
        assert child.model_selection == parent.model_selection and child.git_branch == parent.git_branch
        assert child.execution_cwd == parent.execution_cwd and child.git_isolated is False and child.worktree_path == ""
        for owner in (parent.id, child.id):
            assert session.attachment_store.get("side-upload", conversation_id=owner, workspace_root=parent.workspace_root) == "Full uploaded content"
            assert session.artifact_store.get(artifact, conversation_id=owner, workspace_root=parent.workspace_root) == "Full tool content"
        assert set(session.diagnostic_store.get("message", "message-29").conversation_ids) == {parent.id, child.id}
        assert session.active_conversation_id == parent.id
        assert session.conversation_repo.get_conversation(parent.id).context_snapshot == snapshot
        assert not any(event.get("type") == "conversation.switched" for event in connection["websocket"].sent)
        assert session.run_manager.running_task_for(child.id) is None
    finally:
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("other_window", [False, True])
async def test_side_chat_uses_real_live_source_builder_without_cancelling_or_borrowing_its_run(tmp_path, monkeypatch, other_window):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    source_owner = session
    viewer = None
    if other_window:
        viewer_connection = _connection(tmp_path / "viewer", session_id="side-viewer")
        viewer, _ = await session.ws_manager.connect(**viewer_connection)
        visible = viewer.conversation_repo.create_conversation(title="Unrelated visible task")
        viewer.active_conversation_id = visible.id
        current, socket, visible_id = viewer, viewer_connection["websocket"], visible.id
    else:
        current, socket, visible_id = session, connection["websocket"], parent.id
    live = source_owner.context_builder.fork_from(-1)
    live.append_user("Live parent context not yet in the persisted checkpoint " * 30)
    live.extension_state = {"pending_messages": ["Parent queued item"], "followups": ["Parent follow-up"], "saved_fact": 42}
    live.extension_cursor = {"source": "parent"}
    before = live.export_snapshot()
    release = asyncio.Event()
    cancel = asyncio.Event()
    main_task = asyncio.create_task(release.wait())
    context = RunContext(context_builder=live, cancel_event=cancel)
    pending_input = asyncio.get_running_loop().create_future()
    source_owner.turn_wait_state.pending_user_input["source-question"] = pending_input
    source_owner.turn_wait_state.pending_approval_payloads["source-question"] = {
        "conversation_id": parent.id, "turn_id": "source-turn", "message_id": "source-answer",
        "request": {"subtype": "request_user_input", "questions": [{"id": "source-choice"}]},
    }
    source_owner.run_manager.register(conversation_id=parent.id, task=main_task, task_id="real-live-parent",
        cancel_event=cancel, active_conversation_id=parent.id, run_context=context)
    try:
        assert await current.command_dispatcher._dispatch_client_command(_create_side(parent), current.connection_generation) is True
        result = _result(socket.sent, "side-create")
        assert result["level"] == "success"
        child = current.conversation_repo.get_conversation("side-source01")
        assert child.context_snapshot["history"] == before["history"]
        assert child.context_snapshot["extension_state"] == {"saved_fact": 42}
        assert child.context_snapshot["extension_cursor"] == {}
        assert live.export_snapshot() == before
        assert source_owner.run_manager.context_for(parent.id) is context
        assert current.run_manager.context_for(child.id) is None
        assert source_owner.run_manager.running_task_for(parent.id) is main_task
        assert main_task.done() is False and cancel.is_set() is False
        assert source_owner.turn_wait_state.pending_user_input["source-question"] is pending_input
        assert pending_input.done() is False
        assert source_owner.turn_wait_state.pending_approval_payloads["source-question"]["conversation_id"] == parent.id
        if other_window:
            assert "source-question" not in current.turn_wait_state.pending_user_input
        assert current.active_conversation_id == visible_id
        assert not any(event.get("type") == "conversation.switched" and event.get("conversation_id") == child.id for event in socket.sent)
    finally:
        release.set()
        await main_task
        if viewer is not None:
            await session.ws_manager.shutdown_session(viewer.session_id, reason="test_cleanup")
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "archived", "workspace"])
async def test_invalid_side_chat_source_returns_owned_error_without_creating_target(tmp_path, monkeypatch, failure):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    overrides = {}
    if failure == "missing":
        overrides["source_conversation_id"] = "missing-source"
    elif failure == "archived":
        session.conversation_repo.set_archived(parent.id, True)
    else:
        overrides["workspace_root"] = str(tmp_path)
    try:
        assert await session.command_dispatcher._dispatch_client_command(_create_side(parent, **overrides), session.connection_generation) is True
        result = _result(connection["websocket"].sent, "side-create")
        assert result["level"] == "error" and result["conversation_id"] == "side-source01"
        assert result["data"]["created"] is False
        assert session.conversation_repo.get_conversation("side-source01") is None
        assert session.active_conversation_id == parent.id
    finally:
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("history_length", [1, 2])
async def test_side_chat_retry_preserves_accepted_target_history_and_rejects_rebinding(tmp_path, monkeypatch, history_length):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch, history_length=history_length)
    try:
        command = _create_side(parent)
        await session.command_dispatcher._dispatch_client_command(command, session.connection_generation)
        child = session.conversation_repo.get_conversation("side-source01")
        kept = copy.deepcopy(child.context_snapshot)
        kept["persistent_notes"] = [{"content": "Existing side-chat note"}]
        session.conversation_repo.save_context_snapshot(child.id, kept)
        own_messages = [
            {"id": "side-user", "role": "user", "content": "Real side-chat question"},
            {"id": "side-answer", "role": "assistant", "content": "Real side-chat answer"},
        ]
        for message in own_messages:
            session.conversation_repo.append_transcript_message(child.id, message)
        original = session.conversation_repo.get_conversation(child.id).to_dict()
        await session.command_dispatcher._dispatch_client_command(command, session.connection_generation)
        assert session.conversation_repo.get_conversation(child.id).to_dict() == original
        await session.command_dispatcher._dispatch_client_command(_create_side(parent, command_id="side-reopen"), session.connection_generation)
        reopened = _result(connection["websocket"].sent, "side-reopen")
        assert reopened["data"]["parent_message_index"] == history_length - 1
        assert reopened["data"]["side_chat_transcript"] == own_messages
        assert session.conversation_repo.get_conversation(child.id).to_dict() == original
        another = session.conversation_repo.create_conversation(workspace_root=parent.workspace_root)
        await session.command_dispatcher._dispatch_client_command(_create_side(parent, command_id="side-rebind", source_conversation_id=another.id), session.connection_generation)
        assert _result(connection["websocket"].sent, "side-rebind")["level"] == "error"
        assert session.conversation_repo.get_conversation(child.id).to_dict() == original
        assert session.active_conversation_id == parent.id
    finally:
        await _release_session(session)


@pytest.mark.asyncio
async def test_side_chat_resource_failure_preserves_created_id_and_real_retry_completes_it(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    share = session.artifact_store.share_for_conversation
    failed = True

    def share_once(*args):
        nonlocal failed
        if failed:
            failed = False
            raise OSError("Actual resource synchronization failed")
        return share(*args)

    monkeypatch.setattr(session.artifact_store, "share_for_conversation", share_once)
    try:
        await session.command_dispatcher._dispatch_client_command(_create_side(parent), session.connection_generation)
        error = _result(connection["websocket"].sent, "side-create")
        assert error["level"] == "error" and error["data"]["created"] is True
        assert error["data"]["reason"] == "resource_synchronization_failed"
        accepted = session.conversation_repo.get_conversation("side-source01").to_dict()
        await session.command_dispatcher._dispatch_client_command(_create_side(parent, command_id="side-retry"), session.connection_generation)
        assert _result(connection["websocket"].sent, "side-retry")["level"] == "success"
        assert session.conversation_repo.get_conversation("side-source01").to_dict() == accepted
        assert session.active_conversation_id == parent.id
    finally:
        await _release_session(session)


def test_side_chat_create_parser_carries_real_source_without_frontend_activation():
    request = parse_conversation_create_request({"conversation_type": "side_chat", "source_conversation_id": " source-id ", "activate": True})
    assert request.source_conversation_id == "source-id" and request.activate is False


@pytest.mark.asyncio
async def test_real_side_chat_provider_reply_routes_to_child_with_source_boundary_without_moving_main(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    class SideLLM(LLMAdapter):
        model = "fixture-model"

        async def stream_chat(self, messages, tools=None):
            seen.extend(messages)
            entered.set()
            await release.wait()
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Real isolated side reply.")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "ok"

    adapter = SideLLM()
    main_release = asyncio.Event()
    main_cancel = asyncio.Event()
    main_task = asyncio.create_task(main_release.wait())
    main_context = RunContext(context_builder=session.context_builder, cancel_event=main_cancel)
    pending_input = asyncio.get_running_loop().create_future()
    session.turn_wait_state.pending_user_input["main-question"] = pending_input
    session.turn_wait_state.pending_approval_payloads["main-question"] = {
        "conversation_id": parent.id, "turn_id": "main-turn", "message_id": "main-answer",
        "request": {"subtype": "request_user_input", "questions": [{"id": "main-choice"}]},
    }
    session.run_manager.register(conversation_id=parent.id, task=main_task, task_id="main-blocked-run",
        cancel_event=main_cancel, active_conversation_id=parent.id, run_context=main_context)
    try:
        session.config = replace(session.config, llm=replace(session.config.llm, api_key="test-key", provider="openai", model="fixture-model"))
        monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "fixture-model")
        monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **_kwargs: session.config)
        monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *_args, **_kwargs: adapter)
        monkeypatch.setattr(session, "_build_conversation_tool_registry", lambda *_args, **_kwargs: ToolRegistry())
        await session.command_dispatcher._dispatch_client_command(_create_side(parent), session.connection_generation)
        command = UserCommand("user_message", {
            "conversation_id": "side-source01", "workspace_root": parent.workspace_root,
            "client_command_id": "side-question", "user_message_id": "side-question-user", "assistant_message_id": "side-question-answer",
            "content": "Answer this independent side question.",
        })
        send = asyncio.create_task(session.command_dispatcher._dispatch_client_command(command, session.connection_generation))
        await asyncio.wait_for(entered.wait(), 10)
        assert session.active_conversation_id == parent.id
        assert session.run_manager.running_task_for(parent.id) is main_task
        assert main_task.done() is False and main_cancel.is_set() is False
        assert session.turn_wait_state.pending_user_input["main-question"] is pending_input and pending_input.done() is False
        assert session.turn_wait_state.pending_approval_payloads["main-question"]["conversation_id"] == parent.id
        release.set()
        assert await send is True
        running = session.run_manager.running_task_for("side-source01")
        if running is not None:
            await running
        await session.event_outbox.drain_delivery()
        replies = [event for event in connection["websocket"].sent if event.get("type") == "item.completed"
                   and event.get("item", {}).get("type") == "agent_message"
                   and event.get("item", {}).get("source") == "model_final"]
        assert replies and all(event["conversation_id"] == "side-source01" for event in replies)
        assert any("Real isolated side reply." in str(event) for event in replies)
        assert session.active_conversation_id == parent.id
        assert session.run_manager.running_task_for(parent.id) is main_task
        assert main_task.done() is False and main_cancel.is_set() is False
        assert session.turn_wait_state.pending_user_input["main-question"] is pending_input and pending_input.done() is False
        boundary = [message for message in seen if "You are in a side conversation, not the main thread." in str(message.content)]
        assert len(boundary) == 1 and boundary[0].role == "developer"
        assert any("Recorded user 0" in str(message.content) for message in seen)
        assert any("Answer this independent side question." in str(message.content) for message in seen)
        assert not any(event.get("type") == "conversation.switched" for event in connection["websocket"].sent)
    finally:
        release.set()
        main_release.set()
        await main_task
        await _release_session(session)
