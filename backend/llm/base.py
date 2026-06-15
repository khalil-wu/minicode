"""
LLM 适配层抽象基类（DESIGN.md §一 架构图 LLM Adapter）。

定义了：
  - StreamEvent: 流式事件类型（text_chunk / tool_call / done / error）
  - LLMMessage: 统一消息格式
  - LLMAdapter: 抽象基类
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator


class StreamEventType(Enum):
    """流式事件类型。"""

    TEXT_CHUNK = "text_chunk"  # 文本片段
    THINKING_CHUNK = "thinking_delta"
    IMAGE_CHUNK = "image_chunk"  # 图片内容块（base64）
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

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


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
    usage: UsageInfo = field(default_factory=UsageInfo)
    image_data: str = ""
    image_media_type: str = ""
    # Provider-normalized terminal reason on DONE events, e.g. "stop",
    # "tool_calls", "length", "max_tokens", or "max_output_tokens".
    finish_reason: str = ""


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
    ) -> AsyncIterator[StreamEvent]:
        """
        流式调用 LLM。

        Args:
            messages: 对话消息列表
            tools: 工具 JSON Schema 列表（OpenAI function-calling 格式）

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


def _safe_json_dumps(obj: Any) -> str:
    """安全的 JSON 序列化。"""
    import json

    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
