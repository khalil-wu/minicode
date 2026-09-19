"""Durable Responses compaction windows and their provider ownership."""
from __future__ import annotations

import hashlib
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.llm.base import LLMMessage

NATIVE_COMPACTION_TYPE = "responses_compaction"


def responses_context_origin(base_url: str) -> str:
    return hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest()


def validate_compaction_window(window: dict[str, Any]) -> dict[str, Any]:
    """Validate a network/disk boundary without filtering the returned window."""
    output = window.get("output")
    if not isinstance(window.get("origin"), str) or not window["origin"]:
        raise ValueError("Native compaction window has no provider origin")
    if not isinstance(output, list) or not output:
        raise ValueError("Native compaction returned an empty or invalid context window")
    if any(not isinstance(item, dict) or not isinstance(item.get("type"), str) for item in output):
        raise ValueError("Native compaction contains an invalid context item")
    compacted = [item for item in output if item["type"] == "compaction"]
    if not compacted or any(not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"] for item in compacted):
        raise ValueError("Native compaction window is missing its encrypted state")
    return window


def native_compaction_windows(message: LLMMessage) -> list[dict[str, Any]]:
    return [item for item in message.provider_items if item.get("type") == NATIVE_COMPACTION_TYPE]


def require_native_context_origin(messages: list[LLMMessage], origin: str = "") -> None:
    for message in messages:
        for window in native_compaction_windows(message):
            if window["origin"] != origin:
                raise ValueError(
                    "This context contains an encrypted Responses compaction window. "
                    "Continue with a Responses model on the original provider endpoint; "
                    "it cannot be converted to another provider protocol."
                )
