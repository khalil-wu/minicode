from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Any


NATIVE_IMAGE_LIMIT_BYTES = 20 * 1024 * 1024
NATIVE_PDF_LIMIT_BYTES = 50 * 1024 * 1024

PDF_MEDIA_TYPE = "application/pdf"

# Small text attachments are inlined verbatim into the user message so the model
# sees them without calling read_artifact (matches cc/Codex pasted-text behavior;
# avoids the "model didn't look at the pasted file" failure where the model only
# gets a hint and guesses instead). Larger docs keep the read_artifact hint.
INLINE_TEXT_LIMIT_CHARS = 8_000
PASTED_TEXT_INLINE_LIMIT_CHARS = 64_000


@dataclass(frozen=True)
class AttachmentInputPlan:
    images: list[dict[str, str]] = field(default_factory=list)
    documents: list[dict[str, str]] = field(default_factory=list)
    text_hints: list[str] = field(default_factory=list)
    inlined_texts: list[dict[str, str]] = field(default_factory=list)


def build_attachment_input_plan(
    attachments: list[dict[str, Any]],
    *,
    llm: Any | None = None,
    attachment_store: Any | None = None,
) -> AttachmentInputPlan:
    """Choose native multimodal payloads and cheap artifact fallbacks.

    The policy mirrors Codex/Claude Code style attachment handling: send native
    images/PDFs when the active wire format is known to support them, but keep
    extracted document text addressable through artifacts/RAG instead of
    replaying large files into every prompt turn.
    """

    capability_llm = _primary_llm_adapter(llm)
    mode = _detect_llm_wire_mode(capability_llm)
    images: list[dict[str, str]] = []
    documents: list[dict[str, str]] = []
    hints: list[str] = []
    inlined_texts: list[dict[str, str]] = []

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
        input_source = str(attachment.get("input_source") or "").strip()

        used_native = False
        if kind == "image" and data:
            metadata_hint = _attachment_source_hint(artifact_id, fallback="the attachment metadata")
            if not _supports_native_images(mode, capability_llm):
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

        # Inline small text attachments directly into the user message so the
        # model sees them without calling read_artifact (cc/Codex pasted-text
        # behavior). This also makes the content persist in history for
        # follow-up turns. Large docs keep the read_artifact hint below.
        if (
            attachment_store is not None
            and artifact_id
            and kind != "image"
            and not used_native
        ):
            inline_limit = (
                PASTED_TEXT_INLINE_LIMIT_CHARS
                if input_source == "pasted_text"
                else INLINE_TEXT_LIMIT_CHARS
            )
            inlined = _inline_small_text(
                attachment_store,
                artifact_id,
                file_name,
                limit_chars=inline_limit,
            )
            if inlined is not None:
                inlined_texts.append(inlined)
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
                if input_source == "pasted_text":
                    hints.append(
                        f"- {file_name}: this file is the user's pasted message body. Before responding, "
                        f"you must read its full contents with {source_hint}{chunk_hint} and treat those "
                        "contents as the user message. Do not answer from the filename, title, or summary."
                    )
                else:
                    hints.append(
                        f"- {file_name}: before answering about this file, use {source_hint}{chunk_hint} "
                        "and base claims on the returned contents. "
                        "Do not infer document contents from the filename, title, or summary."
                    )

    return AttachmentInputPlan(
        images=images,
        documents=documents,
        text_hints=_dedupe(hints),
        inlined_texts=inlined_texts,
    )


def _inline_small_text(
    attachment_store: Any,
    artifact_id: str,
    file_name: str,
    *,
    limit_chars: int = INLINE_TEXT_LIMIT_CHARS,
) -> dict[str, str] | None:
    """Return {file_name, artifact_id, content} for a small text attachment, or
    None if the content is missing/empty/too large to inline safely."""
    try:
        content = attachment_store.get(artifact_id)
    except Exception:  # noqa: BLE001 — never break the turn on store read errors
        return None
    if not content or not content.strip():
        return None
    if len(content) > limit_chars:
        return None
    return {"file_name": file_name, "artifact_id": artifact_id, "content": content}


def _primary_llm_adapter(llm: Any | None) -> Any | None:
    """Return the first concrete adapter hidden behind fallback/cache wrappers."""
    current = llm
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        adapters = getattr(current, "_adapters", None)
        if isinstance(adapters, list) and adapters:
            current = adapters[0]
            continue
        adapters = getattr(current, "adapters", None)
        if isinstance(adapters, list) and adapters:
            current = adapters[0]
            continue
        wrapped = getattr(current, "_llm", None) or getattr(current, "llm", None)
        if wrapped is not None:
            current = wrapped
            continue
        break
    return current


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

    if _is_known_text_only_image_provider(host, model):
        return False

    if _model_declares_vision_support(model):
        return True

    if "api.openai.com" in host:
        return True
    # For custom OpenAI-compatible endpoints, prefer native image input unless
    # the provider/model is explicitly known to reject it. Silently dropping the
    # pixels is worse than surfacing a provider error the user can act on.
    return mode.startswith("openai_")


def _is_known_text_only_image_provider(host: str, model: str) -> bool:
    """Providers/models where OpenAI-compatible chat rejects image_url input."""
    if "deepseek" in host or "deepseek" in model:
        return True
    if ("dashscope" in host or "aliyuncs.com" in host or "qwen" in model) and not _model_declares_vision_support(model):
        return True
    if "siliconflow" in host and not _model_declares_vision_support(model):
        return True
    return False


def _model_declares_vision_support(model: str) -> bool:
    normalized = model.replace("_", "-").lower()
    vision_markers = (
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "o3",
        "o4",
        "claude",
        "gemini",
        "vision",
        "visual",
        "-vl",
        "vl-",
        "qwen-vl",
        "qwen2-vl",
        "qwen2.5-vl",
        "qwen3-vl",
        "omni",
        "qvq",
        "glm-4v",
        "glm-4.5v",
        "doubao-vision",
        "doubao-seed-vision",
        "pixtral",
        "llava",
        "internvl",
        "minicpm-v",
        "grok-vision",
    )
    return any(marker in normalized for marker in vision_markers)


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
