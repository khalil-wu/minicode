from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_text, canonical_file_path_key
from backend.secret_redaction import (
    is_sensitive_field_name,
    redact_json_secrets,
    redact_secrets,
)
from filelock import FileLock

logger = logging.getLogger(__name__)

WS_REPLAY_MAX_STRING_CHARS = 16_000
WS_REPLAY_MAX_LIST_ITEMS = 200
WS_REPLAY_MAX_DICT_ITEMS = 200
WS_REPLAY_DATA_URL_OMITTED = "[data URL omitted from websocket replay]"
WS_REPLAY_LIST_TRUNCATED = "[list truncated for websocket replay]"
WS_REPLAY_DICT_TRUNCATED = "[object truncated for websocket replay]"
# Claude Code uses a 30-day default for persisted session and derived-log
# cleanup. Replay files are reconnect state, not conversation history, so the
# same lifecycle keeps abandoned renderer sessions from accumulating forever.
WS_REPLAY_RETENTION_SECONDS = 30 * 24 * 60 * 60

_OMIT = object()
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}

_RAW_PROVIDER_REASONING_TYPES = frozenset(
    {
        "reasoning_text",
        "reasoning_content",
        "raw_reasoning",
        "raw_provider_reasoning",
        "thinking",
        "thinking_delta",
    }
)

def _path_lock(path: Path) -> threading.RLock:
    key = canonical_file_path_key(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _process_lock(path: Path) -> FileLock:
    return FileLock(str(path.with_name(f".{path.name}.mutation.lock")), timeout=60)


def _is_data_url(value: str) -> bool:
    return value[:5].lower() == "data:"


def is_raw_provider_reasoning_event(payload: Any) -> bool:
    """Identify pre-alignment websocket events that exposed provider reasoning.

    Current MiniCode only projects provider ``reasoning_summary_text`` frames.
    Older builds persisted raw Responses/Anthropic/MiniCode reasoning deltas in the
    reconnect log, including some with timeline visibility.  Treat only their
    explicit legacy markers as raw so ordinary reasoning-summary events remain
    replayable.
    """

    if not isinstance(payload, dict):
        return False
    if str(payload.get("type") or "").strip().lower() not in {
        "thinking",
        "thinking_delta",
    }:
        return False
    if bool(
        payload.get("is_raw_provider_reasoning")
    ):
        return True
    visibility = str(payload.get("visibility") or "").strip().lower()
    if visibility in {"hidden", "internal", "redacted"}:
        return True
    reasoning_type = str(
        payload.get("provider_reasoning_type")
        or payload.get("providerReasoningType")
        or ""
    ).strip().lower()
    return reasoning_type in _RAW_PROVIDER_REASONING_TYPES


def _truncate_replay_string(value: str, path: str, truncated_fields: list[str]) -> str:
    if len(value) <= WS_REPLAY_MAX_STRING_CHARS:
        return value
    truncated_fields.append(path)
    omitted = len(value) - WS_REPLAY_MAX_STRING_CHARS
    return (
        value[:WS_REPLAY_MAX_STRING_CHARS]
        + f"\n[... websocket replay truncated {omitted} chars ...]"
    )


def sanitize_ws_live_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the secret-redacted payload used for live renderer delivery.

    Runtime ownership fences and credential-shaped fields are omitted by exact
    field name. Other strings retain their original length and structure, so
    large tool output and data URLs continue to satisfy the live UI contract.
    """

    sanitized = redact_json_secrets(
        dict(payload),
        preserve_data_urls=True,
    )
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_replay_value(
    value: Any,
    path: str,
    *,
    omitted_fields: list[str],
    truncated_fields: list[str],
    omit_dict_values: bool,
) -> Any:
    if isinstance(value, str):
        if _is_data_url(value):
            omitted_fields.append(path)
            return _OMIT if omit_dict_values else WS_REPLAY_DATA_URL_OMITTED
        return _truncate_replay_string(
            redact_secrets(value),
            path,
            truncated_fields,
        )

    if isinstance(value, list):
        result: list[Any] = []
        for index, item in enumerate(value[:WS_REPLAY_MAX_LIST_ITEMS]):
            item_path = f"{path}[{index}]"
            sanitized = _sanitize_replay_value(
                item,
                item_path,
                omitted_fields=omitted_fields,
                truncated_fields=truncated_fields,
                omit_dict_values=False,
            )
            result.append(WS_REPLAY_DATA_URL_OMITTED if sanitized is _OMIT else sanitized)
        if len(value) > WS_REPLAY_MAX_LIST_ITEMS:
            truncated_fields.append(f"{path}[{WS_REPLAY_MAX_LIST_ITEMS}:]")
            result.append(WS_REPLAY_LIST_TRUNCATED)
        return result

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:WS_REPLAY_MAX_DICT_ITEMS]:
            key_text = redact_secrets(str(key))
            item_path = f"{path}.{key_text}" if path else key_text
            if is_sensitive_field_name(key):
                omitted_fields.append(item_path)
                continue
            sanitized = _sanitize_replay_value(
                item,
                item_path,
                omitted_fields=omitted_fields,
                truncated_fields=truncated_fields,
                omit_dict_values=True,
            )
            if sanitized is _OMIT:
                continue
            result[key_text] = sanitized
        if len(items) > WS_REPLAY_MAX_DICT_ITEMS:
            truncated_fields.append(f"{path}.*" if path else "*")
            result["__replay_truncated__"] = WS_REPLAY_DICT_TRUNCATED
        return result

    return value


def sanitize_ws_replay_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a replay-log-safe copy of a websocket event payload.

    Live websocket delivery may include large binary/data-url fields. The replay
    log is long-lived JSONL, so it stores enough metadata to recover ordering and
    UI state without retaining unbounded binary strings or huge tool output.
    """

    omitted_fields: list[str] = []
    truncated_fields: list[str] = []
    root = dict(payload)

    if root.get("type") == "image_chunk" and isinstance(root.get("image_data"), str):
        image_data = str(root.pop("image_data"))
        omitted_fields.append("image_data")
        root["image_data_omitted"] = True
        root["image_data_size"] = len(image_data)

    sanitized = _sanitize_replay_value(
        root,
        "",
        omitted_fields=omitted_fields,
        truncated_fields=truncated_fields,
        omit_dict_values=True,
    )
    if not isinstance(sanitized, dict):
        sanitized = dict(payload)

    existing_omitted = sanitized.get("replay_omitted_fields")
    if isinstance(existing_omitted, list):
        omitted_fields.extend(str(item) for item in existing_omitted)
    existing_truncated = sanitized.get("replay_truncated_fields")
    if isinstance(existing_truncated, list):
        truncated_fields.extend(str(item) for item in existing_truncated)

    if omitted_fields:
        sanitized["replay_omitted_fields"] = sorted(set(omitted_fields))
    if truncated_fields:
        sanitized["replay_truncated_fields"] = sorted(set(truncated_fields))
    return sanitized


@dataclass(frozen=True, slots=True)
class ReplayLogReadStatus:
    """What the last read of a replay log lost, if anything."""

    unreadable: bool = False
    error: str = ""
    dropped_lines: int = 0

    @property
    def degraded(self) -> bool:
        return self.unreadable or self.dropped_lines > 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.unreadable:
            payload["replay_log_unreadable"] = True
            if self.error:
                payload["replay_log_error"] = self.error
        if self.dropped_lines:
            payload["replay_log_dropped_lines"] = self.dropped_lines
        return payload


class WebSocketReplayEventStore:
    """Small JSONL-backed replay log for conversation-scoped websocket events."""

    def __init__(self, *, session_id: str, root_dir: Path) -> None:
        self.session_id = session_id
        self.root_dir = Path(root_dir)
        safe_session_id = re.sub(r"[^A-Za-z0-9_-]+", "_", session_id).strip("_") or "session"
        self.path = self.root_dir / f"{safe_session_id}.jsonl"
        self._lock = _path_lock(self.path)
        self._file_lock = _process_lock(self.path)
        self.read_status = ReplayLogReadStatus()

    @contextmanager
    def _locked(self):
        with self._lock:
            with self._file_lock:
                yield

    def load(self, *, limit: int) -> list[dict[str, Any]]:
        """Pure read of the last ``limit`` events.

        cc transcripts are append-only, codex opens rollouts ``read+append``
        (recorder.rs open_rollout_for_append), and pi's export never touches
        the session. Reads must never rewrite the log: the live append path
        already compacts to ``WS_EVENT_REPLAY_MAX`` via explicit ``rewrite``
        calls, and a caller-supplied smaller limit (e.g. the replay export
        endpoint) must not irreversibly destroy earlier events.
        """

        if limit <= 0 or not self.path.exists():
            return []

        with self._locked():
            events, _should_rewrite = self._read_unlocked()
            return events[-limit:]

    def append(self, payload: dict[str, Any]) -> None:
        if is_raw_provider_reasoning_event(payload):
            return
        with self._locked():
            self.root_dir.mkdir(parents=True, exist_ok=True)
            safe_payload = sanitize_ws_replay_payload(payload)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

    def rewrite(self, events: list[dict[str, Any]]) -> None:
        with self._locked():
            self._rewrite_unlocked(events)

    def delete_for_conversation(self, conversation_id: str) -> int:
        """Remove replay records owned by a hard-deleted conversation."""

        owner = str(conversation_id or "").strip()
        if not owner or not self.path.exists():
            return 0
        with self._locked():
            events, should_rewrite = self._read_unlocked()
            retained = [
                event
                for event in events
                if str(event.get("conversation_id") or "").strip() != owner
            ]
            removed = len(events) - len(retained)
            if self.read_status.degraded:
                # Rewriting keeps only the lines that parsed, so compacting now
                # would permanently erase the corrupt evidence while returning
                # a count that tells the caller nothing happened.
                logger.error(
                    "Refusing to compact a degraded replay log for %s: %s",
                    self.session_id,
                    self.read_status.to_payload(),
                )
                return removed
            if removed or should_rewrite:
                self._rewrite_unlocked(retained)
            return removed

    def _read_unlocked(self) -> tuple[list[dict[str, Any]], bool]:
        """Parse the log, recording any evidence loss on ``self.read_status``.

        A failed or partial read must never look like an empty log: the replay
        export reported ``can_replay_without_gap: true`` over an unreadable
        file, and session init reset the replay sequence counter to 0 so new
        events reused sequence numbers the client already held.
        """
        events: list[dict[str, Any]] = []
        should_rewrite = False
        dropped_lines = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        should_rewrite = True
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Dropping malformed websocket replay log line in %s",
                            self.path,
                        )
                        dropped_lines += 1
                        should_rewrite = True
                        continue
                    if not isinstance(payload, dict):
                        logger.warning(
                            "Dropping non-object websocket replay log line in %s",
                            self.path,
                        )
                        dropped_lines += 1
                        should_rewrite = True
                        continue
                    if is_raw_provider_reasoning_event(payload):
                        should_rewrite = True
                        continue
                    sanitized = sanitize_ws_replay_payload(payload)
                    if sanitized != payload:
                        should_rewrite = True
                    events.append(sanitized)
        except OSError as exc:
            logger.error(
                "Failed to load websocket replay log for %s: %s",
                self.session_id,
                exc,
                exc_info=True,
            )
            self.read_status = ReplayLogReadStatus(unreadable=True, error=str(exc))
            # Never rewrite from a failed read: that would erase the log.
            return [], False
        self.read_status = ReplayLogReadStatus(dropped_lines=dropped_lines)
        return events, should_rewrite

    def _rewrite_unlocked(self, events: list[dict[str, Any]]) -> None:
        lines: list[str] = []
        for payload in events:
            if is_raw_provider_reasoning_event(payload):
                continue
            safe_payload = sanitize_ws_replay_payload(payload)
            lines.append(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")))
        atomic_write_text(self.path, "".join(f"{line}\n" for line in lines))


def delete_replay_events_for_conversation(
    root_dir: Path,
    conversation_id: str,
    *,
    exclude_paths: set[Path] | None = None,
) -> int:
    """Purge a conversation from every renderer-session replay file."""

    owner = str(conversation_id or "").strip()
    root = Path(root_dir)
    if not owner or not root.exists():
        return 0
    excluded = {
        path.resolve(strict=False)
        for path in (exclude_paths or set())
    }
    removed = 0
    for path in root.glob("*.jsonl"):
        if path.resolve(strict=False) in excluded:
            continue
        store = WebSocketReplayEventStore(session_id=path.stem, root_dir=root)
        try:
            removed += store.delete_for_conversation(owner)
        except OSError as exc:
            logger.debug("Failed to purge deleted conversation from %s: %s", path, exc)
    return removed


def cleanup_stale_replay_logs(
    root_dir: Path,
    *,
    now: float | None = None,
    max_age_seconds: float = WS_REPLAY_RETENTION_SECONDS,
) -> int:
    """Delete abandoned renderer replay files using CC's session retention."""

    root = Path(root_dir)
    if not root.exists() or max_age_seconds <= 0:
        return 0
    cutoff = float(time.time() if now is None else now) - float(max_age_seconds)
    removed = 0
    for path in root.glob("*.jsonl"):
        lock = _path_lock(path)
        with lock, _process_lock(path):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.debug("Failed to clean stale websocket replay log %s: %s", path, exc)
    return removed
