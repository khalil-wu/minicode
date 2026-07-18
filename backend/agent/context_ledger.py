from __future__ import annotations

from typing import Literal, TypedDict


ContextLedgerCategory = Literal[
    "system_runtime",
    "guidelines",
    "skills",
    "files_attachments",
    "history",
    "tool_results",
    "memory",
    "compaction_summaries",
]


class ContextLedgerEntry(TypedDict):
    category: ContextLedgerCategory
    label: str
    estimated_tokens: int
    item_count: int
    source_count: int
    sources: list[str]


class ContextLedger(TypedDict):
    schema_version: Literal[1]
    estimated_tokens: int
    actual_tokens: int
    compaction_count: int
    native_attachment_tokens: int
    native_attachment_count: int
    entries: list[ContextLedgerEntry]


def estimate_native_attachments(
    images: list[dict[str, str]] | None,
    documents: list[dict[str, str]] | None,
) -> tuple[int, int, list[str]]:
    """Return a provider-neutral estimate for native multimodal prompt inputs.

    Native image cost is primarily driven by provider-side visual processing,
    not base64 text length, so each image gets a fixed visual floor plus a small
    payload-size component. Native PDFs use a text-like byte estimate with a
    floor because even short documents carry provider parsing overhead.
    """

    tokens = 0
    count = 0
    sources: list[str] = []

    for image in images or []:
        if not isinstance(image, dict) or not str(image.get("data") or "").strip():
            continue
        size_bytes = _base64_decoded_size(str(image.get("data") or ""))
        tokens += 1_024 + (size_bytes // 4_096)
        count += 1
        sources.append(str(image.get("media_type") or "image"))

    for document in documents or []:
        if not isinstance(document, dict) or not str(document.get("data") or "").strip():
            continue
        size_bytes = _base64_decoded_size(str(document.get("data") or ""))
        tokens += max(256, min(100_000, size_bytes // 4))
        count += 1
        sources.append(
            str(document.get("file_name") or document.get("media_type") or "document")
        )

    return tokens, count, list(dict.fromkeys(source for source in sources if source))


def empty_context_ledger(*, estimated_tokens: int = 0, actual_tokens: int = 0) -> ContextLedger:
    return {
        "schema_version": 1,
        "estimated_tokens": max(0, int(estimated_tokens)),
        "actual_tokens": max(0, int(actual_tokens)),
        "compaction_count": 0,
        "native_attachment_tokens": 0,
        "native_attachment_count": 0,
        "entries": [],
    }


def _base64_decoded_size(data: str) -> int:
    encoded = data.strip()
    if "," in encoded and encoded.lower().startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    encoded = "".join(encoded.split())
    if not encoded:
        return 0
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, (len(encoded) * 3) // 4 - padding)
