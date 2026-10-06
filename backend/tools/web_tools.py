"""
Web 工具（DESIGN.md §8.2）。

  - web_fetch:  抓取 URL 内容，返回清洗正文 + 来源 + 抓取状态。权限: AUTO
  - web_search: provider-hosted or direct public web search. 权限: AUTO

工具结果契约：
  - source_url: 数据来源
  - extraction_status: ok | partial | failed
  - content_preview: 清洗后正文前 N 字符
  - artifact_id: 大内容存储引用
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any

import httpx

from backend.artifact.store import ArtifactStore
from backend.permissions.context import ToolExecutionContext
from backend.permissions.network import (
    assess_network_url,
    snapshot_response_extensions,
)
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.html_sanitizer import assess_extraction, sanitize_html_with_status
from backend.llm.errors import (
    classify_llm_error,
    llm_error_raw,
    sanitize_llm_error_message,
)
from backend.tools.web_support import (
    public_http_transport,
    _actual_peer_network_error,
    _assert_response_length_within_limit,
    _decoded_response_headers,
    _detect_proxy,
    _is_hostile_fetch_url,
    _is_permitted_redirect,
    _normalize_domain_list,
    _strip_www,
    _url_has_credentials,
    _wrap_untrusted_content,
    _read_response_bytes,
)
logger = logging.getLogger(__name__)

WEB_FETCH_MAX_CHARS = 100_000
WEB_FETCH_CACHE_TTL_SECONDS = 15 * 60
WEB_FETCH_CACHE_MAX_BYTES = 50 * 1024 * 1024
WEB_FETCH_MAX_URL_LENGTH = 2_000
CONTENT_PREVIEW_CHARS = 800
WEB_REQUEST_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _WebFetchCacheEntry:
    content: str
    extraction_status: str
    artifact_type: str
    stored_at: float
    size_bytes: int
    limitation: str


class WebFetchTool(BaseTool):
    """抓取 URL 内容，返回清洗后正文 + 来源元数据。"""

    name = "web_fetch"
    should_defer = False
    read_only = True
    open_world = True
    result_kind = "web"
    activity_kind = "webSearch"
    display_label = "Fetch"
    timeout_seconds = WEB_REQUEST_TIMEOUT_SECONDS
    # Processed markdown is capped at 100K characters.
    max_result_chars = WEB_FETCH_MAX_CHARS
    description = """IMPORTANT: WebFetch WILL FAIL for authenticated or private URLs. Before using this tool, check whether the URL points to an authenticated service such as Google Docs, Confluence, Jira, or GitHub. If so, use a specialized MCP tool when one is available.

Fetches content from a specified URL and processes it using an AI model. Takes a URL and a prompt, converts HTML to text, uses the configured small, fast model or the current model when no auxiliary model is configured, and returns the model's response. Prefer an MCP-provided fetch tool when available. The URL must be fully formed. For GitHub URLs, prefer the gh CLI."""
    permission = PermissionLevel.AUTO

    def model_description(self) -> str:
        return self.description

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "required": ["url", "prompt"],
                "properties": {
                    "url": {"type": "string", "description": "HTTP or HTTPS URL of the page to fetch."},
                    "prompt": {
                        "type": "string",
                        "description": "The prompt to run on the fetched content.",
                    },
                },
            },
        )

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store
        self._client = None
        self._unrestricted_client = None
        self._proxy_url: str | None = None
        self._url_cache: OrderedDict[tuple[str, bool], _WebFetchCacheEntry] = OrderedDict()
        self._url_cache_size_bytes = 0

    async def _extract_with_prompt(self, text: str, prompt: str, context) -> str:
        """Apply the WebFetch secondary-model prompt contract."""

        llm = getattr(context, "llm", None) if context else None
        if llm is None:
            return ""
        from backend.llm.base import LLMMessage, SideQueryOptions

        content = (
            "Web page content:\n---\n"
            f"{text[:WEB_FETCH_MAX_CHARS]}\n"
            "---\n\n"
            f"{prompt}\n\n"
            "Provide a concise response based only on the content above."
        )
        messages = [LLMMessage(role="user", content=content)]
        side_query = getattr(llm, "side_query", None)
        if callable(side_query):
            return await side_query(
                messages,
                options=SideQueryOptions(
                    operation="web_fetch_apply",
                    query_source="background",
                    use_small_fast_model=bool(llm.configured_small_fast_model_id()),
                    disable_reasoning=True,
                    enable_prompt_cache=False,
                ),
                turn_context=(
                    context.run_context.llm_turn_context
                    if context is not None and context.run_context is not None
                    else None
                ),
            )
        return await llm.simple_chat(messages)

    def _cached_content(self, url: str, *, enforce_network: bool) -> _WebFetchCacheEntry | None:
        key = (url, enforce_network)
        entry = self._url_cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stored_at >= WEB_FETCH_CACHE_TTL_SECONDS:
            self._url_cache.pop(key)
            self._url_cache_size_bytes -= entry.size_bytes
            return None
        self._url_cache.move_to_end(key)
        return entry

    def _cache_content(
        self,
        url: str,
        *,
        content: str,
        extraction_status: str,
        artifact_type: str,
        enforce_network: bool,
        limitation: str = "",
    ) -> None:
        key = (url, enforce_network)
        previous = self._url_cache.pop(key, None)
        if previous is not None:
            self._url_cache_size_bytes -= previous.size_bytes
        size_bytes = max(1, len(content.encode("utf-8")))
        self._url_cache[key] = _WebFetchCacheEntry(
            content=content,
            extraction_status=extraction_status,
            artifact_type=artifact_type,
            stored_at=time.monotonic(),
            size_bytes=size_bytes,
            limitation=limitation,
        )
        self._url_cache_size_bytes += size_bytes
        self._url_cache.move_to_end(key)
        while self._url_cache_size_bytes > WEB_FETCH_CACHE_MAX_BYTES:
            _, evicted = self._url_cache.popitem(last=False)
            self._url_cache_size_bytes -= evicted.size_bytes

    def _get_client(self, *, enforce_network: bool = True):
        client = self._client if enforce_network else self._unrestricted_client
        if client is not None:
            return client

        try:
            import httpx
        except ImportError:
            raise RuntimeError("需要安装 httpx: pip install httpx")

        # Detect system proxy and configure explicitly to avoid httpx auto-detection issues
        proxy_url = _detect_proxy()

        client = httpx.AsyncClient(
            timeout=WEB_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            proxy=proxy_url,
            trust_env=False,
            transport=public_http_transport() if enforce_network and proxy_url is None else None,
            headers={
                "User-Agent": "MiniCode/0.2 (AI Agent; +https://github.com/minicode)",
                "Accept": "text/html,text/plain,application/json,*/*",
            },
        )
        if enforce_network:
            self._client = client
            self._proxy_url = proxy_url
        else:
            self._unrestricted_client = client
        return client

    async def _get_with_permitted_redirects(
        self,
        url: str,
        max_redirects: int = 10,
        *,
        enforce_network: bool = True,
    ):
        """Fetch, following only same-host redirects.

        Returns (response, redirect_url). When a cross-host redirect is hit,
        response is None and redirect_url is the target for the model to re-fetch.
        """
        client = self._get_client(enforce_network=enforce_network)
        current = url
        for _ in range(max_redirects + 1):
            if enforce_network:
                assessment = await asyncio.to_thread(
                    assess_network_url,
                    current,
                )
                if not assessment.allowed:
                    raise RuntimeError(f"Network target blocked: {assessment.reason}")
            resp = await self._get_limited_response(client, current)
            if enforce_network:
                peer_error = _actual_peer_network_error(
                    resp,
                    current,
                    proxy_url=self._proxy_url,
                )
                if peer_error:
                    raise RuntimeError(peer_error)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return resp, None
                redirect_url = urllib.parse.urljoin(current, location)
                if _is_permitted_redirect(current, redirect_url):
                    if enforce_network:
                        assessment = await asyncio.to_thread(
                            assess_network_url,
                            redirect_url,
                        )
                        if not assessment.allowed:
                            raise RuntimeError(
                                f"Redirect target blocked: {assessment.reason}"
                            )
                    current = redirect_url
                    continue
                return None, redirect_url
            return resp, None
        raise RuntimeError(f"Too many redirects (exceeded {max_redirects})")

    @staticmethod
    async def _get_limited_response(client: Any, url: str) -> Any:
        """Read one HTTP response with a hard *decompressed* byte ceiling.

        The MiniCode-owned HTTP client is consumed through ``stream`` so a
        hostile server cannot make ``response.content`` allocate an unbounded
        body before the byte ceiling is enforced.
        """

        import httpx

        stream_method = getattr(client, "stream", None)
        if not callable(stream_method):
            raise RuntimeError("HTTP client does not provide the MiniCode streaming GET boundary")
        stream_context = stream_method("GET", url)
        if not hasattr(stream_context, "__aenter__"):
            raise RuntimeError("HTTP client streaming GET did not return an async context manager")
        async with stream_context as response:
            _assert_response_length_within_limit(response)
            extensions = snapshot_response_extensions(response)
            body = await _read_response_bytes(response)
            return httpx.Response(
                response.status_code,
                headers=_decoded_response_headers(response),
                content=body,
                request=getattr(response, "request", None),
                extensions=extensions,
            )
    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="web.fetch",
            required_args=("url", "prompt"),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["url", "prompt"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to fetch (must start with http:// or https://).",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The prompt to run on the fetched content.",
                    },
                },
            },
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        url = str(args.get("url") or "").strip()
        prompt = str(args.get("prompt") or "").strip()

        if not url:
            return self._error_result("Missing url parameter")
        if not prompt:
            return self._error_result("Missing prompt parameter")

        if not url.startswith(("http://", "https://")):
            return self._error_result("URL must start with http:// or https://")
        if len(url) > WEB_FETCH_MAX_URL_LENGTH:
            return self._error_result(
                f"URL exceeds the {WEB_FETCH_MAX_URL_LENGTH} character limit"
            )
        if any(ord(char) < 0x20 for char in url):
            return self._error_result("URL contains control characters")

        if _url_has_credentials(url):
            return self._error_result(
                "URL must not embed credentials (username:password). Remove them and retry."
            )

        enforce_network = context is None or context.permission.mode != "bypass"
        if enforce_network:
            assessment = assess_network_url(url)
            if not assessment.allowed:
                return self._error_result(
                    f"Network target requires approval or is blocked: {assessment.reason}"
                )

        cached = self._cached_content(url, enforce_network=enforce_network)
        if cached is None:
            try:
                resp, redirect_url = await self._get_with_permitted_redirects(
                    url,
                    enforce_network=enforce_network,
                )
                if resp is None:
                    # Cross-host redirect: return the target for the model to re-fetch
                    # explicitly instead of silently following (open-redirect/SSRF guard).
                    return ToolResult(
                        content=(
                            f"{url} redirects to a different host: {redirect_url}\n\n"
                            "The redirect was NOT followed automatically. If this destination is "
                            "expected, call web_fetch again with that URL."
                        ),
                        is_error=False,
                        source_url=url,
                        extraction_status="partial",
                        status="partial",
                        evidence_type="fetched",
                        display_summary=f"Cross-host redirect: {urlparse(redirect_url).netloc or redirect_url}",
                        result_kind="web",
                    )
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                charset = "utf-8"
                if "charset=" in content_type:
                    charset = content_type.split("charset=")[-1].split(";")[0].strip()

                raw_text = resp.content.decode(charset, errors="replace")

            except Exception as exc:
                detail = f"{type(exc).__name__}: {str(exc).strip()}".rstrip(": ")
                logger.warning("web_fetch 失败 %s: %s", url, detail)
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in {401, 403} or _is_hostile_fetch_url(url):
                    return ToolResult(
                        content=f"Fetch failed for {url}: the site blocked direct extraction ({status_code or 'anti-bot'}).",
                        is_error=False,
                        source_url=url,
                        extraction_status="failed",
                        status="partial",
                        evidence_type="fetched",
                        limitation="blocked by site",
                        display_summary=f"Fetch limited: {urlparse(url).netloc or url}",
                        result_kind="web",
                    )
                if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                    error_kind = "web_fetch_timeout"
                    user_summary = "网页读取超时，未获取页面正文。"
                elif isinstance(exc, httpx.HTTPStatusError):
                    error_kind = "web_fetch_http_status"
                    user_summary = f"网页来源返回 HTTP {status_code}，未能读取页面内容。"
                elif isinstance(exc, httpx.RequestError):
                    error_kind = "web_fetch_network"
                    user_summary = "网页读取失败，网络连接未完成或已中断。"
                else:
                    error_kind = "web_fetch_error"
                    user_summary = "网页读取失败，未获取页面正文。"
                return ToolResult(
                    content=f"Fetch failed for {url}: {detail}",
                    is_error=True,
                    source_url=url,
                    extraction_status="failed",
                    evidence_type="fetched",
                    result_kind="web",
                    error_kind=error_kind,
                    user_summary=user_summary,
                    developer_detail=detail,
                    display_summary=f"读取失败：{urlparse(url).netloc}",
                    model_observation="The fetch failed. No page content was returned.",
                )

            raw_length = len(raw_text)
            limitation = ""
            has_body = bool(raw_text.strip())
            if "application/json" in content_type:
                cleaned = raw_text.strip()
                status = "ok" if cleaned else "failed"
                artifact_type = "json_content"
                if cleaned and self._boss_blocked_response(url, cleaned):
                    status = "failed"
                    limitation = "The site rejected direct extraction."
            else:
                if "text/html" in content_type or "<html" in raw_text.lower() or "<body" in raw_text.lower():
                    cleaned, status, limitation, has_body = sanitize_html_with_status(raw_text)
                else:
                    cleaned = raw_text.strip()
                    status = assess_extraction(cleaned, raw_length)
                artifact_type = "web_content"
            if len(cleaned) > WEB_FETCH_MAX_CHARS:
                cleaned = cleaned[:WEB_FETCH_MAX_CHARS] + "\n\n[Content truncated due to length...]"
                status = "partial" if status == "ok" else status
                limitation = " ".join(filter(None, (limitation, "Page content exceeds the extraction limit.")))
            if status != "failed" and has_body:
                self._cache_content(
                    url,
                    content=cleaned,
                    extraction_status=status,
                    artifact_type=artifact_type,
                    enforce_network=enforce_network,
                    limitation=limitation,
                )
        else:
            cleaned = cached.content
            status = cached.extraction_status
            artifact_type = cached.artifact_type
            limitation = cached.limitation
            has_body = True

        preview = cleaned[:CONTENT_PREVIEW_CHARS] if cleaned else ""
        artifact_id = self._artifact_store.save(
            content=cleaned,
            source=f"web_fetch({url})",
            type=artifact_type,
        )
        artifact_preview = self._artifact_store.get_preview(artifact_id)
        if status == "failed" or not has_body:
            return ToolResult(
                content=_wrap_untrusted_content(
                    f"No usable page body was fetched. {limitation or 'The response contained no readable content.'}\n{cleaned}",
                    "web_fetch",
                ),
                status="partial",
                source_url=url,
                extraction_status=status,
                evidence_type="fetched",
                artifact_id=artifact_id,
                artifact_preview=artifact_preview,
                content_preview=preview,
                limitation=limitation or "No readable page body was available.",
                display_summary=f"Fetch limited: {urlparse(url).netloc or url}",
                result_kind="web",
            )
        extraction_error: Exception | None = None
        try:
            extracted = await self._extract_with_prompt(cleaned, prompt, context)
        except Exception as exc:
            extraction_error = exc
            extracted = ""
        if extracted:
            return ToolResult(
                content=_wrap_untrusted_content(extracted, "web_fetch"),
                source_url=url,
                extraction_status=status,
                evidence_type="fetched",
                content_preview=preview,
                artifact_id=artifact_id,
                artifact_preview=artifact_preview,
                status="partial" if status == "partial" else "success",
                limitation=limitation,
                display_summary=f"Fetched {urlparse(url).netloc or url}",
            )

        if extraction_error is not None:
            classification = classify_llm_error(extraction_error)
            provider = str(
                getattr(getattr(context, "llm", None), "_provider_id", "")
                or getattr(getattr(getattr(context, "llm", None), "_settings", None), "provider", "")
                or "configured_provider"
            ).strip()
            detail = sanitize_llm_error_message(
                extraction_error,
                classification,
                include_provider_details=True,
            )
            provider_message = str(
                llm_error_raw(extraction_error, provider).get("provider_error_message")
                or ""
            ).strip()
            if provider_message:
                detail = f"{detail} provider_message={provider_message}"
            return ToolResult(
                content=f"网页已抓取；模型二次提取失败。原始清洗内容保存在 artifact {artifact_id}。",
                is_error=True,
                source_url=url,
                extraction_status="failed",
                evidence_type="fetched",
                artifact_id=artifact_id,
                artifact_preview=artifact_preview,
                content_preview=preview,
                display_summary=f"已抓取 {urlparse(url).netloc or url}，提取失败",
                result_kind="web",
                limitation="网页抓取成功；模型二次提取失败，原始清洗内容已保存为 artifact",
                provider=provider,
                provider_error_type=classification.provider_error_type,
                error_kind=classification.error_type,
                user_summary="网页已抓取，但模型提取失败。",
                developer_detail=detail,
                recoverable=classification.retryable,
                projection="error",
                model_observation=(
                    "The page was fetched; model extraction failed. The cleaned page is saved as an artifact."
                ),
            )

        return ToolResult(
            content=f"网页已抓取；当前会话没有可用模型执行提取。原始清洗内容保存在 artifact {artifact_id}。",
            is_error=True,
            source_url=url,
            extraction_status=status,
            evidence_type="fetched",
            artifact_id=artifact_id,
            artifact_preview=artifact_preview,
            content_preview=preview,
            display_summary=f"已抓取 {urlparse(url).netloc or url}，未执行提取",
            result_kind="web",
            limitation="网页抓取成功；当前会话没有可用模型，原始清洗内容已保存为 artifact",
            error_kind="provider_unavailable",
            user_summary="网页已抓取，但没有可用模型执行提取。",
            recoverable=False,
            projection="error",
            model_observation=(
                "The page was fetched; extraction was not run because no session model was available. "
                "The cleaned page is saved as an artifact."
            ),
        )

    @staticmethod
    def _boss_blocked_response(url: str, content: str) -> bool:
        host = urlparse(url).hostname or ""
        if host != "zhipin.com" and not host.endswith(".zhipin.com"):
            return False
        payload = json.loads(content)
        return isinstance(payload, dict) and payload.get("code") == 37


class WebSearchTool(BaseTool):
    """Search through a declared hosted capability or configured search API."""

    name = "web_search"
    should_defer = False
    read_only = True
    open_world = True
    result_kind = "search"
    activity_kind = "webSearch"
    display_label = "Search web"
    timeout_seconds = WEB_REQUEST_TIMEOUT_SECONDS
    description = (
        "Search the web for current information and return candidate titles, URLs, and snippets. "
        "Use allowed_domains to restrict sites; requires declared hosted search or a configured TAVILY_API_KEY. "
        "CRITICAL REQUIREMENT: after answering the user's question, you MUST include a "
        '"Sources:" section at the end of your response listing all relevant URLs from the '
        "search results as markdown hyperlinks: [Title](URL). This is MANDATORY - never skip "
        "including sources."
    )
    permission = PermissionLevel.AUTO

    def model_description(self) -> str:
        return (
            "Search the web and return candidate titles, URLs, and snippets. "
            "Use allowed_domains for site-restricted research instead of building site operators into query. "
            "Requires a provider with declared hosted search or a configured TAVILY_API_KEY. "
            "If unavailable, discover an installed search tool with tool_search."
        )

    def _schema_properties(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "query": {
                "type": "string",
                "description": "The search query to use.",
            },
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restrict the search request to these domains, e.g. ['zhipin.com', 'nowcoder.com'].",
            },
        }
        # Building the tool directory must not instantiate a model or require
        # provider credentials. Factory-backed sources are resolved at execute.
        llm = None if callable(self._llm_provider) else self._llm_provider
        supports_hosted_search = callable(getattr(llm, "supports_hosted_web_search", None)) and llm.supports_hosted_web_search()
        supports_blocked_domains = not supports_hosted_search or (
            callable(getattr(llm, "hosted_web_search_supports_blocked_domains", None))
            and llm.hosted_web_search_supports_blocked_domains()
        )
        if supports_blocked_domains:
            properties["blocked_domains"] = {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exclude these domains. Requires a search source with domain exclusion support.",
            }
        return properties

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": self._schema_properties(),
            },
        )

    def __init__(self, llm_provider: Any | None = None) -> None:
        self._llm_provider = llm_provider
        self._client = None
        self._proxy_url: str | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import httpx
        except ImportError:
            raise RuntimeError("需要安装 httpx: pip install httpx")
        self._proxy_url = _detect_proxy()
        self._client = httpx.AsyncClient(
            timeout=WEB_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            proxy=self._proxy_url,
            trust_env=False,
            transport=public_http_transport() if self._proxy_url is None else None,
            headers={
                "User-Agent": "MiniCode/0.2 (AI Agent; +https://github.com/minicode)",
                "Accept": "application/json",
            },
        )
        return self._client

    @staticmethod
    def _domain_matches(url: str, domains: list[str]) -> bool:
        host = _strip_www(urlparse(url).hostname or "")
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    @staticmethod
    def _search_api_key() -> str:
        from backend.vault import EnvVault

        return os.getenv("TAVILY_API_KEY", "").strip() or (EnvVault().get("TAVILY_API_KEY") or "").strip()

    async def _direct_search(
        self,
        query: str,
        *,
        allowed_domains: list[str],
        blocked_domains: list[str],
    ) -> ToolResult:
        from backend.vault.store import VaultReadError

        try:
            api_key = self._search_api_key()
        except VaultReadError as exc:
            return ToolResult(
                content=f"Web search credential is unavailable: {exc}",
                is_error=True,
                extraction_status="failed",
                result_kind="search",
                error_kind="search_configuration",
                user_summary="搜索凭据不可用，请检查 Tavily 密钥配置。",
            )
        if not api_key:
            return ToolResult(
                content=(
                    "Web search is unavailable: the active provider has no declared hosted-search capability "
                    "and TAVILY_API_KEY is not configured. Use tool_search to discover an installed search tool, "
                    "or configure a supported search source. No search request was made."
                ),
                is_error=True,
                extraction_status="failed",
                result_kind="search",
                error_kind="search_unavailable",
                user_summary="当前没有可用搜索源。",
                display_summary="Web search unavailable",
            )
        search_url = "https://api.tavily.com/search"
        assessment = await asyncio.to_thread(assess_network_url, search_url)
        if not assessment.allowed:
            return self._error_result(f"Network target blocked: {assessment.reason}")
        client = self._get_client()
        request = {"query": query, "max_results": 8, "search_depth": "basic", "include_answer": False}
        if allowed_domains:
            request["include_domains"] = allowed_domains
        if blocked_domains:
            request["exclude_domains"] = blocked_domains
        async with client.stream(
            "POST", search_url, headers={"Authorization": f"Bearer {api_key}"}, json=request,
        ) as response:
            peer_error = _actual_peer_network_error(response, search_url, proxy_url=self._proxy_url)
            if peer_error:
                return self._error_result(peer_error)
            response.raise_for_status()
            payload = json.loads(await _read_response_bytes(response))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return self._error_result("The search provider returned no results array.")
        candidates = [(item["title"], item["url"], item.get("content", "")) for item in payload["results"]]
        filtered = [
            result for result in candidates
            if (not allowed_domains or self._domain_matches(result[1], allowed_domains))
            and (not blocked_domains or not self._domain_matches(result[1], blocked_domains))
        ][:8]
        if not filtered:
            return ToolResult(
                content=f'No web search results found for "{query}".',
                extraction_status="ok",
                evidence_type="candidate",
                display_summary=f"Searched web: {query}",
                result_kind="search",
                provider="tavily",
            )
        lines = [f'Search results for "{query}":']
        for index, (title, link, snippet) in enumerate(filtered, start=1):
            lines.extend([f"{index}. {title}", f"URL: {link}"])
            if snippet:
                lines.append(f"Snippet: {snippet}")
        content = "\n".join(lines)
        return ToolResult(
            content=_wrap_untrusted_content(content, "web_search"),
            extraction_status="ok",
            evidence_type="candidate",
            display_summary=f"Searched web: {query}",
            provider="tavily",
            result_kind="search",
            content_preview=content[:CONTENT_PREVIEW_CHARS],
        )

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="web.search",
            required_args=("query",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": self._schema_properties(),
            },
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        query = str(args.get("query") or "").strip()
        allowed_domains = _normalize_domain_list(args.get("allowed_domains"))
        blocked_domains = _normalize_domain_list(args.get("blocked_domains"))

        if not query:
            return self._error_result("缺少 query 参数")

        if allowed_domains and blocked_domains:
            return self._error_result(
                "Cannot specify both allowed_domains and blocked_domains in the same request."
            )

        llm = getattr(context, "llm", None) if context is not None else None
        if llm is None:
            llm = self._llm_provider() if callable(self._llm_provider) else self._llm_provider
        supports_hosted_search = callable(getattr(llm, "supports_hosted_web_search", None)) and llm.supports_hosted_web_search()
        if not supports_hosted_search:
            return await self._direct_search(
                query,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )
        if blocked_domains and not llm.hosted_web_search_supports_blocked_domains():
            return self._error_result(
                "blocked_domains is unavailable for the active hosted-search provider."
            )

        from backend.llm.base import LLMMessage, SideQueryOptions

        try:
            content = await llm.side_query(
                [
                    LLMMessage(
                        role="system",
                        content="You are an assistant for performing a web search tool use",
                    ),
                    LLMMessage(
                        role="user",
                        content=f"Perform a web search for the query: {query}",
                    ),
                ],
                options=SideQueryOptions(
                    operation="web_search_tool",
                    query_source="background",
                    disable_reasoning=True,
                    enable_prompt_cache=False,
                    hosted_web_search=True,
                    web_search_allowed_domains=tuple(allowed_domains),
                    web_search_blocked_domains=tuple(blocked_domains),
                ),
                turn_context=(
                    context.run_context.llm_turn_context
                    if context is not None and context.run_context is not None
                    else None
                ),
            )
        except Exception as exc:
            logger.warning("hosted web_search failed query=%r: %s", query, exc)
            return ToolResult(
                content=f"Hosted web search failed: {exc}",
                is_error=True,
                extraction_status="failed",
                evidence_type="candidate",
            )

        if not str(content or "").strip():
            return ToolResult(
                content="The hosted search returned no usable result text.",
                status="partial",
                extraction_status="partial",
                provider=type(llm).__name__,
                result_kind="search",
                evidence_type="candidate",
            )
        return ToolResult(
            content=_wrap_untrusted_content(content, "web_search"),
            extraction_status="ok",
            evidence_type="candidate",
            display_summary=f"Searched web: {query}",
            provider=type(llm).__name__,
            result_kind="search",
            content_preview=str(content or "")[:CONTENT_PREVIEW_CHARS],
        )
