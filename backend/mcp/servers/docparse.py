"""
docparse MCP Server（DESIGN.md §六.2）。

传输：stdio
依赖：pymupdf4llm（PDF→Markdown）、python-docx（Word→文本）、trafilatura（URL→文本）

功能概述：
  多格式文档解析器。Agent 上传文件或提供 URL 后，此 Server 将内容
  转为结构化 Markdown，支持按章节按需读取（渐进式披露）。

Tools:
  parse(source) → 文档概览
    source: 本地文件路径或 URL
    返回 文档标题 + 结构概览 + 页数/字数 + doc_id
    完整内容写入临时文件，按需读取

  get_section(doc_id, section_index) → 章节内容
    按章节索引读取具体内容（避免一次性灌入全文）

  get_full_text(doc_id) → 完整文本
    返回完整的文档内容（大文档会截断）

Token-efficient 设计：
  - parse() 只返回大纲级别的概览
  - get_section() 按需加载具体章节
  - 渐进式披露：概览 → 章节 → 全文

运行方式：python -m backend.mcp.servers.docparse
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── MCP Server 框架 ────────────────────────────────────────

try:
    from mcp.server.fastmcp import FastMCP
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

if HAS_MCP:
    mcp = FastMCP(
        "docparse",
        instructions=(
            "文档解析 MCP Server。支持 PDF、Word、HTML 等格式。"
            "返回结构化概览，按需提供详细章节内容。"
        ),
    )
else:
    mcp = None


# ── 文档存储 ────────────────────────────────────────────────

# 已解析文档缓存：{doc_id: {"title": str, "sections": [...], "full_text": str}}
_parsed_docs: dict[str, dict[str, Any]] = {}


def _gen_doc_id(source: str) -> str:
    """为文档生成唯一 ID。"""
    return "doc_" + hashlib.md5(source.encode()).hexdigest()[:8]


# ── 解析器 ──────────────────────────────────────────────────

def _parse_pdf(file_path: str) -> dict[str, Any]:
    """
    解析 PDF 文件。

    优先使用 pymupdf4llm（输出 Markdown），fallback 到 pymupdf。
    """
    try:
        import pymupdf4llm
        md_text = pymupdf4llm.to_markdown(file_path)
        return {
            "title": Path(file_path).stem,
            "full_text": md_text,
            "format": "pdf",
            "pages": _count_pdf_pages(file_path),
        }
    except ImportError:
        pass

    # Fallback: pymupdf
    try:
        import pymupdf
        doc = pymupdf.open(file_path)
        pages_text = []
        for page in doc:
            pages_text.append(page.get_text())
        doc.close()
        return {
            "title": Path(file_path).stem,
            "full_text": "\n\n".join(pages_text),
            "format": "pdf",
            "pages": len(pages_text),
        }
    except ImportError:
        return {
            "title": Path(file_path).stem,
            "full_text": "错误: 需要安装 pymupdf4llm 或 pymupdf: pip install pymupdf4llm",
            "format": "pdf",
            "pages": 0,
        }


def _parse_docx(file_path: str) -> dict[str, Any]:
    """解析 Word 文件。"""
    try:
        from docx import Document
        doc = Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return {
            "title": Path(file_path).stem,
            "full_text": "\n\n".join(paragraphs),
            "format": "docx",
            "pages": max(1, len(paragraphs) // 30),
        }
    except ImportError:
        return {
            "title": Path(file_path).stem,
            "full_text": "错误: 需要安装 python-docx: pip install python-docx",
            "format": "docx",
            "pages": 0,
        }


def _parse_text(file_path: str) -> dict[str, Any]:
    """解析纯文本/Markdown 文件。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = Path(file_path).read_text(encoding="latin-1")

    return {
        "title": Path(file_path).stem,
        "full_text": content,
        "format": Path(file_path).suffix.lstrip("."),
        "pages": max(1, len(content.split("\n")) // 50),
    }


def _parse_url(url: str) -> dict[str, Any]:
    """解析 URL 内容。"""
    import urllib.request

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
            metadata = trafilatura.bare_extraction(downloaded)
            title = metadata.get("title", url) if metadata else url
            return {
                "title": title,
                "full_text": text or "无法提取正文",
                "format": "html",
                "pages": 1,
            }
    except ImportError:
        pass

    # Fallback
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MiniCode/0.2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # 简单提取
        title_m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        title = title_m.group(1).strip() if title_m else url
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"title": title, "full_text": text[:10000], "format": "html", "pages": 1}
    except Exception as exc:
        return {"title": url, "full_text": f"获取失败: {exc}", "format": "html", "pages": 0}


def _count_pdf_pages(file_path: str) -> int:
    """统计 PDF 页数。"""
    try:
        import pymupdf
        doc = pymupdf.open(file_path)
        count = len(doc)
        doc.close()
        return count
    except Exception:
        return 0


def _split_sections(text: str) -> list[dict[str, str]]:
    """
    将文本按标题分割为章节。

    识别 Markdown 标题（# ## ### 等）作为分隔符。
    """
    sections: list[dict[str, str]] = []
    current_title = "引言"
    current_lines: list[str] = []

    for line in text.split("\n"):
        heading_match = re.match(r"^(#{1,4})\s+(.+)", line)
        if heading_match:
            # 保存前一个章节
            if current_lines:
                sections.append({
                    "title": current_title,
                    "content": "\n".join(current_lines).strip(),
                })
            current_title = heading_match.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)

    # 保存最后一个章节
    if current_lines:
        sections.append({
            "title": current_title,
            "content": "\n".join(current_lines).strip(),
        })

    # 如果没有检测到章节，将全文作为单一章节
    if not sections:
        sections.append({"title": "全文", "content": text.strip()})

    return sections


# ── MCP Tools ──────────────────────────────────────────────

if HAS_MCP and mcp:

    @mcp.tool()
    def parse(source: str) -> str:
        """
        解析文档，返回结构化概览。

        支持的格式：PDF、Word (.docx)、Markdown (.md)、纯文本 (.txt)、URL。
        返回文档标题、结构概览和 doc_id，用于后续按需读取。

        Args:
            source: 文件路径（如 /path/to/doc.pdf）或 URL（https://...）

        Returns:
            文档概览（标题 + 章节大纲 + doc_id）

        示例:
            parse("/path/to/paper.pdf")
            parse("https://docs.python.org/3/tutorial/")
        """
        # 判断来源类型
        if source.startswith(("http://", "https://")):
            parsed = _parse_url(source)
        elif not Path(source).exists():
            return f"错误: 文件不存在: {source}"
        else:
            ext = Path(source).suffix.lower()
            if ext == ".pdf":
                parsed = _parse_pdf(source)
            elif ext in (".docx", ".doc"):
                parsed = _parse_docx(source)
            else:
                parsed = _parse_text(source)

        # 分割章节
        sections = _split_sections(parsed["full_text"])

        # 生成 doc_id 并缓存
        doc_id = _gen_doc_id(source)
        _parsed_docs[doc_id] = {
            "title": parsed["title"],
            "sections": sections,
            "full_text": parsed["full_text"],
            "format": parsed["format"],
            "pages": parsed["pages"],
            "source": source,
        }

        # 构建概览输出（Token-efficient）
        word_count = len(parsed["full_text"])
        output = [
            f"## 文档概览",
            f"- **标题**: {parsed['title']}",
            f"- **格式**: {parsed['format']}",
            f"- **页数**: {parsed['pages']}",
            f"- **字数**: {word_count}",
            f"- **doc_id**: `{doc_id}`",
            f"",
            f"### 章节结构 ({len(sections)} 个章节)",
        ]

        for i, sec in enumerate(sections):
            preview = sec["content"][:60].replace("\n", " ")
            output.append(f"  {i}. **{sec['title']}** — {preview}...")

        output.append("")
        output.append("使用 `get_section(doc_id, section_index)` 读取具体章节。")

        return "\n".join(output)


    @mcp.tool()
    def get_section(doc_id: str, section_index: int) -> str:
        """
        按章节索引读取文档内容。

        Args:
            doc_id: 由 parse() 返回的文档 ID
            section_index: 章节索引（从 0 开始）

        Returns:
            指定章节的完整内容

        示例:
            get_section("doc_a1b2c3d4", 0)   # 读取第一个章节
            get_section("doc_a1b2c3d4", 2)   # 读取第三个章节
        """
        doc = _parsed_docs.get(doc_id)
        if not doc:
            return f"错误: 文档 '{doc_id}' 不存在。请先使用 parse() 解析文档。"

        sections = doc["sections"]
        if section_index < 0 or section_index >= len(sections):
            return (
                f"错误: 章节索引 {section_index} 超出范围。"
                f"有效范围: 0-{len(sections) - 1}"
            )

        section = sections[section_index]

        # 限制单章节返回长度
        content = section["content"]
        max_chars = 4000
        truncated = len(content) > max_chars

        output = [
            f"## {section['title']}",
            f"（章节 {section_index}/{len(sections) - 1}，来自 {doc['title']}）",
            "",
            content[:max_chars],
        ]

        if truncated:
            output.append(f"\n... (已截取前 {max_chars} 字符)")

        return "\n".join(output)


    @mcp.tool()
    def get_full_text(doc_id: str) -> str:
        """
        获取文档完整文本。

        警告：大文档会被截断到 8000 字符。用于需要全文搜索的场景。

        Args:
            doc_id: 文档 ID

        Returns:
            文档全文（可能截断）
        """
        doc = _parsed_docs.get(doc_id)
        if not doc:
            return f"错误: 文档 '{doc_id}' 不存在。"

        text = doc["full_text"]
        max_chars = 8000

        if len(text) <= max_chars:
            return text

        return text[:max_chars] + f"\n\n... (已截取前 {max_chars}/{len(text)} 字符)"


    @mcp.resource("doc://list")
    def list_docs() -> str:
        """列出已解析的文档。"""
        if not _parsed_docs:
            return "暂无已解析的文档。"

        lines = ["已解析的文档：\n"]
        for doc_id, doc in _parsed_docs.items():
            lines.append(
                f"- `{doc_id}`: {doc['title']} "
                f"({doc['format']}, {len(doc['sections'])} 章节)"
            )
        return "\n".join(lines)


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    """启动 docparse MCP Server。"""
    if not HAS_MCP or not mcp:
        print("错误: 需要安装 MCP SDK: pip install 'mcp[cli]'", file=sys.stderr)
        sys.exit(1)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
