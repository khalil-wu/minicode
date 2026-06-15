from __future__ import annotations

from typing import Any


def create_stream_state(conversation_id: str, message_id: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "accumulated_text": "",
        "tool_calls": {},
    }


def append_stream_text(
    streams: dict[str, dict[str, Any]],
    conversation_id: str | None,
    chunk: str,
) -> str | None:
    if not conversation_id:
        return None
    stream_state = streams.get(str(conversation_id))
    if stream_state is None:
        return None
    stream_state["accumulated_text"] = f"{stream_state.get('accumulated_text', '')}{chunk}"
    return str(stream_state.get("accumulated_text") or "")


def upsert_pending_tool_call(
    stream_state: dict[str, Any],
    tool_id: str,
    record: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    tool_calls = stream_state.get("tool_calls")
    if not isinstance(tool_calls, dict):
        tool_calls = {}
        stream_state["tool_calls"] = tool_calls
    if tool_id:
        tool_calls[tool_id] = dict(record)
    return tool_calls


def remove_pending_tool_call(
    stream_state: dict[str, Any],
    tool_id: str,
) -> dict[str, dict[str, Any]]:
    tool_calls = stream_state.get("tool_calls")
    if not isinstance(tool_calls, dict):
        return {}
    tool_calls.pop(tool_id, None)
    return tool_calls
