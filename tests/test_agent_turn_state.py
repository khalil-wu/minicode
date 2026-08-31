from __future__ import annotations

from backend.agent.turn_state import AgentTurnState
from backend.ws.agent_runner import _project_agent_message_event


def test_turn_state_commits_only_completed_agent_message_items() -> None:
    state = AgentTurnState(now_ms=lambda: 100)
    state.start_agent_message('agent-message')
    state.append_agent_message_delta('agent-message', 'draft')
    state.append_agent_message_delta('agent-message', 'answer')
    assert state.content() == ''
    state.complete_agent_message({
        'id': 'agent-message',
        'type': 'agent_message',
        'text': 'draftanswerfinal',
        'source': 'model_final',
        'status': 'completed',
    })

    snapshot = state.finalize(terminal_status='completed')

    assert snapshot.content == 'draftanswerfinal'
    assert snapshot.blocks == [{
        'type': 'text',
        'itemId': 'agent-message',
        'content': 'draftanswerfinal',
        'source': 'model_final',
        'status': 'completed',
        'isStreaming': False,
    }]
    assert snapshot.tool_calls == []


def test_runner_projection_does_not_persist_provisional_agent_message_source() -> None:
    state = AgentTurnState(now_ms=lambda: 100)

    _project_agent_message_event(state, 'item.started', {
        'item': {
            'id': 'agent-message',
            'type': 'agent_message',
            'source': 'model_final',
        },
    })
    _project_agent_message_event(state, 'agent_message.delta', {
        'item_id': 'agent-message',
        'delta': 'I will inspect the files first.',
        'source': 'model_final',
    })

    assert state._blocks == [{
        'type': 'text',
        'itemId': 'agent-message',
        'content': 'I will inspect the files first.',
        'status': 'in_progress',
        'isStreaming': True,
    }]


def test_turn_state_completed_item_replaces_its_streamed_delta() -> None:
    state = AgentTurnState(now_ms=lambda: 100)
    state.start_agent_message('agent-message')
    state.append_agent_message_delta('agent-message', 'part one ')
    state.append_agent_message_delta('agent-message', 'part two')
    state.complete_agent_message({
        'id': 'agent-message',
        'type': 'agent_message',
        'text': 'part one part two',
        'source': 'partial',
        'status': 'partial',
    }, finish_reason='max_output_tokens')

    snapshot = state.finalize(terminal_status='completed')

    assert snapshot.content == 'part one part two'
    assert snapshot.blocks[0]['source'] == 'partial'
    assert snapshot.blocks[0]['status'] == 'partial'
    assert snapshot.blocks[0]['finishReason'] == 'max_output_tokens'


def test_turn_state_promotes_provider_document_locations_to_durable_citations() -> None:
    state = AgentTurnState(now_ms=lambda: 100)
    provider_raw = {
        'provider': 'anthropic',
        'citations': [
            {
                'source': 'anthropic:document:abc123',
                'title': 'Architecture notes',
                'label': 'Pages 2–3',
                'range': [2, 3],
                'location_type': 'page_location',
            },
            {
                'url': 'https://example.test/source',
                'title': 'Web source',
                'range': [0, 0],
            },
        ],
    }
    state.complete_agent_message({
        'id': 'agent-message',
        'type': 'agent_message',
        'text': 'Provider cited answer.',
        'source': 'model_final',
        'status': 'completed',
    }, provider_raw=provider_raw)

    snapshot = state.finalize(terminal_status='completed')

    assert snapshot.citations == [
        {
            'source': 'anthropic:document:abc123',
            'range': [2, 3],
            'providerNative': True,
            'title': 'Architecture notes',
            'label': 'Pages 2–3',
            'locationType': 'page_location',
        },
        {
            'source': 'https://example.test/source',
            'range': [0, 0],
            'providerNative': True,
            'url': 'https://example.test/source',
            'title': 'Web source',
        },
    ]
    assert snapshot.blocks[0]['providerRaw'] == provider_raw


def test_turn_state_excludes_completed_commentary_from_answer_content() -> None:
    state = AgentTurnState(now_ms=lambda: 100)
    state.complete_agent_message({
        'id': 'commentary-message',
        'type': 'agent_message',
        'text': '我先搜索今天的新闻。',
        'source': 'commentary',
        'status': 'completed',
    })
    state.complete_agent_message({
        'id': 'final-message',
        'type': 'agent_message',
        'text': '这是今天的新闻摘要。',
        'source': 'model_final',
        'status': 'completed',
    })

    snapshot = state.finalize(terminal_status='completed')

    assert snapshot.content == '这是今天的新闻摘要。'
    assert [block['source'] for block in snapshot.blocks] == ['commentary', 'model_final']


def test_turn_state_preserves_immutable_cancelled_and_completed_items() -> None:
    state = AgentTurnState(now_ms=lambda: 100)
    state.start_agent_message('iter-1:agent-message:1')
    state.append_agent_message_delta('iter-1:agent-message:1', 'bad draft')
    state.complete_agent_message({
        'id': 'iter-1:agent-message:1',
        'type': 'agent_message',
        'text': 'bad draft',
        'source': 'cancelled',
        'status': 'cancelled',
    })
    state.start_agent_message('iter-1:agent-message:2')
    state.append_agent_message_delta('iter-1:agent-message:2', 'better answer')
    state.complete_agent_message({
        'id': 'iter-1:agent-message:2',
        'type': 'agent_message',
        'text': 'better answer',
        'source': 'model_final',
        'status': 'completed',
    })
    snapshot = state.finalize(terminal_status='completed')

    assert snapshot.content == 'better answer'
    assert len(snapshot.blocks) == 2
    assert snapshot.blocks[0]['status'] == 'cancelled'
    assert snapshot.blocks[1]['content'] == 'better answer'


def test_turn_state_terminalizes_in_progress_item_as_partial_answer() -> None:
    state = AgentTurnState(now_ms=lambda: 100)
    state.start_agent_message('agent-message')
    state.append_agent_message_delta('agent-message', '等待中的半句话')

    snapshot = state.finalize(terminal_status='failed')

    assert snapshot.content == '等待中的半句话'
    assert snapshot.blocks[0]['content'] == '等待中的半句话'
    assert snapshot.blocks[0]['status'] == 'partial'
    assert snapshot.blocks[0]['source'] == 'partial'
    assert snapshot.blocks[0]['isStreaming'] is False


def test_turn_state_tracks_tool_records_outputs_results_and_citations() -> None:
    state = AgentTurnState(now_ms=lambda: 1234)
    record = state.record_tool_call({
        'id': 'tool_1',
        'name': 'web_fetch',
        'args': {'url': 'https://example.test/weather'},
    })

    assert record is not None
    assert state.pending_tool_call_record(record)['started_at'] == 1234
    state.record_tool_output_delta({'id': 'tool_1', 'output': 'partial output\n'})
    result_data = {
        'id': 'tool_1',
        'summary': 'Fetched weather',
        'duration_ms': 12,
        'artifact_id': 'art_1',
        'source_url': 'https://www.example.test/weather',
        'extraction_status': 'ok',
        'content_preview': 'Beijing 18C',
        'evidence_type': 'fetched',
    }
    state.record_tool_result(result_data)
    citation = state.record_source_citation(result_data)
    duplicate = state.record_source_citation(result_data)

    snapshot = state.finalize(terminal_status='completed')

    assert duplicate is None
    assert citation is not None
    assert citation['label'] == 'example.test'
    assert citation['range'] == [0, 0]
    assert snapshot.citations == [citation]
    tool_record = snapshot.tool_calls[0]
    assert tool_record['status'] == 'success'
    assert tool_record['summary'] == 'Fetched weather'
    assert tool_record['outputPreview'] == 'partial output\n'
    assert tool_record['durationMs'] == 12
    assert tool_record['artifactId'] == 'art_1'
    assert tool_record['sourceUrl'] == 'https://www.example.test/weather'
    assert tool_record['evidenceType'] == 'fetched'


def test_turn_state_tracks_stdout_and_stderr_previews_separately() -> None:
    state = AgentTurnState(now_ms=lambda: 2222)
    record = state.record_tool_call({
        'id': 'cmd_1',
        'name': 'run_command',
        'args': {'command': 'python train.py'},
    })

    assert record is not None
    state.record_tool_output_delta({'id': 'cmd_1', 'output': 'Epoch 1/10 - loss=0.92\n', 'stream': 'stdout'})
    state.record_tool_output_delta({'id': 'cmd_1', 'output': 'warning: slow dataloader\n', 'stream': 'stderr'})

    snapshot = state.finalize(terminal_status='completed')
    tool_record = snapshot.tool_calls[0]

    assert tool_record['outputPreview'] == 'Epoch 1/10 - loss=0.92\nwarning: slow dataloader\n'
    assert tool_record['stdoutPreview'] == 'Epoch 1/10 - loss=0.92\n'
    assert tool_record['stderrPreview'] == 'warning: slow dataloader\n'


def test_turn_state_persists_deliverables_and_hides_deleted_temporary_files() -> None:
    state = AgentTurnState(now_ms=lambda: 2222)
    state.record_tool_call({
        'id': 'write-helper',
        'name': 'write_file',
        'args': {'file_path': 'create_report.py'},
    })
    state.record_tool_result({
        'id': 'write-helper',
        'summary': 'Created helper',
        'diff': {'plus': 20, 'minus': 0, 'patch': '+print("report")'},
    })
    state.record_tool_call({
        'id': 'present-report',
        'name': 'present_file',
        'args': {'path': r'C:\Desktop\report.pdf'},
    })
    state.record_tool_result({
        'id': 'present-report',
        'summary': 'Presented report.pdf',
        'output_files': [{
            'path': r'C:\Desktop\report.pdf',
            'name': 'report.pdf',
            'size': 4096,
            'mime_type': 'application/pdf',
            'is_image': False,
        }],
        'superseded_tool_call_ids': ['write-helper'],
        'removed_file_paths': ['create_report.py'],
    })

    snapshot = state.finalize(terminal_status='completed')

    helper, deliverable = snapshot.tool_calls
    assert helper['temporaryRemoved'] is True
    assert 'diff' not in helper
    assert deliverable['outputFiles'] == [{
        'path': r'C:\Desktop\report.pdf',
        'name': 'report.pdf',
        'size': 4096,
        'mimeType': 'application/pdf',
        'isImage': False,
    }]


def test_turn_state_merges_thinking_only_when_metadata_matches() -> None:
    state = AgentTurnState(now_ms=lambda: 100)
    state.append_thinking('first ', {'source': 'model_preamble', 'visibility': 'timeline'})
    state.append_thinking('second', {'source': 'model_preamble', 'visibility': 'timeline'})
    state.append_thinking('raw', {'source': 'provider', 'is_raw_provider_reasoning': True})

    snapshot = state.finalize(terminal_status='completed')

    assert [block['type'] for block in snapshot.blocks] == ['thinking', 'thinking']
    assert snapshot.blocks[0]['content'] == 'first second'
    assert snapshot.blocks[1]['is_raw_provider_reasoning'] is True


def test_turn_state_persists_model_preamble_process_items() -> None:
    state = AgentTurnState(now_ms=lambda: 1000)

    state.record_process_item({
        'item_id': 'item-preamble',
        'kind': 'process_text',
        'content': '我先查实时天气，再核对来源。',
        'source': 'model_preamble',
        'visibility': 'timeline',
        'status': 'running',
        'created_at': 900,
    })
    state.complete_agent_message({
        'id': 'agent-message',
        'type': 'agent_message',
        'text': '北京今天有雷阵雨。',
        'source': 'model_final',
        'status': 'completed',
    })

    snapshot = state.finalize(terminal_status='completed')

    assert snapshot.blocks == [
        {
            'type': 'process',
            'id': 'item-preamble',
            'itemKind': 'process_text',
            'content': '我先查实时天气，再核对来源。',
            'timestamp': 900,
            'source': 'model_preamble',
            'status': 'completed',
            'visibility': 'timeline',
        },
        {
            'type': 'text',
            'itemId': 'agent-message',
            'content': '北京今天有雷阵雨。',
            'source': 'model_final',
            'status': 'completed',
            'isStreaming': False,
        },
    ]


def test_turn_state_marks_running_blocks_terminal_status() -> None:
    state = AgentTurnState(now_ms=lambda: 1000)
    state.record_progress({'id': 'plan-step', 'message': 'Choosing', 'status': 'running'})
    state.record_tool_call({'id': 'tool_2', 'name': 'run_command', 'args': {'command': 'pytest'}})

    snapshot = state.finalize(terminal_status='failed')

    progress, tool = snapshot.blocks
    assert progress['status'] == 'failed'
    assert tool['record']['status'] == 'failed'
    assert tool['record']['finishedAt'] == 1000


def test_turn_state_drops_debug_progress_from_durable_blocks() -> None:
    state = AgentTurnState(now_ms=lambda: 1000)
    state.record_progress({
        'id': 'provider:request-1',
        'stage': 'status',
        'status': 'running',
        'message': '模型正在响应',
        'provider_state': 'responding',
    })

    assert state.record_progress({
        'id': 'provider:request-1',
        'stage': 'status',
        'status': 'completed',
        'message': '提供商响应完成',
        'provider_state': 'completed',
        'visibility': 'debug',
    }) is None
    assert state.finalize(terminal_status='completed').blocks == []


def test_turn_state_records_done_usage_and_error_message() -> None:
    state = AgentTurnState(now_ms=lambda: 100)

    usage = state.record_done({'usage': {'input_tokens': 3, 'output_tokens': 5}})
    error_message = state.record_error({'message': 'model failed', 'error_type': 'api'})
    snapshot = state.finalize(terminal_status='failed')

    assert usage == {'input_tokens': 3, 'output_tokens': 5}
    assert error_message == 'model failed'
    assert snapshot.usage == {'input_tokens': 3, 'output_tokens': 5}
    assert snapshot.run_failed_message == 'model failed'
