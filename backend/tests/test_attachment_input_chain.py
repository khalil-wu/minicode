from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.utils import normalize_attachment_payloads


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    None, "reference", ["reference"], [{}], [{"artifact_id": "uploaded"}],
    [{"artifact_id": "uploaded", "file_name": "source.txt", "size_bytes": "10"}],
    [{"artifact_id": "uploaded", "file_name": "source.txt", "source_char_count": -1}],
])
async def test_malformed_attachments_fail_input_without_admitting_text_only_turn(tmp_path, invalid):
    class Socket:
        client_state = WebSocketState.CONNECTED

        def __init__(self):
            self.events = []

        async def send_json(self, payload):
            self.events.append(payload)

    socket = Socket()
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    session = WebSocketSession(session_id="attachment-input", websocket=socket,
        llm=SimpleNamespace(model="controlled"), artifact_store=store,
        tool_registry=ToolRegistry(), permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="")), mcp_manager=None)
    repository = ConversationRepository(tmp_path / "conversations")
    session.conversation_repo = repository
    record = repository.create_conversation(workspace_root=str(tmp_path))
    session.active_conversation_id = record.id
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand(type="user_message", data={
            "content": "Use the attached source", "attachments": invalid,
            "conversation_id": record.id, "assistant_message_id": "rejected-message",
            "user_message_id": "rejected-user", "client_command_id": "rejected-command",
        }))
        failures = [event for event in socket.events if event["type"] == "error"]
        terminals = [event for event in socket.events if event["type"] == "done"]
        assert failures[-1]["error_code"] == "invalid_attachments"
        assert len(terminals) == 1
        assert terminals[0]["status"] == "failed"
        assert terminals[0]["message_id"] == "rejected-message"
        receipt = next(event for event in socket.events if event["type"] == "command.result")
        assert receipt["client_command_id"] == "rejected-command"
        assert receipt["data"]["admitted"] is False
        assert receipt["data"]["user_message_id"] == "rejected-user"
        assert receipt["data"]["reason"] == "attachments_rejected"
        assert repository.get_conversation(record.id).transcript == []
        assert not session._has_active_run()
    finally:
        await session.session_lifecycle.shutdown(reason="attachment_input_fixture")


def test_valid_duplicate_attachment_references_keep_one_uploaded_identity():
    reference = {"artifact_id": "uploaded", "file_name": "source.txt", "size_bytes": 10}
    assert len(normalize_attachment_payloads([reference, reference])) == 1


@pytest.mark.asyncio
async def test_missing_workspace_rejects_the_correlated_input(tmp_path):
    class Socket:
        client_state = WebSocketState.CONNECTED

        def __init__(self):
            self.events = []

        async def send_json(self, payload):
            self.events.append(payload)

    socket = Socket()
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    session = WebSocketSession(session_id="workspace-input", websocket=socket,
        llm=SimpleNamespace(model="controlled"), artifact_store=store,
        tool_registry=ToolRegistry(), permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="")), mcp_manager=None)
    repository = ConversationRepository(tmp_path / "conversations")
    session.conversation_repo = repository
    record = repository.create_conversation(workspace_root=str(tmp_path))
    session.active_conversation_id = record.id
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand(type="user_message", data={
            "content": "Keep this draft", "conversation_id": record.id,
            "workspace_root": str(tmp_path / "missing"), "client_command_id": "workspace-command",
            "user_message_id": "workspace-user", "assistant_message_id": "workspace-assistant",
        }))
        receipt = next(event for event in socket.events if event["type"] == "command.result")
        assert receipt["client_command_id"] == "workspace-command"
        assert receipt["data"]["admitted"] is False
        assert receipt["data"]["user_message_id"] == "workspace-user"
        assert receipt["data"]["reason"] == "workspace_activation_failed"
        assert repository.get_conversation(record.id).transcript == []
    finally:
        await session.session_lifecycle.shutdown(reason="workspace_input_fixture")
