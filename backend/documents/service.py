from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import struct
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from defusedxml import ElementTree

from backend.artifact.store import ArtifactStore
from backend.documents.parsers import _parse_docx, _parse_pdf

logger = logging.getLogger(__name__)

MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 10 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200


class AttachmentSecurityError(ValueError):
    """Raised when an archive crosses a resource/safety boundary."""


def _validate_zip_archive(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        raise AttachmentSecurityError(f"Archive contains too many entries (max {MAX_ZIP_ENTRIES}).")
    total = 0
    for info in entries:
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise AttachmentSecurityError("Archive uncompressed content exceeds the 100 MB limit.")
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise AttachmentSecurityError(f"Archive member '{info.filename}' exceeds the 10 MB limit.")
        if info.file_size and info.compress_size == 0:
            raise AttachmentSecurityError(f"Archive member '{info.filename}' has an invalid compression size.")
        if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO:
            raise AttachmentSecurityError(f"Archive member '{info.filename}' exceeds the compression-ratio limit.")


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    kind: str
    file_name: str
    media_type: str
    artifact_id: str
    doc_id: str
    size_bytes: int
    title: str
    page_count: int = 0
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
            "size_bytes": self.size_bytes,
            "title": self.title,
            "page_count": self.page_count,
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

# Extensions whose payload is expected to be human-readable text.  The
# extension is only a hint: uploads are still checked for binary bytes before
# they are decoded.  This keeps a file accidentally renamed to ``.txt`` from
# becoming a page of latin-1 garbage in the model context.
PLAIN_TEXT_SUFFIXES = {".txt", ".md"}

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
    conversation_id: str = "",
    workspace_root: str | Path | None = None,
) -> UploadedDocument:
    import base64 as _b64
    from backend.security.sensitive_files import is_protected_write_path

    submitted_path = Path(file_name or "upload.txt")
    if is_protected_write_path(submitted_path):
        raise ValueError("Sensitive files cannot be uploaded as attachments.")
    safe_name = Path(file_name or "upload.txt").name
    if not safe_name:
        raise ValueError("Uploaded file name is required.")
    if is_protected_write_path(Path(safe_name)):
        raise ValueError("Sensitive files cannot be uploaded as attachments.")
    if not raw_content:
        raise ValueError("Uploaded file is empty.")

    parsed = parse_document_preview(safe_name, raw_content)
    full_text = str(parsed.get("full_text", "")).strip()
    parse_error = str(parsed.get("parse_error") or "").strip()
    if parse_error:
        logger.warning(
            "Document parsing encountered an issue for %s: %s. Proceeding with a safe attachment preview.",
            safe_name,
            parse_error,
        )

    native_data = ""
    if _should_keep_native_attachment(parsed):
        native_data = _b64.b64encode(raw_content).decode("ascii")

    doc_id = _build_doc_id(safe_name, raw_content)
    artifact_id = artifact_store.save(
        full_text,
        source=f"upload:{safe_name}",
        type=str(parsed.get("kind") or "document"),
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    )
    attachment = AttachmentRecord(
        id=f"att_{doc_id[4:]}",
        kind=str(parsed.get("kind") or "document"),
        file_name=safe_name,
        media_type=str(parsed.get("media_type") or _guess_media_type(safe_name)),
        artifact_id=artifact_id,
        doc_id=doc_id,
        size_bytes=len(raw_content),
        title=str(parsed.get("title") or Path(safe_name).stem),
        page_count=max(0, int(parsed.get("pages") or 0)),
        summary=str(parsed.get("summary") or ""),
        data=native_data,
        parse_error=parse_error,
    )

    return UploadedDocument(
        file_name=safe_name,
        doc_id=doc_id,
        artifact_id=artifact_id,
        title=str(parsed.get("title") or Path(safe_name).stem),
        full_text=full_text,
        attachment=attachment,
    )


def parse_document_preview(file_name: str, raw_content: bytes) -> dict[str, Any]:
    """Parse an attachment without persisting it and always return safe display text.

    Parser/dependency failures are diagnostics, not upload failures. Archive
    resource-limit violations intentionally remain hard failures.
    """
    parsed = dict(_parse_uploaded_content(file_name, raw_content))
    full_text = str(parsed.get("full_text") or "").strip()
    parse_error = str(parsed.get("parse_error") or "").strip()

    # Some legacy parser adapters return a localized ``错误:`` line as their
    # content. Never project that diagnostic into model context or a copyable
    # file body.
    if _is_parse_error_text(full_text):
        parse_error = parse_error or full_text
        full_text = ""

    if not full_text:
        media_type = str(parsed.get("media_type") or _guess_media_type(file_name))
        kind = str(parsed.get("kind") or "document")
        full_text = _unavailable_text_fallback(file_name, media_type, kind)
        parse_error = parse_error or "No extractable text was found; the original file remains available."

    parsed["full_text"] = full_text
    if parse_error:
        parsed["parse_error"] = parse_error
    return parsed


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
        # cc sniffs the %PDF- magic before treating a file as a PDF (pdf.ts);
        # a renamed non-PDF must not poison the parse path with a 400 loop.
        if not raw_content.lstrip()[:5].startswith(b"%PDF"):
            return _parse_binary_unknown_document(
                file_name=file_name, raw_content=raw_content
            )
        # cc caps PDF ingestion (apiLimits: 10 inline / 20 per read / 100 API);
        # a bound here keeps 500-page uploads from flooding context.
        pages = _count_pdf_pages_bytes(raw_content)
        if pages > 100:
            raise ValueError(
                f"PDF has {pages} pages; the limit is 100 (cc apiLimits contract)"
            )
        return _parse_binary_document(file_name=file_name, raw_content=raw_content)
    if suffix == ".docx":
        return _parse_binary_document(file_name=file_name, raw_content=raw_content)
    if suffix == ".doc":
        return _parse_legacy_document(file_name=file_name, raw_content=raw_content)
    if suffix in PLAIN_TEXT_SUFFIXES or suffix in CODE_MEDIA_TYPES:
        if _looks_like_text(raw_content):
            return _parse_text_document(file_name=file_name, raw_content=raw_content)
        return _parse_binary_unknown_document(file_name=file_name, raw_content=raw_content)
    # A missing/unknown extension is not proof that the body is text.  Decode
    # only payloads which pass the same conservative text probe used for known
    # text extensions; otherwise retain safe metadata and an explicit warning.
    if _looks_like_text(raw_content):
        return _parse_text_document(file_name=file_name, raw_content=raw_content)
    return _parse_binary_unknown_document(file_name=file_name, raw_content=raw_content)


def _should_keep_native_attachment(parsed: dict[str, Any]) -> bool:
    media_type = str(parsed.get("media_type") or "")
    if str(parsed.get("kind")) == "image":
        return True
    return media_type == "application/pdf"


def _cleanup_temp_file(path: str, attempts: int = 8) -> None:
    """Remove parser temp files, tolerating Windows scanners/parser handles briefly held open."""
    for attempt in range(max(1, attempts)):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt + 1 >= max(1, attempts):
                logger.warning("Could not remove parser temp file after retries: %s", path)
                return
            time.sleep(0.05)


def _parse_binary_document(file_name: str, raw_content: bytes) -> dict[str, Any]:
    suffix = Path(file_name).suffix.lower()
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw_content)

        try:
            parsed = _parse_pdf(temp_path) if suffix == ".pdf" else _parse_docx(temp_path)
        except Exception as exc:
            logger.info("Could not extract text from %s: %s", file_name, exc)
            return _parser_failure_document(file_name, exc)
        parsed["kind"] = "document"
        parsed["media_type"] = (
            "application/pdf"
            if suffix == ".pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        parsed["summary"] = "PDF document" if suffix == ".pdf" else "Word document"
        return parsed
    finally:
        _cleanup_temp_file(temp_path)


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


def _parse_binary_unknown_document(file_name: str, raw_content: bytes) -> dict[str, Any]:
    """Represent an unsupported binary without leaking decoded garbage."""
    media_type = _guess_media_type(file_name)
    size = len(raw_content)
    metadata = (
        f"Binary attachment: {Path(file_name).name}\n"
        f"Size: {size:,} bytes\n"
        f"Media type: {media_type}\n"
        "No extractable text is available for this file."
    )
    return {
        "title": Path(file_name).stem or Path(file_name).name,
        "full_text": metadata,
        "format": Path(file_name).suffix.lower().lstrip(".") or "binary",
        "pages": 1,
        "kind": "binary",
        "media_type": media_type,
        "summary": "Binary attachment",
        "parse_error": "This binary format does not expose extractable text; file metadata is shown instead.",
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
            _validate_zip_archive(archive)
            shared_strings = _read_xlsx_shared_strings(archive)
            sheets = _read_xlsx_sheets(archive, shared_strings)
    except AttachmentSecurityError:
        raise
    except Exception as exc:
        logger.info("Could not extract workbook cells from %s: %s", file_name, exc)
        return _parser_failure_document(file_name, exc)

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
        **(
            {"parse_error": "The workbook contains no readable worksheet cells; the file is still attached."}
            if not sheets
            else {}
        ),
    }


def _parse_pptx_presentation(file_name: str, raw_content: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_content)) as archive:
            _validate_zip_archive(archive)
            slide_names = sorted(
                name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            slides: list[str] = []
            failed_slides: list[str] = []
            for index, slide_name in enumerate(slide_names, start=1):
                try:
                    xml_bytes = archive.read(slide_name)
                except KeyError:
                    continue
                text = _extract_text_from_openxml_slide(xml_bytes)
                if text is None:
                    failed_slides.append(f"slide {index}")
                elif text:
                    slides.append(f"## Slide {index}\n{text}")
            full_text = "\n\n".join(slides).strip()
    except AttachmentSecurityError:
        raise
    except Exception as exc:
        logger.info("Could not extract presentation text from %s: %s", file_name, exc)
        return _parser_failure_document(file_name, exc)

    return {
        "title": Path(file_name).stem,
        "full_text": full_text or "Presentation uploaded with no extractable slide text.",
        "format": "pptx",
        "pages": max(1, len(slide_names) if "slide_names" in locals() else 1),
        "kind": "presentation",
        "media_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "summary": "Presentation deck",
        **(
            {"parse_error": f"Could not parse text from {', '.join(failed_slides)}; other slides remain available."}
            if failed_slides
            else {}
        ),
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
            _validate_zip_archive(archive)
            file_entries = [info for info in archive.infolist() if not info.is_dir()]
            preview_names = [info.filename for info in file_entries[:20]]
            sections = ["Archive contents:", *[f"- {name}" for name in preview_names]]
            parsed_members = 0

            parse_warnings: list[str] = []
            for info in file_entries[:12]:
                if info.file_size > 1_000_000:
                    continue
                member_bytes = archive.read(info.filename)
                if not _supports_embedded_parse(info.filename):
                    continue
                try:
                    parsed = _parse_uploaded_content(info.filename, member_bytes, depth + 1)
                except AttachmentSecurityError:
                    raise
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    parse_warnings.append(f"{info.filename}: {exc}")
                    continue
                member_error = str(parsed.get("parse_error") or "").strip()
                if member_error:
                    parse_warnings.append(f"{info.filename}: {member_error}")
                member_text = str(parsed.get("full_text", "")).strip()
                if not member_text or _is_parse_error_text(member_text):
                    continue
                parsed_members += 1
                sections.append(f"\n## {info.filename}\n{member_text[:6000]}")

            full_text = "\n".join(sections).strip()
    except AttachmentSecurityError:
        raise
    except Exception as exc:
        logger.info("Could not inspect archive %s: %s", file_name, exc)
        return _parser_failure_document(file_name, exc)

    return {
        "title": Path(file_name).stem,
        "full_text": full_text or f"Archive contents unavailable for {file_name}",
        "format": "zip",
        "pages": max(1, len(file_entries) if "file_entries" in locals() else 1),
        "kind": "archive",
        "media_type": "application/zip",
        "summary": f"Archive with {len(file_entries)} files" if "file_entries" in locals() else "Archive file",
        "parsed_members": parsed_members if "parsed_members" in locals() else 0,
        **(
            {"parse_error": f"Some archive members could not be fully parsed: {'; '.join(parse_warnings[:8])}"}
            if "parse_warnings" in locals() and parse_warnings
            else {}
        ),
    }


def _decode_text(raw_content: bytes) -> str:
    if raw_content.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = raw_content.decode("utf-16")
        if _is_sane_text(decoded):
            return decoded
    if b"\x00" in raw_content:
        even_nuls = sum(1 for value in raw_content[::2] if value == 0)
        odd_nuls = sum(1 for value in raw_content[1::2] if value == 0)
        if max(even_nuls, odd_nuls) >= max(2, len(raw_content) // 8):
            encoding = "utf-16-be" if even_nuls > odd_nuls else "utf-16-le"
            decoded = raw_content.decode(encoding)
            if _is_sane_text(decoded):
                return decoded
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            decoded = raw_content.decode(encoding)
            if _is_sane_text(decoded):
                return decoded
        except UnicodeDecodeError:
            continue
    # latin-1 is deliberately a last-resort codec only for payloads which are
    # overwhelmingly printable.  It must never be used to make arbitrary
    # binary bytes look like document text.
    decoded = raw_content.decode("latin-1")
    if _is_sane_text(decoded):
        return decoded
    raise ValueError("Binary attachment does not contain safely decodable text.")


def _looks_like_text(raw_content: bytes) -> bool:
    if not raw_content:
        return False
    # UTF-16 files commonly contain NUL bytes; accept them only with a BOM or
    # a strong alternating-byte signature.
    if raw_content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return _is_sane_text(raw_content.decode("utf-16"))
        except UnicodeDecodeError:
            return False
    if b"\x00" in raw_content:
        even_nuls = sum(1 for value in raw_content[::2] if value == 0)
        odd_nuls = sum(1 for value in raw_content[1::2] if value == 0)
        if max(even_nuls, odd_nuls) < max(2, len(raw_content) // 8):
            return False
        try:
            encoding = "utf-16-be" if even_nuls > odd_nuls else "utf-16-le"
            return _is_sane_text(raw_content.decode(encoding))
        except UnicodeDecodeError:
            return False
    try:
        _decode_text(raw_content)
        return True
    except (UnicodeDecodeError, ValueError):
        return False


def _is_sane_text(value: str) -> bool:
    if not value:
        return False
    # Replacement characters and C0/C1 controls are strong indicators that a
    # binary payload was decoded with the wrong codec.  Keep normal whitespace
    # controls allowed for source files.
    if "\ufffd" in value:
        return False
    suspicious = sum(
        1
        for char in value
        if (ord(char) < 32 and char not in "\t\n\r\f\b")
        or 0x7F <= ord(char) <= 0x9F
    )
    return suspicious / max(1, len(value)) <= 0.01


def _extract_text_from_openxml_slide(raw_content: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(raw_content)
    except ElementTree.ParseError:
        return None

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


def _parser_failure_document(file_name: str, error: Exception) -> dict[str, Any]:
    suffix = Path(file_name).suffix.lower()
    media_type = _guess_media_type(file_name)
    kind = (
        "archive"
        if suffix == ".zip"
        else "presentation"
        if suffix in {".ppt", ".pptx"}
        else "document"
    )
    summary = {
        ".pdf": "PDF document",
        ".doc": "Word document",
        ".docx": "Word document",
        ".xlsx": "Excel workbook",
        ".ppt": "Presentation deck",
        ".pptx": "Presentation deck",
        ".zip": "Archive file",
    }.get(suffix, "File attachment")
    detail = re.sub(r"\s+", " ", str(error or "")).strip()
    if len(detail) > 240:
        detail = f"{detail[:237]}..."
    diagnostic = f"Could not extract text from {Path(file_name).name}"
    if detail:
        diagnostic = f"{diagnostic}: {detail}"
    return {
        "title": Path(file_name).stem or Path(file_name).name,
        "full_text": _unavailable_text_fallback(file_name, media_type, kind),
        "format": suffix.lstrip(".") or "binary",
        "pages": 1,
        "kind": kind,
        "media_type": media_type,
        "summary": summary,
        "parse_error": diagnostic,
    }


def _unavailable_text_fallback(file_name: str, media_type: str, kind: str) -> str:
    return (
        f"{kind.capitalize()} attachment: {Path(file_name).name}\n"
        f"Media type: {media_type}\n"
        "No extractable text is available. The original attachment is preserved."
    )


def _count_pdf_pages_bytes(raw_content: bytes) -> int:
    """Count /Type /Page markers without spawning a parser (bounded scan)."""
    import re as _re

    if not raw_content:
        return 0
    sample = raw_content[: 8 * 1024 * 1024]
    return len(_re.findall(rb"/Type\s*/Page[^s]", sample))


def _guess_media_type(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in IMAGE_MEDIA_TYPES:
        return IMAGE_MEDIA_TYPES[suffix]
    if suffix in CODE_MEDIA_TYPES:
        return CODE_MEDIA_TYPES[suffix]
    if suffix in TEXT_MEDIA_TYPES:
        return TEXT_MEDIA_TYPES[suffix]
    return "application/octet-stream"


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
