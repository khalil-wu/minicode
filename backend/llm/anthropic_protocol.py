"""Anthropic Messages protocol mapping helpers for the adapter.

Extracted from ``backend/llm/anthropic_adapter.py`` so wire-protocol mapping,
error projection and safe request summaries are independent of the adapter
class that streams them.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Model families that use Anthropic's adaptive-thinking request shape.
_ADAPTIVE_THINKING_MODEL_MARKERS = (
    "opus-4-7",
    "opus-4-8",
    "opus-5",
    "fable",
    "mythos",
    "sonnet-5",
)



from backend.agent.prompting import split_sys_prompt_prefix
from backend.llm.base import (
    ProviderActivityEvent,
    StreamEvent,
    StreamEventType,
    ToolCallEvent,
    sanitize_llm_request_metadata,
)
from backend.llm.errors import (
    classify_llm_error,
    llm_error_raw,
    llm_error_status_code,
    retry_after_seconds,
    sanitize_llm_error_message,
)
from backend.llm.openai_usage import (
    _get_usage_cost_usd,
    _get_usage_field,
)
from backend.secret_redaction import redact_secrets
from collections.abc import Mapping
from dataclasses import (
    dataclass,
    field,
)
from typing import Any
import hashlib
import json
import os
import re


def _is_adaptive_thinking_model(model_id: str) -> bool:
    return any(marker in model_id for marker in _ADAPTIVE_THINKING_MODEL_MARKERS)


def _adaptive_thinking_effort(thinking_budget: int) -> str:
    """Map a token budget onto pi's effort ladder (default high).

    pi derives the ladder from its own thinking budgets (minimal 1024 / low
    2048 / medium 8192 / high 16384); these reverse-engineered thresholds
    (16000/10000/4000/2000) are MiniCode's approximation, not pi's table.
    """
    if thinking_budget >= 16000:
        return "max"
    if thinking_budget >= 10000:
        return "xhigh"
    if thinking_budget >= 4000:
        return "high"
    if thinking_budget >= 2000:
        return "medium"
    return "low"


@dataclass(slots=True)
class _CacheEditingState:
    """Prompt-cache editing state owned by one MiniCode conversation."""

    disabled_reason: str = ""
    pending_deletions: list[str] = field(default_factory=list)
    pinned_edits: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)


def _cache_ttl_1h_enabled() -> bool:
    """Whether the 1h prompt-cache TTL is opted in (env MINICODE_CACHE_TTL_1H).

    Mirrors cc's ``should1hCacheTTL`` opt-in gating (claude.ts getCacheControl):
    cache_control stays ``{"type": "ephemeral"}`` by default and only gains
    ``"ttl": "1h"`` when explicitly enabled.
    """
    return os.getenv("MINICODE_CACHE_TTL_1H", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _cache_control(ttl_1h: bool | None = None) -> dict[str, Any]:
    """Build the cache_control marker, cc's ``getCacheControl`` equivalent."""
    if _cache_ttl_1h_enabled() if ttl_1h is None else ttl_1h:
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def _clean_error_message(exc: Any) -> str:
    """清洗错误消息，移除 HTML 标签。"""
    msg = str(exc)
    msg = re.sub(r"<[^>]+>", " ", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    msg = redact_secrets(msg)
    if len(msg) > 300:
        msg = msg[:300] + "..."
    return msg


async def _close_async_iterator(iterator: Any) -> None:
    """Close a provider stream even when parsing exits early or is cancelled.

    Returning from a protocol-error branch (or propagating ``CancelledError``) does not
    close that object automatically, so a turn could leave the HTTP response
    and connection alive until garbage collection. Cleanup must never mask the
    provider error that caused the turn to terminate.
    """

    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except (GeneratorExit, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask root error
        logger.debug(
            "Anthropic provider async iterator close failed: %s",
            _clean_error_message(exc),
        )


def _detached_anthropic_tool_input(value: Any) -> dict[str, Any] | None:
    """Detach a provider-owned tool input without accepting non-object JSON."""
    if not isinstance(value, Mapping):
        return None
    try:
        cloned = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError, OverflowError):
        return None
    return cloned if isinstance(cloned, dict) else None


_ANTHROPIC_REPLAY_CONTENT_TYPES = frozenset(
    {
        "text",
        "thinking",
        "redacted_thinking",
        "tool_use",
        "server_tool_use",
        "web_search_tool_result",
        "web_fetch_tool_result",
        "code_execution_tool_result",
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
        "tool_search_tool_result",
        "container_upload",
        # Beta Messages output blocks used by hosted/connector tools.
        "mcp_tool_use",
        "mcp_tool_result",
        "advisor_tool_result",
        "compaction",
    }
)

_ANTHROPIC_STREAM_CONTENT_TYPES = _ANTHROPIC_REPLAY_CONTENT_TYPES | {"image"}

_ANTHROPIC_DELTA_CONTENT_TYPES: dict[str, frozenset[str]] = {
    "text_delta": frozenset({"text"}),
    "citations_delta": frozenset({"text"}),
    "thinking_delta": frozenset({"thinking"}),
    "signature_delta": frozenset({"thinking"}),
    "input_json_delta": frozenset(
        {"tool_use", "server_tool_use", "mcp_tool_use"}
    ),
    "compaction_delta": frozenset({"compaction"}),
}


def _anthropic_content_delta_protocol_code(
    content_kind: str,
    delta_type: str,
) -> str:
    allowed_content_types = _ANTHROPIC_DELTA_CONTENT_TYPES.get(delta_type)
    if allowed_content_types is None:
        return "unknown_content_delta"
    if content_kind not in allowed_content_types:
        return "content_delta_kind_mismatch"
    return ""


def _detached_anthropic_value(value: Any, *, depth: int = 0) -> Any:
    """Detach a provider value into JSON-compatible provider state."""

    if depth > 10:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): detached
            for key, child in value.items()
            if str(key).strip()
            and (detached := _detached_anthropic_value(child, depth=depth + 1))
            is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            detached
            for child in value
            if (detached := _detached_anthropic_value(child, depth=depth + 1))
            is not None
        ]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json", exclude_none=True)
        except TypeError:
            dumped = model_dump(exclude_none=True)
        return _detached_anthropic_value(dumped, depth=depth + 1)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return _detached_anthropic_value(
            {
                key: child
                for key, child in attributes.items()
                if not str(key).startswith("_")
            },
            depth=depth + 1,
        )
    return None


def _detached_anthropic_content_block(value: Any) -> dict[str, Any] | None:
    detached = _detached_anthropic_value(value)
    if not isinstance(detached, dict):
        return None
    block_type = str(detached.get("type") or "").strip()
    if block_type not in _ANTHROPIC_REPLAY_CONTENT_TYPES:
        return None
    detached["type"] = block_type
    return detached


def _anthropic_provider_message_item(
    content_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not content_blocks:
        return []
    return [
        {
            "type": "anthropic_message",
            "content": [dict(block) for block in content_blocks],
        }
    ]


def _anthropic_replay_content(
    provider_items: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    for item in provider_items:
        if not isinstance(item, dict) or item.get("type") != "anthropic_message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        replay: list[dict[str, Any]] = []
        for block in content:
            detached = _detached_anthropic_content_block(block)
            if detached is not None:
                replay.append(detached)
        if replay:
            return replay
    return None


def _anthropic_tool_protocol_error(
    stop_reason: str,
    tool_calls: list[ToolCallEvent],
) -> str:
    reason = str(stop_reason or "").strip().lower()
    if tool_calls and reason not in {
        "tool_use",
        "max_tokens",
        "model_context_window_exceeded",
    }:
        return (
            "Anthropic Messages returned executable tool calls with an incompatible "
            f"stop_reason ({reason or 'missing'})."
        )
    if not tool_calls and reason == "tool_use":
        return "Anthropic Messages ended with stop_reason=tool_use but returned no complete tool calls."
    return ""


def _anthropic_stream_protocol_error(
    code: str,
    *,
    event_type: str,
    current_index: int | None = None,
    received_index: int | None = None,
    current_kind: str = "",
    delta_type: str = "",
    provider: str,
) -> StreamEvent:
    messages = {
        "invalid_event_envelope": "the provider returned a non-object stream event",
        "event_before_message_start": "a stream event arrived before message_start",
        "duplicate_message_start": "message_start was emitted more than once",
        "nested_content_block_start": "a new content block started before the previous block ended",
        "invalid_content_index": "a content event used an invalid block index",
        "duplicate_content_index": "a content block index was started more than once",
        "unknown_content_block": "the provider returned an unknown content block type",
        "missing_tool_call_id": "a tool-use block had no stable identifier",
        "missing_tool_name": "a tool-use block had no tool name",
        "duplicate_tool_call_id": "a tool-use identifier was reused in the response",
        "content_delta_without_start": "a content delta arrived without an open content block",
        "unknown_content_delta": "the provider returned an unknown content delta type",
        "content_delta_kind_mismatch": "a content delta did not match its open content block type",
        "content_stop_without_start": "a content block ended without a matching start",
        "content_index_mismatch": "a content event targeted the wrong block index",
        "content_event_after_message_delta": "content arrived after terminal message metadata",
        "message_delta_with_open_block": "message metadata arrived before the open content block ended",
        "duplicate_message_delta": "message_delta was emitted more than once",
        "message_stop_with_open_block": "the message ended while a content block was still open",
        "message_stop_without_delta": "message_stop arrived before message_delta",
        "late_event_after_message_stop": "an event arrived after message_stop",
        "unknown_stream_event": "the provider returned an unknown stream event type",
    }
    raw: dict[str, Any] = {
        "provider": provider,
        "provider_error_type": "protocol",
        "error_type": "api",
        "event_type": event_type,
        "protocol_error_code": code,
    }
    if current_index is not None:
        raw["current_content_index"] = current_index
    if received_index is not None:
        raw["received_content_index"] = received_index
    if current_kind:
        raw["current_content_kind"] = current_kind[:80]
    if delta_type:
        raw["received_delta_type"] = delta_type[:80]
    return StreamEvent(
        type=StreamEventType.ERROR,
        content=f"MiniCode Anthropic Messages 协议错误：{messages.get(code, code)}。",
        raw=raw,
    )


def _anthropic_error_fields(value: Any) -> tuple[str, str]:
    """Extract provider code/type without projecting a response body to users."""

    payload: Any = value
    if isinstance(value, BaseException):
        response = getattr(value, "response", None)
        if response is None:
            return "", ""
        try:
            payload = response.json()
        except Exception:
            text = getattr(response, "text", None)
            if not text:
                content = getattr(response, "content", None)
                text = (
                    content.decode("utf-8", errors="replace")
                    if isinstance(content, bytes)
                    else str(content or "")
                )
            try:
                payload = json.loads(str(text or ""))
            except (TypeError, ValueError):
                return "", ""
    if not isinstance(payload, Mapping):
        return "", ""
    error = payload.get("error")
    if isinstance(error, Mapping):
        payload = error
    code = str(payload.get("code") or "").strip()
    schema_type = str(payload.get("type") or "").strip()
    return code[:80], schema_type[:80]


def _anthropic_error_message(value: Any) -> str:
    """Extract the provider's safe human-readable error message."""
    if isinstance(value, BaseException):
        message = str(
            llm_error_raw(value, "anthropic").get("provider_error_message") or ""
        ).strip()
        if message:
            return _clean_error_message(message)
    if isinstance(value, Mapping):
        error = value.get("error") if isinstance(value.get("error"), Mapping) else value
        message = str(error.get("message") or value.get("message") or "").strip()
        if message:
            return _clean_error_message(message)
    return _clean_error_message(value)


def _anthropic_error_hint(
    classification: Any,
    *,
    status_code: int | None = None,
    code: str = "",
    schema_type: str = "",
) -> str:
    parts: list[str] = []
    provider_error_type = str(
        getattr(classification, "provider_error_type", "") or ""
    ).strip()
    if provider_error_type and provider_error_type != "unknown":
        parts.append(f"provider_error_type={provider_error_type}")
    if status_code is not None:
        parts.append(f"status={status_code}")
    if code:
        parts.append(f"provider_error_code={code}")
    if schema_type:
        parts.append(f"provider_error_schema_type={schema_type}")
    return " ".join(parts)


def _anthropic_exception_error_event(
    exc: Exception,
    *,
    provider: str,
) -> StreamEvent:
    classification = classify_llm_error(exc)
    status_code = llm_error_status_code(exc)
    code, schema_type = _anthropic_error_fields(exc)
    provider_message = _anthropic_error_message(exc)
    hint = _anthropic_error_hint(
        classification,
        status_code=status_code,
        code=code,
        schema_type=schema_type,
    )
    suffix = f" ({hint})" if hint else ""
    raw: dict[str, Any] = {
        "provider": provider,
        "provider_error_type": classification.provider_error_type,
        "error_type": classification.error_type,
    }
    raw.update(llm_error_raw(exc, provider))
    if status_code is not None:
        raw["status_code"] = status_code
    if code:
        raw["provider_error_code"] = code
    if schema_type:
        raw["provider_error_schema_type"] = schema_type
    if provider_message:
        raw["provider_error_message"] = provider_message
    retry_after = retry_after_seconds(exc)
    if retry_after > 0:
        raw["retry_after_seconds"] = retry_after
    return StreamEvent(
        type=StreamEventType.ERROR,
        content=(
            f"MiniCode Anthropic Messages 请求失败：{provider_message}{suffix}"
            if provider_message
            else "MiniCode Anthropic Messages 请求失败："
            f"{sanitize_llm_error_message(exc, classification, include_provider_details=False)}{suffix}"
        ),
        raw=raw,
    )


def _anthropic_declared_error_event(
    event: Mapping[str, Any],
    *,
    provider: str,
) -> StreamEvent:
    error = event.get("error") if isinstance(event.get("error"), Mapping) else {}
    message = str(
        error.get("message")
        or event.get("message")
        or "Anthropic stream error"
    )
    code, schema_type = _anthropic_error_fields(error)
    provider_message = _anthropic_error_message(error)
    classification = classify_llm_error(
        " ".join(part for part in (schema_type, code, message) if part)
    )
    status_code: int | None = None
    for candidate in (error.get("status_code"), event.get("status_code")):
        try:
            if candidate is not None:
                status_code = int(candidate)
                break
        except (TypeError, ValueError):
            continue
    hint = _anthropic_error_hint(
        classification,
        status_code=status_code,
        code=code,
        schema_type=schema_type,
    )
    suffix = f" ({hint})" if hint else ""
    raw: dict[str, Any] = {
        "provider": provider,
        "provider_error_type": classification.provider_error_type,
        "error_type": classification.error_type,
    }
    if status_code is not None:
        raw["status_code"] = status_code
    if code:
        raw["provider_error_code"] = code
    if schema_type:
        raw["provider_error_schema_type"] = schema_type
    if provider_message:
        raw["provider_error_message"] = provider_message
    for candidate in (
        error.get("retry_after_seconds"),
        event.get("retry_after_seconds"),
        error.get("retry_after"),
        event.get("retry_after"),
    ):
        try:
            delay = max(0.0, min(float(candidate), 300.0))
        except (TypeError, ValueError):
            continue
        if delay > 0:
            raw["retry_after_seconds"] = delay
            break
    return StreamEvent(
        type=StreamEventType.ERROR,
        content=f"MiniCode Anthropic Messages 请求失败：{provider_message or _clean_error_message(message)}{suffix}",
        raw=raw,
    )


def _anthropic_web_search_sources(block: Any) -> list[tuple[str, str]]:
    """Extract Claude hosted-search result URLs from one content block."""

    block_type = (
        str(block.get("type") or "")
        if isinstance(block, Mapping)
        else str(getattr(block, "type", "") or "")
    )
    if block_type != "web_search_tool_result":
        return []
    content = (
        block.get("content")
        if isinstance(block, Mapping)
        else getattr(block, "content", None)
    )
    if not isinstance(content, list):
        return []
    sources: list[tuple[str, str]] = []
    for item in content:
        title = (
            str(item.get("title") or "").strip()
            if isinstance(item, Mapping)
            else str(getattr(item, "title", "") or "").strip()
        )
        url = (
            str(item.get("url") or "").strip()
            if isinstance(item, Mapping)
            else str(getattr(item, "url", "") or "").strip()
        )
        if url and (title, url) not in sources:
            sources.append((title, url))
    return sources


def _anthropic_refusal_metadata(value: Any) -> dict[str, str]:
    """Detach the structured refusal fields without retaining arbitrary data."""

    if value is None:
        return {}
    refusal_type = str(_anthropic_field(value, "type", "") or "").strip()
    if refusal_type and refusal_type != "refusal":
        return {}
    metadata: dict[str, str] = {"type": "refusal"}
    category = str(_anthropic_field(value, "category", "") or "").strip()
    explanation = re.sub(
        r"\s+",
        " ",
        str(_anthropic_field(value, "explanation", "") or ""),
    ).strip()
    if category:
        metadata["category"] = category[:80]
    if explanation:
        metadata["explanation"] = explanation[:4_096]
    return metadata


def _anthropic_container_metadata(value: Any) -> dict[str, str]:
    """Return the operationally useful, non-content container envelope."""

    if value is None:
        return {}
    container_id = str(_anthropic_field(value, "id", "") or "").strip()
    expires_at_value = _anthropic_field(value, "expires_at", None)
    if hasattr(expires_at_value, "isoformat"):
        try:
            expires_at = str(expires_at_value.isoformat())
        except Exception:
            expires_at = ""
    else:
        expires_at = str(expires_at_value or "").strip()
    metadata: dict[str, str] = {}
    if container_id:
        metadata["id"] = container_id[:256]
    if expires_at:
        metadata["expires_at"] = expires_at[:80]
    return metadata


def _anthropic_public_citation(value: Any) -> dict[str, Any] | None:
    """Normalize Anthropic citations without retaining cited source text.

    Web citations remain linkable. Document/page/block citations intentionally
    use an opaque, stable source key: the provider's ``cited_text`` and raw
    file id must not become a second content-delivery path into the renderer.
    """

    citation_type = str(_anthropic_field(value, "type", "") or "").strip()
    if citation_type == "web_search_result_location":
        url = str(_anthropic_field(value, "url", "") or "").strip()
        title = str(_anthropic_field(value, "title", "") or "").strip()
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return None
        return {
            "url": url,
            "title": re.sub(r"\s+", " ", title).strip()[:512],
            "range": [0, 0],
        }

    def location_index(field_name: str) -> int:
        try:
            return max(0, int(_anthropic_field(value, field_name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    def bounded_text(field_name: str, maximum: int = 512) -> str:
        return re.sub(
            r"\s+",
            " ",
            str(_anthropic_field(value, field_name, "") or ""),
        ).strip()[:maximum]

    if citation_type == "search_result_location":
        raw_source = bounded_text("source", 2_048)
        title = bounded_text("title")
        start = location_index("start_block_index")
        end = max(start, location_index("end_block_index"))
        if re.match(r"^https?://", raw_source, flags=re.IGNORECASE):
            return {
                "url": raw_source,
                "title": title,
                "range": [start, end],
            }
        identity = json.dumps(
            [citation_type, raw_source, title, location_index("search_result_index")],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return {
            "source": f"anthropic:search-result:{digest}",
            "title": title or "Search result",
            "label": f"Blocks {start}–{end}",
            "range": [start, end],
            "location_type": citation_type,
        }

    location_fields = {
        "char_location": ("start_char_index", "end_char_index", "Characters"),
        "page_location": ("start_page_number", "end_page_number", "Pages"),
        "content_block_location": (
            "start_block_index",
            "end_block_index",
            "Blocks",
        ),
    }
    field_names = location_fields.get(citation_type)
    if field_names is None:
        return None
    start_field, end_field, range_label = field_names
    start = location_index(start_field)
    end = max(start, location_index(end_field))
    document_index = location_index("document_index")
    document_title = bounded_text("document_title")
    file_id = bounded_text("file_id", 512)
    identity = json.dumps(
        [citation_type, document_index, document_title, file_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return {
        "source": f"anthropic:document:{digest}",
        "title": document_title or f"Document {document_index + 1}",
        "label": f"{range_label} {start}–{end}",
        "range": [start, end],
        "location_type": citation_type,
    }


def _anthropic_public_citations(value: Any) -> list[dict[str, Any]]:
    values = value if isinstance(value, (list, tuple)) else [value]
    citations: list[dict[str, Any]] = []
    for item in values:
        citation = _anthropic_public_citation(item)
        if citation is not None and citation not in citations:
            citations.append(citation)
    return citations


_ANTHROPIC_RESULT_ACTIVITY_NAMES = {
    "web_search_tool_result": "web_search",
    "web_fetch_tool_result": "web_fetch",
    "code_execution_tool_result": "code_execution",
    "bash_code_execution_tool_result": "bash_code_execution",
    "text_editor_code_execution_tool_result": "text_editor_code_execution",
    "tool_search_tool_result": "tool_search",
    "mcp_tool_result": "mcp",
    "advisor_tool_result": "advisor",
}


def _anthropic_activity_label(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if normalized == "web_search":
        return "Web search"
    if normalized == "web_fetch":
        return "Web fetch"
    if normalized in {
        "code_execution",
        "bash_code_execution",
        "text_editor_code_execution",
    }:
        return "Code execution"
    if normalized.startswith("tool_search") or normalized == "tool_search":
        return "Tool search"
    if normalized == "mcp":
        return "MCP tool"
    if normalized == "advisor":
        return "Advisor"
    return str(name or "Provider tool").replace("_", " ").strip().title()


def _anthropic_input_character_count(value: Any) -> int:
    """Count normalized JSON characters without exposing provider input."""

    detached = _detached_anthropic_value(value)
    if detached in (None, {}, []):
        return 0
    try:
        serialized = json.dumps(
            detached,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return 0
    return len(serialized)


def _anthropic_result_error_code(value: Any, *, depth: int = 0) -> str:
    if depth > 6:
        return ""
    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        error_code = str(_anthropic_field(item, "error_code", "") or "").strip()
        if error_code:
            # Provider error identifiers are useful operational metadata, but
            # raw error text can contain query/input content. Keep only
            # identifier-safe characters and never project a message/body.
            return re.sub(r"[^A-Za-z0-9._:-]+", "_", error_code)[:128]
        for child_name in ("content", "error", "result"):
            child = _anthropic_field(item, child_name, None)
            if child is None or child is item:
                continue
            nested = _anthropic_result_error_code(child, depth=depth + 1)
            if nested:
                return nested
    return ""


def _anthropic_result_failed(value: Any, *, depth: int = 0) -> bool:
    """Fail closed for scalar, list, and nested provider-result envelopes."""

    if depth > 6:
        return False
    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        if bool(_anthropic_field(item, "is_error", False)):
            return True
        content_type = str(_anthropic_field(item, "type", "") or "").lower()
        if "error" in content_type or _anthropic_field(item, "error_code", None):
            return True
        for child_name in ("content", "error", "result"):
            child = _anthropic_field(item, child_name, None)
            if child is None or child is item:
                continue
            if _anthropic_result_failed(child, depth=depth + 1):
                return True
    return False


def _anthropic_provider_activity(
    block: Any,
    *,
    activity_id: str = "",
    terminal: bool = False,
) -> ProviderActivityEvent | None:
    block_type = str(_anthropic_field(block, "type", "") or "").strip()
    if block_type in {"server_tool_use", "mcp_tool_use"}:
        block_activity_id = str(_anthropic_field(block, "id", "") or "").strip()
        tool_name = str(_anthropic_field(block, "name", "") or "").strip()
        label = _anthropic_activity_label(tool_name)
        detail_parts: list[str] = []
        if block_type == "mcp_tool_use":
            server_name = str(
                _anthropic_field(block, "server_name", "") or ""
            ).strip()
            if server_name:
                detail_parts.append(f"Server: {server_name[:256]}")
            if tool_name:
                detail_parts.append(f"Tool: {tool_name[:256]}")
        input_characters = _anthropic_input_character_count(
            _anthropic_field(block, "input", None)
        )
        if input_characters:
            input_label = "Arguments" if block_type == "mcp_tool_use" else "Input"
            detail_parts.append(f"{input_label}: {input_characters} characters")
        running_messages = {
            "web_search": "Searching the web",
            "web_fetch": "Fetching a web page",
            "code_execution": "Running provider code",
            "bash_code_execution": "Running provider code",
            "text_editor_code_execution": "Editing files in the provider container",
        }
        message = running_messages.get(
            tool_name,
            f"Using {label.lower()}",
        )
        return ProviderActivityEvent(
            id=(
                block_activity_id
                or activity_id
                or f"anthropic:{block_type}:{tool_name}"
            ),
            kind=block_type,
            name=label,
            status="running",
            message=message,
            detail=" · ".join(detail_parts),
        )

    if block_type == "container_upload":
        file_id = str(_anthropic_field(block, "file_id", "") or "").strip()
        fingerprint = (
            hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:12]
            if file_id
            else ""
        )
        return ProviderActivityEvent(
            id=(
                activity_id
                or f"anthropic:container-upload:{fingerprint or 'unknown'}"
            ),
            kind=block_type,
            name="Container upload",
            status="completed",
            message="Container file uploaded",
            detail=f"File ID: {fingerprint}" if fingerprint else "",
            count=1,
        )

    if block_type == "compaction":
        stable_id = activity_id or "anthropic:provider-compaction"
        if not terminal:
            return ProviderActivityEvent(
                id=stable_id,
                kind=block_type,
                name="Provider compaction",
                status="running",
                message="Provider context compaction in progress",
            )
        content = _anthropic_field(block, "content", None)
        has_summary = isinstance(content, str) and bool(content.strip())
        return ProviderActivityEvent(
            id=stable_id,
            kind=block_type,
            name="Provider compaction",
            status="completed" if has_summary else "failed",
            message=(
                "Provider context compaction completed"
                if has_summary
                else "Provider context compaction produced no summary"
            ),
        )

    tool_name = _ANTHROPIC_RESULT_ACTIVITY_NAMES.get(block_type)
    if not tool_name:
        return None
    block_activity_id = str(
        _anthropic_field(block, "tool_use_id", "") or ""
    ).strip()
    content = _anthropic_field(block, "content", None)
    failed = bool(_anthropic_field(block, "is_error", False)) or (
        _anthropic_result_failed(content)
    )
    error_code = _anthropic_result_error_code(content)
    label = _anthropic_activity_label(tool_name)
    count: int | None = None
    if tool_name == "web_search":
        count = len(_anthropic_web_search_sources(block))
    message = (
        f"{label} failed"
        if failed
        else (
            f"{label} completed — {count} source{'s' if count != 1 else ''}"
            if count is not None
            else f"{label} completed"
        )
    )
    return ProviderActivityEvent(
        id=block_activity_id or activity_id or f"anthropic:{block_type}",
        kind=block_type,
        name=label,
        status="failed" if failed else "completed",
        message=message,
        detail=f"Error code: {error_code}" if error_code else "",
        count=count,
    )


def _anthropic_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _anthropic_usage_value_or_existing(
    usage_obj: Any,
    field_name: str,
    existing: int,
) -> int:
    if _anthropic_field(usage_obj, field_name, None) is None:
        return existing
    return _get_usage_field(usage_obj, field_name)


def _anthropic_usage_metadata(usage_obj: Any) -> dict[str, Any]:
    """Return strict, non-content Anthropic usage diagnostics."""

    if usage_obj is None:
        return {}
    metadata: dict[str, Any] = {}
    for field_name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_deleted_input_tokens",
    ):
        if _anthropic_field(usage_obj, field_name, None) is not None:
            metadata[field_name] = _get_usage_field(usage_obj, field_name)
    for field_name in ("service_tier", "inference_geo"):
        value = str(_anthropic_field(usage_obj, field_name, "") or "").strip()
        if value:
            metadata[field_name] = value[:80]
    for container_name, counter_names in (
        (
            "server_tool_use",
            ("web_search_requests", "web_fetch_requests"),
        ),
        (
            "cache_creation",
            ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
        ),
    ):
        container = _anthropic_field(usage_obj, container_name, None)
        if container is None:
            continue
        counters = {
            counter_name: _get_usage_field(container, counter_name)
            for counter_name in counter_names
            if _anthropic_field(container, counter_name, None) is not None
        }
        if counters:
            metadata[container_name] = counters
    cost_usd = _get_usage_cost_usd(usage_obj)
    if cost_usd > 0:
        metadata["cost_usd"] = cost_usd
    return metadata


def _anthropic_request_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    clean = sanitize_llm_request_metadata(metadata)
    source = (
        clean.get("conversation_id")
        or clean.get("minicode_session_id")
        or clean.get("session_id")
        or ""
    )
    if not source:
        return {}
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return {"user_id": f"minicode-{digest}"}


def _short_sha256(value: str, length: int = 12) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _json_fingerprint(value: Any, length: int = 12) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except TypeError:
        raw = repr(value)
    return _short_sha256(raw, length=length)


def _anthropic_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [
        str(tool.get("name") or "").strip()
        for tool in tools
        if str(tool.get("name") or "").strip()
    ]


def _anthropic_tool_schema_hashes(tools: list[dict[str, Any]]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for index, tool in enumerate(tools):
        name = str(tool.get("name") or f"tool_{index}").strip()
        hashes[name] = _json_fingerprint(tool)
    return hashes


def _anthropic_tool_schema_size_summary(tools: list[dict[str, Any]]) -> dict[str, Any]:
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
        name = str(tool.get("name") or f"tool_{index}").strip()
        largest.append({"name": name, "chars": chars})
    largest.sort(key=lambda item: (-int(item["chars"]), str(item["name"])))
    return {"tools_chars": total_chars, "largest_tools": largest[:5]}


def _anthropic_input_size_summary(messages: list[dict[str, Any]]) -> dict[str, Any]:
    total_chars = 0
    largest: list[dict[str, Any]] = []
    duplicate_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for index, message in enumerate(messages):
        try:
            raw = json.dumps(
                message,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            raw = repr(message)
        chars = len(raw)
        total_chars += chars
        content_hash = (
            _json_fingerprint(message.get("content"))
            if message.get("content") not in (None, "", [], {})
            else ""
        )
        role = str(message.get("role") or "message")[:80]
        largest.append(
            {
                "index": index,
                "type": "message",
                "role": role,
                "chars": chars,
                **({"content_hash": content_hash} if content_hash else {}),
            }
        )
        if content_hash:
            key = (role, content_hash)
            group = duplicate_groups.setdefault(
                key,
                {
                    "type": "message",
                    "role": role,
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
        key=lambda item: (-int(item.get("chars") or 0), str(item.get("role") or ""))
    )
    return {
        "input_chars": total_chars,
        "largest_input_items": largest[:5],
        "duplicate_input_content": duplicates[:5],
    }


def _strip_excess_anthropic_media(
    messages: list[dict[str, Any]],
    limit: int = 100,
) -> list[dict[str, Any]]:
    media_types = {"image", "document"}
    media_count = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in media_types:
                media_count += 1
            if block.get("type") == "tool_result":
                nested = block.get("content")
                if isinstance(nested, list):
                    media_count += sum(
                        1
                        for item in nested
                        if isinstance(item, dict)
                        and item.get("type") in media_types
                    )
    to_remove = media_count - max(0, int(limit))
    if to_remove <= 0:
        return messages

    # Dropping attachments changes what the model sees. Say so: an answer that
    # ignores an image the user attached is otherwise inexplicable.
    logger.warning(
        "Anthropic request carries %d media blocks over the %d-block limit; "
        "dropping the %d oldest image/document blocks from this request",
        to_remove,
        max(0, int(limit)),
        to_remove,
    )

    stripped_messages: list[dict[str, Any]] = []
    for message in messages:
        if to_remove <= 0 or not isinstance(message.get("content"), list):
            stripped_messages.append(message)
            continue
        next_content: list[Any] = []
        for raw_block in message["content"]:
            if not isinstance(raw_block, dict):
                next_content.append(raw_block)
                continue
            block = dict(raw_block)
            if block.get("type") == "tool_result" and isinstance(
                block.get("content"), list
            ):
                nested_content: list[Any] = []
                for nested in block["content"]:
                    if (
                        to_remove > 0
                        and isinstance(nested, dict)
                        and nested.get("type") in media_types
                    ):
                        to_remove -= 1
                        continue
                    nested_content.append(nested)
                block["content"] = nested_content
            if to_remove > 0 and block.get("type") in media_types:
                to_remove -= 1
                continue
            next_content.append(block)
        next_message = dict(message)
        next_message["content"] = next_content
        stripped_messages.append(next_message)
    return stripped_messages


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


def _safe_anthropic_request_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key in ("model", "max_tokens", "stream", "tool_choice", "thinking"):
        if key in kwargs:
            params[key] = kwargs[key]
    # Detect cache_control at any level (system blocks, tools, messages)
    cache_breakpoints = 0
    cache_edit_count = 0
    system = kwargs.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and "cache_control" in block:
                cache_breakpoints += 1
    tools_list = kwargs.get("tools")
    if isinstance(tools_list, list):
        for tool in tools_list:
            if isinstance(tool, dict) and "cache_control" in tool:
                cache_breakpoints += 1
    messages_list = kwargs.get("messages")
    if isinstance(messages_list, list):
        for msg in messages_list:
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "cache_control" in block:
                            cache_breakpoints += 1
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "cache_edits"
                        ):
                            edits = block.get("edits")
                            cache_edit_count += (
                                len(edits) if isinstance(edits, list) else 0
                            )
    params["cache_control_present"] = cache_breakpoints > 0 or "cache_control" in kwargs
    params["cache_breakpoints"] = cache_breakpoints
    params["cache_edit_count"] = cache_edit_count
    params["cache_editing_header_present"] = bool(
        cache_edit_count
        and isinstance(kwargs.get("extra_headers"), dict)
        and kwargs["extra_headers"].get("anthropic-beta")
    )
    params["metadata_present"] = "metadata" in kwargs
    params["system_blocks"] = (
        len(kwargs.get("system") or []) if isinstance(kwargs.get("system"), list) else 0
    )
    params["tools_len"] = (
        len(kwargs.get("tools") or []) if isinstance(kwargs.get("tools"), list) else 0
    )
    return params


def _anthropic_safe_request_summary(
    *,
    model: str,
    system_text: str,
    stable_system_text: str | None,
    api_messages: list[dict[str, Any]],
    anthropic_tools: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    clean_metadata = sanitize_llm_request_metadata(metadata)
    stable_system = (
        stable_system_text
        if stable_system_text is not None
        else (split_sys_prompt_prefix(system_text).stable_prefix if system_text else "")
    )
    input_counts: dict[str, int] = {}
    for message in api_messages:
        role = str(message.get("role") or "message")
        input_counts[role] = input_counts.get(role, 0) + 1
    return {
        "model": model,
        "wire_api": "anthropic_messages",
        "metadata_keys": sorted(clean_metadata.keys()),
        "prompt_cache_key_present": False,
        "prompt_cache_key_hash": "",
        "request_params": _safe_anthropic_request_params(kwargs),
        "turn_aborted_marker_present": _contains_turn_aborted_marker(api_messages),
        "instructions_len": len(system_text),
        "instructions_hash": _short_sha256(stable_system or system_text),
        "instructions_full_hash": _short_sha256(system_text),
        "tools_len": len(anthropic_tools),
        "tools_hash": _json_fingerprint(anthropic_tools) if anthropic_tools else "",
        "tool_names": _anthropic_tool_names(anthropic_tools),
        "tool_schema_hashes": _anthropic_tool_schema_hashes(anthropic_tools),
        **_anthropic_tool_schema_size_summary(anthropic_tools),
        "input_items_len": len(api_messages),
        **_anthropic_input_size_summary(api_messages),
        "input_item_counts": input_counts,
    }


def _anthropic_system_text_from_payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _anthropic_stable_system_text_from_payload(value: Any) -> str:
    """Return the first cache segment from an assembled Anthropic payload."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    for block in value:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return str(block.get("text") or "")
    return ""


def _anthropic_safe_request_summary_from_payload(
    payload: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    messages = payload.get("messages")
    tools = payload.get("tools")
    api_messages = (
        [dict(item) for item in messages if isinstance(item, dict)]
        if isinstance(messages, list)
        else []
    )
    anthropic_tools = (
        [dict(item) for item in tools if isinstance(item, dict)]
        if isinstance(tools, list)
        else []
    )
    return _anthropic_safe_request_summary(
        model=str(payload.get("model") or ""),
        system_text=_anthropic_system_text_from_payload(payload.get("system")),
        stable_system_text=_anthropic_stable_system_text_from_payload(
            payload.get("system")
        ),
        api_messages=api_messages,
        anthropic_tools=anthropic_tools,
        metadata=metadata,
        kwargs=payload,
    )



