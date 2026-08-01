"""
websearch MCP Server（DESIGN.md §六.2）。

传输：stdio（通过 stdin/stdout 与 MCPClient 通信）
依赖：ddgs（无需 API Key，优先）、duckduckgo-search（兼容 fallback）、trafilatura（正文提取）

功能概述：
  这是 MiniCode 的网页搜索能力提供者。Agent 可以通过此 Server 搜索互联网
  并获取网页内容，用于回答需要实时信息的问题。

Tools:
  search(query) → 搜索摘要
    返回 title + url + 摘要和引用

  fetch_page(url) → 页面正文
    使用 trafilatura 提取正文 Markdown；结果统一遵循 Pi 工具输出契约

输出大小由共享的 Pi 工具结果契约统一处理；此服务器不维护第二套字符阈值。

运行方式：python -m backend.mcp.servers.websearch
"""

from __future__ import annotations

import logging
import sys

from backend.tools.base import truncate_tool_result

logger = logging.getLogger(__name__)


class WebSearchProviderError(RuntimeError):
    """A provider/dependency failure that MCP must report as a tool error."""

# ── MCP Server 框架 ────────────────────────────────────────
# 使用 FastMCP 构建 Server（pip install mcp[cli]）

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    HAS_MCP = True
except ImportError:
    HAS_MCP = False
    ToolAnnotations = None  # type: ignore[assignment,misc]

if HAS_MCP:
    mcp = FastMCP(
        "websearch",
        instructions=(
            "网页搜索 MCP Server。提供互联网搜索和网页内容提取能力。"
            "返回结果遵循 Token-efficient 原则：默认只给摘要和引用。"
        ),
    )
else:
    mcp = None


# ── 搜索引擎封装 ────────────────────────────────────────────

def _search_duckduckgo(query: str, num_results: int | None = None) -> list[dict[str, str]]:
    """
    使用 DuckDuckGo 搜索（无需 API Key）。

    Returns:
        [{"title": "...", "url": "...", "snippet": "..."}, ...]
    """
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError as exc:
            raise WebSearchProviderError(
                "Web search provider is unavailable; install the 'search' dependencies."
            ) from exc

    try:
        results = []
        with DDGS() as ddgs:
            kwargs = {} if num_results is None else {"max_results": num_results}
            for r in ddgs.text(query, **kwargs):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
        return results
    except Exception as exc:
        logger.error("DuckDuckGo 搜索失败: %s", exc)
        raise WebSearchProviderError(f"Web search provider failed: {exc}") from exc


def _fetch_and_extract(url: str) -> dict[str, str]:
    """
    获取 URL 内容并提取正文。

    使用 trafilatura 提取 Markdown 格式正文。
    Fallback 到简单 HTTP 请求 + 基础 HTML 清洗。

    Returns:
        {"title": "...", "content": "...", "word_count": N}
    """
    import urllib.request
    import urllib.error

    # SSRF guard: block loopback / private / link-local / cloud-metadata targets
    # before fetching, mirroring the native web_fetch tool (assess_network_url).
    from backend.permissions.network import assess_network_url

    assessment = assess_network_url(url)
    if not assessment.allowed:
        return {
            "title": url,
            "content": f"获取失败: {assessment.reason or 'URL 目标被网络策略拒绝'}",
            "word_count": "0",
        }

    # 尝试 trafilatura
    try:
        import trafilatura

        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(
                downloaded,
                output_format="markdown",
                include_links=True,
                include_tables=True,
            )
            if text:
                # 提取标题
                metadata = trafilatura.extract(downloaded, output_format="xml")
                title = ""
                if metadata:
                    import re
                    title_match = re.search(r'title="([^"]*)"', metadata)
                    if title_match:
                        title = title_match.group(1)

                return {
                    "title": title or url,
                    "content": text,
                    "word_count": str(len(text)),
                }
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("trafilatura 提取失败: %s, fallback 到基础模式", exc)

    # Fallback：基础 HTTP + 简单提取
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "MiniCode/0.2.0"},
        )
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # 简单标题提取
        import re
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else url

        # 简单正文提取（移除 HTML 标签）
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return {
            "title": title,
            "content": text,
            "word_count": str(len(text)),
        }

    except Exception as exc:
        return {
            "title": url,
            "content": f"获取失败: {exc}",
            "word_count": "0",
        }


# ── MCP Tools 注册 ──────────────────────────────────────────

if HAS_MCP and mcp:

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def search(query: str) -> str:
        """
        搜索互联网。

        返回搜索结果的摘要列表（标题 + URL + 简要描述）。
        每条结果约 50 tokens，总量控制在 300 tokens 以内。

        Args:
            query: 搜索关键词
            num_results: 返回结果数量（1-10，默认 5）

        Returns:
            格式化的搜索摘要

        示例:
            search("Python MCP protocol")
            search("FastAPI WebSocket", num_results=3)
        """
        results = _search_duckduckgo(query)

        if not results:
            return "未找到相关结果。"

        # 格式化输出（Token-efficient：只返回摘要）
        lines = [f"搜索 \"{query}\" 的结果：\n"]
        for i, r in enumerate(results, 1):
            title = r["title"]
            url = r["url"]
            snippet = r["snippet"]
            lines.append(f"{i}. **{title}**")
            lines.append(f"   {url}")
            lines.append(f"   {snippet}")
            lines.append("")

        lines.append(f"共 {len(results)} 条结果。使用 fetch_page(url) 获取详细内容。")
        return truncate_tool_result("\n".join(lines))


    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def fetch_page(url: str) -> str:
        """
        获取网页正文内容。

        使用智能正文提取（trafilatura），返回 Markdown 格式。
        返回结果由共享工具输出契约统一裁剪并在需要时提升为 artifact。

        Args:
            url: 目标网页 URL（以 http:// 或 https:// 开头）

        Returns:
            页面标题 + 正文摘要

        示例:
            fetch_page("https://docs.python.org/3/library/asyncio.html")
        """
        result = _fetch_and_extract(url)

        title = result["title"]
        content = result["content"]
        word_count = result["word_count"]

        output = [
            f"## {title}",
            f"来源: {url}",
            f"字数: {word_count}",
            "",
            content,
        ]
        return truncate_tool_result("\n".join(output))


    @mcp.resource("search://recent")
    def recent_searches() -> str:
        """最近的搜索记录（功能预留）。"""
        return "暂无搜索记录。"


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    """启动 websearch MCP Server（stdio 模式）。"""
    if not HAS_MCP or not mcp:
        print(
            "错误: 需要安装 MCP SDK: pip install 'mcp[cli]'",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
