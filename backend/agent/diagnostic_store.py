"""Bounded, session-local storage for Inspector payloads loaded on demand."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


_SUMMARY_KEYS = frozenset({
    "wire_api", "model", "provider_host", "message_count", "input_items_len",
    "instructions_hash", "instructions_full_hash", "tools_hash", "tools_len",
    "tools_chars", "tool_names", "cache_control_present", "cache_breakpoints",
    "prompt_cache_key_present", "prompt_cache_key_hash", "metadata_keys",
})


@dataclass(frozen=True)
class DiagnosticPayload:
    target_kind: str
    target_id: str
    conversation_id: str
    payload: dict[str, Any]
    created_at: int
    byte_size: int


def _json_size(payload: dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(repr(payload).encode("utf-8", errors="replace"))


def compact_diagnostic_payload(
    payload: dict[str, Any],
    *,
    target_kind: str,
    target_id: str,
    byte_size: int | None = None,
) -> dict[str, Any]:
    """Keep live telemetry useful without shipping raw diagnostic arrays."""
    compact: dict[str, Any] = {
        key: deepcopy(value)
        for key, value in payload.items()
        if key in {
            "kind", "provider", "model", "finish_reason", "event_type", "usage",
            "raw_usage", "stateful_continuation", "loop_metrics", "safety",
            "prompt_cache_diagnostic", "iteration_id", "call_index", "trace_id",
        }
    }
    request_summary = payload.get("request_summary")
    if isinstance(request_summary, dict):
        compact["request_summary"] = {
            key: deepcopy(value)
            for key, value in request_summary.items()
            if key in _SUMMARY_KEYS
        }
    compact.update({
        "diagnostics_deferred": True,
        "diagnostics_ref": f"{target_kind}:{target_id}",
        "diagnostics_bytes": int(byte_size if byte_size is not None else _json_size(payload)),
    })
    return compact


class DiagnosticPayloadStore:
    def __init__(self, *, max_entries: int = 128, max_bytes: int = 8_000_000) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_bytes = max(1, int(max_bytes))
        self._entries: OrderedDict[tuple[str, str], DiagnosticPayload] = OrderedDict()
        self._bytes = 0

    def put(
        self,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any],
        *,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        key = (str(target_kind), str(target_id))
        stored_payload = deepcopy(dict(payload))
        size = _json_size(stored_payload)
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous.byte_size
        entry = DiagnosticPayload(
            target_kind=key[0],
            target_id=key[1],
            conversation_id=str(conversation_id or ""),
            payload=stored_payload,
            created_at=int(time.time() * 1000),
            byte_size=size,
        )
        self._entries[key] = entry
        self._bytes += size
        while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= evicted.byte_size
        return compact_diagnostic_payload(
            stored_payload,
            target_kind=key[0],
            target_id=key[1],
            byte_size=size,
        )

    def get(self, target_kind: str, target_id: str) -> DiagnosticPayload | None:
        key = (str(target_kind), str(target_id))
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry
