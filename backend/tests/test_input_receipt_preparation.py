from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import UserCommand
from backend.tests.test_user_input_presentation_chain import session_for


def test_related_journal_receipts_are_read_at_one_validated_boundary(tmp_path, monkeypatch):
    journal = ExecutionJournal("batch-receipts", base_dir=tmp_path)
    first = journal.append_lifecycle("fixture_started", {"owner": "first"})
    second = journal.append_lifecycle("fixture_finished", {"owner": "second"})
    validate = Mock(wraps=journal._validated_events_unlocked)
    monkeypatch.setattr(journal, "_validated_events_unlocked", validate)
    result = journal.get_events([first.event_id, "absent", second.event_id])
    assert result == {first.event_id: first, second.event_id: second}
    assert validate.call_count == 1
    assert journal.get_event(first.event_id) is first


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_input_history_and_receipt_read_yields_and_preserves_admitted_owner(tmp_path, monkeypatch, cancel):
    session, record, _ = session_for(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    foreground = threading.get_ident()
    reader = session.conversation_repo.get_execution_context
    def held(identity, **kwargs):
        assert threading.get_ident() != foreground
        entered.set()
        assert release.wait(5)
        return reader(identity, **kwargs)
    monkeypatch.setattr(session.conversation_repo, "get_execution_context", held)
    reads = []
    lookup = ExecutionJournal.get_events
    def receipts(journal, identities):
        assert threading.get_ident() != foreground
        reads.append(list(identities))
        return lookup(journal, identities)
    monkeypatch.setattr(ExecutionJournal, "get_events", receipts)
    start = AsyncMock(return_value="actual-run")
    monkeypatch.setattr(session, "start_agent_run", start)
    command = UserCommand("user_message", {"content": "Use the original owner", "conversation_id": record.id,
        "client_command_id": "owned-input", "user_message_id": "actual-user", "assistant_message_id": "actual-answer",
        "_client_queue_key": session.run_manager.durable_client_commands.path.stem})
    task = asyncio.create_task(session.command_dispatcher._handle_command_inner(command))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert not task.done()
        session.active_conversation_id = "different-reader"
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
            start.assert_not_awaited()
        else:
            await task
            assert start.call_args.kwargs["conversation_id"] == record.id
            assert start.call_args.kwargs["metadata"]["user_message_id"] == "actual-user"
            assert len(reads) == 1 and len(reads[0]) == 3
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="input-receipt-worker")
