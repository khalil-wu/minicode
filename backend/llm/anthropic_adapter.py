"""
Anthropic Claude 适配器（DESIGN.md §一 LLM Adapter）。

增强特性：
  - 消息交替规则保证（user/assistant 严格交替）
  - message_delta 最终 usage（含 cache tokens）
  - stop_reason 处理（end_turn / tool_use / max_tokens）
  - system prompt byte-stable prefix split
  - extended thinking 支持（Claude 4+）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, AsyncIterator

import httpx

from backend.agent.prompting import split_sys_prompt_prefix
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
from backend.llm.capabilities import ProviderCapabilities, capabilities_from_anthropic_adapter

logger = logging.getLogger(__name__)

_DELTA_DEBOUNCE_BYTES = 128

_ANTHROPIC_PROMPT_CACHE_CONTROL = {"type": "ephemeral"}

# Anthropic 1h prompt-cache TTL requires this beta header.
# https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching#1-hour-cache-duration
_EXTENDED_CACHE_TTL_BETA_HEADER = "extended-cache-ttl-2025-04-11"


def _cache_ttl_1h_enabled() -> bool:
    """Whether the 1h prompt-cache TTL is opted in (env MINICODE_CACHE_TTL_1H).

    Mirrors cc's ``should1hCacheTTL`` opt-in gating (claude.ts getCacheControl):
    cache_control stays ``{"type": "ephemeral"}`` by default and only gains
    ``"ttl": "1h"`` when explicitly enabled.
    """
    return os.getenv("MINICODE_CACHE_TTL_1H", "").strip().lower() in {"1", "true", "yes", "on"}


def _cache_control() -> dict[str, Any]:
    """Build the cache_control marker, cc's ``getCacheControl`` equivalent."""
    if _cache_ttl_1h_enabled():
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def _clean_error_message(exc: Exception) -> str:
    """清洗错误消息，移除 HTML 标签。"""
    msg = str(exc)
    msg = re.sub(r"<[^>]+>", " ", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    if len(msg) > 300:
        msg = msg[:300] + "..."
    return msg


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


def _is_cache_control_unsupported_error(exc: Exception) -> bool:
    text = _clean_error_message(exc).lower()
    status_code = getattr(exc, "status_code", None)
    mentions_cache_control = "cache_control" in text or "cache control" in text
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
    return bool(status_code in {400, 422} and mentions_cache_control and mentions_incompatibility)


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
        content_hash = _json_fingerprint(message.get("content")) if message.get("content") not in (None, "", [], {}) else ""
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
    duplicates = [group for group in duplicate_groups.values() if int(group.get("count") or 0) > 1]
    duplicates.sort(key=lambda item: (-int(item.get("chars") or 0), str(item.get("role") or "")))
    return {
        "input_chars": total_chars,
        "largest_input_items": largest[:5],
        "duplicate_input_content": duplicates[:5],
    }


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
                        if isinstance(block, dict) and block.get("type") == "cache_edits":
                            edits = block.get("edits")
                            cache_edit_count += len(edits) if isinstance(edits, list) else 0
    params["cache_control_present"] = cache_breakpoints > 0 or "cache_control" in kwargs
    params["cache_breakpoints"] = cache_breakpoints
    params["cache_edit_count"] = cache_edit_count
    params["cache_editing_header_present"] = bool(
        cache_edit_count
        and isinstance(kwargs.get("extra_headers"), dict)
        and kwargs["extra_headers"].get("anthropic-beta")
    )
    params["metadata_present"] = "metadata" in kwargs
    params["system_blocks"] = len(kwargs.get("system") or []) if isinstance(kwargs.get("system"), list) else 0
    params["tools_len"] = len(kwargs.get("tools") or []) if isinstance(kwargs.get("tools"), list) else 0
    return params


def _anthropic_safe_request_summary(
    *,
    model: str,
    system_text: str,
    api_messages: list[dict[str, Any]],
    anthropic_tools: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    clean_metadata = sanitize_llm_request_metadata(metadata)
    stable_system = split_sys_prompt_prefix(system_text).stable_prefix if system_text else ""
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
        "previous_response_id_present": False,
        "previous_response_id_hash": "",
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


class AnthropicAdapter(LLMAdapter):
    """
    Anthropic Claude 适配器。

    使用示例：
        adapter = AnthropicAdapter(api_key="sk-ant-...", model="claude-sonnet-4-6")
        async for event in adapter.stream_chat(messages, tools):
            ...
    """

    # Session-scoped cache of converted tool schemas. Tool schemas sit at
    # API position 2 (after system prompt), so any byte-level change busts
    # the entire tool block AND everything downstream. Memoizing per-adapter
    # (session) locks the schema bytes at first render — mid-session tool
    # description drift no longer churns the serialized array. The cache key
    # includes the input schema fingerprint, matching current Claude Code: a
    # reconnected MCP tool with the same name but a new contract must not reuse
    # a stale first-render schema.
    _tool_schema_cache: dict[str, dict[str, Any]]

    def __init__(
        self,
        api_key: str,
        model: str = "",
        base_url: str | None = None,
        max_tokens: int = 8_000,
        thinking_budget: int | None = None,
        use_auth_token: bool = False,
        use_raw_http: bool = False,
        cache_editing_beta_header: str = "",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._max_tokens = max(1, int(max_tokens or 8_000))
        self._thinking_budget = thinking_budget
        self._use_auth_token = use_auth_token
        self._use_raw_http = use_raw_http
        # Cache editing is a private provider capability. It is disabled unless
        # the caller supplies the exact provider-declared beta header; MiniCode
        # never guesses or enables it from a generic feature flag.
        self._cache_editing_beta_header = str(cache_editing_beta_header or "").strip()
        self._cache_editing_disabled_reason = ""
        self._pending_cache_deletions: list[str] = []
        self._pinned_cache_edits: list[tuple[int, str, dict[str, Any]]] = []
        self._client = None
        self._tool_schema_cache = {}

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        close = getattr(client, "close", None)
        if callable(close):
            await close()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return capabilities_from_anthropic_adapter(self)

    def queue_cache_deletions(self, tool_call_ids: list[str] | tuple[str, ...]) -> bool:
        """Queue provider-native deletions when the configured provider supports them."""
        if not self._cache_editing_beta_header or self._cache_editing_disabled_reason:
            return False
        known = set(self._pending_cache_deletions)
        known.update(
            str(edit.get("cache_reference") or "")
            for _, _, block in self._pinned_cache_edits
            for edit in block.get("edits", [])
            if isinstance(edit, dict)
        )
        for raw_id in tool_call_ids:
            call_id = str(raw_id or "").strip()
            if call_id and call_id not in known:
                self._pending_cache_deletions.append(call_id)
                known.add(call_id)
        return True

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
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url

        self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def _call_with_retry(self, **kwargs: Any):
        """Create one message through the official Anthropic SDK policy."""
        client = self._get_client()
        return await client.messages.create(**kwargs)

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 Claude Messages API。"""
        # 分离 system prompt + 消息交替保证
        system_text, api_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools_cached(tools or []) if tools else []

        # Add cache_control breakpoints to last message and last tool.
        # System blocks get cache_control from _build_system_blocks.
        pending_cache_deletions = tuple(self._pending_cache_deletions)
        cache_editing_enabled = bool(
            self._cache_editing_beta_header and not self._cache_editing_disabled_reason
        )
        cached_messages, cached_tools, new_cache_edit_pin = self._add_cache_breakpoints(
            api_messages,
            anthropic_tools if tools else None,
            cache_editing=cache_editing_enabled,
            new_cache_deletions=pending_cache_deletions,
            pinned_cache_edits=tuple(self._pinned_cache_edits),
            skip_cache_write=bool((metadata or {}).get("prompt_cache_skip_write")),
        )

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": cached_messages,
            "stream": True,
        }
        request_metadata = _anthropic_request_metadata(metadata)
        if request_metadata:
            kwargs["metadata"] = request_metadata

        # 1h cache TTL requires the extended-cache-ttl beta header. The SDK
        # accepts extra_headers on messages.create; the raw-HTTP path pops it
        # into HTTP headers. _strip_all_cache_control removes it on fallback.
        if _cache_ttl_1h_enabled():
            kwargs["extra_headers"] = {"anthropic-beta": _EXTENDED_CACHE_TTL_BETA_HEADER}
        if cache_editing_enabled:
            extra_headers = dict(kwargs.get("extra_headers") or {})
            existing_beta = str(extra_headers.get("anthropic-beta") or "").strip()
            beta_values = [value for value in (existing_beta, self._cache_editing_beta_header) if value]
            extra_headers["anthropic-beta"] = ",".join(dict.fromkeys(beta_values))
            kwargs["extra_headers"] = extra_headers

        # System prompt: split stable/dynamic blocks with cache_control on
        # the stable prefix so Anthropic caches it across turns.
        if system_text:
            kwargs["system"] = self._build_system_blocks(system_text)

        # Extended thinking（Claude 4+）
        if self._should_enable_thinking(messages, anthropic_tools):
            # Anthropic hard-rejects budget_tokens >= max_tokens. Clamp exactly
            # like Claude Code (claude.ts: thinkingBudget = min(maxOutputTokens-1,
            # requestedBudget)) so a user setting thinking_budget >= max_tokens
            # does not produce a 400.
            budget_tokens = min(self._thinking_budget, self._max_tokens - 1)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget_tokens}

        if tools and cached_tools:
            kwargs["tools"] = cached_tools
            kwargs["tool_choice"] = {"type": "auto"}

        request_summary = _anthropic_safe_request_summary(
            model=self._model,
            system_text=system_text,
            api_messages=api_messages,
            anthropic_tools=anthropic_tools,
            metadata=metadata,
            kwargs=kwargs,
        )

        if self._use_raw_http:
            async for event in self._stream_chat_raw_http(kwargs, request_summary=request_summary):
                if event.type == StreamEventType.DONE:
                    self._commit_cache_edit_request(pending_cache_deletions, new_cache_edit_pin)
                yield event
            return

        # 带重试的流式调用
        try:
            stream = await self._call_with_retry(**kwargs)
        except Exception as exc:
            retry_without_cache = _is_cache_control_unsupported_error(exc)
            if retry_without_cache:
                logger.warning(
                    "Anthropic gateway rejected cache_control; retrying without all cache markers: %s",
                    _clean_error_message(exc),
                )
                retry_kwargs = self._strip_all_cache_control(
                    kwargs,
                    cache_editing_beta_header=self._cache_editing_beta_header,
                )
                if cache_editing_enabled:
                    self._cache_editing_disabled_reason = "provider_rejected_cache_editing_request"
                    pending_cache_deletions = ()
                    new_cache_edit_pin = None
                request_summary = _anthropic_safe_request_summary(
                    model=self._model,
                    system_text=system_text,
                    api_messages=api_messages,
                    anthropic_tools=anthropic_tools,
                    metadata=metadata,
                    kwargs=retry_kwargs,
                )
                try:
                    stream = await self._call_with_retry(**retry_kwargs)
                except Exception as retry_exc:
                    logger.error("Anthropic API 调用失败: %s", retry_exc)
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        content=f"Claude API 调用失败: {_clean_error_message(retry_exc)}",
                    )
                    return
            else:
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
        saw_message_stop = False
        provider_items: list[dict[str, Any]] = []
        current_reasoning_item: dict[str, Any] | None = None

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
                                cache_deleted_input_tokens=getattr(usage_obj, "cache_deleted_input_tokens", 0),
                                # Anthropic reports cache reads separately from input_tokens.
                                input_includes_cache_read=False,
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
                        elif cb_type in {"thinking", "redacted_thinking"}:
                            current_reasoning_item = {"type": cb_type}
                            initial_thinking = str(getattr(content_block, "thinking", "") or "")
                            initial_signature = str(getattr(content_block, "signature", "") or "")
                            initial_data = str(getattr(content_block, "data", "") or "")
                            if initial_thinking:
                                current_reasoning_item["thinking"] = initial_thinking
                            if initial_signature:
                                current_reasoning_item["signature"] = initial_signature
                            if initial_data:
                                current_reasoning_item["data"] = initial_data
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
                        signature = getattr(delta, "signature", "")
                        if current_reasoning_item is not None:
                            if thinking:
                                current_reasoning_item["thinking"] = (
                                    str(current_reasoning_item.get("thinking") or "") + str(thinking)
                                )
                            if signature:
                                current_reasoning_item["signature"] = (
                                    str(current_reasoning_item.get("signature") or "") + str(signature)
                                )
                        if thinking:
                            yield StreamEvent(
                                type=StreamEventType.THINKING_CHUNK,
                                content=thinking,
                                raw={"provider_reasoning_type": delta_type},
                            )
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
                    if current_reasoning_item is not None:
                        provider_items.append(current_reasoning_item)
                        current_reasoning_item = None
                    if current_tool_id and current_tool_name:
                        try:
                            arguments = json.loads(current_tool_args) if current_tool_args else {}
                        except (json.JSONDecodeError, TypeError):
                            from backend.llm.json_repair import repair_tool_json
                            arguments = repair_tool_json(current_tool_args) or {"_raw": current_tool_args}
                            arguments_repaired = True
                        else:
                            arguments_repaired = False
                        completed_tool_call = ToolCallEvent(id=current_tool_id, name=current_tool_name, arguments=arguments, arguments_repaired=arguments_repaired)
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
                                cache_deleted_input_tokens=max(
                                    usage.cache_deleted_input_tokens,
                                    int(getattr(usage_obj, "cache_deleted_input_tokens", 0) or 0),
                                ),
                                input_includes_cache_read=False,
                            )

                elif event_type == "message_stop":
                    saw_message_stop = True

                elif event_type == "ping":
                    pass

        except Exception as exc:
            logger.error("Anthropic 流式解析异常: %s", exc)
            yield StreamEvent(type=StreamEventType.ERROR, content=f"Claude 流式响应异常: {_clean_error_message(exc)}")
            return

        if not saw_message_stop:
            logger.error("Anthropic 流在 message_stop 之前结束")
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="Claude 流式响应在完成前中断",
                raw={"provider": "anthropic", "event_type": "eof_without_message_stop"},
            )
            return

        if pending_tool_calls:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=pending_tool_calls)

        if stop_reason == "max_tokens":
            logger.warning("Claude 响应因 max_tokens 截断")

        self._commit_cache_edit_request(pending_cache_deletions, new_cache_edit_pin)
        yield StreamEvent(
            type=StreamEventType.DONE,
            usage=usage,
            finish_reason=stop_reason,
            raw={
                "provider": "anthropic",
                "stop_reason": stop_reason,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                    "cache_read_input_tokens": usage.cache_read_input_tokens,
                    "cache_deleted_input_tokens": usage.cache_deleted_input_tokens,
                },
                "request_summary": request_summary,
            },
            provider_items=provider_items,
        )

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

    async def _stream_chat_raw_http(
        self,
        kwargs: dict[str, Any],
        *,
        request_summary: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream Anthropic Messages over plain HTTP for custom gateways that block the SDK."""
        pending_tool_calls: list[ToolCallEvent] = []
        current_tool_id = ""
        current_tool_name = ""
        current_tool_args = ""
        usage = UsageInfo()
        stop_reason = ""
        saw_message_stop = False
        provider_items: list[dict[str, Any]] = []
        current_reasoning_item: dict[str, Any] | None = None

        try:
            # extra_headers is an SDK-level convention — move it to HTTP
            # headers so it doesn't leak into the JSON body.
            body = dict(kwargs)
            extra_headers = body.pop("extra_headers", None)
            headers = self._raw_headers()
            if isinstance(extra_headers, dict):
                headers.update({str(k): str(v) for k, v in extra_headers.items()})
            # Provider stream liveness is owned by the turn/stream wait
            # policy; the raw transport must not add a second idle timeout.
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream(
                    "POST",
                    self._messages_url(),
                    headers=headers,
                    json=body,
                ) as response:
                    response.raise_for_status()
                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload_text = line[5:].strip()
                        if not payload_text:
                            continue
                        if payload_text == "[DONE]":
                            if saw_message_stop:
                                break
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
                                    cache_deleted_input_tokens=int(usage_obj.get("cache_deleted_input_tokens") or 0),
                                    # Anthropic reports cache reads separately from input_tokens.
                                    input_includes_cache_read=False,
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
                            elif block.get("type") in {"thinking", "redacted_thinking"}:
                                current_reasoning_item = {
                                    key: value
                                    for key, value in block.items()
                                    if key in {"type", "thinking", "signature", "data"}
                                }

                        elif event_type == "content_block_delta":
                            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                            delta_type = str(delta.get("type") or "")
                            if delta_type == "text_delta":
                                text = str(delta.get("text") or "")
                                if text:
                                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=text)
                            elif delta_type in {"thinking_delta", "signature_delta"}:
                                thinking = str(delta.get("thinking") or delta.get("text") or "")
                                signature = str(delta.get("signature") or "")
                                if current_reasoning_item is not None:
                                    if thinking:
                                        current_reasoning_item["thinking"] = str(current_reasoning_item.get("thinking") or "") + thinking
                                    if signature:
                                        current_reasoning_item["signature"] = str(current_reasoning_item.get("signature") or "") + signature
                                if thinking:
                                    yield StreamEvent(
                                        type=StreamEventType.THINKING_CHUNK,
                                        content=thinking,
                                        raw={"provider_reasoning_type": delta_type},
                                    )
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
                            if current_reasoning_item is not None:
                                provider_items.append(current_reasoning_item)
                                current_reasoning_item = None
                            if current_tool_id and current_tool_name:
                                try:
                                    arguments = json.loads(current_tool_args) if current_tool_args else {}
                                except (json.JSONDecodeError, TypeError):
                                    from backend.llm.json_repair import repair_tool_json
                                    arguments = repair_tool_json(current_tool_args) or {"_raw": current_tool_args}
                                    arguments_repaired = True
                                else:
                                    arguments_repaired = False
                                pending_tool_calls.append(
                                    ToolCallEvent(id=current_tool_id, name=current_tool_name, arguments=arguments, arguments_repaired=arguments_repaired)
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
                                    cache_deleted_input_tokens=max(
                                        usage.cache_deleted_input_tokens,
                                        int(usage_obj.get("cache_deleted_input_tokens") or 0),
                                    ),
                                    input_includes_cache_read=False,
                                )
                        elif event_type == "message_stop":
                            saw_message_stop = True
        except Exception as exc:
            logger.error("Anthropic raw HTTP 调用失败: %s", exc)
            yield StreamEvent(type=StreamEventType.ERROR, content=f"Claude API 调用失败: {_clean_error_message(exc)}")
            return

        if not saw_message_stop:
            logger.error("Anthropic raw HTTP 流在 message_stop 之前结束")
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="Claude 流式响应在完成前中断",
                raw={"provider": "anthropic", "event_type": "eof_without_message_stop"},
            )
            return

        if pending_tool_calls:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=pending_tool_calls)
        if stop_reason == "max_tokens":
            logger.warning("Claude 响应因 max_tokens 截断")
        yield StreamEvent(
            type=StreamEventType.DONE,
            usage=usage,
            finish_reason=stop_reason,
            raw={
                "provider": "anthropic",
                "stop_reason": stop_reason,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                    "cache_read_input_tokens": usage.cache_read_input_tokens,
                    "cache_deleted_input_tokens": usage.cache_deleted_input_tokens,
                },
                "request_summary": request_summary or {},
            },
            provider_items=provider_items,
        )

    async def simple_chat(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """非流式调用。"""
        system_text, api_messages = self._convert_messages(messages)

        cached_messages, _, _ = self._add_cache_breakpoints(api_messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": min(self._max_tokens, max(1, int(max_tokens))) if max_tokens else self._max_tokens,
            "messages": cached_messages,
        }

        if system_text:
            kwargs["system"] = self._build_system_blocks(system_text)

        try:
            response = await self._call_with_retry(**kwargs)
        except Exception as exc:
            if _is_cache_control_unsupported_error(exc):
                logger.warning(
                    "Anthropic gateway rejected cache_control; retrying without it: %s",
                    _clean_error_message(exc),
                )
                retry_kwargs = self._strip_all_cache_control(kwargs)
                try:
                    response = await self._call_with_retry(**retry_kwargs)
                except Exception as retry_exc:
                    logger.error("Anthropic simple_chat 失败: %s", retry_exc)
                    raise RuntimeError(f"Claude 调用失败: {retry_exc}") from retry_exc
            else:
                logger.error("Anthropic simple_chat 失败: %s", exc)
                raise RuntimeError(f"Claude 调用失败: {exc}") from exc

        text_parts = []
        for block in getattr(response, "content", []):
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))

        text = "\n".join(text_parts).strip()
        # Side calls (compaction/recovery) must still be counted toward
        # cost/token totals — the streaming DONE path doesn't see them.
        self.record_non_stream_usage(
            getattr(response, "usage", None),
            provider="anthropic",
            model_id=self._model,
            input_includes_cache_read=False,
        )
        if not text:
            raise RuntimeError("Claude 返回空内容")

        return text

    # ── 消息格式转换 ──────────────────────────────────────

    @staticmethod
    def _build_system_blocks(system_text: str) -> list[dict[str, Any]]:
        """Split system prompt into cache-stable and dynamic blocks.

        The stable prefix gets a ``cache_control`` breakpoint so Anthropic
        caches the byte-stable identity/rules/tools contract across turns.
        The dynamic suffix (workspace, skills, memory) is not cached because
        it changes between turns.
        """
        split = split_sys_prompt_prefix(system_text)
        cache_control = _cache_control()
        blocks: list[dict[str, Any]] = []
        if split.stable_prefix.strip():
            blocks.append({
                "type": "text",
                "text": split.stable_prefix,
                "cache_control": dict(cache_control),
            })
        if split.dynamic_suffix.strip():
            blocks.append({"type": "text", "text": split.dynamic_suffix})
        if not blocks:
            blocks.append({
                "type": "text",
                "text": system_text,
                "cache_control": dict(cache_control),
            })
        return blocks

    def _commit_cache_edit_request(
        self,
        consumed_ids: tuple[str, ...],
        new_pin: tuple[int, str, dict[str, Any]] | None,
    ) -> None:
        if consumed_ids:
            consumed = set(consumed_ids)
            self._pending_cache_deletions = [
                call_id for call_id in self._pending_cache_deletions if call_id not in consumed
            ]
        if new_pin is not None and all(existing[2] != new_pin[2] for existing in self._pinned_cache_edits):
            self._pinned_cache_edits.append(new_pin)

    @staticmethod
    def _add_cache_breakpoints(
        api_messages: list[dict[str, Any]],
        anthropic_tools: list[dict[str, Any]] | None = None,
        *,
        cache_editing: bool = False,
        new_cache_deletions: tuple[str, ...] = (),
        pinned_cache_edits: tuple[tuple[int, str, dict[str, Any]], ...] = (),
        skip_cache_write: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[int, str, dict[str, Any]] | None]:
        """Add cache_control breakpoints to messages and tools.

        Mirrors cc's ``addCacheBreakpoints``:
        1. Exactly one message-level cache_control marker on the last message.
        2. One cache_control marker on the last tool definition.
        3. tool_result blocks strictly before the last cache_control marker get
           ``cache_reference: tool_use_id`` to improve cache-hit tracking.
        Combined with the system-block breakpoint from
        ``_build_system_blocks``, this gives Anthropic three cache segments:
          1. Stable system prefix (identity, rules, contracts)
          2. Tool definitions (stable across turns)
          3. Conversation history prefix (grows turn-by-turn)
        """
        cache_control = _cache_control()
        def message_anchor(message: dict[str, Any]) -> str:
            raw = json.dumps(
                message,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()

        message_anchors = [message_anchor(message) for message in api_messages]
        messages = [dict(msg) for msg in api_messages]
        marker_index = len(messages) - 2 if skip_cache_write else len(messages) - 1
        if 0 <= marker_index < len(messages):
            marker = messages[marker_index]
            content = marker.get("content")
            if isinstance(content, list) and content:
                blocks = [dict(block) for block in content]
                blocks[-1]["cache_control"] = dict(cache_control)
                marker["content"] = blocks
            elif isinstance(content, str):
                marker["content"] = [
                    {"type": "text", "text": content, "cache_control": dict(cache_control)}
                ]

            # Add cache_reference to tool_result blocks strictly before the
            # last cache_control marker (cc claude.ts addCacheBreakpoints:
            # "The API requires cache_reference to appear before or on the
            # last cache_control — we use strict before"). Opt-in: cc only
            # sends this under the cache-editing beta (useCachedMC), not on
            # the public Messages API. Only user messages carry tool_results.
            if cache_editing:
                for msg in messages[:marker_index]:
                    if msg.get("role") != "user":
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    new_blocks: list[Any] = []
                    changed = False
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and block.get("tool_use_id")
                            and "cache_reference" not in block
                        ):
                            new_block = dict(block)
                            new_block["cache_reference"] = block["tool_use_id"]
                            new_blocks.append(new_block)
                            changed = True
                        else:
                            new_blocks.append(block)
                    if changed:
                        msg["content"] = new_blocks

        new_pin: tuple[int, str, dict[str, Any]] | None = None
        if cache_editing:
            seen_references: set[str] = set()

            def insert_edits(message_index: int, block: dict[str, Any]) -> None:
                message = messages[message_index]
                if message.get("role") != "user":
                    return
                content = message.get("content")
                if not isinstance(content, list):
                    content = [{"type": "text", "text": str(content or "")}]
                edits = []
                for edit in block.get("edits", []):
                    reference = str(edit.get("cache_reference") or "") if isinstance(edit, dict) else ""
                    if reference and reference not in seen_references:
                        edits.append({"type": "delete", "cache_reference": reference})
                        seen_references.add(reference)
                if not edits:
                    return
                insert_at = 0
                while insert_at < len(content) and isinstance(content[insert_at], dict) and content[insert_at].get("type") == "tool_result":
                    insert_at += 1
                next_content = [dict(item) if isinstance(item, dict) else item for item in content]
                next_content.insert(insert_at, {"type": "cache_edits", "edits": edits})
                message["content"] = next_content

            for message_index, anchor, block in pinned_cache_edits:
                resolved_index = int(message_index)
                if not (
                    0 <= resolved_index < len(messages)
                    and message_anchors[resolved_index] == anchor
                ):
                    resolved_index = next(
                        (index for index, candidate in enumerate(message_anchors) if candidate == anchor),
                        -1,
                    )
                if resolved_index >= 0 and isinstance(block, dict):
                    insert_edits(resolved_index, block)

            if new_cache_deletions:
                block = {
                    "type": "cache_edits",
                    "edits": [
                        {"type": "delete", "cache_reference": call_id}
                        for call_id in new_cache_deletions
                    ],
                }
                for message_index in range(len(messages) - 1, -1, -1):
                    if messages[message_index].get("role") == "user":
                        insert_edits(message_index, block)
                        new_pin = (message_index, message_anchors[message_index], block)
                        break

        tools = list(anthropic_tools or [])
        if tools:
            tools[-1] = dict(tools[-1])
            tools[-1]["cache_control"] = dict(cache_control)

        return messages, tools, new_pin

    @staticmethod
    def _strip_all_cache_control(
        kwargs: dict[str, Any],
        *,
        cache_editing_beta_header: str = "",
    ) -> dict[str, Any]:
        """Remove all cache_control and cache_reference markers for gateway fallback.

        Strips cache_control from system blocks, tool definitions, and
        message content blocks, and cache_reference from tool_result blocks.
        Used when a gateway rejects cache_control.
        """
        cleaned = dict(kwargs)
        cleaned.pop("cache_control", None)
        cleaned.pop("cache_edits", None)

        # Drop the extended-cache-ttl beta header along with the markers —
        # sending it without any cache_control is pointless and some gateways
        # reject unknown beta headers.
        extra_headers = cleaned.get("extra_headers")
        if isinstance(extra_headers, dict) and extra_headers.get("anthropic-beta"):
            extra_headers = dict(extra_headers)
            beta_values = [
                value.strip()
                for value in str(extra_headers.get("anthropic-beta") or "").split(",")
                if value.strip()
            ]
            removed = {
                _EXTENDED_CACHE_TTL_BETA_HEADER,
                str(cache_editing_beta_header or "").strip(),
            }
            remaining = [value for value in beta_values if value not in removed]
            if remaining:
                extra_headers["anthropic-beta"] = ",".join(remaining)
            else:
                extra_headers.pop("anthropic-beta", None)
            if extra_headers:
                cleaned["extra_headers"] = extra_headers
            else:
                cleaned.pop("extra_headers", None)

        _strip_keys = {"cache_control", "cache_reference"}

        system = cleaned.get("system")
        if isinstance(system, list):
            cleaned["system"] = [
                {k: v for k, v in dict(block).items() if k not in _strip_keys}
                if isinstance(block, dict)
                else block
                for block in system
            ]

        tools_list = cleaned.get("tools")
        if isinstance(tools_list, list):
            cleaned["tools"] = [
                {k: v for k, v in dict(tool).items() if k not in _strip_keys}
                if isinstance(tool, dict)
                else tool
                for tool in tools_list
            ]

        messages_list = cleaned.get("messages")
        if isinstance(messages_list, list):
            new_messages: list[dict[str, Any]] = []
            for msg in messages_list:
                if not isinstance(msg, dict):
                    new_messages.append(msg)
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    new_msg = dict(msg)
                    new_msg["content"] = [
                        {k: v for k, v in dict(block).items() if k not in _strip_keys}
                        if isinstance(block, dict)
                        else block
                        for block in content
                        if not (isinstance(block, dict) and block.get("type") == "cache_edits")
                    ]
                    new_messages.append(new_msg)
                else:
                    new_messages.append(msg)
            cleaned["messages"] = new_messages

        return cleaned

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
            if msg.role in {"system", "developer"}:
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
                for item in msg.provider_items:
                    item_type = str(item.get("type") or "")
                    if item_type in {"thinking", "redacted_thinking"}:
                        content_parts.append(dict(item))
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
                    "is_error": bool(getattr(msg, "is_error", False)),
                    "images": list(getattr(msg, "images", []) or []),
                })

        # 第二遍：保证 user/assistant 交替 + 合并连续 tool_result
        api_messages: list[dict[str, Any]] = []
        i = 0

        while i < len(raw_messages):
            msg = raw_messages[i]

            if msg["role"] == "tool_result":
                # 合并连续的 tool_result 为一条 user 消息
                def _tool_result_block(m: dict[str, Any]) -> dict[str, Any]:
                    # Render inline images (cc FileReadTool image block) as
                    # Anthropic image blocks inside the tool_result content; fall
                    # back to a plain string when there are none.
                    images = m.get("images") or []
                    if images:
                        blocks: list[dict[str, Any]] = []
                        for img in images:
                            if img.get("data"):
                                blocks.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": img.get("media_type") or "image/png",
                                        "data": img["data"],
                                    },
                                })
                        if m["content"]:
                            blocks.append({"type": "text", "text": m["content"]})
                        content: Any = blocks or m["content"]
                    else:
                        content = m["content"]
                    block = {
                        "type": "tool_result",
                        "tool_use_id": m["tool_use_id"],
                        "content": content,
                    }
                    # Anthropic is_error (cc messages.ts): mark failed tool results.
                    if m.get("is_error"):
                        block["is_error"] = True
                    return block

                tool_results: list[dict[str, Any]] = [_tool_result_block(msg)]
                j = i + 1
                while j < len(raw_messages) and raw_messages[j]["role"] == "tool_result":
                    tool_results.append(_tool_result_block(raw_messages[j]))
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
        """Return messages with stale Anthropic cache_control markers removed.

        The signature is kept for older tests/extensions, but MiniCode now relies
        on provider automatic prefix caching instead of sending vendor-specific
        cache_control markers.
        """
        _ = (recent_count, max_breakpoints, scan_all)

        cloned_messages: list[dict[str, Any]] = []
        for message in api_messages:
            cloned = dict(message)
            content = cloned.get("content")

            if isinstance(content, list):
                cloned["content"] = [
                    {key: value for key, value in dict(block).items() if key != "cache_control"}
                    for block in content
                ]

            cloned_messages.append(cloned)

        return cloned_messages

    def _should_enable_thinking(
        self,
        messages: list[LLMMessage],
        anthropic_tools: list[dict[str, Any]],
    ) -> bool:
        del messages, anthropic_tools
        return bool(self._thinking_budget and self._thinking_budget > 0)

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

    def _convert_tools_cached(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert tools with session-level schema caching.

        Mirrors current Claude Code's ``toolSchemaCache``: description drift is
        stable for one name/schema pair, while a genuine input-schema change
        receives a distinct cache entry.
        """
        result: list[dict[str, Any]] = []
        for tool in tools:
            func = tool.get("function", {})
            if not func:
                continue
            name = str(func.get("name", "")).strip()
            if not name:
                continue
            input_schema = func.get("parameters", {"type": "object", "properties": {}})
            cache_key = f"{name}:{_json_fingerprint(input_schema)}"
            cached = self._tool_schema_cache.get(cache_key)
            if cached is not None:
                result.append(cached)
                continue
            at: dict[str, Any] = {
                "name": name,
                "description": func.get("description", ""),
                "input_schema": input_schema,
            }
            self._tool_schema_cache[cache_key] = at
            result.append(at)
        return result

    def clear_tool_schema_cache(self) -> None:
        """Clear the session-scoped tool schema cache (on /clear, /compact)."""
        self._tool_schema_cache.clear()

