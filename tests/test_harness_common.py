"""Tests for shared tool helpers."""

from __future__ import annotations

from backend.agent.tool_common import (
    WEB_FETCH_TOOL_NAMES,
    WEB_SEARCH_TOOL_NAMES,
    WEB_TOOL_NAMES,
    _text_arg,
)


class TestTextArg:
    def test_plain_and_nested_text(self) -> None:
        assert _text_arg("  hello world  ") == "hello world"
        assert _text_arg({"query": {"text": "nested"}}) == "nested"
        assert _text_arg([{"q": [{"text": "deep"}]}]) == "deep"

    def test_empty_or_non_text_values(self) -> None:
        for value in ("", "   ", {}, [], None, 42, True, 3.14):
            assert _text_arg(value) == ""

    def test_key_priority(self) -> None:
        assert _text_arg({"url": "https://fallback.test", "query": "primary"}) == "primary"


class TestWebToolConstants:
    def test_sets_are_disjoint_and_union(self) -> None:
        assert isinstance(WEB_SEARCH_TOOL_NAMES, frozenset)
        assert isinstance(WEB_FETCH_TOOL_NAMES, frozenset)
        assert WEB_SEARCH_TOOL_NAMES.isdisjoint(WEB_FETCH_TOOL_NAMES)
        assert WEB_TOOL_NAMES == WEB_SEARCH_TOOL_NAMES | WEB_FETCH_TOOL_NAMES
        assert "web_search" in WEB_TOOL_NAMES
        assert "web_fetch" in WEB_TOOL_NAMES
