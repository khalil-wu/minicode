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

    # MiniCode pages rollout reads in memory and never rewrites transcripts,
    # so scope the read to the endpoint's max window, compute sequence gaps
    # across ALL conversations in scope (a gap anywhere breaks reconnect
    # replay), then apply the conversation filter and only afterwards the
    # caller's limit — a per-conversation export returns the last N events OF
    # that conversation, never another conversation's share of the window.
    window = store.load(limit=MAX_REPLAY_LIMIT)
    read_status = store.read_status
    scoped = [
        event
        for event in window
        if _event_seq(event) is None or _event_seq(event) > clean_after_seq
    ]
    gaps = _durable_chain_gaps(window, after_seq=clean_after_seq)
    events = [
        event
        for event in scoped
        if not clean_conversation_id
        or str(event.get("conversation_id") or "") == clean_conversation_id
    ][-clean_limit:]
    current_seq = _current_seq(session, window)
    # first/last describe the returned events; gaps describe the whole scope.
    event_sequences = [
        seq for seq in (_event_seq(event) for event in events) if seq is not None
    ]
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
        "first_seq": min(event_sequences) if event_sequences else None,
        "last_seq": max(event_sequences) if event_sequences else None,
        "sequence_gaps": gaps,
        # A degraded read is not a clean log. Reporting can_replay_without_gap
        # over an unreadable or partially-dropped log claimed a guarantee the
        # store could not make; the store's read status is the evidence.
        "can_replay_without_gap": len(gaps) == 0 and not read_status.degraded,
        **read_status.to_payload(),
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


def _current_seq(session: Any | None, events: list[dict[str, Any]]) -> int:
    if session is not None:
        try:
            return max(0, int(getattr(session, "_ws_replay_cursor", 0) or 0))
        except (TypeError, ValueError):
            pass
    return max((_event_seq(event) or 0 for event in events), default=0)


def _event_seq(event: dict[str, Any]) -> int | None:
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    return seq if seq > 0 else None


def _durable_chain_gaps(
    events: list[dict[str, Any]],
    *,
    after_seq: int = 0,
) -> list[dict[str, int]]:
    """Report only unproven durable links, not transient wire-seq gaps."""

    ordered: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        seq = _event_seq(event)
        if seq is not None:
            ordered.append((seq, event))
    ordered.sort(key=lambda item: item[0])

    gaps: list[dict[str, int]] = []
    first_after_index = next(
        (index for index, (seq, _event) in enumerate(ordered) if seq > after_seq),
        None,
    )
    if first_after_index is None:
        return gaps

    expected_previous = after_seq
    for index in range(first_after_index, len(ordered)):
        seq, event = ordered[index]
        raw_previous = event.get("previous_replay_seq")
        if isinstance(raw_previous, bool):
            previous_replay_seq = None
        elif isinstance(raw_previous, int) and 0 <= raw_previous <= 9_007_199_254_740_991:
            previous_replay_seq = raw_previous
        else:
            previous_replay_seq = None

        if index == first_after_index and after_seq <= 0:
            # The export window may begin after retention. An explicit first
            # link defines its baseline; a legacy first record has no earlier
            # cursor claim to verify.
            expected_previous = (
                previous_replay_seq
                if previous_replay_seq is not None
                else seq
            )
        elif previous_replay_seq is None:
            # Adjacent legacy JSONL records are the durable order. Numeric gaps
            # can be transient envelopes and therefore are not evidence of loss.
            retained_previous = ordered[index - 1][0] if index > 0 else None
            if retained_previous != expected_previous:
                gaps.append(_chain_gap(expected_previous, seq))
        elif previous_replay_seq != expected_previous:
            gaps.append(_chain_gap(expected_previous, seq))
        expected_previous = seq
    return gaps


def _chain_gap(after_seq: int, before_seq: int) -> dict[str, int]:
    return {
        "after": max(0, after_seq),
        "before": max(0, before_seq),
        # Global wire ids may include transient events, so this is only a lower
        # bound for diagnostics rather than a count of missing durable records.
        "missing": max(1, before_seq - after_seq - 1),
    }


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    return dict(sorted(counts.items()))
