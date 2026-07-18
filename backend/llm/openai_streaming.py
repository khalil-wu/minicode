from __future__ import annotations

import json
import logging
from typing import Any

from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent

logger = logging.getLogger(__name__)

_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"
_THINK_TAG_HOLD = max(len(_THINK_OPEN_TAG), len(_THINK_CLOSE_TAG)) - 1


class _ReasoningSplitter:
    """Routes inline <think>...</think> reasoning out of the answer text stream."""

    def __init__(self) -> None:
        self._inside = False
        self._buf = ""

    def feed(self, delta: str) -> list[tuple[str, str]]:
        self._buf += delta
        out: list[tuple[str, str]] = []
        while True:
            if self._inside:
                idx = self._buf.find(_THINK_CLOSE_TAG)
                if idx == -1:
                    break
                segment = self._buf[:idx]
                if segment:
                    out.append(("reasoning", segment))
                self._buf = self._buf[idx + len(_THINK_CLOSE_TAG):]
                self._inside = False
            else:
                open_idx = self._buf.find(_THINK_OPEN_TAG)
                close_idx = self._buf.find(_THINK_CLOSE_TAG)
                if open_idx == -1 and close_idx == -1:
                    break
                if open_idx != -1 and (close_idx == -1 or open_idx <= close_idx):
                    segment = self._buf[:open_idx]
                    if segment:
                        out.append(("text", segment))
                    self._buf = self._buf[open_idx + len(_THINK_OPEN_TAG):]
                    self._inside = True
                else:
                    segment = self._buf[:close_idx]
                    if segment:
                        out.append(("reasoning", segment))
                    self._buf = self._buf[close_idx + len(_THINK_CLOSE_TAG):]
                    self._inside = False
        if len(self._buf) > _THINK_TAG_HOLD:
            cut = len(self._buf) - _THINK_TAG_HOLD
            out.append((self._kind(), self._buf[:cut]))
            self._buf = self._buf[cut:]
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        out = [(self._kind(), self._buf)]
        self._buf = ""
        return out

    def _kind(self) -> str:
        return "reasoning" if self._inside else "text"


def _splitter_events(segments: list[tuple[str, str]]) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for kind, segment in segments:
        if not segment:
            continue
        if kind == "reasoning":
            events.append(
                StreamEvent(
                    type=StreamEventType.THINKING_CHUNK,
                    content=segment,
                    raw={"provider_reasoning_type": "inline_think"},
                )
            )
        else:
            events.append(StreamEvent(type=StreamEventType.TEXT_CHUNK, content=segment))
    return events


class _ToolCallAccumulator:
    """Accumulates streamed tool-call deltas by stream index."""

    def __init__(self) -> None:
        self._slots: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

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
                "_delta_bytes": 0,
            }
            self._order.append(key)

        slot = self._slots[key]
        if call_id:
            slot["id"] = call_id
        if name:
            slot["name"] = name
        if function.get("arguments"):
            slot["arguments"] += str(function["arguments"])
            slot["_delta_bytes"] += len(str(function["arguments"]))

        return is_new, key, slot

    def finalize(self) -> list[ToolCallEvent]:
        events: list[ToolCallEvent] = []
        for key in self._order:
            slot = self._slots[key]
            call_id = str(slot.get("id") or "").strip()
            name = str(slot.get("name") or "").strip()
            raw_args = str(slot.get("arguments") or "")
            raw_arg_len = len(raw_args)
            if not call_id or not name:
                logger.debug(
                    "Dropping incomplete streamed tool call key=%s id=%r name=%r args=%r",
                    key,
                    call_id,
                    name,
                    raw_args[:200],
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
            events.append(ToolCallEvent(id=call_id, name=name, arguments=arguments))
        return events
