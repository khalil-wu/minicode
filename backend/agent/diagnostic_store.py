"""Bounded, session-local storage for Inspector payloads loaded on demand."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from backend.secret_redaction import redact_json_secrets


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
    conversation_ids: tuple[str, ...]
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
            "raw_usage", "loop_metrics", "safety",
            "prompt_cache_diagnostic", "iteration_id", "call_index", "trace_id",
            "citations", "search_sources", "container", "refusal",
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
        # Inspector data eventually crosses the same renderer boundary as a
        # websocket event, only on demand. Apply the live secret/ownership
        # sanitizer before retaining it so focusing an entry cannot reveal a
        # field that the normal event stream would have removed.
        sanitized = redact_json_secrets(deepcopy(dict(payload)))
        stored_payload = sanitized if isinstance(sanitized, dict) else {}
        size = _json_size(stored_payload)
        previous = self._entries.get(key)
        if (
            key[0] == "provider"
            and previous is not None
            and previous.payload.get("kind") == "provider_trace"
            and stored_payload.get("kind") != "provider_trace"
        ):
            # item.completed and done carry a reduced public provider_raw under
            # the same trace id after the authoritative inspector.update. Keep
            # that later projection compact without downgrading the full trace
            # users fetch when they focus the Inspector entry.
            self._entries.move_to_end(key)
            return compact_diagnostic_payload(
                stored_payload,
                target_kind=key[0],
                target_id=key[1],
                byte_size=size,
            )
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous.byte_size
        entry = DiagnosticPayload(
            target_kind=key[0],
            target_id=key[1],
            conversation_id=str(conversation_id or ""),
            conversation_ids=(str(conversation_id),) if conversation_id else (),
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

    def delete_for_conversation(self, conversation_id: str) -> int:
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        removed = 0
        for key, entry in list(self._entries.items()):
            owners = list(entry.conversation_ids or ((entry.conversation_id,) if entry.conversation_id else ()))
            if owner not in owners:
                continue
            remaining = [value for value in owners if value != owner]
            if remaining:
                self._entries[key] = replace(
                    entry,
                    conversation_id=remaining[0],
                    conversation_ids=tuple(remaining),
                )
                continue
            self._entries.pop(key)
            self._bytes -= entry.byte_size
            removed += 1
        return removed

    def share_for_conversation(self, source_conversation_id: str, target_conversation_id: str) -> int:
        source = str(source_conversation_id or "").strip()
        target = str(target_conversation_id or "").strip()
        if not source or not target or source == target:
            return 0
        shared = 0
        for key, entry in list(self._entries.items()):
            owners = list(entry.conversation_ids or ((entry.conversation_id,) if entry.conversation_id else ()))
            if source not in owners or target in owners:
                continue
            owners.append(target)
            self._entries[key] = replace(entry, conversation_ids=tuple(owners))
            shared += 1
        return shared
