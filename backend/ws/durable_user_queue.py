"""Crash-recoverable queue for user follow-up commands.

The active agent turn is intentionally not resumed after a process crash, but
queued user work must not disappear with the WebSocket object.  This tiny
JSON-backed store uses an inflight slot so a command is either still queued or
replayed after an interrupted dispatch.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock, Timeout

from backend.agent.message import UserCommand
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.ws.client_command_log import _clean_command_id

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 4
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

    @classmethod
    def empty(cls) -> "_DurableQueueState":
        return cls({}, {}, {}, [], {}, {}, {}, {})


class DurableUserMessageQueue:
    def __init__(self, *, session_id: str, root_dir: Path) -> None:
        self._session_key = _safe(session_id)
        self.path = Path(root_dir) / f"{self._session_key}.json"
        self._lock = threading.RLock()
        self._owner_id = uuid4().hex
        self._lease_dir = self.path.parent / ".owner-leases" / self._session_key
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        self._owner_lease = FileLock(
            str(self._lease_path(self._owner_id)),
            timeout=0,
        )
        self._owner_lease.acquire(timeout=0)
        self._closed = False
        self._queues: dict[str, list[UserCommand]] = {}
        self._inflight: dict[str, UserCommand] = {}
        self._turn_inputs: dict[str, list[UserCommand]] = {}
        self._client_pending: list[UserCommand] = []
        self._client_inflight: dict[str, UserCommand] = {}
        self._inflight_owners: dict[str, str] = {}
        self._turn_input_owners: dict[str, str] = {}
        self._client_inflight_owners: dict[str, str] = {}
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

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

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
        return {"type": command.type, "data": data}

    @staticmethod
    def _dict_to_command(payload: Any) -> UserCommand | None:
        if not isinstance(payload, dict) or payload.get("type") != "user_message":
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
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

    def _read_state_unlocked(self) -> _DurableQueueState:
        if not self.path.exists():
            return _DurableQueueState.empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._quarantine_corrupt_file_unlocked(
                "unreadable" if isinstance(exc, OSError) else "malformed_json",
                str(exc),
            )
            return _DurableQueueState.empty()
        if not isinstance(payload, dict):
            self._quarantine_corrupt_file_unlocked(
                "unexpected_payload", f"top-level {type(payload).__name__}"
            )
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
        )

    def _adopt_state_unlocked(self, state: _DurableQueueState) -> None:
        self._queues = state.queues
        self._inflight = state.inflight
        self._turn_inputs = state.turn_inputs
        self._client_pending = state.client_pending
        self._client_inflight = state.client_inflight
        self._inflight_owners = state.inflight_owners
        self._turn_input_owners = state.turn_input_owners
        self._client_inflight_owners = state.client_inflight_owners

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
        recovered = self._recover_stale_owners_unlocked(state)
        self._adopt_state_unlocked(state)
        if recovered:
            self._write_current_unlocked()

    @classmethod
    def _user_command_identity(cls, command: UserCommand) -> str:
        data = dict(command.data or {})
        assistant_message_id = str(data.get("assistant_message_id") or "").strip()
        user_message_id = str(data.get("user_message_id") or "").strip()
        if assistant_message_id or user_message_id:
            return f"message:{assistant_message_id}\0{user_message_id}"
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
                    "queues": serialized_queues,
                    "inflight": serialized_inflight,
                    "turn_inputs": serialized_turn_inputs,
                    "client_pending": [
                        item
                        for command in self._client_pending
                        if (item := self._client_command_to_dict(command)) is not None
                    ],
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
    def _same_user_command(cls, left: UserCommand, right: UserCommand) -> bool:
        return cls._user_command_identity(left) == cls._user_command_identity(right)

    def _update_owner_queue_baseline_unlocked(self, conversation_id: str) -> None:
        commands = list(self._queues.get(conversation_id, []))
        if commands:
            self._owner_queues[conversation_id] = commands
        else:
            self._owner_queues.pop(conversation_id, None)

    @_queue_locked
    def pending_user_messages(self, conversation_id: str) -> list[UserCommand]:
        self._refresh_from_disk_unlocked()
        return list(self._queues.get(str(conversation_id), []))

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

        command = queue.pop(0)
        if not queue:
            self._queues.pop(owner, None)
        self._inflight[owner] = command
        self._inflight_owners[owner] = self._owner_id
        try:
            self._write_current_unlocked()
        except Exception:
            self._inflight.pop(owner, None)
            self._inflight_owners.pop(owner, None)
            self._queues.setdefault(owner, []).insert(0, command)
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
        inflight = self._inflight.get(owner)
        if (
            inflight is None
            or self._inflight_owners.get(owner) != self._owner_id
            or not self._same_user_command(inflight, command)
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
