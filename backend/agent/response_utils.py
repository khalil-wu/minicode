"""Small, non-semantic helpers used by the provider loop.

This module deliberately contains no user-intent classification, answer
quality gate, or natural-language retry policy.  The agent must be free to
choose whether to continue, answer partially, or report a blocker.  Only
protocol bookkeeping belongs here.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from typing import Any

from backend.agent.context import ContextBuilder


def provider_items_for_final_answer(
    provider_items: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Keep ordinary answer history free of Anthropic thinking replay blocks.

    Anthropic thinking signatures belong to a resumable provider response. An
    ordinary accepted answer has crossed that continuation boundary, so its
    internal thinking must not be sent as the next turn's assistant content.
    Tool-use, ``pause_turn``, compaction, and output-limit recovery bypass this
    helper and retain their complete native items.
    """

    if not provider_items:
        return []
    protocol_blocks = {
        "tool_use",
        "server_tool_use",
        "mcp_tool_use",
        "web_search_tool_result",
        "web_fetch_tool_result",
        "code_execution_tool_result",
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
        "tool_search_tool_result",
        "container_upload",
        "mcp_tool_result",
        "advisor_tool_result",
        "compaction",
    }
    prepared: list[dict[str, Any]] = []
    for item in provider_items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"thinking", "redacted_thinking"}:
            continue
        if item_type != "anthropic_message":
            prepared.append(deepcopy(item))
            continue
        content = item.get("content")
        if not isinstance(content, list):
            prepared.append(deepcopy(item))
            continue
        if any(
            isinstance(block, dict)
            and str(block.get("type") or "") in protocol_blocks
            for block in content
        ):
            prepared.append(deepcopy(item))
            continue
        visible_content = [
            deepcopy(block)
            for block in content
            if isinstance(block, dict)
            and str(block.get("type") or "")
            not in {"thinking", "redacted_thinking"}
        ]
        if visible_content:
            prepared_item = deepcopy(item)
            prepared_item["content"] = visible_content
            prepared.append(prepared_item)
    return prepared


def append_assistant_history(
    ctx: ContextBuilder,
    content: str,
    *,
    phase: str = "",
    provider_items: list[dict[str, Any]] | None = None,
) -> None:
    """Append assistant history while tolerating legacy context fakes."""

    append_assistant = ctx.append_assistant
    try:
        parameters = inspect.signature(append_assistant).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "phase" in parameters:
        kwargs["phase"] = phase
    if accepts_kwargs or "provider_items" in parameters:
        kwargs["provider_items"] = provider_items
    append_assistant(content, **kwargs)
