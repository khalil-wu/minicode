"""Shared bounded text helpers for memory storage and prompts."""

from __future__ import annotations


def truncate_middle_tokens(value: str, max_tokens: int) -> str:
    """Keep both ends of text while applying MiniCode's byte-based token cap."""

    max_bytes = max(0, int(max_tokens)) * 4
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    # The omission notice consumes the same budget as the retained text.
    # Reserve its maximum digit count before splitting UTF-8 at codepoints.
    marker_bytes = len(f"…{(len(encoded) + 3) // 4} tokens truncated…".encode("utf-8"))
    if max_bytes <= marker_bytes:
        return "…" if max_bytes >= 3 else "." * max_bytes
    retained_bytes = max_bytes - marker_bytes
    left_budget = retained_bytes // 2
    right_budget = retained_bytes - left_budget
    left = encoded[:left_budget].decode("utf-8", errors="ignore")
    right = encoded[-right_budget:].decode("utf-8", errors="ignore") if right_budget else ""
    removed_tokens = (len(encoded) - len(left.encode("utf-8")) - len(right.encode("utf-8")) + 3) // 4
    return f"{left}…{removed_tokens} tokens truncated…{right}"
