from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.ws.handlers import conversation


def _session(repo: ConversationRepository, active_id: str) -> SimpleNamespace:
    session = SimpleNamespace(
        active_conversation_id=active_id,
        conversation_repo=repo,
        ws_manager=None,
        switch_workspace_for_conversation=AsyncMock(return_value=True),
        reconcile_persisted_ui_agent_state=AsyncMock(return_value=None),
        load_active_conversation_snapshot=Mock(return_value=False),
        start_active_conversation_hydration=Mock(),
        sync_permission_mode_with_active_conversation=Mock(),
        send_payload=AsyncMock(),
        send_conversation_list=AsyncMock(),
        emit_command_result=AsyncMock(),
        attachment_store=SimpleNamespace(share_for_conversation=Mock()),
        artifact_store=SimpleNamespace(share_for_conversation=Mock()),
        diagnostic_store=SimpleNamespace(share_for_conversation=Mock()),
        runtime_snapshot=lambda **_: {"active_conversation_id": session.active_conversation_id},
        context_builder=SimpleNamespace(clear=Mock()),
        session_lifecycle=SimpleNamespace(clear_workspace_runtime=Mock()),
    )
    session.permission_context_for_conversation = Mock(return_value=SimpleNamespace(mode="bypass"))
    session.refresh_llm_selection = Mock()
    session.session_lifecycle.schedule_runtime_capabilities = Mock()
    session.conversation_runtime = SimpleNamespace(defer_repository_hydration=Mock())
    return session


def test_short_switch_rejection_preserves_previous_conversation(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    previous = repo.create_conversation()
    target = repo.create_conversation()
    session = _session(repo, previous.id)
    session.switch_workspace_for_conversation.return_value = False

    asyncio.run(conversation.handle_conversation_switch(session, {"conversation_id": target.id}))

    assert session.active_conversation_id == previous.id
    session.load_active_conversation_snapshot.assert_not_called()
    session.send_payload.assert_not_awaited()


@pytest.mark.parametrize("workspace_activated", [False, True])
def test_clone_activation_reports_and_projects_actual_result(monkeypatch, tmp_path, workspace_activated):
    repo = ConversationRepository(tmp_path / "conversations")
    source = repo.create_conversation()
    session = _session(repo, source.id)
    session.switch_workspace_for_conversation.return_value = workspace_activated
    monkeypatch.setattr(conversation, "_broadcast_conversation_lists", AsyncMock(return_value=[]))

    asyncio.run(conversation.handle_conversation_clone(session, {
        "conversation_id": source.id, "activate": True,
    }))

    result = session.emit_command_result.await_args
    clone_id = result.kwargs["data"]["conversation_id"]
    assert repo.get_conversation(clone_id) is not None
    assert result.kwargs["data"]["activated"] is workspace_activated
    if workspace_activated:
        assert session.active_conversation_id == clone_id
        assert session.send_payload.await_args.args[0]["type"] == "conversation.switched"
        assert session.send_payload.await_args.args[0]["conversation_id"] == clone_id
        assert result.kwargs["level"] == "success"
    else:
        assert session.active_conversation_id == source.id
        session.load_active_conversation_snapshot.assert_not_called()
        session.send_payload.assert_not_awaited()
        assert result.kwargs["level"] == "warning"


@pytest.mark.parametrize("workspace_activated", [False, True])
def test_preferred_list_switch_requires_activation_and_canonical_event(tmp_path, workspace_activated):
    repo = ConversationRepository(tmp_path / "conversations")
    previous = repo.create_conversation()
    target = repo.create_conversation()
    session = _session(repo, previous.id)
    session.switch_workspace_for_conversation.return_value = workspace_activated

    asyncio.run(conversation.handle_conversation_list(session, {
        "preferred_conversation_id": target.id,
    }))

    session.send_conversation_list.assert_awaited_once()
    if workspace_activated:
        assert session.active_conversation_id == target.id
        assert session.send_payload.await_args.args[0]["type"] == "conversation.switched"
        assert session.send_payload.await_args.args[0]["conversation_id"] == target.id
        assert session.send_payload.await_args.args[0]["context_pending"] is True
        session.conversation_runtime.defer_repository_hydration.assert_called_once()
        session.start_active_conversation_hydration.assert_called_once_with(target.id)
    else:
        assert session.active_conversation_id == previous.id
        session.load_active_conversation_snapshot.assert_not_called()
        session.send_payload.assert_not_awaited()
        session.session_lifecycle.clear_workspace_runtime.assert_not_called()


def test_invalid_preferred_list_keeps_valid_previous_owner(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    previous = repo.create_conversation()
    session = _session(repo, previous.id)

    asyncio.run(conversation.handle_conversation_list(session, {
        "preferred_conversation_id": "conv_missing",
    }))

    assert session.active_conversation_id == previous.id
    session.switch_workspace_for_conversation.assert_not_awaited()
    session.send_payload.assert_not_awaited()
    session.session_lifecycle.clear_workspace_runtime.assert_not_called()


def test_invalid_preferred_list_clears_deleted_previous_owner(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    previous = repo.create_conversation()
    session = _session(repo, previous.id)
    repo.delete_conversation(previous.id)

    asyncio.run(conversation.handle_conversation_list(session, {
        "preferred_conversation_id": "conv_missing",
    }))

    assert session.active_conversation_id is None
    session.context_builder.clear.assert_called_once()
    session.session_lifecycle.clear_workspace_runtime.assert_called_once()


def test_list_fallback_failure_opens_history_without_an_unavailable_workspace(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    previous = repo.create_conversation()
    fallback = repo.create_conversation()
    session = _session(repo, previous.id)
    session.switch_workspace_for_conversation.return_value = False
    repo.delete_conversation(previous.id)

    asyncio.run(conversation.handle_conversation_list(session, {}))

    assert session.active_conversation_id == fallback.id
    session.load_active_conversation_snapshot.assert_not_called()
    session.conversation_runtime.defer_repository_hydration.assert_called_once()
    session.start_active_conversation_hydration.assert_called_once_with(fallback.id)
    session.session_lifecycle.clear_workspace_runtime.assert_called_once()
    assert session.send_payload.await_args.args[0]["conversation_id"] == fallback.id
