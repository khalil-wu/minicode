from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.ws.handlers.workspace import handle_workspace_recent_remove


@pytest.mark.asyncio
async def test_recent_workspace_removal_receipt_binds_the_resulting_projectless_owner(monkeypatch):
    session = SimpleNamespace(active_conversation_id="old", conversation_repo=object(),
                              emit_command_result=AsyncMock(), send_payload=AsyncMock())
    monkeypatch.setattr("backend.services.workspace_history.workspace_conversation_ids", lambda *_args: ["old"])
    monkeypatch.setattr("backend.services.workspace_service.remove_workspace_recent", lambda _path: (True, {"type": "workspace.recent.list", "items": []}))
    monkeypatch.setattr("backend.ws.handlers.conversation._conversation_has_active_run", lambda *_args: False)
    monkeypatch.setattr("backend.workspace.state.set_active_workspace_root", lambda _root: None)
    async def create(owner, _data):
        owner.active_conversation_id = "projectless-result"
        return True
    monkeypatch.setattr("backend.ws.handlers.conversation.handle_conversation_create", create)
    await handle_workspace_recent_remove(session, {"path": "C:/owned", "preserve_history": True})
    assert session.emit_command_result.call_args.kwargs["data"] == {
        "path": "C:/owned", "removed": True, "closed_active": True,
        "conversation_id": "projectless-result", "workspace_root": "",
    }
