"""Search source, effective content and code-mode model-context regressions."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.artifact.store import ArtifactStore
from backend.config import LLMSettings
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import ToolResult
from backend.tools.web_tools import WebFetchTool, WebSearchTool
from tests.test_web_tools import _FakeStreamClient, _FakeStreamResponse, _HostedSearchLLM


@pytest.mark.parametrize(("wire", "url", "declared", "expected"), [
    ("responses", "https://api.openai.com/v1", None, True),
    ("responses", "https://chatgpt.com/backend-api/codex", None, True),
    ("responses", "http://localhost:8317/v1", None, False),
    ("responses", "http://localhost:8317/v1", True, True),
    ("responses", "https://api.openai.com/v1", False, False),
    ("chat", "https://api.openai.com/v1", True, False),
])
def test_hosted_search_uses_endpoint_or_explicit_capability(wire, url, declared, expected):
    adapter = object.__new__(OpenAIAdapter)
    adapter._settings = LLMSettings(api_key="", provider="custom", wire_api=wire, base_url=url,
        supports_hosted_web_search=declared)
    assert adapter.supports_hosted_web_search() is expected


@pytest.mark.parametrize(("url", "declared", "expected"), [
    ("https://api.anthropic.com", None, True),
    ("http://localhost:15721", None, False),
    ("http://localhost:15721", True, True),
    ("https://api.anthropic.com", False, False),
])
def test_messages_search_respects_capability_override(url, declared, expected):
    adapter = AnthropicAdapter("", base_url=url, provider_id="custom", supports_hosted_web_search=declared)
    assert adapter.supports_hosted_web_search() is expected


def test_unconfigured_search_does_not_make_rss_or_other_requests(monkeypatch):
    tool = WebSearchTool()
    monkeypatch.setattr(tool, "_search_api_key", lambda: "")
    monkeypatch.setattr(tool, "_get_client", lambda: pytest.fail("unconfigured search made a request"))
    result = asyncio.run(tool.execute({"query": "Agent 后端", "allowed_domains": ["zhipin.com"]}))
    assert result.is_error and result.error_kind == "search_unavailable"
    assert result.extraction_status == "failed" and "No search request was made" in result.content


def test_search_provider_factory_uses_declared_hosted_capability(monkeypatch):
    llm = _HostedSearchLLM(blocked_domains=False)
    tool = WebSearchTool(lambda: llm)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(tool, "_search_api_key", lambda: "")
    result = asyncio.run(tool.execute({"query": "Agent 后端", "allowed_domains": ["nowcoder.com"]}))
    assert not result.is_error
    assert llm.calls[0][1].web_search_allowed_domains == ("nowcoder.com",)


def test_configured_tavily_search_and_schema_do_not_instantiate_a_model(monkeypatch):
    def unavailable_model():
        pytest.fail("configured Tavily search instantiated the model factory")

    tool = WebSearchTool(unavailable_model)
    monkeypatch.setattr(tool, "_search_api_key", lambda: "fixture-search-key")
    tool._direct_search = AsyncMock(return_value=ToolResult(
        "Configured search result", provider="tavily", result_kind="search"))
    assert "blocked_domains" in tool.model_schema().parameters["properties"]
    result = asyncio.run(tool.execute({"query": "Current news", "blocked_domains": ["excluded.test"]}))
    assert result.provider == "tavily"
    tool._direct_search.assert_awaited_once_with("Current news", allowed_domains=[], blocked_domains=["excluded.test"])


def test_tool_directory_does_not_require_model_instantiation():
    def unavailable_model():
        pytest.fail("tool discovery attempted to instantiate an unconfigured model")

    schema = WebSearchTool(unavailable_model).model_schema()
    assert "allowed_domains" in schema.parameters["properties"]


def test_hosted_search_without_text_is_not_reported_as_complete():
    result = asyncio.run(WebSearchTool(_HostedSearchLLM(response="")).execute({"query": "Agent"}))
    assert result.status == "partial" and result.extraction_status == "partial"


@pytest.mark.parametrize(("domain_field", "request_field"), [("allowed_domains", "include_domains"), ("blocked_domains", "exclude_domains")])
def test_search_domain_constraint_reaches_source(monkeypatch, domain_field, request_field):
    calls = []
    class Client:
        def stream(self, method, url, **kwargs):
            calls.append(kwargs)
            return _FakeStreamResponse(b'{"results":[]}', headers={"content-type": "application/json"})
    tool = WebSearchTool()
    tool._client = Client()
    tool._proxy_url = "http://fixture.proxy"
    monkeypatch.setattr(tool, "_search_api_key", lambda: "fixture-key")
    monkeypatch.setattr("backend.tools.web_tools.assess_network_url", lambda url: SimpleNamespace(allowed=True))
    result = asyncio.run(tool.execute({"query": "2027 Agent 后端", domain_field: ["https://www.nowcoder.com/jobs"]}))
    assert calls[0]["json"][request_field] == ["nowcoder.com"]
    assert calls[0]["json"]["query"] == "2027 Agent 后端"
    assert not result.is_error and result.extraction_status == "ok"


@pytest.mark.parametrize(("url", "content_type", "body", "extraction"), [
    ("https://www.zhipin.com/zhaopin/", "application/json", '{"code":37,"message":"您的环境存在异常."}', "failed"),
    ("https://www.nowcoder.com/jobs/detail/1", "text/html", '<html><head><title>Agent backend role</title></head><body><script>app.start()</script></body></html>', "partial"),
])
def test_unusable_fetch_skips_model_and_preserves_diagnostic_body(tmp_path, url, content_type, body, extraction):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = WebFetchTool(store)
    tool._unrestricted_client = _FakeStreamClient(_FakeStreamResponse(body.encode(), headers={"content-type": content_type}))
    tool._extract_with_prompt = AsyncMock(side_effect=AssertionError("no useful body to extract"))
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"))
    result = asyncio.run(tool.execute({"url": url, "prompt": "Extract exact requirements."}, context))
    assert result.status == "partial" and result.extraction_status == extraction
    assert result.artifact_id and store.get(result.artifact_id)
    assert result.limitation and "No usable page body" in result.content
    tool._extract_with_prompt.assert_not_called()
    assert not tool._url_cache


def test_json_business_code_is_not_guessed_for_unrelated_sites(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = WebFetchTool(store)
    body = '{"code":37,"message":"a legitimate domain-specific value"}'
    tool._unrestricted_client = _FakeStreamClient(_FakeStreamResponse(body.encode(), headers={"content-type": "application/json"}))
    tool._extract_with_prompt = AsyncMock(return_value="Valid extracted data")
    result = asyncio.run(tool.execute({"url": "https://example.com/data", "prompt": "Read the value"},
        ToolExecutionContext(permission=PermissionContext(mode="bypass"))))
    assert result.status == "success" and result.extraction_status == "ok"
    assert store.get(result.artifact_id) == body


def test_fetch_keeps_extracted_result_and_full_cleaned_source_separate(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = WebFetchTool(store)
    html = '<html><body><p>Python, LangGraph and PostgreSQL requirements.</p><script>NOISE</script></body></html>'
    tool._unrestricted_client = _FakeStreamClient(_FakeStreamResponse(html.encode()))
    tool._extract_with_prompt = AsyncMock(return_value="The role requires Python, LangGraph and PostgreSQL.")
    result = asyncio.run(tool.execute({"url": "https://example.com/job", "prompt": "Extract requirements"},
        ToolExecutionContext(permission=PermissionContext(mode="bypass"))))
    assert result.status == "success" and "The role requires" in result.content
    assert store.get(result.artifact_id) == "Python, LangGraph and PostgreSQL requirements."
    assert "NOISE" not in result.content_preview


def test_fetch_extraction_uses_page_and_requested_task_without_extra_policy(tmp_path):
    llm = SimpleNamespace(side_query=AsyncMock(return_value="Extracted result"), configured_small_fast_model_id=lambda: "")
    tool = WebFetchTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    result = asyncio.run(tool._extract_with_prompt("Page body from 2025-10-06", "Extract the stated date.",
        SimpleNamespace(llm=llm, run_context=None)))
    assert result == "Extracted result"
    messages = llm.side_query.call_args.args[0]
    assert messages[0].content == (
        "Web page content:\n---\nPage body from 2025-10-06\n---\n\n"
        "Extract the stated date.\n\nProvide a concise response based only on the content above."
    )


def test_fetch_without_extraction_model_reports_facts_and_preserves_source(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = WebFetchTool(store)
    source = "Dated source: 2025-10-06."
    tool._unrestricted_client = _FakeStreamClient(_FakeStreamResponse(source.encode(), headers={"content-type": "text/plain"}))
    result = asyncio.run(tool.execute({"url": "https://example.com/date", "prompt": "Extract the date"},
        ToolExecutionContext(permission=PermissionContext(mode="bypass"))))
    assert result.is_error and result.error_kind == "provider_unavailable"
    assert result.source_url == "https://example.com/date" and result.evidence_type == "fetched"
    assert result.artifact_id and store.get(result.artifact_id) == source
    assert result.content == f"网页已抓取；当前会话没有可用模型执行提取。原始清洗内容保存在 artifact {result.artifact_id}。"
    assert result.limitation == "网页抓取成功；当前会话没有可用模型，原始清洗内容已保存为 artifact"
