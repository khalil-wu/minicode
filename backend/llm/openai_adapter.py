
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

logger = logging.getLogger(__name__)

_DELTA_DEBOUNCE_BYTES = 128

# Some OpenAI-compatible gateways (GLM, DeepSeek-R1, Qwen via vLLM/OpenRouter)
# emit chain-of-thought inline in delta.content wrapped in <think>...</think>
# instead of a separate reasoning_content field.
_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"
# Hold back this many chars across deltas in case a tag is split mid-chunk.
_THINK_TAG_HOLD = max(len(_THINK_OPEN_TAG), len(_THINK_CLOSE_TAG)) - 1

# ChatML/ChatGLM special tokens some gateways leak into content.
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]*\|>")


def _strip_special_tokens(text: str) -> str:
    """Remove leaked <|im_start|>/<|im_end|>/<|endoftext|>/<|user|>/... markers."""
    if not text or "<|" not in text:
        return text
    return _SPECIAL_TOKEN_RE.sub("", text)


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


def _strip_openai_unsupported_fields(value: Any) -> Any:
    """Remove Anthropic-only fields before sending OpenAI-compatible payloads."""
    if isinstance(value, dict):
        return {
            key: _strip_openai_unsupported_fields(item)
            for key, item in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [_strip_openai_unsupported_fields(item) for item in value]
    return value


def _strip_reasoning_visibility_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Retry payload without optional reasoning visibility requests.

    Some OpenAI-compatible gateways reject `reasoning.summary` or Chat
    reasoning hints. The main request should still succeed; only the request
    for provider-visible reasoning gets dropped on this retry.
    """
    cleaned = dict(payload)
    reasoning = cleaned.get("reasoning")
    if isinstance(reasoning, dict):
        next_reasoning = dict(reasoning)
        next_reasoning.pop("summary", None)
        next_reasoning.pop("content", None)
        if next_reasoning:
            cleaned["reasoning"] = next_reasoning
        else:
            cleaned.pop("reasoning", None)
    cleaned.pop("reasoning_summary", None)
    cleaned.pop("reasoning_content", None)
    return cleaned


def _strip_request_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Retry payload without optional trace metadata fields."""
    cleaned = dict(payload)
    cleaned.pop("metadata", None)
    cleaned.pop("store", None)
    return cleaned


class _ReasoningSplitter:
    """Routes inline <think>...</think> reasoning out of the answer text stream.

    Feed each content delta to ``feed``; text inside the tags is returned as
    ``("reasoning", seg)`` (route to THINKING_CHUNK) and text outside as
    ``("text", seg)`` (route to TEXT_CHUNK). A tag split across deltas is held
    back until it can be resolved; call ``flush`` at end of stream to emit any
    remainder. Correctness: when no complete tag is found, the last
    ``_THINK_TAG_HOLD`` chars are held, which is enough that any tag starting
    earlier would already have been matched.
    """

    def __init__(self) -> None:
        self._inside = False
        self._buf = ""

    def feed(self, delta: str) -> list[tuple[str, str]]:
        self._buf += delta
        out: list[tuple[str, str]] = []
        while True:
            if self._inside:
                # Inside reasoning: look for closing </think>
                idx = self._buf.find(_THINK_CLOSE_TAG)
                if idx == -1:
                    break
                segment = self._buf[:idx]
                if segment:
                    out.append(("reasoning", segment))
                self._buf = self._buf[idx + len(_THINK_CLOSE_TAG):]
                self._inside = False
            else:
                # Outside reasoning: look for <think> (enter) or orphan </think>
                # (some models/gateways emit reasoning WITHOUT the opening <think>
                # tag, using only </think> as a separator — e.g. GPT-5.5 via proxy).
                open_idx = self._buf.find(_THINK_OPEN_TAG)
                close_idx = self._buf.find(_THINK_CLOSE_TAG)
                if open_idx == -1 and close_idx == -1:
                    break
                if open_idx != -1 and (close_idx == -1 or open_idx <= close_idx):
                    # Opening <think> found first → enter reasoning mode
                    segment = self._buf[:open_idx]
                    if segment:
                        out.append(("text", segment))
                    self._buf = self._buf[open_idx + len(_THINK_OPEN_TAG):]
                    self._inside = True
                else:
                    # Orphan </think> (no opening <think>): model emitted reasoning
                    # without the opening tag. Everything before </think> is reasoning;
                    # after is the answer text.
                    segment = self._buf[:close_idx]
                    if segment:
                        out.append(("reasoning", segment))
                    self._buf = self._buf[close_idx + len(_THINK_CLOSE_TAG):]
                    self._inside = False
        if len(self._buf) > _THINK_TAG_HOLD:
            cut = len(self._buf) - _THINK_TAG_HOLD
            out.append((self._kind(), self._buf[:cut]))
            self._buf = self._buf[cut:]
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        out = [(self._kind(), self._buf)]
        self._buf = ""
        return out

    def _kind(self) -> str:
        return "reasoning" if self._inside else "text"


def _splitter_events(
    segments: list[tuple[str, str]],
) -> list[StreamEvent]:
    """Map splitter segments to events: reasoning -> THINKING_CHUNK, else TEXT_CHUNK."""
    events: list[StreamEvent] = []
    for kind, segment in segments:
        if not segment:
            continue
        if kind == "reasoning":
            events.append(
                StreamEvent(
                    type=StreamEventType.THINKING_CHUNK,
                    content=segment,
                    raw={"provider_reasoning_type": "inline_think"},
                )
            )
        else:
            events.append(StreamEvent(type=StreamEventType.TEXT_CHUNK, content=segment))
    return events


class _ToolCallAccumulator:
    """Accumulates streamed tool-call deltas, keyed by (id, index) to handle
    gateways that reuse index=0 for multiple calls."""

    def __init__(self) -> None:
        self._slots: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def feed(self, tool_call: dict[str, Any], index: int) -> tuple[bool, str, dict[str, Any]]:
        """Feed a delta chunk. Returns (is_new, slot_key, slot_data)."""
        call_id = tool_call.get("id") or ""
        function = tool_call.get("function") or {}
        name = function.get("name") or ""

        # In OpenAI-compatible streaming the `index` field is the stable
        # per-delta identifier: `id` and `name` arrive only in the first delta
        # of a call, while later deltas carry argument fragments with `index`
        # alone (DeepSeek behaves this way). Keying by `id` would split a single
        # call across an `id:<id>` slot (id+name, empty args) and an `idx:<n>`
        # slot (args, no id/name) — the former then fails arg validation and the
        # latter is dropped. Key by index so every fragment lands in one slot.
        key = f"idx:{index}"

        # Parallel tool calls can reuse an index after the prior one completed;
        # a different id (or changed name) on an existing index slot is a new call.
        existing = self._slots.get(key)
        if existing is not None:
            existing_id = str(existing.get("id") or "")
            existing_name = str(existing.get("name") or "")
            if call_id and existing_id and call_id != existing_id:
                key = f"idx:{index}:{call_id}"
            elif not call_id and name and existing_name and existing_name != name:
                key = f"idx:{index}:{name}"

        is_new = key not in self._slots
        if is_new:
            self._slots[key] = {
                "id": call_id,
                "name": name,
                "arguments": "",
                "_delta_bytes": 0,
            }
            self._order.append(key)

        slot = self._slots[key]
        if call_id:
            slot["id"] = call_id
        if name:
            slot["name"] = name
        if function.get("arguments"):
            slot["arguments"] += str(function["arguments"])
            slot["_delta_bytes"] += len(str(function["arguments"]))

        return is_new, key, slot

    def finalize(self) -> list[ToolCallEvent]:
        """Parse accumulated arguments and return final ToolCallEvent list."""
        events: list[ToolCallEvent] = []
        for key in self._order:
            slot = self._slots[key]
            call_id = str(slot.get("id") or "").strip()
            name = str(slot.get("name") or "").strip()
            raw_args = str(slot.get("arguments") or "")
            raw_arg_len = len(raw_args)
            if not call_id or not name:
                logger.debug(
                    "Dropping incomplete streamed tool call key=%s id=%r name=%r args=%r",
                    key,
                    call_id,
                    name,
                    raw_args[:200],
                )
                continue
            parse_status = "ok"
            try:
                arguments = json.loads(raw_args or "{}")
            except (json.JSONDecodeError, TypeError):
                from backend.llm.json_repair import repair_tool_json
                arguments = repair_tool_json(raw_args) or {"_raw": raw_args}
                parse_status = "repaired" if "_raw" not in arguments else "raw"
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
                parse_status = "wrapped"
            log = logger.warning if name and not arguments else logger.debug
            log(
                "Finalized streamed tool call key=%s id=%s name=%s raw_arg_len=%d parse_status=%s",
                key,
                call_id,
                name,
                raw_arg_len,
                parse_status,
            )
            events.append(ToolCallEvent(
                id=call_id,
                name=name,
                arguments=arguments,
            ))
        return events


def _prefetch_tool_call_event(tool_calls: list[ToolCallEvent]) -> StreamEvent | None:
    if not tool_calls:
        return None
    return StreamEvent(
        type=StreamEventType.TOOL_CALL,
        tool_calls=list(tool_calls),
        tool_calls_final=False,
    )

_TRANSIENT_ERROR_SUBSTRINGS = (
    "concurrency limit exceeded",
    "retry later",
    "rate limit",
    "too many requests",
    "429",
    "timeout",
    "temporarily unavailable",
)
_ADAPTER_RETRY_DELAY_SECONDS = 0.8
_CHAT_HTTP_TIMEOUT_SECONDS = 120.0
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


def _is_lucen_gateway(base_url: str) -> bool:
    host = _provider_host(base_url).lower()
    return host == "lucen.cc" or host.endswith(".lucen.cc")


def _responses_reasoning_effort(settings: LLMSettings, *, has_tools: bool = False) -> str:
    effort = str(settings.reasoning_effort or "").strip().lower()
    if effort == "max":
        # `max` is a UI/OpenRouter alias. OpenAI Responses documents the
        # highest official effort as `xhigh`, and Lucen follows that shape.
        effort = "xhigh"
    if _is_lucen_gateway(settings.base_url):
        # Lucen accepts Responses + tools with explicit reasoning, while
        # tool-enabled requests without reasoning can hang or be rejected.
        if has_tools and not effort:
            return "high"
    return effort


def _responses_reasoning_request(settings: LLMSettings, *, has_tools: bool = False) -> dict[str, Any]:
    """Request provider-visible reasoning summaries whenever the gateway allows it."""
    reasoning: dict[str, Any] = {"summary": "auto"}
    effort = _responses_reasoning_effort(settings, has_tools=has_tools)
    if effort:
        reasoning["effort"] = effort
    return reasoning


def _chat_reasoning_visibility_request(settings: LLMSettings) -> dict[str, Any]:
    """Best-effort hints for OpenAI-compatible chat gateways with reasoning deltas."""
    effort = str(settings.reasoning_effort or "").strip().lower()
    if effort == "max":
        effort = "xhigh"
    payload: dict[str, Any] = {
        "reasoning_summary": "auto",
        "reasoning_content": True,
    }
    if effort:
        payload["reasoning"] = {"effort": effort, "summary": "auto", "content": True}
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


def _is_image_model(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith("gpt-image-") or normalized in {"image-2", "image2"}


def _response_tool_model(model: str) -> str:
    """Responses image generation uses a text/reasoning model plus an image tool."""
    return "gpt-5.4-mini" if _is_image_model(model) else model


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


def _safe_request_params(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload or {}
    params: dict[str, Any] = {}
    for key in ("stream", "store", "tool_choice", "max_tokens", "parallel_tool_calls"):
        value = source.get(key)
        if isinstance(value, (str, bool, int, float)):
            params[key] = value
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
        "turn_aborted_marker_present": _contains_turn_aborted_marker(input_items if input_items is not None else messages or []),
    }
    if instructions:
        summary["instructions_len"] = len(instructions)
        summary["instructions_hash"] = _short_sha256(instructions)
    else:
        summary["instructions_len"] = 0
        summary["instructions_hash"] = ""

    tool_list = tools or []
    summary["tools_len"] = len(tool_list)
    summary["tools_hash"] = _json_fingerprint(tool_list) if tool_list else ""
    summary["tool_names"] = _safe_tool_names(tool_list)
    summary["tool_schema_hashes"] = _safe_tool_schema_hashes(tool_list)

    input_counts: dict[str, int] = {}
    if input_items is not None:
        for item in input_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or item.get("role") or "message")
            input_counts[item_type] = input_counts.get(item_type, 0) + 1
        summary["input_items_len"] = len(input_items)
    elif messages is not None:
        for message in messages:
            role = str(getattr(message, "role", "") or "message")
            input_counts[role] = input_counts.get(role, 0) + 1
        summary["input_items_len"] = len(messages)
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
    """Split system/developer instructions from Responses conversation input."""
    instructions: list[str] = []
    input_messages: list[LLMMessage] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            content = str(message.content or "").strip()
            if content:
                instructions.append(content)
            continue
        input_messages.append(message)
    return "\n\n".join(instructions), input_messages


def _responses_prompt_cache_key(
    settings: LLMSettings,
    instructions: str,
    request_metadata: dict[str, str] | None = None,
) -> str:
    """Build a stable, non-reversible cache routing key for Responses requests."""
    metadata = request_metadata or {}
    stable_scope = {
        key: metadata.get(key, "")
        for key in ("conversation_id", "minicode_session_id", "cwd", "minicode_source")
        if metadata.get(key)
    }
    if not instructions and not stable_scope:
        return ""
    # Key the routing hash on the byte-stable system prefix only, not the
    # concatenated instructions. The stable prefix (everything before
    # __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__) is identical across turns; the dynamic
    # suffix (workspace summary, skills, memory) churns. Hashing only the stable
    # prefix keeps the routing key — and thus the provider-side cache slot —
    # stable across dynamic-context changes, mirroring what the Anthropic path
    # does with cache_control on the split stable block. When no boundary marker
    # is present (raw custom prompt), split_sys_prompt_prefix returns the full
    # text as the stable prefix, preserving prior behavior.
    stable_prefix = split_sys_prompt_prefix(instructions).stable_prefix if instructions else ""
    payload = {
        "provider_host": _provider_host(settings.base_url),
        "model": _response_tool_model(settings.model),
        "instructions_sha256": hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest() if stable_prefix else "",
        "scope": stable_scope,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"minicode-{digest[:48]}"


def _is_image_generation_prompt(messages: list[LLMMessage]) -> bool:
    for message in reversed(messages):
        if message.role != "user":
            continue
        content = message.content.lower()
        return (
            any(
                token in content
                for token in (
                    "生成图片",
                    "生成一张",
                    "生成一个",
                    "画一张",
                    "画个",
                    "绘制",
                    "做一张图",
                    "出一张图",
                    "create an image",
                    "generate an image",
                    "generate a photo",
                    "generate a picture",
                    "draw an image",
                    "draw a picture",
                    "make an image",
                )
            )
            and not message.images
        )
    return False


def _image_generation_tool(model: str) -> dict[str, Any]:
    image_model = model if _is_image_model(model) else "gpt-image-2"
    if image_model == "image-2":
        image_model = "gpt-image-2"
    if image_model == "image2":
        image_model = "gpt-image-2"
    return {"type": "image_generation", "model": image_model}


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


def _error_text(exc: Exception) -> str:
    parts: list[str] = [str(exc)]
    for attr in ("message", "code", "param", "body"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(str(value))
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("text", "content"):
            value = getattr(response, attr, None)
            if value:
                parts.append(str(value))
    return " ".join(parts).lower()


def _error_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _get_usage_field(usage_obj: Any, name: str, default: int = 0) -> int:
    if usage_obj is None:
        return default
    if isinstance(usage_obj, dict):
        value = usage_obj.get(name, default)
    else:
        value = getattr(usage_obj, name, default)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _get_cached_prompt_tokens(usage_obj: Any) -> int:
    details = None
    if isinstance(usage_obj, dict):
        details = usage_obj.get("prompt_tokens_details") or usage_obj.get("input_tokens_details")
    elif usage_obj is not None:
        details = getattr(usage_obj, "prompt_tokens_details", None) or getattr(usage_obj, "input_tokens_details", None)
    return _get_usage_field(details, "cached_tokens", 0)


def _get_reasoning_output_tokens(usage_obj: Any) -> int:
    direct = _get_usage_field(usage_obj, "reasoning_output_tokens", 0)
    if direct:
        return direct
    details = None
    if isinstance(usage_obj, dict):
        details = (
            usage_obj.get("completion_tokens_details")
            or usage_obj.get("output_tokens_details")
        )
    elif usage_obj is not None:
        details = (
            getattr(usage_obj, "completion_tokens_details", None)
            or getattr(usage_obj, "output_tokens_details", None)
        )
    return _get_usage_field(details, "reasoning_tokens", 0)


def _raw_usage_metadata(usage_obj: Any) -> dict[str, Any]:
    if not usage_obj:
        return {}
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    )
    raw: dict[str, Any] = {}
    for field in fields:
        value = _get_usage_field(usage_obj, field, 0)
        if value:
            raw[field] = value
    cached = _get_cached_prompt_tokens(usage_obj)
    if cached:
        raw["cached_prompt_tokens"] = cached
    reasoning = _get_reasoning_output_tokens(usage_obj)
    if reasoning:
        raw["reasoning_output_tokens"] = reasoning
    return raw


def _raw_text_delta_metadata(
    provider: str,
    *,
    usage_obj: Any = None,
    citations: list[dict[str, Any]] | None = None,
    finish_reason: str = "",
    message_phase: str = "",
) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    usage = _raw_usage_metadata(usage_obj)
    if usage:
        raw["provider"] = provider
        raw["usage"] = usage
    if citations:
        raw["provider"] = provider
        raw["citations"] = citations
    if finish_reason:
        raw["provider"] = provider
        raw["finish_reason"] = finish_reason
    if message_phase:
        raw["provider"] = provider
        raw["message_phase"] = message_phase
    return raw


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


def _clean_error_message(exc: Exception) -> str:
    """清洗错误消息，移除 HTML 标签（如 Cloudflare 502 错误页）。"""
    msg = str(exc)
    # 移除 HTML 标签
    msg = re.sub(r'<[^>]+>', ' ', msg)
    # 压缩多余空白
    msg = re.sub(r'\s+', ' ', msg).strip()
    # NOTE: do NOT translate/rewrite the message here. Error classification
    # (classify_llm_error) runs on this text downstream; rewriting "connection
    # error" to Chinese strips the English keyword and forces an `unknown`
    # (non-retryable) verdict. Keep the raw text and let
    # sanitize_llm_error_message produce the final user-facing wording.
    # 截断过长错误
    if len(msg) > 200:
        msg = msg[:200] + '...'
    return msg


def _is_invalid_tool_schema_error(exc: Exception) -> bool:
    """Detect tool/schema compatibility errors from OpenAI-compatible gateways."""
    text = _error_text(exc)
    mentions_tools = any(
        token in text
        for token in (
            "tool",
            "tools",
            "tool_choice",
            "function",
            "function_call",
            "function calling",
        )
    )
    mentions_schema = any(
        token in text
        for token in (
            "schema",
            "json schema",
            "parameters",
            "additionalproperties",
            "additional properties",
            "strict",
        )
    )
    mentions_incompatibility = any(
        token in text
        for token in (
            "invalid",
            "unsupported",
            "not supported",
            "not support",
            "unrecognized",
            "unknown parameter",
            "badrequest",
            "bad request",
        )
    )
    status_code = _error_status_code(exc)

    return (
        bool(mentions_tools and (mentions_schema or mentions_incompatibility))
        or bool(mentions_schema and mentions_incompatibility)
        or bool(status_code == 400 and mentions_tools)
    )


def _is_reasoning_visibility_unsupported_error(exc: Exception) -> bool:
    """Detect gateways rejecting optional reasoning summary/content requests."""
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    mentions_reasoning_visibility = any(
        token in text
        for token in (
            "reasoning.summary",
            "reasoning_summary",
            "reasoning summary",
            "reasoning",
            "summary",
            "reasoning.content",
            "reasoning_content",
            "reasoning text",
            "reasoning_text",
        )
    )
    mentions_incompatibility = any(
        token in text
        for token in (
            "invalid",
            "unsupported",
            "not supported",
            "not support",
            "unrecognized",
            "unknown parameter",
            "unknown field",
            "extra inputs",
            "badrequest",
            "bad request",
        )
    )
    return bool(status_code in {400, 422} and mentions_reasoning_visibility and mentions_incompatibility)


def _is_request_metadata_unsupported_error(exc: Exception) -> bool:
    """Detect gateways rejecting optional request metadata/store fields."""
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    mentions_request_metadata = any(
        token in text
        for token in (
            "metadata",
            "store",
            "stored completion",
            "unknown parameter: 'metadata'",
            "unknown parameter: 'store'",
        )
    )
    mentions_incompatibility = any(
        token in text
        for token in (
            "invalid",
            "unsupported",
            "not supported",
            "not support",
            "unrecognized",
            "unknown parameter",
            "unknown field",
            "extra inputs",
            "badrequest",
            "bad request",
        )
    )
    return bool(status_code in {400, 422} and mentions_request_metadata and mentions_incompatibility)


def _is_blocked_gateway_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    return bool(status_code == 403) or any(
        token in text
        for token in (
            "your request was blocked",
            "request was blocked",
            "blocked by",
            "cloudflare",
            "cf-ray",
            "waf",
            "forbidden",
        )
    )


def _is_transient_gateway_error(exc: Exception) -> bool:
    text = str(exc).lower()
    status_code = _error_status_code(exc)
    return bool(status_code in {408, 409, 425, 429, 500, 502, 503, 504}) or any(
        token in text for token in _TRANSIENT_ERROR_SUBSTRINGS
    )


def _normalize_schema_for_openai(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_normalize_schema_for_openai(item) for item in schema]

    if not isinstance(schema, dict):
        return schema

    normalized = {key: _normalize_schema_for_openai(value) for key, value in schema.items()}
    if normalized.get("type") == "object" and "additionalProperties" not in normalized:
        normalized["additionalProperties"] = False
    return normalized


def _minimal_chat_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    for message in reversed(messages):
        if message.role == "user" and (message.content.strip() or message.images):
            return [
                LLMMessage(
                    role="user",
                    content=message.content.strip() or "Please inspect the attached image.",
                    images=list(message.images),
                ).to_openai_message()
            ]
    return [{"role": "user", "content": "Please continue."}]

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
        self._raw_http_client: httpx.AsyncClient | None = None
        self._use_raw_chat_http = client is None
        self._use_raw_responses_http = client is None
        if client:
            self._client = client
        else:
            proxy_url = (
                os.getenv("LLM_PROXY_URL", "").strip()
                or os.getenv("MINICODE_LLM_PROXY_URL", "").strip()
            )
            no_proxy = os.getenv("NO_PROXY", "") + "," + os.getenv("no_proxy", "")
            base_host = (settings.base_url or "").split("//")[-1].split("/")[0].split(":")[0]
            skip_proxy = any(
                h.strip() and base_host.endswith(h.strip())
                for h in no_proxy.split(",")
            )
            if proxy_url and not skip_proxy:
                http_client = httpx.AsyncClient(proxy=proxy_url)
            else:
                http_client = httpx.AsyncClient(trust_env=False)
            self._raw_http_client = http_client
            self._client = AsyncOpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                http_client=http_client,
                max_retries=0,
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

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        """非流式调用，用于摘要、压缩等内部任务。"""
        if self._settings.wire_api == "responses":
            return await self._simple_responses_api(messages)
        else:
            return await self._simple_chat_completions(messages)

    async def _create_responses_with_retry(self, kwargs: dict[str, Any]) -> Any:
        kwargs = _strip_openai_unsupported_fields(kwargs)
        if self._use_raw_responses_http:
            if kwargs.get("stream"):
                return self._emit_responses_http_stream_events_with_retry(kwargs)
            return await self._create_responses_http_with_retry(kwargs)

        stripped_reasoning_visibility = False
        for attempt in range(2):
            try:
                return await self._client.responses.create(**kwargs)
            except Exception as exc:
                if not stripped_reasoning_visibility and _is_reasoning_visibility_unsupported_error(exc):
                    retry_kwargs = _strip_reasoning_visibility_request(kwargs)
                    if retry_kwargs != kwargs:
                        logger.warning(
                            "Responses API rejected reasoning summary/content request; retrying without it: %s",
                            exc,
                        )
                        kwargs = retry_kwargs
                        stripped_reasoning_visibility = True
                        continue
                if attempt == 0 and _is_transient_gateway_error(exc):
                    logger.warning(
                        "Responses API transient failure, retrying once: %s",
                        exc,
                    )
                    await asyncio.sleep(_ADAPTER_RETRY_DELAY_SECONDS)
                    continue
                raise
        raise RuntimeError("Responses API retry failed without an upstream exception")

    def _responses_url(self) -> str:
        base_url = (self._settings.base_url or "https://api.openai.com/v1").rstrip("/")
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
            timeout=_CHAT_HTTP_TIMEOUT_SECONDS,
        )
        await self._responses_http_raise_for_status(response)
        return _json_to_namespace(response.json())

    async def _create_responses_http_with_retry(self, payload: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        stripped_reasoning_visibility = False
        for attempt in range(2):
            try:
                return await self._create_responses_http(payload)
            except Exception as exc:
                last_exc = exc
                if not stripped_reasoning_visibility and _is_reasoning_visibility_unsupported_error(exc):
                    retry_payload = _strip_reasoning_visibility_request(payload)
                    if retry_payload != payload:
                        logger.warning(
                            "Responses HTTP rejected reasoning summary/content request; retrying without it: %s",
                            exc,
                        )
                        payload = retry_payload
                        stripped_reasoning_visibility = True
                        continue
                if attempt == 0 and _is_transient_gateway_error(exc):
                    logger.warning(
                        "Responses HTTP transient failure, retrying once: %s",
                        exc,
                    )
                    await asyncio.sleep(_ADAPTER_RETRY_DELAY_SECONDS)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Responses HTTP retry failed without an upstream exception")

    async def _emit_responses_http_stream_events(self, payload: dict[str, Any]) -> AsyncIterator[Any]:
        if self._raw_http_client is None:
            raise RuntimeError("Responses HTTP client is not initialized")

        async with self._raw_http_client.stream(
            "POST",
            self._responses_url(),
            headers=self._responses_headers(),
            json=_strip_openai_unsupported_fields(payload),
            timeout=_CHAT_HTTP_TIMEOUT_SECONDS,
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
        for attempt in range(2):
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
                        logger.warning(
                            "Responses HTTP stream rejected reasoning summary/content request; retrying without it: %s",
                            exc,
                        )
                        payload = retry_payload
                        stripped_reasoning_visibility = True
                        continue
                if not yielded_any and attempt == 0 and _is_transient_gateway_error(exc):
                    logger.warning(
                        "Responses HTTP stream transient failure, retrying once: %s",
                        exc,
                    )
                    await asyncio.sleep(_ADAPTER_RETRY_DELAY_SECONDS)
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
        kwargs: dict[str, Any] = {
            "model": _response_tool_model(model),
            "input": api_input,
            "stream": True,
            "store": False,
        }
        if instructions:
            kwargs["instructions"] = instructions
        request_metadata = sanitize_llm_request_metadata(metadata)
        if request_metadata:
            kwargs["metadata"] = request_metadata
        prompt_cache_key = _responses_prompt_cache_key(
            self._settings,
            instructions,
            request_metadata,
        )
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key

        # 添加工具定义
        has_response_tools = False
        if tools:
            responses_tools = self._convert_tools_to_responses_format(tools)
            kwargs["tools"] = responses_tools
            has_response_tools = bool(responses_tools)
        if _is_image_model(model) or _is_image_generation_prompt(messages):
            kwargs["tools"] = [
                *(kwargs.get("tools") or []),
                _image_generation_tool(model),
            ]
            has_response_tools = True

        # Ask every Responses-compatible gateway for provider-visible reasoning
        # summaries. Unsupported gateways are retried without these optional
        # fields by _create_responses_with_retry.
        kwargs["reasoning"] = _responses_reasoning_request(
            self._settings,
            has_tools=has_response_tools,
        )

        try:
            stream = await self._create_responses_with_retry(kwargs)
        except Exception as exc:
            logger.error("Responses API 调用失败: %s", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=_adapter_error_content("LLM API 调用失败", exc),
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
                input_items=api_input,
                prompt_cache_key=str(kwargs.get("prompt_cache_key") or ""),
                previous_response_id=str(kwargs.get("previous_response_id") or ""),
                request_params=kwargs,
            ),
            "safety": _provider_trace_safety(),
            "provider_timeline": provider_timeline,
        }
        finish_reason = ""

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
                    call_id = str(getattr(event, "call_id", "") or item_id).strip()
                    slot = response_tool_items.get(call_id) or response_tool_items.get(item_id) or {}
                    name = str(getattr(event, "name", "") or slot.get("name", "")).strip()
                    arguments_str = getattr(event, "arguments", "{}")

                    try:
                        arguments = json.loads(arguments_str)
                    except (json.JSONDecodeError, TypeError):
                        from backend.llm.json_repair import repair_tool_json
                        arguments = repair_tool_json(arguments_str) or {"_raw": arguments_str}
                    tool_call = ToolCallEvent(
                        id=call_id,
                        name=name,
                        arguments=arguments,
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
                        raw_done.update({
                            "provider": "openai_responses",
                            "model": kwargs["model"],
                            "event_type": event_type,
                            "finish_reason": finish_reason,
                            "usage": _raw_usage_metadata(usage_obj),
                        })
                        if output_items:
                            raw_done["output_items"] = output_items
                        raw_done["safety"] = _provider_trace_safety(output_items)
                elif event_type == "response.incomplete":
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
        except Exception as exc:
            logger.error("Responses API 流式解析异常: %s", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=f"LLM 流式响应异常: {_clean_error_message(exc)}",
            )
            return

        # 输出聚合的 tool_calls
        if pending_tool_calls:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=pending_tool_calls,
            )

        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=finish_reason, raw=raw_done)

    async def _simple_responses_api(self, messages: list[LLMMessage]) -> str:
        """Responses API 非流式调用。"""
        instructions, input_messages = _split_responses_instructions(messages)
        api_input = self._build_responses_input(input_messages)

        model = self._settings.model
        kwargs: dict[str, Any] = {
            "model": _response_tool_model(model),
            "input": api_input,
            "store": False,
        }
        if instructions:
            kwargs["instructions"] = instructions
        prompt_cache_key = _responses_prompt_cache_key(
            self._settings,
            instructions,
            None,
        )
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key

        if _is_image_model(model) or _is_image_generation_prompt(messages):
            kwargs["tools"] = [_image_generation_tool(model)]

        kwargs["reasoning"] = _responses_reasoning_request(self._settings)

        try:
            response = await self._create_responses_with_retry(kwargs)
        except Exception as exc:
            logger.error("Responses API simple_chat 失败: %s", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

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
            if msg.role in {"system", "developer"}:
                continue
            elif msg.role == "user":
                if msg.images or msg.documents:
                    parts: list[dict[str, Any]] = []
                    if msg.content:
                        parts.append({"type": "input_text", "text": msg.content})
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
                        "content": parts or msg.content,
                    })
                else:
                    result.append({
                        "role": "user",
                        "content": msg.content,
                    })
            elif msg.role == "assistant":
                if msg.content:
                    result.append({
                        "role": "assistant",
                        "content": msg.content,
                    })
                if msg.tool_calls:
                    # Responses represents assistant text and function calls as
                    # adjacent items. Keep the short pre-tool narration in
                    # history instead of dropping it when tool_calls are present.
                    for tc in msg.tool_calls:
                        result.append({
                            "type": "function_call",
                            "id": tc.id,
                            "call_id": tc.id,
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        })
            elif msg.role == "tool":
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
                "strict": False,
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
            function_def.pop("strict", None)
            function_def["parameters"] = _normalize_schema_for_openai(
                function_def.get("parameters", {})
            )
            normalized_tool["function"] = function_def
            normalized_tools.append(normalized_tool)
        return normalized_tools

    def _chat_completions_url(self) -> str:
        base_url = (self._settings.base_url or "https://api.openai.com/v1").rstrip("/")
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
        tool_prefetch_emitted = False
        reasoning_splitter = _ReasoningSplitter()

        async with self._raw_http_client.stream(
            "POST",
            self._chat_completions_url(),
            headers=self._chat_headers(),
            json=_strip_openai_unsupported_fields(payload),
            timeout=_CHAT_HTTP_TIMEOUT_SECONDS,
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
                    _append_provider_timeline(provider_timeline, "chat.done")
                    break

                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring malformed chat stream line: %s", line[:200])
                    continue

                usage_obj = chunk.get("usage")
                raw_text_delta = _raw_text_delta_metadata(
                    "openai_chat_completions",
                    usage_obj=usage_obj,
                )
                if usage_obj:
                    usage = UsageInfo(
                        input_tokens=_get_usage_field(usage_obj, "prompt_tokens"),
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

                for tool_call in delta.get("tool_calls") or []:
                    idx = int(tool_call.get("index") or 0)
                    is_new, _key, slot = accumulator.feed(tool_call, idx)
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.tool_call.delta",
                        index=idx,
                        call_id=slot.get("id"),
                        name=slot.get("name"),
                        arguments_chars=len(str(slot.get("arguments") or "")),
                    )
                    if is_new and slot["id"] and slot["name"]:
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_START,
                            tool_call_start=ToolCallStartEvent(
                                id=slot["id"], name=slot["name"], index=idx,
                            ),
                        )
                    elif not is_new and slot["_delta_bytes"] >= _DELTA_DEBOUNCE_BYTES:
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
                        prefetch_event = _prefetch_tool_call_event(accumulator.finalize())
                        if prefetch_event is not None:
                            yield prefetch_event
                            tool_prefetch_emitted = True
                    continue

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
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        async def emit(payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
            async for event in self._emit_chat_http_stream_events(
                payload,
                _safe_request_summary(
                    model=self._settings.model,
                    wire_api="chat",
                    tools=payload.get("tools") if isinstance(payload.get("tools"), list) else [],
                    request_metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                    messages=messages,
                    request_params=payload,
                ),
            ):
                yield event

        async def minimal_payload() -> dict[str, Any]:
            return {
                "model": self._settings.model,
                "messages": _minimal_chat_messages(messages),
                "stream": True,
                **_chat_max_tokens_kwargs(self._settings),
            }

        try:
            async for event in emit(kwargs):
                yield event
            return
        except Exception as exc:
            if _is_request_metadata_unsupported_error(exc):
                logger.warning(
                    "Chat Completions HTTP rejected request metadata; retrying without it: %s",
                    exc,
                )
                retry_kwargs = _strip_request_metadata(kwargs)
                try:
                    async for event in emit(retry_kwargs):
                        yield event
                    return
                except Exception as retry_exc:
                    exc = retry_exc

            if _is_reasoning_visibility_unsupported_error(exc):
                logger.warning(
                    "Chat Completions HTTP rejected reasoning summary/content request; retrying without it: %s",
                    exc,
                )
                retry_kwargs = _strip_reasoning_visibility_request(kwargs)
                try:
                    async for event in emit(retry_kwargs):
                        yield event
                    return
                except Exception as retry_exc:
                    exc = retry_exc

            if tools and (_is_invalid_tool_schema_error(exc) or _is_blocked_gateway_error(exc)):
                logger.warning(
                    "Chat Completions HTTP rejected the tool-enabled request, retrying without tools: %s",
                    exc,
                )
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("tools", None)
                retry_kwargs.pop("tool_choice", None)
                try:
                    async for event in emit(retry_kwargs):
                        yield event
                    return
                except Exception as retry_exc:
                    if _is_blocked_gateway_error(retry_exc):
                        try:
                            async for event in emit(await minimal_payload()):
                                yield event
                            return
                        except Exception as minimal_exc:
                            exc = minimal_exc
                    else:
                        exc = retry_exc
            elif _is_blocked_gateway_error(exc):
                try:
                    async for event in emit(await minimal_payload()):
                        yield event
                    return
                except Exception as minimal_exc:
                    exc = minimal_exc

            _log_chat_provider_error(self._settings, "Chat Completions HTTP API", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=_adapter_error_content("LLM API 调用失败", exc),
            )

    async def _simple_chat_completions_http(self, messages: list[LLMMessage]) -> str:
        if self._raw_http_client is None:
            raise RuntimeError("Chat HTTP client is not initialized")

        openai_messages = [
            msg.to_openai_message()
            for msg in messages
        ]
        payload = {
            "model": self._settings.model,
            "messages": openai_messages,
            "stream": False,
            **_chat_max_tokens_kwargs(self._settings),
        }

        try:
            response = await self._raw_http_client.post(
                self._chat_completions_url(),
                headers=self._chat_headers(),
                json=_strip_openai_unsupported_fields(payload),
                timeout=_CHAT_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except Exception as exc:
            _log_chat_provider_error(self._settings, "Chat Completions HTTP simple_chat", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        data = response.json()
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
        openai_messages = [
            msg.to_openai_message()
            for msg in messages
        ]
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
        if request_metadata:
            kwargs["metadata"] = request_metadata
            kwargs["store"] = False
        kwargs.update(_chat_reasoning_visibility_request(self._settings))

        if tools:
            kwargs["tools"] = self._normalize_chat_tools(tools)
            kwargs["tool_choice"] = "auto"

        chat_request_summary = _safe_request_summary(
            model=self._settings.model,
            wire_api="chat",
            tools=kwargs.get("tools") if isinstance(kwargs.get("tools"), list) else [],
            request_metadata=request_metadata,
            messages=messages,
            request_params=kwargs,
        )

        if self._use_raw_chat_http:
            async for event in self._stream_chat_completions_http(kwargs, messages, tools):
                yield event
            return

        async def create_minimal_stream(error: Exception) -> Any:
            logger.warning(
                "Chat Completions blocked the full agent prompt, retrying with a minimal prompt: %s",
                error,
            )
            return await self._client.chat.completions.create(
                model=self._settings.model,
                messages=_minimal_chat_messages(messages),
                stream=True,
                **_chat_max_tokens_kwargs(self._settings),
            )

        stream: Any | None = None
        try:
            stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(kwargs))
        except Exception as create_exc:
            pending_exc: Exception = create_exc
            if _is_request_metadata_unsupported_error(pending_exc):
                logger.warning(
                    "Chat Completions rejected request metadata; retrying without it: %s",
                    pending_exc,
                )
                retry_kwargs = _strip_request_metadata(kwargs)
                try:
                    stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(retry_kwargs))
                except Exception as retry_exc:
                    pending_exc = retry_exc
            if _is_reasoning_visibility_unsupported_error(pending_exc):
                logger.warning(
                    "Chat Completions rejected reasoning summary/content request; retrying without it: %s",
                    pending_exc,
                )
                retry_kwargs = _strip_reasoning_visibility_request(kwargs)
                try:
                    stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(retry_kwargs))
                except Exception as retry_exc:
                    pending_exc = retry_exc
            if stream is not None:
                pass
            elif tools and (_is_invalid_tool_schema_error(pending_exc) or _is_blocked_gateway_error(pending_exc)):
                logger.warning(
                    "Chat Completions rejected the tool-enabled request, retrying without tools: %s",
                    pending_exc,
                )
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("tools", None)
                retry_kwargs.pop("tool_choice", None)
                try:
                    stream = await self._client.chat.completions.create(**_strip_openai_unsupported_fields(retry_kwargs))
                except Exception as retry_exc:
                    if _is_blocked_gateway_error(retry_exc):
                        try:
                            stream = await create_minimal_stream(retry_exc)
                        except Exception as minimal_exc:
                            _log_chat_provider_error(self._settings, "Chat Completions API minimal retry", minimal_exc)
                            yield StreamEvent(
                                type=StreamEventType.ERROR,
                                content=_adapter_error_content("LLM API 调用失败", minimal_exc),
                            )
                            return
                    else:
                        _log_chat_provider_error(self._settings, "Chat Completions API tool-free retry", retry_exc)
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=_adapter_error_content("LLM API 调用失败", retry_exc),
                        )
                        return
            else:
                if _is_blocked_gateway_error(pending_exc):
                    try:
                        stream = await create_minimal_stream(pending_exc)
                    except Exception as minimal_exc:
                        _log_chat_provider_error(self._settings, "Chat Completions API minimal retry", minimal_exc)
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=_adapter_error_content("LLM API 调用失败", minimal_exc),
                        )
                        return
                else:
                    _log_chat_provider_error(self._settings, "Chat Completions API", pending_exc)
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=_adapter_error_content("LLM API 调用失败", pending_exc),
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
                            input_tokens=_get_usage_field(chunk.usage, "prompt_tokens"),
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
                    for tc in delta.tool_calls:
                        idx = int(tc.index) if tc.index is not None else 0
                        tc_dict = {
                            "id": tc.id or "",
                            "function": {
                                "name": tc.function.name if tc.function else "",
                                "arguments": tc.function.arguments if tc.function else "",
                            },
                        }
                        is_new, _key, slot = accumulator.feed(tc_dict, idx)
                        _append_provider_timeline(
                            provider_timeline,
                            "chat.tool_call.delta",
                            index=idx,
                            call_id=slot.get("id"),
                            name=slot.get("name"),
                            arguments_chars=len(str(slot.get("arguments") or "")),
                        )
                        if is_new and slot["id"] and slot["name"]:
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_start=ToolCallStartEvent(
                                    id=slot["id"], name=slot["name"], index=idx,
                                ),
                            )
                        elif not is_new and slot["_delta_bytes"] >= _DELTA_DEBOUNCE_BYTES:
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
                        prefetch_event = _prefetch_tool_call_event(accumulator.finalize())
                        if prefetch_event is not None:
                            yield prefetch_event
                            tool_prefetch_emitted = True
                    continue
        except Exception as exc:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=f"Stream interrupted: {_clean_error_message(exc)}",
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

    async def _simple_chat_completions(self, messages: list[LLMMessage]) -> str:
        """Chat Completions API 非流式调用。"""
        if self._use_raw_chat_http:
            return await self._simple_chat_completions_http(messages)

        openai_messages = [
            msg.to_openai_message()
            for msg in messages
        ]

        try:
            response = await self._client.chat.completions.create(**_strip_openai_unsupported_fields({
                "model": self._settings.model,
                "messages": openai_messages,
                **_chat_max_tokens_kwargs(self._settings),
            }))
        except Exception as exc:
            _log_chat_provider_error(self._settings, "Chat Completions simple_chat", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()

        raise RuntimeError("LLM 返回空内容")
