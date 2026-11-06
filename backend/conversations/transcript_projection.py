"""Position-addressed public transcript overlays on an immutable checkpoint."""
from __future__ import annotations

from typing import Any


class TranscriptProjectionError(ValueError):
    pass


def overlay_rows(index: list[list[Any]], projection: dict[str, Any]) -> tuple[list[list[Any]], dict[int, dict[str, Any]]]:
    overlay = projection.get("transcript")
    if overlay is None:
        replacement = projection.get("assistant_message")
        if replacement is None:
            return list(index), {}
        position = next((i for i, row in enumerate(index) if row[0] == replacement["id"]), len(index))
        updates = {position: replacement}
        length = max(len(index), position + 1)
    else:
        if (not isinstance(overlay, dict) or isinstance(overlay.get("base_length"), bool)
                or not isinstance(overlay.get("base_length"), int) or overlay["base_length"] != len(index)):
            raise TranscriptProjectionError("Transcript projection does not address its checkpoint")
        length = overlay.get("length")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise TranscriptProjectionError("Invalid projected transcript length")
        raw = overlay.get("updates")
        if not isinstance(raw, dict):
            raise TranscriptProjectionError("Invalid transcript updates")
        updates = {}
        for key, message in raw.items():
            if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key:
                raise TranscriptProjectionError("Invalid transcript update position")
            position = int(key)
            if not 0 <= position < length or not isinstance(message, dict):
                raise TranscriptProjectionError("Transcript update does not address its message")
            if not isinstance(message.get("id"), str) or not isinstance(message.get("role"), str):
                raise TranscriptProjectionError("Invalid projected transcript message")
            updates[position] = message
        if any(position not in updates for position in range(len(index), length)):
            raise TranscriptProjectionError("Transcript projection is missing appended messages")
    rows = index[:length] + [None] * max(0, length - len(index))
    for position, message in updates.items():
        rows[position] = [message["id"], message["role"], 0, 0]
    return rows, updates


def apply_transcript_projection(messages: list[dict[str, Any]], projection: dict[str, Any]) -> list[dict[str, Any]]:
    rows, updates = overlay_rows([[m["id"], m["role"], 0, 0] for m in messages], projection)
    result = messages[:len(rows)] + [None] * max(0, len(rows) - len(messages))
    for position, message in updates.items():
        result[position] = message
    return result


def extend_transcript_projection(
    previous: dict[str, Any], checkpoint_index: list[list[Any]], before: list[dict[str, Any]],
    after: list[dict[str, Any]], *, start: int,
) -> dict[str, Any]:
    _, prior_updates = overlay_rows(checkpoint_index, previous)
    updates = {str(position): message for position, message in prior_updates.items() if position < len(after)}
    for position in range(start, len(after)):
        if position >= len(before) or after[position] != before[position]:
            updates[str(position)] = after[position]
    return {"base_length": len(checkpoint_index), "length": len(after), "updates": updates}
