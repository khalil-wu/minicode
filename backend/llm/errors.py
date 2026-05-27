"""Shared LLM error classification helpers.

The agent uses these helpers to decide whether an upstream error is transient
enough to retry/fallback, or fatal enough to stop immediately and avoid burning
more tokens.
"""

from __future__ import annotations

from dataclasses import dataclass


_FATAL_KEYWORDS = (
    "402",
    "payment required",
    "insufficient balance",
    "insufficient_balance",
    "insufficient quota",
    "insufficient_quota",
    "quota exceeded",
    "billing",
    "payment",
    "invalid api key",
    "incorrect api key",
    "api key is invalid",
    "unauthorized",
    "authentication",
    "401",
)

_BLOCKED_KEYWORDS = (
    "your request was blocked",
    "request was blocked",
    "blocked by",
    "waf",
    "cf-ray",
    "cloudflare",
    "forbidden",
    "403",
)

_TRANSIENT_KEYWORDS = (
    "concurrency limit exceeded",
    "retry later",
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "timeout",
    "timed out",
    "temporarily unavailable",
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
)


@dataclass(frozen=True)
class LLMErrorClassification:
    fatal: bool
    retryable: bool
    error_type: str


def classify_llm_error(message: str | BaseException | None) -> LLMErrorClassification:
    text = _normalize_error_text(message)
    if any(keyword in text for keyword in _FATAL_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="billing")
    if any(keyword in text for keyword in _BLOCKED_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked")
    if any(keyword in text for keyword in _TRANSIENT_KEYWORDS):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api")
    return LLMErrorClassification(fatal=False, retryable=False, error_type="api")


def is_fatal_llm_error(message: str | BaseException | None) -> bool:
    return classify_llm_error(message).fatal


def is_retryable_llm_error(message: str | BaseException | None) -> bool:
    return classify_llm_error(message).retryable


def _normalize_error_text(message: str | BaseException | None) -> str:
    if message is None:
        return ""
    return str(message).lower()
