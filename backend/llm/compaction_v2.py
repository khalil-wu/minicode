"""Source Responses V2 compaction requests and bounded retained input."""

from __future__ import annotations

from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
import json

from backend.llm.base import LLMMessage, SideQueryOptions
from backend.llm.native_compaction import NATIVE_COMPACTION_TYPE, native_compaction_windows, validate_compaction_window
from backend.mcp.result_projection import _truncate_text


def _tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def _part_tokens(part: dict, *, charge_images: bool) -> int:
    if part["type"] in {"input_text", "output_text"}:
        return _tokens(part["text"])
    if charge_images and part["type"] == "input_image":
        if "image_url" not in part:
            return 10_000 if part.get("detail") == "original" else (7373 + 3) // 4
        from backend.mcp.result_projection import mcp_output_visible_bytes

        return (mcp_output_visible_bytes([part]) + 3) // 4
    return 0


def _bounded_parts(content: list[dict], remaining: int, *, charge_images: bool):
    if not charge_images or not any(part["type"] == "input_image" for part in content):
        bounded = []
        for index, part in enumerate(content):
            if part["type"] in {"input_text", "output_text"}:
                if remaining <= 0:
                    continue
                text = _truncate_text(part["text"], "tokens", remaining)
                remaining = max(0, remaining - _tokens(part["text"]))
                if text:
                    bounded.append((index, {**part, "text": text}))
            else:
                bounded.append((index, part))
        return bounded
    # Retain later content, with an image and its harness labels as one unit.
    bounded = []
    end = len(content)
    while end:
        index = end - 1
        if content[index]["type"] == "input_text" and content[index]["text"] == "</image>" and index and content[index - 1]["type"] == "input_image":
            index -= 1
        if content[index]["type"] == "input_image":
            start = index
            if start and content[start - 1]["type"] == "input_text" and (
                    content[start - 1]["text"] == "<image>" or (
                        content[start - 1]["text"].startswith("<image name=") and content[start - 1]["text"].endswith(">"))):
                start -= 1
            cost = sum(_part_tokens(part, charge_images=True) for part in content[start:end])
            if cost <= remaining:
                bounded.extend((position, content[position]) for position in range(end - 1, start - 1, -1))
                remaining -= cost
            else:
                remaining = 0
            end = start
            continue
        part = content[index]
        if part["type"] in {"input_text", "output_text"}:
            if remaining:
                text = _truncate_text(part["text"], "tokens", remaining)
                remaining = max(0, remaining - _tokens(part["text"]))
                if text:
                    bounded.append((index, {**part, "text": text}))
        else:
            bounded.append((index, part))
        end -= 1
    bounded.reverse()
    return bounded


def _retained_agent_item(item: dict) -> bool:
    content = item["content"]
    first = content[0] if content else {}
    text = first.get("text", "")
    descendant = item["author"].startswith(item["recipient"] + "/")
    if text.startswith("Message Type: FINAL_ANSWER\n") or descendant and text.startswith(("Message Type: MESSAGE\n", "Message Type: CHANNEL_POST\n")):
        return False
    return _tokens(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) <= 10_000


def retained_input(adapter, messages: list[LLMMessage], *, max_tokens: int = 64_000, charge_images: bool = True) -> list[dict]:
    candidates = []
    for message in messages:
        windows = native_compaction_windows(message)
        if windows:
            candidates.extend(deepcopy(item) for window in windows for item in window["output"]
                if item.get("role") == "user" or item.get("type") == "agent_message" and _retained_agent_item(item))
        elif message.input_origin == "agent":
            candidates.extend(deepcopy(item) for item in message.provider_items
                if item.get("type") == "agent_message" and _retained_agent_item(item))
        elif message.role == "user" and (message.is_user_input or message.input_origin == "hook"
                or (message.accepted_input or {}).get("kind") in {"user.goal", "user.goal.omitted"}):
            accepted_id = (message.accepted_input or {}).get("model_item_id")
            retained_message = message if accepted_id is not None else replace(message,
                content=message.user_text_parts()[-1], runtime_context="")
            items = adapter._build_responses_input([retained_message])
            candidates.extend({"type": "message", **item} for item in items
                if accepted_id is None or item.get("id") == accepted_id)
    retained = []
    remaining = max_tokens
    for item in reversed(candidates):
        if remaining <= 0:
            break
        content = item["content"]
        if isinstance(content, str):
            content = [{"type": "input_text", "text": content}]
            item = {**item, "content": content}
        count = max(1, sum(_part_tokens(part, charge_images=charge_images) for part in content))
        if item.get("type") == "agent_message":
            count = max(1, _tokens(json.dumps(item, ensure_ascii=False, separators=(",", ":"))))
        if count <= remaining:
            retained.append(item)
            remaining -= count
            continue
        if item.get("type") == "agent_message":
            # Source only truncates Message content; attributed agent inputs
            # stay whole, including encrypted input that cannot be rewritten.
            continue
        bounded_parts = _bounded_parts(content, remaining, charge_images=charge_images)
        positions = [index for index, _ in bounded_parts]
        bounded = [part for _, part in bounded_parts]
        if bounded:
            item = {**item, "content": bounded}
            metadata = item.get("internal_chat_message_metadata_passthrough")
            if metadata is not None:
                item["internal_chat_message_metadata_passthrough"] = {
                    key: [value[index] if index < len(value) else
                        "unknown" if key == "content_item_kinds" else {"provenance": {"type": "unknown"}}
                        for index in positions] if key in {"content_item_kinds", "content_item_metadata"} else value
                    for key, value in metadata.items()}
            retained.append(item)
        remaining = 0
    retained.reverse()
    return retained


async def compact(adapter, messages, *, tools, metadata, turn_context, background=False):
    from backend.llm.openai_adapter import _responses_detach_provider_json, _get_attr_or_item, ResponsesStreamError

    adapter.validate_context(messages)
    settings = adapter._settings
    payload, _, _ = adapter._responses_request_payload(messages, tools, metadata)
    payload["input"].append({"type": "compaction_trigger"})
    # Compaction produces encrypted context, irrespective of the answer's schema.
    if "text" in payload:
        payload["text"] = {key: value for key, value in payload["text"].items() if key != "format"}
        if not payload["text"]:
            payload.pop("text")
    retained = retained_input(adapter, messages, charge_images=adapter._compaction_image_budget)
    origin = adapter.native_context_origin
    request_url = adapter._responses_url()
    request_headers = adapter._responses_headers()
    options = SideQueryOptions(operation="compact", enable_prompt_cache=False,
        max_retries=0 if background else 2, query_source="compact")

    async def complete(_messages, *, context):
        adapter.annotate_side_call(context, provider=settings.provider, model_id=settings.model)
        request_metadata = {**(metadata or {}), **context.request_metadata()}
        outputs = []
        completed = None
        prepared = await adapter._prepare_http_request(payload, metadata=request_metadata,
            base_headers=request_headers)
        stream = adapter._emit_responses_http_stream_events(payload, metadata=request_metadata,
            prepared=prepared, update_turn_state=not background, request_url=request_url)
        async with aclosing(stream):
            async for event in stream:
                kind = _get_attr_or_item(event, "type", "")
                if kind == "response.output_item.done":
                    item = _responses_detach_provider_json(event.item)
                    if item.get("type") == "compaction":
                        outputs.append(item)
                elif kind in {"response.completed", "response.incomplete"}:
                    response = event.response
                    adapter.record_non_stream_usage(_get_attr_or_item(response, "usage", None),
                        provider=settings.provider, model_id=settings.model, input_includes_cache_read=True, context=context)
                    if kind == "response.incomplete":
                        raise RuntimeError("Remote compaction V2 response is incomplete")
                    completed = response
                    break
                elif kind in {"error", "response.error", "response.failed"}:
                    response = _get_attr_or_item(event, "response", event)
                    raise ResponsesStreamError(kind, _get_attr_or_item(response, "error", response),
                        fallback="Remote compaction V2 failed")
        if completed is None:
            raise RuntimeError("Remote compaction V2 stream closed before response.completed")
        if len(outputs) != 1:
            raise ValueError(f"Remote compaction V2 expected exactly one compaction output item, got {len(outputs)}")
        window = validate_compaction_window({"type": NATIVE_COMPACTION_TYPE, "origin": origin,
            "model": settings.model, "response_id": _get_attr_or_item(completed, "id", ""),
            "output": [*retained, outputs[0]], "compaction_model_hash": settings.comp_hash})
        context.record["compaction_output_items"] = len(window["output"])
        return LLMMessage(role="assistant", provider_items=[window])

    return await adapter._run_auxiliary_call(messages, options=options, turn_context=turn_context, complete=complete)
