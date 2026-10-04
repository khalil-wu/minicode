from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import AgentEvent, UserCommand
from backend.conversations.projection_log import append_projection, read_projection
from backend.conversations.repository import ConversationRepository, ConversationStorageCorruptError
from backend.conversations.transcript_index import TranscriptIndexError, encode_transcript, read_message, read_page
from backend.ws.client_command_log import ClientCommandDedupStore
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.payload_contracts import validate_session_projection_payload
from backend.ws.reasoning_batcher import ReasoningEventBatcher, ReasoningFlushDeadline
from backend.ws.stream_state import apply_stream_event, create_stream_state, get_stream_content_blocks
from backend.ws.turn_wait_state import TurnWaitState


def test_command_log_compaction_preserves_original_age_and_distinct_ids(tmp_path, monkeypatch):
    store = ClientCommandDedupStore(session_id="fixture", root_dir=tmp_path)
    store.path.write_text("\n".join(json.dumps({"client_command_id": item, "created_at": 900}) for item in ["older", "repeat", "repeat"]) + "\n", encoding="utf-8")
    monkeypatch.setattr("backend.ws.client_command_log.time.time", lambda: 1000)
    assert store.load_ids(limit=2, max_age_seconds=200) == ["older", "repeat"]
    store.rewrite_ids(["repeat"])
    assert json.loads(store.path.read_text(encoding="utf-8"))["created_at"] == 900
    monkeypatch.setattr("backend.ws.client_command_log.time.time", lambda: 1110)
    assert store.load_ids(limit=2, max_age_seconds=200) == []


@pytest.mark.asyncio
async def test_invalid_utf8_completion_log_retains_ids_reports_evidence_and_suppresses_duplicate(tmp_path, monkeypatch):
    store = ClientCommandDedupStore(session_id="fixture", root_dir=tmp_path)
    raw = b'{"client_command_id":"complete","created_at":1000}\n{"client_command_id":"partial","command_type":"bad\xff"}\n'
    store.path.write_bytes(raw)
    monkeypatch.setattr("backend.ws.client_command_log.time.time", lambda: 1000)
    session = SimpleNamespace(session_id="fixture", emit_command_result=AsyncMock(), run_manager=SimpleNamespace(durable_client_commands=None))
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path)
    assert dispatcher.recent_client_command_ids == ["complete", "partial"]
    assert dispatcher._client_command_seen(UserCommand(type="user_message", data={"client_command_id": "complete"}))
    await dispatcher._replay_pending_client_commands(1)
    data = session.emit_command_result.await_args.kwargs["data"]
    assert data["reason"] == "invalid_utf8" and data["line"] == "2" and data["path"] == str(store.path)
    await dispatcher._replay_pending_client_commands(2)
    assert session.emit_command_result.await_count == 1
    dispatcher.client_command_store.rewrite_ids(["complete"])
    assert store.path.read_bytes() == raw


def test_append_after_torn_tail_preserves_both_completion_identities(tmp_path, monkeypatch):
    store = ClientCommandDedupStore(session_id="fixture", root_dir=tmp_path)
    raw = b'{"client_command_id":"old"'
    store.path.write_bytes(raw)
    monkeypatch.setattr("backend.ws.client_command_log.time.time", lambda: 1000)
    store.append("fresh")
    assert store.path.read_bytes().startswith(raw + b"\n")
    assert store.load_ids(limit=10) == ["old", "fresh"]
    assert store.last_load_error["reason"] == "malformed_json"


def test_indexed_utf8_messages_page_and_reject_declared_byte_truncation(tmp_path):
    messages = [{"id": "u", "role": "user", "content": "中文🌟"}, {"id": "a", "role": "assistant", "content": "回答"}]
    text, index = encode_transcript(messages)
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(text.encode("utf-8"))
    assert read_message(path, index, "a") == messages[1]
    assert read_page(path, index, limit=1)["transcript"] == messages
    path.write_bytes(text[:-1].encode("utf-8"))
    with pytest.raises(TranscriptIndexError, match="truncated"):
        read_message(path, index, "a")
    with pytest.raises(TranscriptIndexError, match="truncated"):
        read_page(path, index, limit=1)


def test_indexed_generation_requires_utf8_instead_of_autodetected_utf16(tmp_path):
    message = {"id": "a", "role": "assistant", "content": "answer"}
    raw = json.dumps(message).encode("utf-16")
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(raw)
    with pytest.raises(UnicodeDecodeError):
        read_message(path, [["a", "assistant", 0, len(raw)]], "a")


def test_terminal_tool_metadata_and_preview_survive_late_events_and_done_fence():
    state = create_stream_state("conv", "message", "run")
    streams = {"conv": state}
    apply_stream_event(streams, "conv", "tool_result", {"id": "call", "name": "fixture", "status": "success", "summary": "Authoritative output", "artifact_id": "art-owner", "content_preview": "preview"})
    terminal = deepcopy(state["tool_calls"]["call"])
    for event_type, data in [("runtime.span", {"tool_call_id": "call", "event": "tool.started", "summary": "Late running"}), ("agent.progress", {"tool_call_id": "call", "summary": "Late waiting", "status": "running"}), ("tool_output_delta", {"id": "call", "output": "late bytes"})]:
        apply_stream_event(streams, "conv", event_type, data)
        assert state["tool_calls"]["call"] == terminal
    blocks = get_stream_content_blocks(state)
    assert blocks[0]["record"]["summary"] == "Authoritative output"
    apply_stream_event(streams, "conv", "done", {"message_id": "message", "turn_id": "run", "status": "completed"})
    fenced = deepcopy(state)
    apply_stream_event(streams, "conv", "agent_message.delta", {"message_id": "message", "turn_id": "run", "item_id": "answer", "delta": "late"})
    assert state == fenced


def test_projection_committed_bytes_preserve_unicode_and_ignore_uncommitted_suffix(tmp_path):
    path = tmp_path / "projection.jsonl"
    first = {"assistant_message": {"id": "a", "content": "中文"}, "context_delta": {}}
    pos = append_projection(path, committed_bytes=0, revision=1, previous={}, current=first)
    with path.open("ab") as stream:
        stream.write(b'uncommitted-partial')
    assert read_projection(path, committed_bytes=pos, revision=1) == first
    second = {"assistant_message": {"id": "a", "content": "中文🌟"}, "context_delta": {}}
    pos = append_projection(path, committed_bytes=pos, revision=2, previous=first, current=second)
    assert read_projection(path, committed_bytes=pos, revision=2) == second
    with pytest.raises(ValueError, match="manifest"):
        read_projection(path, committed_bytes=pos, revision=3)


@pytest.mark.asyncio
async def test_reasoning_close_and_waiter_cleanup_settle_owned_tasks_once():
    batcher = ReasoningEventBatcher(max_chars=100, max_delay_seconds=1)
    first = AgentEvent(type="thinking_delta", data={"conversation_id": "conv", "message_id": "message", "content": "first"})
    assert batcher.push(first) == [first]
    assert batcher.push(AgentEvent(type="thinking_delta", data={**first.data, "content": "second"})) == []
    called = []

    async def flush():
        called.append(batcher.flush_if_pending())

    deadline = ReasoningFlushDeadline(0, flush)
    deadline.arm()
    await deadline.close()
    assert called == [] and not deadline.armed
    assert batcher.flush().data["content"] == "second"
    state = TurnWaitState()
    futures = [asyncio.get_running_loop().create_future() for _ in range(4)]
    for index, kind in enumerate(["approval", "user_input", "elicitation", "provider_oauth"]):
        state.register_waiter(str(index), futures[index], kind=kind)
    state.clear_pending_waiters()
    assert all(future.cancelled() for future in futures) and state.waiter_ids() == set()


def test_recovery_wire_contract_keeps_durable_chain_and_rejects_false_advancement():
    payload = {"type": "session.replay", "last_seq": 3, "current_seq": 8, "replayed_events": 1, "events": [{"type": "tool_result", "seq": 8, "previous_replay_seq": 3}]}
    validate_session_projection_payload(payload)
    with pytest.raises(ValueError, match="chain"):
        validate_session_projection_payload({**payload, "events": [{"type": "tool_result", "seq": 8, "previous_replay_seq": 4}]})


@pytest.mark.parametrize("version", [True, 1.5, "1"])
def test_manifest_format_version_does_not_coerce_other_types(tmp_path, version):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(title="Version fixture")
    path = repo._manifest_path_for(record.id)
    original = json.loads(path.read_text(encoding="utf-8"))
    content = json.dumps({**original, "version": version})
    path.write_text(content, encoding="utf-8")
    repo._manifest_cache.clear()
    with pytest.raises(ConversationStorageCorruptError, match="unsupported manifest version"):
        repo.get_conversation_view(record.id)
    assert path.read_text(encoding="utf-8") == content
