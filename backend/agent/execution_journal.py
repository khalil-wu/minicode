"""Append-only sidechain execution journal for subagent runs.

Mirrors cc's sidechain transcript model:
- ordered facts, not just latest checkpoint snapshots
- durable JSONL under DATA_ROOT
- resume helpers that reconstruct history and unresolved tool uses
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from backend.config import DATA_ROOT

logger = logging.getLogger(__name__)

JOURNAL_ROOT = DATA_ROOT / "sidechains"
_JOURNAL_SCHEMA_VERSION = 1
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

EVENT_TYPES = frozenset({
    "user_prompt",
    "assistant",
    "tool_use",
    "tool_result",
    "progress",
    "system",
    "terminal",
})


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def get_journal_dir(agent_id: str, *, base_dir: Path | None = None) -> Path:
    root = base_dir or JOURNAL_ROOT
    path = root / str(agent_id or "").strip()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_journal_path(agent_id: str, *, base_dir: Path | None = None) -> Path:
    return get_journal_dir(agent_id, base_dir=base_dir) / "events.jsonl"


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
        self.agent_id = str(agent_id or "").strip()
        if not self.agent_id:
            raise ValueError("agent_id is required")
        self.base_dir = base_dir
        self.path = get_journal_path(self.agent_id, base_dir=base_dir)
        self._seq = self._load_last_seq()

    def _load_last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last_seq = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    last_seq = max(last_seq, int(data.get("seq") or 0))
        except OSError as exc:
            logger.warning("Failed reading journal seq for %s: %s", self.agent_id, exc)
        return last_seq

    def append(
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
        lock = _lock_for(self.path)
        with lock:
            self._seq += 1
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
            return event

    def read_events(self) -> list[JournalEvent]:
        if not self.path.exists():
            return []
        events: list[JournalEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt journal line in %s", self.path)
                    continue
                if not isinstance(data, dict):
                    continue
                events.append(JournalEvent.from_dict(data))
        events.sort(key=lambda item: (item.seq, item.ts_ms))
        return events

    def reconstruct_history(self) -> list[dict[str, Any]]:
        """Rebuild provider-shaped history from ordered journal facts."""
        history: list[dict[str, Any]] = []
        for event in self.read_events():
            payload = event.payload
            if event.event_type == "user_prompt":
                content = str(payload.get("content") or payload.get("prompt") or "").strip()
                if content:
                    history.append({"role": "user", "content": content})
                continue
            if event.event_type == "assistant":
                content = str(payload.get("content") or payload.get("text") or "")
                tool_calls = payload.get("tool_calls")
                message: dict[str, Any] = {"role": "assistant", "content": content}
                if isinstance(tool_calls, list) and tool_calls:
                    message["tool_calls"] = tool_calls
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
                continue
            if event.event_type == "tool_result":
                call_id = str(payload.get("tool_call_id") or payload.get("call_id") or "").strip()
                if not call_id:
                    continue
                history.append(
                    {
                        "role": "tool",
                        "content": str(payload.get("content") or ""),
                        "name": str(payload.get("tool_name") or payload.get("name") or "tool"),
                        "tool_call_id": call_id,
                    }
                )
                continue
            if event.event_type == "system":
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
        message = content or (
            f"[Tool call aborted because the run was {reason}. "
            "Do not retry the same call blindly; use retained evidence or a different approach.]"
        )
        for item in self.unresolved_tool_uses():
            closed.append(
                self.append(
                    "tool_result",
                    {
                        "tool_call_id": item["tool_call_id"],
                        "tool_name": item["tool_name"],
                        "content": message,
                        "status": reason,
                        "synthetic": True,
                    },
                    parent_event_id=str(item.get("event_id") or ""),
                )
            )
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
        "path": str(journal.path),
    }
