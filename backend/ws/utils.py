"""
Pure utility functions extracted from ws/handler.py.

These functions handle:
  - Permission mode / level normalization
  - Attachment payload normalization
  - Conversation summary building
  - Text helpers (collapse whitespace, truncate)
"""
from __future__ import annotations

from typing import Any

from backend.tools.base import PermissionLevel

# ── Constants ──────────────────────────────────────────────

CONVERSATION_SUMMARY_MAX_CHARS = 320


# ── Permission helpers ────────────────────────────────────

def normalize_permission_mode(value: str) -> str | None:
    """Accept one wire permission mode, or ``None`` when it is not a mode.

    The token table lives with the permission checker so the wire boundary and
    the policy boundary cannot drift apart; this wrapper only turns the
    checker's rejection into the ``None`` that wire callers expect.
    """
    from backend.permissions.checker import normalize_permission_mode_token

    if not str(value or "").strip():
        return None
    try:
        return normalize_permission_mode_token(value)
    except ValueError:
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
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
            "input_source": str(item.get("input_source", "")).strip(),
            "source_char_count": int(item.get("source_char_count", 0) or 0),
        }
        # Uploaded media is resolved from AttachmentStore by artifact_id. Raw
        # base64 is deliberately not accepted on the WebSocket boundary.
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
        kind = str(attachment.get("kind") or "document").strip() or "document"
        status = ", text_extraction=failed" if parse_error else ""
        lines.append(
            f'<attachment file_name="{attachment["file_name"]}" kind="{kind}" '
            f'doc_id="{attachment["doc_id"]}" artifact_id="{attachment["artifact_id"]}" '
            f'status="{status.lstrip(", ")}" />'
        )
    attachment_block = (
        "<attachments>\n" + "\n".join(lines) + "\n</attachments>"
    )
    if content:
        return f"{content}\n\n{attachment_block}"
    return attachment_block


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
