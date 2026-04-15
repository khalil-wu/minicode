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
    TOOL_CALL = "tool_call"  # 工具调用请求
    DONE = "done"  # 生成完毕
    ERROR = "error"  # 错误


@dataclass
class ToolCallEvent:
    """工具调用事件数据。"""

    id: str  # 工具调用 ID（用于结果关联）
    name: str  # 工具名称
    arguments: dict[str, Any]  # 工具参数


@dataclass
class UsageInfo:
    """Token 用量信息。"""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class StreamEvent:
    """
    LLM 流式返回的统一事件格式。

    根据 type 读取对应字段：
    - TEXT_CHUNK: content 为文本片段
    - TOOL_CALL: tool_calls 列表
    - DONE: usage 用量信息
    - ERROR: content 为错误描述
    """

    type: StreamEventType
    content: str = ""
    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    usage: UsageInfo = field(default_factory=UsageInfo)


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

    def to_openai_message(self) -> dict[str, Any]:
        """转换为 OpenAI Chat Completions API 格式。"""
        msg: dict[str, Any] = {"role": self.role}

        if self.content:
            msg["content"] = self.content

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
            # OpenAI 要求 tool_calls 存在时 content 可以为 null
            if not self.content:
                msg["content"] = None

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
