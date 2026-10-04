"""
Pure utility functions extracted from ws/handler.py.

These functions handle:
  - Permission mode / level normalization
  - Attachment payload normalization
  - Conversation summary building
  - Text helpers (collapse whitespace, truncate)
"""
from __future__ import annotations

from html import escape

from typing import Any

from backend.tools.base import PermissionLevel
from backend.permissions.patterns import normalize_tool_patterns as normalize_tool_patterns

# ── Constants ──────────────────────────────────────────────

CONVERSATION_SUMMARY_MAX_CHARS = 320
USER_INPUT_METADATA_KEYS = ("display_content", "context_refs", "quoted_message")


def normalize_user_input_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Parse the renderer's user-input presentation at command admission."""
    result = {key: data[key] for key in USER_INPUT_METADATA_KEYS if key in data}
    if "display_content" in result and not isinstance(result["display_content"], str):
        raise ValueError("display_content must be a string")
    if "context_refs" in result:
        refs = result["context_refs"]
        if not isinstance(refs, list):
            raise ValueError("context_refs must be a list")
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("kind"), str) or ref["kind"] not in {
                "file", "folder", "url", "skill", "plugin", "browser_annotation",
            }:
                raise ValueError("Each context reference must have a supported kind")
            if not isinstance(ref.get("name"), str) or ("path" in ref and not isinstance(ref["path"], str)):
                raise ValueError("Context reference name and path must be strings")
        result["context_refs"] = [dict(ref) for ref in refs]
    if "quoted_message" in result and result["quoted_message"] is not None:
        quote = result["quoted_message"]
        if not isinstance(quote, dict) or any(not isinstance(quote.get(key), str) for key in ("id", "role", "content")):
            raise ValueError("quoted_message requires string id, role and content")
        result["quoted_message"] = {key: quote[key] for key in ("id", "role", "content")}
    return result


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
        raise ValueError("attachments must be a list of uploaded attachment references")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_attachments:
        if not isinstance(item, dict):
            raise ValueError("Each attachment must be an object")
        artifact_id = item.get("artifact_id", "")
        file_name = item.get("file_name", "")
        if not isinstance(artifact_id, str) or not isinstance(file_name, str):
            raise ValueError("Attachment artifact_id and file_name must be strings")
        artifact_id = artifact_id.strip()
        file_name = file_name.strip()
        doc_id = str(item.get("doc_id", "")).strip()
        if not artifact_id or not file_name:
            raise ValueError("Each attachment requires artifact_id and file_name")
        if artifact_id in seen_ids:
            continue
        seen_ids.add(artifact_id)
        for field_name in ("size_bytes", "source_char_count"):
            value = item.get(field_name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Attachment {field_name} must be a nonnegative integer")
        entry = {
            "id": str(item.get("id", "")).strip() or f"att_{artifact_id}",
            "kind": str(item.get("kind", "")).strip() or "document",
            "file_name": file_name,
            "media_type": str(item.get("media_type", "")).strip() or "text/plain",
            "artifact_id": artifact_id,
            "doc_id": doc_id,
            "size_bytes": item.get("size_bytes", 0),
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
            "input_source": str(item.get("input_source", "")).strip(),
            "source_char_count": item.get("source_char_count", 0),
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
        attributes = {
            "file_name": attachment["file_name"], "kind": kind,
            "doc_id": attachment["doc_id"], "artifact_id": attachment["artifact_id"],
            "status": status.lstrip(", "),
        }
        lines.append("<attachment " + " ".join(
            f'{name}="{escape(value, quote=True)}"' for name, value in attributes.items()
        ) + " />")
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
