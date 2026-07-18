"""
LLM 适配层抽象基类（DESIGN.md §一 架构图 LLM Adapter）。

定义了：
  - StreamEvent: 流式事件类型（text_chunk / tool_call / done / error）
  - LLMMessage: 统一消息格式
  - LLMAdapter: 抽象基类
"""

from __future__ import annotations

import inspect
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from contextvars import ContextVar, Token
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


_REQUEST_METADATA_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SENSITIVE_REQUEST_METADATA_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_REQUEST_METADATA_MAX_PAIRS = 16
_REQUEST_METADATA_MAX_VALUE_CHARS = 512


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
    request_metadata = sanitize_llm_request_metadata(metadata)
    stream_chat = getattr(adapter, "stream_chat")
    if request_metadata and _stream_chat_accepts_metadata(stream_chat):
        return stream_chat(messages, tools=tools, metadata=request_metadata)
    return stream_chat(messages, tools=tools)


class StreamEventType(Enum):
    """流式事件类型。"""

    TEXT_CHUNK = "text_chunk"  # 文本片段
    THINKING_CHUNK = "thinking_delta"
    IMAGE_CHUNK = "image_chunk"  # 图片内容块（base64）
    TOOL_CALL_START = "tool_call_start"  # 工具块开始（id+name 已知，args 未完成）
    TOOL_CALL_DELTA = "tool_call_delta"  # 工具参数 JSON 片段
    TOOL_CALL = "tool_call"  # 工具调用请求（完整参数）
    FALLBACK_RESTART = "fallback_restart"  # 丢弃当前文本草稿并由备用 provider 重开本次响应
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
class ToolCallEvent:
    """工具调用事件数据（完整参数）。"""

    id: str  # 工具调用 ID（用于结果关联）
    name: str  # 工具名称
    arguments: dict[str, Any]  # 工具参数


@dataclass
class UsageInfo:
    """Token 用量信息。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Diagnostic subset reported by reasoning models. Providers normally count
    # this inside output_tokens, so total_tokens intentionally does not add it.
    reasoning_output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        # Cache fields are provider-specific diagnostics and are often a
        # subset of input_tokens (OpenAI) rather than additional tokens.
        return self.input_tokens + self.output_tokens


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

    # Responses API assistant message phase, e.g. commentary/final_answer.
    # Other providers ignore this field.
    phase: str = ""

    # Provider-native Responses output items to round-trip on the next request.
    # This is used for opaque encrypted reasoning/function_call items when
    # previous_response_id is unavailable; it is intentionally not rendered in UI.
    provider_items: list[dict[str, Any]] = field(default_factory=list)

    # user 角色的图片附件（多模态输入）
    # 每项: {"media_type": "image/png", "data": "<base64>"}
    images: list[dict[str, str]] = field(default_factory=list)

    # user role document attachments for provider-native multimodal inputs.
    # Each item: {"media_type": "application/pdf", "data": "<base64>", "file_name": "paper.pdf"}
    documents: list[dict[str, str]] = field(default_factory=list)

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
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                })
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

        if self.role == "tool":
            msg["tool_call_id"] = self.tool_call_id or ""
            if self.name:
                msg["name"] = self.name

        return msg


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
    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        """
        非流式简单调用，用于摘要、压缩等内部任务。

        Returns:
            完整的回复文本
        """

    # Optional per-turn bucket so simple_chat side calls (reflection /
    # last-resort / compaction) also land in the current turn's usage totals,
    # not only the global CostTracker. ContextVar keeps subagent tasks isolated.
    _turn_usage_bucket: ContextVar["UsageInfo | None"] = ContextVar(
        "llm_turn_usage_bucket",
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

    @staticmethod
    def record_non_stream_usage(usage_obj: Any, *, provider: str, model_id: str | None) -> None:
        """Record token usage from a non-streaming response to the global
        CostTracker. Adapters should call this in ``simple_chat`` so that side
        calls (reflection, last-resort recovery, context compaction) are counted
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
                _get_chat_prompt_tokens,
                _get_reasoning_output_tokens,
            )

            input_tokens = _first_usage_field(usage_obj, "input_tokens", "prompt_tokens")
            if not input_tokens:
                input_tokens = _get_chat_prompt_tokens(usage_obj)
            output_tokens = _first_usage_field(usage_obj, "output_tokens", "completion_tokens")
            cache_creation = _first_usage_field(
                usage_obj,
                "cache_creation_input_tokens",
                "cache_creation_tokens",
            )
            cache_read = _get_cached_prompt_tokens(usage_obj)
            reasoning_output_tokens = _get_reasoning_output_tokens(usage_obj)
            if not (
                input_tokens
                or output_tokens
                or cache_creation
                or cache_read
                or reasoning_output_tokens
            ):
                return
            bucket = LLMAdapter._turn_usage_bucket.get()
            if bucket is not None:
                bucket.input_tokens += input_tokens
                bucket.output_tokens += output_tokens
                bucket.cache_creation_input_tokens += cache_creation
                bucket.cache_read_input_tokens += cache_read
                bucket.reasoning_output_tokens += reasoning_output_tokens
            # The per-turn bucket feeds UI/turn totals; the global tracker owns
            # process-wide cost accounting. Side calls must update both rather
            # than choosing one or the other.
            from backend.llm.cost_tracker import CostTracker

            CostTracker.get_instance().record_usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
                reasoning_output_tokens=reasoning_output_tokens,
                model_id=model_id,
                provider=provider,
            )
        except Exception:  # noqa: BLE001 — accounting must never break the call
            logger.debug("Failed to record non-stream usage", exc_info=True)


def _safe_json_dumps(obj: Any) -> str:
    """安全的 JSON 序列化。"""
    import json

    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
