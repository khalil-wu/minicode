from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend.conversations.repository import CONVERSATION_DATA_DIR
from backend.ws.event_log import WebSocketReplayEventStore

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
DEFAULT_REPLAY_LIMIT = 500
MAX_REPLAY_LIMIT = 5_000


class ReplayExportError(ValueError):
    """Raised when a replay export request cannot be satisfied safely."""


def replay_log_root() -> Path:
    return Path(CONVERSATION_DATA_DIR).parent / "ws-event-log"


def replay_export_payload(
    *,
    session_id: str,
    limit: int = DEFAULT_REPLAY_LIMIT,
    conversation_id: str = "",
    after_seq: int = 0,
    ws_manager: Any | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    clean_session_id = _clean_session_id(session_id)
    clean_conversation_id = str(conversation_id or "").strip()
    clean_limit = max(1, min(MAX_REPLAY_LIMIT, int(limit or DEFAULT_REPLAY_LIMIT)))
    clean_after_seq = max(0, int(after_seq or 0))

    session = ws_manager.get_session(clean_session_id) if ws_manager is not None else None
    store = getattr(session, "_ws_event_store", None)
    if store is None:
        store = WebSocketReplayEventStore(
            session_id=clean_session_id,
            root_dir=root_dir or replay_log_root(),
        )

    events = [
        event for event in store.load(limit=clean_limit)
        if _matches_replay_filter(event, conversation_id=clean_conversation_id, after_seq=clean_after_seq)
    ]
    current_seq = _current_seq(session, events)
    sequences = [_event_seq(event) for event in events if _event_seq(event) is not None]
    gaps = _sequence_gaps(sequences, after_seq=clean_after_seq)
    type_counts = _event_type_counts(events)
    omitted_fields = sorted({
        str(field)
        for event in events
        for field in event.get("replay_omitted_fields", []) or []
    })
    truncated_fields = sorted({
        str(field)
        for event in events
        for field in event.get("replay_truncated_fields", []) or []
    })

    return {
        "kind": "minicode_ws_replay_export",
        "schema_version": 1,
        "session_id": clean_session_id,
        "conversation_id": clean_conversation_id or None,
        "after_seq": clean_after_seq,
        "current_seq": current_seq,
        "event_count": len(events),
        "first_seq": min(sequences) if sequences else None,
        "last_seq": max(sequences) if sequences else None,
        "sequence_gaps": gaps,
        "can_replay_without_gap": len(gaps) == 0,
        "type_counts": type_counts,
        "omitted_fields": omitted_fields,
        "truncated_fields": truncated_fields,
        "events": events,
    }


def _clean_session_id(session_id: str) -> str:
    clean = str(session_id or "").strip()
    if not clean or not SESSION_ID_RE.fullmatch(clean):
        raise ReplayExportError("Invalid session_id.")
    return clean


def _matches_replay_filter(event: dict[str, Any], *, conversation_id: str, after_seq: int) -> bool:
    if conversation_id and str(event.get("conversation_id") or "") != conversation_id:
        return False
    seq = _event_seq(event)
    if seq is not None and seq <= after_seq:
        return False
    return True


def _current_seq(session: Any | None, events: list[dict[str, Any]]) -> int:
    if session is not None:
        try:
            return max(0, int(getattr(session, "_ws_event_seq", 0) or 0))
        except (TypeError, ValueError):
            pass
    return max((_event_seq(event) or 0 for event in events), default=0)


def _event_seq(event: dict[str, Any]) -> int | None:
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    return seq if seq > 0 else None


def _sequence_gaps(sequences: list[int], *, after_seq: int = 0) -> list[dict[str, int]]:
    ordered = sorted(set(sequences))
    gaps: list[dict[str, int]] = []
    if after_seq > 0 and ordered and ordered[0] > after_seq + 1:
        gaps.append({"after": after_seq, "before": ordered[0], "missing": ordered[0] - after_seq - 1})
    for previous, current in zip(ordered, ordered[1:]):
        if current > previous + 1:
            gaps.append({"after": previous, "before": current, "missing": current - previous - 1})
    return gaps


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    return dict(sorted(counts.items()))
