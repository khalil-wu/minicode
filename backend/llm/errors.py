"""Shared LLM error classification helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


_AUTH_KEYWORDS = (
    "invalid api key",
    "incorrect api key",
    "api key is invalid",
    "unauthorized",
    "authentication",
    "auth_error",
    "401",
)

_BILLING_KEYWORDS = (
    "payment required",
    "insufficient balance",
    "insufficient_balance",
    "insufficient quota",
    "insufficient_quota",
    "quota exceeded",
    "billing",
    "payment",
    "402",
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

_MODEL_KEYWORDS = (
    "model_not_found",
    "model not found",
    "invalid_model",
    "invalid model",
    "unknown model",
    "model does not exist",
    "model doesn't exist",
    "no such model",
    "model is not found",
)

_NETWORK_KEYWORDS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection error",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connecterror",
    "readtimeout",
    "network",
    # httpx/DeepSeek streaming drops: these are the actual stringified forms
    # of RemoteProtocolError / ConnectError / DNS failures. Without them a
    # transient stream cutoff falls through to `unknown` (non-retryable) and
    # surfaces as a generic "模型调用失败" even for a trivial request.
    "remoteprotocolerror",
    "peer closed connection",
    "server disconnected",
    "incomplete chunked read",
    "all connection attempts failed",
    "getaddrinfo failed",
    "name or service not known",
    "connection closed",
    "readerror",
    "writeerror",
    "500",
    "502",
    "503",
    "504",
)


@dataclass(frozen=True)
class LLMErrorClassification:
    fatal: bool
    retryable: bool
    error_type: str
    provider_error_type: str = "unknown"


def classify_llm_error(message: str | BaseException | None) -> LLMErrorClassification:
    text = _normalize_error_text(message)
    status_codes = _extract_status_codes(message)

    if "provider_error_type=proxy" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="api", provider_error_type="proxy")
    if "provider_error_type=auth" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="auth", provider_error_type="auth")
    if "provider_error_type=billing" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="billing", provider_error_type="billing")
    if "provider_error_type=blocked" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked", provider_error_type="blocked")
    if "provider_error_type=model" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="model", provider_error_type="model")
    if "provider_error_type=rate_limit" in text:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="rate_limit")
    if "provider_error_type=network" in text:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="network")

    if 407 in status_codes or _contains_any(text, ("proxy authentication required", "proxy auth", "407")):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="api", provider_error_type="proxy")
    if 401 in status_codes or _contains_any(text, _AUTH_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="auth", provider_error_type="auth")
    if 402 in status_codes or _contains_any(text, _BILLING_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="billing", provider_error_type="billing")
    if 403 in status_codes or _contains_any(text, _BLOCKED_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked", provider_error_type="blocked")
    if (400 in status_codes or 404 in status_codes) and _contains_any(text, _MODEL_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="model", provider_error_type="model")
    if 429 in status_codes or _contains_any(text, ("rate limit", "rate_limit", "too many requests", "429")):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="rate_limit")
    if "concurrency limit exceeded" in text:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="busy")
    if any(code in status_codes for code in (500, 502, 503, 504)) or _contains_any(text, _NETWORK_KEYWORDS):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="network")
    return LLMErrorClassification(fatal=False, retryable=False, error_type="api", provider_error_type="unknown")


def is_fatal_llm_error(message: str | BaseException | None) -> bool:
    return classify_llm_error(message).fatal


def is_retryable_llm_error(message: str | BaseException | None) -> bool:
    return classify_llm_error(message).retryable


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _normalize_error_text(message: str | BaseException | None) -> str:
    if message is None:
        return ""
    return " ".join(_error_text_fragments(message)).lower()


def _extract_status_codes(message: str | BaseException | None) -> set[int]:
    codes: set[int] = set()
    for item in _error_chain(message):
        for value in (
            getattr(item, "status_code", None),
            getattr(getattr(item, "response", None), "status_code", None),
        ):
            try:
                if value is not None:
                    codes.add(int(value))
            except (TypeError, ValueError):
                continue
    return codes


def _error_chain(message: str | BaseException | None) -> list[str | BaseException]:
    if message is None:
        return []
    if not isinstance(message, BaseException):
        return [message]
    chain: list[str | BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = message
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _error_text_fragments(message: str | BaseException | None) -> list[str]:
    fragments: list[str] = []
    for item in _error_chain(message):
        _append_text(fragments, str(item))
        if not isinstance(item, BaseException):
            continue
        _append_text(fragments, type(item).__name__)
        for attr in ("status_code", "code", "type", "message", "body"):
            _append_text(fragments, _stringify(getattr(item, attr, None)))
        response = getattr(item, "response", None)
        if response is None:
            continue
        _append_text(fragments, _stringify(getattr(response, "status_code", None)))
        body = _response_body_text(response)
        _append_text(fragments, body)
        fragments.extend(_json_fragments(body))
    return fragments


def _append_text(fragments: list[str], value: str) -> None:
    text = value.strip()
    if text:
        fragments.append(text)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _response_body_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return _stringify(text)
    content = getattr(response, "content", None)
    if content:
        return _stringify(content)
    return ""


def _json_fragments(text: str) -> list[str]:
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    _flatten_json(data, out)
    return out


def _flatten_json(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _append_text(out, str(key))
            _flatten_json(item, out)
    elif isinstance(value, list):
        for item in value:
            _flatten_json(item, out)
    else:
        _append_text(out, _stringify(value))


def sanitize_llm_error_message(
    message: str | BaseException | None,
    classification: LLMErrorClassification | None = None,
) -> str:
    """Return user-facing model error text without leaking provider internals."""
    kind = (classification or classify_llm_error(message)).provider_error_type
    if kind in {"busy", "rate_limit"}:
        return "\u6a21\u578b\u6682\u65f6\u7e41\u5fd9\u6216\u8fbe\u5230\u5e76\u53d1\u9650\u5236\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002"
    if kind == "auth":
        return "\u6a21\u578b\u9274\u6743\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5 API Key \u548c\u6a21\u578b\u8bbe\u7f6e\u3002"
    if kind == "billing":
        return "\u6a21\u578b\u670d\u52a1\u989d\u5ea6\u6216\u8ba1\u8d39\u4e0d\u53ef\u7528\uff0c\u8bf7\u68c0\u67e5\u8d26\u6237\u72b6\u6001\u3002"
    if kind == "blocked":
        return "\u6a21\u578b\u8bf7\u6c42\u88ab\u670d\u52a1\u5546\u6216\u7f51\u5173\u62e6\u622a\uff0c\u8bf7\u68c0\u67e5\u6a21\u578b\u3001Base URL\u3001\u7f51\u5173\u89c4\u5219\u6216\u8bf7\u6c42\u5185\u5bb9\u3002"
    if kind == "proxy":
        return "\u8054\u7f51\u8bf7\u6c42\u5931\u8d25\uff1a\u4ee3\u7406\u8ba4\u8bc1\u5931\u8d25\uff08407 Proxy Authentication Required\uff09\u3002\u8bf7\u68c0\u67e5 HTTP_PROXY / HTTPS_PROXY \u6216\u4ee3\u7406\u8ba4\u8bc1\u4fe1\u606f\u3002"
    if kind == "model":
        return "\u6a21\u578b\u540d\u6216\u6a21\u578b\u914d\u7f6e\u65e0\u6548\uff0c\u8bf7\u68c0\u67e5 provider\u3001Base URL \u548c model \u8bbe\u7f6e\u3002"
    if kind == "network":
        return "\u6a21\u578b\u670d\u52a1\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"
    return "\u6a21\u578b\u8c03\u7528\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002"
