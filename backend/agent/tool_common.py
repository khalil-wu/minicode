"""Shared constants and utilities for agent tool runtime helpers."""

from __future__ import annotations

from typing import Any


# ── Tool-name constants (single source of truth) ──────────────────────────
WEB_SEARCH_TOOL_NAMES = frozenset({"web_search"})
WEB_FETCH_TOOL_NAMES = frozenset({"web_fetch"})
WEB_TOOL_NAMES = WEB_SEARCH_TOOL_NAMES | WEB_FETCH_TOOL_NAMES


# ── Shared helpers ────────────────────────────────────────────────────────
def _text_arg(value: Any) -> str:
    """Recursively extract the first meaningful text value from *value*.

    Supports plain strings, dicts (checks common key names), and lists.
    Returns an empty string when nothing useful is found.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("query", "q", "url", "href", "link", "text", "value"):
            text = _text_arg(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        for item in value:
            text = _text_arg(item)
            if text:
                return text
    return ""
