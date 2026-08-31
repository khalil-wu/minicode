"""Durable parent notification outbox for subagent completion delivery.

Modeled after cc's enqueueAgentNotification + pending notification queue:
- durable before notify
- pending / delivered / acked states
- idempotent enqueue and replay
"""

from __future__ import annotations

import json
import logging
import threading
import weakref
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from backend.atomic_io import atomic_write_text, canonical_file_path_key
from backend.agent.runtime_records import epoch_ms
from backend.config import DATA_ROOT
from filelock import FileLock

logger = logging.getLogger(__name__)

OUTBOX_ROOT = DATA_ROOT / "parent_notifications"
NotificationStatus = Literal["pending", "delivered", "acked", "failed"]
# The thread lock covers multiple store instances in one runtime; the adjacent
# FileLock covers separate runtime processes sharing DATA_ROOT. Both are needed:
# atomic replacement prevents torn JSON, while the load-modify-save transaction
# must also exclude a concurrent writer or an idempotent notification can be
# lost.
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()
_ENQUEUE_SUBSCRIBERS: dict[int, weakref.ReferenceType[Any]] = {}
_ENQUEUE_SUBSCRIBERS_GUARD = threading.Lock()
_ENQUEUE_SUBSCRIBER_SEQUENCE = 0
_WAKE_CLAIMS: dict[str, str] = {}
_WAKE_CLAIMS_GUARD = threading.Lock()
MAX_ACTIVE_NOTIFICATIONS = 4096
MAX_ACKED_NOTIFICATION_TOMBSTONES = 512


class ParentNotificationOutboxError(RuntimeError):
    """Base error for a durable parent-notification outbox."""


class ParentNotificationOutboxCorruptionError(ParentNotificationOutboxError):
    """Raised when persisted outbox state cannot be trusted."""


def _lock_for(path: Path) -> threading.Lock:
    key = canonical_file_path_key(path)
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


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
    session_id: str = ""
    mailbox_epoch: int = 0
    kind: str = "subagent_completed"
    payload: dict[str, Any] = field(default_factory=dict)
    status: NotificationStatus = "pending"
    idempotency_key: str = ""
    created_at_ms: int = field(default_factory=epoch_ms)
    updated_at_ms: int = field(default_factory=epoch_ms)
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
            session_id=str(data.get("session_id") or ""),
            subagent_id=str(data.get("subagent_id") or ""),
            mailbox_epoch=int(data.get("mailbox_epoch") or 0),
            kind=str(data.get("kind") or "subagent_completed"),
            payload=dict(data.get("payload") or {}),
            status=status,  # type: ignore[arg-type]
            idempotency_key=str(data.get("idempotency_key") or ""),
            created_at_ms=int(data.get("created_at_ms") or epoch_ms()),
            updated_at_ms=int(data.get("updated_at_ms") or epoch_ms()),
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


def subscribe_parent_notification_enqueued(
    callback: Callable[[ParentNotification], None],
) -> Callable[[], None]:
    """Subscribe to the process-local durable-enqueue signal.

    Claude Code publishes its unified command-queue signal only after the
    notification has entered the queue.  MiniCode keeps the durable outbox as
    the source of truth and mirrors that ordering here: subscribers are merely
    wake hints, while replay always comes from disk.

    Weak references keep a disconnected WebSocket session from being retained
    by the process-wide signal.  The callback must stay non-blocking and hand
    work back to its owning event loop.
    """

    global _ENQUEUE_SUBSCRIBER_SEQUENCE
    if not callable(callback):
        raise TypeError("callback must be callable")
    try:
        reference: weakref.ReferenceType[Any] = weakref.WeakMethod(callback)  # type: ignore[arg-type]
    except TypeError:
        reference = weakref.ref(callback)
    with _ENQUEUE_SUBSCRIBERS_GUARD:
        _ENQUEUE_SUBSCRIBER_SEQUENCE += 1
        token = _ENQUEUE_SUBSCRIBER_SEQUENCE
        _ENQUEUE_SUBSCRIBERS[token] = reference

    def _unsubscribe() -> None:
        with _ENQUEUE_SUBSCRIBERS_GUARD:
            _ENQUEUE_SUBSCRIBERS.pop(token, None)

    return _unsubscribe


def _notify_parent_notification_enqueued(item: ParentNotification) -> None:
    with _ENQUEUE_SUBSCRIBERS_GUARD:
        subscribers = tuple(_ENQUEUE_SUBSCRIBERS.items())
    stale_tokens: list[int] = []
    for token, reference in subscribers:
        callback = reference()
        if callback is None:
            stale_tokens.append(token)
            continue
        try:
            callback(item)
        except Exception:
            # A wake hint must never make the already-durable enqueue fail.
            logger.exception(
                "Parent notification enqueue subscriber failed for %s",
                item.notification_id,
            )
    if stale_tokens:
        with _ENQUEUE_SUBSCRIBERS_GUARD:
            for token in stale_tokens:
                _ENQUEUE_SUBSCRIBERS.pop(token, None)


def claim_parent_notification_wake(
    conversation_id: str,
    owner_token: str,
) -> bool:
    """Reserve one process-local idle wake for a conversation.

    The durable outbox remains authoritative; this is the same role as Claude
    Code's QueryGuard reservation, preventing two WebSocket sessions from
    starting duplicate notification-only queries for the same conversation.
    """

    conversation_id = str(conversation_id or "").strip()
    owner_token = str(owner_token or "").strip()
    if not conversation_id or not owner_token:
        return False
    with _WAKE_CLAIMS_GUARD:
        existing = _WAKE_CLAIMS.get(conversation_id)
        if existing and existing != owner_token:
            return False
        _WAKE_CLAIMS[conversation_id] = owner_token
        return True


def release_parent_notification_wake(
    conversation_id: str,
    owner_token: str,
) -> bool:
    conversation_id = str(conversation_id or "").strip()
    owner_token = str(owner_token or "").strip()
    if not conversation_id or not owner_token:
        return False
    with _WAKE_CLAIMS_GUARD:
        if _WAKE_CLAIMS.get(conversation_id) != owner_token:
            return False
        _WAKE_CLAIMS.pop(conversation_id, None)
        return True


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
        self._process_lock = FileLock(
            str(self.path.with_name(f".{self.path.name}.mutation.lock")),
            timeout=60,
        )

    @contextmanager
    def _locked(self):
        with _lock_for(self.path):
            with self._process_lock:
                yield

    def _empty_data(self) -> dict[str, Any]:
        return {
            "parent_run_id": self.parent_run_id,
            "conversation_id": self.conversation_id,
            "notifications": [],
            "acked_notifications": [],
        }

    def _validate_owner(self, item: ParentNotification) -> None:
        if (
            self.conversation_id
            and item.conversation_id
            and item.conversation_id != self.conversation_id
        ):
            raise ParentNotificationOutboxCorruptionError(
                "Parent notification belongs to a different conversation"
            )
        if (
            not self.conversation_id
            and self.parent_run_id
            and item.parent_run_id
            and item.parent_run_id != self.parent_run_id
        ):
            raise ParentNotificationOutboxCorruptionError(
                "Parent notification belongs to a different parent run"
            )

    def _validate_loaded_data(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ParentNotificationOutboxCorruptionError(
                "Parent notification outbox root must be an object"
            )
        notifications = data.get("notifications", [])
        acked_notifications = data.get("acked_notifications", [])
        if not isinstance(notifications, list) or not isinstance(
            acked_notifications, list
        ):
            raise ParentNotificationOutboxCorruptionError(
                "Parent notification outbox collections must be arrays"
            )
        stored_conversation_id = str(data.get("conversation_id") or "").strip()
        stored_parent_run_id = str(data.get("parent_run_id") or "").strip()
        if (
            self.conversation_id
            and stored_conversation_id
            and stored_conversation_id != self.conversation_id
        ):
            raise ParentNotificationOutboxCorruptionError(
                "Parent notification outbox conversation owner mismatch"
            )
        if (
            not self.conversation_id
            and self.parent_run_id
            and stored_parent_run_id
            and stored_parent_run_id != self.parent_run_id
        ):
            raise ParentNotificationOutboxCorruptionError(
                "Parent notification outbox parent-run owner mismatch"
            )

        seen_ids: set[str] = set()
        seen_idempotency_keys: set[str] = set()
        normalized_active: list[dict[str, Any]] = []
        normalized_acked: list[dict[str, Any]] = []
        for collection, destination in (
            (notifications, normalized_active),
            (acked_notifications, normalized_acked),
        ):
            for raw in collection:
                if not isinstance(raw, dict):
                    raise ParentNotificationOutboxCorruptionError(
                        "Parent notification outbox entry must be an object"
                    )
                notification_id = str(raw.get("notification_id") or "").strip()
                if not notification_id:
                    raise ParentNotificationOutboxCorruptionError(
                        "Parent notification is missing notification_id"
                    )
                if notification_id in seen_ids:
                    raise ParentNotificationOutboxCorruptionError(
                        "Parent notification outbox contains a duplicate notification_id"
                    )
                seen_ids.add(notification_id)
                try:
                    item = ParentNotification.from_dict(raw)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ParentNotificationOutboxCorruptionError(
                        "Parent notification entry is invalid"
                    ) from exc
                self._validate_owner(item)
                if not item.conversation_id and self.conversation_id:
                    item.conversation_id = self.conversation_id
                if not item.parent_run_id and self.parent_run_id:
                    item.parent_run_id = self.parent_run_id
                if item.idempotency_key:
                    if item.idempotency_key in seen_idempotency_keys:
                        raise ParentNotificationOutboxCorruptionError(
                            "Parent notification outbox contains a duplicate idempotency key"
                        )
                    seen_idempotency_keys.add(item.idempotency_key)
                destination.append(item.to_dict())
        return {
            "parent_run_id": stored_parent_run_id or self.parent_run_id,
            "conversation_id": stored_conversation_id or self.conversation_id,
            "notifications": normalized_active,
            "acked_notifications": normalized_acked,
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_data()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return self._empty_data()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ParentNotificationOutboxCorruptionError(
                f"Failed reading parent notification outbox {self.path.name}"
            ) from exc
        return self._validate_loaded_data(data)

    def _save(self, data: dict[str, Any]) -> None:
        active: list[dict[str, Any]] = []
        acked: dict[str, dict[str, Any]] = {}
        for raw in [
            *list(data.get("acked_notifications") or []),
            *list(data.get("notifications") or []),
        ]:
            if not isinstance(raw, dict):
                continue
            item = ParentNotification.from_dict(raw)
            if item.status == "acked":
                acked[item.notification_id] = item.to_dict()
            else:
                active.append(item.to_dict())
        retained_acked = sorted(
            acked.values(),
            key=lambda item: (
                int(item.get("acked_at_ms") or item.get("updated_at_ms") or 0),
                str(item.get("notification_id") or ""),
            ),
        )[-MAX_ACKED_NOTIFICATION_TOMBSTONES:]
        payload = {
            "parent_run_id": str(data.get("parent_run_id") or self.parent_run_id),
            "conversation_id": str(data.get("conversation_id") or self.conversation_id),
            "notifications": active,
            "acked_notifications": retained_acked,
            "updated_at_ms": epoch_ms(),
        }
        _atomic_write_json(self.path, payload)

    def list_notifications(self, *, status: str | None = None) -> list[ParentNotification]:
        data = self._load()
        items: list[ParentNotification] = []
        for raw in [
            *list(data.get("notifications") or []),
            *list(data.get("acked_notifications") or []),
        ]:
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
        session_id: str = "",
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
        requested_notification_id = str(notification_id or "").strip()
        with self._locked():
            data = self._load()
            notifications = [
                ParentNotification.from_dict(raw)
                for raw in [
                    *list(data.get("notifications") or []),
                    *list(data.get("acked_notifications") or []),
                ]
                if isinstance(raw, dict)
            ]
            for existing in notifications:
                if existing.idempotency_key == key:
                    return existing
                if (
                    requested_notification_id
                    and existing.notification_id == requested_notification_id
                ):
                    raise ValueError("notification_id is already used by another notification")
            active_count = sum(item.status != "acked" for item in notifications)
            if active_count >= MAX_ACTIVE_NOTIFICATIONS:
                raise RuntimeError(
                    "Parent notification outbox is full; drain or acknowledge pending notifications"
                )
            item = ParentNotification(
                notification_id=requested_notification_id or uuid4().hex,
                parent_run_id=self.parent_run_id,
                conversation_id=self.conversation_id,
                session_id=str(session_id or "").strip(),
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
        with self._locked():
            data = self._load()
            notifications = [
                ParentNotification.from_dict(raw)
                for raw in [
                    *list(data.get("notifications") or []),
                    *list(data.get("acked_notifications") or []),
                ]
                if isinstance(raw, dict)
            ]
            target: ParentNotification | None = None
            for item in notifications:
                if item.notification_id != notification_id:
                    continue
                target = item
                now = epoch_ms()
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
            # ``delivered`` means appended to an in-memory prompt, not yet
            # consumed by a provider request. It remains replayable until ack.
            if item.status in {"pending", "delivered", "failed"}
        ]

    def delete(self) -> bool:
        """Remove the durable outbox after its owning conversation is deleted."""
        with self._locked():
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False


def enqueue_parent_notification(
    *,
    parent_run_id: str = "",
    conversation_id: str = "",
    session_id: str = "",
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
    item = outbox.enqueue(
        subagent_id=subagent_id,
        session_id=session_id,
        payload=payload,
        kind=kind,
        idempotency_key=idempotency_key,
        mailbox_epoch=mailbox_epoch,
    )
    # Durability precedes observability.  The process-local signal is only a
    # CC-style queue recheck; consumers still inspect replayable outbox state
    # before starting work, so duplicate idempotent enqueues remain harmless.
    _notify_parent_notification_enqueued(item)
    return item


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
