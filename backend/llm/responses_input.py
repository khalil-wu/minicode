"""Project host-admitted input identity into Responses messages."""
from __future__ import annotations

from typing import Any

from backend.llm.base import LLMMessage


def admitted_response_items(message: LLMMessage, item: dict[str, Any]) -> list[dict[str, Any]]:
    facts = message.accepted_input
    goal_kind = (facts or {}).get("kind")
    user_goal = goal_kind in {"user.goal", "user.goal.omitted"}
    if not (message.is_user_input or user_goal) or facts is None or "order" not in facts or "model_item_id" not in facts:
        return [item]

    # Runtime and task context keep their exact text and role, outside the
    # actual accepted input. A heartbeat remains one original InputText.
    text_parts = message.user_text_parts()
    runtime = text_parts[0] if len(text_parts) > 1 else ""
    body = text_parts[-1] if text_parts else ""
    prefix, suffix = runtime, ""
    content = item["content"]
    parts = [{"type": "input_text", "text": content}] if isinstance(content, str) else content
    if facts["single_text"]:
        start = facts["text_start"]
        end = start + facts["text_length"]
        prefix += body[:start]
        suffix = body[end:]
        parts = [{"type": "input_text", "text": body[start:end]}]
    elif runtime:
        parts = parts[1:]

    metadata: dict[str, Any] = {"turn_id": facts["turn_id"]}
    kinds = {"input_text": "user.text", "input_image": "user.image"}
    if all(part["type"] in kinds for part in parts):
        metadata["content_item_kinds"] = [kinds[part["type"]] for part in parts]
        if message.input_origin == "heartbeat" and facts["single_text"] and len(parts) == 1:
            metadata["content_item_kinds"] = ["user.heartbeat"]
        elif user_goal:
            metadata["content_item_kinds"] = [goal_kind]
    accepted = {"type": "message", "id": facts["model_item_id"], "role": "user", "content": parts,
        "internal_chat_message_metadata_passthrough": metadata}
    return [*([{"role": "user", "content": prefix}] if prefix else []), accepted,
        *([{"role": "user", "content": suffix}] if suffix else [])]


def prepare_content_item_kinds(items: list[dict[str, Any]], *, enabled: bool) -> list[dict[str, Any]]:
    if enabled:
        return items
    prepared = []
    for item in items:
        metadata = item.get("internal_chat_message_metadata_passthrough")
        if metadata is not None and "content_item_kinds" in metadata:
            item = dict(item)
            metadata = {key: value for key, value in metadata.items() if key != "content_item_kinds"}
            if metadata:
                item["internal_chat_message_metadata_passthrough"] = metadata
            else:
                item.pop("internal_chat_message_metadata_passthrough")
        prepared.append(item)
    return prepared
