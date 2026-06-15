from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from backend.artifact.store import ArtifactStore
from backend.mcp.servers.docparse import _parse_docx, _parse_pdf, _split_sections
from backend.memory.vector_memory import VectorMemory
from backend.rag.chunker import ChunkMode, Chunker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    kind: str
    file_name: str
    media_type: str
    artifact_id: str
    doc_id: str
    indexed_chunks: int
    size_bytes: int
    title: str
    summary: str = ""
    data: str = ""
    parse_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "kind": self.kind,
            "file_name": self.file_name,
            "media_type": self.media_type,
            "artifact_id": self.artifact_id,
            "doc_id": self.doc_id,
            "indexed_chunks": self.indexed_chunks,
            "size_bytes": self.size_bytes,
            "title": self.title,
            "summary": self.summary,
        }
        if self.data:
            d["data"] = self.data
        if self.parse_error:
            d["parse_error"] = self.parse_error
        return d


@dataclass(frozen=True)
class UploadedDocument:
    file_name: str
    doc_id: str
    artifact_id: str
    indexed_chunks: int
    title: str
    full_text: str
    attachment: AttachmentRecord


CODE_MEDIA_TYPES: dict[str, str] = {
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".jsx": "text/jsx",
    ".json": "application/json",
    ".java": "text/x-java-source",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".c": "text/x-c",
    ".cc": "text/x-c++src",
    ".cpp": "text/x-c++src",
    ".h": "text/x-c",
    ".hpp": "text/x-c++hdr",
    ".sh": "text/x-shellscript",
    ".ps1": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".css": "text/css",
    ".sql": "application/sql",
}

TEXT_MEDIA_TYPES: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
}

IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def ingest_uploaded_document(
    *,
    file_name: str,
    raw_content: bytes,
    artifact_store: ArtifactStore,
) -> UploadedDocument:
    import base64 as _b64

    safe_name = Path(file_name or "upload.txt").name
    if not safe_name:
        raise ValueError("Uploaded file name is required.")
    if not raw_content:
        raise ValueError("Uploaded file is empty.")

    parsed = _parse_uploaded_content(safe_name, raw_content)
    full_text = str(parsed.get("full_text", "")).strip()
    if not full_text:
        raise ValueError("Uploaded document is empty after parsing.")
    parse_error = full_text if _is_parse_error_text(full_text) else ""
    if full_text.startswith("错误:"):
        # 优化：依赖丢失等解析错误不做强制抛出挂掉，将其转换为提示信息，允许文件作为附件正常上传
        logger.warning("Document parsing encountered an issue: %s. Proceeding as plain/raw attachment.", full_text)
    if parse_error and not full_text.startswith("閿欒:"):
        logger.warning("Document parsing encountered an issue: %s. Proceeding as plain/raw attachment.", full_text)

    native_data = ""
    if _should_keep_native_attachment(parsed):
        native_data = _b64.b64encode(raw_content).decode("ascii")

    doc_id = _build_doc_id(safe_name, raw_content)
    artifact_id = artifact_store.save(
        full_text,
        source=f"upload:{safe_name}",
        type=str(parsed.get("kind") or "document"),
    )
    indexed_chunks = 0
    if not parse_error:
        try:
            indexed_chunks = _index_document_chunks(
                doc_id=doc_id,
                file_name=safe_name,
                title=str(parsed.get("title") or Path(safe_name).stem),
                full_text=full_text,
            )
        except Exception as exc:
            logger.warning(
                "Vector indexing failed for '%s' (possibly ChromaDB is unavailable): %s. Falling back to plain ingestion without vector search.",
                safe_name,
                exc,
            )
    attachment = AttachmentRecord(
        id=f"att_{doc_id[4:]}",
        kind=str(parsed.get("kind") or "document"),
        file_name=safe_name,
        media_type=str(parsed.get("media_type") or _guess_media_type(safe_name)),
        artifact_id=artifact_id,
        doc_id=doc_id,
        indexed_chunks=indexed_chunks,
        size_bytes=len(raw_content),
        title=str(parsed.get("title") or Path(safe_name).stem),
        summary=str(parsed.get("summary") or ""),
        data=native_data,
        parse_error=parse_error,
    )

    return UploadedDocument(
        file_name=safe_name,
        doc_id=doc_id,
        artifact_id=artifact_id,
        indexed_chunks=indexed_chunks,
        title=str(parsed.get("title") or Path(safe_name).stem),
        full_text=full_text,
        attachment=attachment,
    )


def _is_parse_error_text(text: str) -> bool:
    return str(text or "").strip().lower().startswith(("error:", "错误:", "閿欒:"))


def _build_doc_id(file_name: str, raw_content: bytes) -> str:
    digest = hashlib.md5()
    digest.update(file_name.encode("utf-8", errors="ignore"))
    digest.update(raw_content)
    return f"doc_{digest.hexdigest()[:10]}"


def _parse_uploaded_content(file_name: str, raw_content: bytes, depth: int = 0) -> dict[str, Any]:
    suffix = Path(file_name).suffix.lower()
    if suffix in IMAGE_MEDIA_TYPES:
        return _parse_image_document(file_name=file_name, raw_content=raw_content)
    if suffix == ".zip":
        return _parse_zip_archive(file_name=file_name, raw_content=raw_content, depth=depth)
    if suffix == ".xlsx":
        return _parse_xlsx_workbook(file_name=file_name, raw_content=raw_content)
    if suffix == ".pptx":
        return _parse_pptx_presentation(file_name=file_name, raw_content=raw_content)
    if suffix == ".ppt":
        return _parse_legacy_presentation(file_name=file_name, raw_content=raw_content)
    if suffix == ".pdf":
        return _parse_binary_document(file_name=file_name, raw_content=raw_content)
    if suffix == ".docx":
        return _parse_binary_document(file_name=file_name, raw_content=raw_content)
    if suffix == ".doc":
        return _parse_legacy_document(file_name=file_name, raw_content=raw_content)
    return _parse_text_document(file_name=file_name, raw_content=raw_content)


def _should_keep_native_attachment(parsed: dict[str, Any]) -> bool:
    media_type = str(parsed.get("media_type") or "")
    if str(parsed.get("kind")) == "image":
        return True
    return media_type == "application/pdf"


def _parse_binary_document(file_name: str, raw_content: bytes) -> dict[str, Any]:
    suffix = Path(file_name).suffix.lower()
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw_content)

        if suffix == ".pdf":
            parsed = _parse_pdf(temp_path)
            parsed["kind"] = "document"
            parsed["media_type"] = "application/pdf"
            parsed["summary"] = "PDF document"
            return parsed
        parsed = _parse_docx(temp_path)
        parsed["kind"] = "document"
        parsed["media_type"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        parsed["summary"] = "Word document"
        return parsed
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _parse_text_document(file_name: str, raw_content: bytes) -> dict[str, Any]:
    content = _decode_text(raw_content)
    suffix = Path(file_name).suffix.lower()
    kind = "code" if suffix in CODE_MEDIA_TYPES else "document"
    return {
        "title": Path(file_name).stem,
        "full_text": content,
        "format": suffix.lstrip(".") or "txt",
        "pages": max(1, len(content.splitlines()) // 50),
        "kind": kind,
        "media_type": _guess_media_type(file_name),
        "summary": _build_text_summary(file_name, kind),
    }


def _parse_legacy_document(file_name: str, raw_content: bytes) -> dict[str, Any]:
    content = _extract_binary_strings(raw_content)
    return {
        "title": Path(file_name).stem,
        "full_text": content,
        "format": "doc",
        "pages": max(1, len(content.splitlines()) // 50),
        "kind": "document",
        "media_type": "application/msword",
        "summary": "Word document",
    }


def _parse_image_document(file_name: str, raw_content: bytes) -> dict[str, Any]:
    width, height = _extract_image_dimensions(raw_content)
    detail_lines = []
    if width and height:
        detail_lines.append(f"Dimensions: {width}x{height}")
    detail_lines.append("Image attached for native multimodal model understanding.")

    full_text = "\n\n".join(detail_lines).strip()
    return {
        "title": Path(file_name).stem,
        "full_text": full_text,
        "format": Path(file_name).suffix.lower().lstrip(".") or "image",
        "pages": 1,
        "kind": "image",
        "media_type": _guess_media_type(file_name),
        "summary": "Image attachment",
    }


def _parse_xlsx_workbook(file_name: str, raw_content: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_content)) as archive:
            shared_strings = _read_xlsx_shared_strings(archive)
            sheets = _read_xlsx_sheets(archive, shared_strings)
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded spreadsheet is not a valid .xlsx file.") from exc

    if not sheets:
        full_text = "Workbook uploaded with no readable worksheet cells."
        page_count = 1
    else:
        full_text = "\n\n".join(sheets)
        page_count = len(sheets)

    return {
        "title": Path(file_name).stem,
        "full_text": full_text,
        "format": "xlsx",
        "pages": page_count,
        "kind": "document",
        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "summary": "Excel workbook",
    }


def _parse_pptx_presentation(file_name: str, raw_content: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_content)) as archive:
            slide_names = sorted(
                name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            slides: list[str] = []
            for index, slide_name in enumerate(slide_names, start=1):
                try:
                    xml_bytes = archive.read(slide_name)
                except KeyError:
                    continue
                text = _extract_text_from_openxml_slide(xml_bytes)
                if text:
                    slides.append(f"## Slide {index}\n{text}")
            full_text = "\n\n".join(slides).strip()
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded presentation is not a valid .pptx file.") from exc

    return {
        "title": Path(file_name).stem,
        "full_text": full_text or "Presentation uploaded with no extractable slide text.",
        "format": "pptx",
        "pages": max(1, len(slide_names) if "slide_names" in locals() else 1),
        "kind": "presentation",
        "media_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "summary": "Presentation deck",
    }


def _parse_legacy_presentation(file_name: str, raw_content: bytes) -> dict[str, Any]:
    content = _extract_binary_strings(raw_content)
    return {
        "title": Path(file_name).stem,
        "full_text": content,
        "format": "ppt",
        "pages": max(1, len(content.splitlines()) // 12),
        "kind": "presentation",
        "media_type": "application/vnd.ms-powerpoint",
        "summary": "Presentation deck",
    }


def _parse_zip_archive(file_name: str, raw_content: bytes, depth: int = 0) -> dict[str, Any]:
    if depth > 1:
        return {
            "title": Path(file_name).stem,
            "full_text": f"Archive {file_name}",
            "format": "zip",
            "pages": 1,
            "kind": "archive",
            "media_type": "application/zip",
            "summary": "Archive file",
        }

    try:
        with zipfile.ZipFile(io.BytesIO(raw_content)) as archive:
            file_entries = [info for info in archive.infolist() if not info.is_dir()]
            preview_names = [info.filename for info in file_entries[:20]]
            sections = ["Archive contents:", *[f"- {name}" for name in preview_names]]
            parsed_members = 0

            for info in file_entries[:12]:
                if info.file_size > 1_000_000:
                    continue
                member_bytes = archive.read(info.filename)
                if not _supports_embedded_parse(info.filename):
                    continue
                parsed = _parse_uploaded_content(info.filename, member_bytes, depth + 1)
                member_text = str(parsed.get("full_text", "")).strip()
                if not member_text:
                    continue
                parsed_members += 1
                sections.append(f"\n## {info.filename}\n{member_text[:6000]}")

            full_text = "\n".join(sections).strip()
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded archive is not a valid zip file.") from exc

    return {
        "title": Path(file_name).stem,
        "full_text": full_text or f"Archive contents unavailable for {file_name}",
        "format": "zip",
        "pages": max(1, len(file_entries) if "file_entries" in locals() else 1),
        "kind": "archive",
        "media_type": "application/zip",
        "summary": f"Archive with {len(file_entries)} files" if "file_entries" in locals() else "Archive file",
        "parsed_members": parsed_members if "parsed_members" in locals() else 0,
    }


def _decode_text(raw_content: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw_content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_content.decode("utf-8", errors="replace")


def _extract_text_from_openxml_slide(raw_content: bytes) -> str:
    try:
        root = ElementTree.fromstring(raw_content)
    except ElementTree.ParseError:
        return ""

    texts: list[str] = []
    for node in root.iter():
        if node.tag.endswith("}t") and node.text and node.text.strip():
            texts.append(node.text.strip())
    return "\n".join(texts)


def _read_xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        xml_bytes = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []

    strings: list[str] = []
    for item in root.iter():
        if item.tag.endswith("}si"):
            parts = [node.text or "" for node in item.iter() if node.tag.endswith("}t") and node.text]
            strings.append("".join(parts).strip())
    return strings


def _read_xlsx_sheets(archive: zipfile.ZipFile, shared_strings: list[str]) -> list[str]:
    workbook_names = _read_xlsx_sheet_map(archive)
    if not workbook_names:
        workbook_names = [
            (f"Sheet {index + 1}", name)
            for index, name in enumerate(
                sorted(
                    entry.filename
                    for entry in archive.infolist()
                    if entry.filename.startswith("xl/worksheets/") and entry.filename.endswith(".xml")
                )
            )
        ]

    sheets: list[str] = []
    for sheet_name, sheet_path in workbook_names:
        try:
            xml_bytes = archive.read(sheet_path)
        except KeyError:
            continue
        rows = _extract_xlsx_rows(xml_bytes, shared_strings)
        if not rows:
            continue
        lines = [f"## Sheet: {sheet_name}"]
        for row in rows[:200]:
            row_values = [value for value in row if value]
            if row_values:
                lines.append(" | ".join(row_values[:20]))
        if len(lines) > 1:
            sheets.append("\n".join(lines))
    return sheets


def _read_xlsx_sheet_map(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    try:
        workbook_xml = archive.read("xl/workbook.xml")
        rels_xml = archive.read("xl/_rels/workbook.xml.rels")
    except KeyError:
        return []

    try:
        workbook_root = ElementTree.fromstring(workbook_xml)
        rels_root = ElementTree.fromstring(rels_xml)
    except ElementTree.ParseError:
        return []

    relationships: dict[str, str] = {}
    for rel in rels_root.iter():
        if rel.tag.endswith("}Relationship"):
            rel_id = rel.attrib.get("Id", "").strip()
            target = rel.attrib.get("Target", "").strip()
            if rel_id and target:
                relationships[rel_id] = f"xl/{target.lstrip('/')}"

    sheets: list[tuple[str, str]] = []
    for sheet in workbook_root.iter():
        if not sheet.tag.endswith("}sheet"):
            continue
        name = sheet.attrib.get("name", "Sheet").strip() or "Sheet"
        rel_id = ""
        for key, value in sheet.attrib.items():
            if key.endswith("}id"):
                rel_id = value.strip()
                break
        target = relationships.get(rel_id)
        if target:
            sheets.append((name, target))
    return sheets


def _extract_xlsx_rows(xml_bytes: bytes, shared_strings: list[str]) -> list[list[str]]:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []

    rows: list[list[str]] = []
    for row in root.iter():
        if not row.tag.endswith("}row"):
            continue
        values: list[str] = []
        for cell in row:
            if not cell.tag.endswith("}c"):
                continue
            cell_type = cell.attrib.get("t", "")
            value = ""
            if cell_type == "inlineStr":
                texts = [node.text or "" for node in cell.iter() if node.tag.endswith("}t") and node.text]
                value = "".join(texts).strip()
            else:
                raw_value = ""
                for node in cell:
                    if node.tag.endswith("}v") and node.text:
                        raw_value = node.text.strip()
                        break
                if raw_value:
                    if cell_type == "s":
                        try:
                            value = shared_strings[int(raw_value)]
                        except (ValueError, IndexError):
                            value = raw_value
                    else:
                        value = raw_value
            values.append(value)
        rows.append(values)
    return rows


def _extract_binary_strings(raw_content: bytes) -> str:
    chunks: list[str] = []
    utf16_matches = re.findall(rb"(?:[\x20-\x7e]\x00){4,}", raw_content)
    ascii_matches = re.findall(rb"[\x20-\x7e]{4,}", raw_content)

    for match in utf16_matches[:120]:
        text = match.decode("utf-16-le", errors="ignore").strip()
        if text:
            chunks.append(text)
    for match in ascii_matches[:120]:
        text = match.decode("latin-1", errors="ignore").strip()
        if text:
            chunks.append(text)

    deduped: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk in seen:
            continue
        seen.add(chunk)
        deduped.append(chunk)
        if len(deduped) >= 80:
            break

    return "\n".join(deduped).strip() or "Binary document uploaded with limited text extraction."


def _guess_media_type(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in IMAGE_MEDIA_TYPES:
        return IMAGE_MEDIA_TYPES[suffix]
    if suffix in CODE_MEDIA_TYPES:
        return CODE_MEDIA_TYPES[suffix]
    if suffix in TEXT_MEDIA_TYPES:
        return TEXT_MEDIA_TYPES[suffix]
    return "text/plain"


def _build_text_summary(file_name: str, kind: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".xlsx":
        return "Excel workbook"
    if suffix == ".md":
        return "Markdown document"
    if kind == "code":
        return f"{suffix.lstrip('.').upper() or 'Code'} source file"
    return "Text document"


def _supports_embedded_parse(file_name: str) -> bool:
    suffix = Path(file_name).suffix.lower()
    return suffix in CODE_MEDIA_TYPES or suffix in {
        ".txt",
        ".md",
        ".pdf",
        ".docx",
        ".doc",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
    }


def _extract_image_dimensions(raw_content: bytes) -> tuple[int | None, int | None]:
    if raw_content.startswith(b"\x89PNG\r\n\x1a\n") and len(raw_content) >= 24:
        width, height = struct.unpack(">II", raw_content[16:24])
        return width, height
    if raw_content.startswith((b"GIF87a", b"GIF89a")) and len(raw_content) >= 10:
        width, height = struct.unpack("<HH", raw_content[6:10])
        return width, height
    return None, None


def _index_document_chunks(
    *,
    doc_id: str,
    file_name: str,
    title: str,
    full_text: str,
) -> int:
    sections = _split_sections(full_text)
    chunker = Chunker()
    vector_memory = VectorMemory(collection_name="documents")

    indexed_chunks = 0
    for section_index, section in enumerate(sections):
        section_title = section.get("title") or f"Section {section_index + 1}"
        section_content = section.get("content", "").strip()
        if not section_content:
            continue

        source = f"{file_name}#{section_title}"
        chunks = chunker.chunk(
            section_content,
            mode=ChunkMode.GENERAL,
            source=source,
        )

        for chunk in chunks:
            vector_memory.remember(
                chunk.content,
                tags=["document", doc_id, file_name],
                importance=3,
                metadata={
                    "doc_id": doc_id,
                    "title": title,
                    "source": file_name,
                    "section_title": section_title,
                    "section_index": section_index,
                    "chunk_index": int(chunk.metadata.get("chunk_index", indexed_chunks)),
                },
            )
            indexed_chunks += 1

    return indexed_chunks
