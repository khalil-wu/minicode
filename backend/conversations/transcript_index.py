"""Byte-addressed pages of an immutable transcript generation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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


def read_message(path: Path, index: list[list[Any]], message_id: str) -> dict[str, Any] | None:
    row = next((row for row in index if row[0] == message_id), None)
    if row is None:
        return None
    with path.open("rb") as stream:
        stream.seek(row[2])
        message = json.loads(stream.read(row[3]))
    if message.get("id") != message_id or message.get("role") != row[1]:
        raise TranscriptIndexError("Transcript index does not match its generation")
    return message


def read_page(
    path: Path, index: list[list[Any]], *, limit: int,
    before_message_id: str = "", replacement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(index)
    replacement_index = -1
    if replacement is not None:
        replacement_index = next((i for i, row in enumerate(rows) if row[0] == replacement["id"]), len(rows))
        row = [replacement["id"], replacement["role"], 0, 0]
        if replacement_index == len(rows): rows.append(row)
        else: rows[replacement_index] = row
    end = len(rows)
    if before_message_id:
        end = next((i for i, row in enumerate(rows) if row[0] == before_message_id), -1)
        if end < 0:
            raise TranscriptCursorMissing("The history page anchor no longer exists; reload the conversation.")
    start = max(0, end - limit)
    if start > 0 and rows[start][1] == "assistant" and rows[start - 1][1] == "user":
        start -= 1
    messages = []
    with path.open("rb") as stream:
        for position in range(start, end):
            if position == replacement_index:
                messages.append(replacement)
                continue
            message_id, role, offset, size = rows[position]
            stream.seek(offset)
            message = json.loads(stream.read(size))
            if message.get("id") != message_id or message.get("role") != role:
                raise TranscriptIndexError("Transcript index does not match its generation")
            messages.append(message)
    return {
        "transcript": messages,
        "transcript_page": {"before_message_id": rows[start][0] if start < end else "",
                            "has_more": start > 0, "total_messages": len(rows)},
    }
