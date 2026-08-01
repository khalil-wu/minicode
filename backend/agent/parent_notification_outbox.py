"""Durable parent notification outbox for subagent completion delivery.

Modeled after cc's enqueueAgentNotification + pending notification queue:
- durable before notify
- pending / delivered / acked states
- idempotent enqueue and replay
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from backend.config import DATA_ROOT

logger = logging.getLogger(__name__)

OUTBOX_ROOT = DATA_ROOT / "parent_notifications"
NotificationStatus = Literal["pending", "delivered", "acked", "failed"]
# NOTE (concurrency scope): these are per-process threading locks. They serialize
# the load-modify-save cycle of enqueue/_update across threads and asyncio tasks
# within ONE runtime process, which is the only supported deployment today.
# `_atomic_write_json` keeps the on-disk file from ever being torn, but it does
# NOT provide cross-process mutual exclusion: two runtime processes writing the
# same parent outbox concurrently could still lose an update (last writer wins).
# If MiniCode ever runs multiple runtime processes sharing DATA_ROOT, replace
# this with OS file locking (flock) or move the outbox into the SQLite swarm
# store. Left as-is intentionally to avoid a high-risk change to a durable path.
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)


def get_outbox_dir(*, base_dir: Path | None = None) -> Path:
    root = base_dir or OUTBOX_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_parent_outbox_path(parent_key: str, *, base_dir: Path | None = None) -> Path:
    safe = str(parent_key or "unknown").strip().replace("/", "_").replace("\\", "_") or "unknown"
    return get_outbox_dir(base_dir=base_dir) / f"{safe}.json"


@dataclass
class ParentNotification:
    notification_id: str
    parent_run_id: str
    conversation_id: str
    subagent_id: str
    mailbox_epoch: int = 0
    kind: str = "subagent_completed"
    payload: dict[str, Any] = field(default_factory=dict)
    status: NotificationStatus = "pending"
    idempotency_key: str = ""
    created_at_ms: int = field(default_factory=_epoch_ms)
    updated_at_ms: int = field(default_factory=_epoch_ms)
    delivered_at_ms: int | None = None
    acked_at_ms: int | None = None
    attempts: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParentNotification":
        status = str(data.get("status") or "pending")
        if status not in {"pending", "delivered", "acked", "failed"}:
            status = "pending"
        return cls(
            notification_id=str(data.get("notification_id") or uuid4().hex),
            parent_run_id=str(data.get("parent_run_id") or ""),
            conversation_id=str(data.get("conversation_id") or ""),
            subagent_id=str(data.get("subagent_id") or ""),
            mailbox_epoch=int(data.get("mailbox_epoch") or 0),
            kind=str(data.get("kind") or "subagent_completed"),
            payload=dict(data.get("payload") or {}),
            status=status,  # type: ignore[arg-type]
            idempotency_key=str(data.get("idempotency_key") or ""),
            created_at_ms=int(data.get("created_at_ms") or _epoch_ms()),
            updated_at_ms=int(data.get("updated_at_ms") or _epoch_ms()),
            delivered_at_ms=(
                int(data["delivered_at_ms"])
                if data.get("delivered_at_ms") is not None
                else None
            ),
            acked_at_ms=(
                int(data["acked_at_ms"])
                if data.get("acked_at_ms") is not None
                else None
            ),
            attempts=int(data.get("attempts") or 0),
            last_error=str(data.get("last_error") or ""),
        )


class ParentNotificationOutbox:
    """Parent-keyed durable outbox with enqueue / mark-delivered / ack / replay."""

    def __init__(
        self,
        *,
        parent_run_id: str = "",
        conversation_id: str = "",
        base_dir: Path | None = None,
    ) -> None:
        self.parent_run_id = str(parent_run_id or "").strip()
        self.conversation_id = str(conversation_id or "").strip()
        self.base_dir = base_dir
        # Prefer conversation scope so background/detach completions remain
        # visible across later parent turns (new run_id) in the same chat.
        # Fall back to parent_run_id for pure unit tests / no-conversation paths.
        self.parent_key = self.conversation_id or self.parent_run_id or "unknown"
        self.path = get_parent_outbox_path(self.parent_key, base_dir=base_dir)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "parent_run_id": self.parent_run_id,
                "conversation_id": self.conversation_id,
                "notifications": [],
            }
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("outbox root must be object")
            notifications = data.get("notifications")
            if not isinstance(notifications, list):
                notifications = []
            return {
                "parent_run_id": str(data.get("parent_run_id") or self.parent_run_id),
                "conversation_id": str(data.get("conversation_id") or self.conversation_id),
                "notifications": notifications,
            }
        except Exception as exc:
            logger.warning("Failed reading parent outbox %s: %s", self.path, exc)
            return {
                "parent_run_id": self.parent_run_id,
                "conversation_id": self.conversation_id,
                "notifications": [],
            }

    def _save(self, data: dict[str, Any]) -> None:
        payload = {
            "parent_run_id": str(data.get("parent_run_id") or self.parent_run_id),
            "conversation_id": str(data.get("conversation_id") or self.conversation_id),
            "notifications": list(data.get("notifications") or []),
            "updated_at_ms": _epoch_ms(),
        }
        _atomic_write_json(self.path, payload)

    def list_notifications(self, *, status: str | None = None) -> list[ParentNotification]:
        data = self._load()
        items: list[ParentNotification] = []
        for raw in data.get("notifications") or []:
            if not isinstance(raw, dict):
                continue
            item = ParentNotification.from_dict(raw)
            if status and item.status != status:
                continue
            items.append(item)
        items.sort(key=lambda item: (item.created_at_ms, item.notification_id))
        return items

    def enqueue(
        self,
        *,
        subagent_id: str,
        payload: dict[str, Any] | None = None,
        kind: str = "subagent_completed",
        idempotency_key: str = "",
        notification_id: str | None = None,
        mailbox_epoch: int = 0,
    ) -> ParentNotification:
        clean_subagent_id = str(subagent_id or "").strip()
        if not clean_subagent_id:
            raise ValueError("subagent_id is required")
        key = str(idempotency_key or f"{kind}:{clean_subagent_id}").strip()
        lock = _lock_for(self.path)
        with lock:
            data = self._load()
            notifications = [
                ParentNotification.from_dict(raw)
                for raw in data.get("notifications") or []
                if isinstance(raw, dict)
            ]
            for existing in notifications:
                if existing.idempotency_key == key or (
                    existing.subagent_id == clean_subagent_id
                    and existing.kind == kind
                    and existing.status in {"pending", "delivered", "acked"}
                ):
                    return existing
            item = ParentNotification(
                notification_id=str(notification_id or uuid4().hex),
                parent_run_id=self.parent_run_id,
                conversation_id=self.conversation_id,
                subagent_id=clean_subagent_id,
                mailbox_epoch=max(0, int(mailbox_epoch or 0)),
                kind=str(kind or "subagent_completed"),
                payload=dict(payload or {}),
                status="pending",
                idempotency_key=key,
            )
            notifications.append(item)
            data["notifications"] = [entry.to_dict() for entry in notifications]
            self._save(data)
            return item

    def _update(
        self,
        notification_id: str,
        *,
        status: NotificationStatus | None = None,
        error: str | None = None,
        bump_attempt: bool = False,
    ) -> ParentNotification | None:
        lock = _lock_for(self.path)
        with lock:
            data = self._load()
            notifications = [
                ParentNotification.from_dict(raw)
                for raw in data.get("notifications") or []
                if isinstance(raw, dict)
            ]
            target: ParentNotification | None = None
            for item in notifications:
                if item.notification_id != notification_id:
                    continue
                target = item
                now = _epoch_ms()
                if bump_attempt:
                    item.attempts += 1
                if status is not None:
                    item.status = status
                    if status == "delivered":
                        item.delivered_at_ms = now
                    elif status == "acked":
                        item.acked_at_ms = now
                        if item.delivered_at_ms is None:
                            item.delivered_at_ms = now
                if error is not None:
                    item.last_error = str(error)
                item.updated_at_ms = now
                break
            if target is None:
                return None
            data["notifications"] = [entry.to_dict() for entry in notifications]
            self._save(data)
            return target

    def mark_delivered(self, notification_id: str) -> ParentNotification | None:
        return self._update(notification_id, status="delivered", bump_attempt=True)

    def ack(self, notification_id: str) -> ParentNotification | None:
        return self._update(notification_id, status="acked")

    def mark_failed(self, notification_id: str, error: str) -> ParentNotification | None:
        return self._update(notification_id, status="failed", error=error, bump_attempt=True)

    def pending(self) -> list[ParentNotification]:
        return self.list_notifications(status="pending")

    def replayable(self) -> list[ParentNotification]:
        return [
            item
            for item in self.list_notifications()
            if item.status in {"pending", "failed", "delivered"}
        ]


def enqueue_parent_notification(
    *,
    parent_run_id: str = "",
    conversation_id: str = "",
    subagent_id: str,
    payload: dict[str, Any] | None = None,
    kind: str = "subagent_completed",
    idempotency_key: str = "",
    base_dir: Path | None = None,
    mailbox_epoch: int = 0,
) -> ParentNotification:
    outbox = ParentNotificationOutbox(
        parent_run_id=parent_run_id,
        conversation_id=conversation_id,
        base_dir=base_dir,
    )
    return outbox.enqueue(
        subagent_id=subagent_id,
        payload=payload,
        kind=kind,
        idempotency_key=idempotency_key,
        mailbox_epoch=mailbox_epoch,
    )


def load_parent_outbox(
    *,
    parent_run_id: str = "",
    conversation_id: str = "",
    base_dir: Path | None = None,
) -> ParentNotificationOutbox:
    return ParentNotificationOutbox(
        parent_run_id=parent_run_id,
        conversation_id=conversation_id,
        base_dir=base_dir,
    )
