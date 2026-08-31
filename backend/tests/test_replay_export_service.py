from __future__ import annotations

import pytest

from backend.services.replay_service import ReplayExportError, replay_export_payload
from backend.ws.event_log import WebSocketReplayEventStore


def test_replay_export_payload_filters_and_reports_gaps(tmp_path) -> None:
    store = WebSocketReplayEventStore(session_id="session-replay-export", root_dir=tmp_path)
    store.append({
        "type": "agent_message.delta",
        "seq": 1,
        "conversation_id": "conv-a",
        "item_id": "agent-message",
        "delta": "hello",
    })
    store.append({
        "type": "image_chunk",
        "seq": 3,
        "conversation_id": "conv-a",
        "image_data": "data:image/png;base64,AA==",
    })
    store.append({
        "type": "done",
        "seq": 4,
        "conversation_id": "conv-b",
    })

    payload = replay_export_payload(
        session_id="session-replay-export",
        root_dir=tmp_path,
        conversation_id="conv-a",
        after_seq=1,
    )

    assert payload["kind"] == "minicode_ws_replay_export"
    assert payload["schema_version"] == 1
    assert payload["event_count"] == 1
    assert payload["first_seq"] == 3
    assert payload["last_seq"] == 3
    assert payload["current_seq"] == 4
    assert payload["can_replay_without_gap"] is True
    assert payload["sequence_gaps"] == []
    assert payload["type_counts"] == {"image_chunk": 1}
    assert payload["omitted_fields"] == ["image_data"]
    assert payload["events"][0]["image_data_omitted"] is True
    assert "image_data" not in payload["events"][0]


def test_replay_export_payload_rejects_invalid_session_id(tmp_path) -> None:
    with pytest.raises(ReplayExportError):
        replay_export_payload(session_id="../bad", root_dir=tmp_path)


def test_replay_export_is_read_only(tmp_path) -> None:
    """Exporting must never truncate the on-disk log (all three refs are append-only)."""

    store = WebSocketReplayEventStore(session_id="session-read-only", root_dir=tmp_path)
    for seq in range(1, 6):
        store.append({
            "type": "agent_message.delta",
            "seq": seq,
            "conversation_id": "conv-a",
            "delta": "x",
        })

    replay_export_payload(session_id="session-read-only", root_dir=tmp_path, limit=1)

    assert [event["seq"] for event in store.load(limit=100)] == [1, 2, 3, 4, 5]


def test_replay_export_limit_applies_after_conversation_filter(tmp_path) -> None:
    store = WebSocketReplayEventStore(session_id="session-scoped-limit", root_dir=tmp_path)
    for seq in range(1, 9):
        store.append({
            "type": "agent_message.delta",
            "seq": seq,
            "conversation_id": "conv-a" if seq % 2 else "conv-b",
            "delta": "x",
        })

    payload = replay_export_payload(
        session_id="session-scoped-limit",
        root_dir=tmp_path,
        conversation_id="conv-a",
        limit=2,
    )

    assert [event["seq"] for event in payload["events"]] == [5, 7]


def test_replay_export_does_not_treat_transient_wire_seq_gaps_as_data_loss(tmp_path) -> None:
    store = WebSocketReplayEventStore(session_id="session-gap-scope", root_dir=tmp_path)
    store.append({"type": "state", "seq": 1, "conversation_id": "conv-a"})
    store.append({"type": "state", "seq": 3, "conversation_id": "conv-b"})

    payload = replay_export_payload(
        session_id="session-gap-scope",
        root_dir=tmp_path,
        conversation_id="conv-a",
    )

    assert payload["events"][0]["seq"] == 1
    assert payload["can_replay_without_gap"] is True
    assert payload["sequence_gaps"] == []


def test_replay_export_reports_an_explicit_durable_chain_break_across_conversations(tmp_path) -> None:
    store = WebSocketReplayEventStore(session_id="session-explicit-gap", root_dir=tmp_path)
    store.append({
        "type": "state",
        "seq": 1,
        "previous_replay_seq": 0,
        "conversation_id": "conv-a",
    })
    store.append({
        "type": "state",
        "seq": 3,
        "previous_replay_seq": 2,
        "conversation_id": "conv-b",
    })

    payload = replay_export_payload(
        session_id="session-explicit-gap",
        root_dir=tmp_path,
        conversation_id="conv-a",
    )

    assert payload["can_replay_without_gap"] is False
    assert payload["sequence_gaps"] == [{"after": 1, "before": 3, "missing": 1}]
