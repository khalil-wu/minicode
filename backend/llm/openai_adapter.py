
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
import json
import logging
import os
import re
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

from backend.config import LLMSettings
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
)

logger = logging.getLogger(__name__)

_DELTA_DEBOUNCE_BYTES = 128


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


def _provider_host(base_url: str) -> str:
    parsed = urlparse(base_url or "https://api.openai.com/v1")
    return parsed.netloc or parsed.path.split("/")[0] or "unknown"


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
        if message.role == "user" and message.content.strip():
            return [{"role": "user", "content": message.content.strip()}]
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

    async def _create_responses_with_retry(self, kwargs: dict[str, Any]) -> Any:
        for attempt in range(2):
            try:
                return await self._client.responses.create(**kwargs)
            except Exception as exc:
                if attempt == 0 and _is_transient_gateway_error(exc):
                    logger.warning(
                        "Responses API transient failure, retrying once: %s",
                        exc,
                    )
                    await asyncio.sleep(_ADAPTER_RETRY_DELAY_SECONDS)
                    continue
                raise
        raise RuntimeError("Responses API retry failed without an upstream exception")

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

        model = self._settings.model
        kwargs: dict[str, Any] = {
            "model": _response_tool_model(model),
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
        if _is_image_model(model) or _is_image_generation_prompt(messages):
            kwargs["tools"] = [
                *(kwargs.get("tools") or []),
                _image_generation_tool(model),
            ]

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
        usage = UsageInfo()
        finish_reason = ""

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
                        from backend.llm.json_repair import repair_tool_json
                        arguments = repair_tool_json(arguments_str) or {"_raw": arguments_str}
                    pending_tool_calls.append(
                        ToolCallEvent(
                            id=call_id,
                            name=name,
                            arguments=arguments,
                        )
                    )

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
                            )
                elif event_type == "response.incomplete":
                    response_obj = getattr(event, "response", None)
                    finish_reason = _response_finish_reason(response_obj) or "incomplete"
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

        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=finish_reason)

    async def _simple_responses_api(self, messages: list[LLMMessage]) -> str:
        """Responses API 非流式调用。"""
        api_input = self._build_responses_input(messages)

        model = self._settings.model
        kwargs: dict[str, Any] = {
            "model": _response_tool_model(model),
            "input": api_input,
        }

        if _is_image_model(model) or _is_image_generation_prompt(messages):
            kwargs["tools"] = [_image_generation_tool(model)]

        if self._settings.reasoning_effort:
            kwargs["reasoning"] = {"effort": self._settings.reasoning_effort}

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
            if msg.role == "system":
                result.append({
                    "role": "system",
                    "content": msg.content,
                })
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

    async def _emit_chat_http_stream_events(self, payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        if self._raw_http_client is None:
            raise RuntimeError("Chat HTTP client is not initialized")

        full_text = ""
        accumulator = _ToolCallAccumulator()
        usage = UsageInfo()
        finish_reason = ""

        async with self._raw_http_client.stream(
            "POST",
            self._chat_completions_url(),
            headers=self._chat_headers(),
            json=payload,
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
                    break

                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring malformed chat stream line: %s", line[:200])
                    continue

                usage_obj = chunk.get("usage")
                if usage_obj:
                    usage = UsageInfo(
                        input_tokens=_get_usage_field(usage_obj, "prompt_tokens"),
                        output_tokens=_get_usage_field(usage_obj, "completion_tokens"),
                        cache_read_input_tokens=_get_cached_prompt_tokens(usage_obj),
                    )

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}

                content = delta.get("content")
                if content:
                    full_text += str(content)
                    yield StreamEvent(
                        type=StreamEventType.TEXT_CHUNK,
                        content=str(content),
                    )

                for tool_call in delta.get("tool_calls") or []:
                    idx = int(tool_call.get("index") or 0)
                    is_new, _key, slot = accumulator.feed(tool_call, idx)
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
                    continue

        tool_call_events = accumulator.finalize()
        if tool_call_events:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=tool_call_events)

        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=finish_reason)

    async def _stream_chat_completions_http(
        self,
        kwargs: dict[str, Any],
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        async def emit(payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
            async for event in self._emit_chat_http_stream_events(payload):
                yield event

        async def minimal_payload() -> dict[str, Any]:
            return {
                "model": self._settings.model,
                "messages": _minimal_chat_messages(messages),
                "stream": True,
                "max_tokens": min(self._settings.max_tokens, 512),
            }

        try:
            async for event in emit(kwargs):
                yield event
            return
        except Exception as exc:
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
            "max_tokens": self._settings.max_tokens,
            "stream": False,
        }

        try:
            response = await self._raw_http_client.post(
                self._chat_completions_url(),
                headers=self._chat_headers(),
                json=payload,
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
    ) -> AsyncIterator[StreamEvent]:
        """使用 Chat Completions API 流式调用。"""
        openai_messages = [
            msg.to_openai_message()
            for msg in messages
        ]

        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": openai_messages,
            "stream": True,
            "max_tokens": self._settings.max_tokens,
            # Ask the gateway to emit a trailing usage-only chunk (choices: [],
            # usage: {...}) after generation. Without this the Chat Completions
            # wire API sends no token counts at all and usage stays at zero.
            "stream_options": {"include_usage": True},
        }

        if tools:
            kwargs["tools"] = self._normalize_chat_tools(tools)
            kwargs["tool_choice"] = "auto"

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
                max_tokens=min(self._settings.max_tokens, 512),
            )

        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            if tools and (_is_invalid_tool_schema_error(exc) or _is_blocked_gateway_error(exc)):
                logger.warning(
                    "Chat Completions rejected the tool-enabled request, retrying without tools: %s",
                    exc,
                )
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("tools", None)
                retry_kwargs.pop("tool_choice", None)
                try:
                    stream = await self._client.chat.completions.create(**retry_kwargs)
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
                if _is_blocked_gateway_error(exc):
                    try:
                        stream = await create_minimal_stream(exc)
                    except Exception as minimal_exc:
                        _log_chat_provider_error(self._settings, "Chat Completions API minimal retry", minimal_exc)
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            content=_adapter_error_content("LLM API 调用失败", minimal_exc),
                        )
                        return
                else:
                    _log_chat_provider_error(self._settings, "Chat Completions API", exc)
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=_adapter_error_content("LLM API 调用失败", exc),
                    )
                    return

        full_text = ""
        accumulator = _ToolCallAccumulator()
        usage = UsageInfo()

        try:
            async for chunk in stream:
                if not chunk.choices:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = UsageInfo(
                            input_tokens=_get_usage_field(chunk.usage, "prompt_tokens"),
                            output_tokens=_get_usage_field(chunk.usage, "completion_tokens"),
                            cache_read_input_tokens=_get_cached_prompt_tokens(chunk.usage),
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
                        idx = int(tc.index) if tc.index is not None else 0
                        tc_dict = {
                            "id": tc.id or "",
                            "function": {
                                "name": tc.function.name if tc.function else "",
                                "arguments": tc.function.arguments if tc.function else "",
                            },
                        }
                        is_new, _key, slot = accumulator.feed(tc_dict, idx)
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
                    continue
        except Exception as exc:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=f"Stream interrupted: {_clean_error_message(exc)}",
            )
            return

        tool_call_events = accumulator.finalize()
        if tool_call_events:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=tool_call_events)

        yield StreamEvent(type=StreamEventType.DONE, usage=usage, finish_reason=finish_reason)

    async def _simple_chat_completions(self, messages: list[LLMMessage]) -> str:
        """Chat Completions API 非流式调用。"""
        if self._use_raw_chat_http:
            return await self._simple_chat_completions_http(messages)

        openai_messages = [
            msg.to_openai_message()
            for msg in messages
        ]

        try:
            response = await self._client.chat.completions.create(
                model=self._settings.model,
                messages=openai_messages,
                max_tokens=self._settings.max_tokens,
            )
        except Exception as exc:
            _log_chat_provider_error(self._settings, "Chat Completions simple_chat", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()

        raise RuntimeError("LLM 返回空内容")
