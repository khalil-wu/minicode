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


# Native image and document blocks are estimated at a fixed 2K tokens until
# exact provider usage arrives. A fixed estimate is deliberate: deriving tokens
# from base64 payload size tracks encoding overhead, not model cost.
# Mechanism and constant taken from cc (services/compact/microCompact.ts:38
# IMAGE_MAX_TOKEN_SIZE, which services/tokenEstimation.ts:404 also aligns to);
# the ledger boundary and its accounting are MiniCode's own.
NATIVE_ATTACHMENT_TOKEN_ESTIMATE = 2_000


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
    """Estimate native multimodal blocks using the fixed per-block contract."""

    tokens = 0
    count = 0
    sources: list[str] = []

    for image in images or []:
        if not isinstance(image, dict) or not str(image.get("data") or "").strip():
            continue
        tokens += NATIVE_ATTACHMENT_TOKEN_ESTIMATE
        count += 1
        sources.append(str(image.get("media_type") or "image"))

    for document in documents or []:
        if not isinstance(document, dict) or not str(document.get("data") or "").strip():
            continue
        tokens += NATIVE_ATTACHMENT_TOKEN_ESTIMATE
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
