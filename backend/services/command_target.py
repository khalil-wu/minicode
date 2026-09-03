"""Shared command-to-conversation target resolution."""

from __future__ import annotations

from typing import Any


def resolve_conversation_target(
    conversation_repo: Any,
    data: dict[str, Any],
    *,
    active_conversation_id: str = "",
) -> tuple[str, Any | None]:
    explicit_conversation_id = str(data.get("conversation_id") or "").strip()
    conversation_id = str(explicit_conversation_id or active_conversation_id or "").strip()
    if not conversation_id:
        return "", None
    return conversation_id, conversation_repo.get_conversation(conversation_id)
