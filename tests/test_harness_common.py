"""Tests for the shared tool-name constants."""

from __future__ import annotations

from backend.agent.tool_common import (
    WEB_FETCH_TOOL_NAMES,
    WEB_SEARCH_TOOL_NAMES,
    WEB_TOOL_NAMES,
)


class TestWebToolConstants:
    def test_sets_are_disjoint_and_union(self) -> None:
        assert isinstance(WEB_SEARCH_TOOL_NAMES, frozenset)
        assert isinstance(WEB_FETCH_TOOL_NAMES, frozenset)
        assert WEB_SEARCH_TOOL_NAMES.isdisjoint(WEB_FETCH_TOOL_NAMES)
        assert WEB_TOOL_NAMES == WEB_SEARCH_TOOL_NAMES | WEB_FETCH_TOOL_NAMES
        assert "web_search" in WEB_TOOL_NAMES
        assert "web_fetch" in WEB_TOOL_NAMES
