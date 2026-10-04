"""Shared constants and utilities for agent tool runtime helpers."""

from __future__ import annotations

# ── Tool-name constants (single source of truth) ──────────────────────────
WEB_SEARCH_TOOL_NAMES = frozenset({"web_search"})
WEB_FETCH_TOOL_NAMES = frozenset({"web_fetch"})
WEB_TOOL_NAMES = WEB_SEARCH_TOOL_NAMES | WEB_FETCH_TOOL_NAMES
