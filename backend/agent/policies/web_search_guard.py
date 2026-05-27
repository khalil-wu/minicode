from __future__ import annotations

import re
from typing import Any

from backend.llm.base import ToolCallEvent

WEB_SEARCH_TOOL_NAMES = {"web_search", "mcp__websearch__search"}
MAX_WEB_SEARCH_CALLS_PER_TURN = 6
MAX_CONSECUTIVE_EMPTY_WEB_SEARCHES = 3
MAX_SIMILAR_EMPTY_WEB_SEARCHES = 2

_EMPTY_WEB_SEARCH_MARKERS = (
    "no results",
    "no search results",
    "returned no results",
    "未返回结果",
    "搜索失败",
    "没有返回结果",
    "failed",
    "error",
)


def web_search_query(args: dict[str, Any]) -> str:
    for key in ("query", "q", "search_query", "pattern"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_query(value: str) -> str:
    text = value.lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b\d{4}[-/年]?\d{0,2}[-/月]?\d{0,2}日?\b", " ", text)
    text = re.sub(r"\b(?:site|intitle|inurl):\S+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _query_similarity(left: str, right: str) -> float:
    left_norm = _normalize_query(left)
    right_norm = _normalize_query(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm or left_norm in right_norm or right_norm in left_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _is_empty_search_record(record: Any) -> bool:
    output = str(getattr(record, "tool_output", "") or "").lower()
    status = getattr(record, "status", "success")
    return status != "success" or any(marker in output for marker in _EMPTY_WEB_SEARCH_MARKERS)


def web_search_guard_reason(
    state: Any,
    tc: ToolCallEvent,
    *,
    queued_tool_calls: list[ToolCallEvent] | None = None,
) -> str:
    if tc.name not in WEB_SEARCH_TOOL_NAMES:
        return ""

    prior = [record for record in state.tool_calls if record.tool_name in WEB_SEARCH_TOOL_NAMES]
    queued_searches = sum(
        1 for queued in (queued_tool_calls or []) if queued.name in WEB_SEARCH_TOOL_NAMES
    )
    total_searches = len(prior) + queued_searches
    if total_searches >= MAX_WEB_SEARCH_CALLS_PER_TURN:
        return (
            f"Search budget reached: already queued or ran {total_searches} web searches this turn. "
            "Use the available results, fetch a known source, or answer with uncertainty."
        )

    consecutive_empty = 0
    for record in reversed(state.tool_calls):
        if record.tool_name not in WEB_SEARCH_TOOL_NAMES:
            continue
        if not _is_empty_search_record(record):
            break
        consecutive_empty += 1
    if consecutive_empty >= MAX_CONSECUTIVE_EMPTY_WEB_SEARCHES:
        return (
            f"Stopped web search after {consecutive_empty} consecutive empty or failed searches. "
            "Do not keep changing keywords; summarize what is known or ask for a narrower target."
        )

    query = web_search_query(tc.arguments)
    if query:
        similar_empty = sum(
            1
            for record in prior
            if _is_empty_search_record(record)
            and _query_similarity(query, web_search_query(record.tool_input)) >= 0.72
        )
        if similar_empty >= MAX_SIMILAR_EMPTY_WEB_SEARCHES:
            return (
                "Skipped another similar web search after repeated empty results. "
                "Use a different source type, fetch a known URL, or answer from the current evidence."
            )
    return ""
