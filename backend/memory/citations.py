"""MiniCode memory citation parsing and hidden-markup removal."""

from __future__ import annotations

import re
from typing import Any

_CITATION_BLOCK_RE = re.compile(
    r"<minicode-memory-citation>.*?</minicode-memory-citation>",
    re.DOTALL,
)


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


def scrub_memory_citations(text: str) -> str:
    return _CITATION_BLOCK_RE.sub("", text)


def _extract_block(text: str, opening: str, closing: str) -> str | None:
    try:
        return text.split(opening, 1)[1].split(closing, 1)[0]
    except (IndexError, ValueError):
        return None


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
        return {
            "path": path.strip(),
            "line_start": int(line_start.strip()),
            "line_end": int(line_end.strip()),
            "note": note[:-1].strip(),
        }
    except ValueError:
        return None
