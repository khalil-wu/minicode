from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.ws.handlers.workspace import handle_workspace_recent_remove
from backend.conversations.repository import ConversationRepository
from backend.workspace.recent_projects import RecentProjectStore


@pytest.mark.asyncio
async def test_recent_workspace_removal_receipt_keeps_the_existing_owner(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", tmp_path / "recent.json")
    root = tmp_path / "owned"
    root.mkdir()
    RecentProjectStore().add(str(root), "Owned")
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(workspace_root=str(root))
    session = SimpleNamespace(active_conversation_id=record.id, conversation_repo=repo, ws_manager=None, is_connected=True,
        send_conversation_list=AsyncMock(), emit_command_result=AsyncMock(), send_payload=AsyncMock())
    create = AsyncMock()
    monkeypatch.setattr("backend.ws.handlers.conversation.handle_conversation_create", create)
    await handle_workspace_recent_remove(session, {"path": str(root), "preserve_history": True})
    assert session.emit_command_result.call_args.kwargs["data"] == {
        "path": str(root), "removed": True,
    }
    assert session.active_conversation_id == record.id
    create.assert_not_awaited()
    assert repo.get_conversation(record.id).execution_cwd == record.execution_cwd
    assert repo.get_conversation(record.id).project_id == ""
