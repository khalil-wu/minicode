"""
OpenAI 适配器（DESIGN.md §一 LLM Adapter）。

支持两种 wire API：
  - "responses": OpenAI Responses API（client.responses.create）
  - "chat":      OpenAI Chat Completions API（client.chat.completions.create）

根据 config.wire_api 自动选择。
兼容 OpenAI 及所有兼容 API（Lucen、vLLM、LiteLLM、OpenRouter 等）。
"""

from __future__ import annotations

import base64
import binascii
import asyncio
import json
import logging
import math
import re
import time
from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx

from backend.config import LLMSettings
from backend.agent.lifecycle_errors import LifecycleStaleError
from backend.agent.provider_lifecycle import LIFECYCLE_RUNTIME_METADATA_KEY
from backend.agent.prompting import (
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    split_sys_prompt_prefix,
)
from backend.llm.errors import (
    classify_llm_error,
    llm_error_raw as _adapter_error_raw,
    _provider_error_body_details,
    _provider_response_body,
    _safe_provider_diagnostic_text,
)
from backend.llm.base import (
    emit_provider_lifecycle_request,
    LLMAdapter,
    LLMSideCallContext,
    LLMMessage,
    ProviderActivityEvent,
    SideQueryOptions,
    StreamEvent,
    StreamEventType,
    ToolCallDeltaEvent,
    ToolCallEvent,
    ToolCallStartEvent,
    UsageInfo,
    clamp_max_tokens_to_context,
    emit_provider_lifecycle_headers,
    emit_provider_lifecycle_response,
    sanitize_llm_request_metadata,
)
from backend.llm.capabilities import (
    ProviderCapabilities,
    capabilities_from_openai_settings,
    is_gpt_image_model,
)
from backend.llm.openai_trace import (
    _CHAT_TOOL_FINISH_REASONS,
    _RESPONSES_MAX_OUTPUT_REASONS,
    _append_provider_timeline,
    _get_attr_or_item,
    _is_instruction_role,
    _json_fingerprint,
    _message_content_text,
    _provider_trace_safety,
    _response_finish_reason,
    _response_timeline_fields,
    _responses_reasoning_summary,
    _responses_safe_provider_value,
    _safe_request_summary,
    _safe_timeline_string,
    _short_sha256,
)

from backend.llm.openai_errors import (
    _clean_error_message,
    _error_status_code,
    _error_text,
)
from backend.llm.openai_payloads import (
    _normalize_schema_for_openai,
    strict_schema_for_openai,
    _strip_openai_unsupported_fields,
)
from backend.llm.proxy_policy import (
    provider_proxy_url_for_base_url,
)
from backend.llm.openai_streaming import (
    _ReasoningSplitter,
    _ToolCallAccumulator,
    _splitter_events,
)
from backend.llm.sse import SSEMalformedBudget, iter_sse_data
from backend.llm.openai_usage import (
    _get_cached_prompt_tokens,
    _get_cache_creation_prompt_tokens,
    _get_chat_prompt_tokens,
    _get_reasoning_output_tokens,
    _get_usage_cost_usd,
    _get_usage_field,
    _raw_text_delta_metadata,
    _raw_usage_metadata,
)
from backend.tools.catalog import canonicalize_tool_schemas
from backend.llm.reasoning_effort import normalize_reasoning_effort
from backend.llm.provider_contracts import ReasoningPolicy
from backend.permissions.network import (
    actual_peer_network_error,
    assess_network_url,
)

logger = logging.getLogger(__name__)

_DELTA_DEBOUNCE_BYTES = 128

# ChatML/ChatGLM special tokens some gateways leak into content.
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]*\|>")


_OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH = 64
_OPENAI_EXPLICIT_PROMPT_CACHE_MIN_TOKENS = 1_024


def _estimate_prompt_tokens_for_cache(text: str) -> int:
    """Approximate rendered prompt tokens using Codex's UTF-8 byte heuristic.

    Codex's ``approx_token_count`` divides UTF-8 byte length by four (rounded
    up), rather than counting Unicode code points.  That distinction matters
    for CJK text and emoji: a code-point estimate can incorrectly keep a
    prompt below OpenAI's 1,024-token explicit-breakpoint minimum.  This is
    only an eligibility estimate; the provider remains authoritative for the
    actual token count and cache behavior.
    """

    if not text:
        return 0
    byte_length = len(text.encode("utf-8"))
    return (byte_length + 3) // 4


def _reject_dedicated_image_agent_model(model: str) -> None:
    """Keep GPT Image models on the upstream Images API boundary.

    Codex calls the Images API for ``gpt-image-*`` models and Pi registers them
    in a separate ImagesProvider. OpenAI Responses image generation instead
    selects a mainline text model plus the built-in ``image_generation`` tool.
    Sending a GPT Image model through the text/agent Responses contract is
    therefore invalid and must fail before any provider request is made.
    """

    if is_gpt_image_model(model):
        raise RuntimeError(
            "provider_error_type=unsupported_capability: "
            f"{model} is a dedicated Images API model and cannot be selected "
            "as the text/agent model"
        )


def _strip_special_tokens(text: str) -> str:
    """Remove leaked <|im_start|>/<|im_end|>/<|endoftext|>/<|user|>/... markers."""
    if not text or "<|" not in text:
        return text
    return _SPECIAL_TOKEN_RE.sub("", text)


# pi openai-completions.ts useMaxTokens: gateways that still require the
# legacy field; everything else (OpenAI, o-series, gpt-5) rejects plain
# max_tokens and needs max_completion_tokens.
_USE_LEGACY_MAX_TOKENS_BASEMARKS = (
    "chutes.ai",
    "api.deepseek.com",
    "api.moonshot.cn",
    "api.together.xyz",
    "api.nvidia.com",
    "api.lingbanpark.club",
    "api.z.ai",
)
_RESPONSES_MIN_MAX_OUTPUT_TOKENS = 16
_REMOTE_IMAGE_MAX_BYTES = 7_500_000
_REMOTE_IMAGE_MAX_REDIRECTS = 4


def _positive_token_limit(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        return 0
    return max(1, int(value))


def _resolved_responses_max_output_tokens(value: Any) -> int | None:
    """Resolve an explicit Responses cap while preserving Auto/Unset.

    MiniCode persists ``0`` as Auto. The Responses API treats omission as
    automatic provider/model selection, while positive legacy values below
    the protocol minimum must be lifted before any network request is made.
    """

    explicit = _positive_token_limit(value)
    if explicit <= 0:
        return None
    return max(_RESPONSES_MIN_MAX_OUTPUT_TOKENS, explicit)


def _clamped_responses_max_output_tokens(
    *,
    value: Any,
    context_window: Any,
    messages: list[LLMMessage],
    tools: list[dict[str, Any]] | None,
) -> int | None:
    explicit = _resolved_responses_max_output_tokens(value)
    if explicit is None:
        return None
    clamped = clamp_max_tokens_to_context(
        context_window=context_window,
        messages=messages,
        tools=tools,
        max_tokens=explicit,
    )
    # The context clamp is intentionally generic and may return one when the
    # estimated window is exhausted. Responses rejects values below sixteen,
    # so keep the wire payload protocol-valid and let the provider report an
    # actual context-window error if the estimate leaves less room than that.
    return max(_RESPONSES_MIN_MAX_OUTPUT_TOKENS, int(clamped))


def _chat_max_tokens_field(settings: LLMSettings, *, model: str | None = None) -> str:
    base_url = str(getattr(settings, "base_url", "") or "").lower()
    model_id = str(
        model if model is not None else getattr(settings, "model", "") or ""
    ).lower()
    if any(mark in base_url for mark in _USE_LEGACY_MAX_TOKENS_BASEMARKS):
        return "max_tokens"
    if model_id.startswith(("o1", "o3", "o4", "gpt-5")):
        return "max_completion_tokens"
    if str(getattr(settings, "provider", "") or "").strip().lower() == "custom":
        return "max_tokens"
    # pi's compat table only assigns the legacy field to known gateways; the
    # OpenAI API itself now rejects max_tokens on reasoning models.
    return "max_completion_tokens"


def _resolved_chat_max_tokens(
    settings: LLMSettings,
    *,
    model: str,
    requested_max_tokens: Any = None,
) -> int:
    """Resolve a safe explicit Chat output cap without borrowing side-model metadata."""

    explicit_request = _positive_token_limit(requested_max_tokens)
    if explicit_request:
        return explicit_request
    configured = _positive_token_limit(getattr(settings, "max_tokens", 0))
    if configured:
        return configured
    # ``0`` is MiniCode's Auto/Unset value.  Chat-compatible providers do not
    # share one portable default field or limit, so omission is the only safe
    # wire representation.  Positive explicit requests and verified provider
    # metadata above remain authoritative.
    return 0


def _prefetch_tool_call_event(tool_calls: list[ToolCallEvent]) -> StreamEvent | None:
    if not tool_calls:
        return None
    return StreamEvent(
        type=StreamEventType.TOOL_CALL,
        tool_calls=list(tool_calls),
        tool_calls_final=False,
    )


# Optional OpenAI chat fields MiniCode adds for tracing and prompt-cache
# affinity. They carry no answer content, so a gateway that rejects one can be
# served by dropping it rather than failing the turn.
_OPTIONAL_CHAT_FIELDS = ("metadata", "store", "prompt_cache_key", "stream_options")
_UNSUPPORTED_ARGUMENT_MARKERS = (
    "unrecognized request argument",
    "unknown parameter",
    "unsupported parameter",
    "unexpected keyword",
    "extra inputs are not permitted",
    "unknown field",
    "not supported",
)


def _without_unsupported_chat_fields(
    payload: dict[str, Any],
    unsupported: set[str],
) -> dict[str, Any]:
    if not unsupported:
        return dict(payload)
    return {key: value for key, value in payload.items() if key not in unsupported}


def _rejected_optional_chat_field(exc: Exception, payload: dict[str, Any]) -> str:
    """Return the optional field a gateway refused, or "" when unrelated."""

    if _error_status_code(exc) not in {400, 422}:
        return ""
    text = _error_text(exc)
    if not any(marker in text for marker in _UNSUPPORTED_ARGUMENT_MARKERS):
        return ""
    for field in _OPTIONAL_CHAT_FIELDS:
        if field in payload and field in text:
            return field
    return ""


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


def _declared_reasoning_effort_levels_for_model(
    settings: LLMSettings,
    model: str | None = None,
) -> tuple[str, ...]:
    """Return capabilities only for the model they were resolved from.

    ``LLMSettings.reasoning_effort_levels`` describes ``settings.model``. Side
    calls may use ``small_fast_model`` instead; inheriting the primary model's
    levels would make an undeclared wire parameter look supported. Pi and Codex
    both attach reasoning capabilities to an individual model definition, so a
    different model remains unknown until its own metadata is available.
    """

    selected_model = str(settings.model or "").strip()
    wire_model = str(model or selected_model).strip()
    # An explicit capability list is also the compatibility contract for
    # direct/embedded adapter construction, where the caller intentionally
    # leaves ``settings.model`` empty. Codex attaches capabilities to a model
    # record when one is selected; it does not discard an explicitly supplied
    # record merely because a host has not named the model yet. A *different*
    # selected wire model remains unknown and must not inherit these levels.
    if selected_model and wire_model != selected_model:
        return ()
    return tuple(getattr(settings, "reasoning_effort_levels", ()) or ())


def _responses_reasoning_effort(
    settings: LLMSettings,
    *,
    model: str | None = None,
    effort: str | None = None,
    has_tools: bool = False,
) -> str:
    del has_tools
    wire_model = str(model or settings.model or "").strip()
    return normalize_reasoning_effort(
        wire_model,
        settings.wire_api,
        settings.reasoning_effort if effort is None else effort,
        _declared_reasoning_effort_levels_for_model(settings, wire_model),
        getattr(settings, "default_reasoning_effort", ""),
    )


def _chat_reasoning_effort(
    settings: LLMSettings,
    *,
    model: str | None = None,
) -> str:
    """Return Pi's OpenAI-compatible Chat reasoning-effort parameter.

    Provider-specific thinking formats need an explicit compatibility record.
    The generic Chat adapter follows Pi's OpenAI-style fallback and emits
    ``reasoning_effort`` only when the selected model declares that capability.
    """
    wire_model = str(model or settings.model or "").strip()
    levels = _declared_reasoning_effort_levels_for_model(settings, wire_model)
    if not levels:
        return ""
    requested = str(settings.reasoning_effort or "").strip().lower()
    if not requested:
        requested = (
            str(getattr(settings, "default_reasoning_effort", "") or "").strip().lower()
        )
    return requested if requested in levels else ""


def _prompt_cache_retention_request(settings: LLMSettings) -> str:
    retention = (
        str(getattr(settings, "prompt_cache_retention", "") or "").strip().lower()
    )
    return retention if retention in {"24h", "in_memory"} else ""


def _responses_reasoning_request(
    settings: LLMSettings,
    *,
    model: str | None = None,
    has_tools: bool = False,
) -> dict[str, Any]:
    """Request provider-visible reasoning summaries whenever the gateway allows it."""
    reasoning: dict[str, Any] = {}
    summary = _responses_reasoning_summary(settings, model=model)
    if summary:
        reasoning["summary"] = summary
    effort = _responses_reasoning_effort(
        settings,
        model=model,
        has_tools=has_tools,
    )
    if effort:
        reasoning["effort"] = effort
    return reasoning


def _responses_disabled_reasoning_request(
    settings: LLMSettings,
    *,
    model: str | None = None,
) -> dict[str, str]:
    """Disable reasoning only when the actual wire model declares ``none``."""

    effort = _responses_reasoning_effort(
        settings,
        model=model,
        effort="none",
    )
    return {"effort": effort} if effort else {}


def _responses_include_request(
    settings: LLMSettings,
    *,
    has_tools: bool = False,
    reasoning_request: dict[str, Any] | None = None,
) -> list[str]:
    """Match Codex and Pi's stateless Responses reasoning replay request.

    Codex requests encrypted reasoning on every Responses HTTP turn and Pi
    round-trips provider-native reasoning items with ``store: false``.
    MiniCode sends this optional field as part of the explicit Responses
    request contract; provider rejection is surfaced to the caller.
    """
    del settings, has_tools, reasoning_request
    return ["reasoning.encrypted_content"]


def _chat_tool_protocol_error(
    finish_reason: str,
    tool_calls: list[ToolCallEvent],
) -> str:
    reason = str(finish_reason or "").strip().lower()
    tool_reasons = _CHAT_TOOL_FINISH_REASONS
    if tool_calls and reason not in tool_reasons | _RESPONSES_MAX_OUTPUT_REASONS:
        return (
            "OpenAI Chat returned executable tool calls with an incompatible "
            f"finish_reason ({reason or 'missing'})."
        )
    if not tool_calls and reason in tool_reasons:
        return (
            f"OpenAI Chat ended with finish_reason={reason} but returned no "
            "complete tool calls."
        )
    return ""


def _chat_incomplete_tool_call_error(
    dropped: list[dict[str, Any]],
    *,
    tool_call_count: int,
    finish_reason: str,
    request_summary: dict[str, Any] | None,
) -> StreamEvent | None:
    """Refuse a Chat turn whose streamed tool batch lost a call.

    ``_chat_tool_protocol_error`` only notices a *fully* empty batch, so two
    parallel calls where one never received an id would execute half of what
    the model asked for. The Responses path already fails closed here
    (``terminal_function_call_missing``); keep Chat identical.

    Returns ``None`` when nothing executable was lost. A gateway that emits a
    stray malformed tool frame and then answers in text with
    ``finish_reason=stop`` produced no tool batch at all — that is a provider
    quirk to discard, not a partially-executed turn, and failing it would
    throw away a valid answer.
    """

    if not dropped:
        return None
    reason = str(finish_reason or "").strip().lower()
    if tool_call_count == 0 and reason not in _CHAT_TOOL_FINISH_REASONS:
        return None
    return StreamEvent(
        type=StreamEventType.ERROR,
        content=(
            f"OpenAI Chat streamed {len(dropped)} tool call(s) without an id or "
            "name; refusing to execute a partial tool batch."
        ),
        raw={
            "provider": "openai_chat_completions",
            "event_type": "incomplete_streamed_tool_call",
            "provider_error_type": "protocol",
            "error_type": "api",
            "protocol_error_code": "incomplete_streamed_tool_call",
            "finish_reason": finish_reason,
            "tool_call_count": tool_call_count,
            "dropped_tool_call_count": len(dropped),
            "dropped_tool_calls": dropped,
            "request_summary": request_summary or {},
            "safety": _provider_trace_safety(),
        },
    )


def _extract_url_citations(event: Any) -> list[dict[str, Any]]:
    """Extract url_citation annotations from a response.output_text.done event.

    The OpenAI Responses API attaches citation annotations to the completed
    output text. Each url_citation carries a URL, title, and optional
    start/end indices into the text. We normalize them into a flat list of
    dicts so the frontend can merge them with web-search sources.
    """
    annotations = getattr(event, "annotations", None)
    if not annotations:
        annotation = getattr(event, "annotation", None)
        annotations = [annotation] if annotation is not None else None
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
        error_obj = (
            payload.get("error") if isinstance(payload.get("error"), dict) else payload
        )
        if isinstance(error_obj, dict):
            code = code or str(error_obj.get("code") or "")
            error_type = error_type or str(error_obj.get("type") or "")
    return (
        _safe_provider_diagnostic_text(code, limit=160),
        _safe_provider_diagnostic_text(error_type, limit=160),
    )


def _truncate_provider_body(body: str) -> str:
    compact = _safe_provider_diagnostic_text(
        re.sub(r"\s+", " ", body or "").strip(),
        limit=_PROVIDER_ERROR_BODY_LOG_LIMIT + 1,
    )
    if len(compact) > _PROVIDER_ERROR_BODY_LOG_LIMIT:
        return compact[:_PROVIDER_ERROR_BODY_LOG_LIMIT] + "..."
    return compact


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
    message = _safe_provider_diagnostic_text(_clean_error_message(exc), limit=240)
    if not message:
        body_details = _provider_error_body_details(_provider_response_body(exc))
        message = str(body_details.get("message") or "provider request failed")
    return f"{prefix}: {message}{suffix}"


@asynccontextmanager
async def _openai_http_stream(
    client: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, Any],
    json_payload: dict[str, Any],
) -> AsyncIterator[Any]:
    """Open a raw stream with an explicit response close boundary.

    ``httpx.AsyncClient.stream`` is implemented as an async-generator context
    manager. If an outer consumer returns immediately after a provider ``error``
    event, closing that nested generator during ``GeneratorExit`` can produce
    ``RuntimeError: generator didn't stop after athrow()``. The production
    client uses ``send(..., stream=True)`` so the response is closed directly;
    lightweight test doubles retain the older context-manager contract.
    """

    build_request = getattr(client, "build_request", None)
    send = getattr(client, "send", None)
    if callable(build_request) and callable(send):
        request = build_request(
            method,
            url,
            headers=headers,
            json=json_payload,
            timeout=None,
        )
        response = await send(request, stream=True)
        try:
            yield response
        finally:
            close = getattr(response, "aclose", None)
            if callable(close):
                await close()
        return

    async with client.stream(
        method,
        url,
        headers=headers,
        json=json_payload,
        timeout=None,
    ) as response:
        yield response


async def _close_async_iterator(iterator: Any) -> None:
    """Close an async iterator at every early-return provider boundary."""

    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except (GeneratorExit, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the root error
        logger.debug(
            "Provider async iterator close failed: %s",
            _clean_error_message(exc),
        )


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
    image_items: list[dict[str, Any]] = []
    for image in message.images:
        data = str(image.get("data") or "")
        if not data:
            continue
        media_type = str(image.get("media_type") or "image/png")
        image_items.append(
            {
                "type": "input_image",
                "image_url": f"data:{media_type};base64,{data}",
                "detail": "auto",
            }
        )
    # Match Pi/Codex Responses encoding: text-only function results stay a
    # plain string. Structured content is reserved for multimodal results.
    # This matters for OpenAI-compatible gateways that accept the documented
    # string form but reject an array containing only ``input_text``.
    if image_items:
        output_items: list[dict[str, Any]] = []
        if output:
            output_items.append({"type": "input_text", "text": output})
        output_items.extend(image_items)
        wire_output: str | list[dict[str, Any]] = output_items
    else:
        wire_output = output
    payload: dict[str, Any] = {
        "type": "function_call_output",
        "call_id": call_id,
        "output": wire_output,
    }
    if status:
        payload["status"] = status
    return payload


def _instruction_text_from_responses_input(
    input_items: list[dict[str, Any]] | None,
) -> str:
    """Recover leading developer/system text for redacted request diagnostics.

    GPT-5.6 explicit caching moves stable instructions into developer
    ``input_text`` blocks because the Responses top-level ``instructions``
    field cannot carry a breakpoint. Diagnostics must still hash that logical
    instruction prefix without copying the prompt into the trace.
    """

    if not input_items:
        return ""
    parts: list[str] = []
    explicit_breakpoint_seen = False
    explicit_boundary_inserted = False
    for item in input_items:
        if not isinstance(item, dict) or not _is_instruction_role(item.get("role")):
            break
        content = item.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text_parts = [
                str(block.get("text") or "").strip()
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            text = "\n".join(text_parts).strip()
            if explicit_breakpoint_seen and parts and not explicit_boundary_inserted:
                parts.append(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
                explicit_boundary_inserted = True
        else:
            text = ""
        if text:
            parts.append(text)
        if isinstance(content, list) and any(
            isinstance(block, dict) and "prompt_cache_breakpoint" in block
            for block in content
        ):
            explicit_breakpoint_seen = True
    return "\n\n".join(parts)


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
        chat_messages.insert(
            0, {"role": "system", "content": "\n\n".join(leading_instructions)}
        )
    return chat_messages


def _instruction_text_from_chat_payload(messages: list[dict[str, Any]] | None) -> str:
    if not messages:
        return ""
    parts: list[str] = []
    explicit_breakpoint_seen = False
    explicit_boundary_inserted = False
    for message in messages:
        if not isinstance(message, dict) or not _is_instruction_role(
            message.get("role")
        ):
            break
        content = message.get("content")
        text = _message_content_text(content).strip()
        if not text:
            continue
        has_explicit_breakpoint = isinstance(content, list) and any(
            isinstance(block, dict) and "prompt_cache_breakpoint" in block
            for block in content
        )
        if explicit_breakpoint_seen and parts and not explicit_boundary_inserted:
            parts.append(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
            explicit_boundary_inserted = True
        parts.append(text)
        if has_explicit_breakpoint:
            explicit_breakpoint_seen = True
    return "\n\n".join(parts)


def _chat_payload_input_items(
    messages: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not messages:
        return []
    return [
        message
        for message in messages
        if isinstance(message, dict) and not _is_instruction_role(message.get("role"))
    ]


def _split_responses_instructions(
    messages: list[LLMMessage],
) -> tuple[str, list[LLMMessage]]:
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


def _responses_prompt_cache_key(client_metadata: dict[str, str] | None) -> str:
    """Use the canonical Responses session id as the prompt-cache key.

    Codex sends its session id directly, while Pi clamps that identity to the
    OpenAI maximum of 64 Unicode characters. Provider/model/prompt hashes are
    deliberately excluded because they would create a MiniCode-only contract.
    """

    session_id = str((client_metadata or {}).get("session_id") or "").strip()
    return session_id[:_OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH]


def _is_official_openai_prompt_cache_breakpoint_model(
    settings: LLMSettings,
    model: str,
) -> bool:
    provider = str(getattr(settings, "provider", "") or "").strip().lower()
    host = _provider_host(str(getattr(settings, "base_url", "") or ""))
    terminal_model = str(model or "").strip().lower().split("/")[-1]
    return bool(
        provider == "openai"
        and host == "api.openai.com"
        and terminal_model.startswith("gpt-5.6")
    )


def _responses_explicit_prompt_cache_input(
    instructions: str,
    input_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    split = split_sys_prompt_prefix(instructions)
    stable = split.stable_prefix.strip()
    # OpenAI requires at least 1,024 rendered tokens before an explicit
    # breakpoint.  Keep the provider's implicit prefix cache for short prompts
    # instead of emitting an explicit mode that can never write a valid cache
    # entry.  Use Codex's UTF-8 byte/4 approximation so multilingual stable
    # prefixes are not incorrectly rejected by a code-point estimate.
    if (
        not stable
        or _estimate_prompt_tokens_for_cache(stable)
        < _OPENAI_EXPLICIT_PROMPT_CACHE_MIN_TOKENS
    ):
        return input_items, False
    developer_items: list[dict[str, Any]] = [
        {
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": split.stable_prefix,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    ]
    if split.dynamic_suffix.strip():
        developer_items.append(
            {
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": split.dynamic_suffix,
                    }
                ],
            }
        )
    return [*developer_items, *input_items], True


def _chat_explicit_prompt_cache_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    instruction_text = _instruction_text_from_chat_payload(messages)
    split = split_sys_prompt_prefix(instruction_text)
    stable = split.stable_prefix.strip()
    if (
        not stable
        or _estimate_prompt_tokens_for_cache(stable)
        < _OPENAI_EXPLICIT_PROMPT_CACHE_MIN_TOKENS
    ):
        return messages, False
    non_instruction_messages = [
        dict(message)
        for message in messages
        if not _is_instruction_role(message.get("role"))
    ]
    result: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": split.stable_prefix,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    ]
    if split.dynamic_suffix.strip():
        result.append(
            {
                "role": "system",
                "content": [{"type": "text", "text": split.dynamic_suffix}],
            }
        )
    result.extend(non_instruction_messages)
    return result, True


def _responses_identity_headers(
    client_metadata: dict[str, str] | None,
) -> dict[str, str]:
    """Project Responses identity onto MiniCode's HTTP headers.

    ``session-id``/``thread-id``/``x-client-request-id`` are OpenAI-generic
    identity/caching headers. The ``x-minicode-*`` entries carry MiniCode's
    own diagnostics; values are only sent when the host supplied them.
    """

    metadata = client_metadata or {}
    session_id = _valid_http_header_value(metadata.get("session_id"))
    thread_id = _valid_http_header_value(metadata.get("thread_id"))
    headers: dict[str, str] = {}
    if thread_id:
        headers["x-client-request-id"] = thread_id
    if session_id:
        headers["session-id"] = session_id
    if thread_id:
        headers["thread-id"] = thread_id
    for header_name in (
        "x-minicode-window-id",
        "x-minicode-turn-metadata",
        "x-minicode-parent-thread-id",
        "x-minicode-installation-id",
        "x-openai-subagent",
    ):
        value = _valid_http_header_value(metadata.get(header_name))
        if value is not None:
            headers[header_name] = value
    return headers


def _valid_http_header_value(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if not candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        return None
    return candidate[:512]


def _responses_side_client_metadata(
    options: SideQueryOptions | None,
) -> dict[str, str]:
    """Project Pi's standalone auxiliary-session identity."""

    if options is None:
        return {}
    session_id = str(options.session_id or "").strip()
    if not session_id:
        return {}
    thread_id = str(getattr(options, "thread_id", "") or "").strip() or session_id
    turn_id = str(getattr(options, "turn_id", "") or "").strip()
    metadata = {"session_id": session_id, "thread_id": thread_id}
    if turn_id:
        metadata["turn_id"] = turn_id
    return metadata


def _responses_client_metadata(
    request_metadata: dict[str, str] | None,
) -> dict[str, str]:
    """Project MiniCode turn identity into Codex Responses client metadata.

    Codex transports request identity in ``client_metadata`` rather than the
    public Responses ``metadata`` field. Keep MiniCode's richer local metadata
    local, and forward only the session/thread/turn lineage that Codex itself
    puts at the top level. Explicit Codex compatibility keys are preserved
    when a host supplies them.
    """

    metadata = request_metadata or {}

    def first(*keys: str) -> str:
        for key in keys:
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        return ""

    conversation_id = first("conversation_id")
    agent_mode = first("agent_mode").lower()
    agent_role = first("agent_role").lower()
    is_non_root_agent = (
        agent_mode in {"subagent", "background"}
        or agent_role in {"subagent", "background"}
        or agent_role.startswith("subagent:")
        or bool(first("x-openai-subagent", "subagent_kind"))
    )
    session_id = (
        first("session_id")
        or conversation_id
        or first(
            "minicode_app_session_id",
            "minicode_session_id",
        )
    )
    thread_id = first("thread_id")
    if not thread_id:
        thread_id = (
            first("minicode_task_id", "run_id")
            if is_non_root_agent
            else conversation_id
        )
    thread_id = thread_id or session_id
    turn_id = first(
        "turn_id",
        "assistant_message_id",
        "run_id",
    )

    client_metadata: dict[str, str] = {}
    if session_id:
        client_metadata["session_id"] = session_id
    if thread_id:
        client_metadata["thread_id"] = thread_id
    if turn_id:
        client_metadata["turn_id"] = turn_id

    passthrough = {
        "x-minicode-installation-id": (
            "minicode_installation_id",
            "installation_id",
        ),
        "x-minicode-window-id": (
            "x-minicode-window-id",
            "minicode_window_id",
            "window_id",
        ),
        "x-minicode-parent-thread-id": ("parent_thread_id",),
        "x-minicode-turn-metadata": ("x-minicode-turn-metadata",),
        "x-openai-subagent": ("x-openai-subagent", "subagent_kind"),
        "ws_request_header_traceparent": (
            "ws_request_header_traceparent",
            "traceparent",
        ),
        "ws_request_header_tracestate": (
            "ws_request_header_tracestate",
            "tracestate",
        ),
    }
    for target, sources in passthrough.items():
        value = first(*sources)
        if value:
            client_metadata[target] = value
    if is_non_root_agent and "x-openai-subagent" not in client_metadata:
        client_metadata["x-openai-subagent"] = "collab_spawn"
    return client_metadata


def _responses_normalize_function_arguments(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return str(value or "")[:20_000]
    else:
        parsed = value if value is not None else {}
    try:
        return json.dumps(
            parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
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


def _responses_detach_provider_json(
    value: Any, *, _seen: set[int] | None = None
) -> Any:
    """Detach authoritative Responses continuation data without rewriting it.

    Provider continuation is replay input, not a diagnostic projection.  In
    particular, encrypted reasoning and its summaries must survive capture,
    persistence, and replay byte-for-byte.  Reject non-JSON/cyclic SDK values
    instead of publishing a silently truncated continuation.
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    seen = _seen if _seen is not None else set()
    value_id = id(value)
    if value_id in seen:
        raise TypeError("cyclic provider continuation value")
    seen.add(value_id)
    try:
        if isinstance(value, (list, tuple)):
            return [
                _responses_detach_provider_json(child, _seen=seen) for child in value
            ]
        if isinstance(value, dict):
            detached: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError("provider continuation object key is not a string")
                detached[key] = _responses_detach_provider_json(child, _seen=seen)
            return detached

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump(mode="json", exclude_none=False)
            except TypeError:
                dumped = model_dump()
            return _responses_detach_provider_json(dumped, _seen=seen)

        raw_dict = getattr(value, "__dict__", None)
        if isinstance(raw_dict, dict):
            return _responses_detach_provider_json(raw_dict, _seen=seen)
    finally:
        seen.remove(value_id)

    raise TypeError(f"unsupported provider continuation value: {type(value).__name__}")


def _responses_error_details(
    error: Any,
    *,
    fallback: str,
) -> tuple[str, dict[str, Any]]:
    """Normalize provider Responses error objects into visible diagnostics."""

    safe_value = _responses_safe_provider_value(error)
    mapping = safe_value if isinstance(safe_value, dict) else {}
    nested_error = mapping.get("error")
    if isinstance(nested_error, dict):
        mapping = nested_error

    details: dict[str, Any] = {}
    for key in (
        "message",
        "code",
        "type",
        "param",
        "status",
        "status_code",
        "request_id",
    ):
        value = mapping.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        if key == "message":
            value = _safe_provider_diagnostic_text(value)
        elif isinstance(value, str):
            value = _safe_provider_diagnostic_text(value, limit=160)
        details[key] = value

    message = str(details.get("message") or "").strip()
    if not message and isinstance(error, str):
        message = _safe_provider_diagnostic_text(error)
    if not message and isinstance(error, (int, float, bool)):
        message = str(error)
    if not message:
        message = fallback
    details["message"] = message
    return message, details


def _responses_error_event_raw(
    event_type: str,
    error: Any,
    *,
    fallback: str,
    provider: str = "openai_responses",
) -> tuple[str, dict[str, Any]]:
    message, details = _responses_error_details(error, fallback=fallback)
    classification_parts = [
        str(details.get(key) or "") for key in ("message", "code", "type")
    ]
    for key in ("status", "status_code"):
        value = details.get(key)
        if value is not None:
            classification_parts.append(f"status={value}")
    classification_input = " ".join(classification_parts)
    classification = classify_llm_error(classification_input)
    raw: dict[str, Any] = {
        "provider": provider,
        "event_type": event_type,
        "provider_error_type": classification.provider_error_type,
        "error_type": classification.error_type,
    }
    provider_error_code = str(details.get("code") or "").strip()
    provider_error_schema_type = str(details.get("type") or "").strip()
    status_value = details.get("status_code")
    if status_value is None:
        status_value = details.get("status")
    try:
        if status_value is not None:
            raw["status_code"] = int(status_value)
    except (TypeError, ValueError):
        pass
    if provider_error_code:
        raw["provider_error_code"] = provider_error_code
    if provider_error_schema_type:
        raw["provider_error_schema_type"] = provider_error_schema_type
    if message:
        raw["provider_error_message"] = message
    raw["provider_error"] = details
    return message, raw


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
            if not isinstance(encrypted_content, str):
                return None
            result["encrypted_content"] = encrypted_content
        try:
            detached_summary = _responses_detach_provider_json(summary)
        except (TypeError, ValueError, RecursionError):
            return None
        if isinstance(detached_summary, list) and detached_summary:
            result["summary"] = detached_summary
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
            "arguments": arguments,
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
    for item in output:
        provider_item = _responses_provider_item_from_output(item)
        if provider_item is not None:
            items.append(provider_item)
    return items


def _responses_tool_calls_from_provider_items(
    provider_items: list[dict[str, Any]],
) -> list[ToolCallEvent]:
    """Recover the authoritative final Responses function-call batch."""
    tool_calls: list[ToolCallEvent] = []
    for item in provider_items:
        if str(item.get("type") or "") != "function_call":
            continue
        call_id = str(item.get("call_id") or item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        arguments_text = item.get("arguments")
        if not call_id or not name or not isinstance(arguments_text, str):
            continue
        arguments_repaired = False
        try:
            arguments = json.loads(arguments_text)
        except (json.JSONDecodeError, TypeError):
            from backend.llm.json_repair import repair_tool_json

            arguments = repair_tool_json(arguments_text) or {"_raw": arguments_text}
            arguments_repaired = True
        tool_calls.append(
            ToolCallEvent(
                id=call_id,
                name=name,
                arguments=arguments,
                arguments_repaired=arguments_repaired,
            )
        )
    return tool_calls


def _responses_provider_items_metadata(
    provider_items: list[dict[str, Any]],
) -> dict[str, Any]:
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
    phases: list[str] = []
    for item in output:
        if str(_get_attr_or_item(item, "type", "") or "") != "message":
            continue
        phase = str(_get_attr_or_item(item, "phase", "") or "").strip()
        if phase:
            phases.append(phase[:40])
    for phase in reversed(phases):
        if phase.lower() in {"final", "final_answer"}:
            return phase
    return phases[-1] if phases else ""


def _responses_message_text_from_item(item: Any) -> str:
    content = _get_attr_or_item(item, "content", []) or []
    if not isinstance(content, list):
        return ""
    text_parts: list[str] = []
    for part in content:
        part_type = str(_get_attr_or_item(part, "type", "") or "")
        if part_type in {"output_text", "text"}:
            text = _get_attr_or_item(part, "text", "")
        elif part_type == "refusal":
            text = _get_attr_or_item(part, "refusal", "")
        else:
            continue
        if isinstance(text, str) and text:
            text_parts.append(text)
    return "".join(text_parts)


def _json_to_namespace(value: Any) -> Any:
    """Recursively expose raw HTTP JSON with SDK-like attributes."""
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: _json_to_namespace(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_json_to_namespace(item) for item in value]
    return value


def _extract_image_result(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if depth > 6:
        return ""
    seen_ids = seen if seen is not None else set()
    marker = id(value)
    if marker in seen_ids:
        return ""
    seen_ids.add(marker)
    if isinstance(value, (list, tuple)):
        for item in value:
            extracted = _extract_image_result(
                item,
                depth=depth + 1,
                seen=seen_ids,
            )
            if extracted:
                return extracted
        return ""
    for key in ("result", "image_data", "b64_json", "data", "url"):
        candidate = _get_attr_or_item(value, key)
        extracted = _extract_image_result(
            candidate,
            depth=depth + 1,
            seen=seen_ids,
        )
        if extracted:
            return extracted
    return ""


def _extract_response_images(response: Any) -> list[str]:
    images: list[str] = []

    def append_image(value: str) -> None:
        if value and value not in images:
            images.append(value)

    output = _get_attr_or_item(response, "output", []) or []
    for item in output:
        item_type = str(_get_attr_or_item(item, "type", ""))
        if item_type == "image_generation_call":
            image = _extract_image_result(item)
            append_image(image)
            continue
        for content in _get_attr_or_item(item, "content", []) or []:
            content_type = str(_get_attr_or_item(content, "type", ""))
            if content_type in {"output_image", "image"}:
                image = _extract_image_result(content)
                append_image(image)
    return images


_GENERATED_IMAGE_MEDIA_TYPES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}


def _generated_image_media_type(decoded: bytes) -> str:
    if len(decoded) >= 12 and decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP":
        return "image/webp"
    for media_type, signatures in _GENERATED_IMAGE_MEDIA_TYPES.items():
        if any(decoded.startswith(signature) for signature in signatures):
            return media_type
    raise ValueError("Images API returned bytes with an unsupported image format")


def _decode_images_api_value(value: str) -> tuple[str, str]:
    """Normalize one provider image into validated base64 plus media type."""

    candidate = str(value or "").strip()
    if not candidate:
        raise ValueError("Images API returned an empty image payload")
    declared_media_type = ""
    encoded = candidate
    if candidate.lower().startswith("data:"):
        header, separator, body = candidate.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ValueError("Images API returned a non-base64 data URL")
        declared_media_type = header[5:].split(";", 1)[0].strip().lower()
        encoded = body.strip()
    elif re.match(r"^https?://", candidate, re.IGNORECASE):
        raise ValueError(
            "Images API returned only a remote URL; configure the provider to return base64 image data"
        )
    max_encoded_length = ((_REMOTE_IMAGE_MAX_BYTES + 2) // 3) * 4 + 16
    if len(encoded) > max_encoded_length:
        raise ValueError("Images API returned image data that exceeds the size limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Images API returned invalid base64 image data") from exc
    if not decoded:
        raise ValueError("Images API returned an empty decoded image")
    if len(decoded) > _REMOTE_IMAGE_MAX_BYTES:
        raise ValueError("Images API returned image data that exceeds the size limit")
    media_type = _generated_image_media_type(decoded)
    if declared_media_type == "image/jpg":
        declared_media_type = "image/jpeg"
    if declared_media_type and declared_media_type != media_type:
        raise ValueError("Images API data URL media type does not match its bytes")
    return encoded, media_type


async def _download_remote_image(
    url: str,
    *,
    proxy_mode: str = "inherit",
) -> tuple[str, str]:
    """Download a provider-returned image through a bounded SSRF-safe path."""

    current = str(url or "").strip()
    proxy_url = _proxy_url_for_base_url(current, proxy_mode)
    client_kwargs: dict[str, Any] = {
        "follow_redirects": False,
        "trust_env": False,
        "timeout": httpx.Timeout(60.0),
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**client_kwargs) as client:
        for redirect_index in range(_REMOTE_IMAGE_MAX_REDIRECTS + 1):
            assessment = await asyncio.to_thread(assess_network_url, current)
            if not assessment.allowed:
                raise ValueError(
                    f"Images API remote image URL was blocked: {assessment.reason}"
                )
            async with client.stream(
                "GET", current, headers={"Accept": "image/*"}
            ) as response:
                peer_error = actual_peer_network_error(
                    response,
                    current,
                    proxy_url=proxy_url,
                )
                if peer_error:
                    raise ValueError(f"Images API remote image blocked: {peer_error}")
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("location") or "").strip()
                    if not location:
                        raise ValueError(
                            "Images API remote image redirect had no location"
                        )
                    if redirect_index >= _REMOTE_IMAGE_MAX_REDIRECTS:
                        raise ValueError(
                            "Images API remote image exceeded the redirect limit"
                        )
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                declared_length = str(
                    response.headers.get("content-length") or ""
                ).strip()
                if (
                    declared_length.isdigit()
                    and int(declared_length) > _REMOTE_IMAGE_MAX_BYTES
                ):
                    raise ValueError("Images API remote image exceeds the size limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _REMOTE_IMAGE_MAX_BYTES:
                        raise ValueError(
                            "Images API remote image exceeds the size limit"
                        )
                decoded = bytes(body)
                if not decoded:
                    raise ValueError("Images API remote image was empty")
                media_type = _generated_image_media_type(decoded)
                declared_type = (
                    str(response.headers.get("content-type") or "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if declared_type == "image/jpg":
                    declared_type = "image/jpeg"
                if declared_type and declared_type != media_type:
                    raise ValueError(
                        "Images API remote image Content-Type does not match its bytes"
                    )
                return base64.b64encode(decoded).decode("ascii"), media_type
    raise ValueError("Images API remote image could not be downloaded")


async def _extract_images_api_images(
    response: Any,
    *,
    proxy_mode: str = "inherit",
) -> list[tuple[str, str]]:
    data = _get_attr_or_item(response, "data", []) or []
    if not isinstance(data, list):
        if isinstance(data, dict) or any(
            _get_attr_or_item(data, key, None) is not None
            for key in ("b64_json", "image_data", "data", "url")
        ):
            data = [data]
        else:
            raise ValueError("Images API response did not contain image data")
    images: list[tuple[str, str]] = []
    remote_url_error: ValueError | None = None
    for item in data:
        raw_value = ""
        for key in ("b64_json", "image_data", "data", "url"):
            candidate = _get_attr_or_item(item, key, "")
            extracted = _extract_image_result(candidate)
            if extracted:
                raw_value = extracted
                break
        if not raw_value:
            continue
        try:
            if re.match(r"^https?://", raw_value, re.IGNORECASE):
                normalized = await _download_remote_image(
                    raw_value,
                    proxy_mode=proxy_mode,
                )
            else:
                normalized = _decode_images_api_value(raw_value)
        except ValueError as exc:
            if re.match(r"^https?://", raw_value, re.IGNORECASE):
                remote_url_error = exc
                continue
            raise
        if normalized not in images:
            images.append(normalized)
    if images:
        return images
    if remote_url_error is not None:
        raise remote_url_error
    raise ValueError("Images API response did not contain image data")


def _image_prompt_from_messages(messages: list[LLMMessage]) -> str:
    """Use only the latest real user text as the image-generation prompt."""

    for message in reversed(messages):
        if str(message.role or "").strip().lower() != "user":
            continue
        content = str(message.content or "")
        runtime_context = str(message.runtime_context or "").strip()
        if runtime_context:
            wrappers = (
                f"<system-reminder>\n{runtime_context}\n</system-reminder>",
                f"<system-reminder>\r\n{runtime_context}\r\n</system-reminder>",
            )
            stripped = content.lstrip()
            for wrapper in wrappers:
                if stripped.startswith(wrapper):
                    stripped = stripped[len(wrapper) :].lstrip("\r\n ")
                    break
            content = stripped
        if content.strip():
            return content.strip()
    return ""


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


def _response_output_item_activity_metadata(item: Any) -> dict[str, Any]:
    """Keep only labels/status needed to explain provider-managed work."""

    metadata: dict[str, Any] = {}
    for field_name in ("type", "id", "name", "server_label", "status"):
        value = _safe_timeline_string(
            _get_attr_or_item(item, field_name, ""), limit=256
        )
        if value:
            metadata[field_name] = value
    tools = _get_attr_or_item(item, "tools", None)
    if isinstance(tools, list):
        metadata["tools_count"] = len(tools)
    metadata["has_error"] = bool(_get_attr_or_item(item, "error", None))
    return metadata


def _unsupported_response_output_item_types(response: Any) -> list[str]:
    output = _get_attr_or_item(response, "output", []) or []
    if not isinstance(output, list):
        return []
    unsupported: list[str] = []
    for item in output:
        item_type = str(_get_attr_or_item(item, "type", "") or "").strip()
        if (
            item_type in _OPENAI_UNSUPPORTED_EXECUTABLE_OUTPUT_ITEMS
            and item_type not in unsupported
        ):
            unsupported.append(item_type)
    return unsupported


_OPENAI_PROVIDER_ACTIVITY_EVENTS: dict[str, tuple[str, str, str]] = {
    "response.web_search_call.in_progress": (
        "Web search",
        "running",
        "Searching the web",
    ),
    "response.web_search_call.searching": (
        "Web search",
        "running",
        "Searching the web",
    ),
    "response.web_search_call.completed": (
        "Web search",
        "completed",
        "Web search completed",
    ),
    "response.file_search_call.in_progress": (
        "File search",
        "running",
        "Searching connected files",
    ),
    "response.file_search_call.searching": (
        "File search",
        "running",
        "Searching connected files",
    ),
    "response.file_search_call.completed": (
        "File search",
        "completed",
        "File search completed",
    ),
    "response.code_interpreter_call.in_progress": (
        "Code execution",
        "running",
        "Preparing provider code execution",
    ),
    "response.code_interpreter_call_code.delta": (
        "Code execution",
        "running",
        "Preparing provider code",
    ),
    "response.code_interpreter_call_code.done": (
        "Code execution",
        "running",
        "Provider code prepared",
    ),
    "response.code_interpreter_call.interpreting": (
        "Code execution",
        "running",
        "Running provider code",
    ),
    "response.code_interpreter_call.completed": (
        "Code execution",
        "completed",
        "Provider code execution completed",
    ),
    "response.image_generation_call.in_progress": (
        "Image generation",
        "running",
        "Preparing image generation",
    ),
    "response.image_generation_call.generating": (
        "Image generation",
        "running",
        "Generating an image",
    ),
    "response.image_generation_call.completed": (
        "Image generation",
        "completed",
        "Image generation completed",
    ),
    "response.mcp_call.in_progress": ("MCP tool", "running", "Calling an MCP tool"),
    "response.mcp_call_arguments.delta": (
        "MCP tool",
        "running",
        "Preparing an MCP tool call",
    ),
    "response.mcp_call_arguments.done": (
        "MCP tool",
        "running",
        "MCP tool call prepared",
    ),
    "response.mcp_call.completed": ("MCP tool", "completed", "MCP tool completed"),
    "response.mcp_call.failed": ("MCP tool", "failed", "MCP tool failed"),
    "response.mcp_list_tools.in_progress": (
        "MCP tools",
        "running",
        "Loading MCP tools",
    ),
    "response.mcp_list_tools.completed": ("MCP tools", "completed", "MCP tools loaded"),
    "response.mcp_list_tools.failed": (
        "MCP tools",
        "failed",
        "Loading MCP tools failed",
    ),
}

_OPENAI_OUTPUT_ITEM_ACTIVITY_PREFIXES = {
    "web_search_call": "response.web_search_call",
    "file_search_call": "response.file_search_call",
    "code_interpreter_call": "response.code_interpreter_call",
    "image_generation_call": "response.image_generation_call",
    "mcp_call": "response.mcp_call",
    "mcp_list_tools": "response.mcp_list_tools",
}

_OPENAI_UNSUPPORTED_EXECUTABLE_OUTPUT_ITEMS = frozenset(
    {
        "computer_call",
        "custom_tool_call",
        "local_shell_call",
        "mcp_approval_request",
    }
)

_OPENAI_PASSIVE_RESPONSE_STREAM_EVENTS = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.queued",
    }
)

_OPENAI_UNSUPPORTED_RESPONSE_STREAM_EVENTS = frozenset(
    {
        "response.audio.delta",
        "response.audio.done",
        "response.audio.transcript.delta",
        "response.audio.transcript.done",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
    }
)

_OPENAI_RESPONSE_STREAM_EVENT_TYPES = (
    frozenset(
        {
            "error",
            # Compatibility gateways have emitted this alias even though the
            # installed OpenAI SDK currently models the event as plain `error`.
            "response.error",
            "response.completed",
            "response.content_part.added",
            "response.content_part.done",
            "response.failed",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.image_generation_call.partial_image",
            "response.incomplete",
            "response.output_item.added",
            "response.output_item.done",
            "response.output_text.annotation.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
            "response.reasoning_text.delta",
            "response.reasoning_text.done",
            "response.refusal.delta",
            "response.refusal.done",
        }
    )
    | _OPENAI_PASSIVE_RESPONSE_STREAM_EVENTS
    | _OPENAI_UNSUPPORTED_RESPONSE_STREAM_EVENTS
    | frozenset(_OPENAI_PROVIDER_ACTIVITY_EVENTS)
)


def _openai_provider_activity(
    event_type: str,
    event: Any,
    item_metadata: dict[str, dict[str, Any]],
) -> ProviderActivityEvent | None:
    spec = _OPENAI_PROVIDER_ACTIVITY_EVENTS.get(event_type)
    if spec is None:
        return None
    label, status, message = spec
    item_id = str(_get_attr_or_item(event, "item_id", "") or "").strip()
    output_index = _get_attr_or_item(event, "output_index", None)
    metadata = (
        item_metadata.get(item_id, {})
        if item_id
        else (
            item_metadata.get(f"output_index:{output_index}", {})
            if isinstance(output_index, int)
            else {}
        )
    )
    tool_name = str(metadata.get("name") or "").strip()
    server_label = str(metadata.get("server_label") or "").strip()
    detail_parts: list[str] = []
    if server_label:
        detail_parts.append(f"Server: {server_label}")
    if tool_name:
        detail_parts.append(f"Tool: {tool_name}")
    if event_type.startswith("response.mcp_call") and tool_name:
        message = f"{message}: {tool_name}"
    count = metadata.get("tools_count")
    if event_type == "response.mcp_list_tools.completed" and isinstance(count, int):
        message = f"MCP tools loaded — {count} tool{'s' if count != 1 else ''}"
    if event_type == "response.code_interpreter_call_code.done":
        code = _get_attr_or_item(event, "code", "")
        if isinstance(code, str) and code:
            detail_parts.append(f"Code: {len(code)} characters")
    if event_type == "response.mcp_call_arguments.done":
        arguments = _get_attr_or_item(event, "arguments", "")
        if isinstance(arguments, str) and arguments:
            detail_parts.append(f"Arguments: {len(arguments)} characters")
    activity_id = item_id or (
        f"openai:{event_type.rsplit('.', 1)[0]}:{output_index}"
        if isinstance(output_index, int)
        else f"openai:{event_type.rsplit('.', 1)[0]}"
    )
    return ProviderActivityEvent(
        id=activity_id,
        kind=str(metadata.get("type") or event_type.rsplit(".", 1)[0]),
        name=label,
        status=status,
        message=message,
        detail=" · ".join(detail_parts),
        count=count if isinstance(count, int) else None,
    )


def _openai_output_item_activity(
    item: Any,
    *,
    output_index: int | None,
    item_metadata: dict[str, dict[str, Any]],
    terminal_status: str = "",
) -> ProviderActivityEvent | None:
    metadata = _response_output_item_activity_metadata(item)
    item_type = str(metadata.get("type") or "")
    prefix = _OPENAI_OUTPUT_ITEM_ACTIVITY_PREFIXES.get(item_type)
    if not prefix:
        return None
    status = str(metadata.get("status") or "").strip().lower()
    final_status = str(terminal_status or "").strip().lower()
    if metadata.get("has_error"):
        status = "failed"
    elif not status and final_status:
        status = "completed" if final_status == "completed" else "failed"
    suffix = {
        "in_progress": "in_progress",
        "searching": "searching",
        "interpreting": "interpreting",
        "generating": "generating",
        "completed": "completed",
        "failed": "failed",
    }.get(status)
    if suffix is None and status not in {"failed", "incomplete"}:
        return None
    event_type = f"{prefix}.{suffix}" if suffix is not None else ""
    item_id = str(metadata.get("id") or "")
    if item_id:
        item_metadata[item_id] = metadata
    if output_index is not None:
        item_metadata[f"output_index:{output_index}"] = metadata
    proxy_event = SimpleNamespace(
        item_id=item_id,
        output_index=output_index,
    )
    activity = (
        _openai_provider_activity(
            event_type,
            proxy_event,
            item_metadata,
        )
        if event_type
        else None
    )
    if activity is not None:
        return activity
    # Some output item types expose a failed terminal status without a
    # dedicated streaming event in the installed SDK. Keep that failure
    # visible instead of silently dropping the provider-managed operation.
    if status in {"failed", "incomplete"}:
        completed_spec = _OPENAI_PROVIDER_ACTIVITY_EVENTS.get(f"{prefix}.completed")
        label = (
            completed_spec[0]
            if completed_spec is not None
            else str(metadata.get("name") or item_type).replace("_", " ").title()
        )
        return ProviderActivityEvent(
            id=item_id or f"openai:{item_type}:{output_index}",
            kind=item_type,
            name=label,
            status="failed",
            message=(
                f"{label} incomplete" if status == "incomplete" else f"{label} failed"
            ),
        )
    return None


def _openai_terminal_output_activities(
    response: Any,
    item_metadata: dict[str, dict[str, Any]],
) -> list[ProviderActivityEvent]:
    output = _get_attr_or_item(response, "output", []) or []
    if not isinstance(output, list):
        return []
    response_status = str(_get_attr_or_item(response, "status", "") or "").strip()
    activities: list[ProviderActivityEvent] = []
    for output_index, item in enumerate(output):
        activity = _openai_output_item_activity(
            item,
            output_index=output_index,
            item_metadata=item_metadata,
            terminal_status=response_status,
        )
        if activity is not None:
            activities.append(activity)
    return activities


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


def _proxy_url_for_base_url(base_url: str, proxy_mode: str = "inherit") -> str:
    return provider_proxy_url_for_base_url(
        base_url,
        proxy_mode=proxy_mode,
    )


def _normalized_openai_base_url(base_url: str) -> str:
    value = str(base_url or "https://api.openai.com/v1").strip()
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc and not parsed.path:
        # A bare host with no path at all gets the conventional OpenAI ``/v1``
        # prefix. Any explicit path the user typed — including a lone ``/`` for
        # a gateway that serves the API at its root — is their decision and must
        # survive verbatim. Stripping the slash first made the root case
        # indistinguishable from a bare host, so such a gateway could not be
        # configured at all.
        return f"{value}/v1"
    return value.rstrip("/")


_RESPONSES_PROTOCOL_ERROR_MESSAGES = {
    "missing_function_call_id": "a function-call event had no stable identifier",
    "missing_function_name": "a function-call event had no tool name",
    "duplicate_function_call_start": "a function-call item started more than once",
    "function_delta_after_done": "function arguments continued after their done event",
    "invalid_function_arguments": "function arguments were not a JSON object",
    "conflicting_function_call_done": "function-call done events disagreed",
    "terminal_function_call_mismatch": "the terminal response disagreed with streamed function calls",
    "terminal_function_call_missing": "a streamed function call was absent from the terminal response",
}


def _responses_tool_protocol_error(
    code: str,
    *,
    event_type: str,
    item_id: str = "",
    call_id: str = "",
) -> StreamEvent:
    raw: dict[str, Any] = {
        "provider": "openai_responses",
        "provider_error_type": "protocol",
        "error_type": "api",
        "event_type": event_type,
        "protocol_error_code": code,
    }
    if item_id:
        raw["item_id_hash"] = _short_sha256(item_id)
    if call_id:
        raw["call_id_hash"] = _short_sha256(call_id)
    return StreamEvent(
        type=StreamEventType.ERROR,
        content=(
            "Responses API function-call protocol violation: "
            f"{_RESPONSES_PROTOCOL_ERROR_MESSAGES.get(code, code)}."
        ),
        raw=raw,
    )


def _responses_text_key(value: Any) -> str:
    item_id = str(_get_attr_or_item(value, "item_id", "") or "").strip()
    content_index = _get_attr_or_item(value, "content_index", None)
    content_label = content_index if isinstance(content_index, int) else 0
    if item_id:
        return f"item:{item_id}:content:{content_label}"
    output_index = _get_attr_or_item(value, "output_index", None)
    output_label = output_index if isinstance(output_index, int) else 0
    return f"output:{output_label}:content:{content_label}"


def _responses_reasoning_key(value: Any) -> str:
    item_id = str(_get_attr_or_item(value, "item_id", "") or "").strip()
    summary_index = _get_attr_or_item(value, "summary_index", None)
    summary_label = summary_index if isinstance(summary_index, int) else 0
    if item_id:
        return f"item:{item_id}:summary:{summary_label}"
    output_index = _get_attr_or_item(value, "output_index", None)
    output_label = output_index if isinstance(output_index, int) else 0
    return f"output:{output_label}:summary:{summary_label}"


class OpenAIAdapter(LLMAdapter):
    """
    OpenAI / 兼容 API 适配器。

    根据 wire_api 设置自动路由到 Responses API 或 Chat Completions API。
    """

    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._default_headers = {
            str(key): str(value)
            for key, value in tuple(getattr(settings, "default_headers", ()) or ())
            if str(key).strip()
        }
        self._owns_http_client = http_client is None
        self._closed = False
        self._http_client: httpx.AsyncClient | None = http_client
        # Optional chat fields this gateway answered 400 for. Remembered so the
        # rejection is paid once per adapter, not on every turn.
        self._chat_unsupported_fields: set[str] = set()
        if http_client is None:
            proxy_url = _proxy_url_for_base_url(
                settings.base_url,
                str(getattr(settings, "proxy_mode", "inherit") or "inherit"),
            )
            if proxy_url:
                http_client = httpx.AsyncClient(proxy=proxy_url, trust_env=False)
            else:
                http_client = httpx.AsyncClient(trust_env=False)
            self._http_client = http_client

    async def aclose(self) -> None:
        """Close adapter-owned network resources exactly once.

        A client injected by an embedder may be shared across adapters and
        remains the caller's responsibility.
        """
        if self._closed:
            return
        self._closed = True
        if not self._owns_http_client:
            return
        http_client = self._http_client
        self._http_client = None
        if http_client is not None:
            await http_client.aclose()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return capabilities_from_openai_settings(
            self._settings,
            provider=self._settings.provider,
        )

    def apply_reasoning_policy(self, policy: ReasoningPolicy) -> None:
        super().apply_reasoning_policy(policy)
        self._settings = replace(
            self._settings,
            reasoning_effort=policy.wire_level,
            reasoning_effort_levels=policy.wire_levels,
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
        if is_gpt_image_model(self._settings.model):
            async for event in self._stream_images_api(messages, metadata=metadata):
                yield event
            return
        if self._settings.wire_api == "responses":
            async for event in self._stream_responses_api(
                messages, tools, metadata=metadata
            ):
                yield event
        else:
            async for event in self._stream_chat_completions(
                messages, tools, metadata=metadata
            ):
                yield event

    async def simple_chat(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """非流式调用，用于摘要、压缩等内部任务。"""
        return await self._simple_chat_with_context(
            messages,
            max_tokens=max_tokens,
            context=None,
        )

    async def _side_query_chat(
        self,
        messages: list[LLMMessage],
        *,
        context: LLMSideCallContext,
    ) -> str:
        return await self._simple_chat_with_context(
            messages,
            max_tokens=context.options.max_tokens,
            context=context,
        )

    async def _simple_chat_with_context(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None,
        context: LLMSideCallContext | None,
    ) -> str:
        side_options = context.options if context is not None else None
        side_model = (
            self.small_fast_model_id()
            if side_options is not None and side_options.use_small_fast_model
            else self._settings.model
        )
        _reject_dedicated_image_agent_model(side_model)
        self.annotate_side_call(
            context,
            provider=str(self._settings.provider or "openai"),
            model_id=side_model,
        )
        if self._settings.wire_api == "responses":
            return await self._simple_responses_api(
                messages,
                max_tokens=max_tokens,
                context=context,
            )
        else:
            return await self._simple_chat_completions(
                messages,
                max_tokens=max_tokens,
                context=context,
            )

    def supports_hosted_web_search(self) -> bool:
        return (
            str(self._settings.provider or "").strip().lower() == "openai"
            and self._settings.wire_api == "responses"
        )

    async def _create_responses_request(
        self,
        kwargs: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
        wire_payload_sink: dict[str, Any] | None = None,
    ) -> Any:
        cleaned_kwargs = _strip_openai_unsupported_fields(kwargs)
        kwargs.clear()
        kwargs.update(cleaned_kwargs)
        hook_kwargs = {"metadata": metadata} if metadata is not None else {}
        if kwargs.get("stream"):
            return self._emit_responses_http_stream_events(
                kwargs,
                wire_payload_sink=wire_payload_sink,
                **hook_kwargs,
            )
        return await self._create_responses_http(
            kwargs,
            wire_payload_sink=wire_payload_sink,
            **hook_kwargs,
        )

    def _responses_url(self) -> str:
        base_url = _normalized_openai_base_url(self._settings.base_url)
        return f"{base_url}/responses"

    def _images_generations_url(self) -> str:
        base_url = _normalized_openai_base_url(self._settings.base_url)
        return f"{base_url}/images/generations"

    def _responses_headers(self) -> dict[str, str]:
        headers = self._openai_transport_headers()
        headers.update(self._default_headers)
        return headers

    def _images_headers(self) -> dict[str, str]:
        headers = self._openai_transport_headers()
        headers.update(self._default_headers)
        return headers

    def _openai_transport_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    async def _prepare_http_request(
        self,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None,
        base_headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Apply MiniCode request and header hooks to one provider request."""

        transformed_payload = await emit_provider_lifecycle_request(
            metadata,
            dict(payload),
        )
        request_payload = dict(transformed_payload)
        if "extra_body" in request_payload:
            raise ValueError(
                "MiniCode HTTP transport does not accept the OpenAI SDK extra_body wrapper"
            )
        extra_headers = request_payload.pop("extra_headers", None)
        headers: dict[str, Any] = dict(base_headers)
        if isinstance(extra_headers, dict):
            headers.update({str(key): value for key, value in extra_headers.items()})
        headers = await emit_provider_lifecycle_headers(metadata, headers)
        return _strip_openai_unsupported_fields(request_payload), headers

    async def _create_images_generation(
        self,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
        wire_payload_sink: dict[str, Any] | None = None,
    ) -> Any:
        """Execute one Images API request through the real provider boundary."""

        sent_payload, headers = await self._prepare_http_request(
            payload,
            metadata=metadata,
            base_headers=self._images_headers(),
        )
        if wire_payload_sink is not None:
            wire_payload_sink.clear()
            wire_payload_sink.update(sent_payload)
        if self._http_client is None:
            raise RuntimeError("OpenAI HTTP client is not initialized")
        async with _openai_http_stream(
            self._http_client,
            "POST",
            self._images_generations_url(),
            headers=headers,
            json_payload=sent_payload,
        ) as response:
            await emit_provider_lifecycle_response(
                metadata,
                int(getattr(response, "status_code", 200) or 200),
                getattr(response, "headers", {}),
            )
            await self._responses_http_raise_for_status(response)
            await response.aread()
            return _json_to_namespace(response.json())

    async def generate_images(
        self,
        prompt: str,
        *,
        size: str | None = None,
        quality: str | None = None,
        metadata: dict[str, Any] | None = None,
        wire_payload_sink: dict[str, Any] | None = None,
    ) -> list[tuple[str, str]]:
        """Generate and validate images without projecting chat text/events.

        This is the shared boundary used by the dedicated-model chat route and
        the optional ``generate_image`` function tool. It never sends text
        token fields or local function schemas to the Images API.
        """

        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("Image prompt is required")
        image_model = str(
            getattr(self._settings, "image_model", "") or self._settings.model or ""
        ).strip()
        if not image_model:
            raise ValueError("Image model is required")
        payload: dict[str, Any] = {
            "model": image_model,
            "prompt": clean_prompt,
            "n": 1,
            "size": str(
                size
                or getattr(self._settings, "image_size", "1024x1024")
                or "1024x1024"
            ),
            "response_format": "b64_json",
        }
        requested_quality = str(
            quality
            if quality is not None
            else getattr(self._settings, "image_quality", "") or ""
        ).strip()
        if requested_quality:
            payload["quality"] = requested_quality
        try:
            response = await self._create_images_generation(
                payload,
                metadata=metadata,
                wire_payload_sink=wire_payload_sink,
            )
        except Exception as exc:
            status_code = _error_status_code(exc)
            error_text = _error_text(exc)
            if (
                status_code in {400, 422}
                and "response_format" in error_text
                and "response_format" in payload
            ):
                payload.pop("response_format", None)
                response = await self._create_images_generation(
                    payload,
                    metadata=metadata,
                    wire_payload_sink=wire_payload_sink,
                )
            else:
                raise
        return await _extract_images_api_images(
            response,
            proxy_mode=str(
                getattr(self._settings, "proxy_mode", "inherit") or "inherit"
            ),
        )

    async def _stream_images_api(
        self,
        messages: list[LLMMessage],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Project a dedicated GPT Image request as one conversational turn."""

        prompt = _image_prompt_from_messages(messages)
        if not prompt:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="图像生成失败：没有可用的用户图像描述。",
                raw={
                    "provider": "openai_images",
                    "provider_error_type": "invalid_request",
                    "error_type": "api",
                    "failure_kind": "configuration_error",
                    "retryable": False,
                },
            )
            return

        activity_id = f"image-generation-{uuid4().hex[:16]}"
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="好的，我来生成这张图片。\n\n",
            phase="final_answer",
        )
        yield StreamEvent(
            type=StreamEventType.PROVIDER_ACTIVITY,
            provider_activity=ProviderActivityEvent(
                id=activity_id,
                kind="image_generation",
                name="图像生成",
                status="running",
                message="正在生成图像",
                detail="Images API 请求已提交",
            ),
        )

        sent_payload: dict[str, Any] = {}
        try:
            images = await self.generate_images(
                prompt,
                metadata=metadata,
                wire_payload_sink=sent_payload,
            )
        except LifecycleStaleError:
            raise
        except Exception as exc:
            content = _adapter_error_content("图像生成失败", exc)
            raw = _adapter_error_raw(exc, "openai_images")
            try:
                status_code = int(raw.get("status_code") or 0)
            except (TypeError, ValueError):
                status_code = 0
            retryable = raw.get("provider_error_type") in {
                "rate_limit",
                "server_error",
                "timeout",
                "connection",
            } or status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            raw.update(
                {
                    "failure_kind": (
                        "provider_unavailable"
                        if status_code >= 500
                        else "rate_limited"
                        if status_code == 429
                        else "authentication_failed"
                        if status_code in {401, 403}
                        else "model_not_found"
                        if status_code == 404
                        else "image_generation_failed"
                    ),
                    "retryable": bool(retryable),
                    "request_params": {
                        key: value
                        for key, value in sent_payload.items()
                        if key in {"model", "n", "size", "quality", "response_format"}
                    },
                }
            )
            yield StreamEvent(
                type=StreamEventType.PROVIDER_ACTIVITY,
                provider_activity=ProviderActivityEvent(
                    id=activity_id,
                    kind="image_generation",
                    name="图像生成",
                    status="failed",
                    message="图像生成失败",
                    detail=content,
                ),
            )
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=content,
                raw=raw,
            )
            return

        for image_data, media_type in images:
            yield StreamEvent(
                type=StreamEventType.IMAGE_CHUNK,
                image_data=image_data,
                image_media_type=media_type,
            )
        yield StreamEvent(
            type=StreamEventType.PROVIDER_ACTIVITY,
            provider_activity=ProviderActivityEvent(
                id=activity_id,
                kind="image_generation",
                name="图像生成",
                status="completed",
                message="图像生成完成",
                detail=f"已生成 {len(images)} 张图片",
                count=len(images),
            ),
        )
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="图像已经为你生成好了。",
            phase="final_answer",
        )
        yield StreamEvent(
            type=StreamEventType.DONE,
            finish_reason="stop",
            raw={
                "provider": "openai_images",
                "model": str(
                    getattr(self._settings, "image_model", "") or self._settings.model
                ),
                "finish_reason": "stop",
                "request_summary": {
                    "request_param_keys": sorted(sent_payload),
                    "request_params": {
                        key: value
                        for key, value in sent_payload.items()
                        if key in {"model", "n", "size", "quality", "response_format"}
                    },
                },
            },
        )

    async def _responses_http_raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        await response.aread()
        response.raise_for_status()

    async def _create_responses_http(
        self,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
        wire_payload_sink: dict[str, Any] | None = None,
    ) -> Any:
        if self._http_client is None:
            raise RuntimeError("Responses HTTP client is not initialized")
        sent_payload, headers = await self._prepare_http_request(
            payload,
            metadata=metadata,
            base_headers=self._responses_headers(),
        )
        if wire_payload_sink is not None:
            wire_payload_sink.clear()
            wire_payload_sink.update(sent_payload)
        async with _openai_http_stream(
            self._http_client,
            "POST",
            self._responses_url(),
            headers=headers,
            json_payload=sent_payload,
        ) as response:
            await emit_provider_lifecycle_response(
                metadata,
                int(getattr(response, "status_code", 200) or 200),
                getattr(response, "headers", {}),
            )
            await self._responses_http_raise_for_status(response)
            await response.aread()
            data = response.json()
        return _json_to_namespace(data)

    async def _emit_responses_http_stream_events(
        self,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
        wire_payload_sink: dict[str, Any] | None = None,
    ) -> AsyncIterator[Any]:
        if self._http_client is None:
            raise RuntimeError("Responses HTTP client is not initialized")

        sent_payload, headers = await self._prepare_http_request(
            payload,
            metadata=metadata,
            base_headers=self._responses_headers(),
        )
        if wire_payload_sink is not None:
            wire_payload_sink.clear()
            wire_payload_sink.update(sent_payload)
        async with _openai_http_stream(
            self._http_client,
            "POST",
            self._responses_url(),
            headers=headers,
            json_payload=sent_payload,
        ) as response:
            await emit_provider_lifecycle_response(
                metadata,
                response.status_code,
                getattr(response, "headers", {}),
            )
            await self._responses_http_raise_for_status(response)

            malformed_budget = SSEMalformedBudget()
            async for raw_payload in iter_sse_data(response):
                line = raw_payload.strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue
                try:
                    event = _json_to_namespace(json.loads(line))
                except json.JSONDecodeError:
                    malformed_budget.reject(line)
                    logger.debug(
                        "Ignoring malformed responses stream event (%d chars)",
                        len(line),
                    )
                    continue
                malformed_budget.accept()
                yield event

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
        client_metadata = _responses_client_metadata(request_metadata)
        prompt_cache_key = _responses_prompt_cache_key(client_metadata)
        prompt_cache_enabled = True
        explicit_prompt_cache = bool(
            prompt_cache_enabled
            and prompt_cache_key
            and _is_official_openai_prompt_cache_breakpoint_model(
                self._settings,
                model,
            )
        )
        if explicit_prompt_cache:
            api_input, explicit_prompt_cache = _responses_explicit_prompt_cache_input(
                instructions,
                api_input,
            )

        # 添加工具定义
        has_response_tools = False
        responses_tools: list[dict[str, Any]] = []
        if tools:
            responses_tools = self._convert_tools_to_responses_format(tools)
            has_response_tools = bool(responses_tools)
        prompt_cache_retention = _prompt_cache_retention_request(self._settings)
        configured_max_output = _clamped_responses_max_output_tokens(
            value=self._settings.max_tokens,
            context_window=self._settings.context_window,
            messages=messages,
            tools=tools,
        )
        reasoning_request = _responses_reasoning_request(
            self._settings,
            has_tools=has_response_tools,
        )
        include_request = _responses_include_request(
            self._settings,
            has_tools=has_response_tools,
            reasoning_request=reasoning_request,
        )

        kwargs: dict[str, Any] = {
            "model": model,
            "input": api_input,
            "stream": True,
            "store": False,
        }
        if instructions and not explicit_prompt_cache:
            kwargs["instructions"] = instructions
        if client_metadata:
            kwargs["client_metadata"] = client_metadata
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key
        # GPT-5.6's default implicit breakpoint is the latest user/tool
        # message.  Keep that breakpoint enabled for the main, append-only
        # agent conversation while still marking the stable system prefix
        # explicitly.  ``mode=explicit`` disables the provider's implicit
        # breakpoint, so reserve it for genuinely standalone side queries.
        identity_headers = _responses_identity_headers(client_metadata)
        if identity_headers:
            kwargs["extra_headers"] = identity_headers
        if prompt_cache_retention:
            kwargs["prompt_cache_retention"] = prompt_cache_retention
        # Codex sends the complete Responses tool-control shape for coding
        # turns, even when the current projection contains no tools (it gates
        # parallel_tool_calls on the prompt's setting and its lite mode;
        # MiniCode always runs the full coding shape).
        kwargs["tools"] = responses_tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = True
        if configured_max_output is not None:
            kwargs["max_output_tokens"] = configured_max_output

        # Ask every Responses-compatible gateway for provider-visible reasoning
        # summaries. Unsupported gateways are retried without these optional
        # fields by _create_responses_request.
        if reasoning_request:
            kwargs["reasoning"] = reasoning_request
        if include_request:
            kwargs["include"] = include_request

        lifecycle_metadata = (
            metadata
            if isinstance(metadata, dict)
            and metadata.get(LIFECYCLE_RUNTIME_METADATA_KEY) is not None
            else None
        )
        wire_request_payload: dict[str, Any] = {}

        def build_responses_request_summary(
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            payload_input = payload.get("input")
            payload_tools = payload.get("tools")
            payload_metadata = payload.get("client_metadata")
            if not isinstance(payload_metadata, dict):
                payload_metadata = payload.get("metadata")
            input_list = payload_input if isinstance(payload_input, list) else []
            logical_instructions = str(payload.get("instructions") or "")
            if not logical_instructions:
                logical_instructions = _instruction_text_from_responses_input(
                    input_list
                )
            return _safe_request_summary(
                model=str(payload.get("model") or self._settings.model),
                wire_api="responses",
                instructions=logical_instructions,
                tools=payload_tools if isinstance(payload_tools, list) else [],
                request_metadata=(
                    payload_metadata if isinstance(payload_metadata, dict) else {}
                ),
                input_items=input_list,
                prompt_cache_key=str(payload.get("prompt_cache_key") or ""),
                request_params=payload,
            )

        async def create_responses_request(payload: dict[str, Any]):
            wire_request_payload.clear()
            wire_request_payload.update(payload)
            return await self._create_responses_request(
                payload,
                metadata=lifecycle_metadata,
                wire_payload_sink=wire_request_payload,
            )

        try:
            stream = await create_responses_request(kwargs)
        except LifecycleStaleError:
            raise
        except Exception as exc:
            logger.error("Responses API 调用失败: %s", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=_adapter_error_content("LLM API 调用失败", exc),
                raw=_adapter_error_raw(exc, "openai_responses"),
            )
            return

        # 解析 Responses API 流式事件
        full_text = ""
        full_reasoning_summary = ""
        pending_tool_calls: list[ToolCallEvent] = []
        response_tool_items: dict[str, dict[str, str]] = {}
        finalized_response_tool_shapes: dict[str, tuple[str, str]] = {}
        response_tool_start_count = 0
        response_output_item_metadata: dict[str, dict[str, Any]] = {}
        response_message_phases: dict[str, str] = {}
        usage = UsageInfo()
        provider_timeline: list[dict[str, Any]] = []
        summary_payload = wire_request_payload or kwargs
        raw_done: dict[str, Any] = {
            "provider": "openai_responses",
            "model": str(summary_payload.get("model") or self._settings.model),
            "request_summary": build_responses_request_summary(summary_payload),
            "safety": _provider_trace_safety(),
            "provider_timeline": provider_timeline,
        }
        finish_reason = ""
        completed_response_provider_items: list[dict[str, Any]] = []
        terminal_tool_calls: list[ToolCallEvent] = []
        completed_response_message_phase = ""
        saw_terminal_response_event = False
        response_text_by_item: dict[str, str] = {}
        response_reasoning_by_part: dict[str, str] = {}
        response_text_last_source: dict[str, str] = {}
        response_reasoning_last_source: dict[str, str] = {}
        terminal_response_text_keys: set[str] = set()
        terminal_response_reasoning_keys: set[str] = set()
        last_response_sequence_number: int | None = None
        seen_response_sequence_numbers: set[int] = set()
        provider_activity_fingerprints: dict[
            str,
            tuple[str, str, str, str, int | None],
        ] = {}

        def accept_provider_activity(
            activity: ProviderActivityEvent | None,
        ) -> bool:
            if activity is None:
                return False
            fingerprint = (
                activity.status,
                activity.message,
                activity.detail,
                activity.name,
                activity.count,
            )
            if provider_activity_fingerprints.get(activity.id) == fingerprint:
                return False
            provider_activity_fingerprints[activity.id] = fingerprint
            return True

        def finalize_response_function_call(
            value: Any,
            *,
            event_type: str,
        ) -> tuple[ToolCallEvent | None, StreamEvent | None]:
            item_id = str(
                _get_attr_or_item(value, "item_id", "")
                or _get_attr_or_item(value, "id", "")
                or ""
            ).strip()
            event_call_id = str(_get_attr_or_item(value, "call_id", "") or "").strip()
            slot = (
                response_tool_items.get(event_call_id)
                or response_tool_items.get(item_id)
                or {}
            )
            call_id = str(slot.get("id") or event_call_id or item_id).strip()
            if not call_id:
                return None, _responses_tool_protocol_error(
                    "missing_function_call_id",
                    event_type=event_type,
                    item_id=item_id,
                )
            name = str(
                _get_attr_or_item(value, "name", "") or slot.get("name", "") or ""
            ).strip()
            if not name:
                return None, _responses_tool_protocol_error(
                    "missing_function_name",
                    event_type=event_type,
                    item_id=item_id,
                    call_id=call_id,
                )
            arguments_value = _get_attr_or_item(value, "arguments", None)
            if arguments_value is None or arguments_value == "":
                arguments_value = slot.get("arguments") or "{}"
            if not isinstance(arguments_value, str):
                return None, _responses_tool_protocol_error(
                    "invalid_function_arguments",
                    event_type=event_type,
                    item_id=item_id,
                    call_id=call_id,
                )

            arguments_repaired = False
            try:
                arguments = json.loads(arguments_value)
            except (json.JSONDecodeError, TypeError):
                from backend.llm.json_repair import repair_tool_json

                arguments = repair_tool_json(arguments_value)
                arguments_repaired = True
            if not isinstance(arguments, dict):
                return None, _responses_tool_protocol_error(
                    "invalid_function_arguments",
                    event_type=event_type,
                    item_id=item_id,
                    call_id=call_id,
                )

            shape = (name, _json_fingerprint(arguments))
            keys = [key for key in (call_id, item_id) if key]
            existing_shapes = {
                finalized_response_tool_shapes[key]
                for key in keys
                if key in finalized_response_tool_shapes
            }
            if existing_shapes:
                if existing_shapes != {shape}:
                    return None, _responses_tool_protocol_error(
                        "conflicting_function_call_done",
                        event_type=event_type,
                        item_id=item_id,
                        call_id=call_id,
                    )
                raw_done.setdefault("duplicate_tool_done_events", []).append(
                    {
                        "event_type": event_type,
                        "item_id_hash": _short_sha256(item_id) if item_id else "",
                        "call_id_hash": _short_sha256(call_id),
                    }
                )
                return None, None

            for key in keys:
                finalized_response_tool_shapes[key] = shape
            slot.update(
                {
                    "id": call_id,
                    "item_id": item_id,
                    "name": name,
                    "arguments": arguments_value,
                }
            )
            response_tool_items[call_id] = slot
            if item_id:
                response_tool_items[item_id] = slot
            return (
                ToolCallEvent(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                    arguments_repaired=arguments_repaired,
                ),
                None,
            )

        def accept_response_sequence(event_type: str, event: Any) -> bool:
            nonlocal last_response_sequence_number
            sequence_number = _get_attr_or_item(event, "sequence_number", None)
            if not isinstance(sequence_number, int) or isinstance(
                sequence_number, bool
            ):
                return True
            if sequence_number in seen_response_sequence_numbers or (
                last_response_sequence_number is not None
                and sequence_number < last_response_sequence_number
            ):
                raw_done.setdefault("dropped_sequence_events", []).append(
                    {
                        "event_type": event_type,
                        "sequence_number": sequence_number,
                        "last_sequence_number": last_response_sequence_number,
                    }
                )
                return False
            if (
                last_response_sequence_number is not None
                and sequence_number > last_response_sequence_number + 1
            ):
                raw_done.setdefault("sequence_gaps", []).append(
                    {
                        "event_type": event_type,
                        "expected_sequence_number": (last_response_sequence_number + 1),
                        "received_sequence_number": sequence_number,
                    }
                )
            seen_response_sequence_numbers.add(sequence_number)
            last_response_sequence_number = sequence_number
            return True

        def merge_citations(citations: list[dict[str, Any]]) -> None:
            if not citations:
                return
            existing = raw_done.setdefault("citations", [])
            if not isinstance(existing, list):
                existing = []
                raw_done["citations"] = existing
            for citation in citations:
                if citation not in existing:
                    existing.append(citation)

        def append_response_text_delta(
            key: str,
            delta: Any,
            *,
            event_type: str,
        ) -> str:
            if not isinstance(delta, str) or not delta:
                return ""
            existing = response_text_by_item.get(key, "")
            if key in terminal_response_text_keys:
                raw_done.setdefault("late_text_events", []).append(
                    {
                        "event_type": event_type,
                        "item_key_hash": _short_sha256(key),
                        "chars": len(delta),
                    }
                )
                return ""
            missing = delta
            if existing and response_text_last_source.get(key) == "snapshot":
                if delta in existing:
                    missing = ""
                else:
                    max_overlap = min(len(existing), len(delta))
                    overlap = 0
                    for size in range(max_overlap, 0, -1):
                        if existing.endswith(delta[:size]):
                            overlap = size
                            break
                    missing = delta[overlap:]
                if missing != delta:
                    raw_done.setdefault("text_delta_overlaps", []).append(
                        {
                            "event_type": event_type,
                            "item_key_hash": _short_sha256(key),
                            "delta_chars": len(delta),
                            "appended_chars": len(missing),
                        }
                    )
            if missing:
                response_text_by_item[key] = existing + missing
                response_text_last_source[key] = "delta"
            return missing

        def reconcile_terminal_text(
            key: str,
            text: Any,
            *,
            event_type: str,
            terminal: bool,
        ) -> str:
            if not isinstance(text, str) or not text:
                return ""
            if key in terminal_response_text_keys and not terminal:
                raw_done.setdefault("late_text_events", []).append(
                    {
                        "event_type": event_type,
                        "item_key_hash": _short_sha256(key),
                        "chars": len(text),
                    }
                )
                return ""
            existing = response_text_by_item.get(key, "")
            if text == existing:
                response_text_last_source[key] = "snapshot"
                if terminal:
                    terminal_response_text_keys.add(key)
                return ""
            if text.startswith(existing):
                response_text_by_item[key] = text
                response_text_last_source[key] = "snapshot"
                if terminal:
                    terminal_response_text_keys.add(key)
                return text[len(existing) :]
            raw_done.setdefault("text_reconciliation_mismatches", []).append(
                {
                    "event_type": event_type,
                    "item_key_hash": _short_sha256(key),
                    "streamed_chars": len(existing),
                    "terminal_chars": len(text),
                }
            )
            response_text_last_source[key] = "snapshot"
            if terminal:
                terminal_response_text_keys.add(key)
            return ""

        def append_reasoning_summary_delta(
            value: Any,
            delta: Any,
            *,
            event_type: str,
        ) -> str:
            if not isinstance(delta, str) or not delta:
                return ""
            key = _responses_reasoning_key(value)
            existing = response_reasoning_by_part.get(key, "")
            if key in terminal_response_reasoning_keys:
                raw_done.setdefault("late_reasoning_events", []).append(
                    {
                        "event_type": event_type,
                        "part_key_hash": _short_sha256(key),
                        "chars": len(delta),
                    }
                )
                return ""
            missing = delta
            if existing and response_reasoning_last_source.get(key) == "snapshot":
                if delta in existing:
                    missing = ""
                else:
                    max_overlap = min(len(existing), len(delta))
                    overlap = 0
                    for size in range(max_overlap, 0, -1):
                        if existing.endswith(delta[:size]):
                            overlap = size
                            break
                    missing = delta[overlap:]
                if missing != delta:
                    raw_done.setdefault("reasoning_delta_overlaps", []).append(
                        {
                            "event_type": event_type,
                            "part_key_hash": _short_sha256(key),
                            "delta_chars": len(delta),
                            "appended_chars": len(missing),
                        }
                    )
            if missing:
                response_reasoning_by_part[key] = existing + missing
                response_reasoning_last_source[key] = "delta"
            return missing

        def reconcile_reasoning_summary(
            value: Any,
            text: Any,
            *,
            event_type: str,
            terminal: bool,
        ) -> str:
            if not isinstance(text, str) or not text:
                return ""
            key = _responses_reasoning_key(value)
            if key in terminal_response_reasoning_keys and not terminal:
                raw_done.setdefault("late_reasoning_events", []).append(
                    {
                        "event_type": event_type,
                        "part_key_hash": _short_sha256(key),
                        "chars": len(text),
                    }
                )
                return ""
            existing = response_reasoning_by_part.get(key, "")
            if text == existing:
                response_reasoning_last_source[key] = "snapshot"
                if terminal:
                    terminal_response_reasoning_keys.add(key)
                return ""
            if text.startswith(existing):
                response_reasoning_by_part[key] = text
                response_reasoning_last_source[key] = "snapshot"
                if terminal:
                    terminal_response_reasoning_keys.add(key)
                return text[len(existing) :]
            raw_done.setdefault("reasoning_reconciliation_mismatches", []).append(
                {
                    "scope": "reasoning_summary_done",
                    "event_type": event_type,
                    "part_key_hash": _short_sha256(key),
                    "streamed_chars": len(existing),
                    "terminal_chars": len(text),
                }
            )
            response_reasoning_last_source[key] = "snapshot"
            if terminal:
                terminal_response_reasoning_keys.add(key)
            return ""

        def terminal_response_text_events(
            response_obj: Any,
            *,
            recovered_from: str,
        ) -> list[StreamEvent]:
            segments: list[dict[str, Any]] = []
            output = _get_attr_or_item(response_obj, "output", []) or []
            if isinstance(output, list):
                for output_index, item in enumerate(output):
                    if str(_get_attr_or_item(item, "type", "") or "") != "message":
                        continue
                    item_id = str(_get_attr_or_item(item, "id", "") or "").strip()
                    phase = str(_get_attr_or_item(item, "phase", "") or "").strip()
                    content = _get_attr_or_item(item, "content", []) or []
                    if not isinstance(content, list):
                        continue
                    for content_index, part in enumerate(content):
                        part_type = str(_get_attr_or_item(part, "type", "") or "")
                        if part_type in {"output_text", "text"}:
                            text = _get_attr_or_item(part, "text", "")
                            content_kind = "text"
                        elif part_type == "refusal":
                            text = _get_attr_or_item(part, "refusal", "")
                            content_kind = "refusal"
                        else:
                            continue
                        if not isinstance(text, str) or not text:
                            continue
                        key = (
                            f"item:{item_id}:content:{content_index}"
                            if item_id
                            else f"output:{output_index}:content:{content_index}"
                        )
                        segments.append(
                            {
                                "key": key,
                                "text": text,
                                "phase": phase,
                                "item_id": item_id,
                                "content_index": content_index,
                                "content_kind": content_kind,
                            }
                        )
            if not segments:
                direct_text = _get_attr_or_item(response_obj, "output_text", "")
                if isinstance(direct_text, str) and direct_text:
                    segments.append(
                        {
                            "key": "response:output_text",
                            "text": direct_text,
                            "phase": _responses_message_phase_from_response(
                                response_obj
                            ),
                            "item_id": "",
                            "content_index": None,
                            "content_kind": "text",
                        }
                    )

            if not segments:
                return []

            terminal_text = "".join(str(segment["text"]) for segment in segments)
            for segment in segments:
                # The terminal response gives us the authoritative item identity.
                # Record it even when all text was already streamed under an
                # anonymous output key so later reconciliation does not treat the
                # same content as a new item.
                response_text_by_item[str(segment["key"])] = str(segment["text"])

            if terminal_text == full_text:
                return []
            if full_text and not terminal_text.startswith(full_text):
                # Do not append conflicting terminal text. A provider may assign
                # item IDs only at response.completed, so per-item comparison can
                # falsely duplicate an otherwise identical aggregate. Keep the
                # diagnostic content-free and fail closed on projection.
                raw_done.setdefault("text_reconciliation_mismatches", []).append(
                    {
                        "scope": recovered_from,
                        "streamed_chars": len(full_text),
                        "terminal_chars": len(terminal_text),
                        "terminal_segments": len(segments),
                    }
                )
                return []

            recovered: list[StreamEvent] = []
            consumed_chars = len(full_text)
            terminal_offset = 0
            for segment in segments:
                text = str(segment["text"])
                segment_end = terminal_offset + len(text)
                if consumed_chars >= segment_end:
                    terminal_offset = segment_end
                    continue
                missing = text[max(0, consumed_chars - terminal_offset) :]
                terminal_offset = segment_end
                if not missing:
                    continue
                phase = str(segment["phase"] or "")
                raw_text = _raw_text_delta_metadata(
                    "openai_responses",
                    message_phase=phase,
                )
                raw_text["recovered_from"] = recovered_from
                content_kind = str(segment["content_kind"] or "text")
                if content_kind == "refusal":
                    raw_text["provider_refusal"] = True
                recovered.append(
                    StreamEvent(
                        type=StreamEventType.TEXT_CHUNK,
                        content=missing,
                        raw=raw_text,
                        phase=phase,
                        item_id=str(segment["item_id"] or ""),
                        content_index=(
                            segment["content_index"]
                            if isinstance(segment["content_index"], int)
                            else None
                        ),
                        content_kind=content_kind,
                    )
                )
            return recovered

        def terminal_response_reasoning_events(
            response_obj: Any,
            *,
            recovered_from: str,
        ) -> list[StreamEvent]:
            segments: list[dict[str, Any]] = []
            output = _get_attr_or_item(response_obj, "output", []) or []
            if isinstance(output, list):
                for item in output:
                    if str(_get_attr_or_item(item, "type", "") or "") != "reasoning":
                        continue
                    item_id = str(_get_attr_or_item(item, "id", "") or "").strip()
                    summary = _get_attr_or_item(item, "summary", []) or []
                    if not isinstance(summary, list):
                        continue
                    for summary_index, part in enumerate(summary):
                        if str(_get_attr_or_item(part, "type", "") or "") not in {
                            "summary_text",
                            "text",
                        }:
                            continue
                        text = _get_attr_or_item(part, "text", "")
                        if isinstance(text, str) and text:
                            segments.append(
                                {
                                    "text": text,
                                    "item_id": item_id,
                                    "summary_index": summary_index,
                                }
                            )
            if not segments:
                return []

            terminal_summary = "".join(str(segment["text"]) for segment in segments)
            if terminal_summary == full_reasoning_summary:
                return []
            if full_reasoning_summary and not terminal_summary.startswith(
                full_reasoning_summary
            ):
                raw_done.setdefault("reasoning_reconciliation_mismatches", []).append(
                    {
                        "scope": recovered_from,
                        "streamed_chars": len(full_reasoning_summary),
                        "terminal_chars": len(terminal_summary),
                        "terminal_segments": len(segments),
                    }
                )
                return []

            recovered: list[StreamEvent] = []
            consumed_chars = len(full_reasoning_summary)
            terminal_offset = 0
            for segment in segments:
                text = str(segment["text"])
                segment_end = terminal_offset + len(text)
                if consumed_chars >= segment_end:
                    terminal_offset = segment_end
                    continue
                missing = text[max(0, consumed_chars - terminal_offset) :]
                terminal_offset = segment_end
                if not missing:
                    continue
                recovered.append(
                    StreamEvent(
                        type=StreamEventType.THINKING_CHUNK,
                        content=missing,
                        raw={
                            "provider_reasoning_type": "reasoning_summary_text",
                            "recovered_from": recovered_from,
                        },
                        item_id=str(segment["item_id"] or ""),
                        content_index=int(segment["summary_index"]),
                        content_kind="thinking",
                    )
                )
            return recovered

        try:
            async for event in stream:
                event_type = str(getattr(event, "type", "") or "")
                if event_type not in _OPENAI_RESPONSE_STREAM_EVENT_TYPES:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=(
                            "Responses API returned an unknown stream event "
                            f"that MiniCode cannot safely interpret: {event_type or 'missing'}"
                        ),
                        raw={
                            "provider": "openai_responses",
                            "provider_error_type": "protocol",
                            "error_type": "api",
                            "event_type": event_type or "missing",
                            "protocol_error_code": "unknown_stream_event",
                        },
                    )
                    return
                if event_type:
                    if not accept_response_sequence(str(event_type), event):
                        continue
                    if saw_terminal_response_event:
                        post_terminal: dict[str, Any] = {"event_type": str(event_type)}
                        sequence_number = _get_attr_or_item(
                            event, "sequence_number", None
                        )
                        if isinstance(sequence_number, int) and not isinstance(
                            sequence_number, bool
                        ):
                            post_terminal["sequence_number"] = sequence_number
                        raw_done.setdefault("post_terminal_events", []).append(
                            post_terminal
                        )
                        continue
                    _append_provider_timeline(
                        provider_timeline,
                        str(event_type),
                        **_response_timeline_fields(str(event_type), event),
                    )

                if event_type in _OPENAI_UNSUPPORTED_RESPONSE_STREAM_EVENTS:
                    feature = (
                        "audio_output"
                        if event_type.startswith("response.audio.")
                        else "custom_tool_execution"
                    )
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=(
                            "Responses API returned a stream feature that "
                            f"MiniCode cannot safely project: {feature}"
                        ),
                        raw={
                            "provider": "openai_responses",
                            "provider_error_type": "protocol",
                            "error_type": "api",
                            "event_type": event_type,
                            "protocol_error_code": "unsupported_stream_feature",
                            "feature": feature,
                        },
                    )
                    return

                # 文本内容增量
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        key = _responses_text_key(event)
                        emitted_delta = append_response_text_delta(
                            key,
                            delta,
                            event_type=event_type,
                        )
                        if not emitted_delta:
                            continue
                        full_text += emitted_delta
                        message_phase = _response_message_phase_for_event(
                            event, response_message_phases
                        )
                        raw_text = _raw_text_delta_metadata(
                            "openai_responses",
                            usage_obj=getattr(event, "usage", None),
                            message_phase=message_phase,
                        )
                        yield StreamEvent(
                            type=StreamEventType.TEXT_CHUNK,
                            content=emitted_delta,
                            raw=raw_text,
                            phase=message_phase,
                            item_id=str(_get_attr_or_item(event, "item_id", "") or ""),
                            content_index=(
                                _get_attr_or_item(event, "content_index", None)
                                if isinstance(
                                    _get_attr_or_item(event, "content_index", None), int
                                )
                                else None
                            ),
                            content_kind="text",
                        )

                # Capture provider-native citations (url_citation annotations)
                # from the completed output text. Attached to the DONE event's
                # raw metadata so the frontend can bind [1] [2] markers to real
                # sources instead of relying solely on tool-call heuristics.
                elif event_type == "response.output_text.done":
                    message_phase = str(
                        _get_attr_or_item(event, "phase", "") or ""
                    ).strip()
                    if message_phase:
                        item_id = str(
                            _get_attr_or_item(event, "item_id", "") or ""
                        ).strip()
                        output_index = _get_attr_or_item(event, "output_index", None)
                        _record_response_message_phase(
                            response_message_phases,
                            item_id=item_id,
                            output_index=output_index
                            if isinstance(output_index, int)
                            else None,
                            phase=message_phase,
                        )
                    merge_citations(_extract_url_citations(event))
                    item_id = str(_get_attr_or_item(event, "item_id", "") or "")
                    content_index = _get_attr_or_item(event, "content_index", None)
                    recovered_text = reconcile_terminal_text(
                        _responses_text_key(event),
                        _get_attr_or_item(event, "text", ""),
                        event_type=event_type,
                        terminal=True,
                    )
                    full_text += recovered_text
                    yield StreamEvent(
                        type=StreamEventType.TEXT_CHUNK,
                        content=recovered_text,
                        raw=(
                            {
                                "provider": "openai_responses",
                                "recovered_from": "response.output_text.done",
                            }
                            if recovered_text
                            else {}
                        ),
                        phase=message_phase,
                        item_id=item_id,
                        content_index=content_index
                        if isinstance(content_index, int)
                        else None,
                        content_kind="text",
                        lifecycle="end",
                    )

                elif event_type == "response.output_text.annotation.added":
                    merge_citations(_extract_url_citations(event))

                elif event_type == "response.refusal.delta":
                    delta = _get_attr_or_item(event, "delta", "")
                    if isinstance(delta, str) and delta:
                        key = _responses_text_key(event)
                        emitted_delta = append_response_text_delta(
                            key,
                            delta,
                            event_type=event_type,
                        )
                        if not emitted_delta:
                            continue
                        full_text += emitted_delta
                        message_phase = _response_message_phase_for_event(
                            event, response_message_phases
                        )
                        raw_text = _raw_text_delta_metadata(
                            "openai_responses",
                            usage_obj=_get_attr_or_item(event, "usage", None),
                            message_phase=message_phase,
                        )
                        raw_text["provider_refusal"] = True
                        yield StreamEvent(
                            type=StreamEventType.TEXT_CHUNK,
                            content=emitted_delta,
                            raw=raw_text,
                            phase=message_phase,
                            item_id=str(_get_attr_or_item(event, "item_id", "") or ""),
                            content_index=(
                                _get_attr_or_item(event, "content_index", None)
                                if isinstance(
                                    _get_attr_or_item(event, "content_index", None),
                                    int,
                                )
                                else None
                            ),
                            content_kind="refusal",
                        )

                elif event_type == "response.refusal.done":
                    message_phase = _response_message_phase_for_event(
                        event, response_message_phases
                    )
                    item_id = str(_get_attr_or_item(event, "item_id", "") or "")
                    content_index = _get_attr_or_item(event, "content_index", None)
                    recovered_text = reconcile_terminal_text(
                        _responses_text_key(event),
                        _get_attr_or_item(event, "refusal", ""),
                        event_type=event_type,
                        terminal=True,
                    )
                    full_text += recovered_text
                    raw_text = {
                        "provider": "openai_responses",
                        "provider_refusal": True,
                    }
                    if recovered_text:
                        raw_text["recovered_from"] = "response.refusal.done"
                    yield StreamEvent(
                        type=StreamEventType.TEXT_CHUNK,
                        content=recovered_text,
                        raw=raw_text,
                        phase=message_phase,
                        item_id=item_id,
                        content_index=(
                            content_index if isinstance(content_index, int) else None
                        ),
                        content_kind="refusal",
                        lifecycle="end",
                    )

                elif event_type in {
                    "response.content_part.added",
                    "response.content_part.done",
                }:
                    part = _get_attr_or_item(event, "part", None)
                    part_type = str(_get_attr_or_item(part, "type", "") or "").strip()
                    merge_citations(_extract_url_citations(part))
                    if part_type in {"output_text", "text"}:
                        terminal_text = _get_attr_or_item(part, "text", "")
                        content_kind = "text"
                    elif part_type == "refusal":
                        terminal_text = _get_attr_or_item(part, "refusal", "")
                        content_kind = "refusal"
                    else:
                        terminal_text = ""
                        content_kind = part_type
                    recovered_text = reconcile_terminal_text(
                        _responses_text_key(event),
                        terminal_text,
                        event_type=event_type,
                        terminal=event_type == "response.content_part.done",
                    )
                    if recovered_text:
                        full_text += recovered_text
                        raw_text = {
                            "provider": "openai_responses",
                            "recovered_from": event_type,
                        }
                        if content_kind == "refusal":
                            raw_text["provider_refusal"] = True
                        yield StreamEvent(
                            type=StreamEventType.TEXT_CHUNK,
                            content=recovered_text,
                            raw=raw_text,
                            phase=_response_message_phase_for_event(
                                event,
                                response_message_phases,
                            ),
                            item_id=str(_get_attr_or_item(event, "item_id", "") or ""),
                            content_index=(
                                _get_attr_or_item(event, "content_index", None)
                                if isinstance(
                                    _get_attr_or_item(event, "content_index", None),
                                    int,
                                )
                                else None
                            ),
                            content_kind=content_kind,
                        )

                # 推理摘要增量（GPT-5 / o-series via Responses API）
                elif event_type == "response.reasoning_summary_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        delta_text = append_reasoning_summary_delta(
                            event,
                            delta,
                            event_type=event_type,
                        )
                        if not delta_text:
                            continue
                        full_reasoning_summary += delta_text
                        yield StreamEvent(
                            type=StreamEventType.THINKING_CHUNK,
                            content=delta_text,
                            raw={"provider_reasoning_type": "reasoning_summary_text"},
                            item_id=str(_get_attr_or_item(event, "item_id", "") or ""),
                            content_index=(
                                _get_attr_or_item(event, "summary_index", None)
                                if isinstance(
                                    _get_attr_or_item(event, "summary_index", None), int
                                )
                                else None
                            ),
                            content_kind="thinking",
                        )

                elif event_type == "response.reasoning_summary_part.added":
                    part = _get_attr_or_item(event, "part", None)
                    initial_text = _get_attr_or_item(part, "text", "")
                    if isinstance(initial_text, str) and initial_text:
                        missing_summary = reconcile_reasoning_summary(
                            event,
                            initial_text,
                            event_type=event_type,
                            terminal=False,
                        )
                        if not missing_summary:
                            continue
                        full_reasoning_summary += missing_summary
                        yield StreamEvent(
                            type=StreamEventType.THINKING_CHUNK,
                            content=missing_summary,
                            raw={"provider_reasoning_type": "reasoning_summary_text"},
                            item_id=str(_get_attr_or_item(event, "item_id", "") or ""),
                            content_index=(
                                _get_attr_or_item(event, "summary_index", None)
                                if isinstance(
                                    _get_attr_or_item(event, "summary_index", None),
                                    int,
                                )
                                else None
                            ),
                            content_kind="thinking",
                        )

                elif event_type in {
                    "response.reasoning_summary_text.done",
                    "response.reasoning_summary_part.done",
                }:
                    part = _get_attr_or_item(event, "part", None)
                    terminal_text = _get_attr_or_item(
                        event, "text", ""
                    ) or _get_attr_or_item(part, "text", "")
                    missing_summary = reconcile_reasoning_summary(
                        event,
                        terminal_text,
                        event_type=event_type,
                        terminal=True,
                    )
                    if missing_summary:
                        full_reasoning_summary += missing_summary
                        yield StreamEvent(
                            type=StreamEventType.THINKING_CHUNK,
                            content=missing_summary,
                            raw={
                                "provider_reasoning_type": "reasoning_summary_text",
                                "recovered_from": event_type,
                            },
                            item_id=str(_get_attr_or_item(event, "item_id", "") or ""),
                            content_index=(
                                _get_attr_or_item(event, "summary_index", None)
                                if isinstance(
                                    _get_attr_or_item(event, "summary_index", None),
                                    int,
                                )
                                else None
                            ),
                            content_kind="thinking",
                            lifecycle="end",
                        )

                elif event_type in {
                    "response.reasoning_text.delta",
                    "response.reasoning_text.done",
                }:
                    # Raw provider reasoning is deliberately not emitted as a
                    # StreamEvent.  The agent-loop projection also rejects
                    # non-summary reasoning, but enforcing the boundary here
                    # protects every adapter consumer (including direct stream
                    # callers) from accidentally exposing private chain-of-
                    # thought.  The content-free provider timeline recorded
                    # above still preserves event/index/length diagnostics.
                    pass

                elif event_type == "response.output_item.added":
                    item = _get_attr_or_item(event, "item", None)
                    item_type = str(_get_attr_or_item(item, "type", "") or "").strip()
                    output_index = _get_attr_or_item(event, "output_index", None)
                    output_index_value = (
                        output_index if isinstance(output_index, int) else None
                    )
                    item_metadata = _response_output_item_activity_metadata(item)
                    metadata_item_id = str(item_metadata.get("id") or "")
                    if metadata_item_id:
                        response_output_item_metadata[metadata_item_id] = item_metadata
                    if output_index_value is not None:
                        response_output_item_metadata[
                            f"output_index:{output_index_value}"
                        ] = item_metadata
                    if item_type in _OPENAI_UNSUPPORTED_EXECUTABLE_OUTPUT_ITEMS:
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=(
                                "Responses API returned an executable output item "
                                f"that MiniCode cannot safely run: {item_type}"
                            ),
                            raw={
                                "provider": "openai_responses",
                                "provider_error_type": "protocol",
                                "error_type": "api",
                                "event_type": event_type,
                                "output_item_type": item_type,
                            },
                        )
                        return
                    activity = _openai_output_item_activity(
                        item,
                        output_index=output_index_value,
                        item_metadata=response_output_item_metadata,
                    )
                    if activity is not None:
                        if accept_provider_activity(activity):
                            yield StreamEvent(
                                type=StreamEventType.PROVIDER_ACTIVITY,
                                provider_activity=activity,
                            )
                    if item_type == "message":
                        item_id = str(_get_attr_or_item(item, "id", "") or "").strip()
                        phase = str(_get_attr_or_item(item, "phase", "") or "").strip()
                        _record_response_message_phase(
                            response_message_phases,
                            item_id=item_id,
                            output_index=output_index_value,
                            phase=phase,
                        )
                        yield StreamEvent(
                            type=StreamEventType.TEXT_CHUNK,
                            phase=phase,
                            item_id=item_id,
                            content_index=None,
                            content_kind="text",
                            lifecycle="start",
                        )
                    elif item_type == "function_call":
                        item_id = str(_get_attr_or_item(item, "id", "") or "").strip()
                        call_id = str(
                            _get_attr_or_item(item, "call_id", "") or item_id
                        ).strip()
                        name = str(_get_attr_or_item(item, "name", "") or "").strip()
                        key = call_id or item_id
                        if not key:
                            yield _responses_tool_protocol_error(
                                "missing_function_call_id",
                                event_type=event_type,
                                item_id=item_id,
                            )
                            return
                        existing_slot = response_tool_items.get(
                            call_id
                        ) or response_tool_items.get(item_id)
                        if (
                            existing_slot is not None
                            and existing_slot.get("started") == "true"
                        ):
                            yield _responses_tool_protocol_error(
                                "duplicate_function_call_start",
                                event_type=event_type,
                                item_id=item_id,
                                call_id=call_id or item_id,
                            )
                            return
                        initial_arguments = _get_attr_or_item(item, "arguments", "")
                        slot = existing_slot or {
                            "id": call_id or item_id,
                            "item_id": item_id,
                            "name": "",
                            "arguments": "",
                        }
                        slot.update(
                            {
                                "id": call_id or item_id,
                                "item_id": item_id,
                                "name": name or slot.get("name", ""),
                                "started": "true",
                            }
                        )
                        if isinstance(initial_arguments, str) and initial_arguments:
                            slot["arguments"] = initial_arguments
                        response_tool_items[key] = slot
                        if call_id:
                            response_tool_items[call_id] = slot
                        if item_id:
                            response_tool_items[item_id] = slot
                        if name:
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_start=ToolCallStartEvent(
                                    id=call_id or item_id,
                                    name=name,
                                    index=response_tool_start_count,
                                ),
                            )
                            response_tool_start_count += 1

                elif event_type == "response.output_item.done":
                    item = _get_attr_or_item(event, "item", None)
                    item_type = str(_get_attr_or_item(item, "type", "") or "").strip()
                    output_index = _get_attr_or_item(event, "output_index", None)
                    output_index_value = (
                        output_index if isinstance(output_index, int) else None
                    )
                    if item_type in _OPENAI_UNSUPPORTED_EXECUTABLE_OUTPUT_ITEMS:
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=(
                                "Responses API completed an executable output item "
                                f"that MiniCode cannot safely run: {item_type}"
                            ),
                            raw={
                                "provider": "openai_responses",
                                "provider_error_type": "protocol",
                                "error_type": "api",
                                "event_type": event_type,
                                "output_item_type": item_type,
                            },
                        )
                        return
                    activity = _openai_output_item_activity(
                        item,
                        output_index=output_index_value,
                        item_metadata=response_output_item_metadata,
                        terminal_status="completed",
                    )
                    if accept_provider_activity(activity):
                        yield StreamEvent(
                            type=StreamEventType.PROVIDER_ACTIVITY,
                            provider_activity=activity,
                        )
                    if item_type == "function_call":
                        tool_call, protocol_error = finalize_response_function_call(
                            item,
                            event_type=event_type,
                        )
                        if protocol_error is not None:
                            yield protocol_error
                            return
                        if tool_call is not None:
                            pending_tool_calls.append(tool_call)
                            prefetch_event = _prefetch_tool_call_event([tool_call])
                            if prefetch_event is not None:
                                yield prefetch_event

                elif event_type == "response.function_call_arguments.delta":
                    event_call_id = str(getattr(event, "call_id", "") or "").strip()
                    item_id = str(getattr(event, "item_id", "") or "").strip()
                    call_id = event_call_id or item_id
                    delta = str(getattr(event, "delta", "") or "")
                    if not call_id:
                        yield _responses_tool_protocol_error(
                            "missing_function_call_id",
                            event_type=event_type,
                            item_id=item_id,
                        )
                        return
                    if any(
                        key in finalized_response_tool_shapes
                        for key in (event_call_id, item_id)
                        if key
                    ):
                        yield _responses_tool_protocol_error(
                            "function_delta_after_done",
                            event_type=event_type,
                            item_id=item_id,
                            call_id=event_call_id or item_id,
                        )
                        return
                    if delta:
                        slot = response_tool_items.get(
                            call_id
                        ) or response_tool_items.get(item_id)
                        if slot is None:
                            slot = {
                                "id": call_id,
                                "item_id": item_id,
                                "name": str(
                                    _get_attr_or_item(event, "name", "") or ""
                                ).strip(),
                                "arguments": "",
                                "started": "false",
                            }
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
                    tool_call, protocol_error = finalize_response_function_call(
                        event,
                        event_type=event_type,
                    )
                    if protocol_error is not None:
                        yield protocol_error
                        return
                    if tool_call is not None:
                        pending_tool_calls.append(tool_call)
                        prefetch_event = _prefetch_tool_call_event([tool_call])
                        if prefetch_event is not None:
                            yield prefetch_event

                elif event_type in _OPENAI_PROVIDER_ACTIVITY_EVENTS:
                    activity = _openai_provider_activity(
                        event_type,
                        event,
                        response_output_item_metadata,
                    )
                    if accept_provider_activity(activity):
                        yield StreamEvent(
                            type=StreamEventType.PROVIDER_ACTIVITY,
                            provider_activity=activity,
                        )

                elif event_type == "response.image_generation_call.partial_image":
                    # Partial images replace one another at the provider. Emitting
                    # each base64 frame as a new chat image would create a noisy
                    # gallery of obsolete previews, so keep bytes out of the
                    # transcript and project one stable generating activity. The
                    # completed image is recovered from response.completed.output.
                    activity = _openai_provider_activity(
                        "response.image_generation_call.generating",
                        event,
                        response_output_item_metadata,
                    )
                    if accept_provider_activity(activity):
                        yield StreamEvent(
                            type=StreamEventType.PROVIDER_ACTIVITY,
                            provider_activity=activity,
                        )

                elif event_type == "response.completed":
                    response_obj = getattr(event, "response", None)
                    if not response_obj:
                        # A terminal frame with no response object carries no
                        # usage, finish_reason or output. Marking the stream
                        # terminal here would let the eof guard below pass and
                        # emit a successful zero-usage DONE, so refuse exactly
                        # like the unclassified-event branch does.
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=(
                                "Responses API sent response.completed without a "
                                "response object."
                            ),
                            raw={
                                "provider": "openai_responses",
                                "provider_error_type": "protocol",
                                "error_type": "api",
                                "event_type": event_type,
                                "protocol_error_code": (
                                    "terminal_event_without_response"
                                ),
                            },
                        )
                        return
                    saw_terminal_response_event = True
                    unsupported_items = _unsupported_response_output_item_types(
                        response_obj
                    )
                    if unsupported_items:
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=(
                                "Responses API completed unsupported executable "
                                "output items: " + ", ".join(unsupported_items)
                            ),
                            raw={
                                "provider": "openai_responses",
                                "provider_error_type": "protocol",
                                "error_type": "api",
                                "event_type": event_type,
                                "output_item_types": unsupported_items,
                            },
                        )
                        return
                    for activity in _openai_terminal_output_activities(
                        response_obj,
                        response_output_item_metadata,
                    ):
                        if accept_provider_activity(activity):
                            yield StreamEvent(
                                type=StreamEventType.PROVIDER_ACTIVITY,
                                provider_activity=activity,
                            )
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
                            cache_read_input_tokens=_get_cached_prompt_tokens(
                                usage_obj
                            ),
                            cache_creation_input_tokens=_get_cache_creation_prompt_tokens(
                                usage_obj
                            ),
                            reasoning_output_tokens=_get_reasoning_output_tokens(
                                usage_obj
                            ),
                            cost_usd=_get_usage_cost_usd(usage_obj),
                        )
                    output_items = _extract_response_output_items(response_obj)
                    response_id = str(
                        _get_attr_or_item(response_obj, "id", "") or ""
                    ).strip()
                    completed_response_provider_items = (
                        _responses_provider_items_from_response(response_obj)
                    )
                    terminal_tool_calls = _responses_tool_calls_from_provider_items(
                        completed_response_provider_items
                    )
                    completed_response_message_phase = (
                        _responses_message_phase_from_response(response_obj)
                    )
                    raw_done.update(
                        {
                            "provider": "openai_responses",
                            "model": kwargs["model"],
                            "event_type": event_type,
                            "finish_reason": finish_reason,
                            "usage": _raw_usage_metadata(usage_obj),
                        }
                    )
                    if response_id:
                        raw_done["response_id_hash"] = _short_sha256(response_id)
                    if output_items:
                        raw_done["output_items"] = output_items
                    if completed_response_provider_items:
                        raw_done["provider_items_summary"] = (
                            _responses_provider_items_metadata(
                                completed_response_provider_items
                            )
                        )
                    if completed_response_message_phase:
                        raw_done["response_message_phase"] = (
                            completed_response_message_phase
                        )
                    raw_done["safety"] = _provider_trace_safety(output_items)
                    for recovered_event in terminal_response_reasoning_events(
                        response_obj,
                        recovered_from="response.completed",
                    ):
                        full_reasoning_summary += recovered_event.content
                        yield recovered_event
                    for recovered_event in terminal_response_text_events(
                        response_obj,
                        recovered_from="response.completed",
                    ):
                        full_text += recovered_event.content
                        yield recovered_event
                elif event_type == "response.incomplete":
                    response_obj = getattr(event, "response", None)
                    unsupported_items = _unsupported_response_output_item_types(
                        response_obj
                    )
                    if unsupported_items:
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=(
                                "Responses API returned unsupported executable "
                                "output items before completion: "
                                + ", ".join(unsupported_items)
                            ),
                            raw={
                                "provider": "openai_responses",
                                "provider_error_type": "protocol",
                                "error_type": "api",
                                "event_type": event_type,
                                "output_item_types": unsupported_items,
                            },
                        )
                        return
                    for activity in _openai_terminal_output_activities(
                        response_obj,
                        response_output_item_metadata,
                    ):
                        if accept_provider_activity(activity):
                            yield StreamEvent(
                                type=StreamEventType.PROVIDER_ACTIVITY,
                                provider_activity=activity,
                            )
                    finish_reason = (
                        _response_finish_reason(response_obj) or "incomplete"
                    )
                    output_items = _extract_response_output_items(response_obj)
                    if finish_reason.strip().lower() in _RESPONSES_MAX_OUTPUT_REASONS:
                        saw_terminal_response_event = True
                        usage_obj = _get_attr_or_item(response_obj, "usage", None)
                        if usage_obj:
                            usage = UsageInfo(
                                input_tokens=_get_usage_field(
                                    usage_obj, "input_tokens"
                                ),
                                output_tokens=_get_usage_field(
                                    usage_obj, "output_tokens"
                                ),
                                cache_read_input_tokens=_get_cached_prompt_tokens(
                                    usage_obj
                                ),
                                cache_creation_input_tokens=_get_cache_creation_prompt_tokens(
                                    usage_obj
                                ),
                                reasoning_output_tokens=_get_reasoning_output_tokens(
                                    usage_obj
                                ),
                                cost_usd=_get_usage_cost_usd(usage_obj),
                            )
                        completed_response_provider_items = (
                            _responses_provider_items_from_response(response_obj)
                        )
                        terminal_tool_calls = _responses_tool_calls_from_provider_items(
                            completed_response_provider_items
                        )
                        completed_response_message_phase = (
                            _responses_message_phase_from_response(response_obj)
                        )
                        raw_done.update(
                            {
                                "provider": "openai_responses",
                                "model": kwargs["model"],
                                "event_type": event_type,
                                "finish_reason": finish_reason,
                                "usage": _raw_usage_metadata(usage_obj),
                                "output_items": output_items,
                                "safety": _provider_trace_safety(output_items),
                            }
                        )
                        for recovered_event in terminal_response_reasoning_events(
                            response_obj,
                            recovered_from="response.incomplete",
                        ):
                            full_reasoning_summary += recovered_event.content
                            yield recovered_event
                        for recovered_event in terminal_response_text_events(
                            response_obj,
                            recovered_from="response.incomplete",
                        ):
                            full_text += recovered_event.content
                            yield recovered_event
                        continue
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=(
                            f"Incomplete response returned, reason: {finish_reason}"
                        ),
                        raw={
                            "provider": "openai_responses",
                            "model": kwargs["model"],
                            "event_type": event_type,
                            "finish_reason": finish_reason,
                            "output_items": output_items,
                            "safety": _provider_trace_safety(output_items),
                        },
                    )
                    return
                elif event_type == "response.failed":
                    response_obj = _get_attr_or_item(event, "response", None)
                    for activity in _openai_terminal_output_activities(
                        response_obj,
                        response_output_item_metadata,
                    ):
                        if accept_provider_activity(activity):
                            yield StreamEvent(
                                type=StreamEventType.PROVIDER_ACTIVITY,
                                provider_activity=activity,
                            )
                    error = (
                        _get_attr_or_item(event, "error", None)
                        or _get_attr_or_item(response_obj, "error", None)
                        or event
                    )
                    message, error_raw = _responses_error_event_raw(
                        event_type,
                        error,
                        fallback="Responses API response failed",
                    )
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=f"Responses API response failed: {message}",
                        raw=error_raw,
                    )
                    return
                elif event_type in {"error", "response.error"}:
                    error = _get_attr_or_item(event, "error", None) or event
                    message, error_raw = _responses_error_event_raw(
                        event_type,
                        error,
                        fallback="Responses API stream error",
                    )
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=f"Responses API error: {message}",
                        raw=error_raw,
                    )
                    return
                elif event_type in _OPENAI_PASSIVE_RESPONSE_STREAM_EVENTS:
                    pass
                else:
                    # Every event in the installed SDK union is explicitly
                    # classified above. Reaching this branch means the local
                    # allow-list and parser drifted apart; do not silently turn
                    # an unhandled event into a successful response.
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=(
                            "Responses API stream event was recognized but not "
                            f"handled: {event_type}"
                        ),
                        raw={
                            "provider": "openai_responses",
                            "provider_error_type": "protocol",
                            "error_type": "api",
                            "event_type": event_type,
                            "protocol_error_code": "unclassified_stream_event",
                        },
                    )
                    return
        except LifecycleStaleError:
            raise
        except Exception as exc:
            logger.error("Responses API 流式解析异常: %s", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=_adapter_error_content("LLM 流式响应异常", exc),
                raw=_adapter_error_raw(exc, "openai_responses"),
            )
            return
        finally:
            await _close_async_iterator(stream)

        if not saw_terminal_response_event:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="Responses API stream ended before a terminal response event.",
                raw={
                    "provider": "openai_responses",
                    "event_type": "eof_without_terminal",
                },
            )
            return

        # The terminal response is authoritative for executable calls. A
        # streamed call that disappears or changes at response.completed must
        # never be executed merely because its arguments.done frame arrived.
        final_tool_calls = terminal_tool_calls
        if pending_tool_calls and not terminal_tool_calls:
            protocol_error = _responses_tool_protocol_error(
                "terminal_function_call_missing",
                event_type=str(raw_done.get("event_type") or "terminal"),
            )
            protocol_error.raw.update(
                {
                    "streamed_count": len(pending_tool_calls),
                    "streamed_call_id_hashes": [
                        _short_sha256(call.id) for call in pending_tool_calls
                    ],
                }
            )
            yield protocol_error
            return
        if terminal_tool_calls and pending_tool_calls:
            streamed_shape = [
                (call.id, call.name, _json_fingerprint(call.arguments))
                for call in pending_tool_calls
            ]
            terminal_shape = [
                (call.id, call.name, _json_fingerprint(call.arguments))
                for call in terminal_tool_calls
            ]
            if streamed_shape != terminal_shape:
                protocol_error = _responses_tool_protocol_error(
                    "terminal_function_call_mismatch",
                    event_type=str(raw_done.get("event_type") or "terminal"),
                )
                protocol_error.raw.update(
                    {
                        "streamed_count": len(pending_tool_calls),
                        "terminal_count": len(terminal_tool_calls),
                        "streamed_call_id_hashes": [
                            _short_sha256(call.id) for call in pending_tool_calls
                        ],
                        "terminal_call_id_hashes": [
                            _short_sha256(call.id) for call in terminal_tool_calls
                        ],
                    }
                )
                yield protocol_error
                return
        if final_tool_calls:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=final_tool_calls,
            )

        raw_done["request_summary"] = build_responses_request_summary(
            wire_request_payload or kwargs,
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
        context: LLMSideCallContext | None = None,
    ) -> str:
        """Collect one standalone Responses stream through response.completed."""
        instructions, input_messages = _split_responses_instructions(messages)
        api_input = self._build_responses_input(input_messages)

        side_options = context.options if context is not None else None
        model = (
            self.small_fast_model_id()
            if side_options is not None and side_options.use_small_fast_model
            else self._settings.model
        )
        client_metadata = _responses_side_client_metadata(side_options)
        prompt_cache_enabled = side_options is None or side_options.enable_prompt_cache
        prompt_cache_key = (
            _responses_prompt_cache_key(client_metadata) if prompt_cache_enabled else ""
        )
        explicit_prompt_cache = bool(
            prompt_cache_enabled
            and prompt_cache_key
            and _is_official_openai_prompt_cache_breakpoint_model(
                self._settings,
                model,
            )
        )
        if explicit_prompt_cache:
            api_input, explicit_prompt_cache = _responses_explicit_prompt_cache_input(
                instructions,
                api_input,
            )
        responses_tools: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "model": model,
            "input": api_input,
            "stream": True,
            "store": False,
        }
        if side_options is not None and side_options.output_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": side_options.output_schema_name,
                    "strict": True,
                    "schema": side_options.output_schema,
                }
            }
        if side_options is not None and side_options.hosted_web_search:
            if not self.supports_hosted_web_search():
                raise RuntimeError(
                    "Hosted web search requires the OpenAI Responses API"
                )
            if side_options.web_search_blocked_domains:
                raise ValueError(
                    "OpenAI hosted web search does not support blocked_domains"
                )
            hosted_tool: dict[str, Any] = {
                "type": "web_search",
                "external_web_access": True,
            }
            if side_options.web_search_allowed_domains:
                hosted_tool["filters"] = {
                    "allowed_domains": list(side_options.web_search_allowed_domains)
                }
            responses_tools = [hosted_tool]
        if instructions and not explicit_prompt_cache:
            kwargs["instructions"] = instructions
        resolved_max_output_tokens = _resolved_responses_max_output_tokens(max_tokens)
        if resolved_max_output_tokens is not None:
            kwargs["max_output_tokens"] = resolved_max_output_tokens
        # Codex sends this complete tool-control shape even when no tools are
        # projected. Several Responses-compatible coding gateways require it.
        kwargs["tools"] = responses_tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = True
        kwargs["client_metadata"] = client_metadata
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key
        # Standalone side queries may opt into explicit-only caching; a normal
        # multi-turn simple call must retain the provider's implicit latest
        # message breakpoint just like the streaming agent path.
        if explicit_prompt_cache and side_options is not None:
            kwargs["prompt_cache_options"] = {"mode": "explicit"}
        identity_headers = _responses_identity_headers(client_metadata)
        if identity_headers:
            kwargs["extra_headers"] = identity_headers
        if prompt_cache_enabled:
            prompt_cache_retention = _prompt_cache_retention_request(self._settings)
            if prompt_cache_retention:
                kwargs["prompt_cache_retention"] = prompt_cache_retention

        if side_options is not None and side_options.disable_reasoning:
            disabled_reasoning = _responses_disabled_reasoning_request(
                self._settings,
                model=model,
            )
            if disabled_reasoning:
                kwargs["reasoning"] = disabled_reasoning
        else:
            reasoning_request = _responses_reasoning_request(
                self._settings,
                model=model,
            )
            if reasoning_request:
                kwargs["reasoning"] = reasoning_request

        try:
            stream = await self._create_responses_request(
                kwargs,
                metadata=context.request_metadata() if context is not None else None,
            )
            try:
                return await self._collect_simple_responses_stream(
                    stream,
                    model=model,
                    context=context,
                )
            finally:
                await _close_async_iterator(stream)
        except LifecycleStaleError:
            raise
        except Exception as exc:
            logger.error("Responses API simple_chat 失败: %s", exc)
            raise RuntimeError(f"LLM 调用失败: {_clean_error_message(exc)}") from exc

    async def _collect_simple_responses_stream(
        self,
        stream: Any,
        *,
        model: str,
        context: LLMSideCallContext | None = None,
    ) -> str:
        """Collect text/citations/usage and reject every non-completed terminal."""

        delta_parts: list[str] = []
        done_parts: list[str] = []
        citations: list[dict[str, Any]] = []
        completed_text = ""
        completed_usage: Any = None
        saw_completed = False

        def add_citations(value: Any) -> None:
            for citation in _extract_url_citations(value):
                if citation not in citations:
                    citations.append(citation)

        def error_message(event: Any, default: str) -> str:
            response_obj = _get_attr_or_item(event, "response", None)
            error = _get_attr_or_item(event, "error", None) or _get_attr_or_item(
                response_obj, "error", None
            )
            message, _details = _responses_error_details(
                error,
                fallback=default,
            )
            return message

        async for event in stream:
            event_type = str(_get_attr_or_item(event, "type", "") or "")
            if event_type == "response.output_text.delta":
                delta = _get_attr_or_item(event, "delta", "")
                if isinstance(delta, str) and delta:
                    delta_parts.append(delta)
            elif event_type == "response.output_text.done":
                done_text = _get_attr_or_item(event, "text", "")
                if isinstance(done_text, str) and done_text:
                    done_parts.append(done_text)
                add_citations(event)
            elif event_type == "response.completed":
                response_obj = _get_attr_or_item(event, "response", None)
                completed_usage = _get_attr_or_item(response_obj, "usage", None)
                direct_text = _get_attr_or_item(response_obj, "output_text", "")
                if isinstance(direct_text, str) and direct_text:
                    completed_text = direct_text
                output = _get_attr_or_item(response_obj, "output", []) or []
                if isinstance(output, list):
                    if not completed_text:
                        completed_text = "".join(
                            _responses_message_text_from_item(item)
                            for item in output
                            if str(_get_attr_or_item(item, "type", "") or "")
                            == "message"
                        )
                    for item in output:
                        if str(_get_attr_or_item(item, "type", "") or "") != "message":
                            continue
                        for content in _get_attr_or_item(item, "content", []) or []:
                            add_citations(content)
                saw_completed = True
                break
            elif event_type == "response.incomplete":
                response_obj = _get_attr_or_item(event, "response", None)
                reason = _response_finish_reason(response_obj) or "unknown"
                raise RuntimeError(f"Incomplete response returned, reason: {reason}")
            elif event_type == "response.failed":
                raise RuntimeError(
                    f"Responses API response failed: {error_message(event, 'unknown error')}"
                )
            elif event_type in {"error", "response.error"}:
                raise RuntimeError(
                    f"Responses API error: {error_message(event, 'unknown error')}"
                )

        if not saw_completed:
            raise RuntimeError("Responses API stream closed before response.completed")

        # Side calls (compaction/recovery/web/memory) contribute to the same
        # turn/global usage accounting as the main stream.
        self.record_non_stream_usage(
            completed_usage,
            provider=str(self._settings.provider or "openai"),
            model_id=model,
            input_includes_cache_read=True,
            context=context,
        )

        text = (completed_text or "".join(delta_parts) or "".join(done_parts)).strip()
        if citations:
            source_lines = ["Sources:"]
            source_lines.extend(
                f"- {citation.get('title') or citation['url']}: {citation['url']}"
                for citation in citations
            )
            text = f"{text}\n\n{chr(10).join(source_lines)}".strip()
        if not text:
            raise RuntimeError("LLM 返回空内容")
        return text

    def _build_responses_input(
        self, messages: list[LLMMessage]
    ) -> list[dict[str, Any]]:
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
                        parts.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{media_type};base64,{data}",
                                "detail": "auto",
                            }
                        )
                    for doc in msg.documents:
                        media_type = doc.get("media_type") or "application/pdf"
                        data = doc.get("data") or ""
                        if not data:
                            continue
                        parts.append(
                            {
                                "type": "input_file",
                                "filename": doc.get("file_name") or "attachment.pdf",
                                "file_data": f"data:{media_type};base64,{data}",
                            }
                        )
                    result.append(
                        {
                            "role": "user",
                            "content": parts or content_text,
                        }
                    )
                else:
                    result.append(
                        {
                            "role": "user",
                            "content": _message_content_text(msg.content),
                        }
                    )
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
        tools = canonicalize_tool_schemas(tools)
        result = []
        for tool in tools:
            func = tool.get("function", {})
            strict = bool(func.get("strict", False))
            parameters = _normalize_schema_for_openai(func.get("parameters", {}))
            if strict:
                # pi: strict tools need required-all + null-wrap or OpenAI
                # rejects every request; fall back to non-strict when the
                # schema cannot be expressed in the strict subset.
                strict_parameters = strict_schema_for_openai(func.get("parameters", {}))
                if strict_parameters is not None:
                    parameters = strict_parameters
                else:
                    strict = False
            result.append(
                {
                    "type": "function",
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "parameters": parameters,
                    "strict": strict,
                }
            )
        return result

    @staticmethod
    def _normalize_chat_tools(
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        tools = canonicalize_tool_schemas(tools)
        normalized_tools: list[dict[str, Any]] = []
        for tool in tools:
            normalized_tool = dict(tool)
            function_def = dict(normalized_tool.get("function", {}))
            # Preserve the tool's strict flag so OpenAI structured outputs are
            # requested for tools that declare strict=True (matching cc's
            # behavior), converting the schema to the strict subset like pi's
            # makeStrictJsonSchema. A schema that cannot be expressed strictly
            # falls back to non-strict instead of a deterministic 400.
            strict = bool(function_def.get("strict", False))
            parameters = _normalize_schema_for_openai(
                function_def.get("parameters", {})
            )
            if strict:
                strict_parameters = strict_schema_for_openai(
                    function_def.get("parameters", {})
                )
                if strict_parameters is not None:
                    parameters = strict_parameters
                else:
                    strict = False
            function_def["strict"] = strict
            function_def["parameters"] = parameters
            normalized_tool["function"] = function_def
            normalized_tools.append(normalized_tool)
        return normalized_tools

    def _chat_completions_url(self) -> str:
        base_url = _normalized_openai_base_url(self._settings.base_url)
        return f"{base_url}/chat/completions"

    def _chat_headers(self) -> dict[str, str]:
        headers = self._openai_transport_headers()
        headers.update(self._default_headers)
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
        *,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._http_client is None:
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
        chat_reasoning_parts: list[str] = []
        chat_reasoning_field = ""
        stream_started_at = time.monotonic()
        first_sse_at: float | None = None
        previous_sse_at: float | None = None
        max_sse_gap_ms = 0.0
        sse_event_count = 0
        content_delta_count = 0

        sent_payload, headers = await self._prepare_http_request(
            payload,
            metadata=metadata,
            base_headers=self._chat_headers(),
        )
        payload_messages = (
            sent_payload.get("messages")
            if isinstance(sent_payload.get("messages"), list)
            else []
        )
        payload_tools = sent_payload.get("tools")
        payload_metadata = sent_payload.get("metadata")
        request_summary = _safe_request_summary(
            model=str(sent_payload.get("model") or self._settings.model),
            wire_api="chat",
            instructions=_instruction_text_from_chat_payload(payload_messages),
            tools=payload_tools if isinstance(payload_tools, list) else [],
            request_metadata=(
                payload_metadata if isinstance(payload_metadata, dict) else {}
            ),
            input_items=_chat_payload_input_items(payload_messages),
            request_params=sent_payload,
        )
        raw_done["request_summary"] = request_summary
        async with _openai_http_stream(
            self._http_client,
            "POST",
            self._chat_completions_url(),
            headers=headers,
            json_payload=sent_payload,
        ) as response:
            await emit_provider_lifecycle_response(
                metadata,
                response.status_code,
                getattr(response, "headers", {}),
            )
            await self._chat_http_raise_for_status(response)

            malformed_budget = SSEMalformedBudget()
            async for raw_payload in iter_sse_data(response):
                now = time.monotonic()
                if first_sse_at is None:
                    first_sse_at = now
                if previous_sse_at is not None:
                    max_sse_gap_ms = max(max_sse_gap_ms, (now - previous_sse_at) * 1000)
                previous_sse_at = now
                sse_event_count += 1
                line = raw_payload.strip()
                if not line:
                    continue
                if line == "[DONE]":
                    saw_terminal_event = True
                    _append_provider_timeline(provider_timeline, "chat.done")
                    break

                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    malformed_budget.reject(line)
                    logger.debug(
                        "Ignoring malformed chat stream event (%d chars)",
                        len(line),
                    )
                    continue
                malformed_budget.accept()

                error_payload = chunk.get("error")
                if error_payload is not None:
                    # Classify like the Responses path does. Emitting a bare
                    # message left provider_stream_error_event with nothing to
                    # read, so an auth failure streamed over HTTP 200 was
                    # classified "network" and replayed the full retry ladder.
                    # A gateway may send a string here, not only an object.
                    message, error_raw = _responses_error_event_raw(
                        "error",
                        error_payload,
                        fallback="Chat completion stream failed",
                        provider="openai_chat_completions",
                    )
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=f"Chat completion stream failed: {message}",
                        raw=error_raw,
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
                        cache_creation_input_tokens=_get_cache_creation_prompt_tokens(
                            usage_obj
                        ),
                        reasoning_output_tokens=_get_reasoning_output_tokens(usage_obj),
                        cost_usd=_get_usage_cost_usd(usage_obj),
                    )
                    raw_done["usage"] = _raw_usage_metadata(usage_obj)
                    _append_provider_timeline(
                        provider_timeline, "chat.usage", usage_present=True
                    )

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}

                reasoning_content = ""
                reasoning_field = ""
                for candidate_field in (
                    "reasoning_content",
                    "reasoning",
                    "reasoning_text",
                ):
                    candidate = delta.get(candidate_field)
                    if isinstance(candidate, str) and candidate:
                        reasoning_content = candidate
                        reasoning_field = candidate_field
                        break
                if reasoning_content:
                    chat_reasoning_field = chat_reasoning_field or reasoning_field
                    chat_reasoning_parts.append(reasoning_content)
                    _append_provider_timeline(
                        provider_timeline,
                        "chat.reasoning_content.delta",
                        delta_chars=len(reasoning_content),
                        field=reasoning_field,
                    )
                    # OpenAI-compatible Chat endpoints stream reasoning in
                    # provider-specific delta fields. Project the delta at the
                    # same boundary as Responses/Anthropic thinking events;
                    # the accumulated provider item below is still retained
                    # for wire replay of the completed assistant message.
                    yield StreamEvent(
                        type=StreamEventType.THINKING_CHUNK,
                        content=reasoning_content,
                        raw={
                            "provider_reasoning_type": reasoning_field,
                        },
                        content_kind="thinking",
                    )

                content = delta.get("content")
                if content:
                    content_delta_count += 1
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
                                id=slot["id"],
                                name=slot["name"],
                                index=idx,
                            ),
                        )
                    elif (
                        slot["_start_emitted"]
                        and slot["_delta_bytes"] >= _DELTA_DEBOUNCE_BYTES
                    ):
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
                        # Never prefetch an incomplete batch: the guard after
                        # the loop fails the turn instead of letting half of
                        # what the model asked for reach the executor.
                        prefetch_calls = accumulator.finalize()
                        prefetch_event = (
                            None
                            if accumulator.dropped_incomplete
                            else _prefetch_tool_call_event(prefetch_calls)
                        )
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

        logger.info(
            "Chat SSE stream summary provider_host=%s model=%s events=%d content_deltas=%d "
            "first_chunk_ms=%s total_ms=%.1f max_gap_ms=%.1f",
            _provider_host(str(self._settings.base_url)),
            self._settings.model,
            sse_event_count,
            content_delta_count,
            ""
            if first_sse_at is None
            else f"{(first_sse_at - stream_started_at) * 1000:.1f}",
            (time.monotonic() - stream_started_at) * 1000,
            max_sse_gap_ms,
        )

        for evt in _splitter_events(reasoning_splitter.flush()):
            if evt.type == StreamEventType.TEXT_CHUNK:
                full_text += evt.content
            yield evt

        tool_call_events = accumulator.finalize()
        incomplete_error = _chat_incomplete_tool_call_error(
            accumulator.dropped_incomplete,
            tool_call_count=len(tool_call_events),
            finish_reason=finish_reason,
            request_summary=request_summary or {},
        )
        if incomplete_error is not None:
            yield incomplete_error
            return
        protocol_error = _chat_tool_protocol_error(
            finish_reason,
            tool_call_events,
        )
        if protocol_error:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=protocol_error,
                raw={
                    "provider": "openai_chat_completions",
                    "event_type": "tool_finish_reason_mismatch",
                    "finish_reason": finish_reason,
                    "tool_call_count": len(tool_call_events),
                    "provider_error_type": "protocol",
                    "error_type": "api",
                    "request_summary": request_summary or {},
                    "safety": _provider_trace_safety(),
                },
            )
            return
        if tool_call_events:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL, tool_calls=tool_call_events
            )

        provider_items = []
        if chat_reasoning_parts:
            provider_items.append(
                {
                    "type": "chat_reasoning",
                    "field": chat_reasoning_field or "reasoning_content",
                    "content": "".join(chat_reasoning_parts),
                }
            )

        yield StreamEvent(
            type=StreamEventType.DONE,
            usage=usage,
            finish_reason=finish_reason,
            raw=raw_done,
            provider_items=provider_items,
        )

    async def _stream_chat_completions_http(
        self,
        kwargs: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        def build_payload_request_summary(payload: dict[str, Any]) -> dict[str, Any]:
            payload_messages = (
                payload.get("messages")
                if isinstance(payload.get("messages"), list)
                else []
            )
            return _safe_request_summary(
                model=str(payload.get("model") or self._settings.model),
                wire_api="chat",
                instructions=_instruction_text_from_chat_payload(payload_messages),
                tools=payload.get("tools")
                if isinstance(payload.get("tools"), list)
                else [],
                request_metadata=payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {},
                input_items=_chat_payload_input_items(payload_messages),
                request_params=payload,
            )

        payload = _without_unsupported_chat_fields(
            kwargs,
            self._chat_unsupported_fields,
        )
        try:
            async for event in self._emit_chat_http_stream_events(
                payload,
                build_payload_request_summary(payload),
                metadata=metadata,
            ):
                yield event
            return
        except LifecycleStaleError:
            raise
        except Exception as exc:
            # MiniCode sends OpenAI's optional chat fields (metadata, store,
            # prompt_cache_key, stream_options) to whatever OpenAI-compatible
            # gateway the user configured. A stricter gateway answers 400
            # "unrecognized argument", which the request build cannot predict.
            # Drop that one field, remember it for this adapter, and retry once:
            # the rejection happens before any SSE data, so no yielded output
            # can be duplicated.
            rejected = _rejected_optional_chat_field(exc, payload)
            if not rejected:
                logger.error(
                    "Chat Completions API request or stream parsing failed: %s", exc
                )
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    content=_adapter_error_content("LLM API call failed", exc),
                    raw=_adapter_error_raw(exc, "openai_chat_completions"),
                )
                return
            self._chat_unsupported_fields.add(rejected)
            logger.info(
                "Chat gateway rejected optional field %r; retrying without it", rejected
            )

        retry_payload = _without_unsupported_chat_fields(
            kwargs,
            self._chat_unsupported_fields,
        )
        try:
            async for event in self._emit_chat_http_stream_events(
                retry_payload,
                build_payload_request_summary(retry_payload),
                metadata=metadata,
            ):
                yield event
        except LifecycleStaleError:
            raise
        except Exception as exc:
            logger.error(
                "Chat Completions API request or stream parsing failed: %s", exc
            )
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=_adapter_error_content("LLM API call failed", exc),
                raw=_adapter_error_raw(exc, "openai_chat_completions"),
            )

    # ══════════════════════════════════════════════════════════════
    #  Chat Completions API 实现（wire_api="chat"）
    # ══════════════════════════════════════════════════════════════

    async def _stream_chat_completions(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        context: LLMSideCallContext | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """使用 Chat Completions API 流式调用。"""
        openai_messages = _openai_chat_messages(messages)
        request_metadata = sanitize_llm_request_metadata(metadata)
        side_options = context.options if context is not None else None
        model = (
            self.small_fast_model_id()
            if side_options is not None and side_options.use_small_fast_model
            else self._settings.model
        )
        prompt_cache_enabled = side_options is None or side_options.enable_prompt_cache
        cache_identity = (
            _responses_side_client_metadata(side_options)
            if side_options is not None
            else _responses_client_metadata(request_metadata)
        )
        prompt_cache_key = (
            _responses_prompt_cache_key(cache_identity) if prompt_cache_enabled else ""
        )
        explicit_prompt_cache = bool(
            prompt_cache_key
            and _is_official_openai_prompt_cache_breakpoint_model(
                self._settings,
                model,
            )
        )
        if explicit_prompt_cache:
            openai_messages, explicit_prompt_cache = (
                _chat_explicit_prompt_cache_messages(openai_messages)
            )
        requested_max_tokens = (
            max_tokens
            if max_tokens is not None
            else side_options.max_tokens
            if side_options is not None
            else None
        )

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "stream": True,
            # Ask the gateway to emit a trailing usage-only chunk (choices: [],
            # usage: {...}) after generation. Without this the Chat Completions
            # wire API sends no token counts at all and usage stays at zero.
            "stream_options": {"include_usage": True},
        }
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key
        # Do not disable GPT-5.6 implicit latest-message caching on the main
        # append-only conversation.  Explicit-only mode is limited to side
        # queries whose prompt suffix is request-specific.
        if explicit_prompt_cache and side_options is not None:
            kwargs["prompt_cache_options"] = {"mode": "explicit"}
        configured_or_requested_max_tokens = _resolved_chat_max_tokens(
            self._settings,
            model=model,
            requested_max_tokens=requested_max_tokens,
        )
        if configured_or_requested_max_tokens > 0:
            kwargs[_chat_max_tokens_field(self._settings, model=model)] = (
                clamp_max_tokens_to_context(
                    context_window=self._settings.context_window,
                    messages=messages,
                    tools=tools,
                    max_tokens=configured_or_requested_max_tokens,
                )
            )
        if self._settings.seed is not None:
            kwargs["seed"] = self._settings.seed
        reasoning_effort = _chat_reasoning_effort(
            self._settings,
            model=model,
        )
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if request_metadata:
            kwargs["metadata"] = request_metadata
            kwargs["store"] = False
        if side_options is not None and side_options.output_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": side_options.output_schema_name,
                    "strict": True,
                    "schema": side_options.output_schema,
                },
            }

        if tools:
            kwargs["tools"] = self._normalize_chat_tools(tools)
            kwargs["tool_choice"] = "auto"

        def build_chat_request_summary(payload: dict[str, Any]) -> dict[str, Any]:
            payload_messages = (
                payload.get("messages")
                if isinstance(payload.get("messages"), list)
                else []
            )
            return _safe_request_summary(
                model=str(payload.get("model") or model),
                wire_api="chat",
                instructions=_instruction_text_from_chat_payload(payload_messages),
                tools=payload.get("tools")
                if isinstance(payload.get("tools"), list)
                else [],
                request_metadata=payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {},
                input_items=_chat_payload_input_items(payload_messages),
                request_params=payload,
            )

        async for event in self._stream_chat_completions_http(
            kwargs,
            metadata=metadata,
        ):
            yield event
        return

    async def _simple_chat_completions(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
        context: LLMSideCallContext | None = None,
    ) -> str:
        """Consume the normal Chat Completions stream to its terminal event."""
        side_options = context.options if context is not None else None
        model = (
            self.small_fast_model_id()
            if side_options is not None and side_options.use_small_fast_model
            else self._settings.model
        )
        text_parts: list[str] = []
        usage = UsageInfo()
        saw_done = False
        async for event in self._stream_chat_completions(
            messages,
            metadata=context.request_metadata() if context is not None else None,
            context=context,
            max_tokens=max_tokens,
        ):
            if event.type == StreamEventType.TEXT_CHUNK and event.content:
                text_parts.append(event.content)
            elif event.type == StreamEventType.DONE:
                usage = event.usage
                saw_done = True
            elif event.type == StreamEventType.ERROR:
                raise RuntimeError(event.content or "Chat completion stream failed")

        if not saw_done:
            raise RuntimeError("Chat completion stream ended before DONE")

        self.record_non_stream_usage(
            usage,
            provider=str(self._settings.provider or "openai"),
            model_id=model,
            input_includes_cache_read=True,
            context=context,
        )
        text = "".join(text_parts).strip()
        if text:
            return text
        raise RuntimeError("LLM 返回空内容")
