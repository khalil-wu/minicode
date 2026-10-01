from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.message import AgentEvent, UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.stream_state import create_stream_state


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def message(cid, name):
    return UserCommand('user_message', {
        'conversation_id': cid, 'content': name,
        'user_message_id': f'user-{name}', 'assistant_message_id': f'assistant-{name}',
    })


async def drain_commands(session):
    async with asyncio.timeout(3):
        while session.command_dispatcher.command_tasks:
            await asyncio.gather(*tuple(session.command_dispatcher.command_tasks))
            session.command_dispatcher.prune_command_tasks()


@pytest_asyncio.fixture
async def make_session(tmp_path, monkeypatch):
    sessions = []
    monkeypatch.setattr('backend.hooks.runtime.run_session_end_hook', AsyncMock())

    def make(session_id='retirement-oracle'):
        session = WebSocketSession(
            session_id, Socket(), SimpleNamespace(),
            ArtifactStore(storage_dir=tmp_path / f'artifacts-{len(sessions)}'),
            ToolRegistry(), PermissionChecker(PermissionSettings()),
            AppConfig(llm=LLMSettings(api_key='')), mcp_manager=None,
        )
        session.session_lifecycle.schedule_task_runtime_update = lambda: None
        # The injected runners own no child-agent tree. Keep teardown focused
        # on the real session/command/queue lifecycle, not an unrelated runtime.
        session.run_manager._cancel_run_tree = AsyncMock()
        sessions.append(session)
        return session

    yield make
    for session in sessions:
        if not session.session_lifecycle.is_shutting_down:
            await session.session_lifecycle.shutdown(reason='oracle_cleanup')


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['disconnect_timeout', 'session_shutdown'])
async def test_retirement_preserves_disk_pending_work_and_releases_owner_lease(make_session, reason):
    old = make_session()
    cid = old.conversation_repo.create_conversation(conversation_id='conv-disk-oracle').id
    other = old.conversation_repo.create_conversation(conversation_id='conv-disk-inflight').id
    old.active_conversation_id = cid
    manager = old.run_manager
    manager.turn_input_queue(cid)
    steer = message(cid, 'steer')
    assert manager.enqueue_user_message_as_steer(cid, steer) is not None
    # Preaccepted by a provider chunk, but not yet acknowledged into context.
    assert manager.turn_input_queue(cid).pop_steer() is not None
    manager.enqueue_user_message(cid, message(cid, 'follow-up'))
    manager.enqueue_user_message(other, message(other, 'inflight'))
    manager.enqueue_user_message(other, message(other, 'tail'))
    inflight = manager.dequeue_user_message(other)
    assert inflight is not None
    queue = manager.durable_queue
    client = UserCommand('session.sync', {'client_command_id': 'client-pending'})
    assert queue.persist_client_command(client)
    assert queue.claim_client_command('client-pending') is not None
    before = json.loads(queue.path.read_text(encoding='utf-8'))
    assert before['turn_inputs'][cid][0]['data']['content'] == 'steer'
    assert before['inflight'][other]['data']['content'] == 'inflight'

    # A concurrently opened store cannot treat a live owner's dispatch as a
    # crash. It must only recover it after real retirement releases the lease.
    fresh = make_session()
    assert fresh.run_manager.dequeue_user_message(other) is None
    assert fresh.run_manager.durable_queue.claim_client_command('client-pending') is None
    await old.session_lifecycle.shutdown(reason=reason)
    persisted = json.loads(queue.path.read_text(encoding='utf-8'))
    assert persisted['queues'][cid][0]['data']['content'] == 'follow-up'
    assert persisted['turn_inputs'][cid][0]['data']['content'] == 'steer'
    assert persisted['inflight'][other]['data']['content'] == 'inflight'
    assert persisted['client_inflight']['client-pending']['data']['client_command_id'] == 'client-pending'
    # A retired scheduler is never allowed to read/claim through a closed lease.
    old.schedule_next_queued_user_message(cid)
    assert not old.command_dispatcher.command_tasks

    recovered = {}
    for owner in (cid, other):
        recovered[owner] = []
        while command := fresh.run_manager.dequeue_user_message(owner):
            recovered[owner].append(command.data['content'])
            fresh.run_manager.finish_user_message_dispatch(owner, command, succeeded=True)
    assert recovered == {cid: ['steer', 'follow-up'], other: ['inflight', 'tail']}
    assert fresh.run_manager.durable_queue.claim_client_command('client-pending') == client
    assert fresh.run_manager.durable_queue.complete_client_command('client-pending')
    verifier = make_session()
    assert verifier.run_manager.dequeue_user_message(cid) is None
    assert verifier.run_manager.dequeue_user_message(other) is None
    assert verifier.run_manager.durable_queue.pending_client_commands() == []


@pytest.mark.asyncio
async def test_live_callback_done_gate_then_true_claim_release_dispatches_once(make_session):
    session = make_session('callback-gate-oracle')
    cid = session.conversation_repo.create_conversation(conversation_id='conv-callback-gate').id
    session.active_conversation_id = cid
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()
    finish_old = asyncio.Event()
    admitted = []
    old_claim = None

    async def callback():
        callback_entered.set()
        await release_callback.wait()

    async def run_locked(content, *, metadata, run_context, query_claim, **kwargs):
        nonlocal old_claim
        assert conversation_query_guards().owns(query_claim)
        admitted.append(content)
        session.conversation_repo.append_transcript_message(cid, {
            'id': metadata['user_message_id'], 'role': 'user', 'content': content,
        })
        session._conversation_streams[cid] = create_stream_state(cid, metadata['assistant_message_id'])
        metadata['_turn_admission_future'].set_result(None)
        if content == 'old':
            old_claim = query_claim
            borrower = asyncio.create_task(callback())
            run_context.lifecycle_cleanup_tasks.add(borrower)
            await callback_entered.wait()
            await finish_old.wait()
        done = AgentEvent.done()
        done.data.update(conversation_id=cid, message_id=metadata['assistant_message_id'])
        await session.send_event(done)
        session.run_manager.mark_delivery_complete(cid, metadata['_run_task_id'])

    session._run_agent_locked = run_locked
    assert await session.command_dispatcher._handle_command(message(cid, 'old'))
    await callback_entered.wait()
    assert await session.command_dispatcher._handle_command(message(cid, 'queued'))
    finish_old.set()
    await drain_commands(session)
    assert session.run_manager.run_tasks == {}
    assert conversation_query_guards().owns(old_claim)
    assert session.run_manager.has_pending_lifecycle_cleanup(cid)
    assert admitted == ['old']
    assert [item['content'] for item in session.run_manager.queued_user_message_snapshot(cid)] == ['queued']
    disk = json.loads(session.run_manager.durable_queue.path.read_text(encoding='utf-8'))
    assert disk['queues'][cid][0]['data']['content'] == 'queued'
    assert not any(p['type'] == 'user_message.queue.updated' and p['status'] == 'dequeued' for p in session.ws.sent)

    # Normal ingress after raw cleanup also queues behind actual borrowers.
    assert await session.command_dispatcher._handle_command(message(cid, 'late'))
    assert admitted == ['old']
    release_callback.set()
    async with asyncio.timeout(3):
        while conversation_query_guards().owns(old_claim):
            await asyncio.sleep(0)
    await drain_commands(session)
    await session.event_outbox.drain_delivery()
    assert admitted == ['old', 'queued', 'late']
    assert session.run_manager.queued_user_message_snapshot(cid) == []
    assert not session.run_manager.has_pending_lifecycle_cleanup(cid)
    assert not any(p.get('error_type') == 'conversation_busy' for p in session.ws.sent)
    assert not any(p.get('reason') == 'conversation_busy' for p in session.ws.sent)
    assert [p['message_id'] for p in session.ws.sent if p['type'] == 'user_message.queue.updated' and p['status'] == 'dequeued'] == ['assistant-queued', 'assistant-late']
    persisted = json.loads(session.run_manager.durable_queue.path.read_text(encoding='utf-8'))
    assert persisted['queues'] == persisted['inflight'] == {}


@pytest.mark.asyncio
async def test_late_true_release_wake_cannot_dispatch_from_retired_closed_queue(make_session):
    old = make_session('late-release-oracle')
    cid = old.conversation_repo.create_conversation(conversation_id='conv-late-release').id
    release = asyncio.Event()
    borrower = asyncio.create_task(release.wait())
    old.run_manager.lifecycle_cleanup_tasks_for(cid).add(borrower)
    claim = conversation_query_guards().try_start(cid, owner_id='ws:retiring')
    assert claim is not None
    old.run_manager.enqueue_user_message(cid, message(cid, 'retained'))
    old.run_manager.release_query_claim(claim)
    await old.session_lifecycle.shutdown(reason='disconnect_timeout')
    assert conversation_query_guards().owns(claim)
    release.set()
    await borrower
    async with asyncio.timeout(3):
        while conversation_query_guards().owns(claim):
            await asyncio.sleep(0)
    assert not old.command_dispatcher.command_tasks
    fresh = make_session('late-release-oracle')
    recovered = fresh.run_manager.dequeue_user_message(cid)
    assert recovered.data['content'] == 'retained'
    fresh.run_manager.finish_user_message_dispatch(cid, recovered, succeeded=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered', [False, True])
@pytest.mark.parametrize('supplied_id', ['assistant-old', 'invalid id/'])
async def test_old_cleanup_handoff_keeps_new_stream_and_captured_message_owner(make_session, delivered, supplied_id):
    session = make_session('done-handoff-oracle')
    cid = session.conversation_repo.create_conversation(conversation_id='conv-done-handoff').id
    session.active_conversation_id = cid
    release_new = asyncio.Event()
    finish_old = asyncio.Event()
    captured = {}
    cleanup_finished = asyncio.Event()
    real_cleanup = session._cleanup_agent_run

    def observe_cleanup(**kwargs):
        real_cleanup(**kwargs)
        cleanup_finished.set()

    session._cleanup_agent_run = observe_cleanup

    async def old_runner(content, *, metadata, **kwargs):
        captured['old_id'] = metadata['assistant_message_id']
        captured['old_task_id'] = metadata['_run_task_id']
        metadata['run_id'] = 'admitted-old-without-record'
        session._conversation_streams[cid] = create_stream_state(cid, metadata['assistant_message_id'])
        # Register a real task's completion callback before the cleanup waiter
        # awaits it. This forces DONE handoff before the old waiter resumes.
        def handoff(_done):
            new = asyncio.create_task(release_new.wait())
            captured['new'] = new
            session.run_manager.register(
                conversation_id=cid, task=new, task_id='new-run',
                cancel_event=asyncio.Event(), active_conversation_id=cid,
            )
            session._conversation_streams[cid] = create_stream_state(cid, 'assistant-new')

        asyncio.current_task().add_done_callback(handoff)
        metadata['_turn_admission_future'].set_result(None)
        await finish_old.wait()
        if delivered:
            event = AgentEvent.done()
            event.data.update(conversation_id=cid, message_id=metadata['assistant_message_id'])
            await session.send_event(event)
            session.run_manager.mark_delivery_complete(cid, metadata['_run_task_id'])

    session._run_agent = old_runner
    await session.start_agent_run('old', conversation_id=cid, metadata={'assistant_message_id': supplied_id})
    finish_old.set()
    await asyncio.wait_for(cleanup_finished.wait(), 3)
    await session.event_outbox.drain_delivery()
    new = captured['new']
    assert not new.done()
    assert session.run_manager.run_tasks[cid] is new
    assert session.run_manager.run_task_ids[cid] == 'new-run'
    assert not session.run_manager.is_delivery_complete(cid)
    stream = session._conversation_streams[cid]
    assert stream['message_id'] == 'assistant-new'
    assert stream['status'] == 'running'
    assert stream['terminal_fenced'] is False
    done = [p for p in session.ws.sent if p['type'] == 'done']
    assert len(done) == 1
    assert done[0]['message_id'] == captured['old_id']
    assert ' ' not in captured['old_id'] and '/' not in captured['old_id']
    assert not any(p['type'] == 'session.state_changed' and p['state'] == 'idle' for p in session.ws.sent)
    # A delayed old error/done cannot stop the new stream's subsequent output.
    delta = AgentEvent.agent_message_delta('new output')
    delta.data.update(conversation_id=cid, message_id='assistant-new')
    await session.send_event(delta)
    assert stream['content_blocks'][0]['content'] == 'new output'
    release_new.set()
    await new
    await drain_commands(session)
