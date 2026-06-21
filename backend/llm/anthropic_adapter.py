"""
Anthropic Claude 适配器（DESIGN.md §一 LLM Adapter）。

增强特性：
  - 消息交替规则保证（user/assistant 严格交替）
  - message_delta 最终 usage（含 cache tokens）
  - 自动 retry（429 rate limit / 529 overloaded）
  - stop_reason 处理（end_turn / tool_use / max_tokens）
  - system prompt cache_control 支持
  - extended thinking 支持（Claude 4+）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator

import httpx

from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    StreamEvent,
    StreamEventType,
    ToolCallDeltaEvent,
    ToolCallEvent,
    ToolCallStartEvent,
    UsageInfo,
)

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0
_RETRYABLE_STATUS_CODES = {429, 529}
_MAX_PROMPT_CACHE_BREAKPOINTS = 4
_DELTA_DEBOUNCE_BYTES = 128
_THINKING_TRIGGER_RE = re.compile(
    r"(debug|fix|bug|error|traceback|refactor|architect|design|plan|"
    r"implement|analy[sz]e|reason|review|test|multi[- ]?step|"
    r"修复|报错|错误|调试|重构|架构|设计|计划|分析|实现|测试|审查)",
    re.IGNORECASE,
)


def _clean_error_message(exc: Exception) -> str:
    """清洗错误消息，移除 HTML 标签。"""
    msg = str(exc)
    msg = re.sub(r"<[^>]+>", " ", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    if len(msg) > 300:
        msg = msg[:300] + "..."
    return msg


def _is_retryable(exc: Exception) -> bool:
    """判断是否可重试（429/529）。"""
    status_code = getattr(exc, "status_code", None)
    if status_code and status_code in _RETRYABLE_STATUS_CODES:
        return True
    cls_name = exc.__class__.__name__
    return "RateLimitError" in cls_name or "OverloadedError" in cls_name


class AnthropicAdapter(LLMAdapter):
    """
    Anthropic Claude 适配器。

    使用示例：
        adapter = AnthropicAdapter(api_key="sk-ant-...", model="claude-sonnet-4-6")
        async for event in adapter.stream_chat(messages, tools):
            ...
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        base_url: str | None = None,
        max_tokens: int = 8192,
        thinking_budget: int | None = None,
        use_auth_token: bool = False,
        use_raw_http: bool = False,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._max_tokens = max_tokens
        self._thinking_budget = thinking_budget
        self._use_auth_token = use_auth_token
        self._use_raw_http = use_raw_http
        self._client = None

    def _get_client(self):
        """懒初始化 Anthropic 客户端。"""
        if self._client is not None:
            return self._client

        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise RuntimeError("需要安装 anthropic: pip install anthropic")

        kwargs: dict[str, Any] = {
            "auth_token" if self._use_auth_token else "api_key": self._api_key,
            "max_retries": 0,  # 我们自己管理 retry
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url

        self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def _call_with_retry(self, **kwargs: Any):
        """带指数退避的 retry 封装。"""
        client = self._get_client()
        last_exc = None

        for attempt in range(_MAX_RETRIES):
            try:
                return await client.messages.create(**kwargs)
            except Exception as exc:
                last_exc = exc
                if _is_retryable(exc) and attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    # 429 响应可能包含 retry-after
                    resp = getattr(exc, "response", None)
                    if resp and hasattr(resp, "headers"):
                        ra = resp.headers.get("retry-after")
                        if ra:
                            try:
                                delay = max(delay, float(ra))
                            except (ValueError, TypeError):
                                pass
                    logger.warning(
                        "Anthropic API 可重试错误 (attempt %d/%d), %.1fs 后重试: %s",
                        attempt + 1, _MAX_RETRIES, delay, _clean_error_message(exc),
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

        raise last_exc  # type: ignore[misc]

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 Claude Messages API。"""
        # 分离 system prompt + 消息交替保证
        system_text, api_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools or []) if tools else []
        cache_budget = _MAX_PROMPT_CACHE_BREAKPOINTS
        if system_text:
            cache_budget -= 1
        if anthropic_tools:
            cache_budget -= 1
        api_messages = self._apply_prompt_cache_controls(
            api_messages,
            max_breakpoints=cache_budget,
            scan_all=True,
        )

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
            "stream": True,
        }

        # System prompt（带 cache_control）
        if system_text:
            kwargs["system"] = [
                {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
            ]

        # Extended thinking（Claude 4+）
        if self._should_enable_thinking(messages, anthropic_tools):
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self._thinking_budget}

        # 转换工具定义，末尾工具加 cache_control（缓存 system+tools 前缀）
        if tools:
            if anthropic_tools:
                anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
                kwargs["tools"] = anthropic_tools
                kwargs["tool_choice"] = {"type": "auto"}

        if self._use_raw_http:
            async for event in self._stream_chat_raw_http(kwargs):
                yield event
            return

        # 带重试的流式调用
        try:
            stream = await self._call_with_retry(**kwargs)
        except Exception as exc:
            logger.error("Anthropic API 调用失败: %s", exc)
            yield StreamEvent(type=StreamEventType.ERROR, content=f"Claude API 调用失败: {_clean_error_message(exc)}")
            return

        # 解析流式事件
        pending_tool_calls: list[ToolCallEvent] = []
        current_tool_id = ""
        current_tool_name = ""
        current_tool_args = ""
        usage = UsageInfo()
        stop_reason = ""

        try:
            async for event in stream:
                event_type = getattr(event, "type", "")

                if event_type == "message_start":
                    msg = getattr(event, "message", None)
                    if msg:
                        usage_obj = getattr(msg, "usage", None)
                        if usage_obj:
                            usage = UsageInfo(
                                input_tokens=getattr(usage_obj, "input_tokens", 0),
                                output_tokens=getattr(usage_obj, "output_tokens", 0),
                                cache_creation_input_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0),
                                cache_read_input_tokens=getattr(usage_obj, "cache_read_input_tokens", 0),
                            )
                        stop_reason = getattr(msg, "stop_reason", "") or ""

                elif event_type == "content_block_start":
                    content_block = getattr(event, "content_block", None)
                    if content_block:
                        cb_type = getattr(content_block, "type", "")
                        if cb_type == "tool_use":
                            current_tool_id = getattr(content_block, "id", "")
                            current_tool_name = getattr(content_block, "name", "")
                            current_tool_args = ""
                            _delta_bytes_since_emit = 0
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_start=ToolCallStartEvent(
                                    id=current_tool_id,
                                    name=current_tool_name,
                                    index=len(pending_tool_calls),
                                ),
                            )
                        elif cb_type == "image":
                            source = getattr(content_block, "source", None)
                            if source:
                                media_type = getattr(source, "media_type", "image/png")
                                data = getattr(source, "data", "")
                                if data:
                                    yield StreamEvent(
                                        type=StreamEventType.IMAGE_CHUNK,
                                        image_data=data,
                                        image_media_type=media_type,
                                    )

                elif event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if not delta:
                        continue
                    delta_type = getattr(delta, "type", "")
                    if delta_type == "text_delta":
                        text = getattr(delta, "text", "")
                        if text:
                            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=text)
                    elif delta_type in {"thinking_delta", "signature_delta"}:
                        thinking = getattr(delta, "thinking", "") or getattr(delta, "text", "")
                        if thinking:
                            yield StreamEvent(type=StreamEventType.THINKING_CHUNK, content=thinking)
                    elif delta_type == "input_json_delta":
                        partial = getattr(delta, "partial_json", "")
                        if partial:
                            current_tool_args += partial
                            _delta_bytes_since_emit += len(partial)
                            if _delta_bytes_since_emit >= _DELTA_DEBOUNCE_BYTES:
                                _delta_bytes_since_emit = 0
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_DELTA,
                                    tool_call_delta=ToolCallDeltaEvent(
                                        id=current_tool_id,
                                        partial_arguments=current_tool_args,
                                    ),
                                )

                elif event_type == "content_block_stop":
                    if current_tool_id and current_tool_name:
                        try:
                            arguments = json.loads(current_tool_args) if current_tool_args else {}
                        except (json.JSONDecodeError, TypeError):
                            from backend.llm.json_repair import repair_tool_json
                            arguments = repair_tool_json(current_tool_args) or {"_raw": current_tool_args}
                        completed_tool_call = ToolCallEvent(id=current_tool_id, name=current_tool_name, arguments=arguments)
                        pending_tool_calls.append(completed_tool_call)
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL,
                            tool_calls=[completed_tool_call],
                            tool_calls_final=False,
                        )
                        current_tool_id = ""
                        current_tool_name = ""
                        current_tool_args = ""

                elif event_type == "message_delta":
                    delta_obj = getattr(event, "delta", None)
                    if delta_obj:
                        sr = getattr(delta_obj, "stop_reason", None)
                        if sr:
                            stop_reason = sr
                    usage_obj = getattr(event, "usage", None)
                    if usage_obj:
                        out_tokens = getattr(usage_obj, "output_tokens", 0)
                        if out_tokens:
                            usage = UsageInfo(
                                input_tokens=usage.input_tokens,
                                output_tokens=out_tokens,
                                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                                cache_read_input_tokens=usage.cache_read_input_tokens,
                            )

                elif event_type == "message_stop":
                    pass

                elif event_type == "ping":
                    pass

        except Exception as exc:
            logger.error("Anthropic 流式解析异常: %s", exc)
            yield StreamEvent(type=StreamEventType.ERROR, content=f"Claude 流式响应异常: {_clean_error_message(exc)}")
            return

        if pending_tool_calls:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=pending_tool_calls)

        if stop_reason == "max_tokens":
            logger.warning("Claude 响应因 max_tokens 截断")

        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=stop_reason)

    def _messages_url(self) -> str:
        endpoint = (self._base_url or "https://api.anthropic.com/v1").rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint = f"{endpoint}/v1"
        return f"{endpoint}/messages"

    def _raw_headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self._use_auth_token:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["x-api-key"] = self._api_key
        return headers

    async def _stream_chat_raw_http(self, kwargs: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        """Stream Anthropic Messages over plain HTTP for custom gateways that block the SDK."""
        pending_tool_calls: list[ToolCallEvent] = []
        current_tool_id = ""
        current_tool_name = ""
        current_tool_args = ""
        usage = UsageInfo()
        stop_reason = ""

        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream(
                    "POST",
                    self._messages_url(),
                    headers=self._raw_headers(),
                    json=kwargs,
                ) as response:
                    response.raise_for_status()
                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload_text = line[5:].strip()
                        if not payload_text or payload_text == "[DONE]":
                            continue
                        try:
                            event = json.loads(payload_text)
                        except json.JSONDecodeError:
                            continue

                        event_type = str(event.get("type") or "")
                        if event_type == "error":
                            error = event.get("error") if isinstance(event.get("error"), dict) else {}
                            message = str(error.get("message") or event.get("message") or "Anthropic stream error")
                            yield StreamEvent(type=StreamEventType.ERROR, content=f"Claude API 调用失败: {message}")
                            return

                        if event_type == "message_start":
                            message = event.get("message") if isinstance(event.get("message"), dict) else {}
                            usage_obj = message.get("usage") if isinstance(message, dict) else {}
                            if isinstance(usage_obj, dict):
                                usage = UsageInfo(
                                    input_tokens=int(usage_obj.get("input_tokens") or 0),
                                    output_tokens=int(usage_obj.get("output_tokens") or 0),
                                    cache_creation_input_tokens=int(usage_obj.get("cache_creation_input_tokens") or 0),
                                    cache_read_input_tokens=int(usage_obj.get("cache_read_input_tokens") or 0),
                                )
                            stop_reason = str(message.get("stop_reason") or "") if isinstance(message, dict) else ""

                        elif event_type == "content_block_start":
                            block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
                            if block.get("type") == "tool_use":
                                current_tool_id = str(block.get("id") or "")
                                current_tool_name = str(block.get("name") or "")
                                current_tool_args = ""
                                _delta_bytes_since_emit = 0
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_START,
                                    tool_call_start=ToolCallStartEvent(
                                        id=current_tool_id,
                                        name=current_tool_name,
                                        index=len(pending_tool_calls),
                                    ),
                                )

                        elif event_type == "content_block_delta":
                            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                            delta_type = str(delta.get("type") or "")
                            if delta_type == "text_delta":
                                text = str(delta.get("text") or "")
                                if text:
                                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=text)
                            elif delta_type in {"thinking_delta", "signature_delta"}:
                                thinking = str(delta.get("thinking") or delta.get("text") or "")
                                if thinking:
                                    yield StreamEvent(type=StreamEventType.THINKING_CHUNK, content=thinking)
                            elif delta_type == "input_json_delta":
                                partial = str(delta.get("partial_json") or "")
                                current_tool_args += partial
                                _delta_bytes_since_emit += len(partial)
                                if _delta_bytes_since_emit >= _DELTA_DEBOUNCE_BYTES:
                                    _delta_bytes_since_emit = 0
                                    yield StreamEvent(
                                        type=StreamEventType.TOOL_CALL_DELTA,
                                        tool_call_delta=ToolCallDeltaEvent(
                                            id=current_tool_id,
                                            partial_arguments=current_tool_args,
                                        ),
                                    )

                        elif event_type == "content_block_stop":
                            if current_tool_id and current_tool_name:
                                try:
                                    arguments = json.loads(current_tool_args) if current_tool_args else {}
                                except (json.JSONDecodeError, TypeError):
                                    from backend.llm.json_repair import repair_tool_json
                                    arguments = repair_tool_json(current_tool_args) or {"_raw": current_tool_args}
                                pending_tool_calls.append(
                                    ToolCallEvent(id=current_tool_id, name=current_tool_name, arguments=arguments)
                                )
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL,
                                    tool_calls=[pending_tool_calls[-1]],
                                    tool_calls_final=False,
                                )
                                current_tool_id = ""
                                current_tool_name = ""
                                current_tool_args = ""

                        elif event_type == "message_delta":
                            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                            if isinstance(delta, dict) and delta.get("stop_reason"):
                                stop_reason = str(delta.get("stop_reason") or "")
                            usage_obj = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                            if isinstance(usage_obj, dict) and usage_obj.get("output_tokens") is not None:
                                usage = UsageInfo(
                                    input_tokens=usage.input_tokens,
                                    output_tokens=int(usage_obj.get("output_tokens") or 0),
                                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                                    cache_read_input_tokens=usage.cache_read_input_tokens,
                                )
        except Exception as exc:
            logger.error("Anthropic raw HTTP 调用失败: %s", exc)
            yield StreamEvent(type=StreamEventType.ERROR, content=f"Claude API 调用失败: {_clean_error_message(exc)}")
            return

        if pending_tool_calls:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=pending_tool_calls)
        if stop_reason == "max_tokens":
            logger.warning("Claude 响应因 max_tokens 截断")
        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=stop_reason)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        """非流式调用。"""
        system_text, api_messages = self._convert_messages(messages)
        api_messages = self._apply_prompt_cache_controls(
            api_messages,
            max_breakpoints=_MAX_PROMPT_CACHE_BREAKPOINTS - (1 if system_text else 0),
            scan_all=True,
        )

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
        }

        if system_text:
            kwargs["system"] = [
                {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
            ]

        try:
            response = await self._call_with_retry(**kwargs)
        except Exception as exc:
            logger.error("Anthropic simple_chat 失败: %s", exc)
            raise RuntimeError(f"Claude 调用失败: {exc}") from exc

        text_parts = []
        for block in getattr(response, "content", []):
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))

        text = "\n".join(text_parts).strip()
        if not text:
            raise RuntimeError("Claude 返回空内容")

        return text

    # ── 消息格式转换 ──────────────────────────────────────

    @staticmethod
    def _convert_messages(
        messages: list[LLMMessage],
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        将 LLMMessage 列表转换为 Anthropic Messages API 格式。

        Anthropic 要求：
          1. system prompt 是顶级字段
          2. user / assistant 严格交替
          3. tool_result 以 user 角色发送，紧跟 assistant(tool_use)
          4. 连续多条 tool_result 需合并为一条 user 消息
        """
        system_parts: list[str] = []
        raw_messages: list[dict[str, Any]] = []

        # 第一遍：提取 system + 初步转换
        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
                continue

            if msg.role == "user":
                if msg.images or msg.documents:
                    parts: list[dict[str, Any]] = []
                    if msg.content:
                        parts.append({"type": "text", "text": msg.content})
                    for img in msg.images:
                        media_type = img.get("media_type") or "image/png"
                        data = img.get("data") or ""
                        if not data:
                            continue
                        parts.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        })
                    for doc in msg.documents:
                        media_type = doc.get("media_type") or "application/pdf"
                        data = doc.get("data") or ""
                        if not data:
                            continue
                        parts.append({
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        })
                    raw_messages.append({"role": "user", "content": parts or msg.content})
                else:
                    raw_messages.append({"role": "user", "content": msg.content})

            elif msg.role == "assistant":
                content_parts: list[dict[str, Any]] = []
                if msg.content:
                    content_parts.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        content_parts.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                raw_messages.append({
                    "role": "assistant",
                    "content": content_parts if content_parts else [{"type": "text", "text": ""}],
                })

            elif msg.role == "tool":
                raw_messages.append({
                    "role": "tool_result",
                    "tool_use_id": msg.tool_call_id or "",
                    "content": msg.content,
                })

        # 第二遍：保证 user/assistant 交替 + 合并连续 tool_result
        api_messages: list[dict[str, Any]] = []
        i = 0

        while i < len(raw_messages):
            msg = raw_messages[i]

            if msg["role"] == "tool_result":
                # 合并连续的 tool_result 为一条 user 消息
                tool_results: list[dict[str, Any]] = [{
                    "type": "tool_result",
                    "tool_use_id": msg["tool_use_id"],
                    "content": msg["content"],
                }]
                j = i + 1
                while j < len(raw_messages) and raw_messages[j]["role"] == "tool_result":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": raw_messages[j]["tool_use_id"],
                        "content": raw_messages[j]["content"],
                    })
                    j += 1
                api_messages.append({"role": "user", "content": tool_results})
                i = j

            elif msg["role"] == "user":
                if api_messages and api_messages[-1]["role"] == "user":
                    # 合并连续 user 消息
                    prev = api_messages[-1]
                    if isinstance(prev["content"], str) and isinstance(msg["content"], str):
                        prev["content"] += "\n\n" + msg["content"]
                    else:
                        prev_blocks = prev["content"] if isinstance(prev["content"], list) else [{"type": "text", "text": prev["content"]}]
                        curr_blocks = msg["content"] if isinstance(msg["content"], list) else [{"type": "text", "text": msg["content"]}]
                        prev["content"] = prev_blocks + curr_blocks
                else:
                    api_messages.append(msg)
                i += 1

            elif msg["role"] == "assistant":
                if api_messages and api_messages[-1]["role"] == "assistant":
                    # 合并连续 assistant 消息
                    prev = api_messages[-1]
                    prev_content = prev["content"] if isinstance(prev["content"], list) else [{"type": "text", "text": prev["content"]}]
                    curr_content = msg["content"] if isinstance(msg["content"], list) else [{"type": "text", "text": msg["content"]}]
                    prev["content"] = prev_content + curr_content
                else:
                    api_messages.append(msg)
                i += 1
            else:
                i += 1

        # 安全检查：确保第一条是 user
        if api_messages and api_messages[0]["role"] != "user":
            api_messages.insert(0, {"role": "user", "content": "(conversation start)"})

        system_text = "\n\n".join(system_parts)
        return system_text, api_messages

    @staticmethod
    def _apply_prompt_cache_controls(
        api_messages: list[dict[str, Any]],
        recent_count: int = 3,
        max_breakpoints: int | None = None,
        scan_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Apply cache_control breakpoints using a stable "turn boundary" strategy.

        Instead of scoring messages (which shifts breakpoint positions each turn and
        invalidates the cache), we place breakpoints at fixed structural positions:
        1. The last message before the current user turn (stable as conversation grows)
        2. A midpoint in longer conversations (for partial prefix reuse)

        This mirrors Claude Code's approach: the prefix up to a breakpoint stays
        byte-identical across turns, maximizing cache hits.
        """
        breakpoint_budget = recent_count if max_breakpoints is None else max(0, max_breakpoints)
        if breakpoint_budget <= 0 or not api_messages:
            return [dict(message) for message in api_messages]

        selected_indices = AnthropicAdapter._select_prompt_cache_breakpoint_indices(
            api_messages,
            breakpoint_budget,
            scan_all=scan_all,
        )

        cloned_messages: list[dict[str, Any]] = []
        for index, message in enumerate(api_messages):
            cloned = dict(message)
            content = cloned.get("content")

            if isinstance(content, list):
                cloned["content"] = [
                    {key: value for key, value in dict(block).items() if key != "cache_control"}
                    for block in content
                ]

            if index not in selected_indices:
                cloned_messages.append(cloned)
                continue

            if isinstance(content, str):
                cloned["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            elif isinstance(cloned.get("content"), list) and cloned["content"]:
                last_block = dict(cloned["content"][-1])
                last_block["cache_control"] = {"type": "ephemeral"}
                cloned["content"][-1] = last_block

            cloned_messages.append(cloned)

        return cloned_messages

    @staticmethod
    def _select_prompt_cache_breakpoint_indices(
        api_messages: list[dict[str, Any]],
        breakpoint_budget: int,
        *,
        scan_all: bool = False,
    ) -> set[int]:
        """Select message breakpoints within Anthropic's cache_control budget."""
        if not api_messages:
            return set()

        n = len(api_messages)
        budget = max(0, min(breakpoint_budget, n))
        if budget <= 0:
            return set()

        if not scan_all:
            return set(range(n - budget, n))

        ranked = sorted(
            range(n),
            key=lambda index: (
                AnthropicAdapter._prompt_cache_candidate_score(api_messages[index]),
                index,
            ),
            reverse=True,
        )
        indices = {
            index
            for index in ranked[:budget]
            if AnthropicAdapter._prompt_cache_candidate_score(api_messages[index]) > 0
        }

        if len(indices) < budget:
            for index in range(n - 1, -1, -1):
                indices.add(index)
                if len(indices) >= budget:
                    break

        return indices

    @staticmethod
    def _prompt_cache_candidate_score(message: dict[str, Any]) -> int:
        content = message.get("content")
        if isinstance(content, str):
            return len(content)
        if not isinstance(content, list):
            return 0

        score = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "document":
                score += 100_000
            elif block_type == "image":
                score += 80_000
            elif block_type == "tool_result":
                score += 20_000 + len(str(block.get("content") or ""))
            elif block_type == "tool_use":
                score += 10_000 + len(json.dumps(block.get("input") or {}, ensure_ascii=False))
            elif block_type == "text":
                score += len(str(block.get("text") or ""))
            else:
                score += len(str(block))
        return score

    def _should_enable_thinking(
        self,
        messages: list[LLMMessage],
        anthropic_tools: list[dict[str, Any]],
    ) -> bool:
        if not self._thinking_budget or self._thinking_budget <= 0:
            return False

        if anthropic_tools:
            return True

        if any(message.images or message.documents for message in messages):
            return True

        # Re-enable thinking if recent tool results suggest complexity
        # (errors, large outputs, or the model is stuck)
        recent_tool_results = [
            m.content or "" for m in messages[-6:]
            if m.role == "tool"
        ]
        for result in recent_tool_results:
            if any(kw in result.lower() for kw in ("error", "traceback", "failed", "exception", "not found")):
                return True
            if len(result) >= 3000:
                return True

        user_text = "\n".join(message.content or "" for message in messages if message.role == "user")
        if len(user_text) >= 900:
            return True
        return bool(_THINKING_TRIGGER_RE.search(user_text))

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        将 OpenAI function-calling 格式转换为 Anthropic tool_use 格式。

        OpenAI:
          {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

        Anthropic:
          {"name": ..., "description": ..., "input_schema": ...}
        """
        anthropic_tools = []
        for tool in tools:
            func = tool.get("function", {})
            if not func:
                continue

            at: dict[str, Any] = {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            }
            anthropic_tools.append(at)

        return anthropic_tools
