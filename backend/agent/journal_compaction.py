"""Compact oversized execution journals in place.

Earlier journals persisted the whole assistant message and context snapshot
with every partial projection.  Once such a projection carries its commit
receipt and a newer projection exists for the same message, nothing reads that
bulk again: commit replay only consumes projections without receipts, history
and extension state rebuild from the newest snapshot, and delta decoding only
follows the bases it names.  Rewriting those records as stubs keeps every
sequence number, event id and receipt while shrinking the file by orders of
magnitude, so opening the journal stops paying for state that is already
durable in the conversation store.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_bytes

logger = logging.getLogger(__name__)

COMPACT_MIN_BYTES = 4 * 1024 * 1024
# Current projections stay around a couple of kilobytes; the legacy records
# worth rewriting start near 100 KB.  A journal without such lines is left alone
# after a plain byte scan.
BULKY_LINE_BYTES = 64 * 1024
PENDING = "conversation_projection_pending"
COMMITTED = "conversation_projection_committed"
DELTA = "conversation_projection_delta"
BULK_FIELDS = ("assistant_message", "context_snapshot", "context_delta")


def compact_journal_file(path: Path, *, min_bytes: int = COMPACT_MIN_BYTES) -> int:
    """Stub superseded, committed projections and return how many records changed.

    The caller holds the journal's locks.  Any irregular record leaves the file
    untouched so the journal's own validation can report it.
    """
    try:
        if path.stat().st_size < min_bytes:
            return 0
        raw = path.read_bytes()
    except OSError:
        return 0
    lines = raw.split(b"\n")
    if not any(len(line) >= BULKY_LINE_BYTES for line in lines):
        return 0
    try:
        records = _parse(lines)
    except ValueError as exc:
        logger.warning("Skipping compaction of %s: %s", path, exc)
        return 0
    targets = _superseded_committed_projections(records)
    if not targets:
        return 0
    output = bytearray()
    for index, (line, data) in enumerate(records):
        if index in targets:
            data["payload"] = _stub(data["payload"])
            line = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        output += line + b"\n"
    atomic_write_bytes(path, bytes(output))
    logger.info(
        "Compacted %d superseded projections in %s (%d -> %d bytes)",
        len(targets), path, len(raw), len(output),
    )
    return len(targets)


def _parse(lines: list[bytes]) -> list[tuple[bytes, dict[str, Any]]]:
    records: list[tuple[bytes, dict[str, Any]]] = []
    seen: set[str] = set()
    owner: str | None = None
    for line in lines:
        if not line.strip():
            continue
        data = json.loads(line)
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            raise ValueError("record is not an event object")
        event_id = str(data.get("event_id") or "")
        if not event_id or event_id in seen:
            raise ValueError("duplicate or missing event id")
        seen.add(event_id)
        try:
            seq = int(data.get("seq") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid sequence") from exc
        if seq != len(records) + 1:
            raise ValueError("sequence is not contiguous")
        agent_id = str(data.get("agent_id") or "")
        if owner is None:
            owner = agent_id
        elif agent_id != owner:
            raise ValueError("mixed journal owners")
        records.append((line, data))
    return records


def _superseded_committed_projections(records: list[tuple[bytes, dict[str, Any]]]) -> set[int]:
    receipts: set[str] = set()
    bases: set[str] = set()
    latest_pending: dict[tuple[str, str], int] = {}
    latest_snapshot: dict[str, int] = {}
    latest_run_snapshot: dict[tuple[str, str], int] = {}
    for index, (_, data) in enumerate(records):
        payload = data["payload"]
        lifecycle = payload.get("lifecycle")
        owner = str(payload.get("conversation_id") or "")
        if lifecycle == COMMITTED:
            receipts.add(str(payload.get("pending_event_id") or ""))
        elif lifecycle == DELTA:
            bases.add(str(payload.get("base_event_id") or ""))
        elif lifecycle == PENDING:
            latest_pending[(owner, _message_id(payload))] = index
        change = payload.get("context_snapshot_change")
        if isinstance(change, dict):
            bases.add(str(change.get("base_event_id") or ""))
        if isinstance(payload.get("context_snapshot"), dict):
            latest_snapshot[owner] = index
            latest_run_snapshot[(owner, str(payload.get("run_id") or ""))] = index
    protected = set(latest_pending.values()) | set(latest_snapshot.values()) | set(latest_run_snapshot.values())
    targets: set[int] = set()
    for index, (_, data) in enumerate(records):
        payload = data["payload"]
        if payload.get("lifecycle") != PENDING or payload.get("compacted") or index in protected:
            continue
        event_id = str(data.get("event_id") or "")
        if event_id in bases or event_id not in receipts or not _message_id(payload):
            continue
        if any(field in payload for field in BULK_FIELDS):
            targets.add(index)
    return targets


def _message_id(payload: dict[str, Any]) -> str:
    message = payload.get("assistant_message")
    return str(message.get("id") or "") if isinstance(message, dict) else ""


def _stub(payload: dict[str, Any]) -> dict[str, Any]:
    stub = {key: value for key, value in payload.items() if key not in BULK_FIELDS}
    stub["assistant_message"] = {"id": _message_id(payload)}
    stub["compacted"] = True
    return stub
