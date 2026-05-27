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

from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
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
    return is_retryable_llm_error(f"{type(exc).__name__}: {exc}")


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

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        last_error: str = ""
        for index, adapter in enumerate(self._adapters):
            is_last = index == len(self._adapters) - 1
            yielded_any_content = False
            fallback_error_message: str | None = None

            try:
                async for event in adapter.stream_chat(messages, tools=tools):
                    if event.type == StreamEventType.ERROR and not yielded_any_content:
                        fallback_error_message = event.content or "stream error"
                        break

                    if event.type in (
                        StreamEventType.TEXT_CHUNK,
                        StreamEventType.TOOL_CALL,
                    ):
                        yielded_any_content = True

                    yield event

                    if event.type == StreamEventType.DONE:
                        return
            except Exception as exc:  # noqa: BLE001
                if yielded_any_content or is_last or is_fatal_llm_error(exc) or not _exception_is_transient(exc):
                    raise
                fallback_error_message = f"{type(exc).__name__}: {exc}"

            if fallback_error_message is None:
                return

            last_error = fallback_error_message
            if is_last or yielded_any_content or is_fatal_llm_error(last_error) or not _error_is_transient(last_error):
                yield StreamEvent(type=StreamEventType.ERROR, content=last_error)
                return

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

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        last_exc: BaseException | None = None
        for index, adapter in enumerate(self._adapters):
            is_last = index == len(self._adapters) - 1
            try:
                return await adapter.simple_chat(messages)
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
