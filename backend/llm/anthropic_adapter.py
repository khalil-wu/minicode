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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from backend.agent.prompting import split_sys_prompt_prefix
from backend.agent.lifecycle_errors import LifecycleStaleError as ExtensionStaleError
from backend.llm.base import (
    emit_provider_lifecycle_request,
    LLMAdapter,
    LLMSideCallContext,
    LLMMessage,
    ProviderActivityEvent,
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
from backend.llm.provider_contracts import ReasoningPolicy
from backend.llm.capabilities import (
    ProviderCapabilities,
    capabilities_from_anthropic_adapter,
)
from backend.llm.errors import (
    classify_llm_error,
    llm_error_status_code,
    llm_error_raw,
    retry_after_seconds,
    sanitize_llm_error_message,
)
from backend.llm.openai_usage import _get_usage_cost_usd, _get_usage_field
from backend.llm.proxy_policy import (
    normalize_provider_proxy_mode,
    provider_httpx_proxy_kwargs,
)
from backend.llm.sse import SSEMalformedBudget, iter_sse_data
from backend.secret_redaction import redact_secrets
from backend.tools.catalog import canonicalize_tool_schemas
logger = logging.getLogger(__name__)

from backend.llm.anthropic_protocol import (
    _adaptive_thinking_effort,
    _anthropic_container_metadata,
    _anthropic_content_delta_protocol_code,
    _anthropic_declared_error_event,
    _anthropic_exception_error_event,
    _anthropic_provider_activity,
    _anthropic_provider_message_item,
    _anthropic_public_citations,
    _anthropic_refusal_metadata,
    _anthropic_replay_content,
    _anthropic_request_metadata,
    _anthropic_safe_request_summary_from_payload,
    _anthropic_stream_protocol_error,
    _anthropic_tool_protocol_error,
    _anthropic_usage_metadata,
    _anthropic_usage_value_or_existing,
    _anthropic_web_search_sources,
    _cache_control,
    _cache_ttl_1h_enabled,
    _detached_anthropic_content_block,
    _detached_anthropic_tool_input,
    _detached_anthropic_value,
    _is_adaptive_thinking_model,
    _ANTHROPIC_STREAM_CONTENT_TYPES,
    _CacheEditingState,
    _json_fingerprint,
    _strip_excess_anthropic_media,
)

_DELTA_DEBOUNCE_BYTES = 128

# Anthropic 1h prompt-cache TTL requires this beta header.
# https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching#1-hour-cache-duration
_EXTENDED_CACHE_TTL_BETA_HEADER = "extended-cache-ttl-2025-04-11"


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
        small_fast_model: str = "",
        base_url: str | None = None,
        max_tokens: int | float = 8_000,
        context_window: int | float = 0,
        thinking_budget: int | None = None,
        use_auth_token: bool = False,
        cache_editing_beta_header: str = "",
        default_headers: Mapping[str, str] | None = None,
        provider_id: str = "anthropic",
        proxy_mode: str = "inherit",
    ) -> None:
        self._api_key = api_key
        self._provider_id = str(provider_id or "anthropic").strip() or "anthropic"
        self._proxy_mode = normalize_provider_proxy_mode(proxy_mode)
        self._model = model
        self._small_fast_model = str(small_fast_model or "").strip()
        self._base_url = base_url
        self._max_tokens = max(1, max_tokens or 8_000)
        self._context_window = context_window
        self._thinking_budget = thinking_budget
        self._configured_thinking_budget = thinking_budget
        self._use_auth_token = use_auth_token
        self._default_headers = {
            str(key): str(value)
            for key, value in dict(default_headers or {}).items()
            if str(key).strip()
        }
        # Cache editing is enabled only when the caller supplies the exact
        # provider-declared beta header; MiniCode never guesses it.
        self._cache_editing_beta_header = str(cache_editing_beta_header or "").strip()
        self._cache_editing_states: dict[str, _CacheEditingState] = {}
        # Latch 1h-cache eligibility per conversation so configuration reloads
        # cannot change cache-control bytes in the middle of a session.
        self._cache_ttl_1h_latches: dict[str, bool] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._tool_schema_cache = {}

    def supports_hosted_web_search(self) -> bool:
        # Hosted search is part of the Anthropic provider contract. A custom
        # Messages endpoint does not inherit it from a matching JSON shape.
        return self._provider_id == "anthropic"

    def hosted_web_search_supports_blocked_domains(self) -> bool:
        return self.supports_hosted_web_search()

    def supported_reasoning_efforts(self) -> tuple[str, ...]:
        return ("off", "high")

    def apply_reasoning_policy(self, policy: ReasoningPolicy) -> None:
        super().apply_reasoning_policy(policy)
        self._thinking_budget = (
            self._configured_thinking_budget
            if policy.level not in {"", "off"}
            else None
        )

    async def aclose(self) -> None:
        self._cache_editing_states.clear()
        self._cache_ttl_1h_latches.clear()
        http_client = self._http_client
        self._http_client = None
        if http_client is not None:
            await http_client.aclose()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return capabilities_from_anthropic_adapter(self)

    @staticmethod
    def _cache_editing_scope_key(
        metadata: dict[str, Any] | None = None,
        *,
        conversation_id: str = "",
    ) -> str:
        clean = sanitize_llm_request_metadata(metadata)
        key = str(
            conversation_id
            or clean.get("conversation_id")
            or clean.get("minicode_session_id")
            or clean.get("session_id")
            or "adapter-default"
        ).strip()
        return key or "adapter-default"

    def _cache_editing_state(
        self,
        metadata: dict[str, Any] | None = None,
        *,
        conversation_id: str = "",
    ) -> _CacheEditingState:
        key = self._cache_editing_scope_key(
            metadata,
            conversation_id=conversation_id,
        )
        state = self._cache_editing_states.get(key)
        if state is None:
            state = _CacheEditingState()
            self._cache_editing_states[key] = state
        return state

    def _cache_ttl_1h_for_metadata(self, metadata: dict[str, Any] | None) -> bool:
        key = self._cache_editing_scope_key(metadata)
        latched = self._cache_ttl_1h_latches.get(key)
        if latched is None:
            latched = _cache_ttl_1h_enabled()
            self._cache_ttl_1h_latches[key] = latched
        return latched

    def queue_cache_deletions(
        self,
        tool_call_ids: list[str] | tuple[str, ...],
        *,
        conversation_id: str = "",
    ) -> bool:
        """Queue provider-native deletions when the configured provider supports them."""
        if not self._cache_editing_beta_header:
            return False
        state = self._cache_editing_state(conversation_id=conversation_id)
        if state.disabled_reason:
            return False
        known = set(state.pending_deletions)
        known.update(
            str(edit.get("cache_reference") or "")
            for _, _, block in state.pinned_edits
            for edit in block.get("edits", [])
            if isinstance(edit, dict)
        )
        for raw_id in tool_call_ids:
            call_id = str(raw_id or "").strip()
            if call_id and call_id not in known:
                state.pending_deletions.append(call_id)
                known.add(call_id)
        return True

    def reset_prompt_cache_editing(self, *, conversation_id: str = "") -> None:
        """Drop cache-edit pins after a local cold-cache prefix rewrite."""

        key = self._cache_editing_scope_key(conversation_id=conversation_id)
        self._cache_editing_states.pop(key, None)
        self._cache_ttl_1h_latches.pop(key, None)

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        async for event in self._stream_chat_with_context(
            messages,
            tools=tools,
            metadata=metadata,
            context=None,
        ):
            yield event

    async def _stream_chat_with_context(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        context: LLMSideCallContext | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 Claude Messages API。"""
        # 分离 system prompt + 消息交替保证
        system_text, api_messages = self._convert_messages(messages)
        side_options = context.options if context is not None else None
        prompt_cache_enabled = (
            side_options is None or side_options.enable_prompt_cache
        )
        model = (
            self.small_fast_model_id()
            if side_options is not None and side_options.use_small_fast_model
            else self._model
        )
        requested_max_tokens = (
            max_tokens
            if max_tokens is not None
            else side_options.max_tokens
            if side_options is not None
            else None
        )
        max_output_tokens = (
            min(self._max_tokens, max(1, requested_max_tokens))
            if requested_max_tokens is not None
            else self._max_tokens
        )
        max_output_tokens = clamp_max_tokens_to_context(
            context_window=self._context_window,
            messages=messages,
            tools=tools,
            max_tokens=max_output_tokens,
        )

        anthropic_tools = self._convert_tools_cached(tools or []) if tools else []
        cache_editing_state = (
            self._cache_editing_state(metadata)
            if prompt_cache_enabled and self._cache_editing_beta_header
            else _CacheEditingState()
        )
        cache_ttl_1h = (
            self._cache_ttl_1h_for_metadata(metadata)
            if prompt_cache_enabled
            else False
        )

        # Add cache-control breakpoints only for requests that explicitly keep
        # prompt caching enabled.
        pending_cache_deletions = (
            tuple(cache_editing_state.pending_deletions)
            if prompt_cache_enabled
            else ()
        )
        cache_editing_enabled = bool(
            prompt_cache_enabled
            and self._cache_editing_beta_header
            and not cache_editing_state.disabled_reason
        )
        if prompt_cache_enabled:
            cached_messages, cached_tools, new_cache_edit_pin = (
                self._add_cache_breakpoints(
                    api_messages,
                    anthropic_tools if tools else None,
                    cache_editing=cache_editing_enabled,
                    new_cache_deletions=pending_cache_deletions,
                    pinned_cache_edits=tuple(cache_editing_state.pinned_edits),
                    skip_cache_write=bool(
                        (metadata or {}).get("prompt_cache_skip_write")
                    ),
                    ttl_1h=cache_ttl_1h,
                )
            )
        else:
            cached_messages = api_messages
            cached_tools = anthropic_tools if tools else None
            new_cache_edit_pin = None

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": cached_messages,
            "stream": True,
        }
        request_metadata = _anthropic_request_metadata(metadata)
        if request_metadata:
            kwargs["metadata"] = request_metadata

        # 1h cache TTL requires the provider's extended-cache beta header.
        if prompt_cache_enabled and cache_ttl_1h:
            kwargs["extra_headers"] = {
                "anthropic-beta": _EXTENDED_CACHE_TTL_BETA_HEADER
            }
        if cache_editing_enabled:
            extra_headers = dict(kwargs.get("extra_headers") or {})
            existing_beta = str(extra_headers.get("anthropic-beta") or "").strip()
            beta_values = [
                value
                for value in (existing_beta, self._cache_editing_beta_header)
                if value
            ]
            extra_headers["anthropic-beta"] = ",".join(dict.fromkeys(beta_values))
            kwargs["extra_headers"] = extra_headers

        # System prompt: split stable/dynamic blocks with cache_control on
        # the stable prefix so Anthropic caches it across turns.
        if system_text:
            kwargs["system"] = (
                    self._build_system_blocks(system_text, ttl_1h=cache_ttl_1h)
                if prompt_cache_enabled
                else system_text
            )

        # Extended thinking（Claude 4+）
        if not (
            side_options is not None and side_options.disable_reasoning
        ) and max_output_tokens > 1 and self._should_enable_thinking(
            messages, anthropic_tools
        ):
            # Anthropic rejects budget_tokens >= max_tokens. Keep the requested
            # budget inside the wire contract instead of retrying a modified
            # request after rejection. Newer model families use adaptive
            # thinking plus output_config.effort instead of a token budget.
            # Capability decisions must follow the model actually placed on
            # the wire. Side queries may select small_fast_model, which can be
            # a different Claude generation from the primary model.
            model_id = str(model or "").lower()
            if _is_adaptive_thinking_model(model_id):
                kwargs["thinking"] = {"type": "adaptive"}
                effort = _adaptive_thinking_effort(self._thinking_budget)
                if effort:
                    kwargs["output_config"] = {"effort": effort}
            else:
                budget_tokens = min(self._thinking_budget, max_output_tokens - 1)
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget_tokens}
            if anthropic_tools:
                # Interleaved thinking is required for tool-use turns with
                # thinking enabled (cc betas.ts INTERLEAVED_THINKING_BETA_HEADER).
                extra_headers = dict(kwargs.get("extra_headers") or {})
                existing_beta = str(extra_headers.get("anthropic-beta") or "").strip()
                beta_values = [
                    value
                    for value in (existing_beta, "interleaved-thinking-2025-05-14")
                    if value
                ]
                extra_headers["anthropic-beta"] = ",".join(dict.fromkeys(beta_values))
                kwargs["extra_headers"] = extra_headers

        if tools and cached_tools:
            kwargs["tools"] = cached_tools
            kwargs["tool_choice"] = {"type": "auto"}

        if side_options is not None and side_options.output_schema is not None:
            output_config = dict(kwargs.get("output_config") or {})
            output_config["format"] = {
                "type": "json_schema",
                "schema": side_options.output_schema,
            }
            kwargs["output_config"] = output_config
            extra_headers = dict(kwargs.get("extra_headers") or {})
            existing_beta = str(extra_headers.get("anthropic-beta") or "").strip()
            beta_values = [
                value
                for value in (
                    existing_beta,
                    "structured-outputs-2025-12-15",
                )
                if value
            ]
            extra_headers["anthropic-beta"] = ",".join(
                dict.fromkeys(beta_values)
            )
            kwargs["extra_headers"] = extra_headers

        if side_options is not None and side_options.hosted_web_search:
            if not self.supports_hosted_web_search():
                raise RuntimeError(
                    "Hosted web search is unavailable for this Anthropic-compatible provider"
                )
            if (
                side_options.web_search_allowed_domains
                and side_options.web_search_blocked_domains
            ):
                raise ValueError("Cannot combine allowed_domains and blocked_domains")
            hosted_tool: dict[str, Any] = {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
            }
            if side_options.web_search_allowed_domains:
                hosted_tool["allowed_domains"] = list(
                    side_options.web_search_allowed_domains
                )
            if side_options.web_search_blocked_domains:
                hosted_tool["blocked_domains"] = list(
                    side_options.web_search_blocked_domains
                )
            request_tools = list(kwargs.get("tools") or [])
            request_tools.append(hosted_tool)
            kwargs["tools"] = request_tools

        request_summary = _anthropic_safe_request_summary_from_payload(
            kwargs,
            metadata,
        )

        try:
            async for event in self._stream_messages(
                kwargs, request_summary=request_summary, metadata=metadata
            ):
                if event.type == StreamEventType.DONE:
                    self._commit_cache_edit_request(
                        cache_editing_state,
                        pending_cache_deletions,
                        new_cache_edit_pin,
                    )
                yield event
        except ExtensionStaleError:
            raise
        except Exception as exc:
            logger.error("Anthropic Messages transport failed: %s", exc)
            yield _anthropic_exception_error_event(
                exc,
                provider=self._provider_id,
            )
        return

    def _messages_url(self) -> str:
        endpoint = (self._base_url or "https://api.anthropic.com/v1").rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint = f"{endpoint}/v1"
        return f"{endpoint}/messages"

    def _request_headers(self) -> dict[str, str]:
        headers = self._transport_headers()
        headers.update(self._default_headers)
        return headers

    def _transport_headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self._use_auth_token and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            # Provider request liveness is owned by the turn/stream policy;
            # this transport must not introduce a second idle timeout.
            self._http_client = httpx.AsyncClient(
                timeout=None,
                follow_redirects=False,
                **provider_httpx_proxy_kwargs(
                    self._messages_url(),
                    proxy_mode=self._proxy_mode,
                ),
            )
        return self._http_client

    async def _prepare_message_request(
        self,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        transformed_payload = await emit_provider_lifecycle_request(
            metadata,
            dict(payload),
        )
        wire_payload = dict(transformed_payload)
        body = dict(wire_payload)
        extra_headers = body.pop("extra_headers", None)
        headers: dict[str, Any] = dict(self._request_headers())
        if isinstance(extra_headers, dict):
            headers.update(
                {
                    str(key): value
                    for key, value in extra_headers.items()
                }
            )
        headers = await emit_provider_lifecycle_headers(metadata, headers)
        return wire_payload, body, headers

    async def _stream_messages(
        self,
        kwargs: dict[str, Any],
        *,
        request_summary: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the MiniCode Anthropic Messages wire protocol."""
        pending_tool_calls: list[ToolCallEvent] = []
        current_tool_id = ""
        current_tool_name = ""
        current_tool_args = ""
        current_tool_initial_input: dict[str, Any] | None = None
        _delta_bytes_since_emit = 0
        usage = UsageInfo()
        provider_usage_metadata: dict[str, Any] = {}
        stop_reason = ""
        saw_message_start = False
        saw_message_delta = False
        saw_message_stop = False
        seen_content_indices: set[int] = set()
        seen_tool_ids: set[str] = set()
        provider_content_blocks: list[dict[str, Any]] = []
        current_provider_block: dict[str, Any] | None = None
        current_provider_input_json = ""
        current_reasoning_item: dict[str, Any] | None = None
        current_message_id = ""
        current_content_index: int | None = None
        current_content_kind = ""
        current_content_item_id = ""
        search_sources: list[tuple[str, str]] = []
        public_citations: list[dict[str, Any]] = []
        refusal_metadata: dict[str, str] = {}
        container_metadata: dict[str, str] = {}

        try:
            # extra_headers belongs to the request envelope, not the JSON body.
            wire_payload, body, headers = await self._prepare_message_request(
                kwargs,
                metadata=metadata,
            )
            request_summary = _anthropic_safe_request_summary_from_payload(
                wire_payload,
                metadata,
            )
            async with self._get_http_client().stream(
                "POST",
                self._messages_url(),
                headers=headers,
                json=body,
            ) as response:
                if response is not None:
                    await emit_provider_lifecycle_response(
                        metadata,
                        int(getattr(response, "status_code", 200) or 200),
                        getattr(response, "headers", {}),
                    )
                    if int(getattr(response, "status_code", 200) or 200) >= 400:
                        await response.aread()
                        response.raise_for_status()
                    malformed_budget = SSEMalformedBudget()
                    async for raw_payload in iter_sse_data(response):
                        payload_text = raw_payload.strip()
                        if not payload_text:
                            continue
                        if payload_text == "[DONE]":
                            if saw_message_stop:
                                break
                            continue
                        try:
                            event = json.loads(payload_text)
                        except json.JSONDecodeError:
                            malformed_budget.reject(payload_text)
                            continue
                        malformed_budget.accept()

                        if not isinstance(event, dict):
                            yield _anthropic_stream_protocol_error(
                                "invalid_event_envelope",
                                event_type="invalid",
                                provider=self._provider_id,
                            )
                            return

                        event_type = str(event.get("type") or "")
                        if event_type == "ping":
                            continue
                        if saw_message_stop:
                            yield _anthropic_stream_protocol_error(
                                "late_event_after_message_stop",
                                event_type=event_type,
                                provider=self._provider_id,
                            )
                            return
                        if event_type == "error":
                            yield _anthropic_declared_error_event(
                                event,
                                provider=self._provider_id,
                            )
                            return
                        if event_type != "message_start" and not saw_message_start:
                            yield _anthropic_stream_protocol_error(
                                "event_before_message_start",
                                event_type=event_type,
                                provider=self._provider_id,
                            )
                            return
                        if saw_message_delta and event_type in {
                            "content_block_start",
                            "content_block_delta",
                            "content_block_stop",
                        }:
                            yield _anthropic_stream_protocol_error(
                                "content_event_after_message_delta",
                                event_type=event_type,
                                provider=self._provider_id,
                            )
                            return

                        if event_type == "message_start":
                            if saw_message_start:
                                yield _anthropic_stream_protocol_error(
                                    "duplicate_message_start",
                                    event_type=event_type,
                                    provider=self._provider_id,
                                )
                                return
                            saw_message_start = True
                            message = (
                                event.get("message")
                                if isinstance(event.get("message"), dict)
                                else {}
                            )
                            current_message_id = (
                                str(message.get("id") or "")
                                if isinstance(message, dict)
                                else ""
                            )
                            if isinstance(message, dict):
                                refusal_metadata.update(
                                    _anthropic_refusal_metadata(
                                        message.get("stop_details")
                                    )
                                )
                                container_metadata.update(
                                    _anthropic_container_metadata(
                                        message.get("container")
                                    )
                                )
                            usage_obj = (
                                message.get("usage")
                                if isinstance(message, dict)
                                else {}
                            )
                            if isinstance(usage_obj, dict):
                                provider_usage_metadata.update(
                                    _anthropic_usage_metadata(usage_obj)
                                )
                                usage = UsageInfo(
                                    input_tokens=_get_usage_field(
                                        usage_obj, "input_tokens"
                                    ),
                                    output_tokens=_get_usage_field(
                                        usage_obj, "output_tokens"
                                    ),
                                    cache_creation_input_tokens=_get_usage_field(
                                        usage_obj, "cache_creation_input_tokens"
                                    ),
                                    cache_read_input_tokens=_get_usage_field(
                                        usage_obj, "cache_read_input_tokens"
                                    ),
                                    cache_deleted_input_tokens=_get_usage_field(
                                        usage_obj, "cache_deleted_input_tokens"
                                    ),
                                    # Anthropic reports cache reads separately from input_tokens.
                                    input_includes_cache_read=False,
                                    input_includes_cache_write=False,
                                    cost_usd=_get_usage_cost_usd(usage_obj),
                                )
                            stop_reason = (
                                str(message.get("stop_reason") or "")
                                if isinstance(message, dict)
                                else ""
                            )

                        elif event_type == "content_block_start":
                            raw_received_index = event.get("index")
                            received_index = (
                                raw_received_index
                                if isinstance(raw_received_index, int)
                                and not isinstance(raw_received_index, bool)
                                and raw_received_index >= 0
                                else None
                            )
                            if current_content_kind:
                                yield _anthropic_stream_protocol_error(
                                    "nested_content_block_start",
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    received_index=received_index,
                                    current_kind=current_content_kind,
                                    provider=self._provider_id,
                                )
                                return
                            if received_index is None:
                                yield _anthropic_stream_protocol_error(
                                    "invalid_content_index",
                                    event_type=event_type,
                                    provider=self._provider_id,
                                )
                                return
                            if received_index in seen_content_indices:
                                yield _anthropic_stream_protocol_error(
                                    "duplicate_content_index",
                                    event_type=event_type,
                                    received_index=received_index,
                                    provider=self._provider_id,
                                )
                                return
                            seen_content_indices.add(received_index)
                            block = (
                                event.get("content_block")
                                if isinstance(event.get("content_block"), dict)
                                else {}
                            )
                            block_type = str(block.get("type") or "")
                            if block_type not in _ANTHROPIC_STREAM_CONTENT_TYPES:
                                yield _anthropic_stream_protocol_error(
                                    "unknown_content_block",
                                    event_type=event_type,
                                    received_index=received_index,
                                    current_kind=block_type,
                                    provider=self._provider_id,
                                )
                                return
                            current_content_index = received_index
                            current_content_kind = str(block.get("type") or "")
                            current_provider_block = (
                                _detached_anthropic_content_block(block)
                            )
                            current_provider_input_json = ""
                            index_label = (
                                current_content_index
                                if current_content_index is not None
                                else "unknown"
                            )
                            current_content_item_id = f"{current_message_id or 'anthropic'}:content:{index_label}"
                            if block.get("type") == "tool_use":
                                current_tool_id = str(block.get("id") or "")
                                current_tool_name = str(block.get("name") or "")
                                current_tool_id = current_tool_id.strip()
                                current_tool_name = current_tool_name.strip()
                                if not current_tool_id:
                                    yield _anthropic_stream_protocol_error(
                                        "missing_tool_call_id",
                                        event_type=event_type,
                                        received_index=received_index,
                                        provider=self._provider_id,
                                    )
                                    return
                                if not current_tool_name:
                                    yield _anthropic_stream_protocol_error(
                                        "missing_tool_name",
                                        event_type=event_type,
                                        received_index=received_index,
                                        provider=self._provider_id,
                                    )
                                    return
                                if current_tool_id in seen_tool_ids:
                                    yield _anthropic_stream_protocol_error(
                                        "duplicate_tool_call_id",
                                        event_type=event_type,
                                        received_index=received_index,
                                        provider=self._provider_id,
                                    )
                                    return
                                seen_tool_ids.add(current_tool_id)
                                current_tool_args = ""
                                current_tool_initial_input = (
                                    _detached_anthropic_tool_input(block.get("input"))
                                )
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
                                current_reasoning_item = current_provider_block or {
                                    key: value
                                    for key, value in block.items()
                                    if key in {
                                        "type",
                                        "thinking",
                                        "signature",
                                        "data",
                                    }
                                }
                                current_provider_block = current_reasoning_item
                                yield StreamEvent(
                                    type=StreamEventType.THINKING_CHUNK,
                                    content=(
                                        str(block.get("thinking") or "")
                                        if block.get("type") == "thinking"
                                        else ""
                                    ),
                                    item_id=current_content_item_id,
                                    content_index=current_content_index,
                                    content_kind="thinking",
                                    lifecycle="start",
                                )
                            elif block.get("type") == "text":
                                yield StreamEvent(
                                    type=StreamEventType.TEXT_CHUNK,
                                    content=str(block.get("text") or ""),
                                    item_id=current_content_item_id,
                                    content_index=current_content_index,
                                    content_kind="text",
                                    lifecycle="start",
                                )
                            elif block.get("type") == "web_search_tool_result":
                                for source in _anthropic_web_search_sources(block):
                                    if source not in search_sources:
                                        search_sources.append(source)
                            for citation in _anthropic_public_citations(
                                block.get("citations")
                            ):
                                if citation not in public_citations:
                                    public_citations.append(citation)
                            activity = _anthropic_provider_activity(
                                block,
                                activity_id=current_content_item_id,
                            )
                            if activity is not None:
                                yield StreamEvent(
                                    type=StreamEventType.PROVIDER_ACTIVITY,
                                    provider_activity=activity,
                                )

                        elif event_type == "content_block_delta":
                            raw_received_index = event.get("index")
                            received_index = (
                                raw_received_index
                                if isinstance(raw_received_index, int)
                                and not isinstance(raw_received_index, bool)
                                and raw_received_index >= 0
                                else None
                            )
                            if received_index is None:
                                yield _anthropic_stream_protocol_error(
                                    "invalid_content_index",
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    current_kind=current_content_kind,
                                    provider=self._provider_id,
                                )
                                return
                            if not current_content_kind:
                                yield _anthropic_stream_protocol_error(
                                    "content_delta_without_start",
                                    event_type=event_type,
                                    received_index=received_index,
                                    provider=self._provider_id,
                                )
                                return
                            if (
                                current_content_index is not None
                                and received_index is not None
                                and received_index != current_content_index
                            ):
                                yield _anthropic_stream_protocol_error(
                                    "content_index_mismatch",
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    received_index=received_index,
                                    current_kind=current_content_kind,
                                    provider=self._provider_id,
                                )
                                return
                            delta = (
                                event.get("delta")
                                if isinstance(event.get("delta"), dict)
                                else {}
                            )
                            delta_type = str(delta.get("type") or "")
                            delta_protocol_code = (
                                _anthropic_content_delta_protocol_code(
                                    current_content_kind,
                                    delta_type,
                                )
                            )
                            if delta_protocol_code:
                                yield _anthropic_stream_protocol_error(
                                    delta_protocol_code,
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    received_index=received_index,
                                    current_kind=current_content_kind,
                                    delta_type=delta_type,
                                    provider=self._provider_id,
                                )
                                return
                            if delta_type == "text_delta":
                                text = str(delta.get("text") or "")
                                if text:
                                    if current_provider_block is not None:
                                        current_provider_block["text"] = str(
                                            current_provider_block.get("text") or ""
                                        ) + text
                                    yield StreamEvent(
                                        type=StreamEventType.TEXT_CHUNK,
                                        content=text,
                                        item_id=current_content_item_id,
                                        content_index=current_content_index,
                                        content_kind="text",
                                    )
                            elif delta_type in {"thinking_delta", "signature_delta"}:
                                thinking = str(
                                    delta.get("thinking") or delta.get("text") or ""
                                )
                                signature = str(delta.get("signature") or "")
                                if current_reasoning_item is not None:
                                    if thinking:
                                        current_reasoning_item["thinking"] = (
                                            str(
                                                current_reasoning_item.get("thinking")
                                                or ""
                                            )
                                            + thinking
                                        )
                                    if signature:
                                        current_reasoning_item["signature"] = (
                                            str(
                                                current_reasoning_item.get("signature")
                                                or ""
                                            )
                                            + signature
                                        )
                                if thinking:
                                    yield StreamEvent(
                                        type=StreamEventType.THINKING_CHUNK,
                                        content=thinking,
                                        raw={"provider_reasoning_type": delta_type},
                                        item_id=current_content_item_id,
                                        content_index=current_content_index,
                                        content_kind="thinking",
                                    )
                            elif delta_type == "input_json_delta":
                                partial = str(delta.get("partial_json") or "")
                                current_provider_input_json += partial
                                if current_tool_id:
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
                            elif delta_type == "citations_delta":
                                citation = _detached_anthropic_value(
                                    delta.get("citation")
                                )
                                if current_provider_block is not None and isinstance(
                                    citation, dict
                                ):
                                    citations = current_provider_block.setdefault(
                                        "citations", []
                                    )
                                    if isinstance(citations, list):
                                        citations.append(citation)
                                for public_citation in _anthropic_public_citations(
                                    delta.get("citation")
                                ):
                                    if public_citation not in public_citations:
                                        public_citations.append(public_citation)
                            elif delta_type == "compaction_delta":
                                if current_provider_block is not None:
                                    for field_name in (
                                        "content",
                                        "encrypted_content",
                                    ):
                                        fragment = delta.get(field_name)
                                        if isinstance(fragment, str) and fragment:
                                            current_provider_block[field_name] = str(
                                                current_provider_block.get(
                                                    field_name
                                                )
                                                or ""
                                            ) + fragment

                        elif event_type == "content_block_stop":
                            raw_received_index = event.get("index")
                            received_index = (
                                raw_received_index
                                if isinstance(raw_received_index, int)
                                and not isinstance(raw_received_index, bool)
                                and raw_received_index >= 0
                                else None
                            )
                            if received_index is None:
                                yield _anthropic_stream_protocol_error(
                                    "invalid_content_index",
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    current_kind=current_content_kind,
                                    provider=self._provider_id,
                                )
                                return
                            if not current_content_kind:
                                yield _anthropic_stream_protocol_error(
                                    "content_stop_without_start",
                                    event_type=event_type,
                                    received_index=received_index,
                                    provider=self._provider_id,
                                )
                                return
                            if (
                                current_content_index is not None
                                and received_index is not None
                                and received_index != current_content_index
                            ):
                                yield _anthropic_stream_protocol_error(
                                    "content_index_mismatch",
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    received_index=received_index,
                                    current_kind=current_content_kind,
                                    provider=self._provider_id,
                                )
                                return
                            if current_content_kind in {
                                "text",
                                "thinking",
                                "redacted_thinking",
                            }:
                                yield StreamEvent(
                                    type=(
                                        StreamEventType.TEXT_CHUNK
                                        if current_content_kind == "text"
                                        else StreamEventType.THINKING_CHUNK
                                    ),
                                    item_id=current_content_item_id,
                                    content_index=current_content_index,
                                    content_kind=(
                                        "text"
                                        if current_content_kind == "text"
                                        else "thinking"
                                    ),
                                    lifecycle="end",
                                )
                            if current_tool_id and current_tool_name:
                                try:
                                    arguments = (
                                        json.loads(current_tool_args)
                                        if current_tool_args
                                        else dict(current_tool_initial_input or {})
                                    )
                                except (json.JSONDecodeError, TypeError):
                                    from backend.llm.json_repair import repair_tool_json

                                    arguments = repair_tool_json(current_tool_args) or {
                                        "_raw": current_tool_args
                                    }
                                    arguments_repaired = True
                                else:
                                    arguments_repaired = False
                                pending_tool_calls.append(
                                    ToolCallEvent(
                                        id=current_tool_id,
                                        name=current_tool_name,
                                        arguments=arguments,
                                        arguments_repaired=arguments_repaired,
                                    )
                                )
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL,
                                    tool_calls=[pending_tool_calls[-1]],
                                    tool_calls_final=False,
                                )
                                current_tool_id = ""
                                current_tool_name = ""
                                current_tool_args = ""
                                current_tool_initial_input = None
                            if current_provider_block is not None:
                                block_type = str(
                                    current_provider_block.get("type") or ""
                                )
                                if block_type in {
                                    "tool_use",
                                    "server_tool_use",
                                    "mcp_tool_use",
                                } and current_provider_input_json:
                                    try:
                                        provider_input = json.loads(
                                            current_provider_input_json
                                        )
                                    except (json.JSONDecodeError, TypeError):
                                        provider_input = None
                                    if isinstance(provider_input, dict):
                                        current_provider_block["input"] = provider_input
                                if (
                                    block_type in {"server_tool_use", "mcp_tool_use"}
                                    and current_provider_input_json
                                ):
                                    activity = _anthropic_provider_activity(
                                        current_provider_block,
                                        activity_id=current_content_item_id,
                                    )
                                    if activity is not None:
                                        yield StreamEvent(
                                            type=StreamEventType.PROVIDER_ACTIVITY,
                                            provider_activity=activity,
                                        )
                                if block_type == "compaction":
                                    activity = _anthropic_provider_activity(
                                        current_provider_block,
                                        activity_id=current_content_item_id,
                                        terminal=True,
                                    )
                                    if activity is not None:
                                        yield StreamEvent(
                                            type=StreamEventType.PROVIDER_ACTIVITY,
                                            provider_activity=activity,
                                        )
                                provider_content_blocks.append(
                                    current_provider_block
                                )
                            current_provider_block = None
                            current_provider_input_json = ""
                            current_reasoning_item = None
                            current_content_index = None
                            current_content_kind = ""
                            current_content_item_id = ""

                        elif event_type == "message_delta":
                            if current_content_kind:
                                yield _anthropic_stream_protocol_error(
                                    "message_delta_with_open_block",
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    current_kind=current_content_kind,
                                    provider=self._provider_id,
                                )
                                return
                            if saw_message_delta:
                                yield _anthropic_stream_protocol_error(
                                    "duplicate_message_delta",
                                    event_type=event_type,
                                    provider=self._provider_id,
                                )
                                return
                            saw_message_delta = True
                            delta = (
                                event.get("delta")
                                if isinstance(event.get("delta"), dict)
                                else {}
                            )
                            if isinstance(delta, dict) and delta.get("stop_reason"):
                                stop_reason = str(delta.get("stop_reason") or "")
                            if isinstance(delta, dict):
                                refusal_metadata.update(
                                    _anthropic_refusal_metadata(
                                        delta.get("stop_details")
                                    )
                                )
                                container_metadata.update(
                                    _anthropic_container_metadata(
                                        delta.get("container")
                                    )
                                )
                            usage_obj = (
                                event.get("usage")
                                if isinstance(event.get("usage"), dict)
                                else {}
                            )
                            if (
                                isinstance(usage_obj, dict)
                                and usage_obj.get("output_tokens") is not None
                            ):
                                provider_usage_metadata.update(
                                    _anthropic_usage_metadata(usage_obj)
                                )
                                usage = UsageInfo(
                                    input_tokens=_anthropic_usage_value_or_existing(
                                        usage_obj,
                                        "input_tokens",
                                        usage.input_tokens,
                                    ),
                                    output_tokens=_anthropic_usage_value_or_existing(
                                        usage_obj,
                                        "output_tokens",
                                        usage.output_tokens,
                                    ),
                                    cache_creation_input_tokens=(
                                        _anthropic_usage_value_or_existing(
                                            usage_obj,
                                            "cache_creation_input_tokens",
                                            usage.cache_creation_input_tokens,
                                        )
                                    ),
                                    cache_read_input_tokens=(
                                        _anthropic_usage_value_or_existing(
                                            usage_obj,
                                            "cache_read_input_tokens",
                                            usage.cache_read_input_tokens,
                                        )
                                    ),
                                    cache_deleted_input_tokens=(
                                        _anthropic_usage_value_or_existing(
                                            usage_obj,
                                            "cache_deleted_input_tokens",
                                            usage.cache_deleted_input_tokens,
                                        )
                                    ),
                                    input_includes_cache_read=False,
                                    input_includes_cache_write=False,
                                    cost_usd=max(
                                        usage.cost_usd,
                                        _get_usage_cost_usd(usage_obj),
                                    ),
                                )
                        elif event_type == "message_stop":
                            if current_content_kind:
                                yield _anthropic_stream_protocol_error(
                                    "message_stop_with_open_block",
                                    event_type=event_type,
                                    current_index=current_content_index,
                                    current_kind=current_content_kind,
                                    provider=self._provider_id,
                                )
                                return
                            if not saw_message_delta:
                                yield _anthropic_stream_protocol_error(
                                    "message_stop_without_delta",
                                    event_type=event_type,
                                    provider=self._provider_id,
                                )
                                return
                            saw_message_stop = True
                        else:
                            yield _anthropic_stream_protocol_error(
                                "unknown_stream_event",
                                event_type=event_type,
                                provider=self._provider_id,
                            )
                            return
        except ExtensionStaleError:
            raise
        except Exception as exc:
            logger.error("Anthropic Messages transport failed: %s", exc)
            yield _anthropic_exception_error_event(
                exc,
                provider=self._provider_id,
            )
            return

        if not saw_message_stop:
            logger.error("Anthropic Messages stream ended before message_stop")
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content="MiniCode Anthropic Messages 流式响应在完成前中断",
                raw={
                    "provider": self._provider_id,
                    "event_type": "eof_without_message_stop",
                    "provider_error_type": "network",
                    "error_type": "api",
                },
            )
            return

        protocol_error = _anthropic_tool_protocol_error(
            stop_reason,
            pending_tool_calls,
        )
        if protocol_error:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                content=protocol_error,
                raw={
                    "provider": self._provider_id,
                    "event_type": "tool_stop_reason_mismatch",
                    "stop_reason": stop_reason,
                    "tool_call_count": len(pending_tool_calls),
                    "provider_error_type": "protocol",
                    "error_type": "api",
                },
            )
            return

        if pending_tool_calls:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL, tool_calls=pending_tool_calls
            )
        if stop_reason == "max_tokens":
            logger.warning("Claude 响应因 max_tokens 截断")
        usage_metadata = dict(provider_usage_metadata)
        usage_metadata.update(
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "cache_deleted_input_tokens": usage.cache_deleted_input_tokens,
            }
        )
        done_raw: dict[str, Any] = {
            "provider": self._provider_id,
            "stop_reason": stop_reason,
            "usage": usage_metadata,
            "request_summary": request_summary or {},
            "search_sources": [
                {"title": title, "url": url}
                for title, url in search_sources
            ],
        }
        if public_citations:
            done_raw["citations"] = public_citations
        if refusal_metadata:
            done_raw["refusal"] = refusal_metadata
        if container_metadata:
            done_raw["container"] = container_metadata
        yield StreamEvent(
            type=StreamEventType.DONE,
            usage=usage,
            finish_reason=stop_reason,
            raw=done_raw,
            provider_items=_anthropic_provider_message_item(
                provider_content_blocks
            ),
        )

    async def simple_chat(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Consume the normal Messages stream and return its completed text."""
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
        model = (
            self.small_fast_model_id()
            if side_options is not None and side_options.use_small_fast_model
            else self._model
        )
        self.annotate_side_call(
            context,
            provider=self._provider_id,
            model_id=model,
        )
        text_parts: list[str] = []
        search_sources: list[tuple[str, str]] = []
        usage = UsageInfo()
        saw_done = False
        async for event in self._stream_chat_with_context(
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
                raw_sources = event.raw.get("search_sources")
                if isinstance(raw_sources, list):
                    for source in raw_sources:
                        if not isinstance(source, Mapping):
                            continue
                        title = str(source.get("title") or "").strip()
                        url = str(source.get("url") or "").strip()
                        if url and (title, url) not in search_sources:
                            search_sources.append((title, url))
            elif event.type == StreamEventType.ERROR:
                failure = RuntimeError(event.content or "Claude stream failed")
                for key in (
                    "status_code",
                    "retry_after_seconds",
                    "provider_error_type",
                    "provider_error_code",
                    "provider_error_schema_type",
                ):
                    value = event.raw.get(key)
                    if value is not None:
                        setattr(failure, key, value)
                raise failure

        if not saw_done:
            raise RuntimeError("Claude stream ended before message_stop")

        text = "".join(text_parts).strip()
        if search_sources:
            source_lines = ["Sources:"]
            source_lines.extend(
                f"- {title or url}: {url}" for title, url in search_sources
            )
            text = f"{text}\n\n{chr(10).join(source_lines)}".strip()
        self.record_non_stream_usage(
            usage,
            provider=self._provider_id,
            model_id=model,
            input_includes_cache_read=False,
            input_includes_cache_write=False,
            context=context,
        )
        if not text:
            raise RuntimeError("Claude 返回空内容")

        return text

    # ── 消息格式转换 ──────────────────────────────────────

    @staticmethod
    def _build_system_blocks(
        system_text: str,
        *,
        ttl_1h: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Split system prompt into cache-stable and dynamic blocks.

        The stable prefix gets a ``cache_control`` breakpoint. Dynamic
        workspace, skill, and memory context stays unmarked so changes there
        do not create repeated cache writes.
        Claude Code marks the stable system prefix only. Workspace, skill,
        memory, and other request-scoped context remain an unmarked suffix so a
        change there does not create a repeated cache write. Tool definitions
        and the conversation checkpoint are marked separately by
        ``_add_cache_breakpoints``.
        """
        split = split_sys_prompt_prefix(system_text)
        cache_control = _cache_control(ttl_1h)
        blocks: list[dict[str, Any]] = []
        if split.stable_prefix.strip():
            blocks.append(
                {
                    "type": "text",
                    "text": split.stable_prefix,
                    "cache_control": dict(cache_control),
                }
            )
        if split.dynamic_suffix.strip():
            blocks.append(
                {
                    "type": "text",
                    "text": split.dynamic_suffix,
                }
            )
        if not blocks:
            blocks.append(
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": dict(cache_control),
                }
            )
        return blocks

    def _commit_cache_edit_request(
        self,
        state: _CacheEditingState,
        consumed_ids: tuple[str, ...],
        new_pin: tuple[int, str, dict[str, Any]] | None,
    ) -> None:
        if consumed_ids:
            consumed = set(consumed_ids)
            state.pending_deletions = [
                call_id
                for call_id in state.pending_deletions
                if call_id not in consumed
            ]
        if new_pin is not None and all(
            existing[2] != new_pin[2] for existing in state.pinned_edits
        ):
            state.pinned_edits.append(new_pin)

    @staticmethod
    def _add_cache_breakpoints(
        api_messages: list[dict[str, Any]],
        anthropic_tools: list[dict[str, Any]] | None = None,
        *,
        cache_editing: bool = False,
        new_cache_deletions: tuple[str, ...] = (),
        pinned_cache_edits: tuple[tuple[int, str, dict[str, Any]], ...] = (),
        skip_cache_write: bool = False,
        ttl_1h: bool | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        tuple[int, str, dict[str, Any]] | None,
    ]:
        """Add cache_control breakpoints to messages and tools.

        Mirrors cc's ``addCacheBreakpoints``:
        1. Exactly one message-level cache_control marker on the last message.
        2. One cache_control marker on the last tool definition.
        3. tool_result blocks strictly before the last cache_control marker get
           ``cache_reference: tool_use_id`` to improve cache-hit tracking.
        Combined with the two system-block breakpoints from
        ``_build_system_blocks``, this gives Anthropic four cache segments:
          1. Stable system prefix (identity, rules, contracts)
          2. Tool definitions (stable across turns)
          3. Conversation history prefix (grows turn-by-turn)
        """
        cache_control = _cache_control(ttl_1h)

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
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": dict(cache_control),
                    }
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
                    reference = (
                        str(edit.get("cache_reference") or "")
                        if isinstance(edit, dict)
                        else ""
                    )
                    if reference and reference not in seen_references:
                        edits.append({"type": "delete", "cache_reference": reference})
                        seen_references.add(reference)
                if not edits:
                    return
                insert_at = 0
                while (
                    insert_at < len(content)
                    and isinstance(content[insert_at], dict)
                    and content[insert_at].get("type") == "tool_result"
                ):
                    insert_at += 1
                next_content = [
                    dict(item) if isinstance(item, dict) else item for item in content
                ]
                next_content.insert(insert_at, {"type": "cache_edits", "edits": edits})
                message["content"] = next_content

            for message_index, anchor, block in pinned_cache_edits:
                resolved_index = int(message_index)
                if not (
                    0 <= resolved_index < len(messages)
                    and message_anchors[resolved_index] == anchor
                ):
                    resolved_index = next(
                        (
                            index
                            for index, candidate in enumerate(message_anchors)
                            if candidate == anchor
                        ),
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
                        parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": data,
                                },
                            }
                        )
                    for doc in msg.documents:
                        media_type = doc.get("media_type") or "application/pdf"
                        data = doc.get("data") or ""
                        if not data:
                            continue
                        parts.append(
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": data,
                                },
                            }
                        )
                    raw_messages.append(
                        {"role": "user", "content": parts or msg.content}
                    )
                else:
                    raw_messages.append({"role": "user", "content": msg.content})

            elif msg.role == "assistant":
                native_content = _anthropic_replay_content(msg.provider_items)
                if native_content is not None:
                    # pause_turn and ordinary tool trajectories replay the exact
                    # provider assistant content, preserving interleaved text,
                    # thinking signatures and hosted server-tool blocks. The
                    # provider-neutral content/tool_calls fields are projections
                    # of this same item and must not be appended a second time.
                    content_parts = native_content
                else:
                    content_parts = []
                    for item in msg.provider_items:
                        item_type = str(item.get("type") or "")
                        if item_type in {"thinking", "redacted_thinking"}:
                            content_parts.append(dict(item))
                    if msg.content:
                        content_parts.append({"type": "text", "text": msg.content})
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            content_parts.append(
                                {
                                    "type": "tool_use",
                                    "id": tc.id,
                                    "name": tc.name,
                                    "input": tc.arguments,
                                }
                            )
                raw_messages.append(
                    {
                        "role": "assistant",
                        "content": content_parts
                        if content_parts
                        else [{"type": "text", "text": ""}],
                    }
                )

            elif msg.role == "tool":
                raw_messages.append(
                    {
                        "role": "tool_result",
                        "tool_use_id": msg.tool_call_id or "",
                        "content": msg.content,
                        "is_error": bool(getattr(msg, "is_error", False)),
                        "images": list(getattr(msg, "images", []) or []),
                    }
                )

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
                                blocks.append(
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": img.get("media_type")
                                            or "image/png",
                                            "data": img["data"],
                                        },
                                    }
                                )
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
                while (
                    j < len(raw_messages) and raw_messages[j]["role"] == "tool_result"
                ):
                    tool_results.append(_tool_result_block(raw_messages[j]))
                    j += 1
                api_messages.append({"role": "user", "content": tool_results})
                i = j

            elif msg["role"] == "user":
                if api_messages and api_messages[-1]["role"] == "user":
                    # 合并连续 user 消息
                    prev = api_messages[-1]
                    if isinstance(prev["content"], str) and isinstance(
                        msg["content"], str
                    ):
                        prev["content"] += "\n\n" + msg["content"]
                    else:
                        prev_blocks = (
                            prev["content"]
                            if isinstance(prev["content"], list)
                            else [{"type": "text", "text": prev["content"]}]
                        )
                        curr_blocks = (
                            msg["content"]
                            if isinstance(msg["content"], list)
                            else [{"type": "text", "text": msg["content"]}]
                        )
                        prev["content"] = prev_blocks + curr_blocks
                else:
                    api_messages.append(msg)
                i += 1

            elif msg["role"] == "assistant":
                if api_messages and api_messages[-1]["role"] == "assistant":
                    # 合并连续 assistant 消息
                    prev = api_messages[-1]
                    prev_content = (
                        prev["content"]
                        if isinstance(prev["content"], list)
                        else [{"type": "text", "text": prev["content"]}]
                    )
                    curr_content = (
                        msg["content"]
                        if isinstance(msg["content"], list)
                        else [{"type": "text", "text": msg["content"]}]
                    )
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
        return system_text, _strip_excess_anthropic_media(api_messages)

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
        tools = canonicalize_tool_schemas(tools)
        for tool in tools:
            func = tool.get("function", {})
            if not func:
                continue

            at: dict[str, Any] = {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
            anthropic_tools.append(at)

        return anthropic_tools

    def _convert_tools_cached(
        self, tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert tools with session-level schema caching.

        Mirrors current Claude Code's ``toolSchemaCache``: description drift is
        stable for one name/schema pair, while a genuine input-schema change
        receives a distinct cache entry.
        """
        result: list[dict[str, Any]] = []
        tools = canonicalize_tool_schemas(tools)
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
