"""Request tracing, redaction and safe-summary helpers for the OpenAI adapter.

Extracted from ``backend/llm/openai_adapter.py`` so the wire adapter stays
focused on protocol while trace/redaction helpers are independently testable.
"""

from __future__ import annotations

from __future__ import annotations
from backend.agent.prompting import split_sys_prompt_prefix
from backend.config import LLMSettings
from backend.llm.base import LLMMessage
from typing import Any
import hashlib
import json


_PROVIDER_TIMELINE_LIMIT = 96


def _responses_reasoning_summary(
    settings: LLMSettings,
    *,
    model: str | None = None,
) -> str:
    selected_model = str(settings.model or "").strip()
    wire_model = str(model or selected_model).strip()
    # See _declared_reasoning_effort_levels_for_model: an unselected direct
    # adapter still owns its explicitly configured Responses summary policy.
    # Only a side-call for a distinct selected model must suppress it.
    if selected_model and wire_model != selected_model:
        return ""
    summary = (
        str(getattr(settings, "responses_reasoning_summary", "off") or "off")
        .strip()
        .lower()
    )
    # An unset/explicitly-disabled summary falls back to the model catalog's
    # declared default (e.g. gpt-5.x defaults to "auto"). Mirrors how
    # _chat_reasoning_effort consults default_reasoning_effort.
    if summary in {"none", "off", "false", "0", ""}:
        fallback = str(
            getattr(settings, "default_reasoning_summary", "") or ""
        ).strip().lower()
        if fallback in {"auto", "detailed"}:
            summary = fallback
        else:
            return ""
    if summary in {"auto", "detailed"}:
        return summary
    return "auto"



def _response_finish_reason(response_obj: Any) -> str:
    if response_obj is None:
        return ""
    details = getattr(response_obj, "incomplete_details", None)
    reason = getattr(details, "reason", "") if details is not None else ""
    if reason:
        return str(reason).strip()
    status = getattr(response_obj, "status", "")
    # Responses has no Chat-Completions-style ``stop`` finish reason.  Its
    # authoritative terminal semantic is the response status itself, including
    # the normal ``completed`` state.  Dropping only that state made successful
    # Codex turns look diagnostically incomplete (``Finish: unknown``) while
    # failed/incomplete states remained visible.
    return str(status or "").strip()


_RESPONSES_MAX_OUTPUT_REASONS = frozenset(
    {"length", "max_tokens", "max_output_tokens", "max_completion_tokens"}
)


_CHAT_TOOL_FINISH_REASONS = frozenset({"tool_calls", "function_call"})



def _short_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _json_fingerprint(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return _short_sha256(raw)


def _safe_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for tool in tools[:64]:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        function_def = tool.get("function")
        if not name and isinstance(function_def, dict):
            name = str(function_def.get("name") or "").strip()
        if name:
            names.append(name[:80])
    return sorted(dict.fromkeys(names))


def _safe_tool_schema_hashes(tools: list[dict[str, Any]]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for tool in tools[:64]:
        if not isinstance(tool, dict):
            continue
        function_def = tool.get("function")
        name = str(tool.get("name") or "").strip()
        if not name and isinstance(function_def, dict):
            name = str(function_def.get("name") or "").strip()
        if not name:
            name = str(tool.get("type") or "").strip()
        if not name:
            continue

        schema: Any = None
        if isinstance(function_def, dict):
            schema = function_def.get("parameters") or function_def.get("input_schema")
        if schema is None:
            schema = tool.get("parameters") or tool.get("input_schema")
        if schema is None:
            schema = {"type": tool.get("type", "")}
        hashes[name[:80]] = _json_fingerprint(schema)
    return dict(sorted(hashes.items()))


def _safe_tool_schema_size_summary(tools: list[dict[str, Any]]) -> dict[str, Any]:
    total_chars = 0
    largest: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        try:
            raw = json.dumps(
                tool,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            raw = repr(tool)
        chars = len(raw)
        total_chars += chars
        function_def = tool.get("function") if isinstance(tool, dict) else None
        name = ""
        if isinstance(tool, dict):
            name = str(tool.get("name") or "").strip()
            if not name and isinstance(function_def, dict):
                name = str(function_def.get("name") or "").strip()
            if not name:
                name = str(tool.get("type") or "").strip()
        largest.append(
            {"name": (name[:80] if name else f"tool_{index}"), "chars": chars}
        )
    largest.sort(key=lambda item: (-int(item["chars"]), str(item["name"])))
    return {"tools_chars": total_chars, "largest_tools": largest[:5]}


def _safe_input_item_content_hash(item: Any) -> str:
    if isinstance(item, dict):
        content = item.get("content")
        if content is None:
            content = item.get("output")
    else:
        content = getattr(item, "content", None)
    if content in (None, "", [], {}):
        return ""
    return _json_fingerprint(content)


def _safe_input_item_label(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, dict):
        item_type = (
            str(item.get("type") or item.get("role") or "message").strip() or "message"
        )
        role = str(item.get("role") or "").strip()
        name = str(item.get("name") or "").strip()
        if not name:
            function_def = item.get("function")
            if isinstance(function_def, dict):
                name = str(function_def.get("name") or "").strip()
        if not name:
            name = str(item.get("tool_name") or item.get("call_id") or "").strip()
    else:
        item_type = (
            str(
                getattr(item, "type", "") or getattr(item, "role", "") or "message"
            ).strip()
            or "message"
        )
        role = str(getattr(item, "role", "") or "").strip()
        name = str(getattr(item, "name", "") or "").strip()
    label: dict[str, Any] = {"index": index, "type": item_type[:80]}
    if role:
        label["role"] = role[:80]
    if name:
        label["name"] = name[:80]
    content_hash = _safe_input_item_content_hash(item)
    if content_hash:
        label["content_hash"] = content_hash
    return label


def _safe_input_size_summary(items: list[Any]) -> dict[str, Any]:
    total_chars = 0
    largest: list[dict[str, Any]] = []
    duplicate_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, item in enumerate(items or []):
        try:
            raw = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            raw = repr(item)
        chars = len(raw)
        total_chars += chars
        label = _safe_input_item_label(item, index)
        largest.append({**label, "chars": chars})
        content_hash = str(label.get("content_hash") or "")
        if content_hash:
            key = (
                str(label.get("type") or ""),
                str(label.get("role") or ""),
                content_hash,
            )
            group = duplicate_groups.setdefault(
                key,
                {
                    "type": key[0],
                    "role": key[1],
                    "content_hash": content_hash,
                    "count": 0,
                    "chars": 0,
                },
            )
            group["count"] = int(group["count"]) + 1
            group["chars"] = int(group["chars"]) + chars
    largest.sort(key=lambda item: (-int(item["chars"]), int(item["index"])))
    duplicates = [
        group for group in duplicate_groups.values() if int(group.get("count") or 0) > 1
    ]
    duplicates.sort(
        key=lambda item: (
            -int(item.get("chars") or 0),
            str(item.get("type") or ""),
            str(item.get("role") or ""),
        )
    )
    return {
        "input_chars": total_chars,
        "largest_input_items": largest[:5],
        "duplicate_input_content": duplicates[:5],
    }


def _safe_request_params(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload or {}
    params: dict[str, Any] = {}
    for key in (
        "stream",
        "store",
        "tool_choice",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "parallel_tool_calls",
        "prompt_cache_retention",
        "prompt_cache_options",
        "reasoning_effort",
        "seed",
    ):
        value = source.get(key)
        if isinstance(value, (str, bool, int, float)):
            params[key] = value
    prompt_cache_options = source.get("prompt_cache_options")
    if isinstance(prompt_cache_options, dict):
        mode = prompt_cache_options.get("mode")
        if isinstance(mode, str) and mode.strip():
            params["prompt_cache_options_mode"] = mode.strip()[:32]
    prompt_cache_breakpoint_count = _count_prompt_cache_breakpoints(source)
    if prompt_cache_breakpoint_count:
        params["prompt_cache_breakpoint_present"] = True
        params["prompt_cache_breakpoint_count"] = prompt_cache_breakpoint_count
    include = source.get("include")
    if isinstance(include, list):
        params["include"] = [
            str(item) for item in include[:16] if isinstance(item, str)
        ]
    stream_options = source.get("stream_options")
    if isinstance(stream_options, dict):
        params["stream_options"] = {
            key: value
            for key, value in stream_options.items()
            if isinstance(value, (str, bool, int, float))
        }
    reasoning = source.get("reasoning")
    if isinstance(reasoning, dict):
        params["reasoning"] = {
            key: value
            for key, value in reasoning.items()
            if key in {"effort", "summary", "content"}
            and isinstance(value, (str, bool, int, float))
        }
    for key in ("reasoning_summary", "reasoning_content"):
        value = source.get(key)
        if isinstance(value, (str, bool, int, float)):
            params[key] = value
    return params


def _safe_request_param_keys(payload: dict[str, Any] | None) -> list[str]:
    """Return provider request field presence without copying prompt content."""
    if not isinstance(payload, dict):
        return []
    safe_keys = {
        "client_metadata",
        "include",
        "max_completion_tokens",
        "max_output_tokens",
        "max_tokens",
        "metadata",
        "model",
        "parallel_tool_calls",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning",
        "reasoning_content",
        "reasoning_effort",
        "reasoning_summary",
        "store",
        "stream",
        "stream_options",
        "tool_choice",
    }
    return sorted(key for key in payload.keys() if str(key) in safe_keys)


def _count_prompt_cache_breakpoints(value: Any) -> int:
    """Count explicit OpenAI cache markers without retaining prompt content."""

    if isinstance(value, dict):
        own = 1 if "prompt_cache_breakpoint" in value else 0
        return own + sum(_count_prompt_cache_breakpoints(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_prompt_cache_breakpoints(item) for item in value)
    return 0



def _contains_turn_aborted_marker(value: Any) -> bool:
    if isinstance(value, str):
        return "<turn_aborted>" in value
    if isinstance(value, dict):
        return any(_contains_turn_aborted_marker(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_turn_aborted_marker(item) for item in value)
    content = getattr(value, "content", None)
    if content is not None:
        return _contains_turn_aborted_marker(content)
    return False


def _instruction_text_from_messages(messages: list[LLMMessage] | None) -> str:
    if not messages:
        return ""
    return "\n\n".join(
        _message_content_text(message.content).strip()
        for message in messages
        if _is_instruction_role(message.role)
        and _message_content_text(message.content).strip()
    )


def _message_content_text(content: Any) -> str:
    """Return text from local strings or OpenAI-style content part arrays."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "\n".join(parts)
        return ""
    return str(content or "")


def _is_instruction_role(role: Any) -> bool:
    return str(role or "").strip().lower() in {"system", "developer"}



def _safe_request_summary(
    *,
    model: str,
    wire_api: str,
    instructions: str = "",
    tools: list[dict[str, Any]] | None = None,
    request_metadata: dict[str, str] | None = None,
    input_items: list[dict[str, Any]] | None = None,
    messages: list[LLMMessage] | None = None,
    prompt_cache_key: str = "",
    request_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = request_metadata or {}
    summary: dict[str, Any] = {
        "model": model,
        "wire_api": wire_api,
        "metadata_keys": sorted(metadata.keys()),
        "prompt_cache_key_present": bool(prompt_cache_key),
        "prompt_cache_key_hash": _short_sha256(prompt_cache_key)
        if prompt_cache_key
        else "",
        "request_params": _safe_request_params(request_params),
        "request_param_keys": _safe_request_param_keys(request_params),
        "turn_aborted_marker_present": _contains_turn_aborted_marker(
            input_items if input_items is not None else messages or []
        ),
    }
    instruction_text = instructions or _instruction_text_from_messages(messages)
    stable_instruction_text = (
        split_sys_prompt_prefix(instruction_text).stable_prefix
        if instruction_text
        else ""
    )
    if instruction_text:
        summary["instructions_len"] = len(instruction_text)
        summary["instructions_hash"] = _short_sha256(
            stable_instruction_text or instruction_text
        )
        summary["instructions_full_hash"] = _short_sha256(instruction_text)
    else:
        summary["instructions_len"] = 0
        summary["instructions_hash"] = ""
        summary["instructions_full_hash"] = ""
    sent_instructions = ""
    if isinstance(request_params, dict) and isinstance(
        request_params.get("instructions"), str
    ):
        sent_instructions = str(request_params.get("instructions") or "")
    summary["instructions_sent_len"] = len(sent_instructions)
    tool_list = tools or []
    summary["tools_len"] = len(tool_list)
    summary["tools_hash"] = _json_fingerprint(tool_list) if tool_list else ""
    summary["tool_names"] = _safe_tool_names(tool_list)
    summary["tool_schema_hashes"] = _safe_tool_schema_hashes(tool_list)
    summary.update(_safe_tool_schema_size_summary(tool_list))

    input_counts: dict[str, int] = {}
    if input_items is not None:
        for item in input_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or item.get("role") or "message")
            input_counts[item_type] = input_counts.get(item_type, 0) + 1
        summary["input_items_len"] = len(input_items)
        summary["input_items_sent_len"] = len(input_items)
        summary["input_items_logical_len"] = len(input_items)
        summary.update(_safe_input_size_summary(input_items))
    elif messages is not None:
        for message in messages:
            role = str(getattr(message, "role", "") or "message")
            input_counts[role] = input_counts.get(role, 0) + 1
        summary["input_items_len"] = len(messages)
        summary["input_items_sent_len"] = len(messages)
        summary["input_items_logical_len"] = len(messages)
        summary.update(_safe_input_size_summary(list(messages)))
    else:
        summary["input_items_sent_len"] = 0
        summary["input_items_logical_len"] = 0
        summary.update(_safe_input_size_summary([]))
    summary["input_item_counts"] = input_counts
    return summary


def _provider_trace_safety(
    output_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "redacted_prompt": True,
        "has_encrypted_reasoning": any(
            bool(item.get("has_encrypted_content"))
            for item in (output_items or [])
            if isinstance(item, dict)
        ),
    }


def _safe_timeline_string(value: Any, *, limit: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if len(text) <= limit else f"{text[:limit]}..."


def _append_provider_timeline(
    timeline: list[dict[str, Any]],
    event: str,
    **fields: Any,
) -> None:
    """Append a bounded, content-free provider stream event summary."""
    if len(timeline) >= _PROVIDER_TIMELINE_LIMIT:
        if timeline and timeline[-1].get("event") == "timeline.truncated":
            timeline[-1]["omitted"] = int(timeline[-1].get("omitted") or 0) + 1
        else:
            timeline.append({"event": "timeline.truncated", "omitted": 1})
        return

    entry: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            entry[key] = value
        elif isinstance(value, int | float):
            entry[key] = value
        else:
            safe_value = _safe_timeline_string(value)
            if safe_value:
                entry[key] = safe_value
    timeline.append(entry)


def _response_timeline_fields(event_type: str, event: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for attr in ("output_index", "content_index", "sequence_number"):
        value = _get_attr_or_item(event, attr, None)
        if isinstance(value, int):
            fields[attr] = value

    response_id = str(_get_attr_or_item(event, "response_id", "") or "").strip()
    if response_id:
        fields["response_id_hash"] = _short_sha256(response_id)

    for attr in ("item_id", "call_id"):
        value = _safe_timeline_string(_get_attr_or_item(event, attr, ""))
        if value:
            fields[attr] = value

    item = _get_attr_or_item(event, "item", None)
    if item is not None:
        item_type = _safe_timeline_string(_get_attr_or_item(item, "type", ""))
        if item_type:
            fields["item_type"] = item_type
        for attr in ("id", "call_id", "name", "status", "phase"):
            value = _safe_timeline_string(_get_attr_or_item(item, attr, ""))
            if value:
                fields[attr if attr != "id" else "item_id"] = value

    delta = _get_attr_or_item(event, "delta", None)
    if isinstance(delta, str) and delta:
        fields["delta_chars"] = len(delta)

    text = _get_attr_or_item(event, "text", None)
    if isinstance(text, str) and text and event_type.endswith(".done"):
        fields["text_chars"] = len(text)

    arguments = _get_attr_or_item(event, "arguments", None)
    if isinstance(arguments, str) and arguments:
        fields["arguments_chars"] = len(arguments)

    code = _get_attr_or_item(event, "code", None)
    if isinstance(code, str) and code:
        fields["code_chars"] = len(code)

    annotations = _get_attr_or_item(event, "annotations", None)
    if isinstance(annotations, list):
        fields["annotation_count"] = len(annotations)

    response = _get_attr_or_item(event, "response", None)
    if response is not None:
        response_id = str(_get_attr_or_item(response, "id", "") or "").strip()
        if response_id:
            fields["response_id_hash"] = _short_sha256(response_id)
        status = _safe_timeline_string(_get_attr_or_item(response, "status", ""))
        if status:
            fields["status"] = status
        finish_reason = _safe_timeline_string(_response_finish_reason(response))
        if finish_reason:
            fields["finish_reason"] = finish_reason
        output = _get_attr_or_item(response, "output", None)
        if isinstance(output, list):
            fields["output_items_len"] = len(output)
        fields["usage_present"] = bool(_get_attr_or_item(response, "usage", None))

    return fields



def _responses_safe_provider_string(value: Any, *, limit: int = 20_000) -> str:
    text = str(value or "")
    return text[: max(1, limit)]


def _responses_safe_provider_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _responses_safe_provider_string(value)
    if isinstance(value, list):
        return [
            item
            for item in (
                _responses_safe_provider_value(child, depth=depth + 1)
                for child in value[:64]
            )
            if item is not None
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:64]:
            safe_key = str(key or "").strip()
            if not safe_key or len(safe_key) > 80:
                continue
            safe_child = _responses_safe_provider_value(child, depth=depth + 1)
            if safe_child is not None:
                result[safe_key] = safe_child
        return result
    raw_dict = getattr(value, "__dict__", None)
    if isinstance(raw_dict, dict):
        return _responses_safe_provider_value(raw_dict, depth=depth + 1)
    return None



def _get_attr_or_item(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)



