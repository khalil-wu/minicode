"""Local document parsers used by attachment and artifact workflows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _is_parse_error(text: str) -> bool:
    return str(text or "").strip().lower().startswith(("error:", "错误:", "閿欒:"))


def _count_pdf_pages(file_path: str) -> int:
    try:
        import pymupdf

        doc = pymupdf.open(file_path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


def _parse_pdf(file_path: str) -> dict[str, Any]:
    """Parse PDF to Markdown when available, then fall back to plain text."""
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
    except Exception as exc:
        logger.warning("pymupdf4llm failed, fallback to pymupdf: %s", exc)

    try:
        import pymupdf

        doc = pymupdf.open(file_path)
        try:
            pages_text = [page.get_text() for page in doc]
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
    except Exception as exc:
        return {
            "title": Path(file_path).stem,
            "full_text": f"错误: PDF 解析失败。请安装依赖或检查文件: {exc}",
            "format": "pdf",
            "pages": 0,
        }


def _parse_docx(file_path: str) -> dict[str, Any]:
    """Parse a Word document into plain paragraph text."""
    try:
        from docx import Document

        doc = Document(file_path)
        paragraphs = [paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()]
        return {
            "title": Path(file_path).stem,
            "full_text": "\n\n".join(paragraphs),
            "format": "docx",
            "pages": max(1, len(paragraphs) // 30),
        }
    except Exception as exc:
        return {
            "title": Path(file_path).stem,
            "full_text": f"错误: 需要安装 python-docx 或 Word 解析失败: {exc}",
            "format": "docx",
            "pages": 0,
        }


__all__ = ["_parse_docx", "_parse_pdf"]
