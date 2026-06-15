"""
Pure utility functions extracted from ws/handler.py.

These functions handle:
  - Permission mode / level normalization
  - Conversation facts: normalize, merge, inherit, extract, summarize
  - Attachment payload normalization
  - Conversation summary building
  - Text helpers (collapse whitespace, truncate)
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from backend.tools.base import PermissionLevel

# ── Constants ──────────────────────────────────────────────

CONVERSATION_SUMMARY_MAX_CHARS = 320
MAX_INHERITED_FACTS = 24
MAX_FACT_NOTE_ITEMS = 8
CONTROL_PROTOCOL_V1 = "control_v1"


# ── Control protocol ──────────────────────────────────────

def uses_control_protocol(value: Any) -> bool:
    return str(value or "").strip().lower() in {CONTROL_PROTOCOL_V1, "control"}


# ── Permission helpers ────────────────────────────────────

def normalize_permission_mode(value: str) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    aliases = {
        "on": "plan",
        "off": "default",
        "ask": "confirm",
        "ask_permissions": "confirm",
        "full_access": "bypass",
        "danger_full_access": "bypass",
        "acceptedits": "accept_edits",
    }
    mode = aliases.get(normalized, normalized)
    if mode in {"default", "plan", "confirm", "bypass", "auto", "accept_edits"}:
        return mode
    return None


def normalize_permission_level(value: Any) -> PermissionLevel | None:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    aliases = {
        "diff_review": "diff",
        "diffreview": "diff",
        "always_deny": "deny",
        "block": "deny",
    }
    normalized = aliases.get(raw, raw)
    for level in PermissionLevel:
        if level.value == normalized:
            return level
    return None


def permission_level_to_token(level: PermissionLevel) -> str:
    return str(level.value)


def normalize_tool_patterns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        pattern = str(item or "").strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        normalized.append(pattern)
    return normalized


def normalize_permission_overrides(value: Any) -> dict[str, PermissionLevel]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, PermissionLevel] = {}
    for raw_pattern, raw_level in value.items():
        pattern = str(raw_pattern or "").strip()
        if not pattern:
            continue
        level = normalize_permission_level(raw_level)
        if level is None:
            continue
        normalized[pattern] = level
    return normalized


def serialize_permission_overrides(overrides: dict[str, PermissionLevel]) -> dict[str, str]:
    return {pattern: permission_level_to_token(level) for pattern, level in overrides.items()}


# ── Text helpers ──────────────────────────────────────────

def collapse_whitespace(value: str) -> str:
    return " ".join(str(value or "").replace("`", "").split()).strip()


def truncate_middle(value: str, max_chars: int) -> str:
    text = value.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= 5:
        return text[:max_chars]

    separator = " ... "
    available = max_chars - len(separator)
    head = max(1, available // 2)
    tail = max(1, available - head)
    return f"{text[:head]}{separator}{text[-tail:]}"


# ── Attachment helpers ────────────────────────────────────

def build_attachment_summary(attachments: list[dict[str, Any]]) -> str:
    labels: list[str] = []
    for attachment in attachments[:3]:
        file_name = str(attachment.get("file_name", "")).strip()
        if not file_name:
            continue
        kind = str(attachment.get("kind", "")).strip() or "document"
        labels.append(f"{file_name} ({kind})")

    if not labels:
        return ""

    suffix = ""
    if len(attachments) > len(labels):
        suffix = f" +{len(attachments) - len(labels)} more"
    return f"Attachments: {', '.join(labels)}{suffix}"


def normalize_attachment_payloads(raw_attachments: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_attachments, list):
        return []

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id", "")).strip()
        file_name = str(item.get("file_name", "")).strip()
        doc_id = str(item.get("doc_id", "")).strip()
        if not artifact_id or not file_name:
            continue
        if artifact_id in seen_ids:
            continue
        seen_ids.add(artifact_id)
        entry = {
            "id": str(item.get("id", "")).strip() or f"att_{artifact_id}",
            "kind": str(item.get("kind", "")).strip() or "document",
            "file_name": file_name,
            "media_type": str(item.get("media_type", "")).strip() or "text/plain",
            "artifact_id": artifact_id,
            "doc_id": doc_id,
            "indexed_chunks": int(item.get("indexed_chunks", 0) or 0),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
        }
        if entry["kind"] == "image" or entry["media_type"] == "application/pdf":
            data = str(item.get("data", "")).strip()
            if data:
                entry["data"] = data
        parse_error = str(item.get("parse_error", "")).strip()
        if parse_error:
            entry["parse_error"] = parse_error
        normalized.append(entry)
    return normalized


def build_effective_user_message(
    user_message: str,
    attachments: list[dict[str, Any]],
) -> str:
    content = user_message.strip()
    if not attachments:
        return content

    lines = []
    for attachment in attachments:
        parse_error = str(attachment.get("parse_error") or "").strip()
        status = ""
        if parse_error:
            media_type = str(attachment.get("media_type") or "").strip()
            status = (
                ", text_extraction=failed, warning=do_not_infer_document_body_from_title"
                if media_type == "application/pdf"
                else ", text_extraction=failed"
            )
        lines.append(
            "- "
            f"{attachment['file_name']} "
            f"({attachment['kind']}, doc_id={attachment['doc_id']}, "
            f"artifact_id={attachment['artifact_id']}, indexed_chunks={attachment['indexed_chunks']}{status})"
        )
    attachment_block = (
        "Attached files are available for this request.\n"
        "Use provider-native multimodal input first for attached images and PDFs when the active model supports it.\n"
        "Use read_artifact only when you need extracted text, indexed chunks, or a fallback for unsupported/oversized files.\n"
        "If an attached PDF says text_extraction=failed and native PDF input is unavailable, do not summarize or interpret the PDF body from the file name/title alone; say the body is unavailable.\n"
        "Do not ask the user to re-upload the same file or re-parse it when artifact_id is present.\n"
        "Attachments:\n"
        + "\n".join(lines)
    )
    if content:
        return f"{content}\n\n{attachment_block}"
    return attachment_block


# ── Fact helpers ──────────────────────────────────────────

def make_fact_key(kind: str, content: str) -> str:
    digest = hashlib.sha1(f"{kind}:{content}".encode("utf-8")).hexdigest()
    return f"{kind}:{digest[:12]}"


def normalize_conversation_fact(raw_fact: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw_fact, dict):
        return None

    kind = collapse_whitespace(str(raw_fact.get("kind", "")))
    content = collapse_whitespace(str(raw_fact.get("content", "")))
    if not kind or not content:
        return None

    priority = raw_fact.get("priority", 50)
    depth = raw_fact.get("depth", 0)
    try:
        normalized_priority = int(priority)
    except (TypeError, ValueError):
        normalized_priority = 50
    try:
        normalized_depth = max(0, int(depth))
    except (TypeError, ValueError):
        normalized_depth = 0

    source_conversation_id = collapse_whitespace(str(raw_fact.get("source_conversation_id", "")))
    origin_conversation_id = collapse_whitespace(
        str(raw_fact.get("origin_conversation_id") or source_conversation_id)
    )
    updated_at = collapse_whitespace(str(raw_fact.get("updated_at", ""))) or datetime.now(UTC).isoformat()
    key = collapse_whitespace(str(raw_fact.get("key", ""))) or make_fact_key(kind, content)

    return {
        "key": key,
        "kind": kind,
        "content": content,
        "source_conversation_id": source_conversation_id,
        "origin_conversation_id": origin_conversation_id,
        "updated_at": updated_at,
        "priority": normalized_priority,
        "depth": normalized_depth,
    }


def merge_conversation_facts(*fact_groups: list[dict[str, Any]], limit: int = MAX_INHERITED_FACTS) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in fact_groups:
        for raw_fact in group:
            fact = normalize_conversation_fact(raw_fact)
            if fact is None:
                continue
            existing = merged.get(fact["key"])
            if existing is None:
                merged[fact["key"]] = fact
                continue
            if (
                fact["priority"] > existing["priority"]
                or (
                    fact["priority"] == existing["priority"]
                    and (
                        fact["updated_at"] > existing["updated_at"]
                        or (
                            fact["updated_at"] == existing["updated_at"]
                            and fact["depth"] < existing["depth"]
                        )
                    )
                )
            ):
                merged[fact["key"]] = fact

    ordered = sorted(
        merged.values(),
        key=lambda fact: (-int(fact["priority"]), int(fact["depth"]), str(fact["updated_at"]), str(fact["key"])),
        reverse=False,
    )
    return ordered[:limit]


def inherit_conversation_fact(raw_fact: dict[str, Any], *, source_conversation_id: str) -> dict[str, Any] | None:
    fact = normalize_conversation_fact(raw_fact)
    if fact is None:
        return None
    fact["source_conversation_id"] = source_conversation_id
    fact["origin_conversation_id"] = fact["origin_conversation_id"] or source_conversation_id
    fact["depth"] = int(fact["depth"]) + 1
    return fact


def build_inherited_memory_note(facts: list[dict[str, Any]], fallback_summary: str = "") -> str:
    normalized_facts = merge_conversation_facts(facts)
    if normalized_facts:
        return "\n".join(
            f"- {fact['content']}"
            for fact in normalized_facts[:MAX_FACT_NOTE_ITEMS]
        )
    return truncate_middle(collapse_whitespace(fallback_summary), CONVERSATION_SUMMARY_MAX_CHARS)


def build_summary_from_facts(facts: list[dict[str, Any]], fallback_summary: str = "") -> str:
    normalized_facts = merge_conversation_facts(facts, limit=3)
    if normalized_facts:
        return truncate_middle(
            " | ".join(fact["content"] for fact in normalized_facts),
            CONVERSATION_SUMMARY_MAX_CHARS,
        )
    return truncate_middle(collapse_whitespace(fallback_summary), CONVERSATION_SUMMARY_MAX_CHARS)


def extract_turn_facts(
    *,
    conversation_id: str,
    user_message: str,
    attachments: list[dict[str, Any]],
    assistant_content: str,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    timestamp = datetime.now(UTC).isoformat()
    normalized_user_message = collapse_whitespace(user_message)
    normalized_assistant_content = collapse_whitespace(assistant_content)

    if normalized_user_message:
        user_fact_content = f"User request: {truncate_middle(normalized_user_message, 160)}"
        facts.append(
            {
                "key": make_fact_key("user_request", user_fact_content),
                "kind": "user_request",
                "content": user_fact_content,
                "source_conversation_id": conversation_id,
                "origin_conversation_id": conversation_id,
                "updated_at": timestamp,
                "priority": 70,
                "depth": 0,
            }
        )

    for attachment in attachments[:4]:
        file_name = collapse_whitespace(str(attachment.get("file_name", "")))
        if not file_name:
            continue
        kind = collapse_whitespace(str(attachment.get("kind", ""))) or "document"
        media_type = collapse_whitespace(str(attachment.get("media_type", "")))
        attachment_summary = collapse_whitespace(str(attachment.get("summary", "")))
        detail_parts = [f"Attachment: {file_name} ({kind})"]
        if media_type:
            detail_parts.append(media_type)
        if attachment_summary:
            detail_parts.append(attachment_summary)
        attachment_fact_content = ". ".join(detail_parts)
        facts.append(
            {
                "key": make_fact_key("attachment", f"{file_name}:{kind}:{media_type}"),
                "kind": "attachment",
                "content": attachment_fact_content,
                "source_conversation_id": conversation_id,
                "origin_conversation_id": conversation_id,
                "updated_at": timestamp,
                "priority": 95,
                "depth": 0,
            }
        )

    if normalized_assistant_content:
        assistant_fact_content = f"Assistant conclusion: {truncate_middle(normalized_assistant_content, 200)}"
        facts.append(
            {
                "key": make_fact_key("assistant_conclusion", assistant_fact_content),
                "kind": "assistant_conclusion",
                "content": assistant_fact_content,
                "source_conversation_id": conversation_id,
                "origin_conversation_id": conversation_id,
                "updated_at": timestamp,
                "priority": 90,
                "depth": 0,
            }
        )

    return merge_conversation_facts(facts)


# ── Conversation summary ──────────────────────────────────

def build_conversation_summary(
    *,
    user_message: str,
    attachments: list[dict[str, Any]],
    assistant_content: str,
    compaction_summary: str = "",
) -> str:
    parts: list[str] = []

    compacted = collapse_whitespace(compaction_summary)
    if compacted:
        parts.append(f"Earlier: {truncate_middle(compacted, 96)}")

    normalized_user_message = collapse_whitespace(user_message)
    if normalized_user_message:
        parts.append(f"User: {truncate_middle(normalized_user_message, 72)}")

    attachment_summary = build_attachment_summary(attachments)
    if attachment_summary:
        parts.append(attachment_summary)

    normalized_assistant_content = collapse_whitespace(assistant_content)
    if normalized_assistant_content:
        assistant_limit = 176 if attachment_summary else 220
        parts.append(
            f"Assistant: {truncate_middle(normalized_assistant_content, assistant_limit)}"
        )

    return truncate_middle(" | ".join(part for part in parts if part), CONVERSATION_SUMMARY_MAX_CHARS)


def build_effective_transcript_content(message: dict[str, Any]) -> str:
    role = str(message.get("role", "")).strip()
    content = str(message.get("content", ""))
    if role == "user":
        attachments = normalize_attachment_payloads(message.get("attachments", []))
        return build_effective_user_message(content, attachments)
    return content


def rebuild_local_facts_from_transcript(
    conversation_id: str,
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    pending_user: dict[str, Any] | None = None

    for message in transcript:
        role = str(message.get("role", "")).strip()
        if role == "user":
            pending_user = message
            continue
        if role != "assistant" or pending_user is None:
            continue

        facts = merge_conversation_facts(
            facts,
            extract_turn_facts(
                conversation_id=conversation_id,
                user_message=str(pending_user.get("content", "")),
                attachments=normalize_attachment_payloads(pending_user.get("attachments", [])),
                assistant_content=str(message.get("content", "")),
            ),
        )
        pending_user = None

    return facts


def build_summary_from_transcript(
    transcript: list[dict[str, Any]],
    *,
    compaction_summary: str = "",
) -> str:
    latest_user: dict[str, Any] | None = None
    latest_assistant: dict[str, Any] | None = None

    for message in transcript:
        role = str(message.get("role", "")).strip()
        if role == "user":
            latest_user = message
            latest_assistant = None
        elif role == "assistant" and latest_user is not None:
            latest_assistant = message

    if latest_user is None or latest_assistant is None:
        return ""

    return build_conversation_summary(
        user_message=str(latest_user.get("content", "")),
        attachments=normalize_attachment_payloads(latest_user.get("attachments", [])),
        assistant_content=str(latest_assistant.get("content", "")),
        compaction_summary=compaction_summary,
    )
