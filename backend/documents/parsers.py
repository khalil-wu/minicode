"""Local document parsers used by attachment and artifact workflows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


MAX_PDF_PAGES = 100


class PDFPageLimitError(ValueError):
    """The upload exceeds the supported PDF page budget."""


def _parse_pdf(file_path: str) -> dict[str, Any]:
    """Count the PDF page tree before extracting any page content."""
    import pymupdf

    with pymupdf.open(file_path) as doc:
        if doc.needs_pass:
            raise ValueError("The PDF is encrypted and requires a password.")
        pages = len(doc)
        if pages > MAX_PDF_PAGES:
            raise PDFPageLimitError(f"PDF has {pages} pages; the limit is {MAX_PDF_PAGES}.")
        try:
            import pymupdf4llm

            full_text = pymupdf4llm.to_markdown(doc)
        except (ImportError, RuntimeError, ValueError) as exc:
            logger.warning("PDF Markdown extraction unavailable; reading page text: %s", exc)
            full_text = "\n\n".join(page.get_text() for page in doc)
        return {
            "title": Path(file_path).stem,
            "full_text": full_text,
            "format": "pdf",
            "pages": pages,
        }


def _parse_docx(file_path: str) -> dict[str, Any]:
    """Read paragraphs and tables in document order, including nested tables."""
    from docx import Document
    from docx.table import Table

    def block_text(parent: Any) -> list[str]:
        blocks = []
        for block in parent.iter_inner_content():
            if isinstance(block, Table):
                rows = [" | ".join(" / ".join(block_text(cell)) for cell in row.cells) for row in block.rows]
                blocks.append("\n".join(rows))
            elif block.text.strip():
                blocks.append(block.text)
        return blocks

    doc = Document(file_path)
    return {
        "title": Path(file_path).stem,
        "full_text": "\n\n".join(block_text(doc)),
        "format": "docx",
        # Word pagination depends on layout, fonts, and the rendering engine.
        "pages": 0,
    }


__all__ = ["_parse_docx", "_parse_pdf"]
