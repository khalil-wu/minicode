"""Search excerpts from the same public text the conversation renders."""
from __future__ import annotations

import re
from typing import Any

from backend.agent.public_projection import public_text
from .models import ConversationSummary


def search_conversation_summary(summary: ConversationSummary) -> dict[str, Any]:
    """Only the identity, ordering and owner metadata consumed by search rows."""
    text_fields = (("id", 1_024), ("title", 512), ("created_at", 128), ("updated_at", 128),
                   ("conversation_type", 64), ("workspace_root", 4_096),
                   ("execution_cwd", 4_096), ("worktree_path", 4_096))
    return {**{key: public_text(getattr(summary, key), max_chars=maximum, single_line=maximum <= 1_024)
               for key, maximum in text_fields},
            "archived": summary.archived, "git_isolated": summary.git_isolated,
            "project_id": public_text(summary.project_id, max_chars=4_096) if summary.project_id is not None else None}


def conversation_message_text(message: dict[str, Any]) -> str:
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return ""
    if role == "user" and isinstance(message.get("display_content"), str):
        return message["display_content"]
    if role == "assistant" and isinstance(message.get("blocks"), list):
        return " ".join(block["content"] for block in message["blocks"]
                        if block.get("type") == "text" and block.get("source") != "runtime"
                        and isinstance(block.get("content"), str))
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(block["text"] for block in content
                        if block.get("type") in {"text", "input_text", "output_text"}
                        and isinstance(block.get("text"), str))
    return ""


def message_match(message: dict[str, Any], pattern: re.Pattern[str]) -> dict[str, str] | None:
    public_message_text = conversation_message_text(message)
    text = " ".join(public_text(public_message_text, max_chars=len(public_message_text)).split())
    match = pattern.search(text)
    if match is None:
        return None
    start, end = max(0, match.start() - 48), min(len(text), match.end() + 96)
    return {"message_id": message["id"],
            "snippet": ("... " if start else "") + text[start:end].strip() + (" ..." if end < len(text) else "")}
