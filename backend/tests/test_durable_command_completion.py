from __future__ import annotations

import asyncio
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import UserCommand
from backend.conversations.repository import ConversationCreationUnavailableError, ConversationRepository
from backend.tests.test_conversation_activation_contract import _session
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.event_outbox import EventOutbox
from backend.ws.handlers import conversation


@pytest.fixture
def clone_dispatcher(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "conversations")
    source = repo.create_conversation(title="original")
    session = _session(repo, source.id)
    session.session_id = "completion-window"
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=None)
    session.event_outbox = EventOutbox(session_id=session.session_id, websocket=None,
        replay_root=tmp_path / "replay", replay_limit=30, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *args: False, workspace_scoped_event_types=())
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)
    monkeypatch.setattr(conversation, "_broadcast_conversation_lists", AsyncMock(return_value=[]))
    calls = []

    async def dispatch(command, generation):
        calls.append(command.data["client_command_id"])
        with session.event_outbox.bind_client_command(command.data["client_command_id"], command.type):
            return await conversation.handle_conversation_clone(session, command.data)

    dispatcher._dispatch_client_command = dispatch
    command = UserCommand("conversation.clone", {"client_command_id": "clone-one", "conversation_id": source.id})
    assert queue.persist_client_command(command)
    yield SimpleNamespace(repo=repo, source=source, session=session, queue=queue,
                          dispatcher=dispatcher, command=command, calls=calls, root=tmp_path)
    session.run_manager.durable_client_commands.close()


def clone_ids(case):
    return [item.id for item in case.repo.list_conversations() if item.parent_conversation_id == case.source.id]


@pytest.mark.asyncio
async def test_failed_secondary_log_cannot_replay_real_clone_after_restart(clone_dispatcher, monkeypatch):
    case = clone_dispatcher

    def fail_append(*args, **kwargs):
        raise OSError("completion index unavailable")

    monkeypatch.setattr(case.dispatcher.client_command_store, "append", fail_append)
    await case.dispatcher._run_durable_client_command("clone-one", 1)
    original, = clone_ids(case)
    assert case.queue.is_client_command_completed("clone-one")
    assert not case.queue.has_client_command("clone-one")
    case.queue.close()
    cold_queue = DurableUserMessageQueue(session_id=case.session.session_id, root_dir=case.root / "queue")
    case.session.run_manager.durable_client_commands = cold_queue
    cold = SessionCommandDispatcher(case.session, root_dir=case.root / "dedup")
    cold.cancelled_user_message = AsyncMock(return_value=None)
    cold._dispatch_client_command = case.dispatcher._dispatch_client_command
    assert cold._client_command_seen(case.command)
    await cold._run_durable_client_command("clone-one", 2)
    assert clone_ids(case) == [original] and case.calls == ["clone-one"]


@pytest.mark.asyncio
async def test_failed_atomic_completion_retries_only_commit_in_live_process(clone_dispatcher, monkeypatch):
    case = clone_dispatcher
    write = case.queue._write_current_unlocked

    def fail_completion():
        if "clone-one" in case.queue._client_completed:
            raise OSError("queue write unavailable")
        write()

    with monkeypatch.context() as fault:
        fault.setattr(case.queue, "_write_current_unlocked", fail_completion)
        with pytest.raises(OSError, match="queue write unavailable"):
            await case.dispatcher._run_durable_client_command("clone-one", 1)
    original, = clone_ids(case)
    assert case.queue.has_client_command("clone-one")
    assert not case.queue.is_client_command_completed("clone-one")
    await case.dispatcher._run_durable_client_command("clone-one", 1)
    assert case.queue.is_client_command_completed("clone-one")
    assert not case.queue.has_client_command("clone-one")
    assert clone_ids(case) == [original] and case.calls == ["clone-one"]


@pytest.mark.asyncio
async def test_cold_replay_after_clone_before_queue_commit_reuses_the_original(clone_dispatcher, monkeypatch):
    case = clone_dispatcher

    def fail_completion(*_args):
        raise OSError("interrupted before queue commit")

    with monkeypatch.context() as fault:
        fault.setattr(case.queue, "complete_client_command", fail_completion)
        with pytest.raises(OSError, match="before queue commit"):
            await case.dispatcher._run_durable_client_command("clone-one", 1)
    original, = clone_ids(case)
    case.queue.close()
    cold_queue = DurableUserMessageQueue(session_id=case.session.session_id, root_dir=case.root / "queue")
    case.session.run_manager.durable_client_commands = cold_queue
    cold = SessionCommandDispatcher(case.session, root_dir=case.root / "dedup")
    cold.cancelled_user_message = AsyncMock(return_value=None)
    cold._dispatch_client_command = case.dispatcher._dispatch_client_command
    await cold._run_durable_client_command("clone-one", 2)

    assert clone_ids(case) == [original]
    assert cold_queue.is_client_command_completed("clone-one")
    assert case.calls == ["clone-one", "clone-one"]


@pytest.mark.asyncio
async def test_actual_process_exit_after_clone_replays_without_another_copy(tmp_path, monkeypatch):
    root = tmp_path / "crash-case"
    repo = ConversationRepository(root / "conversations")
    source = repo.create_conversation(title="Source")
    program = r'''
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from backend.agent.message import UserCommand
from backend.conversations.repository import ConversationRepository
from backend.tests.test_conversation_activation_contract import _session
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.event_outbox import EventOutbox
from backend.ws.handlers import conversation

async def run():
    root, source_id = Path(sys.argv[1]), sys.argv[2]
    repo = ConversationRepository(root / "conversations")
    session = _session(repo, source_id)
    session.session_id = "crash-window"
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=root / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=None)
    session.event_outbox = EventOutbox(session_id=session.session_id, websocket=None, replay_root=root / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    conversation._broadcast_conversation_lists = AsyncMock(return_value=[])
    async def dispatch(command, generation):
        with session.event_outbox.bind_client_command(command.data["client_command_id"], command.type):
            return await conversation.handle_conversation_clone(session, command.data)
    dispatcher = SessionCommandDispatcher(session, root_dir=root / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)
    dispatcher._dispatch_client_command = dispatch
    command = UserCommand("conversation.clone", {"client_command_id": "clone-crash", "conversation_id": source_id})
    assert queue.persist_client_command(command)
    queue.complete_client_command = lambda *_: os._exit(91)
    await dispatcher._run_durable_client_command("clone-crash", 1)

asyncio.run(run())
'''
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program, str(root), source.id],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 91, result.stderr

    cold_repo = ConversationRepository(root / "conversations")
    original, = [item.id for item in cold_repo.list_conversations() if item.parent_conversation_id == source.id]
    session = _session(cold_repo, source.id)
    session.session_id = "crash-window"
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=root / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=None)
    session.event_outbox = EventOutbox(session_id=session.session_id, websocket=None, replay_root=root / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    monkeypatch.setattr(conversation, "_broadcast_conversation_lists", AsyncMock(return_value=[]))

    async def dispatch(command, generation):
        with session.event_outbox.bind_client_command(command.data["client_command_id"], command.type):
            return await conversation.handle_conversation_clone(session, command.data)

    dispatcher = SessionCommandDispatcher(session, root_dir=root / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)
    dispatcher._dispatch_client_command = dispatch
    try:
        await dispatcher._run_durable_client_command("clone-crash", 2)
        assert [item.id for item in cold_repo.list_conversations() if item.parent_conversation_id == source.id] == [original]
        assert queue.is_client_command_completed("clone-crash")
    finally:
        queue.close()


def test_clone_idempotency_preserves_placement_and_deleted_identity(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    first = repo.thread_sections.create("First")
    second = repo.thread_sections.create("Second")
    source = repo.create_conversation(title="Source", sidebar_section_id=first["id"])
    clone = repo.clone_conversation(source.id, title="Copy", idempotency_key="session:request")
    repo.thread_sections.move_thread(clone.id, second["id"])

    cold = ConversationRepository(repo.storage_root)
    repeated = cold.clone_conversation(source.id, title="Copy", idempotency_key="session:request")
    assert repeated.id == clone.id
    sections = cold.thread_sections.list()
    assert next(item for item in sections if item["id"] == second["id"])["item_keys"].count("thread:" + clone.id) == 1
    cold.thread_sections.move_thread(clone.id, None)
    assert cold.clone_conversation(source.id, title="Copy", idempotency_key="session:request").id == clone.id
    assert all("thread:" + clone.id not in item["item_keys"] for item in cold.thread_sections.list())
    with pytest.raises(ConversationCreationUnavailableError, match="different request"):
        cold.clone_conversation(source.id, title="Different", idempotency_key="session:request")
    assert cold.delete_conversation(clone.id)
    with pytest.raises(ConversationCreationUnavailableError, match="unavailable"):
        cold.clone_conversation(source.id, title="Copy", idempotency_key="session:request")
    assert cold.get_conversation(source.id) is not None


def test_concurrent_clone_requests_with_one_id_create_one_record(tmp_path):
    root = tmp_path / "conversations"
    repo = ConversationRepository(root)
    source = repo.create_conversation(title="Source")
    barrier = Barrier(2)

    def clone():
        owner = ConversationRepository(root)
        barrier.wait()
        return owner.clone_conversation(source.id, title="Copy", idempotency_key="session:request")

    with ThreadPoolExecutor(max_workers=2) as executor:
        clones = list(executor.map(lambda _: clone(), range(2)))
    assert clones[0].id == clones[1].id
    assert len([item for item in repo.list_conversations() if item.parent_conversation_id == source.id]) == 1
