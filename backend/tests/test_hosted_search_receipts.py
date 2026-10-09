"""Hosted search requires a native tool outcome, never a model-only reply."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, SideQueryOptions
from backend.tools.web_tools import WebSearchTool
from tests.test_openai_responses_alignment import _adapter, _completed_response
from tests.test_provider_adapters import _install_anthropic_fake


def openai_response(outcome: str):
    response = _completed_response(text="" if outcome == "zero" else "Search answer https://example.test/jobs")
    if outcome != "missing":
        response.response.output = [SimpleNamespace(
            type="web_search_call", id="search-native", status="failed" if outcome == "error" else "completed",
            action=SimpleNamespace(type="search", query="Agent backend jobs"),
        )]
    return response


def anthropic_adapter(outcome: str):
    adapter = AnthropicAdapter("fixture", model="claude-fixture", supports_hosted_web_search=True)
    requests = []

    async def events():
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(
            id="message-search", model="claude-fixture", usage=SimpleNamespace(input_tokens=1, output_tokens=0),
        ))
        index = 0
        if outcome != "missing":
            yield SimpleNamespace(type="content_block_start", index=index, content_block={
                "type": "server_tool_use", "id": "search-native", "name": "web_search", "input": {"query": "Agent backend jobs"},
            })
            yield SimpleNamespace(type="content_block_stop", index=index)
            index += 1
            content = (
                {"type": "web_search_tool_result_error", "error_code": "rate_limit"}
                if outcome == "error" else [] if outcome == "zero" else [{
                    "type": "web_search_result", "title": "Actual result", "url": "https://example.test/jobs",
                }]
            )
            yield SimpleNamespace(type="content_block_start", index=index, content_block={
                "type": "web_search_tool_result", "tool_use_id": "search-native", "content": content,
            })
            yield SimpleNamespace(type="content_block_stop", index=index)
            index += 1
        if outcome != "zero":
            yield SimpleNamespace(type="content_block_start", index=index, content_block={
                "type": "text", "text": "Search answer https://example.test/jobs",
            })
            yield SimpleNamespace(type="content_block_stop", index=index)
        yield SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"), usage=SimpleNamespace(output_tokens=1))
        yield SimpleNamespace(type="message_stop")

    async def call(**kwargs):
        requests.append(kwargs)
        return events()

    _install_anthropic_fake(adapter, call)
    return adapter, requests


@pytest.mark.parametrize("outcome", ["completed", "missing", "zero", "error"])
def test_openai_hosted_search_requires_completed_native_receipt(monkeypatch, outcome):
    adapter, responses = _adapter([openai_response(outcome)])
    tool = WebSearchTool(adapter)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(tool, "_search_api_key", lambda: "")
    tool._direct_search = AsyncMock(side_effect=AssertionError("native search silently changed providers"))

    result = asyncio.run(tool.execute({"query": "Agent backend jobs", "allowed_domains": ["example.test"]}))
    tool._direct_search.assert_not_awaited()

    request = responses.requests[0]
    assert request["tool_choice"] == "required"
    assert request["tools"][0]["filters"] == {"allowed_domains": ["example.test"]}
    assert result.is_error is (outcome in {"missing", "error"})
    assert result.extraction_status == ("failed" if result.is_error else "ok")
    if outcome == "missing":
        assert "no completed web_search_call" in result.content
    elif outcome == "error":
        assert "status=failed" in result.content
    elif outcome == "zero":
        assert "Search completed with no results." in result.content
    else:
        assert "Search answer" in result.content


@pytest.mark.parametrize("outcome", ["completed", "missing", "zero", "error"])
def test_anthropic_hosted_search_requires_native_result_block(monkeypatch, outcome):
    adapter, requests = anthropic_adapter(outcome)
    tool = WebSearchTool(adapter)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(tool, "_search_api_key", lambda: "")
    tool._direct_search = AsyncMock(side_effect=AssertionError("native search silently changed providers"))

    result = asyncio.run(tool.execute({"query": "Agent backend jobs", "blocked_domains": ["excluded.test"]}))
    tool._direct_search.assert_not_awaited()

    request = requests[0]
    assert request["tool_choice"] == {"type": "tool", "name": "web_search"}
    assert request["tools"][-1]["blocked_domains"] == ["excluded.test"]
    assert result.is_error is (outcome in {"missing", "error"})
    assert result.extraction_status == ("failed" if result.is_error else "ok")
    if outcome == "missing":
        assert "no web_search_tool_result" in result.content
    elif outcome == "error":
        assert "rate_limit" in result.content
    elif outcome == "zero":
        assert "Search completed with no results." in result.content
    else:
        assert "Actual result" in result.content


def test_openai_completed_search_event_is_an_execution_receipt():
    adapter, responses = _adapter([
        SimpleNamespace(type="response.web_search_call.completed", item_id="search-native", output_index=0),
        _completed_response(text="Search completed"),
    ])

    result = asyncio.run(adapter.side_query(
        [LLMMessage(role="user", content="Search jobs")],
        options=SideQueryOptions(operation="web_search_tool", hosted_web_search=True, disable_reasoning=True),
    ))

    assert result == "Search completed"
    assert responses.requests[0]["tool_choice"] == "required"


def test_openai_fetch_side_query_keeps_auto_without_search_receipt():
    adapter, responses = _adapter([_completed_response(text="Extracted page content")])

    result = asyncio.run(adapter.side_query(
        [LLMMessage(role="user", content="Extract this page")],
        options=SideQueryOptions(operation="web_fetch_apply", disable_reasoning=True),
    ))

    assert result == "Extracted page content"
    assert responses.requests[0]["tool_choice"] == "auto"
    assert responses.requests[0]["tools"] == []


def test_anthropic_fetch_side_query_does_not_force_hosted_search():
    adapter, requests = anthropic_adapter("missing")

    result = asyncio.run(adapter.side_query(
        [LLMMessage(role="user", content="Extract this page")],
        options=SideQueryOptions(operation="web_fetch_apply", disable_reasoning=True),
    ))

    assert "Search answer" in result
    assert "tool_choice" not in requests[0]
    assert "tools" not in requests[0]
