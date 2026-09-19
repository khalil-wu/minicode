"""Durable per-agent execution evidence journal.

The journal records ordered execution facts for inspection and cleanup. It is
not a substitute for the canonical context checkpoint used to resume a turn.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import threading
from collections.abc import Mapping
from copy import deepcopy
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from backend.agent.checkpoint import validate_storage_id
from backend.agent.runtime_records import epoch_ms
from backend.atomic_io import canonical_file_path_key
from backend.config import DATA_ROOT
from backend.conversations.projection_log import value_change, apply_value_change
from filelock import FileLock

logger = logging.getLogger(__name__)


def _tail_lines(path: Path, *, limit: int = 8) -> list[str]:
    """Return up to ``limit`` trailing lines, newest last, without a full read."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        offset = handle.tell()
        if offset == 0:
            return []
        buffer = b""
        while offset > 0 and buffer.count(b"\n") <= limit:
            step = min(4096, offset)
            offset -= step
            handle.seek(offset)
            buffer = handle.read(step) + buffer
    text = buffer.decode("utf-8", errors="replace")
    return text.split("\n")[-(limit + 1):]


JOURNAL_ROOT = DATA_ROOT / "sidechains"
_JOURNAL_SCHEMA_VERSION = 7
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_SEQUENCES: dict[str, int] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

EVENT_TYPES = frozenset({
    "user_prompt",
    "assistant",
    "tool_use",
    "tool_result",
    "progress",
    "system",
    "cleanup",
    "terminal",
})


class ExecutionJournalError(RuntimeError):
    """Base error for durable execution-journal operations."""


class ExecutionJournalCorruptionError(ExecutionJournalError):
    """Raised when append-only journal evidence is incomplete or invalid."""


_TOOL_USE_OPTIONAL_FIELDS = (
    "call_source",
    "status",
    "started_at",
    "display_hint",
    "input_summary",
    "result_kind",
    "activity_kind",
    "visibility",
    "group_id",
    "step_id",
    "task_id",
    "turn_id",
    "iteration_id",
    "phase",
    "side_effect_kind",
    "idempotent",
    "idempotency_key",
    "request_digest",
    "announcement_only",
    "arguments_complete",
)

_TOOL_RESULT_OPTIONAL_FIELDS = (
    "call_source",
    "artifact_id",
    "artifact_kind",
    "artifact_media_type",
    "artifact_bytes",
    "is_error",
    "diff",
    "source_url",
    "extraction_status",
    "content_preview",
    "evidence_type",
    "duration_ms",
    "display_summary",
    "result_kind",
    "activity_kind",
    "visibility",
    "group_id",
    "step_id",
    "task_id",
    "turn_id",
    "iteration_id",
    "phase",
    "limitation",
    "provider",
    "provider_error_type",
    "error_info",
    "error_kind",
    "user_summary",
    "developer_detail",
    "recoverable",
    "projection",
    "side_effect_kind",
    "idempotent",
    "idempotency_key",
    "cleanup_receipt",
    "output_files",
    "superseded_tool_call_ids",
    "removed_file_paths",
    "request_digest",
    "termination_reason",
    "synthetic",
)


def _required_tool_event_text(
    value: Any,
    *,
    field_name: str,
) -> str:
    text = str(value or "").strip()
    if not text:
        raise ExecutionJournalError(f"{field_name} is required for journal projection")
    return text


def tool_use_journal_payload(event_data: Mapping[str, Any]) -> dict[str, Any]:
    """Project one canonical ``tool_call`` event into durable journal shape."""

    call_id = _required_tool_event_text(
        event_data.get("id") or event_data.get("tool_call_id"),
        field_name="tool_call_id",
    )
    tool_name = _required_tool_event_text(
        event_data.get("name") or event_data.get("tool_name"),
        field_name="tool_name",
    )
    raw_arguments = (
        event_data.get("args")
        if "args" in event_data
        else event_data.get("arguments")
        if "arguments" in event_data
        else event_data.get("input")
    )
    if raw_arguments is None:
        raw_arguments = {}
    if not isinstance(raw_arguments, Mapping):
        raise ExecutionJournalError("tool arguments must be an object for journal projection")

    tool_call: dict[str, Any] = {
        "id": call_id,
        "name": tool_name,
        "arguments": deepcopy(dict(raw_arguments)),
    }
    for field_name in _TOOL_USE_OPTIONAL_FIELDS:
        if field_name in event_data and event_data[field_name] is not None:
            tool_call[field_name] = deepcopy(event_data[field_name])
    return {
        "tool_call": tool_call,
        "lifecycle": "tool_claimed",
    }


def tool_result_journal_payload(
    event_data: Mapping[str, Any],
    *,
    tool_name: str,
) -> dict[str, Any]:
    """Project one canonical ``tool_result`` event without losing UI evidence."""

    call_id = _required_tool_event_text(
        event_data.get("id") or event_data.get("tool_call_id"),
        field_name="tool_call_id",
    )
    resolved_tool_name = _required_tool_event_text(
        tool_name or event_data.get("name") or event_data.get("tool_name"),
        field_name="tool_name",
    )
    content = str(
        event_data.get("content")
        or event_data.get("output")
        or event_data.get("summary")
        or ""
    )
    payload: dict[str, Any] = {
        "tool_call_id": call_id,
        "tool_name": resolved_tool_name,
        "content": content,
        "status": str(
            event_data.get("status")
            or ("failed" if bool(event_data.get("is_error")) else "success")
        ),
        "lifecycle": "tool_completed",
    }
    for field_name in _TOOL_RESULT_OPTIONAL_FIELDS:
        if field_name in event_data and event_data[field_name] is not None:
            payload[field_name] = deepcopy(event_data[field_name])
    return payload


def execution_journal_owner(owner_kind: str, *identity_parts: object) -> str:
    """Build a storage-safe opaque owner id from durable runtime identity."""

    kind = "".join(
        character
        for character in str(owner_kind or "run").strip().lower()
        if character.isascii() and (character.isalnum() or character in {"_", "-"})
    ) or "run"
    canonical = "\0".join(str(part or "") for part in identity_parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{kind}_{digest}"


def _lock_for(path: Path) -> threading.Lock:
    key = canonical_file_path_key(path)
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _process_lock_for(path: Path) -> FileLock:
    # Keep the lock beside the per-agent directory rather than inside it:
    # delete_agent_journal removes that directory while the lock is held, and
    # Windows cannot remove an open lock file.
    return FileLock(
        str(path.parent.parent / f".{path.parent.name}.mutation.lock"),
        timeout=60,
    )


def get_journal_dir(agent_id: str, *, base_dir: Path | None = None) -> Path:
    root = base_dir or JOURNAL_ROOT
    clean_agent_id = validate_storage_id(agent_id, field_name="agent_id")
    path = root / clean_agent_id
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with suppress(OSError):
            path.chmod(0o700)
    return path


def get_journal_path(agent_id: str, *, base_dir: Path | None = None) -> Path:
    return get_journal_dir(agent_id, base_dir=base_dir) / "events.jsonl"


def delete_agent_journal(agent_id: str, *, base_dir: Path | None = None) -> bool:
    """Delete one validated agent journal directory without widening scope."""
    clean_agent_id = validate_storage_id(agent_id, field_name="agent_id")
    root = (base_dir or JOURNAL_ROOT).resolve()
    target = (root / clean_agent_id).resolve()
    target.relative_to(root)
    journal_path = target / "events.jsonl"
    journal_key = canonical_file_path_key(journal_path)
    with _lock_for(journal_path):
        with _process_lock_for(journal_path):
            if not target.exists():
                return False
            shutil.rmtree(target)
            _WRITE_SEQUENCES.pop(journal_key, None)
            return True


@dataclass
class JournalEvent:
    event_type: str
    agent_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    seq: int = 0
    ts_ms: int = field(default_factory=epoch_ms)
    parent_event_id: str = ""
    schema_version: int = _JOURNAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JournalEvent":
        event_type = str(data.get("event_type") or "system")
        if event_type not in EVENT_TYPES:
            event_type = "system"
        return cls(
            event_type=event_type,
            agent_id=str(data.get("agent_id") or ""),
            payload=dict(data.get("payload") or {}),
            event_id=str(data.get("event_id") or uuid4().hex),
            seq=int(data.get("seq") or 0),
            ts_ms=int(data.get("ts_ms") or epoch_ms()),
            parent_event_id=str(data.get("parent_event_id") or ""),
            schema_version=int(data.get("schema_version") or _JOURNAL_SCHEMA_VERSION),
        )


class ExecutionJournal:
    """Per-agent append-only JSONL journal."""

    def __init__(self, agent_id: str, *, base_dir: Path | None = None) -> None:
        self.agent_id = validate_storage_id(agent_id, field_name="agent_id")
        self.base_dir = base_dir
        self.path = get_journal_path(self.agent_id, base_dir=base_dir)
        self._process_lock = _process_lock_for(self.path)
        self._event_cache: list[JournalEvent] | None = None
        self._event_cache_signature: tuple[int, int, int] | None = None
        self._tool_states: dict[str, tuple[str, JournalEvent]] = {}
        self._events_by_id: dict[str, JournalEvent] = {}
        self._projection_heads: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._context_heads: dict[str, tuple[str, dict[str, Any], int]] = {}
        self.unacknowledged_tail_records = 0
        self.unacknowledged_tail_path: Path | None = None
        key = canonical_file_path_key(self.path)
        with self._locked():
            self._isolate_incomplete_tail_unlocked()
            current = _WRITE_SEQUENCES.get(key)
            if current is None:
                current = self._load_last_seq()
                _WRITE_SEQUENCES[key] = current
            self._seq = current

    @contextmanager
    def _locked(self):
        with _lock_for(self.path):
            with self._process_lock:
                yield

    def _isolate_incomplete_tail_unlocked(self) -> None:
        """Move a crash-truncated final JSONL fragment out of the live chain."""

        if not self.path.exists():
            return
        with self.path.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return

            offset = size
            boundary = 0
            while offset > 0:
                step = min(4096, offset)
                offset -= step
                handle.seek(offset)
                chunk = handle.read(step)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    boundary = offset + newline + 1
                    break

            handle.seek(boundary)
            fragment = handle.read(size - boundary)
            digest = hashlib.sha256(fragment).hexdigest()[:20]
            quarantine = self.path.with_name(
                f"{self.path.stem}.incomplete-{digest}.jsonl"
            )
            if not quarantine.exists():
                with quarantine.open("xb") as partial:
                    partial.write(fragment)
                    partial.flush()
                    os.fsync(partial.fileno())
            handle.seek(boundary)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())

        self.unacknowledged_tail_records = 1
        self.unacknowledged_tail_path = quarantine
        logger.error(
            "Execution journal for %s ended in an incomplete JSONL fragment; "
            "isolated %d bytes at %s before resuming the valid prefix.",
            self.agent_id,
            len(fragment),
            quarantine,
        )

    def _load_last_seq(self) -> int:
        """Read the durable tail's sequence without re-validating the journal.

        ``append`` only needs the last allocated sequence. Parsing and
        validating every record here made each append cost O(journal size) and
        a whole run quadratic, synchronously on the event loop. Full record
        validation still happens in ``read_events()``, which is what recovery
        and reconstruction read.

        An unterminated crash fragment is isolated before this method runs.
        Every newline-terminated record is committed evidence, so malformed
        complete records fail closed instead of being skipped.
        """
        if not self.path.exists():
            return 0
        try:
            lines = _tail_lines(self.path)
        except OSError as exc:
            raise ExecutionJournalError(
                f"Failed reading execution journal for {self.agent_id}"
            ) from exc
        for line in reversed(lines):
            text = line.strip()
            if not text:
                continue
            try:
                seq = int(json.loads(text)["seq"])
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                raise ExecutionJournalCorruptionError(
                    f"Execution journal for {self.agent_id} has an invalid final record"
                ) from exc
            if seq < 1:
                raise ExecutionJournalCorruptionError(
                    f"Execution journal for {self.agent_id} has an invalid final sequence"
                )
            return seq
        return 0


    def _read_events_unlocked(self) -> list[JournalEvent]:
        if not self.path.exists():
            return []
        events: list[JournalEvent] = []
        context_heads: dict[str, tuple[str, dict[str, Any], int]] = {}
        seen_event_ids: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    # A blank line carries no record and no loss; every
                    # reference harness skips one rather than refusing the
                    # whole journal.
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal contains invalid JSON at line {line_number}"
                    ) from exc
                if not isinstance(data, dict):
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal record {line_number} must be an object"
                    )
                event_type = str(data.get("event_type") or "").strip()
                if event_type not in EVENT_TYPES:
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal record {line_number} has an invalid event type"
                    )
                if str(data.get("agent_id") or "").strip() != self.agent_id:
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal record {line_number} has the wrong owner"
                    )
                if not isinstance(data.get("payload"), dict):
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal record {line_number} has an invalid payload"
                    )
                try:
                    schema_version = int(data.get("schema_version") or 0)
                    seq = int(data.get("seq") or 0)
                    int(data.get("ts_ms") or 0)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal record {line_number} has invalid metadata"
                    ) from exc
                if schema_version < 1 or schema_version > _JOURNAL_SCHEMA_VERSION:
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal record {line_number} has an unsupported schema"
                    )
                expected_seq = len(events) + 1
                if seq != expected_seq:
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal sequence is not contiguous at line {line_number}"
                    )
                event_id = str(data.get("event_id") or "").strip()
                if not event_id or event_id in seen_event_ids:
                    raise ExecutionJournalCorruptionError(
                        f"Execution journal record {line_number} has an invalid event id"
                    )
                seen_event_ids.add(event_id)
                event = JournalEvent.from_dict(data)
                self._expand_context_snapshot(event, context_heads, events)
                events.append(event)
        return events

    def _expand_context_snapshot(
        self, event: JournalEvent, heads: dict[str, tuple[str, dict[str, Any], int]],
        events: list[JournalEvent],
    ) -> None:
        """Decode the physical snapshot chain into the existing logical event."""
        payload = event.payload
        owner = str(payload.get("conversation_id") or "")
        if event.schema_version >= 7 and "context_snapshot_change" in payload:
            encoded = payload.pop("context_snapshot_change")
            head = heads.get(owner)
            try:
                if head is None or head[0] != encoded["base_event_id"]:
                    raise ValueError("Context snapshot change has no matching base")
                before = self._context_snapshot_base(head[1], events[head[2]:], owner)
                snapshot = apply_value_change(before, encoded["change"])
                if not isinstance(snapshot, dict):
                    raise ValueError("Context snapshot change must produce an object")
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                raise ExecutionJournalCorruptionError("Invalid context snapshot change") from exc
            payload["context_snapshot"] = snapshot
        snapshot = payload.get("context_snapshot")
        if isinstance(snapshot, dict):
            heads[owner] = (event.event_id, snapshot, event.seq)

    def _context_snapshot_base(
        self, snapshot: dict[str, Any], events: list[JournalEvent], owner: str,
    ) -> dict[str, Any]:
        # Private facts between snapshots are already durable. Reuse their
        # existing cursor-aware replay (including checkpoint overlap), so the
        # next snapshot need only encode state absent from those facts.
        updates = [event for event in events
                   if event.payload.get("lifecycle") == "extension_state_delta"
                   and str(event.payload.get("conversation_id") or "") == owner]
        if not updates:
            return snapshot
        state, cursor, _, _ = self.replay_extension_state(
            snapshot.get("extension_state", {}), cursor=snapshot.get("extension_cursor"), events=updates,
        )
        return {**snapshot, "extension_state": state, "extension_cursor": cursor}

    def _file_signature_unlocked(self) -> tuple[int, int, int] | None:
        if not self.path.exists():
            return None
        stat = self.path.stat()
        return (int(stat.st_size), int(stat.st_mtime_ns), int(stat.st_ino))

    def _validated_events_unlocked(self) -> list[JournalEvent]:
        signature = self._file_signature_unlocked()
        if (
            self._event_cache is not None
            and signature == self._event_cache_signature
        ):
            return self._event_cache
        events = self._read_events_unlocked()
        self._event_cache = events
        self._tool_states = self._tool_lifecycle_states(events)
        self._events_by_id = {event.event_id: event for event in events}
        self._context_heads = {
            str(event.payload.get("conversation_id") or ""): (event.event_id, event.payload["context_snapshot"], event.seq)
            for event in events if isinstance(event.payload.get("context_snapshot"), dict)
        }
        self._projection_heads = {}
        for _ in self._expanded_projection_events(events, self._projection_heads):
            pass
        self._event_cache_signature = self._file_signature_unlocked()
        return events

    def append(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        parent_event_id: str = "",
        event_id: str | None = None,
        ts_ms: int | None = None,
    ) -> JournalEvent:
        with self._locked():
            return self._append_unlocked(
                event_type,
                payload,
                parent_event_id=parent_event_id,
                event_id=event_id,
                ts_ms=ts_ms,
            )

    def append_once(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        event_id: str,
        parent_event_id: str = "",
        ts_ms: int | None = None,
    ) -> JournalEvent:
        """Append a stable fact once, returning the durable copy on replay."""

        stable_id = str(event_id or "").strip()
        if not stable_id:
            raise ValueError("event_id is required for append_once")
        clean_type = str(event_type or "system").strip() or "system"
        if clean_type not in EVENT_TYPES:
            clean_type = "system"
        clean_payload = deepcopy(dict(payload or {}))
        with self._locked():
            self._validated_events_unlocked()
            event = self._events_by_id.get(stable_id)
            if event is not None:
                if event.event_type != clean_type:
                    raise ExecutionJournalError(
                        f"Execution journal event id {stable_id!r} changed type"
                    )
                if event.payload != clean_payload:
                    raise ExecutionJournalError(
                        f"Execution journal event id {stable_id!r} changed payload"
                    )
                return event
            return self._append_unlocked(
                clean_type,
                clean_payload,
                parent_event_id=parent_event_id,
                event_id=stable_id,
                ts_ms=ts_ms,
            )

    def _append_unlocked(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        parent_event_id: str = "",
        event_id: str | None = None,
        ts_ms: int | None = None,
    ) -> JournalEvent:
        clean_type = str(event_type or "system").strip() or "system"
        if clean_type not in EVENT_TYPES:
            clean_type = "system"
        key = canonical_file_path_key(self.path)
        # Validate a changed durable chain before appending. Once this
        # instance has validated the current file identity, its own fsync'd
        # appends extend that proof instead of reparsing the entire journal
        # for every fact. External writes change the file signature and
        # force full validation before another append.
        durable_events = self._validated_events_unlocked()
        durable_seq = durable_events[-1].seq if durable_events else 0
        if not self.path.exists():
            self._seq = 0
            _WRITE_SEQUENCES[key] = 0
        next_seq = max(self._seq, _WRITE_SEQUENCES.get(key, 0), durable_seq) + 1
        event = JournalEvent(
            event_type=clean_type,
            agent_id=self.agent_id,
            payload=dict(payload or {}),
            event_id=str(event_id or uuid4().hex),
            seq=next_seq,
            ts_ms=int(ts_ms or epoch_ms()),
            parent_event_id=str(parent_event_id or ""),
        )
        physical = event.to_dict()
        # Own the serialized payload: caller mutations after append must not
        # change the next delta's base or the warm recovery view.
        event.payload = physical["payload"]
        snapshot = event.payload.get("context_snapshot")
        owner = str(event.payload.get("conversation_id") or "")
        if isinstance(snapshot, dict) and owner in self._context_heads:
            base_id, before, base_seq = self._context_heads[owner]
            before = self._context_snapshot_base(before, durable_events[base_seq:], owner)
            physical["payload"] = dict(event.payload)
            physical["payload"].pop("context_snapshot")
            physical["payload"]["context_snapshot_change"] = {
                "base_event_id": base_id, "change": value_change(before, snapshot),
            }
        line = json.dumps(physical, ensure_ascii=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._seq = next_seq
        _WRITE_SEQUENCES[key] = next_seq
        durable_events.append(event)
        self._tool_lifecycle_states((event,), self._tool_states)
        self._events_by_id[event.event_id] = event
        if isinstance(snapshot, dict):
            self._context_heads[owner] = (event.event_id, snapshot, event.seq)
        for _ in self._expanded_projection_events((event,), self._projection_heads):
            pass
        self._event_cache_signature = self._file_signature_unlocked()
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)
        return event

    def append_cleanup(
        self,
        payload: dict[str, Any],
        *,
        parent_event_id: str = "",
    ) -> JournalEvent:
        """Append one canonical resource-cleanup fact."""
        return self.append(
            "cleanup",
            payload,
            parent_event_id=parent_event_id,
        )

    @staticmethod
    def _tool_lifecycle_states(
        events: Iterable[JournalEvent],
        states: dict[str, tuple[str, JournalEvent]] | None = None,
    ) -> dict[str, tuple[str, JournalEvent]]:
        """Return the latest open/closed state for each provider call id."""

        if states is None:
            states = {}
        for event in events:
            if event.event_type == "tool_use":
                tool_call = event.payload.get("tool_call")
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id") or "").strip()
                if call_id:
                    states[call_id] = ("open", event)
                continue
            if event.event_type == "assistant":
                tool_calls = event.payload.get("tool_calls")
                if not isinstance(tool_calls, list):
                    continue
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    call_id = str(tool_call.get("id") or "").strip()
                    if call_id:
                        states[call_id] = ("open", event)
                continue
            if event.event_type == "tool_result":
                call_id = str(
                    event.payload.get("tool_call_id")
                    or event.payload.get("call_id")
                    or ""
                ).strip()
                if call_id:
                    states[call_id] = ("closed", event)
        return states

    def append_tool_use(
        self,
        event_data: Mapping[str, Any],
        *,
        parent_event_id: str = "",
    ) -> JournalEvent | None:
        """Append one tool use unless the same open request was already recorded.

        A closed id is deliberately not a dedupe key: providers and adapters
        may reuse an id on a later turn, and that new use needs a new pair in
        the append-only journal.
        """

        source = event_data.get("tool_call")
        if not isinstance(source, Mapping):
            source = event_data
        payload = tool_use_journal_payload(source)
        call_id = str(payload["tool_call"]["id"])
        with self._locked():
            self._validated_events_unlocked()
            state = self._tool_states.get(call_id)
            if state is not None and state[0] == "open":
                previous = state[1].payload.get("tool_call")
                current = payload.get("tool_call")
                if (
                    isinstance(previous, dict)
                    and isinstance(current, dict)
                    and previous.get("name") == current.get("name")
                    and previous.get("arguments") == current.get("arguments")
                    and str(previous.get("request_digest") or "")
                    == str(current.get("request_digest") or "")
                ):
                    return None
            return self._append_unlocked(
                "tool_use",
                payload,
                parent_event_id=parent_event_id,
            )

    def append_tool_result(
        self,
        event_data: Mapping[str, Any],
        *,
        tool_name: str = "",
        parent_event_id: str = "",
    ) -> JournalEvent | None:
        """Append one result exactly once for the latest open use of its id."""

        payload = tool_result_journal_payload(event_data, tool_name=tool_name)
        call_id = str(payload["tool_call_id"])
        resolved_name = str(payload["tool_name"] or "tool")
        with self._locked():
            self._validated_events_unlocked()
            state = self._tool_states.get(call_id)
            if state is not None and state[0] == "closed":
                return None
            if state is None:
                synthetic_use = tool_use_journal_payload(
                    {
                        "id": call_id,
                        "name": resolved_name,
                        "args": {},
                        "status": "cancelled",
                        "announcement_only": True,
                        "arguments_complete": False,
                        "request_digest": event_data.get("request_digest"),
                    }
                )
                synthetic = self._append_unlocked(
                    "tool_use",
                    synthetic_use,
                )
                parent_event_id = parent_event_id or synthetic.event_id
            else:
                parent_event_id = parent_event_id or state[1].event_id
            return self._append_unlocked(
                "tool_result",
                payload,
                parent_event_id=parent_event_id,
            )

    def read_events(self) -> list[JournalEvent]:
        with self._locked():
            try:
                return list(self._validated_events_unlocked())
            except OSError as exc:
                raise ExecutionJournalError(
                    f"Failed reading execution journal for {self.agent_id}"
                ) from exc

    def reconstruct_history(self, *, events: list[JournalEvent] | None = None) -> list[dict[str, Any]]:
        """Rebuild provider-shaped history from ordered journal facts."""
        events = self.read_events() if events is None else events
        history: list[dict[str, Any]] = []
        start_index = 0
        extension_cursor: dict[str, Any] = {}
        # A context snapshot is a typed replacement item, not a second store.
        # Start from the newest replacement and apply later append-only facts.
        # This preserves opaque provider items, signatures, encrypted
        # reasoning, attachments, and compaction boundaries byte-for-byte.
        for index in range(len(events) - 1, -1, -1):
            snapshot = events[index].payload.get("context_snapshot")
            if not isinstance(snapshot, dict):
                continue
            snapshot_history = snapshot.get("history")
            if not isinstance(snapshot_history, list):
                continue
            history = [
                deepcopy(item)
                for item in snapshot_history
                if isinstance(item, dict)
            ]
            pending_tool_call_ids: set[str] = set()
            for item in history:
                if str(item.get("role") or "") == "assistant":
                    for tool_call in item.get("tool_calls") or []:
                        if isinstance(tool_call, dict):
                            call_id = str(tool_call.get("id") or "").strip()
                            if call_id:
                                pending_tool_call_ids.add(call_id)
                elif str(item.get("role") or "") == "tool":
                    call_id = str(item.get("tool_call_id") or "").strip()
                    if call_id:
                        pending_tool_call_ids.discard(call_id)
            start_index = index + 1
            extension_cursor = dict(snapshot.get("extension_cursor", {}))
            break
        else:
            pending_tool_call_ids = set()

        for event in events[start_index:]:
            payload = event.payload
            if payload.get("lifecycle") == "extension_state_delta":
                current = extension_cursor.get("revision", 0) if extension_cursor.get("run_id") == payload["run_id"] else 0
                for change in payload["extension_changes"][max(0, current - payload["base_revision"]):]:
                    history.extend(deepcopy(change.get("context_messages", [])))
                extension_cursor = {"run_id": payload["run_id"], "revision": max(current, payload["revision"])}
                continue
            if payload.get("lifecycle") == "provider_item_committed":
                message = payload.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                stamp = message.get("timestamp_ms")
                existing = next((item for item in reversed(history) if stamp is not None and item.get("role") == "assistant" and item.get("timestamp_ms") == stamp), None)
                old_ids = {call.get("id") for call in (existing or {}).get("tool_calls", [])}
                if existing is None:
                    history.append(deepcopy(message))
                else:
                    existing.update(deepcopy(message))
                pending_tool_call_ids.update(call["id"] for call in message.get("tool_calls", []) if call.get("id") and call["id"] not in old_ids)
                continue
            if event.event_type == "user_prompt":
                content = str(
                    payload.get("provider_content")
                    or payload.get("content")
                    or payload.get("prompt")
                    or ""
                ).strip()
                if content:
                    history.append({"role": "user", "content": content})
                continue
            if event.event_type == "assistant":
                content = str(payload.get("content") or payload.get("text") or "")
                tool_calls = payload.get("tool_calls")
                message: dict[str, Any] = {"role": "assistant", "content": content}
                if isinstance(tool_calls, list) and tool_calls:
                    message["tool_calls"] = tool_calls
                    pending_tool_call_ids.update(
                        str(tool_call.get("id") or "").strip()
                        for tool_call in tool_calls
                        if isinstance(tool_call, dict)
                        and str(tool_call.get("id") or "").strip()
                    )
                history.append(message)
                continue
            if event.event_type == "tool_use":
                tool_call = payload.get("tool_call")
                if isinstance(tool_call, dict) and (tool_call.get("call_source") or {}).get("kind") in {"code_mode", "extension"}:
                    continue
                if isinstance(tool_call, dict) and tool_call.get("id"):
                    call_id = str(tool_call["id"]).strip()
                    if call_id in pending_tool_call_ids:
                        # A streamed provider item already introduced this
                        # call. Update its authorized arguments, not its count.
                        for message in reversed(history):
                            matched = next((call for call in message.get("tool_calls", []) if call.get("id") == call_id), None)
                            if matched is not None:
                                matched.update(deepcopy(tool_call))
                                break
                        continue
                    history.append(
                        {
                            "role": "assistant",
                            "content": str(payload.get("content") or ""),
                            "tool_calls": [tool_call],
                        }
                    )
                    pending_tool_call_ids.add(str(tool_call["id"]).strip())
                continue
            if event.event_type == "tool_result":
                call_id = str(payload.get("tool_call_id") or payload.get("call_id") or "").strip()
                if not call_id or call_id not in pending_tool_call_ids:
                    continue
                history.append(
                    {
                        "role": "tool",
                        "content": str(payload.get("content") or ""),
                        "name": str(payload.get("tool_name") or payload.get("name") or "tool"),
                        "tool_call_id": call_id,
                    }
                )
                pending_tool_call_ids.discard(call_id)
                continue
            if event.event_type == "system":
                if bool(payload.get("transcript_only")):
                    continue
                content = str(payload.get("content") or "").strip()
                if content:
                    history.append({"role": "system", "content": content})
        return history

    def replay_extension_state(
        self, state: dict[str, Any], *, cursor: dict[str, Any] | None = None,
        run_id: str | None = None, after_seq: int = 0,
        events: list[JournalEvent] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], int, list[dict[str, Any]]]:
        """Replay private updates and context deliveries newer than the supplied snapshot.

        Later full snapshots may advance private state without replacing the
        caller's model history. Track message delivery from the original
        cursor independently so those deliveries are still recovered once.
        """
        from backend.conversations.projection_log import apply_value_change

        events = self.read_events() if events is None else events
        result = state
        position = dict(cursor or {})
        message_position = dict(position)
        context_messages: list[dict[str, Any]] = []
        if position and (not isinstance(position.get("run_id"), str)
                         or isinstance(position.get("revision"), bool)
                         or not isinstance(position.get("revision"), int) or position["revision"] < 0):
            raise ExecutionJournalCorruptionError("Invalid extension state cursor")
        applied = 0
        for event in events:
            payload = event.payload
            snapshot = payload.get("context_snapshot")
            captured = snapshot.get("extension_cursor", {}) if isinstance(snapshot, dict) else {}
            event_run_id = payload.get("run_id") or captured.get("run_id")
            if event.seq <= after_seq or (run_id is not None and event_run_id != run_id):
                continue
            kind = payload.get("lifecycle")
            if captured and captured.get("run_id") == event_run_id:
                current = position.get("revision", 0) if position.get("run_id") == event_run_id else 0
                revision = captured.get("revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise ExecutionJournalCorruptionError("Invalid extension snapshot cursor")
                if revision > current:
                    result = deepcopy(snapshot["extension_state"])
                    position = dict(captured)
                    applied += 1
            elif kind == "extension_state_committed" and run_id is None:
                result = deepcopy(payload["context_snapshot"]["extension_state"])
                position = dict(payload["context_snapshot"].get("extension_cursor", {}))
                applied += 1
            elif kind == "extension_state_delta":
                base, revision, changes = payload["base_revision"], payload["revision"], payload["extension_changes"]
                if (not isinstance(changes, list) or isinstance(base, bool) or not isinstance(base, int) or base < 0
                        or isinstance(revision, bool) or not isinstance(revision, int)
                        or revision - base != len(changes)):
                    raise ExecutionJournalCorruptionError("Invalid extension delta revision")
                message_revision = message_position.get("revision", 0) if message_position.get("run_id") == payload["run_id"] else 0
                for change in changes[max(0, message_revision - base):]:
                    context_messages.extend(deepcopy(change.get("context_messages", [])))
                message_position = {"run_id": payload["run_id"], "revision": max(message_revision, revision)}
                current = position.get("revision", 0) if position.get("run_id") == payload["run_id"] else 0
                if current >= revision:
                    continue
                if current < base:
                    raise ExecutionJournalCorruptionError("Extension delta is missing an earlier committed change")
                if applied == 0:
                    result = deepcopy(state)
                for change in changes[current - base:]:
                    result = apply_value_change(result, change, in_place=True)
                position = {"run_id": payload["run_id"], "revision": revision}
                applied += 1
        return result, position, applied, context_messages

    def reconstruct_context_snapshot(self) -> dict[str, Any]:
        events = self.read_events()
        base = next((event for event in reversed(events) if isinstance(event.payload.get("context_snapshot"), dict)), None)
        snapshot = deepcopy(base.payload["context_snapshot"]) if base is not None else {}
        snapshot["history"] = self.reconstruct_history(events=events)
        snapshot["extension_state"], snapshot["extension_cursor"], _, _ = self.replay_extension_state(
            snapshot.get("extension_state", {}), cursor=snapshot.get("extension_cursor"),
            after_seq=base.seq if base is not None else 0, events=events,
        )
        snapshot.pop("context_revision", None)
        return snapshot

    def unresolved_tool_uses(self) -> list[dict[str, Any]]:
        """Return tool_use entries that never received a matching tool_result."""
        uses: dict[str, dict[str, Any]] = {}
        for event in self.read_events():
            if event.event_type == "tool_use":
                tool_call = event.payload.get("tool_call")
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id") or "").strip()
                if not call_id:
                    continue
                uses[call_id] = {
                    "tool_call_id": call_id,
                    "tool_name": str(tool_call.get("name") or "tool"),
                    "arguments": tool_call.get("arguments"),
                    "side_effect_kind": str(tool_call.get("side_effect_kind") or ""),
                    "idempotent": bool(tool_call.get("idempotent", False)),
                    "idempotency_key": str(tool_call.get("idempotency_key") or ""),
                    "request_digest": str(tool_call.get("request_digest") or ""),
                    "recovery_policy": (
                        "retry_safe"
                        if bool(tool_call.get("idempotent", False))
                        and str(tool_call.get("idempotency_key") or "").strip()
                        else "manual"
                    ),
                    "event_id": event.event_id,
                    "seq": event.seq,
                }
            elif event.event_type == "tool_result":
                call_id = str(
                    event.payload.get("tool_call_id")
                    or event.payload.get("call_id")
                    or ""
                ).strip()
                if call_id:
                    uses.pop(call_id, None)
            elif event.event_type == "assistant":
                tool_calls = event.payload.get("tool_calls")
                if not isinstance(tool_calls, list):
                    continue
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    call_id = str(tool_call.get("id") or "").strip()
                    if not call_id:
                        continue
                    uses[call_id] = {
                        "tool_call_id": call_id,
                        "tool_name": str(tool_call.get("name") or "tool"),
                        "arguments": tool_call.get("arguments"),
                        "side_effect_kind": str(tool_call.get("side_effect_kind") or ""),
                        "idempotent": bool(tool_call.get("idempotent", False)),
                        "idempotency_key": str(tool_call.get("idempotency_key") or ""),
                        "request_digest": str(tool_call.get("request_digest") or ""),
                        "recovery_policy": (
                            "retry_safe"
                            if bool(tool_call.get("idempotent", False))
                            and str(tool_call.get("idempotency_key") or "").strip()
                            else "manual"
                        ),
                        "event_id": event.event_id,
                        "seq": event.seq,
                    }
        return list(uses.values())

    def close_unresolved_tool_uses(
        self,
        *,
        reason: str = "cancelled",
        content: str | None = None,
    ) -> list[JournalEvent]:
        """Synthesize tool_result facts for every unresolved tool_use."""
        closed: list[JournalEvent] = []
        clean_reason = str(reason or "cancelled").strip().lower()
        synthetic_status = (
            "cancelled"
            if clean_reason
            in {
                "aborted",
                "cancelled",
                "canceled",
                "interrupted",
                "user_interrupted",
                "startup_cancelled",
                "consumer_closed",
            }
            else "failed"
        )
        # An unpaired tool_use needs an explicit aborted result rather than a
        # silent gap, so replay and recovery see why it never completed. Every
        # reference harness synthesizes some terminator here rather than leaving
        # the pair open; MiniCode records it as a journal fact so the evidence
        # survives independently of the provider history.  A
        # synthetic result must therefore never look successful merely because
        # the surrounding child reached a terminal state.
        message = content or "[Tool result missing due to internal error]"
        for item in self.unresolved_tool_uses():
            event = self.append_tool_result(
                {
                    "tool_call_id": item["tool_call_id"],
                    "tool_name": item["tool_name"],
                    "content": message,
                    "status": synthetic_status,
                    "termination_reason": clean_reason,
                    "synthetic": True,
                    "request_digest": str(item.get("request_digest") or ""),
                },
                tool_name=str(item["tool_name"]),
                parent_event_id=str(item.get("event_id") or ""),
            )
            if event is not None:
                closed.append(event)
        return closed

    def append_terminal(
        self,
        *,
        status: str,
        summary: str = "",
        reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> JournalEvent:
        payload = {
            "status": status,
            "summary": summary,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        return self.append("terminal", payload)

    def append_lifecycle(
        self,
        lifecycle: str,
        payload: dict[str, Any] | None = None,
    ) -> JournalEvent:
        """Persist a run lifecycle fact using the existing system event type."""

        data = {"lifecycle": str(lifecycle or "system").strip() or "system"}
        if payload:
            data.update(payload)
        if lifecycle == "conversation_projection_pending":
            message_id = str((data.get("assistant_message") or {}).get("id") or "")
            key = (str(data.get("conversation_id") or ""), message_id)
            with self._locked():
                self._validated_events_unlocked()
                head = self._projection_heads.get(key)
                if head is not None and message_id:
                    data = {
                        "lifecycle": "conversation_projection_delta", "conversation_id": key[0],
                        "message_id": message_id, "base_event_id": head[0],
                        "change": value_change(head[1], data),
                    }
                return self._append_unlocked("system", data)
        return self.append("system", data)

    @staticmethod
    def _expanded_projection_events(
        events: Iterable[JournalEvent],
        heads: dict[tuple[str, str], tuple[str, dict[str, Any]]] | None = None,
    ) -> Iterable[JournalEvent]:
        """Materialize projection records only for recovery and the latest write base."""
        heads = {} if heads is None else heads
        for event in events:
            data = event.payload
            lifecycle = data.get("lifecycle")
            if lifecycle == "conversation_projection_pending":
                key = (str(data.get("conversation_id") or ""), str((data.get("assistant_message") or {}).get("id") or ""))
                heads[key] = (event.event_id, data)
            elif lifecycle == "conversation_projection_delta":
                key = (data["conversation_id"], data["message_id"])
                head = heads.get(key)
                if head is None or head[0] != data["base_event_id"]:
                    raise ExecutionJournalCorruptionError("Conversation projection delta has no matching base")
                try:
                    payload = apply_value_change(head[1], data["change"])
                    identity = (payload["conversation_id"], payload["assistant_message"]["id"])
                    if identity != key or payload["lifecycle"] != "conversation_projection_pending":
                        raise ValueError("Conversation projection delta changed its owner")
                except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                    raise ExecutionJournalCorruptionError("Invalid conversation projection delta") from exc
                heads[key] = (event.event_id, payload)
                event = replace(event, payload=payload)
            yield event

    def pending_conversation_projections(self) -> list[JournalEvent]:
        """Return terminal conversation projections without commit receipts."""

        pending: dict[str, JournalEvent] = {}
        for event in self._expanded_projection_events(self.read_events()):
            lifecycle = str(event.payload.get("lifecycle") or "")
            if lifecycle == "conversation_projection_pending":
                pending[event.event_id] = event
            elif lifecycle == "conversation_projection_committed":
                pending_id = str(event.payload.get("pending_event_id") or "").strip()
                committed = pending.pop(pending_id, None)
                if committed is None:
                    continue
                message = committed.payload.get("assistant_message") or {}
                message_id = str(message.get("id") or event.payload.get("message_id") or "")
                conversation_id = committed.payload.get("conversation_id")
                if message_id:
                    # A later committed replacement owns this message's whole
                    # projection, including earlier partial writes that failed.
                    for earlier_id, earlier in tuple(pending.items()):
                        earlier_message = earlier.payload.get("assistant_message") or {}
                        if (
                            earlier.seq < committed.seq
                            and earlier.payload.get("conversation_id") == conversation_id
                            and earlier_message.get("id") == message_id
                        ):
                            pending.pop(earlier_id)
        return list(pending.values())

    def unprojected_terminal_projections(self) -> list[dict[str, Any]]:
        """Build replay payloads only for runtime-committed terminal facts.

        This covers the crash window after the runtime terminal CAS but before
        the WebSocket owner could append its richer conversation projection.
        """

        events = self.read_events()
        covered_ids: set[str] = set()
        for event in events:
            lifecycle = str(event.payload.get("lifecycle") or "")
            if lifecycle in {"conversation_projection_pending", "conversation_projection_delta"}:
                message = event.payload.get("assistant_message") or {"id": event.payload.get("message_id")}
                if isinstance(message, dict):
                    message_id = str(message.get("id") or "").strip()
                    if message_id:
                        covered_ids.add(message_id)
            elif lifecycle == "conversation_projection_committed":
                message_id = str(event.payload.get("message_id") or "").strip()
                if message_id:
                    covered_ids.add(message_id)

        committed_run_ids: set[str] = set()
        committed_intent_ids: set[str] = set()
        failed_run_ids: set[str] = set()
        intents: dict[str, JournalEvent] = {}
        for event in events:
            lifecycle = str(event.payload.get("lifecycle") or "")
            run_id = str(event.payload.get("run_id") or "").strip()
            if lifecycle == "runtime_terminal_committed":
                if run_id:
                    committed_run_ids.add(run_id)
                intent_id = str(
                    event.payload.get("terminal_intent_event_id") or ""
                ).strip()
                if intent_id:
                    committed_intent_ids.add(intent_id)
            elif lifecycle == "runtime_terminal_commit_failed" and run_id:
                failed_run_ids.add(run_id)
            elif lifecycle == "terminal_intent":
                intents[event.event_id] = event

        # A later explicit failure always wins over an earlier/malformed receipt.
        committed_run_ids.difference_update(failed_run_ids)

        assistants: dict[str, JournalEvent] = {}
        projections: list[dict[str, Any]] = []
        for event in events:
            message_id = str(event.payload.get("message_id") or "").strip()
            if event.event_type == "assistant" and message_id:
                assistants[message_id] = event
                continue
            if event.event_type != "terminal" or not message_id:
                continue
            run_id = str(event.payload.get("run_id") or "").strip()
            intent_id = str(
                event.payload.get("terminal_intent_event_id") or ""
            ).strip()
            if not (
                (run_id and run_id in committed_run_ids)
                or (intent_id and intent_id in committed_intent_ids)
            ):
                continue
            if message_id in covered_ids:
                continue
            assistant = assistants.get(message_id)
            if assistant is None:
                continue
            context_snapshot = assistant.payload.get("context_snapshot")
            if not isinstance(context_snapshot, dict):
                continue
            projections.append(
                {
                    "source_event_id": event.event_id,
                    "conversation_id": str(
                        event.payload.get("conversation_id")
                        or assistant.payload.get("conversation_id")
                        or ""
                    ),
                    "assistant_message": {
                        "id": message_id,
                        "role": "assistant",
                        "content": str(assistant.payload.get("content") or ""),
                        "completed_at": int(event.ts_ms),
                        "terminal_status": str(
                            event.payload.get("status") or "completed"
                        ),
                        "termination_reason": str(
                            event.payload.get("reason") or ""
                        ),
                    },
                    "context_snapshot": deepcopy(context_snapshot),
                    "summary": None,
                }
            )
            covered_ids.add(message_id)

        # The process may crash after the runtime CAS receipt but before the
        # assistant/terminal pair is appended. The intent contains the exact
        # context snapshot captured before CAS and is safe only when its receipt
        # explicitly names it (or names the same committed run).
        for intent_id, event in intents.items():
            run_id = str(event.payload.get("run_id") or "").strip()
            if not (
                intent_id in committed_intent_ids
                or (run_id and run_id in committed_run_ids)
            ):
                continue
            assistant = event.payload.get("assistant_message")
            if not isinstance(assistant, dict):
                continue
            message_id = str(assistant.get("id") or event.payload.get("message_id") or "").strip()
            if not message_id or message_id in covered_ids:
                continue
            context_snapshot = event.payload.get("context_snapshot")
            if not isinstance(context_snapshot, dict):
                continue
            projections.append(
                {
                    "source_event_id": intent_id,
                    "conversation_id": str(event.payload.get("conversation_id") or ""),
                    "assistant_message": deepcopy(assistant),
                    "context_snapshot": deepcopy(context_snapshot),
                    "summary": None,
                }
            )
            covered_ids.add(message_id)
        # End hooks may persist private extension state after the model's
        # terminal receipt. Apply only that private state to the same message;
        # never replace the already-committed assistant answer or history.
        event_sequences = {event.event_id: event.seq for event in events}
        updates_by_message: dict[str, list[JournalEvent]] = {}
        for event in events:
            if event.payload.get("lifecycle") in {"extension_state_committed", "extension_state_delta"}:
                updates_by_message.setdefault(str(event.payload.get("message_id") or ""), []).append(event)
        for projection in projections:
            message_id = projection["assistant_message"]["id"]
            snapshot = projection["context_snapshot"]
            state, cursor, count, _ = self.replay_extension_state(
                snapshot.get("extension_state", {}), cursor=snapshot.get("extension_cursor"),
                after_seq=event_sequences[projection["source_event_id"]], events=updates_by_message.get(message_id, []),
            )
            if count:
                snapshot["extension_state"] = state
                snapshot["extension_cursor"] = cursor
        return projections


def load_agent_transcript(agent_id: str, *, base_dir: Path | None = None) -> dict[str, Any]:
    journal = ExecutionJournal(agent_id, base_dir=base_dir)
    events = journal.read_events()
    return {
        "agent_id": agent_id,
        "events": [event.to_dict() for event in events],
        "history": journal.reconstruct_history(),
        "unresolved_tool_uses": journal.unresolved_tool_uses(),
    }
