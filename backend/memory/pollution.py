"""External-context detection for MiniCode memory isolation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from backend.agent.tool_common import WEB_FETCH_TOOL_NAMES, WEB_SEARCH_TOOL_NAMES


_EXTERNAL_CONTEXT_TOOL_NAMES = frozenset(
    {
        *WEB_SEARCH_TOOL_NAMES,
        *WEB_FETCH_TOOL_NAMES,
        "tool_search",
        "browser_control",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        "subscribe_mcp_resource",
        "unsubscribe_mcp_resource",
    }
)
_NON_CONTEXT_STATUSES = frozenset(
    {"running", "pending", "failed", "error", "blocked", "timeout", "cancelled"}
)


def external_context_source(tool_name: Any) -> str | None:
    """Return the stable source id for a tool that can introduce external data."""

    name = str(tool_name or "").strip().lower()
    if not name:
        return None
    if name.startswith("mcp__") or name in _EXTERNAL_CONTEXT_TOOL_NAMES:
        return name
    if name.startswith(("list_mcp_", "read_mcp_", "subscribe_mcp_", "unsubscribe_mcp_")):
        return name
    return None


def pollution_sources_from_tool_calls(records: Iterable[Any]) -> list[str]:
    """Collect external sources whose result reached a completed turn."""

    sources: list[str] = []
    seen: set[str] = set()
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        status = str(raw_record.get("status") or "").strip().lower()
        if status in _NON_CONTEXT_STATUSES:
            continue
        source = external_context_source(
            raw_record.get("name") or raw_record.get("tool_name")
        )
        if source is None or source in seen:
            continue
        seen.add(source)
        sources.append(source)
    return sources


def pollution_sources_from_transcript(transcript: Iterable[Any]) -> list[str]:
    """Recompute pollution after replay, truncation, import, or context forking."""

    records: list[dict[str, Any]] = []
    for raw_message in transcript:
        if not isinstance(raw_message, dict):
            continue
        tool_calls = raw_message.get("tool_calls")
        if isinstance(tool_calls, list):
            records.extend(item for item in tool_calls if isinstance(item, dict))
        blocks = raw_message.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_call":
                continue
            record = block.get("record")
            if isinstance(record, dict):
                records.append(record)
    return pollution_sources_from_tool_calls(records)
