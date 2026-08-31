"""Tests for web tool output contracts: HTML cleaning and extraction_status."""

from __future__ import annotations

import asyncio
import gzip
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.tools.base import ToolResult
from backend.tools.html_sanitizer import assess_extraction, sanitize_html
from backend.permissions.context import PermissionContext, ToolExecutionContext


class _FakeStreamResponse:
    def __init__(self, content: bytes, *, headers: dict[str, str] | None = None) -> None:
        import httpx

        self.status_code = 200
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.content = content
        self.extensions = {}
        self.request = httpx.Request("GET", "https://example.com/")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_bytes(self):
        yield self.content

    def raise_for_status(self) -> None:
        return None


class _FakeStreamClient:
    def __init__(self, response: _FakeStreamResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.stream_calls = 0

    def stream(self, *_args, **_kwargs):
        self.stream_calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


# ── HTML Sanitizer: SVG path coordinates stripped ──


def test_svg_path_coordinates_stripped() -> None:
    html = """
    <html><body>
    <div>北京 18.3℃ 西南风 微风</div>
    <svg viewBox="0 0 1000 500">
      <path d="M854.4 800.9 C854.4 800.9 288 104.8 500 200 L100 300 Z"></path>
      <path d="M0 0 L100 200 C300 400 500 600 700 800"></path>
    </svg>
    </body></html>
    """
    result = sanitize_html(html)
    assert "M854.4" not in result
    assert "800.9" not in result
    assert (
        "288" not in result or "18.3" in result
    )  # 288 from SVG gone, but 18.3 preserved
    assert "104.8" not in result
    assert "L100" not in result


def test_svg_path_numbers_not_in_output() -> None:
    html = """
    <html><body>
    <p>今天气温 22°C</p>
    <svg><path d="M-104.8 288 C500.2 300.1 400 200 100 50"></path></svg>
    </body></html>
    """
    result = sanitize_html(html)
    assert "-104.8" not in result
    assert "22°C" in result


# ── HTML Sanitizer: real text preserved ──


def test_real_weather_text_preserved() -> None:
    html = """
    <html><body>
    <div class="weather-info" style="color: red;">
      <p>北京 18.3℃ 西南风 微风</p>
      <p>空气质量：良</p>
    </div>
    </body></html>
    """
    result = sanitize_html(html)
    assert "北京 18.3℃ 西南风 微风" in result
    assert "空气质量：良" in result


def test_strips_script_style_nav_footer() -> None:
    html = """
    <html><body>
    <header><nav>Menu Item 1</nav></header>
    <script>var x = 1; alert('hi');</script>
    <style>.foo { color: red; }</style>
    <main><p>This is the real content.</p></main>
    <footer>Copyright 2024</footer>
    </body></html>
    """
    result = sanitize_html(html)
    assert "This is the real content." in result
    assert "alert" not in result
    assert "color: red" not in result
    assert "Menu Item 1" not in result
    assert "Copyright 2024" not in result


def test_strips_inline_styles_and_classes() -> None:
    html = '<div class="tw-flex tw-gap-4" style="margin: 10px;"><p>Hello</p></div>'
    result = sanitize_html(html)
    assert "Hello" in result
    assert "tw-flex" not in result
    assert "margin" not in result


def test_strips_canvas_noscript_template() -> None:
    html = """
    <html><body>
    <p>Visible text</p>
    <canvas width="500" height="300">Canvas fallback</canvas>
    <noscript>Enable JavaScript</noscript>
    <template><div>Template content</div></template>
    </body></html>
    """
    result = sanitize_html(html)
    assert "Visible text" in result
    assert "Canvas fallback" not in result
    assert "Enable JavaScript" not in result
    assert "Template content" not in result


# ── extraction_status assessment ──


def test_extraction_status_failed_for_empty() -> None:
    assert assess_extraction("", 5000) == "failed"
    assert assess_extraction("short", 5000) == "failed"


def test_extraction_status_failed_for_very_short() -> None:
    assert assess_extraction("x" * 19, 10000) == "failed"


def test_extraction_status_partial_for_low_ratio() -> None:
    cleaned = "a" * 50
    raw_length = 100_000
    assert assess_extraction(cleaned, raw_length) == "partial"


def test_extraction_status_ok_for_normal_content() -> None:
    cleaned = "a" * 500
    raw_length = 5000
    assert assess_extraction(cleaned, raw_length) == "ok"


# ── Page with only script/style/SVG → extraction_status != ok ──


def test_script_only_page_extraction_status_not_ok() -> None:
    html = """
    <html><body>
    <script>var app = new App(); app.init();</script>
    <style>body { margin: 0; } .container { display: flex; }</style>
    <svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="40"/></svg>
    </body></html>
    """
    cleaned = sanitize_html(html)
    status = assess_extraction(cleaned, len(html))
    assert status != "ok"


# ── ToolResult.to_context_string() with web metadata ──


def test_tool_result_context_string_with_web_fields() -> None:
    result = ToolResult(
        content="已抓取页面（约 500 tokens）",
        source_url="https://example.com/weather",
        extraction_status="ok",
        evidence_type="fetched",
        content_preview="北京 18.3℃ 西南风 微风\n空气质量：良",
        artifact_id="art_123",
    )
    ctx = result.to_context_string()
    assert "[evidence: fetched, extraction: ok]" in ctx
    assert "Source: https://example.com/weather" in ctx
    assert "--- content preview ---" in ctx
    assert "北京 18.3℃" in ctx
    assert "art_123" in ctx


def test_tool_result_context_string_failed_extraction() -> None:
    result = ToolResult(
        content="抓取失败: timeout",
        is_error=True,
        source_url="https://example.com/broken",
        extraction_status="failed",
        evidence_type="fetched",
    )
    ctx = result.to_context_string()
    assert "status: error" in ctx
    assert "evidence: fetched" in ctx
    assert "extraction: failed" in ctx
    assert "Source: https://example.com/broken" in ctx


# ── Artifact preview comes from cleaned text ──


def test_artifact_stores_cleaned_not_raw_html(tmp_path: Path) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.tools.web_tools import WebFetchTool

    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = WebFetchTool(store)

    html_with_svg = """
    <html><body>
    <p>Real content here</p>
    <svg><path d="M854.4 800.9 C100 200 300 400 500 600 Z"></path></svg>
    <script>alert('xss')</script>
    </body></html>
    """

    mock_client = _FakeStreamClient(
        _FakeStreamResponse(html_with_svg.encode("utf-8"))
    )
    tool._client = mock_client

    result = asyncio.run(
        tool.execute(
            {"url": "https://example.com/page", "prompt": "Extract the page content."}
        )
    )

    assert "M854.4" not in result.content
    assert "alert" not in result.content
    if result.content_preview:
        assert "M854.4" not in result.content_preview
        assert "alert" not in result.content_preview
    if result.artifact_preview:
        assert "M854.4" not in result.artifact_preview


# ── web_search returns candidates, not facts ──


class _HostedSearchLLM:
    def __init__(
        self,
        *,
        blocked_domains: bool = True,
        response: str = "Current result\n\nSources:\n- Example: https://example.com/result",
        error: Exception | None = None,
    ) -> None:
        self._blocked_domains = blocked_domains
        self._response = response
        self._error = error
        self.calls: list[tuple[list[object], object]] = []

    def supports_hosted_web_search(self) -> bool:
        return True

    def hosted_web_search_supports_blocked_domains(self) -> bool:
        return self._blocked_domains

    async def side_query(self, messages, *, options, turn_context=None):
        del turn_context
        self.calls.append((messages, options))
        if self._error is not None:
            raise self._error
        return self._response


def test_web_search_schema_matches_anthropic_hosted_tool() -> None:
    from backend.tools.web_tools import WebSearchTool

    schema = WebSearchTool(_HostedSearchLLM()).get_schema().to_openai_tool()
    description = schema["function"]["description"]

    # cc WebSearch prompt: the mandatory Sources section is part of the tool
    # contract the model sees.
    assert description.startswith(
        "Search the web for current information and return candidate titles, URLs, and snippets."
    )
    assert "Sources:" in description
    properties = schema["function"]["parameters"]["properties"]
    assert set(properties) == {"query", "allowed_domains", "blocked_domains"}


def test_web_search_schema_matches_openai_hosted_tool_domain_filter() -> None:
    from backend.tools.web_tools import WebSearchTool

    schema = WebSearchTool(
        _HostedSearchLLM(blocked_domains=False)
    ).get_schema().to_openai_tool()

    assert set(schema["function"]["parameters"]["properties"]) == {
        "query",
        "allowed_domains",
    }


def test_web_tools_declare_open_world_runtime_metadata(tmp_path: Path) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.tools.web_tools import WebFetchTool, WebSearchTool

    fetch = WebFetchTool(ArtifactStore(storage_dir=tmp_path))
    search = WebSearchTool(_HostedSearchLLM())

    assert fetch.to_runtime_metadata()["open_world"] is True
    assert search.to_runtime_metadata()["open_world"] is True
    assert fetch.timeout_seconds == 60.0
    assert search.timeout_seconds == 60.0
    assert fetch.should_defer is False
    assert search.should_defer is False


def test_web_fetch_schema_description_guides_github_and_citations(
    tmp_path: Path,
) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.tools.web_tools import WebFetchTool

    schema = (
        WebFetchTool(ArtifactStore(storage_dir=tmp_path)).get_schema().to_openai_tool()
    )
    description = schema["function"]["description"]

    assert "processes it using an AI model" in description
    assert "small, fast model" in description
    assert "gh CLI" in description
    assert set(schema["function"]["parameters"]["properties"]) == {"url", "prompt"}
    assert schema["function"]["parameters"]["required"] == ["url", "prompt"]


def test_web_fetch_uses_bounded_small_model_policy_and_reuses_url_cache(
    tmp_path: Path,
) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.tools.web_tools import WebFetchTool

    class FakeLLM:
        def __init__(self) -> None:
            self.calls: list[tuple[list, object]] = []

        async def side_query(self, messages, *, options, turn_context=None):
            del turn_context
            self.calls.append((messages, options))
            return "requested facts"

    tool = WebFetchTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    client = _FakeStreamClient(
        _FakeStreamResponse(
            b"<html><body><main>Stable page facts</main></body></html>"
        )
    )
    tool._client = client
    llm = FakeLLM()
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        llm=llm,
    )

    first = asyncio.run(
        tool.execute(
            {"url": "https://example.com/cache", "prompt": "facts"},
            context,
        )
    )
    second = asyncio.run(
        tool.execute(
            {"url": "https://example.com/cache", "prompt": "other facts"},
            context,
        )
    )

    assert not first.is_error
    assert not second.is_error
    assert client.stream_calls == 1
    assert len(llm.calls) == 2
    options = llm.calls[0][1]
    assert options.operation == "web_fetch_apply"
    assert options.max_tokens is None
    assert options.use_small_fast_model is True
    assert options.disable_reasoning is True
    assert options.enable_prompt_cache is False


def test_web_fetch_prompt_failure_bubbles_up_without_returning_raw_page(
    tmp_path: Path,
) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.tools.web_tools import WebFetchTool

    class FailingLLM:
        async def side_query(self, messages, *, options, turn_context=None):
            del messages, options, turn_context
            raise ConnectionError("incomplete chunked read")

    tool = WebFetchTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        llm=FailingLLM(),
    )

    with pytest.raises(ConnectionError, match="incomplete chunked read"):
        asyncio.run(
            tool._extract_with_prompt(
                "RAW PAGE CONTENT MUST NOT BE RETURNED",
                "Summarize the page",
                context,
            )
        )


def test_web_fetch_stream_path_enforces_wire_limit_and_checks_direct_peer() -> None:
    from backend.tools.web_tools import WebFetchTool
    from backend.tools.web_support import (
        WEB_FETCH_MAX_WIRE_BYTES,
        _actual_peer_network_error,
    )

    class _NetworkStream:
        def get_extra_info(self, name):
            return ("192.168.1.20", 443) if name == "server_addr" else None

    class _Response:
        status_code = 200
        headers = {"content-length": "4"}
        request = None
        extensions = {"network_stream": _NetworkStream()}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_bytes(self):
            yield b"body"

    class _Client:
        def stream(self, *_args, **_kwargs):
            return _Response()

    response = asyncio.run(WebFetchTool._get_limited_response(_Client(), "https://example.com"))
    assert response.content == b"body"
    assert "private peer" in _actual_peer_network_error(response, "https://example.com")
    assert _actual_peer_network_error(response, "https://example.com", proxy_url="http://proxy") == ""

    class _TooLarge(_Response):
        headers = {"content-length": str(WEB_FETCH_MAX_WIRE_BYTES + 1)}

    class _TooLargeClient:
        def stream(self, *_args, **_kwargs):
            return _TooLarge()

    with pytest.raises(RuntimeError, match="fetch limit"):
        asyncio.run(WebFetchTool._get_limited_response(_TooLargeClient(), "https://example.com"))


def test_network_assessment_rejects_invalid_ports_before_or_without_dns() -> None:
    from backend.permissions.network import assess_network_url

    for resolve_dns in (True, False):
        assessment = assess_network_url(
            "https://example.com:invalid/image.png",
            resolve_dns=resolve_dns,
        )
        assert assessment.allowed is False
        assert assessment.reason == "URL port is invalid"


def test_web_fetch_reads_actual_httpcore_server_addr_metadata() -> None:
    from backend.permissions.network import connected_peer_ip
    from backend.tools.web_tools import WebFetchTool

    async def scenario() -> tuple[bytes, str | None]:
        async def handle(reader, writer):
            await reader.read(4096)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nbody"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            import httpx

            async with httpx.AsyncClient(trust_env=False) as client:
                response = await WebFetchTool._get_limited_response(
                    client,
                    f"http://127.0.0.1:{port}/",
                )
            return response.content, connected_peer_ip(response)
        finally:
            server.close()
            await server.wait_closed()

    content, peer = asyncio.run(scenario())
    assert content == b"body"
    assert peer == "127.0.0.1"


def test_web_fetch_does_not_reapply_gzip_headers_to_decoded_body() -> None:
    from backend.permissions.network import connected_peer_ip
    from backend.tools.web_tools import WebFetchTool

    original = "MiniCode gzip response · 已解码".encode("utf-8")
    compressed = gzip.compress(original)

    async def scenario():
        async def handle(reader, writer):
            await reader.read(4096)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Content-Encoding: gzip\r\n"
                + f"Content-Length: {len(compressed)}\r\n".encode("ascii")
                + b"X-MiniCode-Test: preserved\r\n"
                + b"Connection: close\r\n\r\n"
                + compressed
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            import httpx

            async with httpx.AsyncClient(trust_env=False) as client:
                response = await WebFetchTool._get_limited_response(
                    client,
                    f"http://127.0.0.1:{port}/gzip",
                )
            return response
        finally:
            server.close()
            await server.wait_closed()

    response = asyncio.run(scenario())

    assert response.content == original
    assert response.text == original.decode("utf-8")
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(original))
    assert response.headers["x-minicode-test"] == "preserved"
    assert connected_peer_ip(response) == "127.0.0.1"


def test_web_fetch_bypass_mode_preserves_explicit_full_network_access(
    tmp_path: Path,
) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.tools.web_tools import WebFetchTool

    class _Stream:
        def get_extra_info(self, name):
            return ("169.254.169.254", 80) if name == "server_addr" else None

    class _Response:
        status_code = 200
        headers = {}
        content = b"ok"
        extensions = {"network_stream": _Stream()}

    class _ResponseContext(_Response):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_bytes(self):
            yield self.content

    class _Client:
        def stream(self, _method, _url, **_kwargs):
            return _ResponseContext()

    async def scenario():
        tool = WebFetchTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
        tool._client = _Client()
        return await tool._get_with_permitted_redirects(
            "https://public-name.example/",
            enforce_network=False,
        )

    response, redirect = asyncio.run(scenario())
    assert response.status_code == 200
    assert redirect is None


def test_web_search_uses_exact_hosted_side_query_contract() -> None:
    from backend.tools.web_tools import WebSearchTool

    llm = _HostedSearchLLM()
    result = asyncio.run(
        WebSearchTool(llm).execute(
            {"query": "current release", "allowed_domains": ["example.com"]}
        )
    )

    assert not result.is_error
    assert result.extraction_status == "ok"
    assert result.evidence_type == "candidate"
    assert '<untrusted_tool_result source="web_search">' in result.content
    assert "https://example.com/result" in result.content
    assert len(llm.calls) == 1
    messages, options = llm.calls[0]
    assert [(message.role, message.content) for message in messages] == [
        ("system", "You are an assistant for performing a web search tool use"),
        ("user", "Perform a web search for the query: current release"),
    ]
    assert options.operation == "web_search_tool"
    assert options.disable_reasoning is True
    assert options.enable_prompt_cache is False
    assert options.hosted_web_search is True
    assert options.web_search_allowed_domains == ("example.com",)
    assert options.web_search_blocked_domains == ()


def test_web_search_rejects_unsupported_or_conflicting_domain_filters() -> None:
    from backend.tools.web_tools import WebSearchTool

    openai_llm = _HostedSearchLLM(blocked_domains=False)
    blocked = asyncio.run(
        WebSearchTool(openai_llm).execute(
            {"query": "release", "blocked_domains": ["example.com"]}
        )
    )
    conflicting = asyncio.run(
        WebSearchTool(_HostedSearchLLM()).execute(
            {
                "query": "release",
                "allowed_domains": ["example.com"],
                "blocked_domains": ["blocked.example"],
            }
        )
    )

    assert blocked.is_error
    assert "blocked_domains is unavailable" in blocked.content
    assert openai_llm.calls == []
    assert conflicting.is_error
    assert "Cannot specify both" in conflicting.content


def test_web_search_provider_failure_does_not_fall_back_to_scraping() -> None:
    from backend.tools.web_tools import WebSearchTool

    result = asyncio.run(
        WebSearchTool(_HostedSearchLLM(error=RuntimeError("provider unavailable"))).execute(
            {"query": "current release"}
        )
    )

    assert result.is_error
    assert result.extraction_status == "failed"
    assert result.content == "Hosted web search failed: provider unavailable"


def test_web_search_uses_direct_rss_fallback_without_hosted_provider(monkeypatch) -> None:
    from backend.tools.web_tools import WebSearchTool

    class _Response:
        status_code = 200
        headers = {"content-type": "application/rss+xml"}
        content = b"""<?xml version=\"1.0\"?><rss><channel>
          <item><title>Example headline</title><link>https://example.com/news</link><description>Example summary</description></item>
        </channel></rss>"""

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def stream(self, *_args, **_kwargs):
            return _FakeStreamResponse(_Response.content, headers=_Response.headers)

    tool = WebSearchTool()
    tool._client = _Client()
    tool._proxy_url = "http://proxy.example"
    monkeypatch.setattr("backend.tools.web_tools.assess_network_url", lambda _url: type("A", (), {"allowed": True})())

    result = asyncio.run(tool.execute({"query": "example news"}))

    assert not result.is_error
    assert result.extraction_status == "ok"
    assert "Example headline" in result.content
    assert "https://example.com/news" in result.content
    assert result.provider == "bing-rss"


def test_web_fetch_anti_bot_site_is_limited_not_fatal(tmp_path: Path) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.tools.web_tools import WebFetchTool

    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = WebFetchTool(store)
    mock_client = _FakeStreamClient(error=RuntimeError("network unavailable"))
    tool._client = mock_client

    result = asyncio.run(
        tool.execute(
            {
                "url": "https://www.zhihu.com/question/123",
                "prompt": "Extract the answer.",
            },
            # The test is about hostile-site classification after the request
            # is attempted. Explicitly opt into network access so it does not
            # depend on this machine's DNS/private-peer assessment.
            context=ToolExecutionContext(permission=PermissionContext(mode="bypass")),
        )
    )

    assert not result.is_error
    assert result.extraction_status == "failed"
    assert result.limitation == "blocked by site"
    assert "blocked direct extraction" in result.content.lower()
