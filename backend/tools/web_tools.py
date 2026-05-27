"""
Web 工具（DESIGN.md §8.2）。

  - web_fetch:  抓取 URL 内容，返回清洗正文 + 来源 + 抓取状态。权限: AUTO
  - web_search: 联网搜索（DuckDuckGo HTML）。只返回候选来源列表。权限: AUTO

工具结果契约：
  - source_url: 数据来源
  - extraction_status: ok | partial | failed
  - content_preview: 清洗后正文前 N 字符
  - artifact_id: 大内容存储引用
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.html_sanitizer import assess_extraction, sanitize_html

logger = logging.getLogger(__name__)

WEB_FETCH_TOKEN_LIMIT = 2000
WEB_FETCH_TIMEOUT = 15
WEB_FETCH_MAX_CHARS = 200_000
CONTENT_PREVIEW_CHARS = 800

WEB_SEARCH_MAX_RESULTS = 8
WEB_SEARCH_TIMEOUT = 12


class WebFetchTool(BaseTool):
    """抓取 URL 内容，返回清洗后正文 + 来源元数据。"""

    name = "web_fetch"
    read_only = True
    description = (
        "抓取指定 URL 的网页内容并返回清洗后的正文文本。"
        "返回结果包含 source_url、extraction_status（ok/partial/failed）和正文预览。"
        "示例: web_fetch(url='https://docs.python.org/3/library/asyncio.html')。"
        "注意: 会发起外部网络请求。部分网站可能拒绝访问。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import httpx
        except ImportError:
            raise RuntimeError("需要安装 httpx: pip install httpx")

        self._client = httpx.AsyncClient(
            timeout=WEB_FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
            headers={
                "User-Agent": "MiniCode/0.2 (AI Agent; +https://github.com/minicode)",
                "Accept": "text/html,text/plain,application/json,*/*",
            },
        )
        return self._client

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的完整 URL（需包含 http:// 或 https://）",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "最大抓取字符数，默认 200000",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        url = args.get("url", "")
        max_length = args.get("max_length", WEB_FETCH_MAX_CHARS)

        if not url:
            return self._error_result("缺少 url 参数")

        if not url.startswith(("http://", "https://")):
            return self._error_result("URL 必须以 http:// 或 https:// 开头")

        try:
            client = self._get_client()
            resp = await client.get(url)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()

            raw = resp.content
            if len(raw) > max_length:
                raw = raw[:max_length]

            raw_text = raw.decode(charset, errors="replace")

        except Exception as exc:
            logger.warning("web_fetch 失败 %s: %s", url, exc)
            return ToolResult(
                content=f"抓取失败: {exc}",
                is_error=True,
                source_url=url,
                extraction_status="failed",
                evidence_type="fetched",
            )

        # JSON: return directly with metadata
        if "application/json" in content_type:
            estimated_tokens = len(raw_text) // 4
            preview = raw_text[:CONTENT_PREVIEW_CHARS]
            if estimated_tokens <= WEB_FETCH_TOKEN_LIMIT:
                return ToolResult(
                    content=raw_text[:8000],
                    source_url=url,
                    extraction_status="ok",
                    evidence_type="fetched",
                    content_preview=preview,
                )
            artifact_id = self._artifact_store.save(
                content=raw_text[:max_length],
                source=f"web_fetch({url})",
                type="json_content",
            )
            return ToolResult(
                content=f"已抓取 JSON（约 {estimated_tokens} tokens）",
                source_url=url,
                extraction_status="ok",
                evidence_type="fetched",
                artifact_id=artifact_id,
                content_preview=preview,
            )

        # HTML → sanitized text
        raw_length = len(raw_text)
        if "<html" in raw_text.lower() or "<body" in raw_text.lower():
            cleaned = sanitize_html(raw_text)
        else:
            cleaned = raw_text.strip()

        status = assess_extraction(cleaned, raw_length)
        preview = cleaned[:CONTENT_PREVIEW_CHARS] if cleaned else ""
        estimated_tokens = len(cleaned) // 4

        if estimated_tokens <= WEB_FETCH_TOKEN_LIMIT:
            return ToolResult(
                content=cleaned[:8000] if cleaned else "页面无可提取正文内容",
                source_url=url,
                extraction_status=status,
                evidence_type="fetched",
                content_preview=preview,
            )

        # Large content → artifact (store cleaned text, not raw HTML)
        artifact_id = self._artifact_store.save(
            content=cleaned[:max_length],
            source=f"web_fetch({url})",
            type="web_content",
        )
        artifact_preview = self._artifact_store.get_preview(artifact_id, lines=10)

        return ToolResult(
            content=f"已抓取 {url}（约 {estimated_tokens} tokens，extraction: {status}）",
            source_url=url,
            extraction_status=status,
            evidence_type="fetched",
            artifact_id=artifact_id,
            artifact_preview=artifact_preview,
            content_preview=preview,
        )


class WebSearchTool(BaseTool):
    """联网搜索工具 — 只返回候选来源列表，不把摘要当事实。"""

    name = "web_search"
    read_only = True
    description = (
        "使用 DuckDuckGo 搜索引擎查询信息。"
        "只返回候选来源列表（标题 + URL + 摘要片段），不保证摘要准确。"
        "要获取可靠内容，需对感兴趣的 URL 调用 web_fetch。"
        "示例: web_search(query='fastapi 0.115 changelog')。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import httpx
        except ImportError:
            raise RuntimeError("需要安装 httpx: pip install httpx")

        self._client = httpx.AsyncClient(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=3,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        return self._client

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询词，支持中英文",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"最多返回结果数量，默认 {WEB_SEARCH_MAX_RESULTS}，最大 20",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        query = args.get("query", "").strip()
        max_results = min(int(args.get("max_results", WEB_SEARCH_MAX_RESULTS)), 20)

        if not query:
            return self._error_result("缺少 query 参数")

        try:
            results = await self._duckduckgo_search(query, max_results)
        except Exception as exc:
            logger.warning("web_search 失败 query=%r: %s", query, exc)
            return ToolResult(
                content=f"搜索失败: {exc}",
                is_error=True,
                extraction_status="failed",
                evidence_type="candidate",
            )

        if not results:
            return ToolResult(
                content=f"搜索 '{query}' 未返回结果。请尝试更换关键词。",
                extraction_status="failed",
                evidence_type="candidate",
            )

        lines = [
            f"搜索 '{query}' 返回 {len(results)} 条候选来源。",
            "证据类型：candidate（候选来源，不可作为最终事实引用）。",
            "注意：以下摘要仅供参考，不保证准确。如需可靠内容请对目标 URL 调用 web_fetch。\n",
        ]
        for i, result in enumerate(results, 1):
            lines.append(f"[{i}] {result['title']}")
            lines.append(f"    URL: {result['url']}")
            if result.get("snippet"):
                lines.append(f"    片段: {result['snippet']}")
            lines.append("")

        content = "\n".join(lines).strip()
        return ToolResult(
            content=content,
            extraction_status="ok",
            evidence_type="candidate",
        )

    async def _duckduckgo_search(self, query: str, max_results: int) -> list[dict[str, str]]:
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}&kl=cn-zh"

        client = self._get_client()
        resp = await client.get(url)
        resp.raise_for_status()

        html = resp.text
        results = self._parse_ddg_html(html, max_results)
        return results

    @staticmethod
    def _parse_ddg_html(html: str, max_results: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []

        result_blocks = re.findall(
            r'<div class="result[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>',
            html,
            flags=re.S | re.I,
        )

        for block in result_blocks:
            if len(results) >= max_results:
                break

            title_match = re.search(
                r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                block,
                flags=re.S | re.I,
            )
            if not title_match:
                continue

            raw_url = title_match.group(1)
            title_html = title_match.group(2)
            title = re.sub(r"<[^>]+>", "", title_html).strip()

            real_url = _extract_ddg_url(raw_url)
            if not real_url:
                continue

            snippet_match = re.search(
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                block,
                flags=re.S | re.I,
            )
            snippet = ""
            if snippet_match:
                snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                snippet = re.sub(r"\s+", " ", snippet)

            if title and real_url:
                results.append({
                    "title": title,
                    "url": real_url,
                    "snippet": snippet[:280],
                })

        if not results:
            results = _fallback_parse_ddg(html, max_results)

        return results


def _extract_ddg_url(raw: str) -> str:
    if raw.startswith("http"):
        return raw
    m = re.search(r"uddg=([^&]+)", raw)
    if m:
        try:
            return urllib.parse.unquote(m.group(1))
        except Exception:
            pass
    return ""


def _fallback_parse_ddg(html: str, max_results: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="(https?://[^"]{10,})"[^>]*>(.*?)</a>', html, flags=re.S | re.I):
        if len(results) >= max_results:
            break
        url = m.group(1)
        if "duckduckgo.com" in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if title:
            results.append({"title": title, "url": url, "snippet": ""})
    return results
