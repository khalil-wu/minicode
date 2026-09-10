from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class Provider(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        if False:
            yield

    async def simple_chat(self, messages):
        return ""


@pytest.fixture
def session(tmp_path, monkeypatch):
    owner = WebSocketSession(
        session_id="admission-session", websocket=SimpleNamespace(), llm=Provider(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")), tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), config=AppConfig(llm=LLMSettings(api_key="fixture")),
    )
    owner.conversation_repo = ConversationRepository(tmp_path / "conversations")
    conversation = owner.conversation_repo.create_conversation(title="Owner", workspace_root=str(tmp_path), permission_mode="confirm")
    owner.active_conversation_id = conversation.id
    owner.set_permission_context_mode("confirm", source="fixture")
    monkeypatch.setattr(owner, "send_event", AsyncMock())
    monkeypatch.setattr(owner, "emit_permission_mode_updated", AsyncMock())
    monkeypatch.setattr(owner, "auto_approve_pending_tool_approvals", AsyncMock())
    monkeypatch.setattr(owner, "_reject_pending_approvals", AsyncMock())
    monkeypatch.setattr(owner, "start_agent_run", AsyncMock())
    monkeypatch.setattr(owner, "activate_workspace_path", AsyncMock(return_value=True))
    monkeypatch.setattr(owner, "git_branch_for", lambda _path: "fixture")
    monkeypatch.setattr(owner.session_lifecycle, "workspace_root_for_conversation", lambda _conversation: tmp_path)
    monkeypatch.setattr(owner.run_manager, "watch_conversation_notifications", lambda _id: None)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    yield owner
    owner.run_manager.stop_notification_wake_intake()
    owner.run_manager.close_durable_queue()


@pytest.mark.parametrize("behavior", ["follow_up", "steer"])
def test_queued_prompt_settings_do_not_reconfigure_the_current_turn(session, tmp_path, behavior):
    next_workspace = tmp_path / "next-workspace"
    next_workspace.mkdir()
    conversation_id = session.active_conversation_id

    async def scenario():
        running = asyncio.create_task(asyncio.Event().wait())
        session.run_manager.run_tasks[conversation_id] = running
        session.run_manager.turn_input_queue(conversation_id)
        try:
            await session.command_dispatcher._handle_command_inner(UserCommand(type="user_message", data={
                "content": "next request", "conversation_id": conversation_id, "permission_mode": "bypass",
                "workspace_root": str(next_workspace), "streaming_behavior": behavior,
                "assistant_message_id": "next-answer", "user_message_id": "next-user",
            }))
            current = session.conversation_repo.get_conversation(conversation_id)
            assert current.permission_mode == "confirm"
            assert current.workspace_root == str(tmp_path)
            assert session.permission_context.mode == "confirm"
            session.activate_workspace_path.assert_not_awaited()
            session.auto_approve_pending_tool_approvals.assert_not_awaited()
            session.start_agent_run.assert_not_awaited()
            queued = session.run_manager.dequeue_user_message(conversation_id)
            assert queued is not None
            assert queued.data["workspace_root"] == str(next_workspace)
            assert queued.data["permission_mode"] == "bypass"

            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            session.run_manager.run_tasks.pop(conversation_id)
            queued.data["_queued_user_message_dispatch"] = True
            await session.command_dispatcher._handle_command_inner(queued)
            current = session.conversation_repo.get_conversation(conversation_id)
            assert current.permission_mode == "bypass"
            assert current.workspace_root == str(next_workspace)
            session.activate_workspace_path.assert_awaited_once()
            session.start_agent_run.assert_awaited_once()
            session.run_manager.finish_user_message_dispatch(conversation_id, queued, succeeded=True)
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            session.run_manager.run_tasks.clear()

    asyncio.run(scenario())


def test_accepted_same_workspace_steer_applies_its_permission_mode(session, tmp_path):
    conversation_id = session.active_conversation_id

    async def scenario():
        running = asyncio.create_task(asyncio.Event().wait())
        session.run_manager.run_tasks[conversation_id] = running
        turn_inputs = session.run_manager.turn_input_queue(conversation_id)
        try:
            await session.command_dispatcher._handle_command_inner(UserCommand(type="user_message", data={
                "content": "redirect this turn", "conversation_id": conversation_id,
                "permission_mode": "bypass", "workspace_root": str(tmp_path), "streaming_behavior": "steer",
            }))
            assert turn_inputs.pending_count() == 1
            assert session.run_manager.queued_user_messages(conversation_id) == []
            assert session.conversation_repo.get_conversation(conversation_id).permission_mode == "bypass"
            session.activate_workspace_path.assert_not_awaited()
            session.start_agent_run.assert_not_awaited()
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            session.run_manager.run_tasks.clear()

    asyncio.run(scenario())


def test_rejected_message_identity_does_not_apply_prompt_settings(session, tmp_path):
    conversation_id = session.active_conversation_id
    current = session.conversation_repo.get_conversation(conversation_id)
    current.transcript = [{"id": "existing-user", "role": "user", "content": "already admitted"}]
    current.context_snapshot = {"turn_admissions": {"existing-user": {"client_command_id": "original-command"}}}
    session.conversation_repo.save_conversation(current)
    other = tmp_path / "other"
    other.mkdir()

    asyncio.run(session.command_dispatcher._handle_command_inner(UserCommand(type="user_message", data={
        "content": "different content", "conversation_id": conversation_id, "user_message_id": "existing-user",
        "client_command_id": "conflicting-command", "permission_mode": "bypass", "workspace_root": str(other),
    })))

    saved = session.conversation_repo.get_conversation(conversation_id)
    assert saved.permission_mode == "confirm"
    assert saved.workspace_root == str(tmp_path)
    session.activate_workspace_path.assert_not_awaited()
    session.start_agent_run.assert_not_awaited()
    assert session.send_event.await_args.args[0].data["error_code"] == "turn_admission_conflict"
