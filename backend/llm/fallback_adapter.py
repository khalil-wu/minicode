"""
Fallback LLM 适配器（youhua.md P1-3）。

将多个 LLMAdapter 按优先级串联起来：
  - 主 adapter 返回 429/5xx/连接错误时，透明切换到下一个
  - 对上游调用者完全透明：接口与 LLMAdapter 一致
  - 不缓存任何有状态数据，只做委托与路由

典型用法：
    primary = OpenAIAdapter(...)
    backup = AnthropicAdapter(...)
    adapter = FallbackLLMAdapter([primary, backup])
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    StreamEvent,
    StreamEventType,
    stream_chat_with_request_metadata,
)
from backend.llm.capabilities import ProviderCapabilities, combine_fallback_capabilities
from backend.llm.errors import is_fatal_llm_error, is_retryable_llm_error

logger = logging.getLogger(__name__)


# 判定为“可切换到下一家”的错误特征
_FALLBACK_ERROR_KEYWORDS = (
    "concurrency limit exceeded",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota exceeded",
    "insufficient_quota",
    "retry later",
    "429",
    "500",
    "502",
    "503",
    "504",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection error",
    "connection reset",
    "connection refused",
    "connection aborted",
    "timeout",
    "timed out",
)

_ = _FALLBACK_ERROR_KEYWORDS


def _error_is_transient(error_message: str) -> bool:
    return is_retryable_llm_error(error_message)


def _exception_is_transient(exc: BaseException) -> bool:
    # Preserve structured status/response fields and the chained cause. The
    # shared classifier already understands them; flattening to text can lose
    # a 429/5xx when the exception message itself omits the status code.
    return is_retryable_llm_error(exc)


class FallbackLLMAdapter(LLMAdapter):
    """按顺序尝试多个 LLMAdapter，遇到可恢复错误时自动切换下一个。"""

    def __init__(self, adapters: list[LLMAdapter]) -> None:
        non_empty = [adapter for adapter in adapters if adapter is not None]
        if not non_empty:
            raise ValueError("FallbackLLMAdapter requires at least one adapter")
        self._adapters = non_empty

    @property
    def adapters(self) -> list[LLMAdapter]:
        return list(self._adapters)

    async def aclose(self) -> None:
        """Close every owned provider adapter, preserving all close attempts."""
        import asyncio

        closers = []
        for adapter in self._adapters:
            close = getattr(adapter, "aclose", None)
            if callable(close):
                closers.append(close())
        if closers:
            await asyncio.gather(*closers, return_exceptions=True)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return combine_fallback_capabilities(self.adapters)

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        last_error: str = ""
        for index, adapter in enumerate(self._adapters):
            is_last = index == len(self._adapters) - 1
            yielded_text = False
            yielded_non_restartable_content = False
            fallback_error_message: str | None = None
            structured_transient_error = False
            incomplete_eof = False

            try:
                async for event in stream_chat_with_request_metadata(
                    adapter,
                    messages,
                    tools=tools,
                    metadata=metadata,
                ):
                    if event.type == StreamEventType.ERROR:
                        fallback_error_message = event.content or "stream error"
                        break

                    if event.type == StreamEventType.TEXT_CHUNK and event.content:
                        yielded_text = True
                    elif event.type in (
                        StreamEventType.IMAGE_CHUNK,
                        StreamEventType.THINKING_CHUNK,
                        StreamEventType.TOOL_CALL_START,
                        StreamEventType.TOOL_CALL_DELTA,
                        StreamEventType.TOOL_CALL,
                    ):
                        yielded_non_restartable_content = True

                    yield event

                    if event.type == StreamEventType.DONE:
                        return
            except Exception as exc:  # noqa: BLE001
                if (
                    is_last
                    or yielded_non_restartable_content
                    or is_fatal_llm_error(exc)
                    or not _exception_is_transient(exc)
                ):
                    raise
                fallback_error_message = f"{type(exc).__name__}: {exc}"
                structured_transient_error = True

            if fallback_error_message is None:
                fallback_error_message = "provider stream ended without DONE"
                incomplete_eof = True

            last_error = fallback_error_message
            if (
                is_last
                or yielded_non_restartable_content
                or (
                    not incomplete_eof
                    and not structured_transient_error
                    and (
                        is_fatal_llm_error(last_error)
                        or not _error_is_transient(last_error)
                    )
                )
            ):
                yield StreamEvent(type=StreamEventType.ERROR, content=last_error)
                return

            if yielded_text:
                yield StreamEvent(
                    type=StreamEventType.FALLBACK_RESTART,
                    content=last_error,
                    raw={
                        "failed_adapter": type(adapter).__name__,
                        "fallback_adapter": type(self._adapters[index + 1]).__name__,
                        "reason": last_error,
                    },
                )

            logger.warning(
                "Primary LLM adapter %s failed (%s); falling back to next provider",
                type(adapter).__name__,
                last_error,
            )

        # 兜底：所有 adapter 都未产出 DONE
        yield StreamEvent(
            type=StreamEventType.ERROR,
            content=last_error or "all LLM adapters failed without response",
        )

    async def simple_chat(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        last_exc: BaseException | None = None
        for index, adapter in enumerate(self._adapters):
            is_last = index == len(self._adapters) - 1
            try:
                return await adapter.simple_chat(messages, max_tokens=max_tokens)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if is_last or is_fatal_llm_error(exc) or not _exception_is_transient(exc):
                    raise
                logger.warning(
                    "simple_chat on %s failed (%s); trying next provider",
                    type(adapter).__name__,
                    exc,
                )
        # 理论上不会到这里；保底抛出最后一次异常
        if last_exc is not None:
            raise last_exc
        return ""
