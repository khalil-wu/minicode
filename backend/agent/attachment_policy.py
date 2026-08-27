from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.llm.capabilities import capabilities_for_adapter


# MiniCode derives these from the provider request limits: Anthropic caps an
# encoded image at 5 MiB, so raw bytes must stay at or below 3.75 MiB after the
# 4/3 base64 expansion; a PDF must stay below 20 MiB to leave room inside the
# 32 MiB request envelope.  These conservative limits also keep fallback
# providers safe instead of accepting an attachment that only the primary can
# serialize.
NATIVE_IMAGE_LIMIT_BYTES = (5 * 1024 * 1024 * 3) // 4
NATIVE_PDF_LIMIT_BYTES = 20 * 1024 * 1024
NATIVE_PDF_PAGE_LIMIT = 100
NATIVE_MEDIA_COUNT_LIMIT = 100

PDF_MEDIA_TYPE = "application/pdf"

@dataclass(frozen=True)
class AttachmentInputPlan:
    images: list[dict[str, str]] = field(default_factory=list)
    documents: list[dict[str, str]] = field(default_factory=list)
    text_hints: list[str] = field(default_factory=list)
    inlined_texts: list[dict[str, str]] = field(default_factory=list)
    unavailable: list[dict[str, str]] = field(default_factory=list)


class AttachmentUnavailableError(RuntimeError):
    """Raised when a current-turn attachment cannot be resolved by its owner."""

    def __init__(self, attachments: list[dict[str, str]]) -> None:
        self.attachments = [dict(item) for item in attachments]
        labels = [
            str(item.get("file_name") or item.get("artifact_id") or "attachment")
            for item in attachments
        ]
        rendered = ", ".join(dict.fromkeys(labels))
        super().__init__(
            f"The attachment is no longer available in this conversation: {rendered}. "
            "Restore or upload it again before sending."
        )


def build_attachment_input_plan(
    attachments: list[dict[str, Any]],
    *,
    llm: Any | None = None,
    attachment_store: Any | None = None,
    conversation_id: str = "",
    workspace_root: str = "",
) -> AttachmentInputPlan:
    """Choose native multimodal payloads and cheap artifact fallbacks.

    The policy mirrors MiniCode style attachment handling: send native
    images/PDFs when the active wire format is known to support them, but keep
    extracted document text addressable through scoped artifacts instead of
    replaying large files into every prompt turn.
    """

    capability_llm = _primary_llm_adapter(llm)
    mode = _detect_llm_wire_mode(capability_llm)
    images: list[dict[str, str]] = []
    documents: list[dict[str, str]] = []
    hints: list[str] = []
    inlined_texts: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []

    for attachment in attachments:
        file_name = (
            str(attachment.get("file_name") or "attachment").strip() or "attachment"
        )
        media_type = str(attachment.get("media_type") or "").strip()
        kind = str(attachment.get("kind") or "").strip()
        artifact_id = str(attachment.get("artifact_id") or "").strip()
        data = str(attachment.get("data") or "").strip()
        stored_attachment: dict[str, Any] = {}
        if artifact_id and attachment_store is not None:
            try:
                payload = attachment_store.get_payload(
                    artifact_id,
                    conversation_id=conversation_id,
                    workspace_root=workspace_root,
                )
            except Exception:  # noqa: BLE001 - a missing native body falls back to extracted text
                payload = None
            if payload is None:
                # Owner-scoped runtime inputs must resolve through the durable
                # store. Unscoped SDK/local ingestion may still pass a direct
                # in-process payload because there is no cross-session owner to
                # confuse with another conversation.
                if conversation_id or workspace_root:
                    unavailable.append(
                        {"artifact_id": artifact_id, "file_name": file_name}
                    )
                    continue
                payload = None
            if payload is not None:
                metadata = payload.get("metadata")
                if isinstance(metadata, dict):
                    raw_stored_attachment = metadata.get("attachment")
                    if isinstance(raw_stored_attachment, dict):
                        stored_attachment = raw_stored_attachment
                data = str(payload.get("native_data") or "").strip()
                file_name = (
                    str(stored_attachment.get("file_name") or file_name).strip()
                    or "attachment"
                )
                media_type = str(
                    stored_attachment.get("media_type") or media_type
                ).strip()
                kind = str(stored_attachment.get("kind") or kind).strip()
        size_bytes = _native_data_size_bytes(data) if data else int(
            stored_attachment.get("size_bytes")
            or attachment.get("size_bytes")
            or 0
        )
        parse_error = str(attachment.get("parse_error") or "").strip()
        try:
            page_count = max(
                0,
                int(attachment.get("page_count") or attachment.get("pages") or 0),
            )
        except (TypeError, ValueError):
            page_count = 0
        input_source = str(attachment.get("input_source") or "").strip()

        used_native = False
        if kind == "image" and data:
            metadata_hint = _attachment_source_hint(
                artifact_id, fallback="the attachment metadata"
            )
            if not _supports_native_images(mode, capability_llm):
                hints.append(
                    f"- {file_name}: the active model/API does not support native image input, "
                    "so the image pixels were not sent to the model. "
                    f"Only stored metadata is available via {metadata_hint}; "
                    "switch to a vision-capable model to inspect the image itself."
                )
            elif len(images) + len(documents) >= NATIVE_MEDIA_COUNT_LIMIT:
                hints.append(
                    f"- {file_name}: native media input skipped because the request already contains "
                    f"the provider maximum of {NATIVE_MEDIA_COUNT_LIMIT} media items (images/PDFs)."
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
                fallback="the extracted attachment text if available",
            )
            if (
                _supports_native_pdf(mode, capability_llm)
                and _fits_limit(size_bytes, NATIVE_PDF_LIMIT_BYTES)
                and (page_count <= 0 or page_count <= NATIVE_PDF_PAGE_LIMIT)
                and len(images) + len(documents) < NATIVE_MEDIA_COUNT_LIMIT
            ):
                documents.append(
                    {
                        "media_type": PDF_MEDIA_TYPE,
                        "data": data,
                        "file_name": file_name,
                    }
                )
                used_native = True
            elif len(images) + len(documents) >= NATIVE_MEDIA_COUNT_LIMIT:
                hints.append(
                    f"- {file_name}: native media input skipped because the request already contains "
                    f"the provider maximum of {NATIVE_MEDIA_COUNT_LIMIT} media items (images/PDFs)."
                )
            elif not _supports_native_pdf(mode, capability_llm):
                hints.append(
                    f"- {file_name}: the active API format does not accept native PDF input; "
                    f"use {text_hint} for extracted text."
                )
            elif page_count > NATIVE_PDF_PAGE_LIMIT:
                hints.append(
                    f"- {file_name}: native PDF input skipped because it has {page_count} pages, "
                    f"above the provider maximum of {NATIVE_PDF_PAGE_LIMIT}; use {text_hint} for extracted text."
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

        # MiniCode preserves the complete pasted/file text in the
        # user turn. Size changes must not turn the user's message into a
        # compulsory follow-up tool call with different semantics.
        if (
            attachment_store is not None
            and artifact_id
            and kind != "image"
            and not used_native
        ):
            inlined = _inline_text(
                attachment_store,
                artifact_id,
                file_name,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            )
            if inlined is not None:
                inlined_texts.append(inlined)
                continue

        if artifact_id and kind != "image":
            if used_native and media_type == PDF_MEDIA_TYPE:
                hints.append(
                    f"- {file_name}: native PDF is attached; extracted text is available via "
                    f"read_artifact('{artifact_id}')."
                )
            else:
                source_hint = f"read_artifact('{artifact_id}')"
                if input_source == "pasted_text":
                    hints.append(
                        f"- {file_name}: this file is the user's pasted message body. Before responding, "
                        f"you must read its full contents with {source_hint} and treat those "
                        "contents as the user message. Do not answer from the filename, title, or summary."
                    )
                else:
                    hints.append(
                        f"- {file_name}: before answering about this file, use {source_hint} "
                        "and base claims on the returned contents. "
                        "Do not infer document contents from the filename, title, or summary."
                    )

    return AttachmentInputPlan(
        images=images,
        documents=documents,
        text_hints=_dedupe(hints),
        inlined_texts=inlined_texts,
        unavailable=unavailable,
    )


def _inline_text(
    attachment_store: Any,
    artifact_id: str,
    file_name: str,
    *,
    conversation_id: str = "",
    workspace_root: str = "",
) -> dict[str, str] | None:
    """Return the complete owner-scoped text attachment for user projection."""
    try:
        content = attachment_store.get(
            artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
    except Exception:  # noqa: BLE001 — never break the turn on store read errors
        return None
    if not content or not content.strip():
        return None
    return {"file_name": file_name, "artifact_id": artifact_id, "content": content}


def _primary_llm_adapter(llm: Any | None) -> Any | None:
    """Return the concrete adapter hidden behind a session-owned wrapper."""
    current = llm
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        wrapped = getattr(current, "_llm", None) or getattr(current, "llm", None)
        if wrapped is not None:
            current = wrapped
            continue
        break
    return current


def _attachment_source_hint(
    artifact_id: str,
    *,
    fallback: str,
) -> str:
    sources: list[str] = []
    if artifact_id:
        sources.append(f"read_artifact('{artifact_id}')")
    return " or ".join(sources) if sources else fallback


def _detect_llm_wire_mode(llm: Any | None) -> str:
    if llm is None:
        return "auto"

    # The adapter is the source of truth for its wire contract. Do not infer
    # protocol or model capabilities from Python class names, hostnames, or
    # model slugs.
    capabilities = capabilities_for_adapter(llm)
    wire_api = str(capabilities.wire_api or "").strip().lower()
    if wire_api in {"responses", "chat"}:
        return f"openai_{wire_api}"
    if wire_api == "anthropic":
        return "anthropic"

    return "auto"


def _supports_native_pdf(mode: str, llm: Any | None = None) -> bool:
    capability = capabilities_for_adapter(llm).native_pdf
    if capability is not None:
        return capability
    return mode in {"auto", "openai_responses", "anthropic"}


def _supports_native_images(mode: str, llm: Any | None) -> bool:
    # Match MiniCode's model-input contract: unknown metadata stays permissive, and
    # only an explicit provider declaration can suppress image bytes. Never
    # infer vision support from a hostname or a model-name substring.
    del mode
    return capabilities_for_adapter(llm).vision is not False


def _fits_limit(size_bytes: int, limit: int) -> bool:
    return size_bytes <= 0 or size_bytes <= limit


def _native_data_size_bytes(data: str) -> int:
    """Return decoded base64 size without allocating a second large buffer."""
    compact = "".join(str(data or "").split())
    if not compact:
        return 0
    padding = 2 if compact.endswith("==") else 1 if compact.endswith("=") else 0
    return max(0, (len(compact) * 3) // 4 - padding)


def _dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        deduped.append(line)
    return deduped
