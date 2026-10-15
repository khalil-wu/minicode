from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from threading import Event
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.guardian.host import GuardianHostState
from backend.agent.guardian.manual_approval import ManualApprovalReceipt, require_owned_denial
from backend.agent.guardian.action import GuardianAction
from backend.agent.guardian.prompt import GuardianPermissionEvidence, build_guardian_prompt
from backend.conversations.repository import ConversationRepository
from backend.ws.handlers.guardian import COMMAND, handle_approve_guardian_denied_action


def denial(conversation_id="thread"):
    return {"review_id": "review", "conversation_id": conversation_id, "turn_id": "turn", "environment_id": "local",
        "request_digest": "exact-request", "policy_identity": "exact-policy", "action_json": '{"tool":"exec_command","command":["shell","-c","pwd"]}',
        "history_reset_version": 0, "assessment": {"id": "review", "status": "denied", "turn_id": "turn", "target_item_id": "exec", "started_at_ms": 1000,
            "action": {"type": "command", "source": "unified_exec", "command": "shell -c pwd", "cwd": "/original"}}}


def test_manual_approval_frames_exact_source_developer_authorization():
    record = denial()
    receipt = ManualApprovalReceipt.from_denial(record)
    assert json.loads(receipt.approved_action_json) == {"action": record["assessment"]["action"], "outcome": "allowed"}
    assert receipt.developer_body == (
        "The user has manually approved a specific action that was previously `Rejected`.\n\n"
        "Treat this as approval to perform that exact action in the same context in which it was originally requested.\n"
        "Do not assume this also authorizes similar operations with different payloads.\n\n"
        f"Approved action:\n{receipt.approved_action_json}"
    )
    assert receipt.request_digest == "exact-request" and receipt.policy_identity == "exact-policy"
    assert receipt.action_json == record["action_json"]


def test_selected_client_event_cannot_replace_denied_action_or_owner():
    record = denial()
    require_owned_denial(deepcopy(record["assessment"]), record, "thread")
    for event, owner in (({**record["assessment"], "id": "/approve"}, "thread"),
        ({**record["assessment"], "turn_id": "new-turn"}, "thread"),
        ({**record["assessment"], "action": {"type": "command", "command": "another payload"}}, "thread"),
        (record["assessment"], "another-thread")):
        with pytest.raises(ValueError):
            require_owned_denial(event, record, owner)


def case(tmp_path, *, cold=False):
    repository = ConversationRepository(tmp_path / "conversations")
    thread = repository.create_conversation(workspace_root=str(tmp_path))
    builder = ContextBuilder(conversation_id=thread.id, workspace_root=tmp_path)
    record = denial(thread.id)
    builder.guardian_evidence.review_ledger["review"] = record
    repository.save_context_snapshot(thread.id, builder.export_snapshot())
    host = GuardianHostState(thread.id)
    if not cold:
        host.bind(builder)
    lock = asyncio.Lock()
    session = SimpleNamespace(conversation_repo=repository, active_conversation_id="different-active-thread",
        guardian_host_for_conversation=lambda owner: host if owner == thread.id else None,
        _conversation_projection_lock=lambda owner: lock,
        emit_command_result=AsyncMock(), send_event=AsyncMock())
    return session, repository, host, thread, deepcopy(record["assessment"])


@pytest.mark.asyncio
async def test_recorded_approval_commits_owner_snapshot_without_running_task_or_creating_message(tmp_path):
    session, repository, host, thread, event = case(tmp_path)
    revision = host.evidence.user_message_revision
    await handle_approve_guardian_denied_action(session, {"threadId": thread.id, "conversation_id": thread.id, "event": event})
    loaded = repository.get_conversation(thread.id)
    assert loaded.transcript == []
    assert loaded.context_snapshot["history"][-1]["role"] == "developer"
    assert "same context in which it was originally requested" in loaded.context_snapshot["history"][-1]["content"]
    assert loaded.context_snapshot["guardian_evidence"]["review_ledger"]["review"]["manually_approved"] is True
    assert host.evidence.user_message_revision == revision + 1
    assert host.denied_action("review")["manually_approved"] is True
    action = GuardianAction.freeze({"tool": "exec_command", "command": ["shell", "-c", "pwd"], "cwd": "/original"},
        call_id="retry-exec", turn_id="retry-turn", request_digest="retry-digest", policy_identity="exact-policy", environment_id="local")
    prompt = build_guardian_prompt(host.snapshot(), action, GuardianPermissionEvidence("local"), session_id=thread.id)
    assert "developer: The user has manually approved a specific action" in prompt.text
    assert "Do not assume this also authorizes similar operations with different payloads." in prompt.text
    assert action.json_payload in prompt.text  # The next retry still requires a complete Guardian request.
    session.emit_command_result.assert_awaited_once_with(COMMAND, "", data={"conversation_id": thread.id, "review_id": "review", "approval_recorded": True})
    await handle_approve_guardian_denied_action(session, {"threadId": thread.id, "conversation_id": thread.id, "event": event})
    assert host.evidence.user_message_revision == revision + 1
    assert len(repository.get_conversation(thread.id).context_snapshot["history"]) == len(loaded.context_snapshot["history"])


@pytest.mark.asyncio
async def test_cold_inactive_owner_restores_owned_denial_from_checkpoint(tmp_path):
    session, repository, host, thread, event = case(tmp_path, cold=True)
    await handle_approve_guardian_denied_action(session, {"threadId": thread.id, "conversation_id": thread.id, "event": event})
    assert host.builder._conversation_id == thread.id
    assert host.denied_action("review")["manually_approved"] is True
    assert session.active_conversation_id == "different-active-thread"
    assert repository.get_conversation(thread.id).context_snapshot["history"][-1]["role"] == "developer"


@pytest.mark.asyncio
async def test_failed_persistence_creates_no_live_authorization_and_leaves_action_retryable(tmp_path, monkeypatch):
    session, repository, host, thread, event = case(tmp_path)
    revision = host.evidence.user_message_revision
    before = host.builder.export_snapshot()
    def fail_commit(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(repository, "commit_turn_projection", fail_commit)
    await handle_approve_guardian_denied_action(session, {"threadId": thread.id, "conversation_id": thread.id, "event": event})
    assert host.evidence.user_message_revision == revision
    assert host.builder.export_snapshot() == before
    assert "manually_approved" not in host.denied_action("review")
    assert session.send_event.await_args.args[0].data["level"] == "error"
    session.emit_command_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_preserves_actual_durable_and_live_manual_approval(tmp_path, monkeypatch):
    session, repository, host, thread, event = case(tmp_path)
    entered, release = Event(), Event()
    original_commit = repository.commit_turn_projection
    def commit(*args, **kwargs):
        entered.set()
        release.wait()
        return original_commit(*args, **kwargs)
    monkeypatch.setattr(repository, "commit_turn_projection", commit)
    task = asyncio.create_task(handle_approve_guardian_denied_action(session, {"threadId": thread.id, "conversation_id": thread.id, "event": event}))
    await asyncio.to_thread(entered.wait)
    task.cancel()
    release.set()
    await task
    assert host.denied_action("review")["manually_approved"] is True
    assert repository.get_conversation(thread.id).context_snapshot["guardian_evidence"]["review_ledger"]["review"]["manually_approved"] is True
