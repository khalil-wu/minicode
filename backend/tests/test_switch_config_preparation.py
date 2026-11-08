from __future__ import annotations

import asyncio
import threading

import pytest

from backend.tests.test_conversation_activation_contract import _session
from backend.conversations.repository import ConversationRepository
from backend.ws.handlers.conversation import _publish_conversation_switch


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "navigate", "model"])
async def test_switch_configuration_read_yields_and_does_not_overwrite_newer_selection(tmp_path, monkeypatch, change):
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(title="Target", transcript=[{"role": "user", "content": "Retained"}])
    session = _session(repo, "source")
    session.session_lifecycle.workspace_root_for_conversation = lambda *_: tmp_path
    entered, release = threading.Event(), threading.Event()
    captured = object()
    loop_thread = threading.get_ident()
    def load(*, cwd, resolve_credentials):
        assert threading.get_ident() != loop_thread
        assert cwd == tmp_path and not resolve_credentials
        entered.set()
        assert release.wait(5)
        return captured
    monkeypatch.setattr("backend.config.load_config", load)
    task = asyncio.create_task(_publish_conversation_switch(session, record, record.to_dict(), {"_reemit_pending": False}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert session.active_conversation_id == record.id and not task.done()
        if change == "navigate":
            session.active_conversation_id = "new-reader"
        elif change == "model":
            session.config = object()
    finally:
        release.set()
        await task
    if change == "none":
        session.refresh_llm_selection.assert_called_once_with(config_snapshot=captured)
        session.start_active_conversation_hydration.assert_called_once_with(record.id)
    else:
        session.refresh_llm_selection.assert_not_called()
        if change == "navigate":
            session.send_payload.assert_not_called()
            session.start_active_conversation_hydration.assert_not_called()
