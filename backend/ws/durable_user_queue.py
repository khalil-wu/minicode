"""Crash-recoverable queue for user follow-up commands.

The active agent turn is intentionally not resumed after a process crash, but
queued user work must not disappear with the WebSocket object.  This tiny
JSON-backed store uses an inflight slot so a command is either still queued or
replayed after an interrupted dispatch.
"""

from __future__ import annotations

import json
import hashlib
import math
import logging
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from filelock import FileLock, Timeout

from backend.agent.message import UserCommand
from backend.agent.turn_input import ScheduledInputCommand
from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.ws.client_command_log import _clean_command_id

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 7
_OWNER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_") or "session"


def _queue_locked(method):
    """Keep in-memory transitions and their durable publish in one critical section."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._locked():
            return method(self, *args, **kwargs)

    return wrapped


@dataclass
class _DurableQueueState:
    queues: dict[str, list[UserCommand]]
    inflight: dict[str, UserCommand]
    turn_inputs: dict[str, list[UserCommand]]
    client_pending: list[UserCommand]
    client_inflight: dict[str, UserCommand]
    inflight_owners: dict[str, str]
    turn_input_owners: dict[str, str]
    client_inflight_owners: dict[str, str]
    migrated_sources: set[str]
    paused_conversations: dict[str, str]
    client_cancelled: dict[str, dict[str, Any]]

    @classmethod
    def empty(cls) -> "_DurableQueueState":
        return cls({}, {}, {}, [], {}, {}, {}, {}, set(), {}, {})


class DurableUserMessageQueue:
    @staticmethod
    def queue_key_for_session(session_id: str) -> str:
        return _safe(session_id)

    def __init__(self, *, session_id: str, root_dir: Path, shared_inputs: bool = False) -> None:
        self._session_key = self.queue_key_for_session(session_id)
        self._shared_inputs = shared_inputs
        self.path = Path(root_dir) / f"{self._session_key}.json"
        self._cancelled_receipt_dir = Path(root_dir) / "cancelled-inputs" / self._session_key
        self._lock = threading.RLock()
        self._owner_id = uuid4().hex
        self._lease_dir = self.path.parent / ".owner-leases" / self._session_key
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        self._owner_lease = FileLock(
            str(self._lease_path(self._owner_id)),
            timeout=0,
            thread_local=False,
        )
        self._owner_lease.acquire(timeout=0)
        self._closed = False
        self._queues: dict[str, list[UserCommand]] = {}
        self._inflight: dict[str, UserCommand] = {}
        self._turn_inputs: dict[str, list[UserCommand]] = {}
        self._client_pending: list[UserCommand] = []
        self._client_inflight: dict[str, UserCommand] = {}
        self._client_cancelled: dict[str, dict[str, Any]] = {}
        self._inflight_owners: dict[str, str] = {}
        self._turn_input_owners: dict[str, str] = {}
        self._client_inflight_owners: dict[str, str] = {}
        self._migrated_sources: set[str] = set()
        self._paused_conversations: dict[str, str] = {}
        self._owner_queues: dict[str, list[UserCommand]] = {}
        self._owner_inflight: dict[str, UserCommand] = {}
        self._owner_turn_inputs: dict[str, list[UserCommand]] = {}
        # Structured evidence for the most recent durable read failure.  A
        # corrupt queue file must never silently masquerade as "no queued
        # work"; callers and diagnostics read this instead.
        self.last_load_error: dict[str, Any] | None = None

    @property
    def load_error(self) -> dict[str, Any] | None:
        """Evidence for the latest corrupt/unreadable durable state read."""
        return self.last_load_error

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def _lease_path(self, owner_id: str) -> Path:
        return self._lease_dir / f"{owner_id}.lock"

    def _ensure_open(self) -> None:
        if self._closed or not self._owner_lease.is_locked:
            raise RuntimeError("durable user queue owner lease is closed")

    def close(self) -> None:
        """Release this runtime owner's liveness lease without rewriting state.

        Any owned inflight entries remain durable.  A later owner can then
        mechanically prove that this lease is no longer held and recover them.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._owner_lease.release(force=True)
            except Exception:
                logger.exception(
                    "Unable to release durable queue owner lease %s",
                    self._owner_id,
                )


    def _owner_is_live_unlocked(self, owner_id: str) -> bool:
        owner = str(owner_id or "").strip()
        if not _OWNER_ID_PATTERN.fullmatch(owner):
            return False
        if owner == self._owner_id:
            return not self._closed and self._owner_lease.is_locked

        probe = FileLock(str(self._lease_path(owner)), timeout=0)
        try:
            probe.acquire(timeout=0)
        except Timeout:
            return True
        except Exception as exc:
            # Duplicate execution is worse than delayed recovery.  If the OS
            # cannot prove the foreign lease is free, preserve its ownership.
            logger.warning(
                "Unable to verify durable queue owner lease %s: %s",
                owner,
                exc,
            )
            return True
        else:
            probe.release(force=True)
            return False

    @contextmanager
    def _locked(self):
        """Serialize one queue owner across threads and MiniCode processes."""

        with self._lock:
            self._ensure_open()
            with file_mutation_locks([self.path]):
                yield

    @staticmethod
    def _command_to_dict(command: Any) -> dict[str, Any] | None:
        if not isinstance(command, UserCommand) or command.type != "user_message":
            return None
        # Underscore keys carry routing/private metadata; only drop the ones
        # holding non-serializable runtime handles instead of stripping every
        # one (crash recovery used to silently lose the serializable rest).
        data: dict[str, Any] = {}
        for key, value in dict(command.data or {}).items():
            if str(key).startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    continue
            data[str(key)] = value
        try:
            json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning("Skipping non-serializable queued user command")
            return None
        result = {"type": command.type, "data": data}
        if isinstance(command, ScheduledInputCommand):
            result["host_scheduled_input"] = {
                "turn_trigger": command.turn_trigger, "public_content": command.public_content,
                "task_id": command.task_id, "scheduled_run_id": command.scheduled_run_id,
            }
        return result

    @staticmethod
    def _dict_to_command(payload: Any) -> UserCommand | None:
        if not isinstance(payload, dict) or payload.get("type") != "user_message":
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        host_input = payload.get("host_scheduled_input")
        if host_input is not None:
            return ScheduledInputCommand(type="user_message", data=dict(data), **host_input)
        return UserCommand(type="user_message", data=dict(data))

    @staticmethod
    def _client_command_to_dict(command: Any) -> dict[str, Any] | None:
        if not isinstance(command, UserCommand):
            return None
        data = {
            str(key): value
            for key, value in dict(command.data or {}).items()
            if not str(key).startswith("_")
        }
        if command.type == "user_message" and command.data.get("_legacy_user_message_identity") is True:
            data["_legacy_user_message_identity"] = True
        if command.type == "interrupt" and "_user_stop_target" in command.data:
            data["_user_stop_target"] = command.data["_user_stop_target"]
        command_id = _clean_command_id(data.get("client_command_id"))
        if not command_id:
            return None
        data["client_command_id"] = command_id
        try:
            json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning("Skipping non-serializable client command %s", command_id)
            return None
        return {"type": command.type, "data": data}

    @staticmethod
    def _dict_to_client_command(payload: Any) -> UserCommand | None:
        if not isinstance(payload, dict):
            return None
        command_type = str(payload.get("type") or "").strip()
        data = payload.get("data")
        if not command_type or not isinstance(data, dict):
            return None
        command_id = _clean_command_id(data.get("client_command_id"))
        if not command_id:
            return None
        normalized = dict(data)
        normalized["client_command_id"] = command_id
        return UserCommand(type=command_type, data=normalized)

    def load(self) -> tuple[dict[str, list[UserCommand]], dict[str, UserCommand]]:
        with self._locked():
            return self._load_unlocked()

    def _load_unlocked(self) -> tuple[dict[str, list[UserCommand]], dict[str, UserCommand]]:
        self._refresh_from_disk_unlocked()
        self._owner_queues = {
            conversation_id: list(commands)
            for conversation_id, commands in self._queues.items()
        }
        self._owner_inflight = {
            conversation_id: command
            for conversation_id, command in self._inflight.items()
            if self._inflight_owners.get(conversation_id) == self._owner_id
        }
        self._owner_turn_inputs = {
            conversation_id: list(commands)
            for conversation_id, commands in self._turn_inputs.items()
            if self._turn_input_owners.get(conversation_id) == self._owner_id
        }
        return (
            {
                conversation_id: list(commands)
                for conversation_id, commands in self._queues.items()
            },
            {},
        )

    def _quarantine_corrupt_file_unlocked(self, reason: str, detail: str) -> None:
        """Preserve an unreadable queue file as evidence instead of overwriting it.

        The next ``_write_current_unlocked`` would otherwise destroy the only
        copy of the queued commands.  Renaming under the file lock keeps the
        failed state inspectable and prevents silent data loss.
        """
        evidence: dict[str, Any] = {
            "path": str(self.path),
            "reason": reason,
            "detail": detail,
            "owner_id": self._owner_id,
        }
        try:
            timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            quarantine_path = self.path.with_name(
                f"{self.path.name}.corrupt-{timestamp}-{uuid4().hex[:8]}"
            )
            self.path.replace(quarantine_path)
            evidence["quarantined_to"] = str(quarantine_path)
        except OSError as exc:
            evidence["quarantine_error"] = str(exc)
        self.last_load_error = evidence
        logger.error(
            "Durable user queue %s is unreadable (%s: %s); quarantined to %s. "
            "Queued commands in this file are preserved but NOT loaded.",
            self.path,
            reason,
            detail,
            evidence.get("quarantined_to", "<quarantine failed>"),
        )

    @staticmethod
    def _valid_cancelled_receipt(command_id: str, record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        command = DurableUserMessageQueue._dict_to_client_command(record.get("command"))
        cancelled_at = record.get("cancelled_at")
        return bool(
            command is not None and command.type == "user_message"
            and DurableUserMessageQueue._command_id(command) == command_id
            and isinstance(record.get("reason"), str)
            and isinstance(cancelled_at, (int, float)) and not isinstance(cancelled_at, bool)
            and math.isfinite(cancelled_at)
        )

    def _cancelled_receipt_path(self, command_id: str, client_queue_key: str = "") -> Path:
        filename = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
        if self._shared_inputs and client_queue_key:
            if _safe(client_queue_key) != client_queue_key:
                raise ValueError("A Stop receipt has an invalid original window identity")
            return self._cancelled_receipt_dir / client_queue_key / f"{filename}.json"
        return self._cancelled_receipt_dir / f"{filename}.json"

    def _cancelled_receipt_unlocked(self, command_id: str, client_queue_key: str = "") -> dict[str, Any] | None:
        if not command_id:
            return None
        path = self._cancelled_receipt_path(command_id, client_queue_key)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not self._shared_inputs or not client_queue_key:
                return None
            # A supported earlier build wrote shared receipts in a flat CID
            # namespace. Read only the record proven to belong to this origin.
            legacy = self._cancelled_receipt_unlocked(command_id)
            return legacy if legacy is not None and legacy.get("client_queue_key") == client_queue_key else None
        if not self._valid_cancelled_receipt(command_id, record):
            raise ValueError(f"Invalid durable Stop receipt: {path}")
        if self._shared_inputs and client_queue_key and record.get("client_queue_key") != client_queue_key:
            raise ValueError("A Stop receipt is bound to another original window")
        return record

    @staticmethod
    def _same_cancelled_receipt(first: dict[str, Any], second: dict[str, Any], origin: str) -> bool:
        # Old window receipts did not carry the producer key; their archive
        # namespace supplies it. Original command/reason/time stay immutable.
        return all(record.get("client_queue_key", origin) == origin for record in (first, second)) and {
            key: value for key, value in first.items() if key != "client_queue_key"
        } == {key: value for key, value in second.items() if key != "client_queue_key"}

    def _archive_cancelled_receipt_unlocked(self, command_id: str, record: dict[str, Any]) -> None:
        origin = str(record.get("client_queue_key") or "")
        existing = self._cancelled_receipt_unlocked(command_id, origin)
        if existing is not None:
            if not self._same_cancelled_receipt(existing, record, origin):
                raise ValueError(f"Conflicting durable Stop receipts for {command_id}")
            return
        atomic_write_text(
            self._cancelled_receipt_path(command_id, origin),
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        )

    def _read_state_unlocked(self) -> _DurableQueueState:
        if not self.path.exists():
            return _DurableQueueState.empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as exc:
            self._quarantine_corrupt_file_unlocked(
                "malformed_json",
                str(exc),
            )
            return _DurableQueueState.empty()
        if not isinstance(payload, dict):
            self._quarantine_corrupt_file_unlocked(
                "unexpected_payload", f"top-level {type(payload).__name__}"
            )
            return _DurableQueueState.empty()
        migrated = payload.get("migrated_sources", [])
        if not isinstance(migrated, list) or any(not isinstance(name, str) for name in migrated):
            self._quarantine_corrupt_file_unlocked("unexpected_payload", "invalid migrated_sources")
            return _DurableQueueState.empty()
        paused = payload.get("paused_conversations", {})
        if not isinstance(paused, dict) or any(not isinstance(token, str) or not token for token in paused.values()):
            self._quarantine_corrupt_file_unlocked("unexpected_payload", "invalid paused_conversations")
            return _DurableQueueState.empty()
        cancelled = payload.get("client_cancelled", {})
        if not isinstance(cancelled, dict) or any(
            not isinstance(command_id, str)
            or _clean_command_id(command_id) != command_id
            or not self._valid_cancelled_receipt(command_id, record)
            for command_id, record in cancelled.items()
        ):
            self._quarantine_corrupt_file_unlocked("unexpected_payload", "invalid client_cancelled")
            return _DurableQueueState.empty()
        queues: dict[str, list[UserCommand]] = {}
        raw_queues = payload.get("queues")
        if not isinstance(raw_queues, dict):
            raw_queues = {}
        for conversation_id, entries in raw_queues.items():
            if not isinstance(conversation_id, str) or not isinstance(entries, list):
                continue
            commands = [self._dict_to_command(item) for item in entries]
            queues[conversation_id] = [item for item in commands if item is not None]
        inflight: dict[str, UserCommand] = {}
        raw_inflight = payload.get("inflight")
        if not isinstance(raw_inflight, dict):
            raw_inflight = {}
        for conversation_id, item in raw_inflight.items():
            command = self._dict_to_command(item)
            if isinstance(conversation_id, str) and command is not None:
                inflight[conversation_id] = command
        turn_inputs: dict[str, list[UserCommand]] = {}
        raw_turn_inputs = payload.get("turn_inputs")
        if not isinstance(raw_turn_inputs, dict):
            raw_turn_inputs = {}
        for conversation_id, entries in raw_turn_inputs.items():
            if not isinstance(conversation_id, str) or not isinstance(entries, list):
                continue
            commands = [self._dict_to_command(item) for item in entries]
            turn_inputs[conversation_id] = [item for item in commands if item is not None]

        raw_client_pending = payload.get("client_pending")
        if not isinstance(raw_client_pending, list):
            raw_client_pending = []
        client_pending = [
            command
            for item in raw_client_pending
            if (command := self._dict_to_client_command(item)) is not None
        ]
        client_inflight: dict[str, UserCommand] = {}
        raw_client_inflight = payload.get("client_inflight")
        if isinstance(raw_client_inflight, dict):
            for raw_command_id, item in raw_client_inflight.items():
                command = self._dict_to_client_command(item)
                if command is not None:
                    command_id = _clean_command_id(raw_command_id) or self._command_id(command)
                    if command_id:
                        client_inflight[command_id] = command

        version = payload.get("version", 1)
        if isinstance(version, int) and not isinstance(version, bool) and version <= 6:
            for command in [*client_pending, *client_inflight.values()]:
                if command.type == "user_message" and not command.data.get("user_message_id"):
                    command.data["_legacy_user_message_identity"] = True

        raw_ownership = payload.get("ownership")
        if not isinstance(raw_ownership, dict):
            raw_ownership = {}

        def owner_map(name: str, valid_keys: set[str]) -> dict[str, str]:
            raw = raw_ownership.get(name)
            if not isinstance(raw, dict):
                return {}
            return {
                str(key): str(owner)
                for key, owner in raw.items()
                if isinstance(key, str)
                and key in valid_keys
                and isinstance(owner, str)
                and _OWNER_ID_PATTERN.fullmatch(owner)
            }

        return _DurableQueueState(
            queues=queues,
            inflight=inflight,
            turn_inputs=turn_inputs,
            client_pending=client_pending,
            client_inflight=client_inflight,
            inflight_owners=owner_map("inflight", set(inflight)),
            turn_input_owners=owner_map("turn_inputs", set(turn_inputs)),
            client_inflight_owners=owner_map(
                "client_inflight",
                set(client_inflight),
            ),
            migrated_sources=set(migrated),
            paused_conversations=paused,
            client_cancelled=cancelled,
        )

    def _adopt_state_unlocked(self, state: _DurableQueueState) -> None:
        self._queues = state.queues
        self._inflight = state.inflight
        self._turn_inputs = state.turn_inputs
        self._client_pending = state.client_pending
        self._client_inflight = state.client_inflight
        self._client_cancelled = state.client_cancelled
        self._inflight_owners = state.inflight_owners
        self._turn_input_owners = state.turn_input_owners
        self._client_inflight_owners = state.client_inflight_owners
        self._migrated_sources = state.migrated_sources
        self._paused_conversations = state.paused_conversations

    def migrate_user_messages_to(self, destination: "DurableUserMessageQueue") -> int | None:
        """Transfer an inactive legacy window's turn inputs, retaining its client commands."""
        with ExitStack() as scope:
            for queue in sorted((self, destination), key=lambda item: canonical_file_path_key(item.path)):
                scope.enter_context(queue._locked())
            if any(
                lease.stem != self._owner_id and self._owner_is_live_unlocked(lease.stem)
                for lease in self._lease_dir.glob("*.lock")
            ):
                return None
            self._refresh_from_disk_unlocked()
            if self._inflight or self._turn_inputs:
                return None
            # R6 could commit a window Stop and crash before pausing/settling
            # its shared dispatch. Import that authority before the shared
            # reader recovers any dead inflight or turn-input owner.
            receipts: dict[tuple[str, str], dict[str, Any]] = {}
            for path in sorted(self._cancelled_receipt_dir.glob("*.json")):
                record = json.loads(path.read_text(encoding="utf-8"))
                command = self._dict_to_client_command(record.get("command")) if isinstance(record, dict) else None
                command_id = self._command_id(command) if command is not None else ""
                if not self._valid_cancelled_receipt(command_id, record) or path != self._cancelled_receipt_path(command_id):
                    raise ValueError("A legacy Stop receipt is malformed or bound to the wrong command id")
                origin = str(record.get("client_queue_key") or self.path.stem)
                if "client_queue_key" not in record:
                    record = {**record, "client_queue_key": origin}
                existing = destination._cancelled_receipt_unlocked(command_id, origin)
                if existing is not None and not self._same_cancelled_receipt(existing, record, origin):
                    raise ValueError("Conflicting durable Stop receipts; original metadata was retained")
                receipts[(origin, command_id)] = record
            state = destination._read_state_unlocked()
            for command_id, record in state.client_cancelled.items():
                key = (str(record.get("client_queue_key") or ""), command_id)
                if key in receipts and not self._same_cancelled_receipt(receipts[key], record, key[0]):
                    raise ValueError("Conflicting durable Stop receipts; original metadata was retained")
            destination._suppress_cancelled_inputs_unlocked(state, cancellations=receipts)
            for (_, command_id), record in receipts.items():
                destination._archive_cancelled_receipt_unlocked(command_id, record)
            destination._refresh_from_disk_unlocked()
            if self.last_load_error is not None:
                destination.last_load_error = self.last_load_error
                return 0
            source_id = self.path.name
            transferred = 0
            if source_id not in destination._migrated_sources:
                for conversation_id, commands in self._queues.items():
                    known = {
                        self._user_command_identity(command)
                        for command in (
                            *destination._queues.get(conversation_id, ()),
                            *destination._turn_inputs.get(conversation_id, ()),
                            *([destination._inflight[conversation_id]] if conversation_id in destination._inflight else []),
                        )
                    }
                    pending = destination._queues.setdefault(conversation_id, [])
                    for command in commands:
                        command.data["_client_queue_key"] = self.path.stem
                        identity = self._user_command_identity(command)
                        if identity not in known:
                            pending.append(command)
                            known.add(identity)
                            transferred += 1
                # Publish the source receipt with the transferred work. If the
                # source clear fails, a later migration must not replay work
                # that may already have completed in the shared queue.
                destination._migrated_sources.add(source_id)
                destination._write_current_unlocked()
            self._queues.clear()
            self._inflight.clear()
            self._turn_inputs.clear()
            self._inflight_owners.clear()
            self._turn_input_owners.clear()
            self._write_current_unlocked()
            return transferred

    def _recover_stale_owners_unlocked(self, state: _DurableQueueState) -> bool:
        """Replay only work whose persisted owner lease is provably free."""

        changed = False
        conversation_ids = set(state.inflight) | set(state.turn_inputs)
        for conversation_id in conversation_ids:
            replay: list[UserCommand] = []
            turn_owner = state.turn_input_owners.get(conversation_id, "")
            if conversation_id in state.turn_inputs and not self._owner_is_live_unlocked(
                turn_owner
            ):
                replay.extend(state.turn_inputs.pop(conversation_id))
                state.turn_input_owners.pop(conversation_id, None)
                changed = True

            inflight_owner = state.inflight_owners.get(conversation_id, "")
            if conversation_id in state.inflight and not self._owner_is_live_unlocked(
                inflight_owner
            ):
                replay.append(state.inflight.pop(conversation_id))
                state.inflight_owners.pop(conversation_id, None)
                changed = True

            if replay:
                state.queues[conversation_id] = [
                    *replay,
                    *state.queues.get(conversation_id, []),
                ]

        recovered_client_commands: list[UserCommand] = []
        for command_id in list(state.client_inflight):
            owner = state.client_inflight_owners.get(command_id, "")
            if self._owner_is_live_unlocked(owner):
                continue
            recovered_client_commands.append(state.client_inflight.pop(command_id))
            state.client_inflight_owners.pop(command_id, None)
            changed = True

        if recovered_client_commands:
            pending_ids = {
                self._command_id(command)
                for command in state.client_pending
                if self._command_id(command)
            }
            recovered_client_commands = [
                command
                for command in recovered_client_commands
                if self._command_id(command) not in pending_ids
            ]
            state.client_pending = [
                *recovered_client_commands,
                *state.client_pending,
            ]

        dangling_inflight = set(state.inflight_owners) - set(state.inflight)
        dangling_turn_inputs = set(state.turn_input_owners) - set(state.turn_inputs)
        dangling_client = set(state.client_inflight_owners) - set(state.client_inflight)
        if dangling_inflight or dangling_turn_inputs or dangling_client:
            for key in dangling_inflight:
                state.inflight_owners.pop(key, None)
            for key in dangling_turn_inputs:
                state.turn_input_owners.pop(key, None)
            for key in dangling_client:
                state.client_inflight_owners.pop(key, None)
            changed = True
        return changed

    def _refresh_from_disk_unlocked(self) -> None:
        state = self._read_state_unlocked()
        # Archive original cancelled user input before removing it from the
        # frequently rewritten queue. A crash between these writes leaves both
        # copies; the immutable receipt still suppresses automatic replay.
        changed = bool(state.client_cancelled)
        for command_id, record in state.client_cancelled.items():
            self._archive_cancelled_receipt_unlocked(command_id, record)
        state.client_cancelled = {}
        changed = self._suppress_cancelled_inputs_unlocked(state) or changed
        recovered = self._recover_stale_owners_unlocked(state)
        self._adopt_state_unlocked(state)
        if recovered or changed:
            self._write_current_unlocked()

    @classmethod
    def cancellation_input_identity(cls, command: UserCommand) -> str:
        payload = cls._client_command_to_dict(command)
        if payload is None:
            raise ValueError("A Stop input identity requires a serializable client command")
        if "_submitted_content" in command.data:
            payload["data"]["content"] = command.data["_submitted_content"]
        payload["data"].pop("_legacy_user_message_identity", None)
        for key in ("conversation_id", "user_message_id", "assistant_message_id", "window_instance_id", "window_owner_id"):
            payload["data"].pop(key, None)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def legacy_input_origins(
        cls, command: UserCommand, *, conversation_id: str = "",
        candidates: Iterable[tuple[str, UserCommand]],
    ) -> set[str]:
        command_id = cls._command_id(command)
        if not command_id or command.type != "user_message":
            return set()
        identity = cls.cancellation_input_identity(command)
        data = {**command.data, "conversation_id": command.data.get("conversation_id") or conversation_id}
        result = set()
        for origin, candidate in candidates:
            if candidate.type != command.type or cls._command_id(candidate) != command_id:
                continue
            if cls.cancellation_input_identity(candidate) != identity:
                continue
            if any(data.get(key) and candidate.data.get(key) and data[key] != candidate.data[key]
                   for key in ("conversation_id", "user_message_id", "assistant_message_id", "window_instance_id", "window_owner_id")):
                continue
            result.add(origin)
        return result

    @staticmethod
    def _state_input_entries(state: _DurableQueueState) -> list[tuple[str, UserCommand]]:
        entries = [(owner, command) for lane in (state.queues, state.turn_inputs) for owner, commands in lane.items() for command in commands]
        entries.extend(state.inflight.items())
        entries.extend(("", command) for command in [*state.client_pending, *state.client_inflight.values()])
        return entries

    def _resolve_legacy_input_scopes_unlocked(
        self, state: _DurableQueueState, *, cancellations: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> bool:
        if not self._shared_inputs:
            return False
        entries = [(owner, command) for owner, command in self._state_input_entries(state)
                   if self._command_id(command) and not command.data.get("_client_queue_key")]
        if not entries:
            return False
        # Missing producer metadata in one old lane cannot distinguish two
        # identical accepted requests. Cross-lane crash mirrors are permitted.
        for lane in (state.queues, state.turn_inputs):
            for owner, commands in lane.items():
                identities = set()
                for command in commands:
                    if command.data.get("_client_queue_key") or not self._command_id(command):
                        continue
                    identity = json.dumps([owner, command.data.get("user_message_id"), command.data.get("assistant_message_id"),
                        command.data.get("window_instance_id"), command.data.get("window_owner_id"), self.cancellation_input_identity(command)],
                        ensure_ascii=False, sort_keys=True)
                    if identity in identities:
                        raise ValueError("A legacy shared input has ambiguous original windows; input was preserved")
                    identities.add(identity)
        command_ids = {self._command_id(command) for _, command in entries}
        candidates: list[tuple[str, UserCommand]] = [
            (origin, UserCommand(**record["command"])) for (origin, _), record in (cancellations or {}).items()
        ]
        base = self.path.parent.parent
        # Read atomic snapshots only. No foreign owner is claimed or modified.
        # This one-time legacy pass disappears once unique origins are stamped.
        for path in sorted(base.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("A legacy window input snapshot is malformed")
            raw_commands = [*payload.get("client_pending", []), *payload.get("client_inflight", {}).values(),
                *payload.get("inflight", {}).values(),
                *(item for lane in ("queues", "turn_inputs") for commands in payload.get(lane, {}).values() for item in commands)]
            raw_commands.extend(record["command"] for record in payload.get("client_cancelled", {}).values())
            for raw in raw_commands:
                candidate = self._dict_to_client_command(raw)
                if candidate is not None and self._command_id(candidate) in command_ids:
                    candidates.append((path.stem, candidate))
        for parent, scoped in ((base / "cancelled-inputs", False), (self._cancelled_receipt_dir, True)):
            for directory in parent.glob("*"):
                if not directory.is_dir():
                    continue
                origin = directory.name
                for command_id in command_ids:
                    name = hashlib.sha256(command_id.encode("utf-8")).hexdigest() + ".json"
                    path = directory / name
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        continue
                    if not self._valid_cancelled_receipt(command_id, record):
                        raise ValueError("A legacy Stop receipt is malformed or bound to the wrong command id")
                    if scoped and record.get("client_queue_key") != origin:
                        raise ValueError("A shared Stop receipt is bound to another original window")
                    candidates.append((origin, UserCommand(**record["command"])))
        changed = False
        for owner, command in entries:
            origins = self.legacy_input_origins(command, conversation_id=owner, candidates=candidates)
            if len(origins) > 1:
                raise ValueError("A legacy shared input has ambiguous original windows; input was preserved")
            if origins:
                command.data["_client_queue_key"] = origins.pop()
                changed = True
        return changed

    def _cancellation_scope_matches(self, command: UserCommand, record: dict[str, Any], conversation_id: str = "") -> bool:
        original = UserCommand(**record["command"])
        if self._command_id(command) != self._command_id(original):
            return False
        if not self._shared_inputs:
            return True
        if origin := str(command.data.get("_client_queue_key") or ""):
            return origin == record.get("client_queue_key")
        # Supported old shared lanes lack the producer key. Their bound
        # conversation and transcript ids must identify the stopped input.
        owner = str(command.data.get("conversation_id") or conversation_id or "")
        original_owner = str(original.data.get("conversation_id") or "")
        if not owner or not original_owner or owner != original_owner:
            return False
        for key in ("user_message_id", "assistant_message_id", "window_instance_id", "window_owner_id"):
            actual, stopped = str(command.data.get(key) or ""), str(original.data.get(key) or "")
            if actual and stopped and actual != stopped:
                return False
        return self.cancellation_input_identity(command) == self.cancellation_input_identity(original)

    def _cancellation_for_command_unlocked(self, command: UserCommand, conversation_id: str = "") -> dict[str, Any] | None:
        command_id = self._command_id(command)
        if not self._shared_inputs:
            return self._cancelled_receipt_unlocked(command_id)
        if origin := str(command.data.get("_client_queue_key") or ""):
            return self._cancelled_receipt_unlocked(command_id, origin)
        return None

    def _suppress_cancelled_inputs_unlocked(
        self, state: _DurableQueueState, *, cancellations: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> bool:
        """Stop intent wins before any lane can recover a dead dispatch owner."""
        changed = False
        receipts: dict[tuple[str, str], dict[str, Any] | None] = {}
        external = list((cancellations or {}).values())
        changed = self._resolve_legacy_input_scopes_unlocked(state, cancellations=cancellations)
        def stopped(command: UserCommand, conversation_id: str = "") -> bool:
            if command.type != "user_message" or not (command_id := self._command_id(command)):
                return False
            origin = str(command.data.get("_client_queue_key") or "")
            legacy_identity = json.dumps([str(command.data.get("conversation_id") or conversation_id),
                command.data.get("user_message_id"), command.data.get("assistant_message_id"),
                self.cancellation_input_identity(command)], ensure_ascii=False, sort_keys=True)
            key = (origin or legacy_identity, command_id)
            if key not in receipts:
                matching = [record for record in external if self._cancellation_scope_matches(command, record, conversation_id)]
                if len(matching) > 1:
                    raise ValueError("A legacy shared input has ambiguous original windows; input was preserved")
                receipts[key] = matching[0] if matching else self._cancellation_for_command_unlocked(command, conversation_id)
            receipt = receipts[key]
            if receipt is None:
                return False
            original = UserCommand(**receipt["command"])
            bound_owner = str(command.data.get("conversation_id") or conversation_id or "")
            stopped_owner = str(original.data.get("conversation_id") or "")
            if bound_owner and stopped_owner and bound_owner != stopped_owner:
                raise ValueError("A cancelled command id is bound to another conversation")
            if self.cancellation_input_identity(command) != self.cancellation_input_identity(original):
                raise ValueError("A cancelled command id contains different user input; original data was not replaced")
            return True
        for lane in (state.queues, state.turn_inputs):
            for owner, commands in list(lane.items()):
                kept = [command for command in commands if not stopped(command, owner)]
                if len(kept) != len(commands):
                    changed = True
                    if kept:
                        lane[owner] = kept
                    else:
                        lane.pop(owner)
                        if lane is state.turn_inputs:
                            state.turn_input_owners.pop(owner, None)
        for owner, command in list(state.inflight.items()):
            if stopped(command, owner):
                state.inflight.pop(owner)
                state.inflight_owners.pop(owner, None)
                changed = True
        kept = [command for command in state.client_pending if not stopped(command)]
        changed = changed or len(kept) != len(state.client_pending)
        state.client_pending = kept
        for command_id, command in list(state.client_inflight.items()):
            if stopped(command):
                state.client_inflight.pop(command_id)
                state.client_inflight_owners.pop(command_id, None)
                changed = True
        return changed

    def _current_state(self) -> _DurableQueueState:
        return _DurableQueueState(self._queues, self._inflight, self._turn_inputs, self._client_pending,
            self._client_inflight, self._inflight_owners, self._turn_input_owners,
            self._client_inflight_owners, self._migrated_sources, self._paused_conversations, self._client_cancelled)

    @classmethod
    def _user_command_identity(cls, command: UserCommand) -> str:
        data = dict(command.data or {})
        assistant_message_id = str(data.get("assistant_message_id") or "").strip()
        user_message_id = str(data.get("user_message_id") or "").strip()
        if assistant_message_id or user_message_id:
            return f"message:{data.get('_client_queue_key', '')}\0{assistant_message_id}\0{user_message_id}"
        payload = cls._command_to_dict(command) or {"type": command.type, "data": {}}
        return "payload:" + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _indexed_commands(
        cls,
        commands: list[UserCommand],
    ) -> list[tuple[tuple[str, int], UserCommand]]:
        counts: dict[str, int] = {}
        indexed: list[tuple[tuple[str, int], UserCommand]] = []
        for command in commands:
            identity = cls._user_command_identity(command)
            occurrence = counts.get(identity, 0)
            counts[identity] = occurrence + 1
            indexed.append(((identity, occurrence), command))
        return indexed

    @classmethod
    def _merge_queue_delta(
        cls,
        latest: list[UserCommand],
        previous: list[UserCommand],
        desired: list[UserCommand],
    ) -> list[UserCommand]:
        """Three-way merge one owner's FIFO edits without reviving consumed work."""

        latest_indexed = cls._indexed_commands(latest)
        previous_indexed = cls._indexed_commands(previous)
        desired_indexed = cls._indexed_commands(desired)
        previous_keys = {key for key, _command in previous_indexed}
        desired_keys = {key for key, _command in desired_indexed}
        removed_keys = previous_keys - desired_keys

        merged = [
            (key, command)
            for key, command in latest_indexed
            if key not in removed_keys
        ]

        # Apply an explicit reorder only to slots that still contain this
        # owner's earlier commands. Commands already claimed or completed by
        # another live owner are deliberately absent and never reconstructed.
        merged_keys = {key for key, _command in merged}
        desired_common = [
            (key, command)
            for key, command in desired_indexed
            if key in previous_keys and key in merged_keys
        ]
        common_keys = {key for key, _command in desired_common}
        common_slots = [
            index
            for index, (key, _command) in enumerate(merged)
            if key in common_keys
        ]
        if len(common_slots) == len(desired_common):
            for slot, entry in zip(common_slots, desired_common, strict=True):
                merged[slot] = entry

        # Insert newly accepted commands relative to the nearest surviving
        # successor. Appended commands have no successor and therefore join
        # the end of the latest global FIFO, after concurrent earlier writes.
        for desired_index, (key, command) in enumerate(desired_indexed):
            if key in previous_keys or any(existing_key == key for existing_key, _ in merged):
                continue
            successor_keys = [
                successor_key
                for successor_key, _successor in desired_indexed[desired_index + 1 :]
            ]
            insertion_index = next(
                (
                    index
                    for index, (existing_key, _existing) in enumerate(merged)
                    if existing_key in successor_keys
                ),
                len(merged),
            )
            merged.insert(insertion_index, (key, command))

        return [command for _key, command in merged]

    @_queue_locked
    def save(
        self,
        queues: dict[str, list[Any]],
        inflight: dict[str, Any],
        turn_inputs: dict[str, list[Any]] | None = None,
    ) -> None:
        desired_queues = {
            str(conversation_id): [command for command in commands if isinstance(command, UserCommand)]
            for conversation_id, commands in queues.items()
        }
        desired_inflight = {
            str(conversation_id): command
            for conversation_id, command in inflight.items()
            if isinstance(command, UserCommand)
        }
        desired_turn_inputs = {
            str(conversation_id): [command for command in commands if isinstance(command, UserCommand)]
            for conversation_id, commands in (turn_inputs or {}).items()
        }
        self._refresh_from_disk_unlocked()

        merged_queues = dict(self._queues)
        for conversation_id in set(self._owner_queues) | set(desired_queues):
            merged = self._merge_queue_delta(
                list(self._queues.get(conversation_id, [])),
                list(self._owner_queues.get(conversation_id, [])),
                list(desired_queues.get(conversation_id, [])),
            )
            if merged:
                merged_queues[conversation_id] = merged
            else:
                merged_queues.pop(conversation_id, None)
        self._queues = merged_queues

        def merge_owned_delta(
            latest: dict[str, Any],
            owners: dict[str, str],
            previous: dict[str, Any],
            desired: dict[str, Any],
            *,
            label: str,
        ) -> tuple[dict[str, Any], dict[str, str]]:
            merged = dict(latest)
            merged_owners = dict(owners)
            for conversation_id in set(previous) | set(desired):
                before = previous.get(conversation_id)
                after = desired.get(conversation_id)
                if before == after:
                    continue
                current_owner = merged_owners.get(conversation_id, "")
                if conversation_id in previous and current_owner != self._owner_id:
                    # Ownership was settled or transferred after this owner's
                    # snapshot. Never let a stale save delete the new state.
                    continue
                if conversation_id in desired:
                    if (
                        conversation_id in merged
                        and current_owner
                        and current_owner != self._owner_id
                    ):
                        raise RuntimeError(
                            f"{label} for {conversation_id} is owned by another live runtime"
                        )
                    merged[conversation_id] = after
                    merged_owners[conversation_id] = self._owner_id
                elif current_owner == self._owner_id:
                    merged.pop(conversation_id, None)
                    merged_owners.pop(conversation_id, None)
            return merged, merged_owners

        self._inflight, self._inflight_owners = merge_owned_delta(
            self._inflight,
            self._inflight_owners,
            self._owner_inflight,
            desired_inflight,
            label="inflight user command",
        )
        self._turn_inputs, self._turn_input_owners = merge_owned_delta(
            self._turn_inputs,
            self._turn_input_owners,
            self._owner_turn_inputs,
            desired_turn_inputs,
            label="turn input queue",
        )
        self._write_current_unlocked()
        self._owner_queues = {
            conversation_id: list(commands)
            for conversation_id, commands in desired_queues.items()
        }
        self._owner_inflight = {
            conversation_id: command
            for conversation_id, command in desired_inflight.items()
            if self._inflight_owners.get(conversation_id) == self._owner_id
            and self._inflight.get(conversation_id) == command
        }
        self._owner_turn_inputs = {
            conversation_id: list(commands)
            for conversation_id, commands in desired_turn_inputs.items()
            if self._turn_input_owners.get(conversation_id) == self._owner_id
            and self._turn_inputs.get(conversation_id) == commands
        }

    def _write_current_unlocked(self) -> None:
        state = self._current_state()
        self._suppress_cancelled_inputs_unlocked(state)
        self._adopt_state_unlocked(state)
        serialized_queues: dict[str, list[dict[str, Any]]] = {}
        for conversation_id, commands in self._queues.items():
            entries = [self._command_to_dict(command) for command in commands]
            serialized_queues[str(conversation_id)] = [item for item in entries if item is not None]
        serialized_inflight = {
            str(conversation_id): item
            for conversation_id, command in self._inflight.items()
            if (item := self._command_to_dict(command)) is not None
        }
        serialized_turn_inputs: dict[str, list[dict[str, Any]]] = {}
        for conversation_id, commands in self._turn_inputs.items():
            entries = [self._command_to_dict(command) for command in commands]
            serialized_turn_inputs[str(conversation_id)] = [
                item for item in entries if item is not None
            ]
        atomic_write_text(
            self.path,
            json.dumps(
                {
                    "version": _SCHEMA_VERSION,
                    "migrated_sources": sorted(self._migrated_sources),
                    "paused_conversations": self._paused_conversations,
                    "queues": serialized_queues,
                    "inflight": serialized_inflight,
                    "turn_inputs": serialized_turn_inputs,
                    "client_pending": [
                        item
                        for command in self._client_pending
                        if (item := self._client_command_to_dict(command)) is not None
                    ],
                    "client_cancelled": self._client_cancelled,
                    "client_inflight": {
                        command_id: item
                        for command_id, command in self._client_inflight.items()
                        if (item := self._client_command_to_dict(command)) is not None
                    },
                    "ownership": {
                        "inflight": {
                            conversation_id: owner_id
                            for conversation_id, owner_id in self._inflight_owners.items()
                            if conversation_id in serialized_inflight
                            and _OWNER_ID_PATTERN.fullmatch(owner_id)
                        },
                        "turn_inputs": {
                            conversation_id: owner_id
                            for conversation_id, owner_id in self._turn_input_owners.items()
                            if conversation_id in serialized_turn_inputs
                            and _OWNER_ID_PATTERN.fullmatch(owner_id)
                        },
                        "client_inflight": {
                            command_id: owner_id
                            for command_id, owner_id in self._client_inflight_owners.items()
                            if command_id in self._client_inflight
                            and _OWNER_ID_PATTERN.fullmatch(owner_id)
                        },
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _command_id(command: UserCommand) -> str:
        return _clean_command_id(command.data.get("client_command_id"))

    @classmethod
    def same_user_command(cls, left: UserCommand, right: UserCommand) -> bool:
        return cls._user_command_identity(left) == cls._user_command_identity(right)

    @classmethod
    def same_pending_payload(cls, left: UserCommand, right: UserCommand) -> bool:
        return cls.same_user_command(left, right) and cls._command_to_dict(left) == cls._command_to_dict(right)

    def _update_owner_queue_baseline_unlocked(self, conversation_id: str) -> None:
        commands = list(self._queues.get(conversation_id, []))
        if commands:
            self._owner_queues[conversation_id] = commands
        else:
            self._owner_queues.pop(conversation_id, None)

    @_queue_locked
    def pending_user_messages(self, conversation_id: str) -> list[UserCommand]:
        self._refresh_from_disk_unlocked()
        self._update_owner_queue_baseline_unlocked(str(conversation_id))
        return list(self._queues.get(str(conversation_id), []))

    @_queue_locked
    def pending_user_message_snapshot(self) -> dict[str, list[UserCommand]]:
        self._refresh_from_disk_unlocked()
        return {owner: list(commands) for owner, commands in self._queues.items()}

    @_queue_locked
    def add_queued_user_message(self, conversation_id: str, command: UserCommand, validate_admission) -> int:
        self._refresh_from_disk_unlocked()
        message_id = command.data["assistant_message_id"]
        user_id = command.data["user_message_id"]
        owned = [*self._queues.get(conversation_id, []), *self._turn_inputs.get(conversation_id, []),
            *([self._inflight[conversation_id]] if conversation_id in self._inflight else [])]
        if any(item.data.get("assistant_message_id") == message_id or item.data.get("user_message_id") == user_id for item in owned):
            raise ValueError("This queued input is already pending or has been claimed")
        validate_admission()
        self._queues.setdefault(conversation_id, []).append(command)
        self._write_current_unlocked()
        self._update_owner_queue_baseline_unlocked(conversation_id)
        return len(self._queues[conversation_id])

    @_queue_locked
    def update_queued_user_message(self, conversation_id: str, message_id: str, payload: dict[str, Any]) -> UserCommand | None:
        from dataclasses import replace
        from backend.ws.utils import USER_INPUT_METADATA_KEYS

        self._refresh_from_disk_unlocked()
        queue = self._queues.get(conversation_id, [])
        index = next((index for index, command in enumerate(queue)
            if command.data.get("assistant_message_id") == message_id), -1)
        if index < 0:
            return None
        data = dict(queue[index].data)
        for key in (*USER_INPUT_METADATA_KEYS, "attachments", "skills", "plugins"):
            data.pop(key, None)
        data.update(payload)
        data["_submitted_content"] = payload["content"]
        command = replace(queue[index], data=data)
        queue[index] = command
        self._write_current_unlocked()
        self._update_owner_queue_baseline_unlocked(conversation_id)
        return command

    @_queue_locked
    def reorder_queued_user_messages(self, conversation_id: str, message_ids: list[str]) -> list[UserCommand]:
        self._refresh_from_disk_unlocked()
        queue = self._queues.get(conversation_id, [])
        expected = [command.data["assistant_message_id"] for command in queue]
        if sorted(message_ids) != sorted(expected):
            raise ValueError("queue reorder must include every queued submission exactly once")
        by_id = {command.data["assistant_message_id"]: command for command in queue}
        ordered = [by_id[message_id] for message_id in message_ids]
        if ordered:
            self._queues[conversation_id] = ordered
        self._write_current_unlocked()
        self._update_owner_queue_baseline_unlocked(conversation_id)
        return ordered

    @_queue_locked
    def set_user_queue_paused(self, conversation_id: str, stopped_message_id: str) -> None:
        """Keep repeat Stop for one message idempotent across queue owners."""
        self._refresh_from_disk_unlocked()
        if stopped_message_id:
            self._paused_conversations[conversation_id] = stopped_message_id
        else:
            self._paused_conversations.pop(conversation_id, None)
        self._write_current_unlocked()

    @_queue_locked
    def paused_user_queues(self) -> dict[str, str]:
        self._refresh_from_disk_unlocked()
        return dict(self._paused_conversations)

    @_queue_locked
    def claim_user_message(self, conversation_id: str) -> UserCommand | None:
        """Atomically claim one conversation FIFO item for this live owner."""

        self._refresh_from_disk_unlocked()
        owner = str(conversation_id or "").strip()
        if not owner:
            return None
        existing = self._inflight.get(owner)
        if existing is not None:
            return None
        # A live turn may own promoted steer inputs without a normal dispatch
        # slot. Do not let another runtime start a follow-up underneath it.
        if owner in self._turn_inputs:
            return None
        queue = self._queues.get(owner)
        if not queue:
            self._update_owner_queue_baseline_unlocked(owner)
            return None

        index = 0
        if owner in self._paused_conversations:
            # New input can consent to its own execution after this Stop.
            # A Stop of a later message invalidates that earlier consent.
            pause_token = self._paused_conversations[owner]
            index = next((i for i, item in enumerate(queue) if item.data.get("_queue_explicit_send") == pause_token), -1)
            if index < 0:
                return None
        command = queue.pop(index)
        if not queue:
            self._queues.pop(owner, None)
        self._inflight[owner] = command
        self._inflight_owners[owner] = self._owner_id
        try:
            self._write_current_unlocked()
        except Exception:
            self._inflight.pop(owner, None)
            self._inflight_owners.pop(owner, None)
            self._queues.setdefault(owner, []).insert(index, command)
            raise
        self._update_owner_queue_baseline_unlocked(owner)
        self._owner_inflight[owner] = command
        return command

    @_queue_locked
    def settle_user_message(
        self,
        conversation_id: str,
        command: UserCommand,
        *,
        succeeded: bool,
    ) -> bool:
        """Complete or requeue a claim, but only for its actual live owner."""

        self._refresh_from_disk_unlocked()
        owner = str(conversation_id or "").strip()
        receipt = self._cancellation_for_command_unlocked(command, owner)
        if receipt is not None:
            if self.cancellation_input_identity(command) != self.cancellation_input_identity(UserCommand(**receipt["command"])):
                raise ValueError("A cancelled command id contains different user input")
            self._owner_inflight.pop(owner, None)
            return True
        inflight = self._inflight.get(owner)
        if (
            inflight is None
            or self._inflight_owners.get(owner) != self._owner_id
            or not self.same_user_command(inflight, command)
        ):
            return False

        self._inflight.pop(owner, None)
        self._inflight_owners.pop(owner, None)
        if not succeeded:
            self._queues.setdefault(owner, []).insert(0, inflight)
        try:
            self._write_current_unlocked()
        except Exception:
            if not succeeded:
                self._queues[owner].pop(0)
                if not self._queues[owner]:
                    self._queues.pop(owner, None)
            self._inflight[owner] = inflight
            self._inflight_owners[owner] = self._owner_id
            raise
        self._owner_inflight.pop(owner, None)
        self._update_owner_queue_baseline_unlocked(owner)
        return True

    @_queue_locked
    def has_client_command(self, client_command_id: str) -> bool:
        self._refresh_from_disk_unlocked()
        command_id = _clean_command_id(client_command_id)
        if not command_id:
            return False
        if command_id in self._client_inflight:
            return True
        return any(self._command_id(command) == command_id for command in self._client_pending)

    @_queue_locked
    def persist_client_command(self, command: UserCommand) -> bool:
        """Durably accept a command before its websocket ACK is emitted."""
        self._refresh_from_disk_unlocked()
        payload = self._client_command_to_dict(command)
        if payload is None:
            return False
        command_id = _clean_command_id(command.data.get("client_command_id"))
        if self._cancelled_receipt_unlocked(command_id) is not None:
            return True
        if command_id in self._client_inflight or any(
            self._command_id(item) == command_id
            for item in self._client_pending
        ):
            return True
        normalized = self._dict_to_client_command(payload)
        if normalized is None:
            return False
        self._client_pending.append(normalized)
        try:
            self._write_current_unlocked()
        except Exception:
            self._client_pending.pop()
            raise
        return True

    @_queue_locked
    def pending_client_commands(self) -> list[UserCommand]:
        self._refresh_from_disk_unlocked()
        return list(self._client_pending)

    @_queue_locked
    def cancelled_client_command(self, client_command_id: str, *, client_queue_key: str = "") -> dict[str, Any] | None:
        self._refresh_from_disk_unlocked()
        return self._cancelled_receipt_unlocked(_clean_command_id(client_command_id), client_queue_key)

    @_queue_locked
    def prepare_client_cancellation(self, command: UserCommand, *, reason: str, client_queue_key: str = "") -> dict[str, Any]:
        """Capture the accepted original before publishing its shared Stop intent."""
        self._refresh_from_disk_unlocked()
        command_id = self._command_id(command)
        if not command_id or command.type != "user_message":
            raise ValueError("A durable Stop receipt requires a user input client command id")
        origin = client_queue_key or str(command.data.get("_client_queue_key") or self.path.stem)
        existing = self._cancelled_receipt_unlocked(command_id, origin) if self._shared_inputs or origin == self.path.stem else None
        if existing is not None:
            return existing
        owner = self._client_inflight_owners.get(command_id) if origin == self.path.stem else None
        if owner is not None and owner != self._owner_id:
            raise RuntimeError("Cannot cancel a client input owned by another live dispatcher")
        inflight = self._client_inflight.get(command_id)
        pending = list(self._client_pending)
        accepted = (inflight or next((item for item in pending if self._command_id(item) == command_id), command)) if origin == self.path.stem else command
        payload = self._client_command_to_dict(accepted)
        if "_submitted_content" in accepted.data:
            payload["data"]["content"] = accepted.data["_submitted_content"]
        payload["data"].pop("_legacy_user_message_identity", None)
        payload["data"].update({key: command.data[key] for key in (
            "conversation_id", "user_message_id", "assistant_message_id",
        ) if key in command.data})
        record = {"command": payload, "reason": reason, "cancelled_at": time.time(),
                  "client_queue_key": origin}
        return record

    @_queue_locked
    def cancel_client_command(
        self, command: UserCommand, *, reason: str, cancellation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit original Stop intent, then suppress every automatic replay lane."""
        self._refresh_from_disk_unlocked()
        command_id = self._command_id(command)
        origin = str((cancellation or {}).get("client_queue_key") or command.data.get("_client_queue_key") or self.path.stem)
        existing = self._cancelled_receipt_unlocked(command_id, origin)
        if existing is not None:
            if cancellation is not None and not self._same_cancelled_receipt(existing, cancellation, origin):
                raise ValueError("Conflicting durable Stop receipts; original metadata was retained")
            return existing
        record = cancellation if cancellation is not None else self.prepare_client_cancellation(command, reason=reason)
        if not self._valid_cancelled_receipt(command_id, record):
            raise ValueError("A durable Stop receipt is not bound to this user input")
        if self.cancellation_input_identity(command) != self.cancellation_input_identity(UserCommand(**record["command"])):
            raise ValueError("A durable Stop receipt contains different user input")
        owner = self._client_inflight_owners.get(command_id)
        if owner is not None and owner != self._owner_id:
            raise RuntimeError("Cannot cancel a client input owned by another live dispatcher")
        for conversation_id, current in self._inflight.items():
            if self._cancellation_scope_matches(current, record, conversation_id) and self._inflight_owners.get(conversation_id) not in {None, self._owner_id}:
                raise RuntimeError("Cannot cancel a user input owned by another live conversation dispatcher")
        for conversation_id, entries in self._turn_inputs.items():
            if any(self._cancellation_scope_matches(current, record, conversation_id) for current in entries) and self._turn_input_owners.get(conversation_id) not in {None, self._owner_id}:
                raise RuntimeError("Cannot cancel a turn input owned by another live conversation dispatcher")
        original_identity = self.cancellation_input_identity(UserCommand(**record["command"]))
        original_owner = str(record["command"]["data"].get("conversation_id") or "")
        commands = [(current, "") for current in [*self._client_pending, *self._client_inflight.values()]]
        # Conversation lanes carry their owner in the map as well as the input.
        commands.extend((current, owner) for owner, current in self._inflight.items())
        commands.extend((item, owner) for lane in (self._queues, self._turn_inputs) for owner, entries in lane.items() for item in entries)
        for current, conversation_id in commands:
            if not self._cancellation_scope_matches(current, record, conversation_id):
                continue
            if self.cancellation_input_identity(current) != original_identity:
                raise ValueError("A cancelled command id contains different user input; original data was not replaced")
            current_owner = str(current.data.get("conversation_id") or conversation_id or "")
            if original_owner and current_owner and current_owner != original_owner:
                raise ValueError("A cancelled command id is bound to another conversation")
        if self._shared_inputs:
            self._suppress_cancelled_inputs_unlocked(self._read_state_unlocked(), cancellations={(origin, command_id): record})
        self._archive_cancelled_receipt_unlocked(command_id, record)
        # A failure to prune or publish leaves the immutable intent available
        # to the next reader, before stale-owner recovery can claim this input.
        self._refresh_from_disk_unlocked()
        return record

    @_queue_locked
    def bind_client_command_owner(self, client_command_id: str, conversation_id: str) -> bool:
        """Bind the claimed input before its conversation admission begins."""
        self._refresh_from_disk_unlocked()
        command_id = _clean_command_id(client_command_id)
        if self._client_inflight_owners.get(command_id) != self._owner_id:
            return False
        command = self._client_inflight[command_id]
        previous_owner = command.data.get("conversation_id")
        if previous_owner and previous_owner != conversation_id:
            raise ValueError("A durable input cannot change conversation owner")
        if previous_owner == conversation_id:
            return True
        command.data["conversation_id"] = conversation_id
        try:
            self._write_current_unlocked()
        except Exception:
            if previous_owner is None:
                command.data.pop("conversation_id", None)
            else:
                command.data["conversation_id"] = previous_owner
            raise
        return True

    @_queue_locked
    def discard_pending_client_command(self, client_command_id: str) -> bool:
        """Atomically settle a pending command already recorded as handled.

        The recent-command log is the idempotency record.  If a crash or an
        older runtime left both that record and a pending queue entry, replay
        must remove the stale entry instead of repeatedly skipping it forever.
        A command claimed by another live owner is left untouched.
        """

        self._refresh_from_disk_unlocked()
        command_id = _clean_command_id(client_command_id)
        if not command_id or command_id in self._client_inflight:
            return False
        index = next(
            (
                item_index
                for item_index, command in enumerate(self._client_pending)
                if self._command_id(command) == command_id
            ),
            -1,
        )
        if index < 0:
            return False
        command = self._client_pending.pop(index)
        try:
            self._write_current_unlocked()
        except Exception:
            self._client_pending.insert(index, command)
            raise
        return True

    @_queue_locked
    def claim_client_command(self, client_command_id: str) -> UserCommand | None:
        """Move a persisted command from pending to inflight atomically."""
        self._refresh_from_disk_unlocked()
        command_id = _clean_command_id(client_command_id)
        if not command_id:
            return None
        existing = self._client_inflight.get(command_id)
        if existing is not None:
            return None
        index = next(
            (
                item_index
                for item_index, command in enumerate(self._client_pending)
                if self._command_id(command) == command_id
            ),
            -1,
        )
        if index < 0:
            return None
        command = self._client_pending.pop(index)
        self._client_inflight[command_id] = command
        self._client_inflight_owners[command_id] = self._owner_id
        try:
            self._write_current_unlocked()
        except Exception:
            self._client_inflight.pop(command_id, None)
            self._client_inflight_owners.pop(command_id, None)
            self._client_pending.insert(index, command)
            raise
        return command

    @_queue_locked
    def complete_client_command(self, client_command_id: str) -> bool:
        """Commit completion; only after this may the id enter the dedup log."""
        self._refresh_from_disk_unlocked()
        command_id = _clean_command_id(client_command_id)
        if self._cancelled_receipt_unlocked(command_id) is not None:
            return True
        if self._client_inflight_owners.get(command_id) != self._owner_id:
            return False
        command = self._client_inflight.pop(command_id, None)
        if command is None:
            return False
        self._client_inflight_owners.pop(command_id, None)
        try:
            self._write_current_unlocked()
        except Exception:
            self._client_inflight[command_id] = command
            self._client_inflight_owners[command_id] = self._owner_id
            raise
        return True

    @_queue_locked
    def release_client_command(self, client_command_id: str) -> bool:
        """Return interrupted execution ownership to the durable pending list."""
        self._refresh_from_disk_unlocked()
        command_id = _clean_command_id(client_command_id)
        if self._client_inflight_owners.get(command_id) != self._owner_id:
            return False
        command = self._client_inflight.pop(command_id, None)
        if command is None:
            return False
        self._client_inflight_owners.pop(command_id, None)
        self._client_pending.insert(0, command)
        try:
            self._write_current_unlocked()
        except Exception:
            self._client_pending.pop(0)
            self._client_inflight[command_id] = command
            self._client_inflight_owners[command_id] = self._owner_id
            raise
        return True

