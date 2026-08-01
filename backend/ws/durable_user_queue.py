"""Crash-recoverable queue for user follow-up commands.

The active agent turn is intentionally not resumed after a process crash, but
queued user work must not disappear with the WebSocket object.  This tiny
JSON-backed store uses an inflight slot so a command is either still queued or
replayed after an interrupted dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from uuid import uuid4
from pathlib import Path
from typing import Any

from backend.agent.message import UserCommand
from backend.ws.client_command_log import _clean_command_id

logger = logging.getLogger(__name__)


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_") or "session"


class DurableUserMessageQueue:
    def __init__(self, *, session_id: str, root_dir: Path) -> None:
        self.path = Path(root_dir) / f"{_safe(session_id)}.json"
        self._queues: dict[str, list[UserCommand]] = {}
        self._inflight: dict[str, UserCommand] = {}
        self._turn_inputs: dict[str, list[UserCommand]] = {}
        self._client_pending: list[UserCommand] = []
        self._client_inflight: dict[str, UserCommand] = {}

    @staticmethod
    def _command_to_dict(command: Any) -> dict[str, Any] | None:
        if not isinstance(command, UserCommand) or command.type != "user_message":
            return None
        data = {
            str(key): value
            for key, value in dict(command.data or {}).items()
            if not str(key).startswith("_")
        }
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
        if not self.path.exists():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Unable to load durable user queue %s: %s", self.path, exc)
            return {}, {}
        if not isinstance(payload, dict):
            return {}, {}
        queues: dict[str, list[UserCommand]] = {}
        for conversation_id, entries in (payload.get("queues") or {}).items():
            if not isinstance(conversation_id, str) or not isinstance(entries, list):
                continue
            commands = [self._dict_to_command(item) for item in entries]
            queues[conversation_id] = [item for item in commands if item is not None]
        inflight: dict[str, UserCommand] = {}
        for conversation_id, item in (payload.get("inflight") or {}).items():
            command = self._dict_to_command(item)
            if isinstance(conversation_id, str) and command is not None:
                inflight[conversation_id] = command
        turn_inputs: dict[str, list[UserCommand]] = {}
        for conversation_id, entries in (payload.get("turn_inputs") or {}).items():
            if not isinstance(conversation_id, str) or not isinstance(entries, list):
                continue
            commands = [self._dict_to_command(item) for item in entries]
            turn_inputs[conversation_id] = [item for item in commands if item is not None]

        client_pending = [
            command
            for item in (payload.get("client_pending") or [])
            if (command := self._dict_to_client_command(item)) is not None
        ]
        client_inflight: list[UserCommand] = []
        raw_client_inflight = payload.get("client_inflight") or {}
        if isinstance(raw_client_inflight, dict):
            for item in raw_client_inflight.values():
                command = self._dict_to_client_command(item)
                if command is not None:
                    client_inflight.append(command)
        # Active turns are not resumed after a process crash. Replay promoted
        # steer inputs and an interrupted normal dispatch ahead of the ordinary
        # FIFO queue, preserving their promotion order.
        replay_conversation_ids = set(inflight) | set(turn_inputs)
        for conversation_id in replay_conversation_ids:
            replay = [*turn_inputs.get(conversation_id, [])]
            command = inflight.get(conversation_id)
            if command is not None:
                replay.append(command)
            queues[conversation_id] = [*replay, *queues.get(conversation_id, [])]
        self._queues = {conversation_id: list(commands) for conversation_id, commands in queues.items()}
        self._inflight = {}
        self._turn_inputs = {}
        # A process crash invalidates execution ownership.  Requeue every
        # inflight client command ahead of commands that had not been claimed.
        self._client_pending = [*client_inflight, *client_pending]
        self._client_inflight = {}
        if client_inflight:
            self._write_current()
        return queues, {}

    def save(
        self,
        queues: dict[str, list[Any]],
        inflight: dict[str, Any],
        turn_inputs: dict[str, list[Any]] | None = None,
    ) -> None:
        self._queues = {
            str(conversation_id): [command for command in commands if isinstance(command, UserCommand)]
            for conversation_id, commands in queues.items()
        }
        self._inflight = {
            str(conversation_id): command
            for conversation_id, command in inflight.items()
            if isinstance(command, UserCommand)
        }
        self._turn_inputs = {
            str(conversation_id): [command for command in commands if isinstance(command, UserCommand)]
            for conversation_id, commands in (turn_inputs or {}).items()
        }
        self._write_current()

    def _write_current(self) -> None:
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}-{uuid4().hex}.tmp"
        )
        try:
            tmp_path.write_text(
                json.dumps(
                    {
                        "version": 3,
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
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            last_error: PermissionError | None = None
            for attempt in range(5):
                try:
                    tmp_path.replace(self.path)
                    return
                except PermissionError as exc:
                    last_error = exc
                    if attempt == 4:
                        raise
                    # Windows file scanners and another queue writer can hold
                    # the destination for a few milliseconds. Preserve the
                    # atomic replace contract while allowing that transient
                    # lock to clear instead of dropping the queued turn.
                    time.sleep(0.02 * (attempt + 1))
            if last_error is not None:
                raise last_error
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Unable to remove temporary queue file %s", tmp_path)

    @staticmethod
    def _command_id(command: UserCommand) -> str:
        return _clean_command_id(command.data.get("client_command_id"))

    def has_client_command(self, client_command_id: str) -> bool:
        command_id = _clean_command_id(client_command_id)
        if not command_id:
            return False
        if command_id in self._client_inflight:
            return True
        return any(self._command_id(command) == command_id for command in self._client_pending)

    def persist_client_command(self, command: UserCommand) -> bool:
        """Durably accept a command before its websocket ACK is emitted."""
        payload = self._client_command_to_dict(command)
        if payload is None:
            return False
        command_id = _clean_command_id(command.data.get("client_command_id"))
        if self.has_client_command(command_id):
            return True
        normalized = self._dict_to_client_command(payload)
        if normalized is None:
            return False
        self._client_pending.append(normalized)
        try:
            self._write_current()
        except Exception:
            self._client_pending.pop()
            raise
        return True

    def pending_client_commands(self) -> list[UserCommand]:
        return list(self._client_pending)

    def claim_client_command(self, client_command_id: str) -> UserCommand | None:
        """Move a persisted command from pending to inflight atomically."""
        command_id = _clean_command_id(client_command_id)
        if not command_id:
            return None
        existing = self._client_inflight.get(command_id)
        if existing is not None:
            return existing
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
        try:
            self._write_current()
        except Exception:
            self._client_inflight.pop(command_id, None)
            self._client_pending.insert(index, command)
            raise
        return command

    def complete_client_command(self, client_command_id: str) -> bool:
        """Commit completion; only after this may the id enter the dedup log."""
        command_id = _clean_command_id(client_command_id)
        command = self._client_inflight.pop(command_id, None)
        if command is None:
            return False
        try:
            self._write_current()
        except Exception:
            self._client_inflight[command_id] = command
            raise
        return True

    def release_client_command(self, client_command_id: str) -> bool:
        """Return interrupted execution ownership to the durable pending list."""
        command_id = _clean_command_id(client_command_id)
        command = self._client_inflight.pop(command_id, None)
        if command is None:
            return False
        self._client_pending.insert(0, command)
        try:
            self._write_current()
        except Exception:
            self._client_pending.pop(0)
            self._client_inflight[command_id] = command
            raise
        return True
