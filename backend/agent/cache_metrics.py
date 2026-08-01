from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from backend.agent.message import AgentEvent
from backend.permissions.context import ToolExecutionContext


_DEFAULT_SAVED_MS: dict[str, int] = {
    "read_file.file_state": 18,
    "list_files.result": 12,
    "grep_files.search": 120,
    "glob_files.search": 45,
    "provider.prompt": 300,
}


def args_signature(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def cache_metric_payload(
    *,
    cache_layer: str,
    tool_name: str = "",
    run_id: str = "",
    turn_id: str = "",
    args_signature_value: str = "",
    hit: bool,
    stale: bool = False,
    evicted: bool = False,
    estimated_saved_ms: int | None = None,
    payload_size_bytes: int | None = None,
) -> dict[str, Any]:
    saved_ms = estimated_saved_ms
    if saved_ms is None:
        saved_ms = _DEFAULT_SAVED_MS.get(cache_layer, 0) if hit else 0
    payload: dict[str, Any] = {
        "kind": "cache_metric",
        "type": "cache.lookup",
        "cache_layer": cache_layer,
        "tool_name": tool_name,
        "run_id": run_id,
        "turn_id": turn_id,
        "args_signature": args_signature_value,
        "hit": bool(hit),
        "stale": bool(stale),
        "evicted": bool(evicted),
        "estimated_saved_ms": max(0, int(saved_ms or 0)),
        "observed_at": int(time.time() * 1000),
    }
    if payload_size_bytes is not None:
        payload["payload_size_bytes"] = max(0, int(payload_size_bytes or 0))
    return payload


def cache_metric_event(**kwargs: Any) -> AgentEvent:
    payload = cache_metric_payload(**kwargs)
    target = f"{payload['cache_layer']}:{payload['args_signature'] or payload['observed_at']}"
    return AgentEvent.inspector_update(
        "cache",
        target,
        payload,
    )


async def emit_cache_metric(context: ToolExecutionContext | None, **kwargs: Any) -> None:
    emit = getattr(context, "emit_event", None) if context else None
    if emit is None:
        return
    event = cache_metric_event(
        run_id=str(getattr(context, "task_id", "") or (context.metadata or {}).get("run_id", "")),
        turn_id=str((context.metadata or {}).get("turn_id") or (context.metadata or {}).get("assistant_message_id") or ""),
        **kwargs,
    )
    await emit(event.type, dict(event.data))
