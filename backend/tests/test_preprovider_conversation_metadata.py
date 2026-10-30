"""Metadata reads and no-op UI preparation preserve durable input admission."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.execution_journal import execution_journal_owner
from backend.agent.message import UserCommand
from backend.agent.runtime import default_runtime
from backend.conversations.repository import ConversationRepository
from backend.services.goal_runtime import GoalTurnAccounting, new_goal
from backend.tests.test_user_input_presentation_chain import session_for


def packet(conversation_id):
    return {"conversation_id": conversation_id, "content": "Continue the accepted work",
        "client_command_id": "current-owned-command", "user_message_id": "current-owned-user",
        "assistant_message_id": "current-owned-assistant"}


@pytest.mark.asyncio
async def test_dispatch_checks_owner_metadata_then_reads_full_admission_evidence_once(tmp_path, monkeypatch):
    session, conversation, _adapter = session_for(tmp_path, monkeypatch)
    full = Mock(wraps=session.conversation_repo.get_conversation)
    summary = Mock(wraps=session.conversation_repo.get_conversation_summary)
    start = AsyncMock()
    monkeypatch.setattr(session.conversation_repo, "get_conversation", full)
    monkeypatch.setattr(session.conversation_repo, "get_conversation_summary", summary)
    monkeypatch.setattr(session, "start_agent_run", start)
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", packet(conversation.id)))
        full.assert_called_once_with(conversation.id)
        assert summary.call_args_list[0].args == (conversation.id,)
        start.assert_awaited_once()
        assert start.await_args.kwargs["conversation_id"] == conversation.id
        assert start.await_args.kwargs["metadata"]["user_message_id"] == "current-owned-user"
    finally:
        await session.session_lifecycle.shutdown(reason="metadata_admission_test")


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty_ui", [False, True])
async def test_real_query_only_skips_unchanged_ui_write_and_keeps_user_journal_and_receipt(tmp_path, monkeypatch, dirty_ui):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    repo = session.conversation_repo
    children = [{"id": "retained-child", "role": "explore", "status": "done", "resultContent": "Existing child evidence"}]
    snapshot = {"history": [{"role": "user", "content": "Previous accepted request"}],
        "ui_agent_state": {"plan": None, "todos": [], "subagents": deepcopy(children), "agentProgress": []}}
    if dirty_ui:
        snapshot["ui_agent_state"]["todos"] = [{"id": "old", "content": "Completed prior work", "status": "completed"}]
    repo.commit_turn_admission(conversation.id,
        user_message={"id": "previous-user", "role": "user", "content": "Previous accepted request"},
        context_snapshot=snapshot)
    repo.update_compaction(conversation.id, "compacted", "Retain the earlier compaction evidence")
    reset_saves = []
    save = repo.save_context_snapshot

    def track_save(owner, value):
        if not adapter.requests:
            assert session._conversation_projection_lock(owner).locked()
            reset_saves.append(deepcopy(value))
        return save(owner, value)

    admission = Mock(wraps=repo.commit_turn_admission)
    monkeypatch.setattr(repo, "save_context_snapshot", track_save)
    monkeypatch.setattr(repo, "commit_turn_admission", admission)
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", packet(conversation.id)))
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        await session.event_outbox.drain_delivery()
        await session.event_outbox.drain_persistence()
        assert len(adapter.requests) == 1
        assert len(reset_saves) == int(dirty_ui)
        admission.assert_called_once()
        accepted_snapshot = admission.call_args.kwargs["context_snapshot"]
        assert accepted_snapshot["ui_agent_state"] == {"plan": None, "todos": [], "subagents": children, "agentProgress": []}
        saved = ConversationRepository(repo._base_dir).get_conversation(conversation.id)
        assert [item["id"] for item in saved.transcript] == ["previous-user", "current-owned-user", "current-owned-assistant"]
        assert saved.transcript[0]["content"] == "Previous accepted request"
        assert saved.context_snapshot["ui_agent_state"]["subagents"] == children
        assert saved.compaction_summary == "Retain the earlier compaction evidence"
        assert "Earlier: Retain the earlier compaction evidence" in saved.summary
        receipt = next(event for event in session.ws.messages if event.get("type") == "command.result"
            and event.get("command") == "user_message" and event.get("data", {}).get("admitted"))
        assert receipt["data"]["user_message_id"] == "current-owned-user"
        journal = default_runtime().execution_journal(execution_journal_owner("conversation", repo.store_instance_id(), conversation.id))
        records = journal.read_events()
        assert any(event.event_type == "user_prompt" and event.payload.get("user_message_id") == "current-owned-user" for event in records)
        assert any(event.payload.get("lifecycle") == "turn_input_consumed" and event.payload.get("client_command_id") == "current-owned-command" for event in records)
    finally:
        await session.session_lifecycle.shutdown(reason="ui_reset_admission_test")


@pytest.mark.asyncio
async def test_changed_ui_write_failure_still_stops_before_provider_and_admission(tmp_path, monkeypatch):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    repo = session.conversation_repo
    original = {"ui_agent_state": {"plan": {"steps": [{"step": "Existing plan", "status": "completed"}]},
        "todos": [], "subagents": [], "agentProgress": []}}
    repo.save_context_snapshot(conversation.id, original)
    save = Mock(side_effect=OSError("actual reset persistence failure"))
    monkeypatch.setattr(repo, "save_context_snapshot", save)
    try:
        assert await session.command_dispatcher._handle_command(UserCommand("user_message", packet(conversation.id))) is False
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        save.assert_called_once()
        assert adapter.requests == []
        saved = repo.get_conversation(conversation.id)
        assert saved.context_snapshot == original and saved.transcript == []
    finally:
        await session.session_lifecycle.shutdown(reason="ui_reset_failure_test")


def test_goal_start_reads_current_metadata_without_history_and_retains_mutation(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation()
    first_goal = new_goal("First explicit goal")
    repo.update_goal(conversation.id, first_goal)
    full = Mock(side_effect=AssertionError("Goal startup only needs durable metadata"))
    monkeypatch.setattr(repo, "get_conversation", full)
    first = GoalTurnAccounting(repo, conversation.id)
    assert first.goal_id == first_goal["id"]
    assert repo.get_conversation_summary(conversation.id).goal["_continuation_pending"] is False
    second_goal = new_goal("Replacement explicit goal")
    repo.update_goal(conversation.id, second_goal)
    second = GoalTurnAccounting(repo, conversation.id)
    assert second.goal_id == second_goal["id"] and first.goal_id == first_goal["id"]
    paused = {**second_goal, "status": "paused"}
    repo.update_goal(conversation.id, paused)
    before = repo.get_conversation_summary(conversation.id)
    assert GoalTurnAccounting(repo, conversation.id).goal_id == ""
    assert repo.get_conversation_summary(conversation.id).revision == before.revision
    full.assert_not_called()
