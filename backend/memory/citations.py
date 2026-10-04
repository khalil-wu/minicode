"""MiniCode memory citation parsing and hidden-markup removal."""

from __future__ import annotations

from typing import Any

def parse_memory_citation(citations: list[str]) -> dict[str, Any] | None:
    entries: list[dict[str, Any]] = []
    rollout_ids: list[str] = []
    seen: set[str] = set()
    for citation in citations:
        entries_block = _extract_block(citation, "<citation_entries>", "</citation_entries>")
        if entries_block:
            for line in entries_block.splitlines():
                entry = _parse_entry(line)
                if entry is not None:
                    entries.append(entry)
        ids_block = _extract_block(citation, "<rollout_ids>", "</rollout_ids>")
        ids_block = ids_block or _extract_block(citation, "<thread_ids>", "</thread_ids>")
        if ids_block:
            for value in ids_block.splitlines():
                value = value.strip()
                if value and value not in seen:
                    seen.add(value)
                    rollout_ids.append(value)
    if not entries and not rollout_ids:
        return None
    return {"entries": entries, "rollout_ids": rollout_ids}


def _extract_block(text: str, opening: str, closing: str) -> str | None:
    start = text.find(opening)
    if start < 0:
        return None
    start += len(opening)
    end = text.find(closing, start)
    return text[start:end] if end >= 0 else None


def _parse_entry(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line or "|note=[" not in line:
        return None
    location, note = line.rsplit("|note=[", 1)
    if not note.endswith("]") or ":" not in location:
        return None
    path, line_range = location.rsplit(":", 1)
    if "-" not in line_range:
        return None
    line_start, line_end = line_range.split("-", 1)
    try:
        start = int(line_start.strip())
        end = int(line_end.strip())
    except ValueError:
        return None
    path = path.strip()
    if not path or start < 1 or end < start:
        return None
    return {"path": path, "line_start": start, "line_end": end, "note": note[:-1].strip()}
