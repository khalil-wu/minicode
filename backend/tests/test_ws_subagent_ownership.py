from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.runtime import AgentRuntime
from backend.permissions.context import PermissionContext
from backend.services.workspace_service import resolve_requested_workspace
from backend.ws.handlers.misc import (
    handle_send_message,
    handle_subagent_cancel,
    handle_subagent_status,
    handle_subagent_transcript,
)


class Session:
    def __init__(self, workspace: Path, *, conversation_id="conversation-1", session_id="session-1"):
        self.session_id = session_id
        self.active_conversation_id = conversation_id
        self.permission_context = PermissionContext(mode="confirm")
        self.workspace = workspace
        self.events = []
        self.results = []
        self.payloads = []
        self.conversation_repo = SimpleNamespace(get_conversation=lambda _id: SimpleNamespace(
            workspace_root=str(workspace), worktree_path="",
        ))
        self.session_lifecycle = SimpleNamespace(
            workspace_root=workspace,
            current_workspace_root=lambda: workspace,
        )

    def resolve_requested_workspace(self, requested=None):
        return resolve_requested_workspace(self.workspace, requested)

    async def send_event(self, event):
        self.events.append(event.to_ws_message())

    async def emit_command_result(self, command, message, **kwargs):
        self.results.append({"command": command, "message": message, **kwargs})

    async def send_payload(self, payload, **kwargs):
        self.payloads.append(payload)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    current = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: current)
    yield current
    current.close(release_lease=True)


def start_nested(runtime, *, session_id="session-1", conversation_id="conversation-1", background=False):
    runtime.start_run(run_id="main-run", conversation_id=conversation_id, session_id=session_id)
    parent = runtime.start_subagent(
        subagent_id="parent-agent", parent_run_id="main-run", agent_type="research", session_id=session_id,
    )
    child = runtime.start_subagent(
        subagent_id="nested-agent", parent_run_id=parent.subagent_id, agent_type="review", session_id=session_id, background=background,
    )
    runtime.execution_journal(child.subagent_id).append("user_prompt", {"content": "Review the nested work"})
    return parent, child


def test_nested_agent_status_transcript_message_and_cancel_use_the_same_owner(runtime, tmp_path):
    _, child = start_nested(runtime)
    session = Session(tmp_path)

    async def run():
        worker = asyncio.create_task(asyncio.Event().wait())
        runtime.register_subagent_task(
            child.subagent_id, worker, parent_run_id="parent-agent", session_id=session.session_id,
        )
        try:
            await handle_subagent_status(session, {"subagent_id": child.subagent_id})
            await handle_subagent_transcript(session, {"subagent_id": child.subagent_id})
            await handle_send_message(session, {
                "recipient": child.subagent_id, "message": "Check this detail", "message_id": "nested-message",
            })
            await handle_subagent_cancel(session, {"subagent_id": child.subagent_id})
            assert worker.cancelling()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            runtime.release_subagent_task(child.subagent_id, expected_task=worker)

    asyncio.run(run())
    assert not [event for event in session.events if event.get("level") == "error"]
    transcript = next(result for result in session.results if result["command"] == "subagent.transcript")
    assert transcript["data"]["messages"][0]["content"] == "Review the nested work"
    assert session.payloads[0]["conversation_id"] == "conversation-1"
    assert session.results[-1]["command"] == "subagent.cancel"
    assert session.results[-1]["message"] == "Subagent cancellation accepted."


def test_queued_nested_agent_resolves_ownership_from_task_metadata(runtime, tmp_path):
    runtime.start_run(run_id="main-run", conversation_id="conversation-1", session_id="session-1")
    parent = runtime.start_subagent(subagent_id="parent-agent", parent_run_id="main-run", agent_type="review")
    session = Session(tmp_path)

    async def run():
        worker = asyncio.create_task(asyncio.Event().wait())
        runtime.register_subagent_task(
            "queued-child", worker, parent_run_id=parent.subagent_id, session_id="session-1", pending=True,
        )
        try:
            await handle_subagent_status(session, {"subagent_id": "queued-child"})
            await handle_subagent_cancel(session, {"subagent_id": "queued-child"})
            assert worker.cancelling()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            runtime.release_subagent_task("queued-child", expected_task=worker)

    asyncio.run(run())
    assert session.results[0]["data"]["snapshot"]["status"] == "pending"
    assert session.results[-1]["message"] == "Subagent cancellation accepted."


def test_reopened_session_reads_durable_nested_history_without_rehydrating_live_agents(monkeypatch, tmp_path):
    metrics_file = tmp_path / "metrics.jsonl"
    previous = AgentRuntime(metrics_file=metrics_file, enable_lease_heartbeat=False)
    parent, child = start_nested(previous, session_id="closed-desktop-session")
    for agent in (child, parent):
        previous.complete_subagent(agent.subagent_id, agent_path=agent.agent_path, mailbox_epoch=agent.mailbox_epoch)
    previous.store_subagent_result(
        child.subagent_id, status="completed", content="Durable nested result",
        agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch,
    )
    previous.complete_run("main-run")
    previous.close(release_lease=True)

    restored = AgentRuntime(metrics_file=metrics_file, enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: restored)
    # Simulate history evicted from the runtime's bounded memory retention.
    restored._subagents.clear()
    restored._subagent_results.clear()
    restored._runs.clear()
    session = Session(tmp_path, session_id="reopened-desktop-session")
    try:
        asyncio.run(handle_subagent_status(session, {"subagent_id": child.subagent_id}))
        asyncio.run(handle_subagent_transcript(session, {"subagent_id": child.subagent_id}))
        assert session.events[0]["type"] == "subagent.done"
        assert session.results[0]["data"]["snapshot"]["result"]["content"] == "Durable nested result"
        assert session.results[1]["data"]["messages"][0]["content"] == "Review the nested work"
        assert restored.get_subagent(child.subagent_id) is None
        assert restored.get_subagent(parent.subagent_id) is None
        assert restored.get_run("main-run") is None
    finally:
        restored.close(release_lease=True)


@pytest.mark.parametrize("handler,payload", [
    (handle_subagent_status, {"subagent_id": "nested-agent"}),
    (handle_subagent_transcript, {"subagent_id": "nested-agent"}),
    (handle_subagent_cancel, {"subagent_id": "nested-agent"}),
    (handle_send_message, {"recipient": "nested-agent", "message": "Do not deliver", "message_id": "blocked-message"}),
])
@pytest.mark.parametrize("mismatch", ["conversation", "workspace"])
def test_all_nested_commands_reject_cross_conversation_and_workspace(runtime, tmp_path, handler, payload, mismatch):
    start_nested(runtime)
    session = Session(tmp_path, conversation_id="conversation-2" if mismatch == "conversation" else "conversation-1")
    request = dict(payload)
    if mismatch == "workspace":
        other = tmp_path / "other-project"
        other.mkdir()
        request["workspace_root"] = str(other)
    asyncio.run(handler(session, request))
    responses = session.events + session.results
    assert len(responses) == 1
    assert responses[0]["level"] == "error"
    assert mismatch in responses[0]["message"]
    assert session.payloads == []


@pytest.mark.parametrize("handler,payload", [
    (handle_subagent_cancel, {"subagent_id": "nested-agent"}),
    (handle_send_message, {"recipient": "nested-agent", "message": "Do not deliver"}),
])
def test_mutation_keeps_the_live_execution_session_boundary(runtime, tmp_path, handler, payload):
    start_nested(runtime)
    session = Session(tmp_path, session_id="another-session")
    asyncio.run(handler(session, payload))
    response = (session.events + session.results)[0]
    assert response["level"] == "error"
    assert "different session" in response["message"]
    assert session.payloads == []


def test_mutation_rejects_missing_execution_session_identity(runtime, tmp_path):
    start_nested(runtime, session_id="")
    session = Session(tmp_path)
    asyncio.run(handle_subagent_cancel(session, {"subagent_id": "nested-agent"}))
    assert session.events[-1]["level"] == "error"
    assert "execution session could not be verified" in session.events[-1]["message"]


@pytest.mark.parametrize("unfinished", ["worker", "cleanup"])
def test_follow_up_cannot_take_over_a_terminal_record_that_is_still_finishing(runtime, tmp_path, unfinished):
    _, child = start_nested(runtime)
    runtime.complete_subagent(child.subagent_id, agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
    session = Session(tmp_path, session_id="reopened-session")

    async def run():
        worker = None
        if unfinished == "worker":
            worker = asyncio.create_task(asyncio.Event().wait())
            runtime.register_subagent_task(child.subagent_id, worker, parent_run_id="parent-agent", session_id="session-1")
        else:
            runtime.get_subagent(child.subagent_id).cleanup_pending = True
        try:
            await handle_send_message(session, {"recipient": child.subagent_id, "message": "Continue"})
            assert session.results[-1]["level"] == "error"
            assert "still finishing" in session.results[-1]["message"]
        finally:
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                runtime.release_subagent_task(child.subagent_id, expected_task=worker)

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["conversation", "workspace", "missing-session"])
def test_terminal_follow_up_still_requires_current_scope_and_new_session(runtime, tmp_path, mismatch):
    _, child = start_nested(runtime)
    runtime.complete_subagent(child.subagent_id, agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
    session = Session(
        tmp_path,
        conversation_id="another-conversation" if mismatch == "conversation" else "conversation-1",
        session_id="" if mismatch == "missing-session" else "reopened-session",
    )
    request = {"recipient": child.subagent_id, "message": "Continue"}
    if mismatch == "workspace":
        other = tmp_path / "other-project"
        other.mkdir()
        request["workspace_root"] = str(other)
    asyncio.run(handle_send_message(session, request))
    assert session.results[-1]["level"] == "error"
    assert ("session" if mismatch == "missing-session" else mismatch) in session.results[-1]["message"]
    assert runtime.get_subagent(child.subagent_id).session_id == "session-1"


def test_read_rejects_missing_conversation_owner_even_when_a_record_exists(runtime, tmp_path):
    start_nested(runtime, conversation_id="")
    session = Session(tmp_path)
    asyncio.run(handle_subagent_status(session, {"subagent_id": "nested-agent"}))
    assert session.events[-1]["level"] == "error"
    assert "owner could not be verified" in session.events[-1]["message"]


def test_owner_resolution_does_not_accept_cycles_or_transcript_only_identity(runtime, tmp_path):
    runtime._subagent_task_metadata.update({
        "cycle-a": {"parent_run_id": "cycle-b", "session_id": "session-1"},
        "cycle-b": {"parent_run_id": "cycle-a", "session_id": "session-1"},
    })
    runtime.execution_journal("transcript-only").append("user_prompt", {
        "content": "This does not establish ownership", "conversation_id": "conversation-1",
    })
    session = Session(tmp_path)
    for agent_id in ("cycle-a", "transcript-only"):
        asyncio.run(handle_subagent_transcript(session, {"subagent_id": agent_id}))
    assert all(event["level"] == "error" for event in session.events)
    assert session.results == []


def test_broadcast_includes_nested_agents_only_in_the_owned_conversation(runtime):
    parent, child = start_nested(runtime)
    runtime.start_run(run_id="other-run", conversation_id="other-conversation", session_id="other-session")
    runtime.start_subagent(subagent_id="other-agent", parent_run_id="other-run", agent_type="review")
    message = runtime.send_swarm_message(
        sender_id="user", recipient_id="all", content="Shared direction", conversation_id="conversation-1",
    )
    assert message.recipient_mailbox_epochs == {
        parent.subagent_id: parent.mailbox_epoch,
        child.subagent_id: child.mailbox_epoch,
    }


def test_nested_parent_mailbox_and_result_notifications_keep_durable_owner(runtime):
    parent, child = start_nested(runtime, background=True)
    runtime._subagents.pop(parent.subagent_id)
    runtime._runs.pop("main-run")
    message = runtime.send_swarm_message(
        sender_id=child.subagent_id, sender_mailbox_epoch=child.mailbox_epoch,
        recipient_id="parent", content="Nested update", conversation_id="conversation-1",
    )
    result = runtime.store_subagent_result(
        child.subagent_id, status="completed", content="Nested result",
        agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch,
    )
    assert result is not None
    notifications = runtime.list_parent_notifications(conversation_id="conversation-1")
    assert {notice["kind"] for notice in notifications} == {"mailbox_wake", "subagent_completed"}
    assert all(notice["session_id"] == "session-1" for notice in notifications)
    result_notice = next(notice for notice in notifications if notice["kind"] == "subagent_completed")
    assert result_notice["parent_run_id"] == parent.subagent_id
    assert message.recipient_id == "parent"
    assert runtime.get_subagent(parent.subagent_id) is None
    assert runtime.get_run("main-run") is None

    with pytest.raises(ValueError, match="conversation ownership mismatch"):
        runtime.send_swarm_message(
            sender_id=child.subagent_id, sender_mailbox_epoch=child.mailbox_epoch,
            recipient_id="parent", content="Wrong conversation", conversation_id="other-conversation",
        )
