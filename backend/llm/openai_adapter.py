
"""
OpenAI 适配器（DESIGN.md §一 LLM Adapter）。

支持两种 wire API：
  - "responses": OpenAI Responses API（client.responses.create）
  - "chat":      OpenAI Chat Completions API（client.chat.completions.create）

根据 config.wire_api 自动选择。
兼容 OpenAI 及所有兼容 API（Lucen、vLLM、LiteLLM、OpenRouter 等）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

from backend.config import LLMSettings
from backend.agent.prompting import split_sys_prompt_prefix
from backend.llm.errors import classify_llm_error
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    StreamEvent,
    StreamEventType,
    ToolCallDeltaEvent,
    ToolCallEvent,
    ToolCallStartEvent,
    UsageInfo,
    sanitize_llm_request_metadata,
)
from backend.llm.capabilities import ProviderCapabilities, capabilities_from_openai_settings
from backend.llm.openai_errors import (
    _clean_error_message,
    _error_status_code,
    _error_text,
    _is_prompt_cache_retention_unsupported_error,
    _is_reasoning_visibility_unsupported_error,
    _is_request_metadata_unsupported_error,
    _is_stream_options_unsupported_error,
    _is_transient_gateway_error,
    _retry_after_seconds,
)
from backend.llm.openai_payloads import (
    _normalize_schema_for_openai,
    _strip_metadata_request,
    _strip_openai_unsupported_fields,
    _strip_prompt_cache_retention_request,
    _strip_reasoning_visibility_request,
    _strip_request_metadata,
    _strip_responses_stateful_request,
)
from backend.llm.openai_streaming import _ReasoningSplitter, _ToolCallAccumulator, _splitter_events
from backend.llm.openai_usage import (
    _get_cached_prompt_tokens,
    _get_chat_prompt_tokens,
    _get_reasoning_output_tokens,
    _get_usage_field,
    _raw_text_delta_metadata,
    _raw_usage_metadata,
)
from backend.llm.reasoning_effort import normalize_reasoning_effort

logger = logging.getLogger(__name__)

_DELTA_DEBOUNCE_BYTES = 128

# ChatML/ChatGLM special tokens some gateways leak into content.
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]*\|>")


@dataclass
class _ResponsesContinuationState:
    previous_response_id: str = ""
    covered_item_fingerprints: list[str] = field(default_factory=list)
    covered_item_match_fingerprints: list[str] = field(default_factory=list)
    prompt_cache_key: str = ""
    instructions_hash: str = ""
    instructions_full_hash: str = ""
    tools_hash: str = ""


_MAX_RESPONSES_CONTINUATION_STATES = 128
_RESPONSES_RUNTIME_BLOCK_RE = re.compile(
    r"\A(?:\s*(?:"
    r"<permissions instructions>[\s\S]*?</permissions instructions>|"
    r"<environment_context>[\s\S]*?</environment_context>|"
    r"<collaboration_mode>[\s\S]*?</collaboration_mode>|"
    r"<agent_mode>[\s\S]*?</agent_mode>|"
    r"<turn_aborted>[\s\S]*?</turn_aborted>|"
    r"<tool_runtime_context>[\s\S]*?</tool_runtime_context>|"
    r"Current time:[^\n]*(?:\n|$)"
    r")\s*)+",
    re.IGNORECASE,
)
_RESPONSES_RUNTIME_CONTEXT_OMITTED_PLACEHOLDER = "[runtime context omitted]"


def _strip_special_tokens(text: str) -> str:
    """Remove leaked <|im_start|>/<|im_end|>/<|endoftext|>/<|user|>/... markers."""
    if not text or "<|" not in text:
        return text
    return _SPECIAL_TOKEN_RE.sub("", text)


def _safe_string_list(value: Any, *, max_items: int, max_item_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[: max(0, max_items)]:
        text = str(item or "").strip()
        if not text:
            continue
        result.append(text[: max(1, max_item_chars)])
    return result


def _chat_max_tokens_kwargs(settings: LLMSettings) -> dict[str, int]:
    """Return an explicit Chat Completions output cap only when configured.

    A value <= 0 means "let the provider/model default decide". This keeps
    MiniCode from adding a hidden output ceiling while still allowing users who
    deliberately configure a cap to send one.
    """
    try:
        max_tokens = int(settings.max_tokens)
    except (TypeError, ValueError):
        return {}
    return {"max_tokens": max_tokens} if max_tokens > 0 else {}


def _prefetch_tool_call_event(tool_calls: list[ToolCallEvent]) -> StreamEvent | None:
    if not tool_calls:
        return None
    return StreamEvent(
        type=StreamEventType.TOOL_CALL,
        tool_calls=list(tool_calls),
        tool_calls_final=False,
    )

_ADAPTER_RETRY_DELAY_SECONDS = 0.8
_PROVIDER_ERROR_BODY_LOG_LIMIT = 1200
_PROVIDER_TIMELINE_LIMIT = 96
_FUNCTION_CALL_RESULT_RE = re.compile(
    r'^<function_call_result\s+status="(?P<status>[^"]+)"\s+call_id="(?P<call_id>[^"]*)">\n'
    r"(?P<output>.*)\n</function_call_result>\s*$",
    re.DOTALL,
)


def _provider_host(base_url: str) -> str:
    parsed = urlparse(base_url or "https://api.openai.com/v1")
    return parsed.netloc or parsed.path.split("/")[0] or "unknown"


def _responses_reasoning_effort(settings: LLMSettings, *, has_tools: bool = False) -> str:
    return normalize_reasoning_effort(
        settings.model,
        settings.wire_api,
        settings.reasoning_effort,
        settings.reasoning_effort_levels,
    )


def _responses_reasoning_summary(settings: LLMSettings) -> str:
    summary = str(getattr(settings, "responses_reasoning_summary", "off") or "off").strip().lower()
    if summary in {"none", "off", "false", "0"}:
        return ""
    if summary in {"auto", "detailed"}:
        return summary
    return "auto"


def _prompt_cache_retention_request(settings: LLMSettings) -> str:
    retention = str(getattr(settings, "prompt_cache_retention", "") or "").strip().lower()
    return retention if retention in {"24h", "in_memory"} else ""


def _responses_reasoning_request(settings: LLMSettings, *, has_tools: bool = False) -> dict[str, Any]:
    """Request provider-visible reasoning summaries whenever the gateway allows it."""
    reasoning: dict[str, Any] = {}
    summary = _responses_reasoning_summary(settings)
    if summary:
        reasoning["summary"] = summary
    effort = _responses_reasoning_effort(settings, has_tools=has_tools)
    if effort:
        reasoning["effort"] = effort
    return reasoning


def _responses_include_request(
    settings: LLMSettings,
    *,
    has_tools: bool = False,
    reasoning_request: dict[str, Any] | None = None,
    stateful_store: bool = False,
) -> list[str]:
    """Return optional Responses include fields needed for stateless continuity.

    OpenAI's Responses state can be chained with previous_response_id. When that
    is unavailable (store disabled, unsupported gateway, or stateless mode), the
    documented fallback is to request encrypted reasoning and pass it back as an
    input item on the next turn. Request it for reasoning/tool turns; compatible
    gateways that do not support it are retried without the optional include.
    """
    if stateful_store:
        return []
    reasoning_effort = _responses_reasoning_effort(settings, has_tools=has_tools)
    if reasoning_request or has_tools or reasoning_effort:
        return ["reasoning.encrypted_content"]
    return []


def _add_stateless_reasoning_include_if_needed(
    payload: dict[str, Any],
    settings: LLMSettings,
    *,
    has_tools: bool | None = None,
    reasoning_visibility_supported: bool = True,
) -> None:
    """Add encrypted reasoning include after downgrading a stateful request.

    The normal stateful path uses provider-side response state, so it does not
    need encrypted reasoning echoed into the response. If the request is retried
    without ``store``/``previous_response_id``, add the include back so stateless
    history can preserve reasoning/tool state on later turns.
    """
    if not reasoning_visibility_supported or "include" in payload:
        return
    reasoning = payload.get("reasoning")
    reasoning_request = reasoning if isinstance(reasoning, dict) else None
    inferred_has_tools = bool(payload.get("tools")) if has_tools is None else has_tools
    include_request = _responses_include_request(
        settings,
        has_tools=inferred_has_tools,
        reasoning_request=reasoning_request,
        stateful_store=False,
    )
    if include_request:
        payload["include"] = include_request


def _chat_reasoning_visibility_request(settings: LLMSettings) -> dict[str, Any]:
    """Best-effort hints for OpenAI-compatible chat gateways with reasoning deltas."""
    effort = normalize_reasoning_effort(
        settings.model,
        settings.wire_api,
        settings.reasoning_effort,
        settings.reasoning_effort_levels,
    )
    summary = _responses_reasoning_summary(settings)
    payload: dict[str, Any] = {}
    if summary:
        payload["reasoning_summary"] = summary
        payload["reasoning_content"] = True
    if effort:
        reasoning: dict[str, Any] = {"effort": effort}
        if summary:
            reasoning["summary"] = summary
            reasoning["content"] = True
        payload["reasoning"] = reasoning
    return payload


def _response_finish_reason(response_obj: Any) -> str:
    if response_obj is None:
        return ""
    details = getattr(response_obj, "incomplete_details", None)
    reason = getattr(details, "reason", "") if details is not None else ""
    if reason:
        return str(reason)
    status = getattr(response_obj, "status", "")
    if status and str(status) != "completed":
        return str(status)
    return ""


def _extract_url_citations(event: Any) -> list[dict[str, Any]]:
    """Extract url_citation annotations from a response.output_text.done event.

    The OpenAI Responses API attaches citation annotations to the completed
    output text. Each url_citation carries a URL, title, and optional
    start/end indices into the text. We normalize them into a flat list of
    dicts so the frontend can merge them with web-search sources.
    """
    annotations = getattr(event, "annotations", None)
    if not annotations:
        return []
    citations: list[dict[str, Any]] = []
    for ann in annotations:
        ann_type = getattr(ann, "type", "")
        if ann_type != "url_citation":
            continue
        url = getattr(ann, "url", "") or ""
        title = getattr(ann, "title", "") or ""
        start = getattr(ann, "start_index", None)
        end = getattr(ann, "end_index", None)
        if not url:
            continue
        entry: dict[str, Any] = {"url": str(url), "title": str(title)}
        if start is not None and end is not None:
            entry["range"] = [int(start), int(end)]
        citations.append(entry)
    return citations


def _provider_response_body(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    for owner in (response, exc):
        if owner is None:
            continue
        for attr in ("text", "body", "content"):
            value = getattr(owner, attr, None)
            if value:
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                if isinstance(value, (dict, list)):
                    try:
                        return json.dumps(value, ensure_ascii=False)
                    except (TypeError, ValueError):
                        return str(value)
                return str(value)
    return ""


def _provider_error_fields(exc: Exception, body: str) -> tuple[str, str]:
    code = str(getattr(exc, "code", "") or "")
    error_type = str(getattr(exc, "type", "") or "")
    if code and error_type:
        return code, error_type
    try:
        payload = json.loads(body) if body else {}
    except (TypeError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        error_obj = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        if isinstance(error_obj, dict):
            code = code or str(error_obj.get("code") or "")
            error_type = error_type or str(error_obj.get("type") or "")
    return code, error_type


def _truncate_provider_body(body: str) -> str:
    compact = re.sub(r"\s+", " ", body or "").strip()
    if len(compact) > _PROVIDER_ERROR_BODY_LOG_LIMIT:
        return compact[:_PROVIDER_ERROR_BODY_LOG_LIMIT] + "..."
    return compact


def _log_chat_provider_error(settings: LLMSettings, context: str, exc: Exception) -> None:
    body = _provider_response_body(exc)
    code, error_type = _provider_error_fields(exc, body)
    logger.error(
        "%s failed provider_host=%s model=%s wire_api=%s status=%s provider_error_type=%s provider_error_code=%s response_body=%s",
        context,
        _provider_host(settings.base_url),
        settings.model,
        settings.wire_api,
        _error_status_code(exc) or "",
        error_type or "",
        code or "",
        _truncate_provider_body(body),
        exc_info=True,
    )


def _provider_error_hint(exc: Exception) -> str:
    classification = classify_llm_error(exc)
    status = _error_status_code(exc)
    body = _provider_response_body(exc)
    code, error_type = _provider_error_fields(exc, body)
    parts = []
    if classification.provider_error_type != "unknown":
        parts.append(f"provider_error_type={classification.provider_error_type}")
    if status is not None:
        parts.append(f"status={status}")
    if code:
        parts.append(f"provider_error_code={code}")
    if error_type:
        parts.append(f"provider_error_schema_type={error_type}")
    return " ".join(parts)


def _adapter_error_content(prefix: str, exc: Exception) -> str:
    hint = _provider_error_hint(exc)
    suffix = f" ({hint})" if hint else ""
    return f"{prefix}: {_clean_error_message(exc)}{suffix}"


def _adapter_error_raw(exc: Exception, provider: str) -> dict[str, Any]:
    classification = classify_llm_error(exc)
    raw: dict[str, Any] = {
        "provider": provider,
        "provider_error_type": classification.provider_error_type,
        "error_type": classification.error_type,
    }
    retry_after = _retry_after_seconds(exc)
    if retry_after > 0:
        raw["retry_after_seconds"] = retry_after
    return raw


def _responses_function_call_output_item(message: LLMMessage) -> dict[str, Any]:
    """Convert a tool message to a Responses function_call_output item.

    ContextBuilder keeps status in a provider-neutral XML wrapper for text-only
    providers. Responses has a native status field, so unwrap that local wrapper
    before sending the provider payload.
    """
    call_id = str(message.tool_call_id or "")
    output = str(message.content or "")
    status = ""
    match = _FUNCTION_CALL_RESULT_RE.match(output)
    if match:
        wrapped_call_id = str(match.group("call_id") or "")
        if not call_id or wrapped_call_id == call_id:
            output = match.group("output")
            raw_status = str(match.group("status") or "").strip().lower()
            if raw_status == "completed":
                status = "completed"
            elif raw_status in {"error", "failed", "blocked", "incomplete"}:
                status = "incomplete"
            elif raw_status in {"in_progress", "completed", "incomplete"}:
                status = raw_status
    payload: dict[str, Any] = {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }
    if status:
        payload["status"] = status
    return payload


def _short_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _json_fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
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
        largest.append({"name": (name[:80] if name else f"tool_{index}"), "chars": chars})
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
        item_type = str(item.get("type") or item.get("role") or "message").strip() or "message"
        role = str(item.get("role") or "").strip()
        name = str(item.get("name") or "").strip()
        if not name:
            function_def = item.get("function")
            if isinstance(function_def, dict):
                name = str(function_def.get("name") or "").strip()
        if not name:
            name = str(item.get("tool_name") or item.get("call_id") or "").strip()
    else:
        item_type = str(getattr(item, "type", "") or getattr(item, "role", "") or "message").strip() or "message"
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
    duplicates = [group for group in duplicate_groups.values() if int(group.get("count") or 0) > 1]
    duplicates.sort(key=lambda item: (-int(item.get("chars") or 0), str(item.get("type") or ""), str(item.get("role") or "")))
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
        "parallel_tool_calls",
        "prompt_cache_retention",
        "seed",
    ):
        value = source.get(key)
        if isinstance(value, (str, bool, int, float)):
            params[key] = value
    include = source.get("include")
    if isinstance(include, list):
        params["include"] = [
            str(item)
            for item in include[:16]
            if isinstance(item, str)
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
        "include",
        "max_tokens",
        "metadata",
        "model",
        "parallel_tool_calls",
        "previous_response_id",
        "prompt_cache_key",
        "prompt_cache_retention",
        "reasoning",
        "reasoning_content",
        "reasoning_summary",
        "store",
        "stream",
        "stream_options",
        "tool_choice",
    }
    return sorted(key for key in payload.keys() if str(key) in safe_keys)


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
        if _is_instruction_role(message.role) and _message_content_text(message.content).strip()
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


def _openai_chat_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    """Build Chat Completions messages without replayed instruction snapshots."""
    chat_messages: list[dict[str, Any]] = []
    leading_instructions: list[str] = []
    seen_instruction_blocks: set[str] = set()
    accepting_leading_instructions = True

    for message in messages:
        role = str(message.role or "").strip().lower()
        if _is_instruction_role(role):
            if not accepting_leading_instructions:
                continue
            content = _message_content_text(message.content).strip()
            if not content or content in seen_instruction_blocks:
                continue
            seen_instruction_blocks.add(content)
            leading_instructions.append(content)
            continue

        accepting_leading_instructions = False
        chat_messages.append(message.to_openai_message())

    if leading_instructions:
        chat_messages.insert(0, {"role": "system", "content": "\n\n".join(leading_instructions)})
    return chat_messages


def _instruction_text_from_chat_payload(messages: list[dict[str, Any]] | None) -> str:
    if not messages:
        return ""
    return "\n\n".join(
        _message_content_text(message.get("content")).strip()
        for message in messages
        if isinstance(message, dict)
        and _is_instruction_role(message.get("role"))
        and _message_content_text(message.get("content")).strip()
    )


def _chat_payload_input_items(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not messages:
        return []
    return [
        message
        for message in messages
        if isinstance(message, dict) and not _is_instruction_role(message.get("role"))
    ]


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
    previous_response_id: str = "",
    request_params: dict[str, Any] | None = None,
    input_items_omitted_by_continuation: int = 0,
) -> dict[str, Any]:
    metadata = request_metadata or {}
    summary: dict[str, Any] = {
        "model": model,
        "wire_api": wire_api,
        "metadata_keys": sorted(metadata.keys()),
        "prompt_cache_key_present": bool(prompt_cache_key),
        "prompt_cache_key_hash": _short_sha256(prompt_cache_key) if prompt_cache_key else "",
        "previous_response_id_present": bool(previous_response_id),
        "previous_response_id_hash": _short_sha256(previous_response_id) if previous_response_id else "",
        "request_params": _safe_request_params(request_params),
        "request_param_keys": _safe_request_param_keys(request_params),
        "turn_aborted_marker_present": _contains_turn_aborted_marker(input_items if input_items is not None else messages or []),
    }
    instruction_text = instructions or _instruction_text_from_messages(messages)
    stable_instruction_text = (
        split_sys_prompt_prefix(instruction_text).stable_prefix
        if instruction_text
        else ""
    )
    if instruction_text:
        summary["instructions_len"] = len(instruction_text)
        summary["instructions_hash"] = _short_sha256(stable_instruction_text or instruction_text)
        summary["instructions_full_hash"] = _short_sha256(instruction_text)
    else:
        summary["instructions_len"] = 0
        summary["instructions_hash"] = ""
        summary["instructions_full_hash"] = ""
    sent_instructions = ""
    if isinstance(request_params, dict) and isinstance(request_params.get("instructions"), str):
        sent_instructions = str(request_params.get("instructions") or "")
    summary["instructions_sent_len"] = len(sent_instructions)
    summary["instructions_omitted_by_continuation"] = bool(
        previous_response_id and instruction_text and not sent_instructions
    )

    tool_list = tools or []
    summary["tools_len"] = len(tool_list)
    summary["tools_hash"] = _json_fingerprint(tool_list) if tool_list else ""
    summary["tool_names"] = _safe_tool_names(tool_list)
    summary["tool_schema_hashes"] = _safe_tool_schema_hashes(tool_list)
    summary.update(_safe_tool_schema_size_summary(tool_list))

    input_counts: dict[str, int] = {}
    omitted_by_continuation = max(0, int(input_items_omitted_by_continuation or 0))
    if input_items is not None:
        for item in input_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or item.get("role") or "message")
            input_counts[item_type] = input_counts.get(item_type, 0) + 1
        summary["input_items_len"] = len(input_items)
        summary["input_items_sent_len"] = len(input_items)
        summary["input_items_omitted_by_continuation"] = omitted_by_continuation
        summary["input_items_logical_len"] = len(input_items) + omitted_by_continuation
        summary.update(_safe_input_size_summary(input_items))
    elif messages is not None:
        for message in messages:
            role = str(getattr(message, "role", "") or "message")
            input_counts[role] = input_counts.get(role, 0) + 1
        summary["input_items_len"] = len(messages)
        summary["input_items_sent_len"] = len(messages)
        summary["input_items_omitted_by_continuation"] = omitted_by_continuation
        summary["input_items_logical_len"] = len(messages) + omitted_by_continuation
        summary.update(_safe_input_size_summary(list(messages)))
    else:
        summary["input_items_sent_len"] = 0
        summary["input_items_omitted_by_continuation"] = omitted_by_continuation
        summary["input_items_logical_len"] = omitted_by_continuation
        summary.update(_safe_input_size_summary([]))
    summary["input_item_counts"] = input_counts
    return summary


def _provider_trace_safety(output_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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


def _split_responses_instructions(messages: list[LLMMessage]) -> tuple[str, list[LLMMessage]]:
    """Split leading system/developer instructions from Responses input.

    Restored transcripts may contain historical system/developer snapshots after
    user turns. Those are not current instructions and must not be re-promoted
    into the next request's top-level ``instructions``.
    """
    instructions: list[str] = []
    seen_instruction_blocks: set[str] = set()
    input_messages: list[LLMMessage] = []
    accepting_leading_instructions = True
    for message in messages:
        role = str(message.role or "").strip().lower()
        if _is_instruction_role(role):
            if not accepting_leading_instructions:
                continue
            content = _message_content_text(message.content).strip()
            if content and content not in seen_instruction_blocks:
                seen_instruction_blocks.add(content)
                instructions.append(content)
            continue
        accepting_leading_instructions = False
        input_messages.append(message)
    return "\n\n".join(instructions), input_messages


def _responses_prompt_cache_key(
    settings: LLMSettings,
    instructions: str,
    request_metadata: dict[str, str] | None = None,
) -> str:
    """Build a stable, non-reversible cache routing key for Responses requests."""
    if not instructions:
        return ""
    # Key the routing hash on the byte-stable system prefix only, not the
    # concatenated instructions. The stable prefix (everything before
    # __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__) is identical across turns; the dynamic
    # suffix (workspace summary, skills, memory) churns. Hashing only the stable
    # prefix keeps the routing key — and thus the provider-side cache slot —
    # stable across dynamic-context changes, mirroring what the Anthropic path
    # does with cache_control on the split stable block. The key intentionally
    # avoids per-conversation/workspace metadata: OpenAI combines this value with
    # the real prompt prefix for routing, so adding conversation ids only splits
    # identical stable prefixes across smaller cache pools. Stateful continuation
    # remains conversation-scoped in _responses_continuation_state_key.
    # When no boundary marker is present (raw custom prompt),
    # split_sys_prompt_prefix returns the full text as the stable prefix,
    # preserving prior behavior.
    stable_prefix = split_sys_prompt_prefix(instructions).stable_prefix if instructions else ""
    payload = {
        "provider_host": _provider_host(settings.base_url),
        "model": settings.model,
        "instructions_sha256": hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest() if stable_prefix else "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"minicode-{digest[:48]}"


def _responses_stateful_instructions_hash(instructions: str) -> str:
    """Hash the durable instruction prefix that must match for continuation.

    MiniCode deliberately places workspace, skill, memory, and other per-turn
    context after ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY``. Those bytes should route
    to the same prompt-cache slot and should not by themselves disable
    ``previous_response_id``. The full current instructions are still sent on
    each request; this hash only decides whether the provider-side conversation
    state is safe to reuse.
    """
    if not instructions:
        return ""
    stable_prefix = split_sys_prompt_prefix(instructions).stable_prefix or instructions
    return _short_sha256(stable_prefix)


def _responses_stateful_full_instructions_hash(instructions: str) -> str:
    return _short_sha256(instructions) if instructions else ""


def _responses_stateful_continuation_enabled(settings: LLMSettings) -> bool:
    return bool(getattr(settings, "responses_stateful_continuation", False))


def _responses_continuation_state_key(
    settings: LLMSettings,
    request_metadata: dict[str, str],
    prompt_cache_key: str,
) -> str:
    conversation_id = str(request_metadata.get("conversation_id") or "").strip()
    session_id = str(
        request_metadata.get("minicode_session_id")
        or request_metadata.get("minicode_app_session_id")
        or ""
    ).strip()
    cwd = str(request_metadata.get("cwd") or "").strip()
    if not (conversation_id or session_id):
        return ""
    payload = {
        "provider_host": _provider_host(settings.base_url),
        "model": settings.model,
        "conversation_id": conversation_id,
        "session_id": session_id,
        "cwd": cwd,
        "prompt_cache_key": prompt_cache_key,
    }
    return _json_fingerprint(payload)


def _responses_input_item_fingerprint(item: dict[str, Any]) -> str:
    return _json_fingerprint(item)


def _responses_input_fingerprints(items: list[dict[str, Any]]) -> list[str]:
    return [_responses_input_item_fingerprint(item) for item in items]


def _responses_strip_runtime_blocks(text: str) -> str:
    original = str(text or "")
    stripped = _RESPONSES_RUNTIME_BLOCK_RE.sub("", original, count=1).lstrip()
    if stripped == original:
        return original
    return stripped or _RESPONSES_RUNTIME_CONTEXT_OMITTED_PLACEHOLDER


def _responses_match_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize cache-churning user runtime wrappers for continuation matching.

    The provider-side response state still contains the original item bytes; this
    fingerprint only decides whether local history covers the same logical prior
    user turn after ContextBuilder has stripped old environment/collaboration
    wrappers from replayed history. Function calls are matched by their stable
    call contract because provider-native output ids are not always present in
    local replayed history.
    """
    if item.get("type") == "function_call":
        call_id = str(item.get("call_id") or item.get("id") or "").strip()
        return {
            "type": "function_call",
            "call_id": call_id,
            "name": str(item.get("name") or "").strip(),
            "arguments": _responses_normalize_function_arguments(item.get("arguments", "")),
        }
    if item.get("role") != "user":
        return item
    content = item.get("content")
    if isinstance(content, str):
        normalized = _responses_strip_runtime_blocks(content)
        return {**item, "content": normalized}
    if isinstance(content, list):
        changed = False
        parts: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "input_text" and isinstance(part.get("text"), str):
                normalized = _responses_strip_runtime_blocks(str(part.get("text") or ""))
                changed = changed or normalized != part.get("text")
                parts.append({**part, "text": normalized})
            else:
                parts.append(part)
        if changed:
            return {**item, "content": parts}
    return item


def _responses_input_match_fingerprints(items: list[dict[str, Any]]) -> list[str]:
    return [_json_fingerprint(_responses_match_item(item)) for item in items]


def _responses_normalize_function_arguments(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return str(value or "")[:20_000]
    else:
        parsed = value if value is not None else {}
    try:
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value or "")[:20_000]


def _responses_function_call_input_item(tc: ToolCallEvent) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": tc.id,
        "call_id": tc.id,
        "name": tc.name,
        "arguments": _responses_normalize_function_arguments(tc.arguments),
    }


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
            for item in (_responses_safe_provider_value(child, depth=depth + 1) for child in value[:64])
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


def _responses_provider_item_from_output(item: Any) -> dict[str, Any] | None:
    """Return opaque Responses output items safe to round-trip as input.

    This preserves encrypted reasoning state for stateless / ZDR-compatible
    operation and native function_call ids for the immediate tool-result turn.
    Message text is handled separately through normal assistant history.
    """
    item_type = str(_get_attr_or_item(item, "type", "") or "").strip()
    item_id = str(_get_attr_or_item(item, "id", "") or "").strip()
    status = str(_get_attr_or_item(item, "status", "") or "").strip()
    if item_type == "reasoning":
        encrypted_content = _get_attr_or_item(item, "encrypted_content", "")
        if not encrypted_content:
            encrypted_content = _get_attr_or_item(item, "encrypted_content_delta", "")
        summary = _get_attr_or_item(item, "summary", []) or []
        if not encrypted_content and not summary:
            return None
        result: dict[str, Any] = {"type": "reasoning"}
        if item_id:
            result["id"] = item_id
        if status:
            result["status"] = status
        if encrypted_content:
            result["encrypted_content"] = _responses_safe_provider_string(encrypted_content, limit=120_000)
        safe_summary = _responses_safe_provider_value(summary)
        if isinstance(safe_summary, list) and safe_summary:
            result["summary"] = safe_summary[:16]
        return result
    if item_type == "function_call":
        call_id = str(_get_attr_or_item(item, "call_id", "") or item_id).strip()
        name = str(_get_attr_or_item(item, "name", "") or "").strip()
        arguments = _get_attr_or_item(item, "arguments", "")
        if not call_id or not name or not isinstance(arguments, str):
            return None
        result = {
            "type": "function_call",
            "id": item_id or call_id,
            "call_id": call_id,
            "name": name,
            "arguments": _responses_normalize_function_arguments(arguments),
        }
        if status:
            result["status"] = status
        return result
    return None


def _responses_provider_items_from_response(response: Any) -> list[dict[str, Any]]:
    output = _get_attr_or_item(response, "output", []) or []
    if not isinstance(output, list):
        return []
    items: list[dict[str, Any]] = []
    for item in output[:64]:
        provider_item = _responses_provider_item_from_output(item)
        if provider_item is not None:
            items.append(provider_item)
    return items


def _responses_provider_items_metadata(provider_items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    encrypted_reasoning = 0
    hashes: list[str] = []
    for item in provider_items[:64]:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if not item_type:
            continue
        counts[item_type] = counts.get(item_type, 0) + 1
        if item_type == "reasoning" and item.get("encrypted_content"):
            encrypted_reasoning += 1
            hashes.append(_short_sha256(str(item.get("encrypted_content") or "")))
    return {
        "count": len(provider_items),
        "item_counts": counts,
        "encrypted_reasoning_items": encrypted_reasoning,
        "encrypted_reasoning_hashes": hashes[:8],
    }


def _responses_message_phase_from_response(response: Any) -> str:
    output = _get_attr_or_item(response, "output", []) or []
    if not isinstance(output, list):
        return ""
    for item in output:
        if str(_get_attr_or_item(item, "type", "") or "") != "message":
            continue
        phase = str(_get_attr_or_item(item, "phase", "") or "").strip()
        if phase:
            return phase[:40]
    return ""


def _responses_message_text_from_item(item: Any) -> str:
    content = _get_attr_or_item(item, "content", []) or []
    if not isinstance(content, list):
        return ""
    text_parts: list[str] = []
    for part in content:
        part_type = str(_get_attr_or_item(part, "type", "") or "")
        if part_type not in {"output_text", "text"}:
            continue
        text = _get_attr_or_item(part, "text", "")
        if isinstance(text, str) and text:
            text_parts.append(text)
    return "".join(text_parts)


def _responses_local_output_items_from_response(
    response: Any,
    *,
    fallback_text: str = "",
) -> list[dict[str, Any]]:
    """Return local-history-equivalent items represented by a Responses output."""
    output = _get_attr_or_item(response, "output", []) or []
    if not isinstance(output, list):
        output = []
    items: list[dict[str, Any]] = []
    for item in output:
        item_type = str(_get_attr_or_item(item, "type", "") or "")
        if item_type == "message":
            text = _responses_message_text_from_item(item)
            if text:
                assistant_item: dict[str, Any] = {"role": "assistant", "content": text}
                phase = str(_get_attr_or_item(item, "phase", "") or "").strip()
                if phase:
                    assistant_item["phase"] = phase[:40]
                items.append(assistant_item)
        elif item_type == "function_call":
            call_id = str(_get_attr_or_item(item, "call_id", "") or _get_attr_or_item(item, "id", "") or "").strip()
            name = str(_get_attr_or_item(item, "name", "") or "").strip()
            arguments = _get_attr_or_item(item, "arguments", "")
            if call_id and name and isinstance(arguments, str):
                items.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": name,
                        "arguments": _responses_normalize_function_arguments(arguments),
                    }
                )
    if not items and fallback_text:
        items.append({"role": "assistant", "content": fallback_text})
    return items


def _responses_completed_turn_replay_items(
    *,
    local_items: list[dict[str, Any]],
    provider_items: list[dict[str, Any]],
    fallback_text: str = "",
    pending_tool_calls: list[ToolCallEvent] | None = None,
) -> list[dict[str, Any]]:
    """Return the exact input-item sequence this assistant turn will replay."""
    reasoning_items = [
        dict(item)
        for item in provider_items
        if isinstance(item, dict) and item.get("type") == "reasoning"
    ]
    provider_function_items = [
        dict(item)
        for item in provider_items
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    assistant_items = [
        dict(item)
        for item in local_items
        if isinstance(item, dict) and item.get("role") == "assistant" and item.get("content")
    ]
    if fallback_text and not assistant_items:
        assistant_items = [{"role": "assistant", "content": fallback_text}]

    function_items: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()

    def add_function_item(item: dict[str, Any]) -> None:
        if item.get("type") != "function_call":
            return
        call_id = str(item.get("call_id") or item.get("id") or "").strip()
        if call_id and call_id in seen_call_ids:
            return
        if call_id:
            seen_call_ids.add(call_id)
        function_items.append(dict(item))

    for item in provider_function_items:
        add_function_item(item)
    for item in local_items:
        if isinstance(item, dict):
            add_function_item(item)
    for tool_call in pending_tool_calls or []:
        if tool_call.id in seen_call_ids:
            continue
        seen_call_ids.add(tool_call.id)
        function_items.append(_responses_function_call_input_item(tool_call))

    return [*reasoning_items, *assistant_items, *function_items]


def _responses_stateful_unsupported_error(exc: Exception) -> bool:
    text = _error_text(exc)
    if not text:
        return False
    markers = (
        "previous_response_id",
        "unknown parameter: 'previous_response_id'",
        "unknown parameter: previous_response_id",
        "store",
        "stored completion",
        "not found",
        "does not exist",
    )
    return any(marker in text for marker in markers)


def _responses_stateful_permanently_unsupported_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    if status_code not in {400, 422}:
        return False
    mentions_stateful = any(
        token in text
        for token in (
            "previous_response_id",
            "store",
            "stored completion",
        )
    )
    mentions_incompatibility = any(
        token in text
        for token in (
            "unknown parameter",
            "unknown field",
            "unsupported",
            "not supported",
            "not support",
            "unrecognized",
            "extra inputs",
            "bad request",
            "badrequest",
        )
    )
    return bool(mentions_stateful and mentions_incompatibility)


def _get_attr_or_item(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_to_namespace(value: Any) -> Any:
    """Recursively expose raw HTTP JSON with SDK-like attributes."""
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _json_to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_json_to_namespace(item) for item in value]
    return value


def _extract_image_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    for key in ("result", "image_data", "b64_json", "data"):
        candidate = _get_attr_or_item(value, key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _extract_response_images(response: Any) -> list[str]:
    images: list[str] = []
    output = _get_attr_or_item(response, "output", []) or []
    for item in output:
        item_type = str(_get_attr_or_item(item, "type", ""))
        if item_type == "image_generation_call":
            image = _extract_image_result(item)
            if image:
                images.append(image)
            continue
        for content in _get_attr_or_item(item, "content", []) or []:
            content_type = str(_get_attr_or_item(content, "type", ""))
            if content_type in {"output_image", "image"}:
                image = _extract_image_result(content)
                if image:
                    images.append(image)
    return images


def _extract_response_output_items(response: Any) -> list[dict[str, Any]]:
    """Return a safe structural summary of Responses output items.

    The provider may include opaque reasoning blocks with encrypted_content.
    Preserve their presence for diagnostics without copying hidden/private
    content, long tool arguments, or message text into the UI transcript.
    """
    output = _get_attr_or_item(response, "output", []) or []
    if not isinstance(output, list):
        return []
    items: list[dict[str, Any]] = []
    for index, item in enumerate(output[:64]):
        item_type = str(_get_attr_or_item(item, "type", "") or "")
        if not item_type:
            continue
        entry: dict[str, Any] = {"type": item_type, "index": index}
        item_id = str(_get_attr_or_item(item, "id", "") or "").strip()
        if item_id:
            entry["id"] = item_id
        status = str(_get_attr_or_item(item, "status", "") or "").strip()
        if status:
            entry["status"] = status
        if item_type == "function_call":
            call_id = str(_get_attr_or_item(item, "call_id", "") or "").strip()
            name = str(_get_attr_or_item(item, "name", "") or "").strip()
            if call_id:
                entry["call_id"] = call_id
            if name:
                entry["name"] = name
            arguments = _get_attr_or_item(item, "arguments", "")
            if isinstance(arguments, str):
                entry["arguments_chars"] = len(arguments)
        elif item_type == "message":
            role = str(_get_attr_or_item(item, "role", "") or "").strip()
            if role:
                entry["role"] = role
            phase = str(_get_attr_or_item(item, "phase", "") or "").strip()
            if phase:
                entry["phase"] = phase[:40]
            content = _get_attr_or_item(item, "content", []) or []
            if isinstance(content, list):
                entry["content_types"] = [
                    str(_get_attr_or_item(part, "type", "") or "")
                    for part in content[:16]
                    if str(_get_attr_or_item(part, "type", "") or "")
                ]
        elif item_type == "reasoning":
            summary = _get_attr_or_item(item, "summary", []) or []
            entry["summary_count"] = len(summary) if isinstance(summary, list) else 0
            entry["has_encrypted_content"] = bool(
                _get_attr_or_item(item, "encrypted_content", "") or ""
            )
        elif item_type == "web_search_call":
            action = _get_attr_or_item(item, "action", None)
            action_type = str(_get_attr_or_item(action, "type", "") or "").strip()
            if action_type:
                entry["action_type"] = action_type
        items.append(entry)
    return items


def _record_response_message_phase(
    phases: dict[str, str],
    *,
    item_id: str = "",
    output_index: int | None = None,
    phase: str = "",
) -> None:
    safe_phase = str(phase or "").strip()
    if not safe_phase:
        return
    if item_id:
        phases[item_id] = safe_phase
    if output_index is not None:
        phases[f"output_index:{output_index}"] = safe_phase


def _response_message_phase_for_event(event: Any, phases: dict[str, str]) -> str:
    item_id = str(_get_attr_or_item(event, "item_id", "") or "").strip()
    if item_id and item_id in phases:
        return phases[item_id]
    output_index = _get_attr_or_item(event, "output_index", None)
    if isinstance(output_index, int):
        return phases.get(f"output_index:{output_index}", "")
    return ""


def _proxy_url_for_base_url(base_url: str) -> str:
    parsed = urlparse(str(base_url or ""))
    host = str(parsed.hostname or "").strip().lower()
    port = parsed.port
    no_proxy = ",".join(
        value
        for value in (os.getenv("NO_PROXY", ""), os.getenv("no_proxy", ""))
        if value
    )
    if host and _host_matches_no_proxy(host, port, no_proxy):
        return ""

    explicit = (
        os.getenv("LLM_PROXY_URL", "").strip()
        or os.getenv("MINICODE_LLM_PROXY_URL", "").strip()
    )
    if explicit:
        return explicit

    if parsed.scheme.lower() == "https":
        proxy = os.getenv("HTTPS_PROXY", "").strip() or os.getenv("https_proxy", "").strip()
    else:
        proxy = os.getenv("HTTP_PROXY", "").strip() or os.getenv("http_proxy", "").strip()
    return (
        proxy
        or os.getenv("ALL_PROXY", "").strip()
        or os.getenv("all_proxy", "").strip()
    )


def _normalized_openai_base_url(base_url: str) -> str:
    value = str(base_url or "https://api.openai.com/v1").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc and not parsed.path.strip("/"):
        return f"{value}/v1"
    return value


def _host_matches_no_proxy(host: str, port: int | None, no_proxy: str) -> bool:
    for raw_entry in str(no_proxy or "").split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if "://" in entry:
            entry = urlparse(entry).netloc
        entry_host = entry
        entry_port: int | None = None
        if entry.count(":") == 1:
            candidate_host, candidate_port = entry.rsplit(":", 1)
            if candidate_port.isdigit():
                entry_host = candidate_host
                entry_port = int(candidate_port)
        entry_host = entry_host.lstrip(".").strip("[]")
        if not entry_host or (entry_port is not None and entry_port != port):
            continue
        if host == entry_host or host.endswith(f".{entry_host}"):
            return True
    return False

class OpenAIAdapter(LLMAdapter):
    """
    OpenAI / 兼容 API 适配器。

    根据 wire_api 设置自动路由到 Responses API 或 Chat Completions API。
    """

    def __init__(
        self,
        settings: LLMSettings,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._closed = False
        self._raw_http_client: httpx.AsyncClient | None = None
        self._responses_continuation_state: OrderedDict[str, _ResponsesContinuationState] = OrderedDict()
        self._responses_reasoning_visibility_supported = True
        self._chat_reasoning_visibility_supported = True
        self._responses_prompt_cache_retention_supported = True
        self._responses_metadata_supported = True
        self._chat_metadata_supported = True
        self._chat_stream_usage_supported = True
        self._responses_stateful_supported = True
        self._use_raw_chat_http = client is None
        self._use_raw_responses_http = client is None
        if client:
            self._client = client
        else:
            proxy_url = _proxy_url_for_base_url(settings.base_url)
            if proxy_url:
                http_client = httpx.AsyncClient(proxy=proxy_url, trust_env=False)
            else:
                http_client = httpx.AsyncClient(trust_env=False)
            self._raw_http_client = http_client
            self._client = AsyncOpenAI(
                api_key=settings.api_key,
                base_url=_normalized_openai_base_url(settings.base_url),
                http_client=http_client,
                max_retries=0,
            )

    async def aclose(self) -> None:
        """Close adapter-owned network resources exactly once.

        A client injected by an embedder may be shared across adapters and
        remains the caller's responsibility.
        """
        if self._closed:
            return
        self._closed = True
        if not self._owns_client:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            await close()
        self._raw_http_client = None

    def export_stateful_continuation(self) -> dict[str, Any]:
        """Return provider-side Responses continuation state for local snapshots.

        The payload intentionally contains only opaque response ids and local
        fingerprints. It does not include prompt text, tool arguments, or tool
        outputs, but it is enough for a freshly constructed adapter to continue
        using ``previous_response_id`` after a websocket/session rebuild.
        """
        if not _responses_stateful_continuation_enabled(self._settings):
            return {}
        states: list[dict[str, Any]] = []
        for key, state in self._responses_continuation_state.items():
            if not key or not state.previous_response_id:
                continue
            states.append(
                {
                    "key": key,
                    "previous_response_id": state.previous_response_id,
                    "covered_item_fingerprints": list(state.covered_item_fingerprints),
                    "covered_item_match_fingerprints": list(state.covered_item_match_fingerprints),
                    "prompt_cache_key": state.prompt_cache_key,
                    "instructions_hash": state.instructions_hash,
                    "instructions_full_hash": state.instructions_full_hash,
                    "tools_hash": state.tools_hash,
                }
            )
        if not states:
            return {}
        return {"version": 1, "provider": "openai_responses", "states": states}

    def import_stateful_continuation(self, payload: Any) -> int:
        """Restore Responses continuation state from a local conversation snapshot."""
        if not _responses_stateful_continuation_enabled(self._settings):
            return 0
        if not isinstance(payload, dict):
            return 0
        raw_states = payload.get("states")
        if not isinstance(raw_states, list):
            return 0

        restored = 0
        for raw in raw_states[-_MAX_RESPONSES_CONTINUATION_STATES:]:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "").strip()
            previous_response_id = str(raw.get("previous_response_id") or "").strip()
            if not key or not previous_response_id or len(key) > 256 or len(previous_response_id) > 256:
                continue
            covered = _safe_string_list(raw.get("covered_item_fingerprints"), max_items=4096, max_item_chars=128)
            covered_match = _safe_string_list(raw.get("covered_item_match_fingerprints"), max_items=4096, max_item_chars=128)
            if not covered:
                continue
            self._responses_continuation_state[key] = _ResponsesContinuationState(
                previous_response_id=previous_response_id,
                covered_item_fingerprints=covered,
                covered_item_match_fingerprints=covered_match,
                prompt_cache_key=str(raw.get("prompt_cache_key") or "")[:256],
                instructions_hash=str(raw.get("instructions_hash") or "")[:64],
                instructions_full_hash=str(raw.get("instructions_full_hash") or "")[:64],
                tools_hash=str(raw.get("tools_hash") or "")[:64],
            )
            self._responses_continuation_state.move_to_end(key)
            restored += 1

        while len(self._responses_continuation_state) > _MAX_RESPONSES_CONTINUATION_STATES:
            self._responses_continuation_state.popitem(last=False)
        return restored

    @property
    def capabilities(self) -> ProviderCapabilities:
        return capabilities_from_openai_settings(
            self._settings,
            provider=self._settings.provider,
        )

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        流式调用 LLM。根据 wire_api 路由到对应 API。
        """
        if self._settings.wire_api == "responses":
            async for event in self._stream_responses_api(messages, tools, metadata=metadata):
                yield event
        else:
            async for event in self._stream_chat_completions(messages, tools, metadata=metadata):
                yield event

    async def simple_chat(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """非流式调用，用于摘要、压缩等内部任务。"""
        if self._settings.wire_api == "responses":
            return await self._simple_responses_api(messages, max_tokens=max_tokens)
        else:
            return await self._simple_chat_completions(messages, max_tokens=max_tokens)

    async def _create_responses_with_retry(self, kwargs: dict[str, Any]) -> Any:
        cleaned_kwargs = _strip_openai_unsupported_fields(kwargs)
        kwargs.clear()
        kwargs.update(cleaned_kwargs)
        self._apply_responses_optional_downgrades(kwargs)
        if self._use_raw_responses_http:
            if kwargs.get("stream"):
                return self._emit_responses_http_stream_events_with_retry(kwargs)
            return await self._create_responses_http_with_retry(kwargs)

        stripped_reasoning_visibility = False
        stripped_prompt_cache_retention = False
        transient_retry_available = True
        for _attempt in range(8):
            try:
                return await self._client.responses.create(**kwargs)
            except Exception as exc:
                if not stripped_reasoning_visibility and _is_reasoning_visibility_unsupported_error(exc):
                    retry_kwargs = _strip_reasoning_visibility_request(kwargs)
                    if retry_kwargs != kwargs:
                        self._responses_reasoning_visibility_supported = False
                        logger.warning(
                            "Responses API rejected reasoning summary/content request; retrying without it: %s",
                            exc,
                        )
                        kwargs.clear()
                        kwargs.update(retry_kwargs)
                        stripped_reasoning_visibility = True
                        continue
                if not stripped_prompt_cache_retention and _is_prompt_cache_retention_unsupported_error(exc):
                    retry_kwargs = _strip_prompt_cache_retention_request(kwargs)
                    if retry_kwargs != kwargs:
                        self._responses_prompt_cache_retention_supported = False
                        logger.warning(
                            "Responses API rejected prompt cache retention request; retrying without it: %s",
                            exc,
                        )
                        kwargs.clear()
                        kwargs.update(retry_kwargs)
                        stripped_prompt_cache_retention = True
                        continue
                if _is_request_metadata_unsupported_error(exc):
                    retry_kwargs = self._responses_metadata_stateful_retry_payload(kwargs, exc)
                    if retry_kwargs is not None and retry_kwargs != kwargs:
                        logger.warning(
                            "Responses API rejected optional metadata/store fields; retrying without them: %s",
                            exc,
                        )
                        kwargs.clear()
                        kwargs.update(retry_kwargs)
                        continue
                if transient_retry_available and _is_transient_gateway_error(exc):
                    transient_retry_available = False
                    delay = _retry_after_seconds(exc) or _ADAPTER_RETRY_DELAY_SECONDS
                    logger.warning(
                        "Responses API transient failure, retrying once in %.3gs: %s",
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        raise RuntimeError("Responses API retry failed without an upstream exception")

    def _responses_url(self) -> str:
        base_url = _normalized_openai_base_url(self._settings.base_url)
        return f"{base_url}/responses"

    def _responses_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    async def _responses_http_raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        await response.aread()
        response.raise_for_status()

    async def _create_responses_http(self, payload: dict[str, Any]) -> Any:
        if self._raw_http_client is None:
            raise RuntimeError("Responses HTTP client is not initialized")
        response = await self._raw_http_client.post(
            self._responses_url(),
            headers=self._responses_headers(),
            json=_strip_openai_unsupported_fields(payload),
            timeout=None,
        )
        await self._responses_http_raise_for_status(response)
        return _json_to_namespace(response.json())

    async def _create_responses_http_with_retry(self, payload: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        stripped_reasoning_visibility = False
        stripped_prompt_cache_retention = False
        transient_retry_available = True
        for _attempt in range(8):
            try:
                return await self._create_responses_http(payload)
            except Exception as exc:
                last_exc = exc
                if not stripped_reasoning_visibility and _is_reasoning_visibility_unsupported_error(exc):
                    retry_payload = _strip_reasoning_visibility_request(payload)
                    if retry_payload != payload:
                        self._responses_reasoning_visibility_supported = False
                        logger.warning(
                            "Responses HTTP rejected reasoning summary/content request; retrying without it: %s",
                            exc,
                        )
                        payload.clear()
                        payload.update(retry_payload)
                        stripped_reasoning_visibility = True
                        continue
                if not stripped_prompt_cache_retention and _is_prompt_cache_retention_unsupported_error(exc):
                    retry_payload = _strip_prompt_cache_retention_request(payload)
                    if retry_payload != payload:
                        self._responses_prompt_cache_retention_supported = False
                        logger.warning(
                            "Responses HTTP rejected prompt cache retention request; retrying without it: %s",
                            exc,
                        )
                        payload.clear()
                        payload.update(retry_payload)
                        stripped_prompt_cache_retention = True
                        continue
                if _is_request_metadata_unsupported_error(exc):
                    retry_payload = self._responses_metadata_stateful_retry_payload(payload, exc)
                    if retry_payload is not None and retry_payload != payload:
                        logger.warning(
                            "Responses HTTP rejected optional metadata/store fields; retrying without them: %s",
                            exc,
                        )
                        payload.clear()
                        payload.update(retry_payload)
                        continue
                if transient_retry_available and _is_transient_gateway_error(exc):
                    transient_retry_available = False
                    delay = _retry_after_seconds(exc) or _ADAPTER_RETRY_DELAY_SECONDS
                    logger.warning(
                        "Responses HTTP transient failure, retrying once in %.3gs: %s",
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Responses HTTP retry failed without an upstream exception")

    def _apply_responses_optional_downgrades(self, payload: dict[str, Any]) -> None:
        if not self._responses_reasoning_visibility_supported:
            cleaned = _strip_reasoning_visibility_request(payload)
            payload.clear()
            payload.update(cleaned)
        if not self._responses_prompt_cache_retention_supported:
            payload.pop("prompt_cache_retention", None)
        if not self._responses_metadata_supported:
            payload.pop("metadata", None)
        if not self._responses_stateful_supported:
            payload.pop("store", None)
            payload.pop("previous_response_id", None)

    def _apply_chat_optional_downgrades(self, payload: dict[str, Any]) -> None:
        if not self._chat_reasoning_visibility_supported:
            cleaned = _strip_reasoning_visibility_request(payload)
            payload.clear()
            payload.update(cleaned)
        if not self._chat_metadata_supported:
            payload.pop("metadata", None)
            payload.pop("store", None)
        if not self._chat_stream_usage_supported:
            payload.pop("stream_options", None)

    def _responses_metadata_stateful_retry_payload(
        self,
        payload: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any] | None:
        text = _error_text(exc)
        retry_payload = dict(payload)
        changed = False
        mentions_metadata = "metadata" in text
        mentions_stateful = any(
            token in text
            for token in (
                "store",
                "stored completion",
            )
        )

        if mentions_metadata and "metadata" in retry_payload:
            retry_payload = _strip_metadata_request(retry_payload)
            self._responses_metadata_supported = False
            changed = True

        if mentions_stateful:
            if retry_payload.get("previous_response_id"):
                return None
            if "store" in retry_payload:
                retry_payload = _strip_responses_stateful_request(retry_payload)
                self._responses_stateful_supported = False
                self._responses_continuation_state.clear()
                _add_stateless_reasoning_include_if_needed(
                    retry_payload,
                    self._settings,
                    reasoning_visibility_supported=self._responses_reasoning_visibility_supported,
                )
                changed = True

        if not changed:
            retry_payload = _strip_request_metadata(payload)
            if retry_payload == payload:
                return None
            self._responses_metadata_supported = False
            if "store" in payload and not payload.get("previous_response_id"):
                self._responses_stateful_supported = False
                self._responses_continuation_state.clear()
                _add_stateless_reasoning_include_if_needed(
                    retry_payload,
                    self._settings,
                    reasoning_visibility_supported=self._responses_reasoning_visibility_supported,
                )
            return retry_payload

        return retry_payload

    async def _emit_responses_http_stream_events(self, payload: dict[str, Any]) -> AsyncIterator[Any]:
        if self._raw_http_client is None:
            raise RuntimeError("Responses HTTP client is not initialized")

        async with self._raw_http_client.stream(
            "POST",
            self._responses_url(),
            headers=self._responses_headers(),
            json=_strip_openai_unsupported_fields(payload),
            timeout=None,
        ) as response:
            await self._responses_http_raise_for_status(response)

            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue
                try:
                    yield _json_to_namespace(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("Ignoring malformed responses stream line: %s", line[:200])

    async def _emit_responses_http_stream_events_with_retry(self, payload: dict[str, Any]) -> AsyncIterator[Any]:
        stripped_reasoning_visibility = False
        stripped_prompt_cache_retention = False
        transient_retry_available = True
        for _attempt in range(8):
            yielded_any = False
            try:
                async for event in self._emit_responses_http_stream_events(payload):
                    yielded_any = True
                    yield event
                return
            except Exception as exc:
                if not yielded_any and not stripped_reasoning_visibility and _is_reasoning_visibility_unsupported_error(exc):
                    retry_payload = _strip_reasoning_visibility_request(payload)
                    if retry_payload != payload:
                        self._responses_reasoning_visibility_supported = False
                        logger.warning(
                            "Responses HTTP stream rejected reasoning summary/content request; retrying without it: %s",
                            exc,
                        )
                        payload.clear()
                        payload.update(retry_payload)
                        stripped_reasoning_visibility = True
                        continue
                if not yielded_any and not stripped_prompt_cache_retention and _is_prompt_cache_retention_unsupported_error(exc):
                    retry_payload = _strip_prompt_cache_retention_request(payload)
                    if retry_payload != payload:
                        self._responses_prompt_cache_retention_supported = False
                        logger.warning(
                            "Responses HTTP stream rejected prompt cache retention request; retrying without it: %s",
                            exc,
                        )
                        payload.clear()
                        payload.update(retry_payload)
                        stripped_prompt_cache_retention = True
                        continue
                if not yielded_any and _is_request_metadata_unsupported_error(exc):
                    retry_payload = self._responses_metadata_stateful_retry_payload(payload, exc)
                    if retry_payload is not None and retry_payload != payload:
                        logger.warning(
                            "Responses HTTP stream rejected optional metadata/store fields; retrying without them: %s",
                            exc,
                        )
                        payload.clear()
                        payload.update(retry_payload)
                        continue
                if not yielded_any and transient_retry_available and _is_transient_gateway_error(exc):
                    transient_retry_available = False
                    delay = _retry_after_seconds(exc) or _ADAPTER_RETRY_DELAY_SECONDS
                    logger.warning(
                        "Responses HTTP stream transient failure, retrying once in %.3gs: %s",
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    # ══════════════════════════════════════════════════════════════
    #  Responses API 实现（wire_api="responses"）
    # ══════════════════════════════════════════════════════════════

    async def _stream_responses_api(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        使用 Responses API 流式调用。

        Responses API 格式：
          input: list[dict] — 消息列表
          tools: list[dict] — 工具定义
          stream: bool — 流式
        """
        # 构建 instructions + input（Responses API 消息格式）
        instructions, input_messages = _split_responses_instructions(messages)
        api_input = self._build_responses_input(input_messages)

        model = self._settings.model
        request_metadata = sanitize_llm_request_metadata(metadata)
        prompt_cache_key = _responses_prompt_cache_key(
            self._settings,
            instructions,
            request_metadata,
        )

        # 添加工具定义
        has_response_tools = False
        responses_tools: list[dict[str, Any]] = []
        if tools:
            responses_tools = self._convert_tools_to_responses_format(tools)
            has_response_tools = bool(responses_tools)
        stateful_enabled = (
            _responses_stateful_continuation_enabled(self._settings)
            and self._responses_stateful_supported
        )
        stateful_configured = _responses_stateful_continuation_enabled(self._settings)
        state_key = (
            _responses_continuation_state_key(self._settings, request_metadata, prompt_cache_key)
            if stateful_enabled
            else ""
        )
        stateful_disabled_reason = ""
        if not stateful_configured:
            stateful_disabled_reason = "setting_disabled"
        elif not self._responses_stateful_supported:
            stateful_disabled_reason = "provider_unsupported"
        elif not state_key:
            stateful_disabled_reason = "missing_conversation_or_session_metadata"
        current_instructions_hash = _responses_stateful_instructions_hash(instructions)
        current_instructions_full_hash = _responses_stateful_full_instructions_hash(instructions)
        current_tools_hash = _json_fingerprint(responses_tools)
        request_input = api_input
        request_input_fingerprints = _responses_input_fingerprints(api_input)
        request_input_match_fingerprints = _responses_input_match_fingerprints(api_input)
        previous_response_id = ""
        covered_prefix_fingerprints: list[str] = []
        covered_prefix_match_fingerprints: list[str] = []
        input_items_omitted = 0
        stateful_reset_reason = ""
        if state_key:
            state = self._responses_continuation_state.get(state_key)
            if state and state.previous_response_id and state.prompt_cache_key == prompt_cache_key:
                self._responses_continuation_state.move_to_end(state_key)
                if state.instructions_hash != current_instructions_hash:
                    stateful_reset_reason = "request_shape_changed"
                    self._responses_continuation_state.pop(state_key, None)
                else:
                    covered = state.covered_item_match_fingerprints or state.covered_item_fingerprints
                    if (
                        covered
                        and len(request_input_match_fingerprints) > len(covered)
                        and request_input_match_fingerprints[: len(covered)] == covered
                    ):
                        previous_response_id = state.previous_response_id
                        covered_prefix_fingerprints = list(
                            state.covered_item_fingerprints[: len(covered)]
                        )
                        covered_prefix_match_fingerprints = list(covered)
                        request_input = api_input[len(covered):]
                        request_input_fingerprints = request_input_fingerprints[len(covered):]
                        request_input_match_fingerprints = request_input_match_fingerprints[len(covered):]
                        input_items_omitted = len(covered)
                    else:
                        stateful_reset_reason = "history_prefix_changed"
                        self._responses_continuation_state.pop(state_key, None)

        kwargs: dict[str, Any] = {
            "model": model,
            "input": request_input,
            "stream": True,
        }
        if state_key:
            kwargs["store"] = True
        elif self._responses_stateful_supported:
            kwargs["store"] = False
        # Responses continuation only carries response items. OpenAI explicitly
        # does not inherit ``instructions`` through ``previous_response_id``, so
        # every request must send the current system/developer instructions.
        if instructions:
            kwargs["instructions"] = instructions
        if request_metadata:
            kwargs["metadata"] = request_metadata
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        prompt_cache_retention = _prompt_cache_retention_request(self._settings)
        if prompt_cache_retention:
            kwargs["prompt_cache_retention"] = prompt_cache_retention
        if responses_tools:
            kwargs["tools"] = responses_tools
        try:
            configured_max_output = int(self._settings.max_tokens)
        except (TypeError, ValueError):
            configured_max_output = 0
        if configured_max_output > 0:
            kwargs["max_output_tokens"] = configured_max_output

        # Ask every Responses-compatible gateway for provider-visible reasoning
        # summaries. Unsupported gateways are retried without these optional
        # fields by _create_responses_with_retry.
        reasoning_request = _responses_reasoning_request(
            self._settings,
            has_tools=has_response_tools,
        )
        if reasoning_request:
            kwargs["reasoning"] = reasoning_request
        include_request = _responses_include_request(
            self._settings,
            has_tools=has_response_tools,
            reasoning_request=reasoning_request,
            stateful_store=kwargs.get("store") is True,
        )
        if include_request:
            kwargs["include"] = include_request

        try:
            stream = await self._create_responses_with_retry(kwargs)
        except Exception as exc:
            if previous_response_id and _responses_stateful_unsupported_error(exc):
                logger.warning(
                    "Responses stateful continuation failed; retrying stateless and resetting continuation: %s",
                    exc,
                )
                self._responses_continuation_state.pop(state_key, None)
                if _responses_stateful_permanently_unsupported_error(exc):
                    self._responses_stateful_supported = False
                    self._responses_continuation_state.clear()
                retry_kwargs = dict(kwargs)
                retry_kwargs["input"] = api_input
                retry_kwargs.pop("previous_response_id", None)
                if instructions:
                    retry_kwargs["instructions"] = instructions
                if not self._responses_stateful_supported:
                    retry_kwargs.pop("store", None)
                elif (
                    "store" in _error_text(exc) and "previous_response_id" not in _error_text(exc)
                ):
                    retry_kwargs["store"] = False
                request_input = api_input
                request_input_fingerprints = _responses_input_fingerprints(api_input)
                request_input_match_fingerprints = _responses_input_match_fingerprints(api_input)
                previous_response_id = ""
                covered_prefix_fingerprints = []
                covered_prefix_match_fingerprints = []
                input_items_omitted = 0
                stateful_reset_reason = "stateful_retry_stateless"
                _add_stateless_reasoning_include_if_needed(
                    retry_kwargs,
                    self._settings,
                    has_tools=has_response_tools,
                    reasoning_visibility_supported=self._responses_reasoning_visibility_supported,
                )
                try:
                    stream = await self._create_responses_with_retry(retry_kwargs)
                    kwargs = retry_kwargs
                except Exception as retry_exc:
                    logger.error("Responses API 调用失败: %s", retry_exc)
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=_adapter_error_content("LLM API 调用失败", retry_exc),
                        raw=_adapter_error_raw(retry_exc, "openai_responses"),
                    )
                    return
            else:
                logger.error("Responses API 调用失败: %s", exc)
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    content=_adapter_error_content("LLM API 调用失败", exc),
                    raw=_adapter_error_raw(exc, "openai_responses"),
                )
                return

        # 解析 Responses API 流式事件
        full_text = ""
        pending_tool_calls: list[ToolCallEvent] = []
        response_tool_items: dict[str, dict[str, str]] = {}
        response_message_phases: dict[str, str] = {}
        usage = UsageInfo()
        provider_timeline: list[dict[str, Any]] = []
        raw_done: dict[str, Any] = {
            "provider": "openai_responses",
            "model": kwargs["model"],
            "request_summary": _safe_request_summary(
                model=str(kwargs["model"]),
                wire_api="responses",
                instructions=instructions,
                tools=kwargs.get("tools") if isinstance(kwargs.get("tools"), list) else [],
                request_metadata=request_metadata,
                input_items=request_input,
                prompt_cache_key=str(kwargs.get("prompt_cache_key") or ""),
                previous_response_id=str(kwargs.get("previous_response_id") or ""),
                request_params=kwargs,
                input_items_omitted_by_continuation=input_items_omitted,
            ),
            "safety": _provider_trace_safety(),
            "provider_timeline": provider_timeline,
            "stateful_continuation": {
                "configured": bool(stateful_configured),
                "enabled": bool(state_key and kwargs.get("store") is True),
                "used": bool(previous_response_id),
                "input_items_omitted": input_items_omitted,
                **(
                    {"disabled_reason": stateful_disabled_reason}
                    if stateful_disabled_reason
                    else {"disabled_reason": "store_not_sent"}
                    if state_key and kwargs.get("store") is not True
                    else {}
                ),
                **({"reset_reason": stateful_reset_reason} if stateful_reset_reason else {}),
            },
        }
        finish_reason = ""
        completed_response_id = ""
        completed_response_local_items: list[dict[str, Any]] = []
        completed_response_provider_items: list[dict[str, Any]] = []
        completed_response_message_phase = ""
        saw_terminal_response_event = False

        try:
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type:
                    _append_provider_timeline(
                        provider_timeline,
                        str(event_type),
                        **_response_timeline_fields(str(event_type), event),
                    )

                # 文本内容增量
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        full_text += delta
                        message_phase = _response_message_phase_for_event(event, response_message_phases)
                        raw_text = _raw_text_delta_metadata(
                            "openai_responses",
                            usage_obj=getattr(event, "usage", None),
                            message_phase=message_phase,
                        )
                        yield StreamEvent(
                            type=StreamEventType.TEXT_CHUNK,
                            content=delta,
                            raw=raw_text,
                            phase=message_phase,
                        )

                # Capture provider-native citations (url_citation annotations)
                # from the completed output text. Attached to the DONE event's
                # raw metadata so the frontend can bind [1] [2] markers to real
                # sources instead of relying solely on tool-call heuristics.
                elif event_type == "response.output_text.done":
                    message_phase = str(_get_attr_or_item(event, "phase", "") or "").strip()
                    if message_phase:
                        item_id = str(_get_attr_or_item(event, "item_id", "") or "").strip()
                        output_index = _get_attr_or_item(event, "output_index", None)
                        _record_response_message_phase(
                            response_message_phases,
                            item_id=item_id,
                            output_index=output_index if isinstance(output_index, int) else None,
                            phase=message_phase,
                        )
                    citations = _extract_url_citations(event)
                    if citations:
                        raw_done.setdefault("citations", citations)

                # 推理摘要增量（GPT-5 / o-series via Responses API）
                elif event_type in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                }:
                    delta = getattr(event, "delta", "")
                    if delta:
                        reasoning_type = (
                            "reasoning_summary_text"
                            if event_type == "response.reasoning_summary_text.delta"
                            else "reasoning_text"
                        )
                        yield StreamEvent(
                            type=StreamEventType.THINKING_CHUNK,
                            content=str(delta),
                            raw={"provider_reasoning_type": reasoning_type},
                        )

                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    item_type = getattr(item, "type", "")
                    if item_type == "message":
                        item_id = str(getattr(item, "id", "") or "").strip()
                        phase = str(getattr(item, "phase", "") or "").strip()
                        output_index = _get_attr_or_item(event, "output_index", None)
                        _record_response_message_phase(
                            response_message_phases,
                            item_id=item_id,
                            output_index=output_index if isinstance(output_index, int) else None,
                            phase=phase,
                        )
                    elif item_type == "function_call":
                        item_id = str(getattr(item, "id", "") or "").strip()
                        call_id = str(getattr(item, "call_id", "") or item_id).strip()
                        name = str(getattr(item, "name", "") or "").strip()
                        key = call_id or item_id
                        if key:
                            slot = {
                                "id": call_id or item_id,
                                "item_id": item_id,
                                "name": name,
                                "arguments": "",
                            }
                            response_tool_items[key] = slot
                            if item_id and item_id != key:
                                response_tool_items[item_id] = slot
                            if name:
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_START,
                                    tool_call_start=ToolCallStartEvent(
                                        id=call_id or item_id,
                                        name=name,
                                        index=len(response_tool_items) - 1,
                                    ),
                                )

                elif event_type == "response.function_call_arguments.delta":
                    event_call_id = str(getattr(event, "call_id", "") or "").strip()
                    item_id = str(getattr(event, "item_id", "") or "").strip()
                    call_id = event_call_id or item_id
                    delta = str(getattr(event, "delta", "") or "")
                    if call_id and delta:
                        slot = response_tool_items.get(call_id) or response_tool_items.get(item_id)
                        if slot is None:
                            slot = {"id": call_id, "item_id": item_id, "name": "", "arguments": ""}
                            response_tool_items[call_id] = slot
                            if item_id and item_id != call_id:
                                response_tool_items[item_id] = slot
                        slot["arguments"] += delta
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call_delta=ToolCallDeltaEvent(
                                id=slot.get("id") or call_id,
                                partial_arguments=slot["arguments"],
                            ),
                        )

                # 函数调用
                elif event_type == "response.function_call_arguments.done":
                    item_id = str(getattr(event, "item_id", "") or "").strip()
                    event_call_id = str(getattr(event, "call_id", "") or "").strip()
                    slot = response_tool_items.get(event_call_id) or response_tool_items.get(item_id) or {}
                    call_id = str(slot.get("id") or event_call_id or item_id).strip()
                    name = str(getattr(event, "name", "") or slot.get("name", "")).strip()
                    # Prefer the delta-accumulated arguments; some OpenAI-compatible
                    # gateways stream args via function_call_arguments.delta but send
                    # an empty/missing `arguments` on .done, which would otherwise
                    # drop the whole tool input. Mirrors the Chat path, which always
                    # finalizes from the index-keyed accumulator.
                    arguments_str = getattr(event, "arguments", "") or slot.get("arguments") or "{}"

                    arguments_repaired = False
                    try:
                        arguments = json.loads(arguments_str)
                    except (json.JSONDecodeError, TypeError):
                        from backend.llm.json_repair import repair_tool_json
                        arguments = repair_tool_json(arguments_str) or {"_raw": arguments_str}
                        arguments_repaired = True
                    tool_call = ToolCallEvent(
                        id=call_id,
                        name=name,
                        arguments=arguments,
                        arguments_repaired=arguments_repaired,
                    )
                    pending_tool_calls.append(tool_call)
                    prefetch_event = _prefetch_tool_call_event([tool_call])
                    if prefetch_event is not None:
                        yield prefetch_event

                # 完成
                elif event_type in {
                    "response.image_generation_call.partial_image",
                    "response.image_generation_call.completed",
                }:
                    image_data = _extract_image_result(event)
                    if image_data:
                        yield StreamEvent(
                            type=StreamEventType.IMAGE_CHUNK,
                            image_data=image_data,
                            image_media_type="image/png",
                        )

                elif event_type == "response.completed":
                    saw_terminal_response_event = True
                    response_obj = getattr(event, "response", None)
                    if response_obj:
                        finish_reason = _response_finish_reason(response_obj)
                        for image_data in _extract_response_images(response_obj):
                            yield StreamEvent(
                                type=StreamEventType.IMAGE_CHUNK,
                                image_data=image_data,
                                image_media_type="image/png",
                            )
                        usage_obj = getattr(response_obj, "usage", None)
                        if usage_obj:
                            usage = UsageInfo(
                                input_tokens=_get_usage_field(usage_obj, "input_tokens"),
                                output_tokens=_get_usage_field(usage_obj, "output_tokens"),
                                cache_read_input_tokens=_get_cached_prompt_tokens(usage_obj),
                                reasoning_output_tokens=_get_reasoning_output_tokens(usage_obj),
                            )
                        output_items = _extract_response_output_items(response_obj)
                        completed_response_id = str(_get_attr_or_item(response_obj, "id", "") or "").strip()
                        completed_response_provider_items = _responses_provider_items_from_response(response_obj)
                        completed_response_message_phase = _responses_message_phase_from_response(response_obj)
                        completed_response_local_items = _responses_local_output_items_from_response(
                            response_obj,
                            fallback_text=full_text,
                        )
                        if pending_tool_calls:
                            if full_text and not any(item.get("role") == "assistant" for item in completed_response_local_items):
                                completed_response_local_items.insert(0, {"role": "assistant", "content": full_text})
                            for tool_call in pending_tool_calls:
                                existing_function_call_ids = {
                                    str(item.get("call_id") or "")
                                    for item in completed_response_local_items
                                    if item.get("type") == "function_call"
                                }
                                if tool_call.id not in existing_function_call_ids:
                                    completed_response_local_items.append(_responses_function_call_input_item(tool_call))
                        raw_done.update({
                            "provider": "openai_responses",
                            "model": kwargs["model"],
                            "event_type": event_type,
                            "finish_reason": finish_reason,
                            "usage": _raw_usage_metadata(usage_obj),
                        })
                        if completed_response_id:
                            raw_done["response_id_hash"] = _short_sha256(completed_response_id)
                        if output_items:
                            raw_done["output_items"] = output_items
                        if completed_response_provider_items:
                            raw_done["provider_items_summary"] = _responses_provider_items_metadata(
                                completed_response_provider_items
                            )
                        if completed_response_message_phase:
                            raw_done["response_message_phase"] = completed_response_message_phase
                        raw_done["safety"] = _provider_trace_safety(output_items)
                elif event_type == "response.incomplete":
                    saw_terminal_response_event = True
                    response_obj = getattr(event, "response", None)
                    finish_reason = _response_finish_reason(response_obj) or "incomplete"
                    raw_done.update({
                        "provider": "openai_responses",
                        "model": kwargs["model"],
                        "event_type": event_type,
                        "finish_reason": finish_reason,
                    })
                    output_items = _extract_response_output_items(response_obj)
                    if output_items:
                        raw_done["output_items"] = output_items
                    raw_done["safety"] = _provider_trace_safety(output_items)
                elif event_type == "response.failed":
                    error = getattr(event, "error", None) or getattr(getattr(event, "response", None), "error", None)
                    message = str(getattr(error, "message", "") or error or "Responses API response failed")
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=f"Responses API response failed: {message}",
                        raw={"provider": "openai_responses", "event_type": event_type},
                    )
                    return
        except Exception as exc:
            if (
                previous_response_id
                and not provider_timeline
                and _responses_stateful_unsupported_error(exc)
            ):
                logger.warning(
                    "Responses continuation failed during first stream iteration; "
                    "resetting state and replaying stateless: %s",
                    exc,
                )
                self._responses_continuation_state.pop(state_key, None)
                if _responses_stateful_permanently_unsupported_error(exc):
                    self._responses_stateful_supported = False
                    self._responses_continuation_state.clear()
                async for retry_event in self._stream_responses_api(messages, tools, metadata=metadata):
                    yield retry_event
                return
            logger.error("Responses API 流式解析异常: %s", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=f"LLM 流式响应异常: {_clean_error_message(exc)}",
                raw=_adapter_error_raw(exc, "openai_responses"),
            )
            return

        if not saw_terminal_response_event:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="Responses API stream ended before a terminal response event.",
                raw={"provider": "openai_responses", "event_type": "eof_without_terminal"},
            )
            return

        # 输出聚合的 tool_calls
        if pending_tool_calls:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=pending_tool_calls,
            )

        if state_key and completed_response_id and kwargs.get("store") is True:
            replay_items = _responses_completed_turn_replay_items(
                local_items=completed_response_local_items,
                provider_items=completed_response_provider_items,
                fallback_text=full_text,
                pending_tool_calls=pending_tool_calls,
            )
            response_item_fingerprints = _responses_input_fingerprints(replay_items)
            response_item_match_fingerprints = _responses_input_match_fingerprints(replay_items)
            next_covered_fingerprints = [
                *covered_prefix_fingerprints,
                *request_input_fingerprints,
                *response_item_fingerprints,
            ]
            next_covered_match_fingerprints = [
                *covered_prefix_match_fingerprints,
                *request_input_match_fingerprints,
                *response_item_match_fingerprints,
            ]
            if next_covered_fingerprints:
                self._responses_continuation_state[state_key] = _ResponsesContinuationState(
                    previous_response_id=completed_response_id,
                    covered_item_fingerprints=next_covered_fingerprints,
                    covered_item_match_fingerprints=next_covered_match_fingerprints,
                    prompt_cache_key=prompt_cache_key,
                    instructions_hash=current_instructions_hash,
                    instructions_full_hash=current_instructions_full_hash,
                    tools_hash=current_tools_hash,
                )
                self._responses_continuation_state.move_to_end(state_key)
                while len(self._responses_continuation_state) > _MAX_RESPONSES_CONTINUATION_STATES:
                    self._responses_continuation_state.popitem(last=False)
                raw_done["stateful_continuation"] = {
                    **dict(raw_done.get("stateful_continuation") or {}),
                    "stored_response_id_hash": _short_sha256(completed_response_id),
                    "covered_items": len(next_covered_fingerprints),
                    "response_output_items_covered": len(response_item_fingerprints),
                }

        raw_done["request_summary"] = _safe_request_summary(
            model=str(kwargs["model"]),
            wire_api="responses",
            instructions=instructions,
            tools=kwargs.get("tools") if isinstance(kwargs.get("tools"), list) else [],
            request_metadata=request_metadata,
            input_items=request_input,
            prompt_cache_key=str(kwargs.get("prompt_cache_key") or ""),
            previous_response_id=str(kwargs.get("previous_response_id") or ""),
            request_params=kwargs,
            input_items_omitted_by_continuation=input_items_omitted,
        )

        yield StreamEvent(
            type=StreamEventType.DONE,
            usage=usage,
            finish_reason=finish_reason,
            raw=raw_done,
            provider_items=completed_response_provider_items,
            phase=completed_response_message_phase,
        )

    async def _simple_responses_api(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Responses API 非流式调用。"""
        instructions, input_messages = _split_responses_instructions(messages)
        api_input = self._build_responses_input(input_messages)

        model = self._settings.model
        kwargs: dict[str, Any] = {
            "model": model,
            "input": api_input,
            "store": False,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if max_tokens:
            kwargs["max_output_tokens"] = max(1, int(max_tokens))
        prompt_cache_key = _responses_prompt_cache_key(
            self._settings,
            instructions,
            None,
        )
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key
        prompt_cache_retention = _prompt_cache_retention_request(self._settings)
        if prompt_cache_retention:
            kwargs["prompt_cache_retention"] = prompt_cache_retention

        reasoning_request = _responses_reasoning_request(self._settings)
        if reasoning_request:
            kwargs["reasoning"] = reasoning_request

        try:
            response = await self._create_responses_with_retry(kwargs)
        except Exception as exc:
            logger.error("Responses API simple_chat 失败: %s", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        # Side calls (compaction/recovery) must be counted toward
        # cost/token totals, not only the streaming DONE frames.
        _resp_usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        self.record_non_stream_usage(
            _resp_usage,
            provider="openai",
            model_id=model,
            input_includes_cache_read=True,
        )

        # 从 response.output 提取文本
        text = getattr(response, "output_text", "") or ""
        text = text.strip()

        if not text:
            # 尝试从 output 数组中提取
            output = getattr(response, "output", [])
            for item in output:
                if getattr(item, "type", "") == "message":
                    for content in getattr(item, "content", []):
                        if getattr(content, "type", "") == "output_text":
                            text = getattr(content, "text", "")
                            break

        if not text:
            raise RuntimeError("LLM 返回空内容")

        return text

    def _build_responses_input(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """将 LLMMessage 列表转换为 Responses API 的 input 格式。"""
        result: list[dict[str, Any]] = []

        for msg in messages:
            role = str(msg.role or "").strip().lower()
            if role in {"system", "developer"}:
                continue
            elif role == "user":
                if msg.images or msg.documents:
                    parts: list[dict[str, Any]] = []
                    content_text = _message_content_text(msg.content)
                    if content_text:
                        parts.append({"type": "input_text", "text": content_text})
                    for img in msg.images:
                        media_type = img.get("media_type") or "image/png"
                        data = img.get("data") or ""
                        if not data:
                            continue
                        parts.append({
                            "type": "input_image",
                            "image_url": f"data:{media_type};base64,{data}",
                            "detail": "auto",
                        })
                    for doc in msg.documents:
                        media_type = doc.get("media_type") or "application/pdf"
                        data = doc.get("data") or ""
                        if not data:
                            continue
                        parts.append({
                            "type": "input_file",
                            "filename": doc.get("file_name") or "attachment.pdf",
                            "file_data": f"data:{media_type};base64,{data}",
                        })
                    result.append({
                        "role": "user",
                        "content": parts or content_text,
                    })
                else:
                    result.append({
                        "role": "user",
                        "content": _message_content_text(msg.content),
                    })
            elif role == "assistant":
                provider_items = [
                    dict(item)
                    for item in (msg.provider_items or [])
                    if isinstance(item, dict)
                    and str(item.get("type") or "") in {"reasoning", "function_call"}
                ]
                provider_function_call_ids = {
                    str(item.get("call_id") or item.get("id") or "")
                    for item in provider_items
                    if item.get("type") == "function_call"
                }
                for item in provider_items:
                    if item.get("type") == "reasoning":
                        result.append(item)
                assistant_text = _message_content_text(msg.content)
                if assistant_text:
                    assistant_item: dict[str, Any] = {
                        "role": "assistant",
                        "content": assistant_text,
                    }
                    phase = str(getattr(msg, "phase", "") or "").strip()
                    if phase:
                        assistant_item["phase"] = phase[:40]
                    result.append(assistant_item)
                for item in provider_items:
                    if item.get("type") == "function_call":
                        result.append(item)
                if msg.tool_calls:
                    # Responses represents assistant text and function calls as
                    # adjacent items. Keep the short pre-tool narration in
                    # history instead of dropping it when tool_calls are present.
                    for tc in msg.tool_calls:
                        if tc.id not in provider_function_call_ids:
                            result.append(_responses_function_call_input_item(tc))
            elif role == "tool":
                result.append(_responses_function_call_output_item(msg))

        return result

    @staticmethod
    def _convert_tools_to_responses_format(
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将 OpenAI function-calling 格式转换为 Responses API 格式。"""
        result = []
        for tool in tools:
            func = tool.get("function", {})
            result.append({
                "type": "function",
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "parameters": _normalize_schema_for_openai(func.get("parameters", {})),
                "strict": bool(func.get("strict", False)),
            })
        return result

    @staticmethod
    def _normalize_chat_tools(
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_tools: list[dict[str, Any]] = []
        for tool in tools:
            normalized_tool = dict(tool)
            function_def = dict(normalized_tool.get("function", {}))
            # Preserve the tool's strict flag so OpenAI structured outputs are
            # requested for tools that declare strict=True (matching cc's
            # behavior). The schema normalization below still enforces
            # additionalProperties:false for every object schema.
            function_def["strict"] = bool(function_def.get("strict", False))
            function_def["parameters"] = _normalize_schema_for_openai(
                function_def.get("parameters", {})
            )
            normalized_tool["function"] = function_def
            normalized_tools.append(normalized_tool)
        return normalized_tools

    def _chat_completions_url(self) -> str:
        base_url = _normalized_openai_base_url(self._settings.base_url)
        return f"{base_url}/chat/completions"

    def _chat_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    async def _chat_http_raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        await response.aread()
        response.raise_for_status()

    async def _emit_chat_http_stream_events(
        self,
        payload: dict[str, Any],
        request_summary: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._raw_http_client is None:
            raise RuntimeError("Chat HTTP client is not initialized")

        full_text = ""
        accumulator = _ToolCallAccumulator()
        usage = UsageInfo()
        provider_timeline: list[dict[str, Any]] = []
        raw_done: dict[str, Any] = {
            "provider": "openai_chat_completions",
            "model": self._settings.model,
            "request_summary": request_summary or {},
            "safety": _provider_trace_safety(),
            "provider_timeline": provider_timeline,
        }
        finish_reason = ""
        saw_terminal_event = False
        tool_prefetch_emitted = False
        reasoning_splitter = _ReasoningSplitter()

        async with self._raw_http_client.stream(
            "POST",
            self._chat_completions_url(),
            headers=self._chat_headers(),
            json=_strip_openai_unsupported_fields(payload),
            timeout=None,
        ) as response:
            await self._chat_http_raise_for_status(response)

            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    saw_terminal_event = True
                    _append_provider_timeline(provider_timeline, "chat.done")
                    break

                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring malformed chat stream line: %s", line[:200])
                    continue

                if isinstance(chunk.get("error"), dict):
                    message = str(chunk["error"].get("message") or "Chat completion stream failed")
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=f"Chat completion stream failed: {message}",
                        raw={"provider": "openai_chat_completions", "event_type": "error"},
                    )
                    return

                usage_obj = chunk.get("usage")
                raw_text_delta = _raw_text_delta_metadata(
                    "openai_chat_completions",
                    usage_obj=usage_obj,
                )
                if usage_obj:
                    usage = UsageInfo(
                        input_tokens=_get_chat_prompt_tokens(usage_obj),
                        output_tokens=_get_usage_field(usage_obj, "completion_tokens"),
                        cache_read_input_tokens=_get_cached_prompt_tokens(usage_obj),
                        reasoning_output_tokens=_get_reasoning_output_tokens(usage_obj),
                    )
                    raw_done["usage"] = _raw_usage_metadata(usage_obj)
                    _append_provider_timeline(provider_timeline, "chat.usage", usage_present=True)

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}

                reasoning_content = delta.get("reasoning_content")
                if reasoning_content:
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.reasoning_content.delta",
                        delta_chars=len(str(reasoning_content)),
                    )
                    yield StreamEvent(
                        type=StreamEventType.THINKING_CHUNK,
                        content=str(reasoning_content),
                        raw={"provider_reasoning_type": "reasoning_content"},
                    )

                content = delta.get("content")
                if content:
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.content.delta",
                        delta_chars=len(str(content)),
                    )
                    cleaned = _strip_special_tokens(str(content))
                    for evt in _splitter_events(reasoning_splitter.feed(cleaned)):
                        if evt.type == StreamEventType.TEXT_CHUNK:
                            full_text += evt.content
                            evt.raw = raw_text_delta
                        yield evt

                tool_call_deltas = delta.get("tool_calls") or []
                if tool_call_deltas:
                    # _ReasoningSplitter intentionally holds a short suffix so
                    # split <think> tags cannot leak into visible text.  A tool
                    # delta is a hard provider boundary: release that suffix
                    # before any tool event, otherwise the loop seals a visibly
                    # truncated narration and the held characters arrive late.
                    for evt in _splitter_events(reasoning_splitter.flush()):
                        if evt.type == StreamEventType.TEXT_CHUNK:
                            full_text += evt.content
                            evt.raw = raw_text_delta
                        yield evt

                for tool_call in tool_call_deltas:
                    idx = int(tool_call.get("index") or 0)
                    should_start, _key, slot = accumulator.feed(tool_call, idx)
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.tool_call.delta",
                        index=idx,
                        call_id=slot.get("id"),
                        name=slot.get("name"),
                        arguments_chars=len(str(slot.get("arguments") or "")),
                    )
                    if should_start:
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_START,
                            tool_call_start=ToolCallStartEvent(
                                id=slot["id"], name=slot["name"], index=idx,
                            ),
                        )
                    elif slot["_start_emitted"] and slot["_delta_bytes"] >= _DELTA_DEBOUNCE_BYTES:
                        slot["_delta_bytes"] = 0
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call_delta=ToolCallDeltaEvent(
                                id=slot["id"],
                                partial_arguments=slot["arguments"],
                            ),
                        )

                # NOTE: do not break here. With stream_options.include_usage the
                # gateway sends the token counts in a trailing chunk (choices: [])
                # AFTER the finish_reason chunk. Breaking on finish_reason would
                # drop it and leave usage at zero. The loop ends on [DONE] / EOF.
                if choice.get("finish_reason"):
                    finish_reason = str(choice.get("finish_reason") or "")
                    raw_done["finish_reason"] = finish_reason
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.finish",
                        finish_reason=finish_reason,
                    )
                    if not tool_prefetch_emitted and finish_reason == "tool_calls":
                        for evt in _splitter_events(reasoning_splitter.flush()):
                            if evt.type == StreamEventType.TEXT_CHUNK:
                                full_text += evt.content
                                evt.raw = raw_text_delta
                            yield evt
                        prefetch_event = _prefetch_tool_call_event(accumulator.finalize())
                        if prefetch_event is not None:
                            yield prefetch_event
                            tool_prefetch_emitted = True
                    continue

        if not saw_terminal_event and finish_reason:
            # Some OpenAI-compatible gateways close the SSE response after a
            # valid finish_reason without emitting the optional [DONE] sentinel.
            # The semantic completion marker is sufficient; treating this as a
            # failed stream discards valid text/tool calls and triggers retries.
            saw_terminal_event = True
            raw_done["terminal_fallback"] = "eof_after_finish_reason"
            _append_provider_timeline(
                provider_timeline,
                "chat.eof_after_finish",
                finish_reason=finish_reason,
            )

        if not saw_terminal_event:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="Chat completion stream ended before [DONE].",
                raw={
                    "provider": "openai_chat_completions",
                    "event_type": "eof_without_terminal",
                    "request_summary": request_summary or {},
                    "safety": _provider_trace_safety(),
                    "provider_timeline": provider_timeline,
                },
            )
            return

        for evt in _splitter_events(reasoning_splitter.flush()):
            if evt.type == StreamEventType.TEXT_CHUNK:
                full_text += evt.content
            yield evt

        tool_call_events = accumulator.finalize()
        if tool_call_events:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=tool_call_events)

        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=finish_reason, raw=raw_done)

    async def _stream_chat_completions_http(
        self,
        kwargs: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        self._apply_chat_optional_downgrades(kwargs)

        def build_payload_request_summary(payload: dict[str, Any]) -> dict[str, Any]:
            payload_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            return _safe_request_summary(
                model=self._settings.model,
                wire_api="chat",
                instructions=_instruction_text_from_chat_payload(payload_messages),
                tools=payload.get("tools") if isinstance(payload.get("tools"), list) else [],
                request_metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                input_items=_chat_payload_input_items(payload_messages),
                request_params=payload,
            )

        async def emit(payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
            async for event in self._emit_chat_http_stream_events(
                payload,
                build_payload_request_summary(payload),
            ):
                yield event

        try:
            async for event in emit(kwargs):
                yield event
            return
        except Exception as exc:
            if _is_stream_options_unsupported_error(exc):
                logger.warning(
                    "Chat Completions HTTP rejected stream usage options; retrying without them: %s",
                    exc,
                )
                self._chat_stream_usage_supported = False
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("stream_options", None)
                try:
                    async for event in emit(retry_kwargs):
                        yield event
                    return
                except Exception as retry_exc:
                    exc = retry_exc
                    kwargs = retry_kwargs
            if _is_request_metadata_unsupported_error(exc):
                logger.warning(
                    "Chat Completions HTTP rejected request metadata; retrying without it: %s",
                    exc,
                )
                self._chat_metadata_supported = False
                retry_kwargs = _strip_request_metadata(kwargs)
                try:
                    async for event in emit(retry_kwargs):
                        yield event
                    return
                except Exception as retry_exc:
                    exc = retry_exc
                    kwargs = retry_kwargs

            if _is_reasoning_visibility_unsupported_error(exc):
                logger.warning(
                    "Chat Completions HTTP rejected reasoning summary/content request; retrying without it: %s",
                    exc,
                )
                retry_kwargs = _strip_reasoning_visibility_request(kwargs)
                if retry_kwargs != kwargs:
                    self._chat_reasoning_visibility_supported = False
                    try:
                        async for event in emit(retry_kwargs):
                            yield event
                        return
                    except Exception as retry_exc:
                        exc = retry_exc
                        kwargs = retry_kwargs

            _log_chat_provider_error(self._settings, "Chat Completions HTTP API", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=_adapter_error_content("LLM API 调用失败", exc),
                raw=_adapter_error_raw(exc, "openai_chat_completions"),
            )

    async def _simple_chat_completions_http(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        if self._raw_http_client is None:
            raise RuntimeError("Chat HTTP client is not initialized")

        openai_messages = _openai_chat_messages(messages)
        payload = {
            "model": self._settings.model,
            "messages": openai_messages,
            "stream": False,
            **_chat_max_tokens_kwargs(self._settings),
        }
        if max_tokens:
            for key in ("max_tokens", "max_completion_tokens"):
                if key in payload:
                    payload[key] = max(1, int(max_tokens))
        if self._settings.seed is not None:
            payload["seed"] = self._settings.seed

        try:
            response = await self._raw_http_client.post(
                self._chat_completions_url(),
                headers=self._chat_headers(),
                json=_strip_openai_unsupported_fields(payload),
                timeout=None,
            )
            response.raise_for_status()
        except Exception as exc:
            _log_chat_provider_error(self._settings, "Chat Completions HTTP simple_chat", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        data = response.json()
        # Side calls must be counted toward cost/token totals.
        self.record_non_stream_usage(
            data.get("usage") if isinstance(data, dict) else None,
            provider="openai",
            model_id=self._settings.model,
            input_includes_cache_read=True,
        )
        choices = data.get("choices") or []
        if choices:
            message = (choices[0] or {}).get("message") or {}
            content = message.get("content")
            if content:
                return str(content).strip()

        raise RuntimeError("LLM 返回空内容")

    # ══════════════════════════════════════════════════════════════
    #  Chat Completions API 实现（wire_api="chat"）
    # ══════════════════════════════════════════════════════════════

    async def _stream_chat_completions(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """使用 Chat Completions API 流式调用。"""
        openai_messages = _openai_chat_messages(messages)
        request_metadata = sanitize_llm_request_metadata(metadata)

        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": openai_messages,
            "stream": True,
            # Ask the gateway to emit a trailing usage-only chunk (choices: [],
            # usage: {...}) after generation. Without this the Chat Completions
            # wire API sends no token counts at all and usage stays at zero.
            "stream_options": {"include_usage": True},
            **_chat_max_tokens_kwargs(self._settings),
        }
        if self._settings.seed is not None:
            kwargs["seed"] = self._settings.seed
        if request_metadata:
            kwargs["metadata"] = request_metadata
            kwargs["store"] = False
        kwargs.update(_chat_reasoning_visibility_request(self._settings))

        if tools:
            kwargs["tools"] = self._normalize_chat_tools(tools)
            kwargs["tool_choice"] = "auto"

        self._apply_chat_optional_downgrades(kwargs)

        def build_chat_request_summary(payload: dict[str, Any]) -> dict[str, Any]:
            payload_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            return _safe_request_summary(
                model=self._settings.model,
                wire_api="chat",
                instructions=_instruction_text_from_chat_payload(payload_messages),
                tools=payload.get("tools") if isinstance(payload.get("tools"), list) else [],
                request_metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                input_items=_chat_payload_input_items(payload_messages),
                request_params=payload,
            )

        sent_kwargs = kwargs
        chat_request_summary = build_chat_request_summary(sent_kwargs)

        if self._use_raw_chat_http:
            async for event in self._stream_chat_completions_http(kwargs):
                yield event
            return

        stream: Any | None = None
        try:
            stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(kwargs))
        except Exception as create_exc:
            pending_exc: Exception = create_exc
            pending_kwargs = kwargs
            if _is_stream_options_unsupported_error(pending_exc):
                logger.warning(
                    "Chat Completions rejected stream usage options; retrying without them: %s",
                    pending_exc,
                )
                self._chat_stream_usage_supported = False
                retry_kwargs = dict(pending_kwargs)
                retry_kwargs.pop("stream_options", None)
                try:
                    stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(retry_kwargs))
                    sent_kwargs = retry_kwargs
                except Exception as retry_exc:
                    pending_exc = retry_exc
                    pending_kwargs = retry_kwargs
            if _is_request_metadata_unsupported_error(pending_exc):
                logger.warning(
                    "Chat Completions rejected request metadata; retrying without it: %s",
                    pending_exc,
                )
                self._chat_metadata_supported = False
                retry_kwargs = _strip_request_metadata(pending_kwargs)
                try:
                    stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(retry_kwargs))
                    sent_kwargs = retry_kwargs
                except Exception as retry_exc:
                    pending_exc = retry_exc
                    pending_kwargs = retry_kwargs
            if _is_reasoning_visibility_unsupported_error(pending_exc):
                logger.warning(
                    "Chat Completions rejected reasoning summary/content request; retrying without it: %s",
                    pending_exc,
                )
                retry_kwargs = _strip_reasoning_visibility_request(pending_kwargs)
                if retry_kwargs != pending_kwargs:
                    self._chat_reasoning_visibility_supported = False
                    try:
                        stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(retry_kwargs))
                        sent_kwargs = retry_kwargs
                    except Exception as retry_exc:
                        pending_exc = retry_exc
                        pending_kwargs = retry_kwargs
            if stream is not None:
                chat_request_summary = build_chat_request_summary(sent_kwargs)
            else:
                _log_chat_provider_error(self._settings, "Chat Completions API", pending_exc)
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    content=_adapter_error_content("LLM API 调用失败", pending_exc),
                    raw=_adapter_error_raw(pending_exc, "openai_chat_completions"),
                )
                return

        full_text = ""
        accumulator = _ToolCallAccumulator()
        usage = UsageInfo()
        provider_timeline: list[dict[str, Any]] = []
        raw_done: dict[str, Any] = {
            "provider": "openai_chat_completions",
            "model": self._settings.model,
            "request_summary": chat_request_summary,
            "safety": _provider_trace_safety(),
            "provider_timeline": provider_timeline,
        }
        finish_reason = ""
        tool_prefetch_emitted = False
        reasoning_splitter = _ReasoningSplitter()

        try:
            async for chunk in stream:
                if not chunk.choices:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = UsageInfo(
                            input_tokens=_get_chat_prompt_tokens(chunk.usage),
                            output_tokens=_get_usage_field(chunk.usage, "completion_tokens"),
                            cache_read_input_tokens=_get_cached_prompt_tokens(chunk.usage),
                            reasoning_output_tokens=_get_reasoning_output_tokens(chunk.usage),
                        )
                        raw_done["usage"] = _raw_usage_metadata(chunk.usage)
                        _append_provider_timeline(provider_timeline, "chat.usage", usage_present=True)
                    continue

                choice = chunk.choices[0]
                delta = choice.delta
                raw_text_delta = _raw_text_delta_metadata(
                    "openai_chat_completions",
                    usage_obj=getattr(chunk, "usage", None),
                )

                reasoning_content = getattr(delta, "reasoning_content", None)
                if reasoning_content:
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.reasoning_content.delta",
                        delta_chars=len(str(reasoning_content)),
                    )
                    yield StreamEvent(
                        type=StreamEventType.THINKING_CHUNK,
                        content=str(reasoning_content),
                        raw={"provider_reasoning_type": "reasoning_content"},
                    )

                if delta and delta.content:
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.content.delta",
                        delta_chars=len(str(delta.content)),
                    )
                    cleaned = _strip_special_tokens(delta.content)
                    for evt in _splitter_events(reasoning_splitter.feed(cleaned)):
                        if evt.type == StreamEventType.TEXT_CHUNK:
                            full_text += evt.content
                            evt.raw = raw_text_delta
                        yield evt

                if delta and delta.tool_calls:
                    # Preserve the stream contract that all response text is
                    # emitted before the first tool boundary.  Without this,
                    # the splitter's held suffix is delivered after
                    # TOOL_CALL_START and the agent timeline loses the end of
                    # the model's sentence.
                    for evt in _splitter_events(reasoning_splitter.flush()):
                        if evt.type == StreamEventType.TEXT_CHUNK:
                            full_text += evt.content
                            evt.raw = raw_text_delta
                        yield evt
                    for tc in delta.tool_calls:
                        idx = int(tc.index) if tc.index is not None else 0
                        tc_dict = {
                            "id": tc.id or "",
                            "function": {
                                "name": tc.function.name if tc.function else "",
                                "arguments": tc.function.arguments if tc.function else "",
                            },
                        }
                        should_start, _key, slot = accumulator.feed(tc_dict, idx)
                        _append_provider_timeline(
                            provider_timeline,
                            "chat.tool_call.delta",
                            index=idx,
                            call_id=slot.get("id"),
                            name=slot.get("name"),
                            arguments_chars=len(str(slot.get("arguments") or "")),
                        )
                        if should_start:
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_start=ToolCallStartEvent(
                                    id=slot["id"], name=slot["name"], index=idx,
                                ),
                            )
                        elif slot["_start_emitted"] and slot["_delta_bytes"] >= _DELTA_DEBOUNCE_BYTES:
                            slot["_delta_bytes"] = 0
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_DELTA,
                                tool_call_delta=ToolCallDeltaEvent(
                                    id=slot["id"],
                                    partial_arguments=slot["arguments"],
                                ),
                            )

                # Do not break on finish_reason: the trailing usage-only chunk
                # (choices: [], handled above) arrives afterward. Breaking here
                # would skip it and leave usage at zero. Loop ends at stream EOF.
                if choice.finish_reason:
                    finish_reason = str(choice.finish_reason or "")
                    raw_done["finish_reason"] = finish_reason
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.finish",
                        finish_reason=finish_reason,
                    )
                    if not tool_prefetch_emitted and finish_reason == "tool_calls":
                        for evt in _splitter_events(reasoning_splitter.flush()):
                            if evt.type == StreamEventType.TEXT_CHUNK:
                                full_text += evt.content
                                evt.raw = raw_text_delta
                            yield evt
                        prefetch_event = _prefetch_tool_call_event(accumulator.finalize())
                        if prefetch_event is not None:
                            yield prefetch_event
                            tool_prefetch_emitted = True
                    continue
        except Exception as exc:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=f"Stream interrupted: {_clean_error_message(exc)}",
                raw=_adapter_error_raw(exc, "openai_chat_completions"),
            )
            return

        if not finish_reason:
            _append_provider_timeline(provider_timeline, "chat.eof_without_terminal")
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="Chat completion stream ended before a finish reason.",
                raw={
                    "provider": "openai_chat_completions",
                    "event_type": "eof_without_terminal",
                    "request_summary": chat_request_summary,
                    "safety": _provider_trace_safety(),
                    "provider_timeline": provider_timeline,
                },
            )
            return

        for evt in _splitter_events(reasoning_splitter.flush()):
            if evt.type == StreamEventType.TEXT_CHUNK:
                full_text += evt.content
            yield evt

        tool_call_events = accumulator.finalize()
        if tool_call_events:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=tool_call_events)

        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=finish_reason, raw=raw_done)

    async def _simple_chat_completions(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Chat Completions API 非流式调用。"""
        if self._use_raw_chat_http:
            return await self._simple_chat_completions_http(messages, max_tokens=max_tokens)

        openai_messages = _openai_chat_messages(messages)

        try:
            payload = {
                "model": self._settings.model,
                "messages": openai_messages,
                **_chat_max_tokens_kwargs(self._settings),
            }
            if max_tokens:
                for key in ("max_tokens", "max_completion_tokens"):
                    if key in payload:
                        payload[key] = max(1, int(max_tokens))
            if self._settings.seed is not None:
                payload["seed"] = self._settings.seed
            response = await self._client.chat.completions.create(
                **_strip_openai_unsupported_fields(payload)
            )
        except Exception as exc:
            _log_chat_provider_error(self._settings, "Chat Completions simple_chat", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        # Side calls must be counted toward cost/token totals.
        self.record_non_stream_usage(
            getattr(response, "usage", None),
            provider="openai",
            model_id=self._settings.model,
            input_includes_cache_read=True,
        )

        choice = response.choices[0] if response.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()

        raise RuntimeError("LLM 返回空内容")
