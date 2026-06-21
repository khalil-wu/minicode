from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Any


NATIVE_IMAGE_LIMIT_BYTES = 20 * 1024 * 1024
NATIVE_PDF_LIMIT_BYTES = 50 * 1024 * 1024

PDF_MEDIA_TYPE = "application/pdf"


@dataclass(frozen=True)
class AttachmentInputPlan:
    images: list[dict[str, str]] = field(default_factory=list)
    documents: list[dict[str, str]] = field(default_factory=list)
    text_hints: list[str] = field(default_factory=list)


def build_attachment_input_plan(
    attachments: list[dict[str, Any]],
    *,
    llm: Any | None = None,
) -> AttachmentInputPlan:
    """Choose native multimodal payloads and cheap artifact fallbacks.

    The policy mirrors Codex/Claude Code style attachment handling: send native
    images/PDFs when the active wire format is known to support them, but keep
    extracted document text addressable through artifacts/RAG instead of
    replaying large files into every prompt turn.
    """

    mode = _detect_llm_wire_mode(llm)
    images: list[dict[str, str]] = []
    documents: list[dict[str, str]] = []
    hints: list[str] = []

    for attachment in attachments:
        file_name = str(attachment.get("file_name") or "attachment").strip() or "attachment"
        media_type = str(attachment.get("media_type") or "").strip()
        kind = str(attachment.get("kind") or "").strip()
        data = str(attachment.get("data") or "").strip()
        artifact_id = str(attachment.get("artifact_id") or "").strip()
        doc_id = str(attachment.get("doc_id") or "").strip()
        indexed_chunks = int(attachment.get("indexed_chunks") or 0)
        size_bytes = int(attachment.get("size_bytes") or 0)
        parse_error = str(attachment.get("parse_error") or "").strip()

        used_native = False
        if kind == "image" and data:
            metadata_hint = _attachment_source_hint(artifact_id, fallback="the attachment metadata")
            if not _supports_native_images(mode, llm):
                hints.append(
                    f"- {file_name}: the active model/API does not support native image input, "
                    "so the image pixels were not sent to the model. "
                    f"Only stored metadata is available via {metadata_hint}; "
                    "switch to a vision-capable model to inspect the image itself."
                )
            elif _fits_limit(size_bytes, NATIVE_IMAGE_LIMIT_BYTES):
                images.append({"media_type": media_type or "image/png", "data": data})
                used_native = True
            else:
                hints.append(
                    f"- {file_name}: native image input skipped because the file is too large; "
                    f"use {metadata_hint} for stored metadata if needed."
                )
        elif media_type == PDF_MEDIA_TYPE and data:
            text_hint = _attachment_source_hint(
                artifact_id,
                doc_id,
                fallback="the extracted attachment text if available",
            )
            if _supports_native_pdf(mode) and _fits_limit(size_bytes, NATIVE_PDF_LIMIT_BYTES):
                documents.append(
                    {
                        "media_type": PDF_MEDIA_TYPE,
                        "data": data,
                        "file_name": file_name,
                    }
                )
                used_native = True
            elif not _supports_native_pdf(mode):
                hints.append(
                    f"- {file_name}: the active API format does not accept native PDF input; "
                    f"use {text_hint} for extracted text."
                )
            else:
                hints.append(
                    f"- {file_name}: native PDF input skipped because it exceeds the safe request limit; "
                    f"use {text_hint} for extracted text."
                )

        if parse_error:
            diagnostic_hint = _attachment_source_hint(
                artifact_id,
                fallback="the attachment diagnostic metadata",
            )
            if used_native and media_type == PDF_MEDIA_TYPE:
                hints.append(
                    f"- {file_name}: text extraction failed, but the native PDF is attached for the model to read. "
                    "If the model cannot inspect the native PDF, say that the PDF body is unavailable instead of inferring from the title."
                )
            else:
                hints.append(
                    f"- {file_name}: PDF/text extraction failed and no native PDF was attached to this model request. "
                    f"Do not summarize or interpret the document body from the title alone. "
                    f"Use {diagnostic_hint} only to inspect the diagnostic, or ask the user to retry with a supported PDF parser/model."
                )
            continue

        if artifact_id and kind != "image":
            if used_native and media_type == PDF_MEDIA_TYPE:
                hints.append(
                    f"- {file_name}: native PDF is attached; extracted text is available via "
                    f"read_artifact('{artifact_id}') and doc_id {doc_id or 'unknown'}."
                )
            else:
                source_hint = f"read_artifact('{artifact_id}')"
                if doc_id:
                    source_hint += f" or doc_id {doc_id}"
                chunk_hint = f"; {indexed_chunks} indexed chunks" if indexed_chunks else ""
                hints.append(f"- {file_name}: parsed text is available via {source_hint}{chunk_hint}.")

    return AttachmentInputPlan(images=images, documents=documents, text_hints=_dedupe(hints))


def _attachment_source_hint(
    artifact_id: str,
    doc_id: str = "",
    *,
    fallback: str,
) -> str:
    sources: list[str] = []
    if artifact_id:
        sources.append(f"read_artifact('{artifact_id}')")
    if doc_id:
        sources.append(f"doc_id {doc_id}")
    return " or ".join(sources) if sources else fallback


def _detect_llm_wire_mode(llm: Any | None) -> str:
    if llm is None:
        return "auto"

    adapters = getattr(llm, "_adapters", None)
    if isinstance(adapters, list) and adapters:
        return _detect_llm_wire_mode(adapters[0])

    class_name = llm.__class__.__name__.lower()
    if "anthropic" in class_name:
        return "anthropic"

    settings = getattr(llm, "_settings", None)
    wire_api = str(getattr(settings, "wire_api", "") or "").strip().lower()
    if wire_api in {"responses", "chat"}:
        return f"openai_{wire_api}"
    if wire_api == "anthropic":
        return "anthropic"

    return "auto"


def _supports_native_pdf(mode: str) -> bool:
    return mode in {"auto", "openai_responses", "anthropic"}


def _supports_native_images(mode: str, llm: Any | None) -> bool:
    if mode == "auto":
        return True
    if mode == "anthropic":
        return True

    settings = getattr(llm, "_settings", None)
    model = str(getattr(settings, "model", "") or "").strip().lower()
    base_url = str(getattr(settings, "base_url", "") or "").strip().lower()
    host = urlsplit(base_url).netloc.lower()

    if "deepseek" in host or "deepseek" in model:
        return False
    if ("dashscope" in host or "aliyuncs.com" in host or "qwen" in model) and not any(
        marker in model for marker in ("vl", "vision", "omni")
    ):
        return False
    if "siliconflow" in host and not any(marker in model for marker in ("vl", "vision", "omni", "gpt-4o", "gemini")):
        return False

    vision_markers = (
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "o3",
        "o4",
        "claude",
        "gemini",
        "vision",
        "vl",
        "omni",
        "qvq",
    )
    if any(marker in model for marker in vision_markers):
        return True
    if "api.openai.com" in host:
        return True
    return False


def _fits_limit(size_bytes: int, limit: int) -> bool:
    return size_bytes <= 0 or size_bytes <= limit


def _dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        deduped.append(line)
    return deduped
