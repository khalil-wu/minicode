"""Native planned/started facts survive canonical projection and a cold journal."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import time

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_serial
from backend.agent.tool_events import tool_call_pending_event
from backend.agent.turn_state import AgentTurnState
from backend.llm.base import ToolCallStartEvent
from backend.tests.test_native_exec_policy_chain import authorize, setup


def planned():
    return {"id": "actual-native-call", "name": "run_command", "args": {"command": 'Write-Output "actual"'},
        "status": "pending", "started_at": 1000, "streaming_announcement": False,
        "command_presentation": {"command": 'Write-Output "actual"', "cwd": "C:/actual", "started_at_ms": 1000},
        "command_actions": [{"type": "unknown", "command": 'Write-Output "actual"'}],
        "request_digest": "frozen-native-request", "turn_id": "actual-turn", "iteration_id": "actual-turn:1"}


def recorder(journal):
    return QueryJournalRecorder(journal, {"run_id": "actual-turn"}, AgentState(user_message='Native journal request'), ContextBuilder(), None, "actual-owner")


def test_canonical_pending_real_execution_clock_and_result_keep_exact_metadata():
    state = AgentTurnState(now_ms=lambda: 9000)
    event = planned()
    record = state.record_tool_call(event)
    assert record['streamingAnnouncement'] is False and 'executionStartedAt' not in record
    resume = state.pending_tool_call_record(record)
    assert resume['command_presentation'] == event['command_presentation']
    assert resume['command_actions'] == event['command_actions'] and resume['streaming_announcement'] is False
    assert state.record_tool_execution_started({'tool_call_id': event['id'], 'started_at': 0, 'turn_id': 'actual-turn'})
    # Re-delivering an earlier display item cannot erase the actual start fact.
    state.record_tool_call(event)
    state.record_tool_result({'id': event['id'], 'status': 'success', 'summary': 'actual output',
        'streaming_announcement': False, 'command_presentation': event['command_presentation'],
        'execution_started_at': 0, 'duration_ms': 1200, 'exit_code': 0,
        'stdout_preview': 'actual stdout', 'stderr_preview': '', 'command_actions': event['command_actions']})
    saved = state.finalize(terminal_status='completed').tool_calls[0]
    assert saved['commandPresentation'] == event['command_presentation'] and saved['executionStartedAt'] == 0
    assert saved['durationMs'] == 1200 and saved['exitCode'] == 0
    assert saved['stdoutPreview'] == 'actual stdout' and saved['stderrPreview'] == ''
    assert saved['commandActions'] == event['command_actions']
    assert state.record_tool_execution_started({'tool_call_id': event['id'], 'started_at': 8000}) is False
    assert state.finalize(terminal_status='completed').tool_calls[0] == saved


def test_query_recorder_keeps_announcement_raw_and_planned_request_without_claiming_execution(tmp_path):
    journal = ExecutionJournal('planned-native', base_dir=tmp_path)
    owner = recorder(journal)
    pending = {'id': 'actual-native-call', 'name': 'run_command', 'args': {}, 'status': 'pending',
        'streaming_announcement': True, 'started_at': 1}
    owner.record_event(AgentEvent('tool_call', pending))
    assert journal.unresolved_tool_uses() == [] and journal.reconstruct_history() == []
    raw = journal.read_events()[0]
    assert raw.payload['lifecycle'] == 'tool_announcement' and raw.payload['streaming_announcement'] is True
    data = planned()
    owner.record_event(AgentEvent('tool_call', data))
    owner.record_event(AgentEvent('tool_call', deepcopy(data)))
    cold = ExecutionJournal('planned-native', base_dir=tmp_path)
    uses = [event for event in cold.read_events() if event.event_type == 'tool_use']
    assert len(uses) == 1
    call = cold.reconstruct_history()[0]['tool_calls'][0]
    assert call['arguments'] == data['args'] and call['command_presentation'] == data['command_presentation']
    assert call['streaming_announcement'] is False and 'execution_started_at' not in call
    assert cold.unresolved_tool_uses()[0]['command_presentation'] == data['command_presentation']


def test_actual_started_event_advances_same_journal_call_once_and_survives_missing_result(tmp_path):
    journal = ExecutionJournal('started-native', base_dir=tmp_path)
    owner = recorder(journal)
    data = planned()
    owner.record_event(AgentEvent('tool_call', data))
    started = AgentEvent('runtime.span', {'event': 'tool.started', 'tool_call_id': data['id'], 'started_at': 2100,
        'turn_id': data['turn_id'], 'iteration_id': data['iteration_id']})
    owner.record_event(started)
    owner.record_event(started)
    cold = ExecutionJournal('started-native', base_dir=tmp_path)
    calls = [call for message in cold.reconstruct_history() for call in message.get('tool_calls', [])]
    assert len(calls) == 1 and calls[0]['execution_started_at'] == 2100
    assert calls[0]['command_presentation']['started_at_ms'] == 1000
    assert len([event for event in cold.read_events() if event.event_type == 'tool_use']) == 2
    cold.close_unresolved_tool_uses(reason='consumer_closed')
    result = next(event.payload for event in cold.read_events() if event.event_type == 'tool_result')
    assert result['execution_started_at'] == 2100 and result['command_presentation'] == data['command_presentation']
    assert result['command_actions'] == data['command_actions']
    assert result['synthetic'] is True and result['status'] == 'cancelled'
    assert 'duration_ms' not in result and 'exit_code' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('abort', [False, True])
async def test_real_native_producer_to_query_recorder_and_cold_canonical_record(tmp_path, abort):
    host = setup(tmp_path)
    host.context.cancel_event = asyncio.Event()
    call, auth = authorize(host, command='Write-Output "JOURNAL_NATIVE_ACTUAL"')
    state = AgentState(user_message='Real native journal task')
    state.ui_tool_started_at[call.id] = time.time() - 30
    builder = ContextBuilder()
    builder.bind_tool_context(host.context)
    journal = ExecutionJournal('native-producer', base_dir=tmp_path / 'journal')
    owner = QueryJournalRecorder(journal, {'run_id': 'source-turn'}, state, builder, None, 'source-owner')
    transcript = AgentTurnState(now_ms=lambda: int(time.time() * 1000))
    events = []

    def consume(event):
        events.append(event)
        owner.record_event(event)
        if event.type == 'tool_call':
            transcript.record_tool_call(event.data)
        elif event.type == 'tool_result':
            transcript.record_tool_result(event.data)
        elif event.type == 'runtime.span' and event.data.get('event') == 'tool.started':
            transcript.record_tool_execution_started(event.data)

    async def emit(kind, payload):
        consume(AgentEvent(kind, payload))

    async def decide(call_id):
        return {'action': 'reject' if abort else 'approve', 'decision': 'abort' if abort else 'approved',
            'request_digest': auth.request.digest}

    host.context.emit_event = emit
    consume(tool_call_pending_event(ToolCallStartEvent(id=call.id, name=call.name),
        started_at=int(state.ui_tool_started_at[call.id] * 1000), iteration_id='iter:1', tool_registry=host.registry))
    stream = execute_serial(call, perm=auth.permission_decision.permission_level,
        permission_decision=auth.permission_decision, permission_checker=host.checker,
        permission_context=host.context.permission, ctx=builder, state=state,
        tool_registry=host.registry, tool_ctx=host.context, approval_handler=decide, skill_manager=None, iteration_id='iter:1')
    async def execute():
        async for event in stream:
            consume(event)
    if abort:
        with pytest.raises(asyncio.CancelledError):
            await execute()
    else:
        await execute()
    result = next(event.data for event in events if event.type == 'tool_result')
    saved = transcript.finalize(terminal_status='cancelled' if abort else 'completed').tool_calls[0]
    cold = ExecutionJournal('native-producer', base_dir=tmp_path / 'journal')
    history = cold.reconstruct_history()
    calls = [item for message in history for item in message.get('tool_calls', [])]
    assert len(calls) == 1 and calls[0]['arguments']['command'] == auth.request.arguments['command']
    assert calls[0]['command_presentation'] == saved['commandPresentation'] == result['command_presentation']
    persisted = next(event.payload for event in cold.read_events() if event.event_type == 'tool_result')
    assert persisted['streaming_announcement'] is False
    assert persisted['command_presentation'] == result['command_presentation']
    if abort:
        assert saved['status'] == persisted['status'] == 'cancelled'
        assert not {'executionStartedAt', 'durationMs', 'exitCode'}.intersection(saved)
        assert not {'execution_started_at', 'duration_ms', 'exit_code'}.intersection(persisted)
    else:
        assert saved['exitCode'] == persisted['exit_code'] == 0
        assert saved['durationMs'] == persisted['duration_ms'] == result['duration_ms']
        assert saved['executionStartedAt'] == persisted['execution_started_at'] == result['execution_started_at']
        assert 'JOURNAL_NATIVE_ACTUAL' in saved['stdoutPreview']
        assert 'JOURNAL_NATIVE_ACTUAL' in persisted['stdout_preview']
    assert cold.unresolved_tool_uses() == []
