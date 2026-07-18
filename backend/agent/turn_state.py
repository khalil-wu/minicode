from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


COMMAND_OUTPUT_PREVIEW_LIMIT = 60_000
TIMELINE_TEXT_SOURCES = {'model_preamble', 'post_tool', 'runtime'}
TEXT_METADATA_KEYS = {
    'source',
    'visibility',
    'role',
    'phase',
    'segmentId',
    'iterationIndex',
    'streamAttempt',
    'sealReason',
    'sealed',
    'promoteAllUnsealedNarration',
    'providerRaw',
    'finishReason',
}


def _int_or(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _is_timeline_text_block(block: dict[str, Any]) -> bool:
    return (
        block.get('visibility') in {'timeline', 'debug'}
        or block.get('role') == 'runtime'
        or str(block.get('phase') or '').strip().lower() == 'commentary'
        or block.get('source') in TIMELINE_TEXT_SOURCES
    )


def _is_answer_text_block(block: dict[str, Any]) -> bool:
    return block.get('type') == 'text' and not _is_timeline_text_block(block)


def _is_model_narration_text_block(block: dict[str, Any]) -> bool:
    return (
        block.get('type') == 'text'
        and block.get('source') == 'model_narration'
        and block.get('visibility') == 'timeline'
        and block.get('sealed') is not True
    )


def _has_metadata_value(value: Any) -> bool:
    return value is not None and value != ''


@dataclass(slots=True)
class AgentTurnSnapshot:
    content: str = ''
    blocks: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    run_failed_message: str = ''


class AgentTurnState:
    def __init__(self, *, now_ms: Callable[[], int]) -> None:
        self._now_ms = now_ms
        self._blocks: list[dict[str, Any]] = []
        self._citations: list[dict[str, Any]] = []
        self._usage: dict[str, int] = {}
        self._run_failed_message = ''

    def append_text(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        if not content:
            return
        metadata = {
            key: value
            for key, value in dict(metadata or {}).items()
            if key in TEXT_METADATA_KEYS and _has_metadata_value(value)
        }
        if self._blocks and self._blocks[-1].get('type') == 'text':
            previous_metadata = {
                key: self._blocks[-1].get(key)
                for key in TEXT_METADATA_KEYS
                if key in self._blocks[-1]
            }
            if previous_metadata == metadata:
                previous = str(self._blocks[-1].get('content') or '')
                self._blocks[-1]['content'] = previous + content
                return
        block = {'type': 'text', 'content': content}
        block.update(metadata)
        self._blocks.append(block)

    def finalize_text(self, metadata: dict[str, Any] | None = None) -> bool:
        """Seal the latest streamed answer text block as the final reply."""
        metadata = {
            key: value
            for key, value in dict(metadata or {}).items()
            if key in TEXT_METADATA_KEYS and _has_metadata_value(value)
        }
        metadata.setdefault('source', 'model_final')
        if not metadata.get('visibility'):
            metadata['visibility'] = 'final'
        if not metadata.get('phase'):
            metadata['phase'] = 'final'
        if metadata.get('visibility') == 'timeline' and metadata.get('source') == 'model_narration':
            target_segment_id = str(metadata.get('segmentId') or '').strip()
            for index in range(len(self._blocks) - 1, -1, -1):
                block = self._blocks[index]
                if not _is_model_narration_text_block(block):
                    continue
                if target_segment_id and str(block.get('segmentId') or '').strip() != target_segment_id:
                    continue
                next_block = dict(block)
                next_block.update(metadata)
                next_block['visibility'] = 'timeline'
                next_block['sealed'] = True
                next_block['isStreaming'] = False
                self._blocks[index] = next_block
                return True
            return False
        if (
            metadata.get('visibility') == 'final'
            and metadata.get('source') == 'model_narration'
            and metadata.get('promoteAllUnsealedNarration')
        ):
            narration_indexes = [
                index
                for index, block in enumerate(self._blocks)
                if _is_model_narration_text_block(block)
            ]
            if narration_indexes:
                first_index = narration_indexes[0]
                narration_content = ''.join(
                    str(self._blocks[index].get('content') or '')
                    for index in narration_indexes
                )
                first_block = dict(self._blocks[first_index])
                promoted = dict(first_block)
                promoted.update(metadata)
                promoted['content'] = narration_content
                promoted['visibility'] = 'final'
                promoted['phase'] = metadata.get('phase') or 'final'
                promoted['sealed'] = True
                promoted['isStreaming'] = False
                self._blocks = [
                    promoted if index == first_index else block
                    for index, block in enumerate(self._blocks)
                    if index not in narration_indexes[1:]
                ]
                return True
        for index in range(len(self._blocks) - 1, -1, -1):
            block = self._blocks[index]
            if block.get('type') != 'text':
                continue
            if _is_timeline_text_block(block):
                continue
            if not str(block.get('content') or '').strip():
                continue
            if block.get('sealed') is True or block.get('visibility') == 'final' or block.get('phase') == 'final':
                return True
            next_block = dict(block)
            next_block.update(metadata)
            self._blocks[index] = next_block
            return True
        return False

    def replace_text(self, content: str = '', metadata: dict[str, Any] | None = None) -> None:
        metadata = dict(metadata or {})
        if metadata.get('source') == 'model_narration':
            target_segment_id = str(metadata.get('segmentId') or '').strip()
            self._blocks = [
                block
                for block in self._blocks
                if not (
                    _is_model_narration_text_block(block)
                    and (
                        not target_segment_id
                        or str(block.get('segmentId') or '').strip() == target_segment_id
                    )
                )
            ]
            if content:
                block = {'type': 'text', 'content': content}
                block.update(metadata)
                self._blocks.append(block)
            return
        self._blocks = [
            block
            for block in self._blocks
            if block.get('type') != 'text' or not _is_answer_text_block(block)
        ]
        if content:
            self._blocks.append({'type': 'text', 'content': content})

    def append_thinking(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        if not content:
            return
        metadata = dict(metadata or {})
        if self._blocks and self._blocks[-1].get('type') == 'thinking':
            previous_metadata = {
                key: self._blocks[-1].get(key)
                for key in ('source', 'visibility', 'is_raw_provider_reasoning', 'provider_reasoning_type')
                if key in self._blocks[-1]
            }
            if previous_metadata == metadata:
                previous = str(self._blocks[-1].get('content') or '')
                self._blocks[-1]['content'] = previous + content
                return
        block: dict[str, Any] = {'type': 'thinking', 'content': content}
        block.update(metadata)
        self._blocks.append(block)

    def content(self) -> str:
        return ''.join(
            str(block.get('content') or '')
            for block in self._blocks
            if _is_answer_text_block(block)
        )

    def _tool_call_record_matches(self, record: dict[str, Any], incoming: dict[str, Any]) -> bool:
        if record.get('id') != incoming.get('id'):
            return False
        for incoming_key, record_key in (
            ('turnId', 'turnId'),
            ('iterationId', 'iterationId'),
            ('stepId', 'stepId'),
        ):
            incoming_value = str(incoming.get(incoming_key) or '').strip()
            record_value = str(record.get(record_key) or '').strip()
            if incoming_value and record_value and incoming_value != record_value:
                return False
        return True

    def _event_matches_tool_call_record(self, record: dict[str, Any], tool_id: str, data: dict[str, Any]) -> bool:
        if record.get('id') != tool_id:
            return False
        for source_key, record_key in (
            ('turn_id', 'turnId'),
            ('iteration_id', 'iterationId'),
            ('step_id', 'stepId'),
        ):
            event_value = str(data.get(source_key) or '').strip()
            record_value = str(record.get(record_key) or '').strip()
            if event_value and record_value and event_value != record_value:
                return False
        return True

    def _replace_tool_call_record(self, record: dict[str, Any]) -> None:
        for block in self._blocks:
            if block.get('type') == 'tool_call' and isinstance(block.get('record'), dict):
                if self._tool_call_record_matches(block['record'], record):
                    block['record'] = record
                    return
        self._blocks.append({'type': 'tool_call', 'record': record})

    def _find_tool_call_record(self, tool_id: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        data = data or {}
        for block in self._blocks:
            if block.get('type') == 'tool_call' and isinstance(block.get('record'), dict):
                if self._event_matches_tool_call_record(block['record'], tool_id, data):
                    return block['record']
        return None

    def _upsert_progress(self, progress: dict[str, Any]) -> None:
        progress_id = str(progress.get('id') or '').strip()
        if not progress_id:
            return
        for index, block in enumerate(self._blocks):
            if block.get('type') == 'progress' and block.get('id') == progress_id:
                self._blocks[index] = progress
                return
        self._blocks.append(progress)

    def tool_call_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for block in self._blocks:
            if block.get('type') == 'tool_call' and isinstance(block.get('record'), dict):
                records.append(dict(block['record']))
        return records

    def pending_tool_call_record(self, record: dict[str, Any]) -> dict[str, Any]:
        resume = {
            'id': record.get('id', ''),
            'name': record.get('name', ''),
            'args': record.get('args', {}),
            'status': record.get('status', 'running'),
            'started_at': record.get('startedAt', 0),
            'display_hint': record.get('displayHint', ''),
            'input_summary': record.get('inputSummary', ''),
            'result_kind': record.get('resultKind', ''),
            'activity_kind': record.get('activityKind', ''),
            'group_id': record.get('groupId', ''),
            'step_id': record.get('stepId', ''),
            'turn_id': record.get('turnId', ''),
            'iteration_id': record.get('iterationId', ''),
            'phase': record.get('phase', ''),
            'display_scope': record.get('displayScope', ''),
            'panel_hint': record.get('panelHint', ''),
            'requires_attention': bool(record.get('requiresAttention', False)),
        }
        return resume

    def record_tool_call(self, data: dict[str, Any]) -> dict[str, Any] | None:
        tool_id = str(data.get('id') or '').strip()
        tool_name = str(data.get('name') or '').strip()
        if not tool_id or not tool_name:
            return None
        tool_args = data.get('args') if isinstance(data.get('args'), dict) else {}
        record: dict[str, Any] = {
            'id': tool_id,
            'name': tool_name,
            'args': tool_args,
            'status': str(data.get('status') or 'running'),
            'startedAt': int(data.get('started_at') or self._now_ms()),
            'displayHint': str(data.get('display_hint') or ''),
            'inputSummary': str(data.get('input_summary') or ''),
        }
        for source_key, target_key in (
            ('result_kind', 'resultKind'),
            ('activity_kind', 'activityKind'),
            ('group_id', 'groupId'),
            ('step_id', 'stepId'),
            ('turn_id', 'turnId'),
            ('iteration_id', 'iterationId'),
            ('phase', 'phase'),
            ('display_scope', 'displayScope'),
            ('panel_hint', 'panelHint'),
        ):
            if data.get(source_key):
                record[target_key] = str(data.get(source_key) or '')
        if isinstance(data.get('requires_attention'), bool):
            record['requiresAttention'] = data['requires_attention']
        self._replace_tool_call_record(record)
        return record

    def record_progress(self, data: dict[str, Any]) -> dict[str, Any] | None:
        message = str(data.get('message') or '').strip()
        fallback_id = str(data.get('stage') or 'status') + ':' + message
        progress_id = str(data.get('id') or fallback_id).strip()
        if not progress_id or not message:
            return None
        progress: dict[str, Any] = {
            'type': 'progress',
            'id': progress_id,
            'stage': str(data.get('stage') or 'status'),
            'status': str(data.get('status') or 'info'),
            'message': message,
            'timestamp': self._now_ms(),
        }
        for source_key, target_key in (
            ('phase', 'phase'),
            ('label', 'label'),
            ('summary', 'summary'),
            ('visibility', 'visibility'),
            ('group_id', 'groupId'),
            ('step_id', 'stepId'),
        ):
            if data.get(source_key):
                progress[target_key] = str(data.get(source_key) or '')
        if data.get('detail'):
            progress['detail'] = str(data.get('detail') or '')
        if data.get('count') is not None:
            progress['count'] = int(data.get('count') or 0)
        if data.get('ephemeral'):
            progress['ephemeral'] = True
        # Ephemeral progress with the same group_id replaces any previous
        # ephemeral progress in that group (cc pattern: avoid message list
        # bloat from high-frequency sleep/bash progress ticks).
        if data.get('ephemeral') and data.get('group_id'):
            self._replace_ephemeral_progress(progress)
            return progress
        self._upsert_progress(progress)
        return progress

    def _replace_ephemeral_progress(self, progress: dict[str, Any]) -> None:
        """Replace the last ephemeral progress block in the same group."""
        group_id = str(progress.get('groupId') or '').strip()
        if not group_id:
            self._upsert_progress(progress)
            return
        # Walk blocks in reverse; replace the last ephemeral progress in
        # the same group, or fall back to upsert by id.
        for index in range(len(self._blocks) - 1, -1, -1):
            block = self._blocks[index]
            if (
                block.get('type') == 'progress'
                and block.get('groupId') == group_id
                and block.get('ephemeral')
            ):
                self._blocks[index] = progress
                return
        self._blocks.append(progress)

    def record_process_item(self, data: dict[str, Any]) -> dict[str, Any] | None:
        content = str(data.get('content') or data.get('summary') or '').strip()
        if not content or data.get('visibility') == 'debug':
            return None
        item_id = str(data.get('item_id') or data.get('id') or '').strip()
        if not item_id:
            return None
        process: dict[str, Any] = {
            'type': 'process',
            'id': item_id,
            'itemKind': str(data.get('kind') or 'process_text'),
            'content': content,
            'timestamp': _int_or(data.get('created_at'), self._now_ms()),
        }
        for source_key, target_key in (
            ('title', 'title'),
            ('summary', 'summary'),
            ('source', 'source'),
            ('status', 'status'),
            ('role', 'role'),
            ('visibility', 'visibility'),
            ('loop_id', 'loopId'),
            ('iteration_id', 'iterationId'),
            ('parent_id', 'parentId'),
            ('group_id', 'groupId'),
            ('step_id', 'stepId'),
            ('display_scope', 'displayScope'),
            ('panel_hint', 'panelHint'),
        ):
            if data.get(source_key):
                process[target_key] = str(data.get(source_key) or '')
        tool_call_ids = data.get('tool_call_ids')
        if isinstance(tool_call_ids, list):
            process['toolCallIds'] = [
                str(item)
                for item in tool_call_ids
                if str(item or '').strip()
            ]
        for source_key, target_key in (
            ('default_collapsed', 'defaultCollapsed'),
            ('requires_attention', 'requiresAttention'),
        ):
            if isinstance(data.get(source_key), bool):
                process[target_key] = data[source_key]
        for key in ('seq', 'order'):
            if data.get(key) is not None:
                process[key] = _int_or(data.get(key), 0)
        for source_key, target_key in (
            ('skill_name', 'skillName'),
            ('skillName', 'skillName'),
            ('trigger_mode', 'triggerMode'),
            ('triggerMode', 'triggerMode'),
            ('source_level', 'sourceLevel'),
            ('sourceLevel', 'sourceLevel'),
            ('reason', 'reason'),
        ):
            if data.get(source_key):
                process[target_key] = str(data.get(source_key) or '')
        token_estimate = data.get('token_estimate', data.get('tokenEstimate'))
        if token_estimate is not None:
            process['tokenEstimate'] = _int_or(token_estimate, 0)
        self._upsert_process(process)
        return process

    def _upsert_process(self, process: dict[str, Any]) -> None:
        process_id = str(process.get('id') or '').strip()
        if not process_id:
            return
        for index, block in enumerate(self._blocks):
            if block.get('type') == 'process' and block.get('id') == process_id:
                self._blocks[index] = process
                return
        self._blocks.append(process)

    def record_tool_output_delta(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get('id') or '').strip()
        output = str(data.get('output') or '')
        if not tool_id or not output:
            return
        existing_record = self._find_tool_call_record(tool_id, data)
        if existing_record is None:
            return
        updated_record = dict(existing_record)
        updated_record['outputPreview'] = _append_bounded_output(
            str(updated_record.get('outputPreview') or ''),
            output,
        )
        stream = str(data.get('stream') or 'stdout').strip().lower()
        preview_key = 'stderrPreview' if stream == 'stderr' else 'stdoutPreview'
        updated_record[preview_key] = _append_bounded_output(
            str(updated_record.get(preview_key) or ''),
            output,
        )
        self._replace_tool_call_record(updated_record)

    def record_tool_result(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get('id') or '').strip()
        if not tool_id:
            return
        existing_record = self._find_tool_call_record(tool_id, data)
        if existing_record is None:
            return
        updated_record = dict(existing_record)
        updated_record['status'] = str(data.get('status') or '') or (
            'failed' if bool(data.get('is_error')) else 'success'
        )
        updated_record['summary'] = str(data.get('summary') or '')
        if data.get('duration_ms') is not None:
            updated_record['durationMs'] = int(data.get('duration_ms') or 0)
        if data.get('artifact_id'):
            updated_record['artifactId'] = data.get('artifact_id')
        if data.get('diff') is not None:
            updated_record['diff'] = data.get('diff')
        for source_key, target_key in (
            ('display_summary', 'displaySummary'),
            ('result_kind', 'resultKind'),
            ('activity_kind', 'activityKind'),
            ('group_id', 'groupId'),
            ('step_id', 'stepId'),
            ('turn_id', 'turnId'),
            ('limitation', 'limitation'),
            ('provider', 'provider'),
            ('provider_error_type', 'providerErrorType'),
            ('error_kind', 'errorKind'),
            ('user_summary', 'userSummary'),
            ('developer_detail', 'developerDetail'),
            ('projection', 'projection'),
            ('iteration_id', 'iterationId'),
            ('phase', 'phase'),
            ('display_scope', 'displayScope'),
            ('panel_hint', 'panelHint'),
        ):
            if data.get(source_key):
                updated_record[target_key] = str(data.get(source_key) or '')
        if isinstance(data.get('error_info'), dict):
            updated_record['errorInfo'] = data.get('error_info')
        if isinstance(data.get('recoverable'), bool):
            updated_record['recoverable'] = data.get('recoverable')
        if isinstance(data.get('requires_attention'), bool):
            updated_record['requiresAttention'] = data.get('requires_attention')
        for source_key, target_key in (
            ('source_url', 'sourceUrl'),
            ('extraction_status', 'extractionStatus'),
            ('content_preview', 'contentPreview'),
            ('evidence_type', 'evidenceType'),
        ):
            if data.get(source_key):
                updated_record[target_key] = data.get(source_key)
        updated_record['finishedAt'] = self._now_ms()
        self._replace_tool_call_record(updated_record)

    def record_source_citation(self, data: dict[str, Any]) -> dict[str, Any] | None:
        if bool(data.get('is_error')):
            return None
        if str(data.get('evidence_type') or '').strip() != 'fetched':
            return None
        if str(data.get('extraction_status') or '').strip() == 'failed':
            return None
        source_url = str(data.get('source_url') or '').strip()
        if not source_url:
            return None
        if any(citation.get('source') == source_url for citation in self._citations):
            return None
        label = urlparse(source_url).netloc.lower().removeprefix('www.') or source_url
        citation = {
            'source': source_url,
            'url': source_url,
            'label': label,
            'title': label,
            'range': (0, 0),
        }
        self._citations.append(citation)
        return citation

    def record_done(self, data: dict[str, Any]) -> dict[str, int]:
        usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
        self._usage = dict(usage)
        return dict(self._usage)

    def record_error(self, data: dict[str, Any]) -> str:
        self._run_failed_message = (
            str(data.get('message') or '').strip()
            or str(data.get('error_type') or '').strip()
            or 'Agent run failed'
        )
        return self._run_failed_message

    def _terminalized_blocks(self, terminal_status: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for block in self._blocks:
            next_block = dict(block)
            if next_block.get('type') == 'progress' and next_block.get('status') == 'running':
                next_block['status'] = terminal_status
                next_block['timestamp'] = self._now_ms()
            elif next_block.get('type') == 'process' and next_block.get('status') == 'running':
                next_block['status'] = terminal_status
            elif next_block.get('type') == 'tool_call' and isinstance(next_block.get('record'), dict):
                record = dict(next_block['record'])
                if record.get('status') == 'running':
                    record['status'] = 'failed'
                    record.setdefault('error', 'Tool result was not received before the run ended.')
                    record.setdefault('terminationReason', 'missing_tool_result')
                    record['finishedAt'] = self._now_ms()
                next_block['record'] = record
            blocks.append(next_block)
        return blocks

    def finalize(self, *, terminal_status: str) -> AgentTurnSnapshot:
        blocks = self._terminalized_blocks(terminal_status)
        tool_calls = [
            dict(block['record'])
            for block in blocks
            if block.get('type') == 'tool_call' and isinstance(block.get('record'), dict)
        ]
        return AgentTurnSnapshot(
            content=self.content(),
            blocks=blocks,
            tool_calls=tool_calls,
            citations=[dict(citation) for citation in self._citations],
            usage=dict(self._usage),
            run_failed_message=self._run_failed_message,
        )


def _append_bounded_output(current: str, chunk: str) -> str:
    next_output = current + chunk
    if len(next_output) <= COMMAND_OUTPUT_PREVIEW_LIMIT:
        return next_output
    return (
        f'[output truncated: showing latest {COMMAND_OUTPUT_PREVIEW_LIMIT} chars]\n'
        + next_output[-COMMAND_OUTPUT_PREVIEW_LIMIT:]
    )
