"""Shared bounded text helpers for memory storage and prompts."""

from __future__ import annotations


def truncate_middle_tokens(value: str, max_tokens: int) -> str:
    """Keep both ends of text while applying MiniCode's byte-based token cap."""

    max_bytes = max(0, int(max_tokens)) * 4
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    left_budget = max_bytes // 2
    right_budget = max_bytes - left_budget
    left = encoded[:left_budget].decode("utf-8", errors="ignore")
    right = encoded[-right_budget:].decode("utf-8", errors="ignore") if right_budget else ""
    removed_tokens = (max(0, len(encoded) - max_bytes) + 3) // 4
    return f"{left}…{removed_tokens} tokens truncated…{right}"
