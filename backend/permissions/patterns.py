"""Shared normalization for user/session permission pattern lists."""

from __future__ import annotations

from typing import Any


def normalize_tool_patterns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        pattern = str(item or "").strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        normalized.append(pattern)
    return normalized
