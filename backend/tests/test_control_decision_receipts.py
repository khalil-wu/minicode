from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.agent.message import UserCommand
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.event_outbox import EventOutbox
from backend.ws.handler import WebSocketSession
from backend.ws.turn_wait_state import TurnWaitState
from backend.ws.session_lifecycle import SessionLifecycle
from backend.conversations.repository import ConversationRepository


class ReceiptSocket:
    def __init__(self):
        self.sent = []
        self.inbound = asyncio.Queue()
        self.acks = asyncio.Queue()
        self.results = asyncio.Queue()
        self.decision_future = None

    async def receive_text(self):
        return await self.inbound.get()

    async def send_json(self, payload):
        self.sent.append(payload)
        if payload['type'] == 'client.command.ack':
            self.acks.put_nowait(payload)
        if payload['type'] == 'command.result':
            settled = self.decision_future is not None and self.decision_future.done()
            self.results.put_nowait((payload, settled))


@pytest_asyncio.fixture
async def session(tmp_path):
    session = WebSocketSession.__new__(WebSocketSession)
    session.session_id = 'control-receipts'
    session.conversation_runtime = SimpleNamespace(active_conversation_id='conv_receipts')
    session.conversation_repo = ConversationRepository(tmp_path / 'conversations')
    session.conversation_repo.create_conversation(conversation_id='conv_receipts', memory_mode='disabled')
    session.session_lifecycle = SessionLifecycle(session)
    session.config = SimpleNamespace(agent=SimpleNamespace(approval_timeout_seconds=None))
    session._extension_shutdown_requested = False
    session._conversation_streams = {}
    session.turn_wait_state = TurnWaitState()
    session.approval_diff_cache = {}
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / 'queue')
    session.run_manager = SimpleNamespace(durable_queue=queue, durable_client_commands=queue)
    session.event_outbox = EventOutbox(
        session_id=session.session_id, websocket=ReceiptSocket(),
        replay_root=tmp_path / 'events', replay_limit=1000, cleanup_tasks=set(),
        has_active_run=lambda: False, requires_conversation_owner=lambda *_args: False,
        workspace_scoped_event_types=(),
    )
    session.command_dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path)
    try:
        yield session
    finally:
        session.turn_wait_state.clear_pending_waiters()
        await session.event_outbox.drain_delivery()
        await session.event_outbox.drain_persistence()
        queue.close()


def register(session, *, lane='approval', early=False):
    session.turn_wait_state.pending_approval_payloads['request-1'] = {
        'type': 'control_request', 'request_id': 'request-1',
        'conversation_id': 'conv_receipts', 'turn_id': 'turn-1', 'message_id': 'message-1',
        'request': {'subtype': 'can_use_tool', 'tool_name': 'run_command',
                    'input': {'command': 'echo fixture'}, 'request_digest': 'digest-1'},
    }
    if early:
        return None
    future = asyncio.get_running_loop().create_future()
    session.turn_wait_state.register_waiter('request-1', future, kind=lane)
    session.ws.decision_future = future
    return future


def command(kind='control_response', *, action='approve', command_id='decision-1', **changes):
    data = {
        'request_id': 'request-1', 'conversation_id': 'conv_receipts',
        'turn_id': 'turn-1', 'message_id': 'message-1',
        'request_digest': 'digest-1', 'client_command_id': command_id,
    }
    if kind == 'control_response':
        data['response'] = {'subtype': 'success', 'response': {'action': action}}
    data.update(changes)
    if kind == 'control_response':
        data['response']['response']['request_digest'] = data.pop('request_digest')
    return UserCommand(kind, data)


async def handle(session, cmd):
    with session.event_outbox.bind_client_command(cmd.data['client_command_id'], cmd.type):
        assert await session.command_dispatcher._handle_command(cmd)
    return await asyncio.wait_for(session.ws.results.get(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', ['approval', 'user_input', 'elicitation', 'provider_oauth'])
@pytest.mark.parametrize('kind,action', [
    ('control_response', 'approve'), ('control_response', 'reject'),
    ('control_cancel_request', 'reject'),
])
async def test_control_receipt_follows_waiter_acceptance_and_has_both_command_ids(session, lane, kind, action):
    future = register(session, lane=lane)
    receipt, settled_at_delivery = await handle(session, command(kind, action=action))
    assert settled_at_delivery
    assert future.result()['action'] == action
    assert receipt['command'] == kind
    assert receipt['level'] == 'success'
    assert receipt['message'] == ''
    assert receipt['client_command_id'] == receipt['data']['client_command_id'] == 'decision-1'
    assert not any(event['type'] == 'tool_result' for event in session.ws.sent)
    # A different command cannot overwrite a decision before its consumer runs finally.
    duplicate, _ = await handle(session, command(kind, action=action, command_id='decision-2'))
    assert duplicate['level'] == 'error'
    assert future.result()['action'] == action


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['control_response', 'control_cancel_request'])
@pytest.mark.parametrize('changes', [
    {'conversation_id': 'another-conversation'}, {'turn_id': 'another-turn'},
    {'message_id': 'another-message'}, {'request_digest': 'another-digest'},
    {'request_id': 'missing'},
])
async def test_stale_or_wrong_owner_controls_return_correlated_error_without_settling(session, kind, changes):
    future = register(session)
    receipt, settled = await handle(session, command(kind, **changes))
    assert not settled
    assert not future.done()
    assert receipt['command'] == kind
    assert receipt['level'] == 'error'
    assert receipt['message']
    assert receipt['client_command_id'] == receipt['data']['client_command_id'] == 'decision-1'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['control_response', 'control_cancel_request'])
async def test_cancelled_waiter_is_not_mistaken_for_early_response(session, kind):
    future = register(session)
    future.cancel()
    receipt, _ = await handle(session, command(kind))
    assert receipt['level'] == 'error'
    assert session.turn_wait_state.pending_approval_responses == {}


@pytest.mark.asyncio
async def test_early_response_is_accepted_once_and_reaches_existing_approval_handler(session):
    register(session, early=True)
    receipt, settled = await handle(session, command())
    assert receipt['level'] == 'success'
    assert not settled  # Existing early-response handoff, not a claim of tool execution.
    duplicate, _ = await handle(session, command(action='reject', command_id='decision-2'))
    assert duplicate['level'] == 'error'
    assert (await session.approval_handler('request-1'))['action'] == 'approve'
    assert session.turn_wait_state.waiter_ids() == set()


@pytest.mark.asyncio
async def test_empty_control_response_returns_error(session):
    register(session)
    receipt, settled = await handle(session, UserCommand('control_response', {
        'request_id': 'request-1', 'client_command_id': 'decision-1',
        'response': {'subtype': 'success', 'response': {}},
    }))
    assert not settled
    assert receipt['level'] == 'error'
    assert receipt['message'] == 'Control response is empty'


@pytest.mark.asyncio
async def test_durable_ack_then_semantic_receipt_and_negative_ack_retry_close_the_loop(session, monkeypatch):
    future = register(session)
    queue = session.run_manager.durable_client_commands
    persist = queue.persist_client_command

    def fail_write(_command):
        raise OSError('injected durable write failure')

    monkeypatch.setattr(queue, 'persist_client_command', fail_write)
    cmd = command()
    raw = json.dumps({'type': cmd.type, **cmd.data})
    run = asyncio.create_task(session.command_dispatcher.run(session.connection_generation))
    try:
        session.ws.inbound.put_nowait(raw)
        rejected_ack = await asyncio.wait_for(session.ws.acks.get(), timeout=1)
        assert rejected_ack['accepted'] is False
        assert rejected_ack['reason'] == 'command.persistence'
        assert not future.done()
        assert session.ws.results.empty()
        assert not session.command_dispatcher.command_tasks

        monkeypatch.setattr(queue, 'persist_client_command', persist)
        session.ws.inbound.put_nowait(raw)
        admitted_ack = await asyncio.wait_for(session.ws.acks.get(), timeout=1)
        assert admitted_ack.get('accepted') is not False
        receipt, settled = await asyncio.wait_for(session.ws.results.get(), timeout=1)
        assert settled and future.result()['action'] == 'approve'
        assert receipt['level'] == 'success' and receipt['message'] == ''
        assert receipt['client_command_id'] == receipt['data']['client_command_id'] == 'decision-1'
        assert session.ws.sent.index(admitted_ack) < session.ws.sent.index(receipt)
        await asyncio.gather(*tuple(session.command_dispatcher.command_tasks))
        assert not queue.has_client_command('decision-1')
        assert 'decision-1' in session.command_dispatcher.recent_client_command_id_set
    finally:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
