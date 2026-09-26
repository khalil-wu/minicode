"""Regeneration rewrites the transcript, so it must not run while another
owner (a scheduled task, a REST run, or another window) holds the conversation.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class _Provider(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        if False:
            yield

    async def simple_chat(self, messages):
        return ""


@pytest.fixture
def session(tmp_path, monkeypatch):
    owner = WebSocketSession(
        session_id="regen-session", websocket=SimpleNamespace(), llm=_Provider(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")), tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), config=AppConfig(llm=LLMSettings(api_key="fixture")),
    )
    owner.conversation_repo = ConversationRepository(tmp_path / "conversations")
    conversation = owner.conversation_repo.create_conversation(
        title="Owner", workspace_root=str(tmp_path), permission_mode="confirm",
        transcript=[
            {"id": "u1", "role": "user", "content": "first"},
            {"id": "a1", "role": "assistant", "content": "answer"},
        ],
    )
    owner.active_conversation_id = conversation.id
    owner.set_permission_context_mode("confirm", source="fixture")
    monkeypatch.setattr(owner, "send_event", AsyncMock())
    monkeypatch.setattr(owner, "start_agent_run", AsyncMock())
    monkeypatch.setattr(owner.session_lifecycle, "workspace_root_for_conversation", lambda _c: tmp_path)
    monkeypatch.setattr(owner.run_manager, "watch_conversation_notifications", lambda _id: None)
    yield owner, conversation.id
    owner.run_manager.stop_notification_wake_intake()
    owner.run_manager.close_durable_queue()


def _regenerate(owner, conversation_id):
    return owner.command_dispatcher._handle_command_inner(UserCommand(
        type="user_message",
        data={
            "content": "redo", "conversation_id": conversation_id,
            "retry_from_message_id": "u1",
            "assistant_message_id": "a2", "user_message_id": "u2",
        },
    ))


def test_regenerate_is_blocked_by_a_foreign_query_claim(session, monkeypatch):
    owner, conversation_id = session
    prepare = Mock(return_value={"user_message": {"id": "u1"}})
    monkeypatch.setattr(owner, "_prepare_retry_from_message", prepare)

    claim = conversation_query_guards().try_start(conversation_id, owner_id="scheduler:test")
    assert claim is not None
    try:
        asyncio.run(_regenerate(owner, conversation_id))
    finally:
        assert conversation_query_guards().end(claim) is True

    prepare.assert_not_called()
    owner.start_agent_run.assert_not_awaited()
    transcript = owner.conversation_repo.get_conversation(conversation_id).transcript
    assert [message["id"] for message in transcript] == ["u1", "a1"]
    errors = [
        call.args[0]
        for call in owner.send_event.await_args_list
        if getattr(call.args[0], "type", "") == "error"
    ]
    assert any(event.data.get("error_type") == "conversation_busy" for event in errors)


def test_regenerate_rewinds_when_no_other_owner_holds_the_conversation(session, monkeypatch):
    owner, conversation_id = session
    prepare = Mock(return_value={"user_message": {"id": "u1"}})
    monkeypatch.setattr(owner, "_prepare_retry_from_message", prepare)

    asyncio.run(_regenerate(owner, conversation_id))

    prepare.assert_called_once()
