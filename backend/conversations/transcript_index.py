"""Byte-addressed pages of a checkpoint plus committed transcript updates."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, BinaryIO
from .compression import open_history_binary
from .search import message_match
from .transcript_projection import overlay_rows


class TranscriptCursorMissing(ValueError):
    pass


class TranscriptIndexError(RuntimeError):
    pass


def encode_transcript(messages: list[dict[str, Any]]) -> tuple[str, list[list[Any]]]:
    lines: list[str] = []
    index: list[list[Any]] = []
    offset = 0
    for message in messages:
        line = json.dumps(message, ensure_ascii=False) + "\n"
        size = len(line.encode("utf-8"))
        index.append([message["id"], message["role"], offset, size])
        lines.append(line)
        offset += size
    return "".join(lines), index


def read_message(
    path: Path, index: list[list[Any]], message_id: str, *, projection: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    rows, updates = overlay_rows(index, projection or {})
    position = next((i for i, row in enumerate(rows) if row[0] == message_id), None)
    if position is None:
        return None
    if position in updates:
        return updates[position]
    with open_history_binary(path) as stream:
        return _read_indexed_message(stream, rows[position])


def _read_indexed_message(stream: BinaryIO, row: list[Any]) -> dict[str, Any]:
    message_id, role, offset, size = row
    stream.seek(offset)
    raw = stream.read(size)
    if len(raw) != size:
        raise TranscriptIndexError("Transcript generation is truncated at its indexed byte range")
    message = json.loads(raw.decode("utf-8"))
    if not isinstance(message, dict) or message.get("id") != message_id or message.get("role") != role:
        raise TranscriptIndexError("Transcript index does not match its generation")
    return message


def find_match(
    path: Path, index: list[list[Any]], pattern: re.Pattern[str], *, replacement: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Scan visible rows once, stopping at the first content match."""
    rows, updates = overlay_rows(index, projection if projection is not None else {"assistant_message": replacement})
    with open_history_binary(path) as stream:
        for position, row in enumerate(rows):
            if row[1] not in {"user", "assistant"}:
                continue
            message = updates[position] if position in updates else _read_indexed_message(stream, row)
            match = message_match(message, pattern)
            if match is not None:
                return match
    return None


def read_page(
    path: Path, index: list[list[Any]], *, limit: int,
    before_message_id: str = "", replacement: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows, updates = overlay_rows(index, projection if projection is not None else {"assistant_message": replacement})
    end = len(rows)
    if before_message_id:
        end = next((i for i, row in enumerate(rows) if row[0] == before_message_id), -1)
        if end < 0:
            raise TranscriptCursorMissing("The history page anchor no longer exists; reload the conversation.")
    start = max(0, end - limit)
    if start > 0 and rows[start][1] == "assistant" and rows[start - 1][1] == "user":
        start -= 1
    messages = []
    with open_history_binary(path) as stream:
        for position in range(start, end):
            messages.append(updates[position] if position in updates else _read_indexed_message(stream, rows[position]))
    return {
        "transcript": messages,
        "transcript_page": {"before_message_id": rows[start][0] if start < end else "",
                            "has_more": start > 0, "total_messages": len(rows)},
    }
