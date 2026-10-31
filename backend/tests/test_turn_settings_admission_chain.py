from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import UserCommand
from backend.api import _state
from backend.conversations.repository import ConversationRepository
from backend.main import app
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.tests.test_user_input_presentation_chain import session_for
from tests.test_conversations import _install_noop_llm, _receive_until_event


def _user_result(socket):
    for _ in range(120):
        event = socket.receive_json()
        if event.get("type") == "command.result" and event.get("command") == "user_message":
            return event
    raise AssertionError("No user-message result")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("workspace_selected", [False, True])
def test_real_capacity_rejection_keeps_permission_workspace_and_cwd(tmp_path, monkeypatch, workspace_selected):
    _install_noop_llm(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    selected = tmp_path / "selected"
    selected.mkdir()
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=turn-settings-capacity") as socket:
            _receive_until_event(socket, "llm.model.updated")
            session = _state.ws_manager.get_session("turn-settings-capacity")
            owner = session.conversation_repo.create_conversation(title="Draft", permission_mode="confirm", memory_mode="none")
            socket.send_json({"type": "conversation.switch", "conversation_id": owner.id})
            _receive_until_event(socket, "conversation.switched", max_attempts=100)

            async def occupy():
                session.task_manager._max_tasks = 1
                return session.task_manager.create("controlled-capacity", asyncio.Event().wait()).id

            held = client.portal.call(occupy)
            socket.send_json({"type": "user_message", "content": "preserve this input", "conversation_id": owner.id,
                "permission_mode": "bypass", "client_command_id": "capacity-input",
                "user_message_id": "capacity-user", "assistant_message_id": "capacity-answer",
                "_permission_mode_selection_revision": 999, "_workspace_binding_selection_revision": 999,
                **({"workspace_root": str(selected)} if workspace_selected else {})})
            result = _user_result(socket)
            record = session.conversation_repo.get_conversation(owner.id)
            assert result["level"] == "error" and result["data"]["admitted"] is False
            assert record.permission_mode == "confirm" and record.permission_mode_revision == 0
            assert record.workspace_root == record.execution_cwd == "" and record.workspace_binding_revision == 0
            assert not record.transcript and session.session_lifecycle.current_workspace_root() is None
            async def wait_for_rejection():
                tasks = tuple(session.command_dispatcher.execution_command_tasks)
                if tasks:
                    await asyncio.gather(*tasks)

            client.portal.call(wait_for_rejection)
            retained = session.run_manager.durable_client_commands.pending_client_commands()
            assert len(retained) == 1
            assert retained[0].data["_permission_mode_selection_revision"] == 0
            assert retained[0].data["_workspace_binding_selection_revision"] == 0
            client.portal.call(session.task_manager.cancel, held)


@pytest.mark.asyncio
async def test_real_admission_uses_prepared_settings_and_commits_them_with_the_user(tmp_path, monkeypatch):
    session, _, adapter = session_for(tmp_path, monkeypatch)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    owner = session.conversation_repo.create_conversation(title="Draft", permission_mode="confirm", memory_mode="none")
    session.active_conversation_id = owner.id
    session.permission_context = session.permission_context_for_conversation(owner, source="test")
    selected = tmp_path / "accepted-workspace"
    selected.mkdir()
    observed = []
    submit = session.query_engine.submit

    async def query(submission):
        saved = session.conversation_repo.get_conversation(owner.id)
        observed.append((submission.runtime.permission_context.mode, submission.runtime.workspace_root,
                         saved.permission_mode, saved.workspace_root))
        async for event in submit(submission):
            yield event

    monkeypatch.setattr(session.query_engine, "submit", query)
    try:
        await asyncio.wait_for(session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
            "content": "apply one admitted input", "conversation_id": owner.id, "permission_mode": "bypass",
            "workspace_root": str(selected), "client_command_id": "accepted-settings",
            "user_message_id": "settings-user", "assistant_message_id": "settings-answer",
        }), session.connection_generation), 30)
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        await session.event_outbox.drain_delivery()
        saved = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert observed == [("bypass", selected, "confirm", "")]
        assert saved.permission_mode == "bypass" and saved.permission_mode_revision == 1
        assert saved.workspace_root == saved.execution_cwd == str(selected) and saved.workspace_binding_revision == 1
        assert [item["id"] for item in saved.transcript if item["role"] == "user"] == ["settings-user"]
        assert session.permission_context.mode == "bypass"
        assert session.session_lifecycle.current_workspace_root() == selected
        assert len(adapter.requests) == 1
        assert any(event.get("type") == "command.result" and event.get("data", {}).get("admitted")
                   for event in session.ws.messages)
    finally:
        await session.session_lifecycle.shutdown(reason="settings-admission-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["workspace_prepare", "request_capture", "admission_write"])
async def test_real_startup_failures_keep_unadmitted_thread_settings(tmp_path, monkeypatch, failure_phase):
    session, _, adapter = session_for(tmp_path, monkeypatch)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    owner = session.conversation_repo.create_conversation(title="Draft", permission_mode="confirm", memory_mode="none")
    session.active_conversation_id = owner.id
    session.permission_context = session.permission_context_for_conversation(owner, source="test")
    selected = tmp_path / "unadmitted-workspace"
    selected.mkdir()
    if failure_phase == "workspace_prepare":
        async def fail_prepare(_self):
            raise OSError("controlled workspace preparation failure")
        monkeypatch.setattr("backend.workspace.context.WorkspaceContext.initialize", fail_prepare)
    elif failure_phase == "request_capture":
        def fail_capture(*_args, **_kwargs):
            raise OSError("controlled request capture failure")
        monkeypatch.setattr(session, "capture_agent_run_request", fail_capture)
    else:
        commit = session.conversation_repo._commit_record

        def fail_admission(record):
            if any(item.get("id") == "rejected-settings-user" for item in record.transcript):
                raise OSError("controlled admission write failure")
            return commit(record)

        monkeypatch.setattr(session.conversation_repo, "_commit_record", fail_admission)
    try:
        await asyncio.wait_for(session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
            "content": "do not commit rejected settings", "conversation_id": owner.id, "permission_mode": "bypass",
            "workspace_root": str(selected), "client_command_id": "rejected-settings",
            "user_message_id": "rejected-settings-user", "assistant_message_id": "rejected-settings-answer",
        }), session.connection_generation), 25)
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        await session.event_outbox.drain_delivery()
        saved = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert saved.permission_mode == "confirm" and saved.permission_mode_revision == 0
        assert saved.workspace_root == saved.execution_cwd == "" and saved.workspace_binding_revision == 0
        assert not any(item["role"] == "user" for item in saved.transcript)
        assert not adapter.requests
        assert session.session_lifecycle.current_workspace_root() is None
        assert any(event.get("type") == "command.result" and event.get("data", {}).get("admitted") is False
                   for event in session.ws.messages)
    finally:
        await session.session_lifecycle.shutdown(reason="settings-rejection-test")


@pytest.mark.asyncio
async def test_stop_during_workspace_preparation_keeps_the_original_settings_and_input(tmp_path, monkeypatch):
    from backend.workspace.context import WorkspaceContext

    session, _, adapter = session_for(tmp_path, monkeypatch)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    owner = session.conversation_repo.create_conversation(title="Preparing", memory_mode="none")
    session.active_conversation_id = owner.id
    selected = tmp_path / "preparing-workspace"
    selected.mkdir()
    entered, release = asyncio.Event(), asyncio.Event()
    initialize = WorkspaceContext.initialize

    async def held_initialize(context):
        entered.set()
        await release.wait()
        return await initialize(context)

    monkeypatch.setattr(WorkspaceContext, "initialize", held_initialize)
    sending = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
        "content": "retain cancelled preparation", "conversation_id": owner.id, "permission_mode": "bypass",
        "workspace_root": str(selected), "client_command_id": "preparing-settings",
        "user_message_id": "preparing-user", "assistant_message_id": "preparing-answer",
    }), session.connection_generation))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await asyncio.wait_for(session.command_dispatcher._dispatch_client_command(UserCommand("interrupt", {
            "conversation_id": owner.id, "message_id": "preparing-answer",
        }), session.connection_generation), 10)
        assert await asyncio.wait_for(sending, 10)
        saved = session.conversation_repo.get_conversation(owner.id)
        assert saved.permission_mode == "confirm" and saved.workspace_root == saved.execution_cwd == ""
        assert not adapter.requests and not session.run_manager.run_tasks
        archived = await session.command_dispatcher.cancelled_user_message("preparing-settings")
        assert archived["command"]["data"]["content"] == "retain cancelled preparation"
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="settings-prepare-stop-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_selection", ["settings", "conversation"])
async def test_slow_admission_keeps_its_run_snapshot_and_preserves_newer_selection(tmp_path, monkeypatch, newer_selection):
    session, _, _ = session_for(tmp_path, monkeypatch)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    owner = session.conversation_repo.create_conversation(title="A", permission_mode="confirm", memory_mode="none")
    other = session.conversation_repo.create_conversation(title="B", execution_cwd=str(tmp_path), permission_mode="confirm", memory_mode="none")
    session.active_conversation_id = owner.id
    session.permission_context = session.permission_context_for_conversation(owner, source="test")
    selected, newer = tmp_path / "planned", tmp_path / "newer"
    selected.mkdir()
    newer.mkdir()
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []
    submit = session.query_engine.submit

    async def query(submission):
        observed.append((submission.runtime.permission_context.mode, submission.runtime.workspace_root))
        entered.set()
        await release.wait()
        async for event in submit(submission):
            yield event

    monkeypatch.setattr(session.query_engine, "submit", query)
    sending = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
        "content": "keep this run's settings", "conversation_id": owner.id, "permission_mode": "bypass",
        "workspace_root": str(selected), "client_command_id": "slow-settings",
        "user_message_id": "slow-user", "assistant_message_id": "slow-answer",
    }), session.connection_generation))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        assert session.conversation_repo.get_conversation(owner.id).permission_mode == "confirm"
        if newer_selection == "settings":
            await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.permission_mode.set", {
                "conversation_id": owner.id, "mode": "auto",
            }), session.connection_generation)
            session.conversation_repo.update_workspace_binding(owner.id, workspace_root=str(newer), execution_cwd=str(newer))
        else:
            await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch", {
                "conversation_id": other.id,
            }), session.connection_generation)
        release.set()
        assert await asyncio.wait_for(sending, 25)
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        saved = session.conversation_repo.get_conversation(owner.id)
        assert observed == [("bypass", selected)]
        if newer_selection == "settings":
            assert saved.permission_mode == "auto" and saved.workspace_root == saved.execution_cwd == str(newer)
            assert session.permission_context.mode == "auto"
        else:
            assert saved.permission_mode == "bypass" and saved.workspace_root == str(selected)
            assert session.active_conversation_id == other.id and session.permission_context.mode == "confirm"
            assert session.session_lifecycle.workspace_root_for_conversation() == tmp_path
        assert [item["id"] for item in saved.transcript if item["role"] == "user"] == ["slow-user"]
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="settings-selection-test")


@pytest.mark.asyncio
async def test_queued_input_keeps_its_settings_without_overwriting_a_later_permission_choice(tmp_path, monkeypatch):
    session, owner, _ = session_for(tmp_path, monkeypatch)
    session.conversation_repo.update_memory_mode(owner.id, "none")
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []
    submit = session.query_engine.submit

    async def query(submission):
        observed.append((submission.user_message, submission.runtime.permission_context.mode))
        if submission.user_message == "first":
            entered.set()
            await release.wait()
        async for event in submit(submission):
            yield event

    monkeypatch.setattr(session.query_engine, "submit", query)
    first = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
        "content": "first", "conversation_id": owner.id, "client_command_id": "first-queued-settings",
        "user_message_id": "first-queued-user", "assistant_message_id": "first-queued-answer",
    }), session.connection_generation))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        await session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
            "content": "second", "conversation_id": owner.id, "permission_mode": "bypass",
            "client_command_id": "second-queued-settings", "user_message_id": "second-queued-user",
            "assistant_message_id": "second-queued-answer",
        }), session.connection_generation)
        queued, = session.run_manager.queued_user_messages(owner.id)
        assert queued.data["_permission_mode_selection_revision"] == 0
        await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.permission_mode.set", {
            "conversation_id": owner.id, "mode": "auto",
        }), session.connection_generation)
        release.set()
        await asyncio.wait_for(first, 25)
        async with asyncio.timeout(25):
            while (len(observed) < 2 or session.running_agent_task_for(owner.id)
                    or session.run_manager.is_queue_dispatching(owner.id)):
                await asyncio.sleep(.01)
        saved = session.conversation_repo.get_conversation(owner.id)
        assert observed == [("first", "confirm"), ("second", "bypass")]
        assert saved.permission_mode == session.permission_context.mode == "auto"
        assert [item["id"] for item in saved.transcript if item["role"] == "user"] == ["first-queued-user", "second-queued-user"]
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="settings-queue-test")


@pytest.mark.asyncio
async def test_cold_replay_keeps_saved_selection_versions_after_a_real_process_exit(tmp_path, monkeypatch):
    session, owner, _ = session_for(tmp_path, monkeypatch)
    session.conversation_repo.update_memory_mode(owner.id, "none")
    queue = session.run_manager.durable_client_commands
    queue.close()
    program = r'''
import json, os, sys
from pathlib import Path
from backend.agent.message import UserCommand
from backend.ws.durable_user_queue import DurableUserMessageQueue
queue = DurableUserMessageQueue(session_id=sys.argv[1], root_dir=Path(sys.argv[2]))
assert queue.persist_client_command(UserCommand('user_message', json.loads(sys.argv[3])))
os._exit(77)
'''
    data = {"content": "replay original settings", "conversation_id": owner.id, "permission_mode": "bypass",
        "client_command_id": "cold-settings", "user_message_id": "cold-settings-user",
        "assistant_message_id": "cold-settings-answer", "_permission_mode_selection_revision": 0,
        "_workspace_binding_selection_revision": 0}
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program, session.session_id,
        str(queue.path.parent), json.dumps(data)], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 77, result.stderr
    cold = DurableUserMessageQueue(session_id=session.session_id, root_dir=queue.path.parent)
    session.run_manager._durable_client_commands = cold
    session.conversation_repo.update_permission_mode(owner.id, "auto")
    session.permission_context = session.permission_context_for_conversation(
        session.conversation_repo.get_conversation(owner.id), source="newer-choice")
    observed = []
    submit = session.query_engine.submit

    async def query(submission):
        observed.append(submission.runtime.permission_context.mode)
        async for event in submit(submission):
            yield event

    monkeypatch.setattr(session.query_engine, "submit", query)
    try:
        await session.command_dispatcher._replay_pending_client_commands(session.connection_generation)
        tasks = tuple(session.command_dispatcher.execution_command_tasks)
        assert tasks
        await asyncio.wait_for(asyncio.gather(*tasks), 25)
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        saved = ConversationRepository(session.conversation_repo._base_dir).get_conversation(owner.id)
        assert observed == ["bypass"]
        assert saved.permission_mode == session.permission_context.mode == "auto"
        assert [item["id"] for item in saved.transcript if item["role"] == "user"] == ["cold-settings-user"]
        assert cold.is_client_command_completed("cold-settings")
    finally:
        await session.session_lifecycle.shutdown(reason="settings-cold-test")


@pytest.mark.parametrize("write_fails", [False, True])
def test_repository_admission_publishes_settings_and_message_in_one_generation(tmp_path, monkeypatch, write_fails):
    repository = ConversationRepository(tmp_path / "conversations")
    owner = repository.create_conversation(title="Atomic settings", memory_mode="none")
    selected = tmp_path / "selected"
    selected.mkdir()
    arguments = {"user_message": {"id": "atomic-user", "role": "user", "content": "accepted together"},
        "context_snapshot": {"turn_admissions": {"atomic-user": {}}},
        "permission_mode": "bypass", "expected_permission_mode_revision": 0,
        "workspace_binding": {"workspace_root": str(selected), "execution_cwd": str(selected)},
        "expected_workspace_binding_revision": 0}
    if write_fails:
        def fail(_record):
            raise OSError("controlled generation failure")
        with monkeypatch.context() as fault:
            fault.setattr(repository, "_commit_record", fail)
            with pytest.raises(OSError, match="controlled generation failure"):
                repository.commit_turn_admission(owner.id, **arguments)
    else:
        repository.commit_turn_admission(owner.id, **arguments)
    saved = ConversationRepository(repository._base_dir).get_conversation(owner.id)
    assert saved.permission_mode == ("confirm" if write_fails else "bypass")
    assert saved.workspace_root == ("" if write_fails else str(selected))
    assert [item["id"] for item in saved.transcript] == ([] if write_fails else ["atomic-user"])
    assert repository.get_conversation(owner.id).permission_mode == saved.permission_mode


def test_settings_versions_preserve_newer_selections_even_after_values_change_back(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    owner = repository.create_conversation(memory_mode="none")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    repository.update_permission_mode(owner.id, "bypass")
    repository.update_permission_mode(owner.id, "confirm")
    repository.update_workspace_binding(owner.id, workspace_root=str(second), execution_cwd=str(second))
    repository.update_workspace_binding(owner.id, workspace_root="", execution_cwd="")
    before = repository.get_conversation(owner.id)
    repository.commit_turn_admission(owner.id, user_message={"id": "older-input", "role": "user", "content": "old settings"},
        context_snapshot={}, permission_mode="bypass", expected_permission_mode_revision=0,
        workspace_binding={"workspace_root": str(first), "execution_cwd": str(first)}, expected_workspace_binding_revision=0)
    saved = ConversationRepository(repository._base_dir).get_conversation(owner.id)
    assert saved.permission_mode == "confirm" and saved.permission_mode_revision == before.permission_mode_revision == 2
    assert saved.workspace_root == "" and saved.workspace_binding_revision == before.workspace_binding_revision == 2
