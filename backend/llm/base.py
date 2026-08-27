"""
LLM 适配层抽象基类（DESIGN.md §一 架构图 LLM Adapter）。

定义了：
  - StreamEvent: 流式事件类型（text_chunk / tool_call / done / error）
  - LLMMessage: 统一消息格式
  - LLMAdapter: 抽象基类
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, cast
from uuid import uuid4

from backend.agent.lifecycle_errors import LifecycleStaleError
from backend.agent.lifecycle_observer import LIFECYCLE_RUNTIME_METADATA_KEY
from backend.agent.provider_lifecycle import ProviderLifecycleRuntime
from backend.llm.errors import classify_llm_error, llm_error_status_code, retry_after_seconds
from backend.llm.provider_contracts import ReasoningPolicy

logger = logging.getLogger(__name__)


_REQUEST_METADATA_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SENSITIVE_REQUEST_METADATA_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_REQUEST_METADATA_MAX_PAIRS = 16
_REQUEST_METADATA_MAX_VALUE_CHARS = 512
_LOCAL_REQUEST_METADATA_KEYS = {
    "prompt_cache_skip_write",
    # Prompt-cache fork fields are fixed, hash-only diagnostics produced by
    # the loop. They must survive the general 16-pair trace budget even when
    # a child request also carries the full canonical routing projection.
    "prompt_cache_fork_status",
    "prompt_cache_fork_stable_prefix",
    "prompt_cache_parent_stable_hash",
    "prompt_cache_child_stable_hash",
    "prompt_cache_parent_tools_hash",
    "prompt_cache_child_tools_hash",
    LIFECYCLE_RUNTIME_METADATA_KEY,
}
# Side-query retry: the delay curve (0.5s exponential base, 8s cap, jitter
# downward) comes from pi-ai's transport-layer getRetryDelayMs; the 3-attempt
# ceiling matches pi's settings.retry.maxRetries default. pi's own auxiliary
# calls actually use a 2s base with no cap, so this fuse is deliberately
# tighter than upstream.
_SIDE_QUERY_MAX_RETRIES = 3
_SIDE_QUERY_OPERATION_MAX_RETRIES = {
    # Compaction owns its complete retry budget in this layer.  Keeping it
    # smaller than the foreground stream budget prevents an outer compaction
    # loop from multiplying provider calls during an outage.
    "compact": 2,
}
_SIDE_QUERY_BASE_DELAY_SECONDS = 0.5
_SIDE_QUERY_MAX_DELAY_SECONDS = 8.0
_SIDE_QUERY_SERVER_DELAY_LIMIT_SECONDS = 60.0
# Auxiliary calls are not streamed, so the agent loop's per-event watchdog
# (provider_stream_wait) never sees them, and the raw HTTP clients run with
# timeout=None because stream liveness is owned by that watchdog. Without a
# bound here a half-open connection during compaction wedges the turn with no
# event and no recovery. Matches the default stream idle budget.
_SIDE_QUERY_ATTEMPT_TIMEOUT_SECONDS = 300.0


def resolve_provider_lifecycle_runtime(
    metadata: dict[str, Any] | None,
) -> ProviderLifecycleRuntime | None:
    # The turn-scoped binding is authoritative. Request metadata may have
    # crossed an adapter boundary or come from an older host, so it must never
    # override the live MiniCode lifecycle generation.
    bound_runtime = LLMAdapter.current_provider_lifecycle_runtime()
    if bound_runtime is not None:
        return cast(ProviderLifecycleRuntime, bound_runtime)
    if isinstance(metadata, dict):
        runtime = metadata.get(LIFECYCLE_RUNTIME_METADATA_KEY)
        if runtime is not None:
            return cast(ProviderLifecycleRuntime, runtime)
    return None


async def emit_provider_lifecycle_request(
    metadata: dict[str, Any] | None,
    payload: Any,
) -> Any:
    """Apply the session lifecycle request hook at the real wire boundary.

    The payload is provider-specific (after messages, tool schemas, and
    adapter options have been assembled).  Keeping this helper next to the
    header/response hooks prevents the orchestration loop from firing a
    second, generic pre-request event that extensions could mistake for the
    actual HTTP/SDK request.
    """

    runner = resolve_provider_lifecycle_runtime(metadata)
    emit_request = getattr(runner, "emit_before_provider_request", None)
    if not callable(emit_request):
        return payload
    try:
        value = await emit_request(payload)
    except LifecycleStaleError:
        # A stale runtime is a host lifecycle violation, not an ordinary
        # extension-handler failure.  Pi prevents old contexts from issuing
        # any post-reload side effect; silently sending the unhooked request
        # would let a captured generation continue on the wire.
        raise
    except Exception:
        logger.debug("Provider lifecycle request hook failed", exc_info=True)
        return payload
    if value is None:
        return payload
    if isinstance(payload, dict):
        if not isinstance(value, Mapping):
            raise TypeError(
                "before_provider_request must return a mapping for this provider request"
            )
        return dict(value)
    return value


async def emit_provider_lifecycle_headers(
    metadata: dict[str, Any] | None,
    headers: dict[str, Any],
    *,
    omit_value: Any = None,
) -> dict[str, Any]:
    """Apply the session lifecycle header hook at the real wire boundary."""

    runner = resolve_provider_lifecycle_runtime(metadata)
    emit_headers = getattr(runner, "emit_before_provider_headers", None)
    if not callable(emit_headers):
        return headers
    working_headers = dict(headers)
    try:
        # Pi's runner contract is in-place: handlers mutate the supplied
        # object and their return value is intentionally ignored.
        await emit_headers(working_headers)
    except LifecycleStaleError:
        raise
    except Exception:
        logger.debug("Provider lifecycle header hook failed", exc_info=True)
        return headers
    # Pi treats null header values as deletion markers.  Raw HTTP transports
    # implement deletion by dropping the key; the official SDKs expose an
    # ``omit`` sentinel so their default header merge cannot resurrect it.
    deleted = {
        str(key).strip().casefold()
        for key, value in working_headers.items()
        if value is None and str(key).strip()
    }
    resolved: dict[str, Any] = {}
    emitted_deletions: set[str] = set()
    for raw_key, value in working_headers.items():
        key = str(raw_key).strip()
        if not key:
            continue
        normalized = key.casefold()
        if normalized in deleted:
            if omit_value is not None and normalized not in emitted_deletions:
                resolved[key] = omit_value
                emitted_deletions.add(normalized)
            continue
        if value is None:
            continue
        resolved[key] = str(value)
    return resolved


async def emit_provider_lifecycle_response(
    metadata: dict[str, Any] | None,
    status: int,
    headers: Any = None,
) -> None:
    """Notify the session lifecycle after provider response headers arrive."""

    runner = resolve_provider_lifecycle_runtime(metadata)
    emit_response = getattr(runner, "emit_after_provider_response", None)
    if not callable(emit_response):
        return
    try:
        await emit_response(
            int(status),
            dict(headers or {}) if hasattr(headers, "items") else {},
        )
    except LifecycleStaleError:
        raise
    except Exception:
        logger.debug("Provider lifecycle response hook failed", exc_info=True)

def sanitize_llm_request_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Return provider-safe request metadata.

    Provider request metadata is useful for trace correlation, but it must stay
    small and non-secret. Keep only short scalar values with conservative keys.
    """
    if not isinstance(metadata, dict):
        return {}

    clean: dict[str, str] = {}
    for raw_key, raw_value in metadata.items():
        if len(clean) >= _REQUEST_METADATA_MAX_PAIRS:
            break
        key = str(raw_key or "").strip()
        if not key or not _REQUEST_METADATA_KEY_RE.fullmatch(key):
            continue
        if key in _LOCAL_REQUEST_METADATA_KEYS:
            continue
        if _SENSITIVE_REQUEST_METADATA_KEY_RE.search(key):
            continue
        if raw_value is None or isinstance(raw_value, (dict, list, tuple, set)):
            continue
        if isinstance(raw_value, bool):
            value = "true" if raw_value else "false"
        elif isinstance(raw_value, (str, int, float)):
            value = str(raw_value)
        else:
            continue
        value = value.strip()
        if not value:
            continue
        clean[key] = value[:_REQUEST_METADATA_MAX_VALUE_CHARS]
    return clean


def _stream_chat_accepts_metadata(stream_chat: Any) -> bool:
    try:
        signature = inspect.signature(stream_chat)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return "metadata" in signature.parameters


def stream_chat_with_request_metadata(
    adapter: Any,
    messages: list["LLMMessage"],
    tools: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator["StreamEvent"]:
    """Call stream_chat with request metadata only when the adapter supports it."""
    request_metadata: dict[str, Any] = sanitize_llm_request_metadata(metadata)
    if isinstance(metadata, dict):
        for key in _LOCAL_REQUEST_METADATA_KEYS:
            if key in metadata:
                request_metadata[key] = metadata[key]
    stream_chat = getattr(adapter, "stream_chat")
    if request_metadata and _stream_chat_accepts_metadata(stream_chat):
        return stream_chat(messages, tools=tools, metadata=request_metadata)
    return stream_chat(messages, tools=tools)


async def safe_stream_chat_with_request_metadata(
    adapter: Any,
    messages: list["LLMMessage"],
    tools: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator["StreamEvent"]:
    """Normalize adapter exceptions into the loop's normal ERROR event.

    Adapters normally translate transport failures themselves, but third-party
    adapters and lightweight test providers can raise while opening or
    iterating a stream. Letting those exceptions escape the loop bypasses the
    bounded stream-retry ladder. This wrapper preserves cancellation and turns
    every other exception into one classified ERROR event, keeping retry,
    telemetry, and terminalization in a single path.
    """

    stream: AsyncIterator[StreamEvent] | None = None
    try:
        stream = stream_chat_with_request_metadata(adapter, messages, tools, metadata)
        async for event in stream:
            yield event
    except asyncio.CancelledError:
        raise
    except LifecycleStaleError:
        # A stale lifecycle capability is a MiniCode-internal generation
        # mismatch, not a provider failure. The adapters re-raise it on purpose;
        # normalizing it here would erase the distinction and let the stream
        # retry ladder treat an obsolete extension as a flaky model. The agent
        # runtime's own boundary (``fail_provider_runtime``) logs the traceback
        # and terminalizes the turn as a runtime error.
        raise
    except Exception as exc:  # noqa: BLE001 - normalize provider boundary failures
        # Provider exceptions can embed request headers, query parameters, or
        # gateway response bodies. Classify from the original exception, but
        # only log/emit the shared redacted diagnostic.
        from backend.llm.errors import (
            classify_llm_error,
            llm_error_raw,
            sanitize_llm_error_message,
        )

        classification = classify_llm_error(exc)
        safe_error = sanitize_llm_error_message(exc, classification)
        logger.warning(
            "Provider stream raised; converting to ERROR event: %s (%s)",
            safe_error,
            type(exc).__name__,
        )
        # The normalized ERROR event must carry the same diagnostics an adapter
        # emits: provider_stream_error_event classifies and schedules retries
        # from status_code / provider_error_code / provider_error_schema_type
        # and honours the server's Retry-After.
        raw = llm_error_raw(exc, str(getattr(adapter, "_provider_id", "") or "unknown"))
        yield StreamEvent(
            type=StreamEventType.ERROR,
            content=safe_error,
            raw=raw,
        )
    finally:
        close = getattr(stream, "aclose", None) if stream is not None else None
        if callable(close):
            try:
                await close()
            except Exception:
                logger.debug("Provider stream cleanup failed", exc_info=True)


class StreamEventType(Enum):
    """流式事件类型。"""

    TEXT_CHUNK = "text_chunk"  # 文本片段
    THINKING_CHUNK = "thinking_delta"
    IMAGE_CHUNK = "image_chunk"  # 图片内容块（base64）
    PROVIDER_ACTIVITY = "provider_activity"  # Provider 托管工具的有意义进度
    TOOL_CALL_START = "tool_call_start"  # 工具块开始（id+name 已知，args 未完成）
    TOOL_CALL_DELTA = "tool_call_delta"  # 工具参数 JSON 片段
    TOOL_CALL = "tool_call"  # 工具调用请求（完整参数）
    DONE = "done"  # 生成完毕
    ERROR = "error"  # 错误


@dataclass
class ToolCallStartEvent:
    """工具调用开始事件（参数尚未完成）。"""

    id: str
    name: str
    index: int = 0


@dataclass
class ToolCallDeltaEvent:
    """工具调用参数增量事件。"""

    id: str
    partial_arguments: str


@dataclass
class ProviderActivityEvent:
    """User-meaningful lifecycle for a provider-managed operation.

    These operations (hosted web/file search, code execution, image generation,
    MCP, and similar server tools) are executed by the provider rather than by
    MiniCode's local ToolExecutor.  They therefore must not be represented as a
    normal TOOL_CALL, which would execute them a second time.  A stable activity
    id lets the frontend replace a running row with its terminal state instead
    of appending low-information lifecycle noise.
    """

    id: str
    kind: str
    name: str
    status: str
    message: str
    detail: str = ""
    count: int | None = None


@dataclass
class ToolCallEvent:
    """工具调用事件数据（完整参数）。"""

    id: str  # 工具调用 ID（用于结果关联）
    name: str  # 工具名称
    arguments: dict[str, Any]  # 工具参数
    # A parser recovered malformed provider JSON.  It remains in history so the
    # provider's tool-call protocol stays balanced, but it must never execute.
    arguments_repaired: bool = False
    duplicate_id: bool = False


@dataclass
class UsageInfo:
    """Token 用量信息。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_deleted_input_tokens: int = 0
    # Diagnostic subset reported by reasoning models. Providers normally count
    # this inside output_tokens, so total_tokens intentionally does not add it.
    reasoning_output_tokens: int = 0
    # Whether input_tokens already includes cache_read_input_tokens. Each
    # adapter sets this from its wire contract; downstream accounting does not
    # infer it from provider or model names.
    input_includes_cache_read: bool = True
    # OpenAI reports cache writes as a classification inside input_tokens,
    # while Anthropic/Pi report them as an additional counter.
    input_includes_cache_write: bool = True
    # Provider-neutral normalized prompt accounting. Adapters populate these
    # per request so mixed-provider turn aggregation never has to combine
    # incompatible booleans.
    ordinary_input_tokens: int = 0
    prompt_cache_total_tokens: int = 0
    # Provider-reported request cost. MiniCode intentionally has no local
    # pricing table; zero means the provider did not report an authoritative cost.
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        # Cache fields are provider-specific diagnostics and are often a
        # subset of input_tokens (OpenAI) rather than additional tokens.
        return self.input_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """Tokens that actually cost something this turn.

        ``input_tokens`` counts the whole prompt on every request, and an agent
        turn resends its context each iteration, so summing it across a turn
        measures traffic rather than cost: a 50k context over 60 iterations
        reads as 3M tokens while the real context never left 50k. Cached prefix
        reads are the part being resent, so excluding them leaves new input plus
        generated output — the same shape as Codex's `r`non_cached_input``
        rollout budget.

        Only subtract cache reads when they are actually part of ``input_tokens``
        (OpenAI). For Anthropic, cache reads are reported separately, so
        subtracting them would under-count billable input.
        """
        ordinary = self.normalized_ordinary_input_tokens
        cache_write = (
            0
            if self.input_includes_cache_write
            else max(0, self.cache_creation_input_tokens)
        )
        return ordinary + cache_write + max(0, self.output_tokens)

    @property
    def normalized_ordinary_input_tokens(self) -> int:
        if self.ordinary_input_tokens > 0:
            return max(0, self.ordinary_input_tokens)
        if self.prompt_cache_total_tokens > 0:
            return max(
                0,
                self.prompt_cache_total_tokens
                - max(0, self.cache_read_input_tokens)
                - max(0, self.cache_creation_input_tokens),
            )
        ordinary = max(0, self.input_tokens)
        if self.input_includes_cache_read:
            ordinary -= min(max(0, self.cache_read_input_tokens), ordinary)
        if self.input_includes_cache_write:
            ordinary -= min(max(0, self.cache_creation_input_tokens), ordinary)
        return max(0, ordinary)

    @property
    def normalized_prompt_cache_total_tokens(self) -> int:
        if self.prompt_cache_total_tokens > 0:
            return max(0, self.prompt_cache_total_tokens)
        total = self.normalized_ordinary_input_tokens
        total += max(0, self.cache_read_input_tokens)
        total += max(0, self.cache_creation_input_tokens)
        return total


def _normalize_usage_int(value: Any) -> int:
    """Normalize an untrusted provider counter at the adapter boundary."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        return (
            int(value)
            if math.isfinite(value) and value >= 0 and value.is_integer()
            else 0
        )
    if isinstance(value, str):
        stripped = value.strip()
        return int(stripped) if stripped.isascii() and stripped.isdecimal() else 0
    return 0


def _normalize_usage_cost(value: Any) -> float:
    """Normalize an optional provider-reported cost without raising."""
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


@dataclass(frozen=True, slots=True)
class SideQueryOptions:
    """Execution policy for one auxiliary model call.

    Side queries are intentionally separate from the main agent stream.  The
    caller names the operation, adapters resolve their provider-specific small
    model, and optional reasoning/cache features are disabled explicitly.
    """

    operation: str
    # Pi gives each standalone auxiliary request its own session identity.
    # Keeping it on this immutable options object makes bounded retries reuse
    # the same request identity instead of generating a new one per attempt.
    session_id: str = field(default_factory=lambda: str(uuid4()))
    thread_id: str = ""
    turn_id: str = ""
    max_tokens: int | None = None
    use_small_fast_model: bool = False
    disable_reasoning: bool = False
    enable_prompt_cache: bool = True
    hosted_web_search: bool = False
    web_search_allowed_domains: tuple[str, ...] = ()
    web_search_blocked_domains: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    output_schema_name: str = "minicode_output_schema"
    # Per-attempt liveness bound. None uses the module default; 0 disables it.
    attempt_timeout_seconds: float | None = None
    # Number of retries owned by this auxiliary operation.  ``None`` keeps the
    # normal side-query default; callers such as compaction can request a
    # smaller, operation-specific budget without nesting another retry policy.
    max_retries: int | None = None
    query_source: str = ""


@dataclass
class StreamEvent:
    """
    LLM 流式返回的统一事件格式。

    根据 type 读取对应字段：
    - TEXT_CHUNK: content 为文本片段
    - IMAGE_CHUNK: image_data (base64), image_media_type
    - TOOL_CALL: tool_calls 列表
    - DONE: usage 用量信息
    - ERROR: content 为错误描述
    """

    type: StreamEventType
    content: str = ""
    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    tool_call_start: ToolCallStartEvent | None = None
    tool_call_delta: ToolCallDeltaEvent | None = None
    provider_activity: ProviderActivityEvent | None = None
    # True when this TOOL_CALL event contains the final, complete batch for the
    # assistant message. Adapters may emit earlier TOOL_CALL events with
    # tool_calls_final=False as individual tool_use blocks become complete.
    tool_calls_final: bool = True
    usage: UsageInfo = field(default_factory=UsageInfo)
    image_data: str = ""
    image_media_type: str = ""
    # Provider-normalized terminal reason on DONE events, e.g. "stop",
    # "tool_calls", "length", "max_tokens", or "max_output_tokens".
    finish_reason: str = ""
    # Provider-native metadata kept off the user-facing UI by default. Adapters
    # attach small raw fragments here so usage deltas, stop reasons, and future
    # provider-specific fields are not discarded by normalization.
    raw: dict[str, Any] = field(default_factory=dict)
    # Provider-declared text phase for TEXT_CHUNK events, e.g. "commentary" or
    # "final_answer" from the Responses API message item.
    phase: str = ""
    # Stable provider content identity. Codex carries item_id on every delta;
    # Pi carries contentIndex through start/delta/end.
    item_id: str = ""
    content_index: int | None = None
    content_kind: str = ""
    lifecycle: str = "delta"
    # Provider-native output items that must be round-tripped internally on the
    # next request, such as Responses encrypted reasoning. Kept out of raw UI
    # diagnostics so opaque provider state is not exposed in inspector payloads.
    provider_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LLMMessage:
    """
    统一消息格式。

    对应 OpenAI Chat Completions 的 messages 数组元素，
    同时支持 tool_call 和 tool_result。
    """

    role: str  # system / user / assistant / tool
    content: str = ""
    name: str | None = None  # 用于 tool 角色

    # assistant 角色的工具调用
    tool_calls: list[ToolCallEvent] | None = None

    # tool 角色的结果
    tool_call_id: str | None = None

    # Anthropic is_error: True when this tool result is an error, so the adapter
    # marks the tool_result block with is_error (cc utils/messages.ts) instead of
    # the model inferring failure from the result text. Other providers ignore it.
    is_error: bool = False

    # Responses API assistant message phase, e.g. commentary/final_answer.
    # Other providers ignore this field.
    phase: str = ""

    # Provider-native Responses output items to round-trip on the next request.
    # This preserves opaque encrypted reasoning/function_call items across the
    # stateless Responses transcript replay and is never rendered in the UI.
    provider_items: list[dict[str, Any]] = field(default_factory=list)

    # user 角色的图片附件（多模态输入）
    # 每项: {"media_type": "image/png", "data": "<base64>"}
    images: list[dict[str, str]] = field(default_factory=list)

    # user role document attachments for provider-native multimodal inputs.
    # Each item: {"media_type": "application/pdf", "data": "<base64>", "file_name": "paper.pdf"}
    documents: list[dict[str, str]] = field(default_factory=list)

    # Durable, scoped references used to rehydrate media after a session resume.
    # Provider adapters ignore this field and only consume images/documents.
    attachment_refs: list[dict[str, Any]] = field(default_factory=list)

    # Trusted runtime reminder body attached by ContextBuilder. ``content`` is
    # still the provider projection; this field is the provenance signal used
    # when refreshing or removing injected context.
    runtime_context: str = ""

    # Pi's native transcript carries a message timestamp.  It is assigned once
    # when a message enters the durable MiniCode history and then replayed on
    # every provider request.  Re-generating it while rebuilding a request
    # changes the serialized transcript and defeats exact-prefix caching.
    # ``None`` keeps old callers/snapshots compatible; adapters use a stable
    # deterministic fallback for such legacy messages.
    timestamp_ms: int | None = None

    def to_openai_message(self) -> dict[str, Any]:
        """转换为 OpenAI Chat Completions API 格式。"""
        msg: dict[str, Any] = {"role": self.role}

        if self.role == "user" and self.images:
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            for img in self.images:
                media_type = img.get("media_type") or "image/png"
                data = img.get("data") or ""
                if not data:
                    continue
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{data}"},
                    }
                )
            if parts:
                msg["content"] = parts
        else:
            msg["content"] = self.content or ""

        if self.role == "assistant" and self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": _safe_json_dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
            # OpenAI 允许 tool_calls 存在时 content 为 null，但 DeepSeek 强要求必须为 string 否则报 400 Bad Request
            msg["content"] = self.content or ""

        # OpenAI-compatible reasoning gateways (DeepSeek, llama.cpp,
        # OpenCode-Go, etc.) require the provider's reasoning field to be
        # replayed on the assistant message that precedes a tool result.  Keep
        # the opaque value out of the normal UI content, but project the exact
        # field name captured by the adapter back onto the wire message.
        if self.role == "assistant" and self.provider_items:
            for item in self.provider_items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type not in {"chat_reasoning", "reasoning_content"}:
                    continue
                field = str(
                    item.get("field")
                    or item.get("reasoning_field")
                    or "reasoning_content"
                ).strip()
                if field not in {"reasoning_content", "reasoning", "reasoning_text"}:
                    continue
                value = item.get("content")
                if value is None:
                    value = item.get("text")
                if isinstance(value, str):
                    msg[field] = value

        if self.role == "tool":
            msg["tool_call_id"] = self.tool_call_id or ""
            if self.name:
                msg["name"] = self.name

        return msg


_PI_CONTEXT_SAFETY_TOKENS = 4_096
_PI_ESTIMATED_IMAGE_CHARS = 4_800


def estimate_llm_context_tokens(
    messages: list[LLMMessage],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate a provider context with Pi's four-characters-per-token rule."""

    total = 0
    for message in messages:
        chars = len(str(message.content or ""))
        chars += _PI_ESTIMATED_IMAGE_CHARS * (
            len(message.images) + len(message.documents)
        )
        if message.tool_calls:
            chars += sum(
                len(str(call.name or "")) + len(_safe_json_dumps(call.arguments))
                for call in message.tool_calls
            )
        if message.provider_items:
            chars += len(_safe_json_dumps(message.provider_items))
        total += math.ceil(chars / 4)
    if tools:
        total += math.ceil(len(_safe_json_dumps(tools)) / 4)
    return max(0, total)


def clamp_max_tokens_to_context(
    *,
    context_window: Any,
    messages: list[LLMMessage],
    tools: list[dict[str, Any]] | None,
    max_tokens: Any,
) -> int | float:
    """Apply Pi's request-time output clamp without rewriting model metadata."""

    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, (int, float))
        or not math.isfinite(float(max_tokens))
    ):
        raise ValueError("maxTokens must be a finite number")
    requested: int | float = (
        int(max_tokens) if float(max_tokens).is_integer() else float(max_tokens)
    )
    requested = max(1, requested)
    if (
        isinstance(context_window, bool)
        or not isinstance(context_window, (int, float))
        or not math.isfinite(float(context_window))
        or context_window <= 0
    ):
        return requested
    available = (
        context_window
        - estimate_llm_context_tokens(messages, tools)
        - _PI_CONTEXT_SAFETY_TOKENS
    )
    return min(requested, max(1, available))


class LLMAdapter(ABC):
    """
    LLM 适配器抽象基类。

    所有 LLM Provider（OpenAI / Claude / 本地模型）都实现此接口。
    核心方法：stream_chat()，返回 StreamEvent 异步迭代器。
    """

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        流式调用 LLM。

        Args:
            messages: 对话消息列表
            tools: 工具 JSON Schema 列表（OpenAI function-calling 格式）
            metadata: provider request metadata for trace correlation. Adapters
                may ignore it when their provider or gateway does not support it.

        Yields:
            StreamEvent 事件流

        事件顺序：
        1. 多个 TEXT_CHUNK（文本片段）
        2. 零或多个 TOOL_CALL（工具调用）
        3. 一个 DONE（结束）
        """
        yield StreamEvent(type=StreamEventType.DONE)  # type: ignore[misc]

    @abstractmethod
    async def simple_chat(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """
        非流式简单调用，用于摘要、压缩等内部任务。

        Returns:
            完整的回复文本。max_tokens 由有权威输出预算的辅助请求使用。
        """

    # Optional per-turn bucket so simple_chat side calls (compaction /
    # last-resort / compaction) also land in the current turn's usage totals,
    # not only the global CostTracker. ContextVar keeps subagent tasks isolated.
    _turn_usage_bucket: ContextVar["UsageInfo | None"] = ContextVar(
        "llm_turn_usage_bucket",
        default=None,
    )
    _side_query_options: ContextVar["SideQueryOptions | None"] = ContextVar(
        "llm_side_query_options",
        default=None,
    )
    _side_call_record: ContextVar["dict[str, Any] | None"] = ContextVar(
        "llm_side_call_record",
        default=None,
    )
    _side_call_records: ContextVar["list[dict[str, Any]] | None"] = ContextVar(
        "llm_side_call_records",
        default=None,
    )
    _provider_lifecycle_runtime: ContextVar[Any | None] = ContextVar(
        "llm_provider_lifecycle_runtime",
        default=None,
    )

    @classmethod
    def bind_turn_usage(cls, usage: "UsageInfo") -> Token:
        """Attach a mutable UsageInfo bucket for the current agent-loop turn."""
        return cls._turn_usage_bucket.set(usage)

    @classmethod
    def unbind_turn_usage(cls, token: Token) -> None:
        # ContextVar tokens must be reset in the same Context that set them.
        # Subagent event pumps historically advanced the child loop across
        # tasks; keep this defensive so a residual mismatch never hard-fails
        # an otherwise healthy turn.
        try:
            cls._turn_usage_bucket.reset(token)
        except ValueError:
            cls._turn_usage_bucket.set(None)

    @classmethod
    def bind_side_call_records(cls, records: list[dict[str, Any]]) -> Token:
        """Attach a turn-owned sink for structured auxiliary-call telemetry."""
        return cls._side_call_records.set(records)

    @classmethod
    def unbind_side_call_records(cls, token: Token) -> None:
        try:
            cls._side_call_records.reset(token)
        except ValueError:
            cls._side_call_records.set(None)

    @classmethod
    def current_side_query_options(cls) -> "SideQueryOptions | None":
        return cls._side_query_options.get()

    @classmethod
    def bind_provider_lifecycle_runtime(cls, runtime: Any | None) -> Token:
        """Bind the current MiniCode lifecycle runtime for provider calls."""

        return cls._provider_lifecycle_runtime.set(runtime)

    @classmethod
    def unbind_provider_lifecycle_runtime(cls, token: Token) -> None:
        try:
            cls._provider_lifecycle_runtime.reset(token)
        except ValueError:
            cls._provider_lifecycle_runtime.set(None)

    @classmethod
    def current_provider_lifecycle_runtime(cls) -> Any | None:
        return cls._provider_lifecycle_runtime.get()

    @classmethod
    def annotate_side_call(cls, *, provider: str, model_id: str) -> None:
        record = cls._side_call_record.get()
        if record is None:
            return
        record["provider"] = str(provider or "")
        record["model"] = str(model_id or "")

    def model_id(self) -> str:
        """Return the adapter's main model without requiring provider casts."""
        settings = getattr(self, "_settings", None)
        return str(
            getattr(settings, "model", "")
            or getattr(self, "_model", "")
            or ""
        ).strip()

    def supported_reasoning_efforts(self) -> tuple[str, ...]:
        """Return reasoning levels exposed by this concrete transport."""

        capabilities = getattr(self, "capabilities", None)
        return tuple(
            str(value).strip().lower()
            for value in (
                getattr(capabilities, "reasoning_effort_levels", ()) or ()
            )
            if str(value).strip()
        )

    def current_reasoning_effort(self) -> str:
        """Return the MiniCode canonical reasoning level for this adapter."""

        policy = getattr(self, "_reasoning_policy", None)
        if isinstance(policy, ReasoningPolicy):
            return policy.level
        capabilities = getattr(self, "capabilities", None)
        return str(
            getattr(capabilities, "effective_reasoning_effort", "") or ""
        ).strip().lower()

    def apply_reasoning_policy(self, policy: ReasoningPolicy) -> None:
        """Apply a canonical reasoning policy at the provider boundary."""

        self._reasoning_policy = policy

    def small_fast_model_id(self) -> str:
        """Return the explicitly configured small/fast model.

        MiniCode never substitutes a provider default or the primary model
        when a side call asks for a small model. Missing configuration is a
        capability error at the request boundary.
        """
        settings = getattr(self, "_settings", None)
        configured = str(
            getattr(settings, "small_fast_model", "")
            or getattr(self, "_small_fast_model", "")
            or ""
        ).strip()
        if configured:
            return configured
        raise RuntimeError(
            "Small/fast model selection requires an explicit configured model"
        )

    def reset_prompt_cache_editing(self, *, conversation_id: str = "") -> None:
        """Reset provider-native cache-edit state after a local prefix rewrite.

        Most providers have no native cache-editing state. Adapters that do
        (currently Anthropic/Claude cache editing) override this hook so a
        time-based local microcompact cannot replay stale cache references.
        """

        del conversation_id

    async def side_query(
        self,
        messages: list[LLMMessage],
        *,
        options: "SideQueryOptions",
    ) -> str:
        """Run one observable auxiliary call without mutating main-loop state."""
        operation = str(options.operation or "side_query").strip() or "side_query"
        record: dict[str, Any] = {
            "id": f"side:{operation}:{uuid4().hex[:12]}",
            "operation": operation,
            "provider": "",
            "model": "",
            "status": "running",
            "elapsed_ms": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "reasoning_output_tokens": 0,
                "cost_usd": 0.0,
            },
        }
        options_token = self._side_query_options.set(options)
        record_token = self._side_call_record.set(record)
        started = time.monotonic()
        attempt = 0
        max_retries = (
            max(0, int(options.max_retries))
            if options.max_retries is not None
            else int(_SIDE_QUERY_OPERATION_MAX_RETRIES.get(operation, _SIDE_QUERY_MAX_RETRIES))
        )
        query_source = str(options.query_source or "").strip().lower()
        if not query_source:
            query_source = (
                "compact"
                if operation == "compact"
                else "side_question"
                if operation == "context_side_query"
                else "background"
            )
        foreground_529_sources = {
            "user",
            "main",
            "foreground",
            "sdk",
            "agent:custom",
            "agent:default",
            "agent:builtin",
            "compact",
            "side_question",
        }
        consecutive_529 = 0
        try:
            simple_chat = self.simple_chat
            accepts_limit = False
            if options.max_tokens is not None:
                try:
                    parameters = inspect.signature(simple_chat).parameters
                    accepts_limit = "max_tokens" in parameters or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )
                except (TypeError, ValueError):
                    accepts_limit = False
            while True:
                record["attempts"] = attempt + 1
                try:
                    attempt_timeout = (
                        options.attempt_timeout_seconds
                        if options.attempt_timeout_seconds is not None
                        else _SIDE_QUERY_ATTEMPT_TIMEOUT_SECONDS
                    )
                    result = await asyncio.wait_for(
                        (
                            simple_chat(messages, max_tokens=options.max_tokens)
                            if accepts_limit
                            else simple_chat(messages)
                        ),
                        timeout=(attempt_timeout if attempt_timeout > 0 else None),
                    )
                    record["status"] = "completed"
                    record["retry_count"] = attempt
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    classification = classify_llm_error(exc)
                    status_code = llm_error_status_code(exc)
                    is_529 = status_code == 529 or (
                        classification.provider_error_type == "busy"
                        and "529" in str(exc).lower()
                    )
                    if is_529:
                        consecutive_529 += 1
                    else:
                        consecutive_529 = 0
                    background_capacity_error = is_529 and (
                        query_source not in foreground_529_sources
                        or consecutive_529 >= 3
                    )
                    if (
                        not classification.retryable
                        or background_capacity_error
                        or attempt >= max_retries
                    ):
                        raise
                    # pi fails fast when the server asks for more than the
                    # retry budget instead of sleeping the request away.
                    server_delay = retry_after_seconds(exc, maximum=float("inf"))
                    if server_delay > _SIDE_QUERY_SERVER_DELAY_LIMIT_SECONDS:
                        raise
                    delay_seconds = min(
                        _SIDE_QUERY_BASE_DELAY_SECONDS * (2 ** attempt),
                        _SIDE_QUERY_MAX_DELAY_SECONDS,
                    )
                    delay_seconds *= 1.0 - random.random() * 0.25
                    delay_seconds = max(delay_seconds, server_delay)
                    attempt += 1
                    logger.warning(
                        "Retrying auxiliary model call %s (%d/%d) in %.1fs after %s",
                        operation,
                        attempt,
                        max_retries,
                        delay_seconds,
                        classification.provider_error_type,
                    )
                    await asyncio.sleep(delay_seconds)
        except BaseException as exc:
            record["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            record["error_type"] = type(exc).__name__
            record["retry_count"] = attempt
            raise
        finally:
            record["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
            sink = self._side_call_records.get()
            if sink is not None:
                sink.append(record)
            self._side_call_record.reset(record_token)
            self._side_query_options.reset(options_token)

    @staticmethod
    def record_non_stream_usage(
        usage_obj: Any,
        *,
        provider: str,
        model_id: str | None,
        input_includes_cache_read: bool,
        input_includes_cache_write: bool = True,
    ) -> None:
        """Record token usage from a non-streaming response to the global
        CostTracker. Adapters should call this in ``simple_chat`` so that side
        calls (last-resort recovery and context compaction) are counted
        toward totals/budgets instead of only the main streaming DONE frames.
        Defensive: silently no-ops if usage is missing or shaped unexpectedly.
        """

        if usage_obj is None:
            return
        try:
            # Reuse the streaming parser so Responses/DeepSeek cache and
            # reasoning fields stay consistent between stream DONE and simple_chat.
            from backend.llm.openai_usage import (
                _first_usage_field,
                _get_cached_prompt_tokens,
                _get_cache_creation_prompt_tokens,
                _get_chat_prompt_tokens,
                _get_reasoning_output_tokens,
                _get_usage_cost_usd,
            )

            input_tokens = _first_usage_field(
                usage_obj, "input_tokens", "prompt_tokens"
            )
            if not input_tokens:
                input_tokens = _get_chat_prompt_tokens(usage_obj)
            output_tokens = _first_usage_field(
                usage_obj, "output_tokens", "completion_tokens"
            )
            cache_creation = _get_cache_creation_prompt_tokens(usage_obj)
            cache_read = _get_cached_prompt_tokens(usage_obj)
            reasoning_output_tokens = _get_reasoning_output_tokens(usage_obj)
            cost_usd = _get_usage_cost_usd(usage_obj)
            input_tokens = _normalize_usage_int(input_tokens)
            output_tokens = _normalize_usage_int(output_tokens)
            cache_creation = _normalize_usage_int(cache_creation)
            cache_read = _normalize_usage_int(cache_read)
            reasoning_output_tokens = _normalize_usage_int(reasoning_output_tokens)
            cost_usd = _normalize_usage_cost(cost_usd)
            ordinary_input = input_tokens
            if input_includes_cache_read:
                ordinary_input -= min(cache_read, ordinary_input)
            if input_includes_cache_write:
                ordinary_input -= min(cache_creation, ordinary_input)
            ordinary_input = max(0, ordinary_input)
            prompt_cache_total = ordinary_input + cache_read + cache_creation
            side_record = LLMAdapter._side_call_record.get()
            if side_record is not None:
                side_record["provider"] = str(provider or "")
                side_record["model"] = str(model_id or "")
                side_record["usage"] = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                    "reasoning_output_tokens": reasoning_output_tokens,
                    "cost_usd": cost_usd,
                    "input_includes_cache_read": bool(input_includes_cache_read),
                    "input_includes_cache_write": bool(
                        input_includes_cache_write
                    ),
                    "ordinary_input_tokens": ordinary_input,
                    "prompt_cache_total_tokens": prompt_cache_total,
                }
            if not (
                input_tokens
                or output_tokens
                or cache_creation
                or cache_read
                or reasoning_output_tokens
                or cost_usd
            ):
                return
            bucket = LLMAdapter._turn_usage_bucket.get()
            if bucket is not None:
                bucket.input_tokens += input_tokens
                bucket.output_tokens += output_tokens
                bucket.cache_creation_input_tokens += cache_creation
                bucket.cache_read_input_tokens += cache_read
                bucket.ordinary_input_tokens += ordinary_input
                bucket.prompt_cache_total_tokens += prompt_cache_total
                bucket.reasoning_output_tokens += reasoning_output_tokens
                bucket.cost_usd += cost_usd
                # A turn can contain Anthropic-style side calls alongside an
                # OpenAI-style main stream. If any provider reports cache reads
                # separately, preserve that fact for billable-token math rather
                # than subtracting those tokens from the combined input.
                bucket.input_includes_cache_read = (
                    bucket.input_includes_cache_read and bool(input_includes_cache_read)
                )
                bucket.input_includes_cache_write = (
                    bucket.input_includes_cache_write
                    and bool(input_includes_cache_write)
                )
            # The loop commits the complete turn bucket once. Standalone side
            # calls without a bound turn still write directly to the tracker.
            if bucket is not None:
                return
            from backend.llm.cost_tracker import CostTracker

            CostTracker.get_instance().record_usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
                ordinary_input_tokens=ordinary_input,
                prompt_cache_total_tokens=prompt_cache_total,
                reasoning_output_tokens=reasoning_output_tokens,
                model_id=model_id,
                provider=provider,
                input_includes_cache_read=input_includes_cache_read,
                input_includes_cache_write=input_includes_cache_write,
                cost_usd=cost_usd,
            )
        except Exception:  # noqa: BLE001 — accounting must never break the call
            # Accounting must not fail the call, but it must not fail silently
            # either: a dropped record understates session cost and leaves the
            # rollout/turn budget short of the tokens actually spent.
            logger.warning("Failed to record non-stream usage", exc_info=True)

    def supports_hosted_web_search(self) -> bool:
        """Whether this exact provider wire contract supports hosted search."""
        return False

    def hosted_web_search_supports_blocked_domains(self) -> bool:
        return False


def _safe_json_dumps(obj: Any) -> str:
    """安全的 JSON 序列化。"""
    import json

    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
