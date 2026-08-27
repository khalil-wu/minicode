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
import time
from collections.abc import Mapping
from copy import deepcopy
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from backend.agent.checkpoint import validate_storage_id
from backend.atomic_io import canonical_file_path_key
from backend.config import DATA_ROOT
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
_JOURNAL_SCHEMA_VERSION = 1
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
    "artifact_id",
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


def _epoch_ms() -> int:
    return int(time.time() * 1000)


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
    ts_ms: int = field(default_factory=_epoch_ms)
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
            ts_ms=int(data.get("ts_ms") or _epoch_ms()),
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
        self.unacknowledged_tail_records = 0
        key = canonical_file_path_key(self.path)
        with self._locked():
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

    def _load_last_seq(self) -> int:
        """Read the durable tail's sequence without re-validating the journal.

        ``append`` only needs the last allocated sequence. Parsing and
        validating every record here made each append cost O(journal size) and
        a whole run quadratic, synchronously on the event loop. Full record
        validation still happens in ``read_events()``, which is what recovery
        and reconstruction read.

        A trailing line that does not parse is an unacknowledged partial append
        (the process died between the write and the fsync), not evidence that
        the journal is unusable. This runs from ``__init__`` on every turn and
        no backend caller handles ExecutionJournalCorruptionError, so raising
        here bricked the conversation permanently. Skip back to the newest
        record that does parse and record the skipped tail so the loss stays
        visible; ``read_events()`` still refuses a genuinely corrupt journal.
        """
        if not self.path.exists():
            return 0
        try:
            lines = _tail_lines(self.path)
        except OSError as exc:
            raise ExecutionJournalError(
                f"Failed reading execution journal for {self.agent_id}"
            ) from exc
        skipped = 0
        for line in reversed(lines):
            text = line.strip()
            if not text:
                continue
            try:
                seq = int(json.loads(text)["seq"])
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                skipped += 1
                continue
            if seq < 1:
                skipped += 1
                continue
            if skipped:
                self.unacknowledged_tail_records = skipped
                logger.error(
                    "Execution journal for %s ends in %d unreadable record(s); "
                    "resuming from seq %d. The partial tail is left on disk.",
                    self.agent_id,
                    skipped,
                    seq,
                )
            return seq
        if skipped:
            self.unacknowledged_tail_records = skipped
            logger.error(
                "Execution journal for %s has no readable record in its tail "
                "(%d unreadable line(s)); allocating from 0.",
                self.agent_id,
                skipped,
            )
        return 0


    def _read_events_unlocked(self) -> list[JournalEvent]:
        if not self.path.exists():
            return []
        events: list[JournalEvent] = []
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
                events.append(JournalEvent.from_dict(data))
        return events

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
        self._seq = max(self._seq, _WRITE_SEQUENCES.get(key, 0), durable_seq) + 1
        _WRITE_SEQUENCES[key] = self._seq
        event = JournalEvent(
            event_type=clean_type,
            agent_id=self.agent_id,
            payload=dict(payload or {}),
            event_id=str(event_id or uuid4().hex),
            seq=self._seq,
            ts_ms=int(ts_ms or _epoch_ms()),
            parent_event_id=str(parent_event_id or ""),
        )
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        durable_events.append(event)
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
    ) -> dict[str, tuple[str, JournalEvent]]:
        """Return the latest open/closed state for each provider call id."""

        states: dict[str, tuple[str, JournalEvent]] = {}
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
            states = self._tool_lifecycle_states(self._validated_events_unlocked())
            state = states.get(call_id)
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
            events = self._validated_events_unlocked()
            state = self._tool_lifecycle_states(events).get(call_id)
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

    def reconstruct_history(self) -> list[dict[str, Any]]:
        """Rebuild provider-shaped history from ordered journal facts."""
        events = self.read_events()
        history: list[dict[str, Any]] = []
        start_index = 0
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
            break
        else:
            pending_tool_call_ids = set()

        for event in events[start_index:]:
            payload = event.payload
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
                if isinstance(tool_call, dict) and tool_call.get("id"):
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
        return self.append("system", data)

    def pending_conversation_projections(self) -> list[JournalEvent]:
        """Return terminal conversation projections without commit receipts."""

        pending: dict[str, JournalEvent] = {}
        committed: set[str] = set()
        for event in self.read_events():
            lifecycle = str(event.payload.get("lifecycle") or "")
            if lifecycle == "conversation_projection_pending":
                pending[event.event_id] = event
            elif lifecycle == "conversation_projection_committed":
                pending_id = str(event.payload.get("pending_event_id") or "").strip()
                if pending_id:
                    committed.add(pending_id)
        return [
            event
            for event_id, event in pending.items()
            if event_id not in committed
        ]

    def unprojected_terminal_projections(self) -> list[dict[str, Any]]:
        """Build replay payloads only for runtime-committed terminal facts.

        This covers the crash window after the runtime terminal CAS but before
        the WebSocket owner could append its richer conversation projection.
        """

        events = self.read_events()
        covered_ids: set[str] = set()
        for event in events:
            lifecycle = str(event.payload.get("lifecycle") or "")
            if lifecycle == "conversation_projection_pending":
                message = event.payload.get("assistant_message")
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
        return projections


def record_sidechain_events(
    agent_id: str,
    events: Iterable[dict[str, Any] | JournalEvent],
    *,
    base_dir: Path | None = None,
) -> list[JournalEvent]:
    journal = ExecutionJournal(agent_id, base_dir=base_dir)
    recorded: list[JournalEvent] = []
    for item in events:
        if isinstance(item, JournalEvent):
            recorded.append(
                journal.append(
                    item.event_type,
                    item.payload,
                    parent_event_id=item.parent_event_id,
                    event_id=item.event_id,
                    ts_ms=item.ts_ms,
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("event_type") or item.get("type") or "system")
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = {
                key: value
                for key, value in item.items()
                if key not in {"event_type", "type", "parent_event_id", "event_id", "ts_ms"}
            }
        recorded.append(
            journal.append(
                event_type,
                payload,
                parent_event_id=str(item.get("parent_event_id") or ""),
                event_id=str(item.get("event_id") or "") or None,
                ts_ms=item.get("ts_ms"),
            )
        )
    return recorded


def load_agent_transcript(agent_id: str, *, base_dir: Path | None = None) -> dict[str, Any]:
    journal = ExecutionJournal(agent_id, base_dir=base_dir)
    events = journal.read_events()
    return {
        "agent_id": agent_id,
        "events": [event.to_dict() for event in events],
        "history": journal.reconstruct_history(),
        "unresolved_tool_uses": journal.unresolved_tool_uses(),
    }
