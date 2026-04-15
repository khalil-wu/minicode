"""
OpenAI 适配器（DESIGN.md §一 LLM Adapter）。

支持两种 wire API：
  - "responses": OpenAI Responses API（client.responses.create）
  - "chat":      OpenAI Chat Completions API（client.chat.completions.create）

根据 config.wire_api 自动选择。
兼容 OpenAI 及所有兼容 API（Lucen、vLLM、LiteLLM、OpenRouter 等）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from backend.config import LLMSettings
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    StreamEvent,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
)

logger = logging.getLogger(__name__)


def _clean_error_message(exc: Exception) -> str:
    """清洗错误消息，移除 HTML 标签（如 Cloudflare 502 错误页）。"""
    msg = str(exc)
    # 移除 HTML 标签
    msg = re.sub(r'<[^>]+>', ' ', msg)
    # 压缩多余空白
    msg = re.sub(r'\s+', ' ', msg).strip()
    # 截断过长错误
    if len(msg) > 200:
        msg = msg[:200] + '...'
    return msg


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
        self._client = client or AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        流式调用 LLM。根据 wire_api 路由到对应 API。
        """
        if self._settings.wire_api == "responses":
            async for event in self._stream_responses_api(messages, tools):
                yield event
        else:
            async for event in self._stream_chat_completions(messages, tools):
                yield event

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        """非流式调用，用于摘要、压缩等内部任务。"""
        if self._settings.wire_api == "responses":
            return await self._simple_responses_api(messages)
        else:
            return await self._simple_chat_completions(messages)

    # ══════════════════════════════════════════════════════════════
    #  Responses API 实现（wire_api="responses"）
    # ══════════════════════════════════════════════════════════════

    async def _stream_responses_api(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        使用 Responses API 流式调用。

        Responses API 格式：
          input: list[dict] — 消息列表
          tools: list[dict] — 工具定义
          stream: bool — 流式
        """
        # 构建 input（Responses API 消息格式）
        api_input = self._build_responses_input(messages)

        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "input": api_input,
            "stream": True,
        }

        # 添加 reasoning（如果模型支持）
        if self._settings.reasoning_effort:
            kwargs["reasoning"] = {"effort": self._settings.reasoning_effort}

        # 添加工具定义
        if tools:
            responses_tools = self._convert_tools_to_responses_format(tools)
            kwargs["tools"] = responses_tools

        try:
            stream = await self._client.responses.create(**kwargs)
        except Exception as exc:
            logger.error("Responses API 调用失败: %s", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=f"LLM API 调用失败: {_clean_error_message(exc)}",
            )
            return

        # 解析 Responses API 流式事件
        full_text = ""
        pending_tool_calls: list[ToolCallEvent] = []
        usage = UsageInfo()

        try:
            async for event in stream:
                event_type = getattr(event, "type", "")

                # 文本内容增量
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        full_text += delta
                        yield StreamEvent(
                            type=StreamEventType.TEXT_CHUNK,
                            content=delta,
                        )

                # 函数调用
                elif event_type == "response.function_call_arguments.done":
                    call_id = getattr(event, "call_id", "") or getattr(event, "item_id", "")
                    name = getattr(event, "name", "")
                    arguments_str = getattr(event, "arguments", "{}")

                    try:
                        arguments = json.loads(arguments_str)
                    except (json.JSONDecodeError, TypeError):
                        arguments = {"_raw": arguments_str}

                    pending_tool_calls.append(
                        ToolCallEvent(
                            id=call_id,
                            name=name,
                            arguments=arguments,
                        )
                    )

                # 完成
                elif event_type == "response.completed":
                    response_obj = getattr(event, "response", None)
                    if response_obj:
                        usage_obj = getattr(response_obj, "usage", None)
                        if usage_obj:
                            usage = UsageInfo(
                                input_tokens=getattr(usage_obj, "input_tokens", 0),
                                output_tokens=getattr(usage_obj, "output_tokens", 0),
                            )
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

        yield StreamEvent(type=StreamEventType.DONE, usage=usage)

    async def _simple_responses_api(self, messages: list[LLMMessage]) -> str:
        """Responses API 非流式调用。"""
        api_input = self._build_responses_input(messages)

        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "input": api_input,
        }

        if self._settings.reasoning_effort:
            kwargs["reasoning"] = {"effort": self._settings.reasoning_effort}

        try:
            response = await self._client.responses.create(**kwargs)
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
            if msg.role == "system":
                result.append({
                    "role": "system",
                    "content": msg.content,
                })
            elif msg.role == "user":
                result.append({
                    "role": "user",
                    "content": msg.content,
                })
            elif msg.role == "assistant":
                if msg.tool_calls:
                    # 助手的工具调用：转换为 function_call output items
                    for tc in msg.tool_calls:
                        result.append({
                            "type": "function_call",
                            "id": tc.id,
                            "call_id": tc.id,
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        })
                elif msg.content:
                    result.append({
                        "role": "assistant",
                        "content": msg.content,
                    })
            elif msg.role == "tool":
                result.append({
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id or "",
                    "output": msg.content,
                })

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
                "parameters": func.get("parameters", {}),
                "strict": func.get("strict", False),
            })
        return result

    # ══════════════════════════════════════════════════════════════
    #  Chat Completions API 实现（wire_api="chat"）
    # ══════════════════════════════════════════════════════════════

    async def _stream_chat_completions(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """使用 Chat Completions API 流式调用。"""
        openai_messages = [msg.to_openai_message() for msg in messages]

        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": openai_messages,
            "stream": True,
            "max_tokens": self._settings.max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            logger.error("Chat Completions API 调用失败: %s", exc)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=f"LLM API 调用失败: {_clean_error_message(exc)}",
            )
            return

        full_text = ""
        pending_tool_calls: dict[int, dict[str, Any]] = {}
        usage = UsageInfo()

        async for chunk in stream:
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = UsageInfo(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    )
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if delta and delta.content:
                full_text += delta.content
                yield StreamEvent(
                    type=StreamEventType.TEXT_CHUNK,
                    content=delta.content,
                )

            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in pending_tool_calls:
                        pending_tool_calls[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    if tc.id:
                        pending_tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            pending_tool_calls[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            pending_tool_calls[idx]["arguments"] += tc.function.arguments

            if choice.finish_reason:
                break

        if pending_tool_calls:
            tool_call_events = []
            for idx in sorted(pending_tool_calls.keys()):
                tc_data = pending_tool_calls[idx]
                try:
                    arguments = json.loads(tc_data["arguments"])
                except (json.JSONDecodeError, TypeError):
                    arguments = {"_raw": tc_data["arguments"]}

                tool_call_events.append(
                    ToolCallEvent(
                        id=tc_data["id"],
                        name=tc_data["name"],
                        arguments=arguments,
                    )
                )

            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=tool_call_events,
            )

        yield StreamEvent(type=StreamEventType.DONE, usage=usage)

    async def _simple_chat_completions(self, messages: list[LLMMessage]) -> str:
        """Chat Completions API 非流式调用。"""
        openai_messages = [msg.to_openai_message() for msg in messages]

        try:
            response = await self._client.chat.completions.create(
                model=self._settings.model,
                messages=openai_messages,
                max_tokens=self._settings.max_tokens,
            )
        except Exception as exc:
            logger.error("Chat Completions simple_chat 失败: %s", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()

        raise RuntimeError("LLM 返回空内容")
