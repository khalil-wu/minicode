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
import re
import sys
from pathlib import Path
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.tools.base import truncate_tool_result
from backend.tools.web_tools import WEB_FETCH_MAX_CHARS

logger = logging.getLogger(__name__)

# ── MCP Server 框架 ────────────────────────────────────────

try:
    from mcp.server.fastmcp import Context, FastMCP
    HAS_MCP = True
except ImportError:
    HAS_MCP = False
    Context = Any  # type: ignore[misc,assignment]

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

# Parsed documents are isolated by the trusted turn owner supplied through MCP
# request metadata. A doc_id alone is not an authorization boundary.
OwnerScope = tuple[str, str]
_parsed_docs: dict[tuple[str, str, str], dict[str, Any]] = {}


def _owner_from_context(ctx: Any) -> OwnerScope:
    try:
        meta = ctx.request_context.meta
    except (AttributeError, ValueError):
        meta = None
    extras = getattr(meta, "model_extra", None) or {}
    owner = extras.get("minicode.dev/owner")
    if not isinstance(owner, dict):
        raise PermissionError("文档工具缺少可信会话上下文")
    conversation_id = str(owner.get("conversation_id") or "").strip()
    workspace_root = str(owner.get("workspace_root") or "").strip()
    if not conversation_id:
        raise PermissionError("文档工具缺少会话所有者")
    return conversation_id, workspace_root


def _doc_key(owner: OwnerScope, doc_id: str) -> tuple[str, str, str]:
    return owner[0], owner[1], doc_id


def _gen_doc_id(source: str) -> str:
    """为文档生成唯一 ID。"""
    return "doc_" + hashlib.md5(source.encode()).hexdigest()[:8]


def _is_parse_error(text: str) -> bool:
    return str(text or "").strip().lower().startswith(("error:", "错误:", "閿欒:"))


def _doc_from_attachment_ref(ref: str, owner: OwnerScope) -> dict[str, Any] | None:
    resolved = AttachmentStore().resolve_content(
        ref,
        conversation_id=owner[0],
        workspace_root=owner[1],
    )
    if resolved is None:
        return None

    artifact_id, content, metadata = resolved
    attachment = metadata.get("attachment")
    if not isinstance(attachment, dict):
        attachment = {}

    file_name = str(attachment.get("file_name") or artifact_id)
    media_type = str(attachment.get("media_type") or "")
    fmt = "pdf" if media_type == "application/pdf" else Path(file_name).suffix.lstrip(".")
    try:
        pages = int(attachment.get("pages") or attachment.get("page_count") or 0)
    except (TypeError, ValueError):
        pages = 0
    return {
        "title": str(attachment.get("title") or Path(file_name).stem),
        "full_text": content,
        "format": fmt or str(attachment.get("kind") or "document"),
        "pages": pages,
        "source": str(attachment.get("doc_id") or artifact_id),
        "artifact_id": artifact_id,
        "attachment": attachment,
    }


def _doc_from_artifact_ref(ref: str, owner: OwnerScope) -> dict[str, Any] | None:
    store = ArtifactStore()
    content = store.get(ref, conversation_id=owner[0], workspace_root=owner[1])
    if content is None:
        return None

    meta = store.get_meta(ref)
    source = str(getattr(meta, "source", "") or ref)
    return {
        "title": Path(source).stem or ref,
        "full_text": content,
        "format": str(getattr(meta, "type", "") or "text"),
        "pages": max(1, len(content.splitlines()) // 50),
        "source": ref,
        "artifact_id": ref,
    }


def _store_parsed_doc(owner: OwnerScope, source: str, parsed: dict[str, Any]) -> str:
    attachment = parsed.get("attachment")
    doc_id = ""
    if isinstance(attachment, dict):
        doc_id = str(attachment.get("doc_id") or "").strip()
    doc_id = doc_id or _gen_doc_id(source)
    _parsed_docs[_doc_key(owner, doc_id)] = {
        "title": parsed["title"],
        "sections": _split_sections(str(parsed["full_text"])),
        "full_text": parsed["full_text"],
        "format": parsed["format"],
        "pages": parsed["pages"],
        "source": parsed.get("source", source),
        "artifact_id": parsed.get("artifact_id"),
    }
    return doc_id


def _get_or_load_doc(owner: OwnerScope, doc_id: str) -> dict[str, Any] | None:
    doc = _parsed_docs.get(_doc_key(owner, doc_id))
    if doc is not None:
        return doc
    parsed = _doc_from_attachment_ref(doc_id, owner) or _doc_from_artifact_ref(doc_id, owner)
    if parsed is None:
        return None
    loaded_doc_id = _store_parsed_doc(owner, doc_id, parsed)
    return _parsed_docs.get(_doc_key(owner, loaded_doc_id))


def _resolve_owner_path(source: str, owner: OwnerScope) -> Path:
    workspace_root = owner[1]
    if not workspace_root:
        raise PermissionError("当前会话没有可读取本地文件的工作区")
    root = Path(workspace_root).expanduser().resolve()
    candidate = Path(source).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"文件超出当前工作区: {source}") from exc
    return resolved


# ── 解析器 ──────────────────────────────────────────────────

def _parse_pdf(file_path: str) -> dict[str, Any]:
    """
    解析 PDF 文件。

    优先使用 pymupdf4llm（输出 Markdown），fallback 到 pymupdf。
    """
    try:
        import pymupdf4llm
        md_text = pymupdf4llm.to_markdown(file_path)
        if _is_parse_error(md_text):
            raise RuntimeError(md_text)
        return {
            "title": Path(file_path).stem,
            "full_text": md_text,
            "format": "pdf",
            "pages": _count_pdf_pages(file_path),
        }
    except (ImportError, Exception) as exc:
        logger.warning("pymupdf4llm failed, fallback to pymupdf: %s", exc)

    # Fallback: pymupdf
    try:
        import pymupdf
        doc = pymupdf.open(file_path)
        try:
            pages_text = []
            for page in doc:
                pages_text.append(page.get_text())
            full_text = "\n\n".join(pages_text)
            if not full_text.strip():
                raise RuntimeError("PDF contains no extractable text")
            return {
                "title": Path(file_path).stem,
                "full_text": full_text,
                "format": "pdf",
                "pages": len(pages_text),
            }
        finally:
            doc.close()
    except (ImportError, Exception) as exc:
        return {
            "title": Path(file_path).stem,
            "full_text": f"错误: PDF 解析失败。请安装依赖或检查文件: {exc}",
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
    except (ImportError, Exception) as exc:
        return {
            "title": Path(file_path).stem,
            "full_text": f"错误: 需要安装 python-docx 或 Word 解析失败: {exc}",
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

    # SSRF guard: block loopback / private / link-local / cloud-metadata targets
    # before any fetch. Mirrors the native web_fetch tool, which routes through
    # the same assess_network_url. Without this, parse("http://169.254.169.254/…")
    # reached internal endpoints while web_fetch refused the identical URL.
    from backend.permissions.network import assess_network_url

    def require_allowed(target: str) -> None:
        assessment = assess_network_url(target)
        if not assessment.allowed:
            raise PermissionError(
                f"{assessment.reason or 'URL 目标被网络策略拒绝'}: {target}"
            )

    class PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            require_allowed(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    require_allowed(url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MiniCode/0.2.0"})
        opener = urllib.request.build_opener(PolicyRedirectHandler())
        with opener.open(req, timeout=15) as resp:
            require_allowed(resp.geturl())
            html = resp.read(WEB_FETCH_MAX_CHARS + 1)
        if len(html) > WEB_FETCH_MAX_CHARS:
            raise ValueError("远程文档超过共享 web_fetch 资源边界")
        decoded = html.decode("utf-8", errors="replace")
        try:
            import trafilatura

            text = trafilatura.extract(
                decoded,
                output_format="markdown",
                include_links=True,
                include_tables=True,
            )
            metadata = trafilatura.bare_extraction(decoded)
            title = metadata.get("title", url) if metadata else url
            if text:
                return {
                    "title": title,
                    "full_text": text,
                    "format": "html",
                    "pages": 1,
                }
        except ImportError:
            pass

        # Dependency-free fallback for minimal installs.
        title_m = re.search(r"<title>(.*?)</title>", decoded, re.I | re.S)
        title = title_m.group(1).strip() if title_m else url
        text = re.sub(r"<script[^>]*>.*?</script>", "", decoded, flags=re.S | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"title": title, "full_text": text, "format": "html", "pages": 1}
    except PermissionError:
        raise
    except Exception as exc:
        return {"title": url, "full_text": f"获取失败: {exc}", "format": "html", "pages": 0}


def _count_pdf_pages(file_path: str) -> int:
    """统计 PDF 页数。"""
    try:
        import pymupdf
        doc = pymupdf.open(file_path)
        try:
            return len(doc)
        finally:
            doc.close()
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
    def parse(source: str, ctx: Context) -> str:
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
        owner = _owner_from_context(ctx)
        # 判断来源类型
        if source.startswith(("http://", "https://")):
            parsed = _parse_url(source)
        elif (
            loaded := _doc_from_attachment_ref(source, owner)
            or _doc_from_artifact_ref(source, owner)
        ) is not None:
            parsed = loaded
        else:
            try:
                resolved_source = _resolve_owner_path(source, owner)
            except PermissionError as exc:
                return f"错误: {exc}"
            if not resolved_source.exists() or not resolved_source.is_file():
                return f"错误: 文件不存在: {source}"
            ext = resolved_source.suffix.lower()
            if ext == ".pdf":
                parsed = _parse_pdf(str(resolved_source))
            elif ext in (".docx", ".doc"):
                parsed = _parse_docx(str(resolved_source))
            else:
                parsed = _parse_text(str(resolved_source))

        # 分割章节
        doc_id = _store_parsed_doc(owner, source, parsed)
        sections = _parsed_docs[_doc_key(owner, doc_id)]["sections"]

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
            output.append(f"  {i}. **{sec['title']}**")

        output.append("")
        output.append("使用 `get_section(doc_id, section_index)` 读取具体章节。")

        return "\n".join(output)


    @mcp.tool()
    def get_section(doc_id: str, section_index: int, ctx: Context) -> str:
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
        owner = _owner_from_context(ctx)
        doc = _get_or_load_doc(owner, doc_id)
        if not doc:
            return f"错误: 文档 '{doc_id}' 不存在。请先使用 parse() 解析文档。"

        sections = doc["sections"]
        if section_index < 0 or section_index >= len(sections):
            return (
                f"错误: 章节索引 {section_index} 超出范围。"
                f"有效范围: 0-{len(sections) - 1}"
            )

        section = sections[section_index]

        content = section["content"]
        output = [
            f"## {section['title']}",
            f"（章节 {section_index}/{len(sections) - 1}，来自 {doc['title']}）",
            "",
            content,
        ]
        return truncate_tool_result("\n".join(output))


    @mcp.tool()
    def get_full_text(doc_id: str, ctx: Context) -> str:
        """
        获取文档完整文本。

        返回结果遵循共享 Pi 工具输出契约；完整内容仍保存在文档句柄中。

        Args:
            doc_id: 文档 ID

        Returns:
            文档全文（可能截断）
        """
        owner = _owner_from_context(ctx)
        doc = _get_or_load_doc(owner, doc_id)
        if not doc:
            return f"错误: 文档 '{doc_id}' 不存在。"

        return truncate_tool_result(str(doc["full_text"]))

# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    """启动 docparse MCP Server。"""
    if not HAS_MCP or not mcp:
        print("错误: 需要安装 MCP SDK: pip install 'mcp[cli]'", file=sys.stderr)
        sys.exit(1)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
