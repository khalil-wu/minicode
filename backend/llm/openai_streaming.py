from __future__ import annotations

import json
import logging
from typing import Any

from backend.llm.base import ToolCallEvent

logger = logging.getLogger(__name__)

class _ToolCallAccumulator:
    """Accumulates streamed tool-call deltas by stream index."""

    def __init__(self) -> None:
        self._slots: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        # Slots that finished without both an id and a name. The caller must
        # fail the turn instead of running a partial batch: with two parallel
        # calls, dropping one silently executes half of what the model asked
        # for.
        self.dropped_incomplete: list[dict[str, Any]] = []

    def feed(self, tool_call: dict[str, Any], index: int) -> tuple[bool, str, dict[str, Any]]:
        call_id = tool_call.get("id") or ""
        function = tool_call.get("function") or {}
        name = function.get("name") or ""
        key = f"idx:{index}"

        existing = self._slots.get(key)
        if existing is not None:
            existing_id = str(existing.get("id") or "")
            existing_name = str(existing.get("name") or "")
            if call_id and existing_id and call_id != existing_id:
                key = f"idx:{index}:{call_id}"
            elif not call_id and name and existing_name and existing_name != name:
                key = f"idx:{index}:{name}"

        is_new = key not in self._slots
        if is_new:
            self._slots[key] = {
                "id": call_id,
                "name": name,
                "arguments": "",
                # Keep the last parsed snapshot only to recognize gateways
                # that replay the entire completed JSON document in a finish
                # chunk.  A valid object is not itself a lifecycle boundary:
                # providers may legally append another property in a later
                # delta.
                "_last_complete_snapshot": "",
                "_delta_bytes": 0,
                "_start_emitted": False,
            }
            self._order.append(key)

        slot = self._slots[key]
        if call_id:
            slot["id"] = call_id
        if name:
            slot["name"] = name
        argument_delta = str(function.get("arguments") or "")
        if argument_delta:
            # A function call owns one JSON argument document.  Do not stop
            # after the first parseable object: ``{"a":1}`` followed by
            # ``,"b":2}`` is a valid streamed document.  Only suppress an
            # exact/same-value replay of the already complete snapshot, which
            # a few OpenAI-compatible gateways emit in their finish chunk.
            current = str(slot.get("arguments") or "")
            snapshot = str(slot.get("_last_complete_snapshot") or "")
            duplicate_snapshot = False
            if snapshot and argument_delta.strip():
                try:
                    duplicate_snapshot = (
                        argument_delta.strip() == snapshot.strip()
                        or (
                            isinstance(json.loads(argument_delta), dict)
                            and json.loads(argument_delta) == json.loads(snapshot)
                        )
                    )
                except (json.JSONDecodeError, TypeError):
                    duplicate_snapshot = False
            if not duplicate_snapshot:
                slot["arguments"] = current + argument_delta
                slot["_delta_bytes"] += len(argument_delta)
                if self._is_complete_json_object(str(slot["arguments"])):
                    slot["_last_complete_snapshot"] = str(slot["arguments"])

        # Some OpenAI-compatible providers split id and function.name across
        # separate deltas. Emit START exactly once when both fields first
        # become available, rather than tying it to slot creation.
        should_start = bool(
            slot.get("id")
            and slot.get("name")
            and not slot.get("_start_emitted")
        )
        if should_start:
            slot["_start_emitted"] = True
        return should_start, key, slot

    @staticmethod
    def _is_complete_json_object(value: str) -> bool:
        if not value:
            return False
        try:
            return isinstance(json.loads(value), dict)
        except (json.JSONDecodeError, TypeError):
            return False

    def finalize(self) -> list[ToolCallEvent]:
        events: list[ToolCallEvent] = []
        self.dropped_incomplete = []
        for key in self._order:
            slot = self._slots[key]
            call_id = str(slot.get("id") or "").strip()
            name = str(slot.get("name") or "").strip()
            raw_args = str(slot.get("arguments") or "")
            raw_arg_len = len(raw_args)
            if not call_id or not name:
                logger.warning(
                    "Incomplete streamed tool call key=%s has_id=%s has_name=%s raw_arg_len=%d",
                    key,
                    bool(call_id),
                    bool(name),
                    raw_arg_len,
                )
                self.dropped_incomplete.append(
                    {
                        "key": key,
                        "has_id": bool(call_id),
                        "has_name": bool(name),
                        "raw_arg_len": raw_arg_len,
                    }
                )
                continue
            parse_status = "ok"
            try:
                arguments = json.loads(raw_args or "{}")
            except (json.JSONDecodeError, TypeError):
                from backend.llm.json_repair import repair_tool_json

                arguments = repair_tool_json(raw_args) or {"_raw": raw_args}
                parse_status = "repaired" if "_raw" not in arguments else "raw"
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
                parse_status = "wrapped"
            log = logger.warning if name and not arguments else logger.debug
            log(
                "Finalized streamed tool call key=%s id=%s name=%s raw_arg_len=%d parse_status=%s",
                key,
                call_id,
                name,
                raw_arg_len,
                parse_status,
            )
            events.append(ToolCallEvent(
                id=call_id,
                name=name,
                arguments=arguments,
                arguments_repaired=parse_status != "ok",
            ))
        return events
