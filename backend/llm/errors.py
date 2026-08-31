"""Shared LLM error classification helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any


_AUTH_KEYWORDS = (
    "invalid api key",
    "incorrect api key",
    "api key is invalid",
    "unauthorized",
    "authentication",
    "auth_error",
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
)

_CONTENT_FILTER_KEYWORDS = (
    "content exists risk",
    "content_filter",
)

_BLOCKED_KEYWORDS = (
    "your request was blocked",
    "request was blocked",
    "blocked by",
    "waf",
    "cf-ray",
    "cloudflare",
    "forbidden",
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

_UNSUPPORTED_CAPABILITY_KEYWORDS = (
    "no endpoints found that support image input",
    "does not support image input",
    "doesn't support image input",
    "image input is not supported",
    "image inputs are not supported",
    "unsupported image input",
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
    "stream ended before [done]",
    "ended before [done]",
    "closed before response.completed",
    "ended before a finish reason",
    "ended before message_stop",
    "stream ended without a terminal event",
    "流式响应在完成前中断",
    "eof_without_terminal",
    "readerror",
    "writeerror",
)

_HTTP_STATUS_PATTERN = re.compile(r"(?:\bhttp\s*|\bstatus(?:_code)?\s*[=:]\s*)(\d{3})\b", re.IGNORECASE)


@dataclass(frozen=True)
class LLMErrorClassification:
    fatal: bool
    retryable: bool
    error_type: str
    provider_error_type: str = "unknown"


def classify_llm_error(message: str | BaseException | None) -> LLMErrorClassification:
    text = _normalize_error_text(message)
    status_codes = _extract_status_codes(message)

    # A provider's structured code is stronger than the transport status.
    # Gateways may use 503 for a configuration/model routing failure; that
    # must remain visible as a fatal model error instead of entering the
    # retry ladder as generic network unavailability.
    structured = _structured_provider_error_signal(message)
    if structured is not None:
        return structured

    if (
        "provider_error_type=protocol" in text
        or "provider_error_code=convert_request_failed" in text
        or "convert_request_failed" in text
    ):
        return LLMErrorClassification(
            fatal=True,
            retryable=False,
            error_type="provider_protocol",
            provider_error_type="protocol",
        )

    # A structured HTTP server status is authoritative. Gateway-generated
    # 52x pages commonly contain words such as "Cloudflare" or "blocked";
    # treating those pages as policy refusals makes a transient outage fatal.
    # Keep 529's established overloaded/busy meaning and classify every other
    # 5xx response as a retryable provider/network failure.
    if 529 in status_codes:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="busy")
    if any(500 <= code <= 599 for code in status_codes):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="network")

    if "provider_error_type=proxy" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="api", provider_error_type="proxy")
    if "provider_error_type=auth" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="auth", provider_error_type="auth")
    if "provider_error_type=billing" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="billing", provider_error_type="billing")
    if "provider_error_type=content_filter" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked", provider_error_type="content_filter")
    if "provider_error_type=blocked" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked", provider_error_type="blocked")
    if "provider_error_type=model" in text:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="model", provider_error_type="model")
    if "provider_error_type=unsupported_capability" in text:
        return LLMErrorClassification(
            fatal=True,
            retryable=False,
            error_type="provider_capability",
            provider_error_type="unsupported_capability",
        )
    if "provider_error_type=rate_limit" in text:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="rate_limit")
    if "provider_error_type=busy" in text:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="busy")
    if "provider_error_type=network" in text:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="network")

    if _contains_any(text, _UNSUPPORTED_CAPABILITY_KEYWORDS):
        return LLMErrorClassification(
            fatal=True,
            retryable=False,
            error_type="provider_capability",
            provider_error_type="unsupported_capability",
        )

    # A structured HTTP status is authoritative, for the same reason 5xx is
    # handled above: a gateway's own error page carries words like
    # "Cloudflare", "forbidden" or "billing", so letting the keyword tables
    # decide first turned a genuine 429 into a fatal policy block that the
    # retry ladder skipped entirely.
    if 407 in status_codes:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="api", provider_error_type="proxy")
    if 401 in status_codes:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="auth", provider_error_type="auth")
    if 402 in status_codes:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="billing", provider_error_type="billing")
    if 429 in status_codes:
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="rate_limit")
    # Content filtering is the more specific reading of a 403 than a generic
    # policy block, so it keeps its precedence over the numeric rule.
    if _contains_any(text, _CONTENT_FILTER_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked", provider_error_type="content_filter")
    if 403 in status_codes:
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked", provider_error_type="blocked")
    if (400 in status_codes or 404 in status_codes) and _contains_any(text, _MODEL_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="model", provider_error_type="model")
    if any(code in status_codes for code in (408, 409, 425)):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="network")

    # Keyword fallbacks, for providers that surface no usable status code.
    if _contains_any(text, ("proxy authentication required", "proxy auth")):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="api", provider_error_type="proxy")
    if _contains_any(text, _AUTH_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="auth", provider_error_type="auth")
    if _contains_any(text, _BILLING_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="billing", provider_error_type="billing")
    if _contains_any(text, _BLOCKED_KEYWORDS):
        return LLMErrorClassification(fatal=True, retryable=False, error_type="blocked", provider_error_type="blocked")
    if _contains_any(text, ("rate limit", "rate_limit", "too many requests")):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="rate_limit")
    if _contains_any(
        text,
        (
            "concurrency limit exceeded",
            "overloaded",
            "resourceexhausted",
            "resource exhausted",
        ),
    ):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="busy")
    if _contains_any(text, _NETWORK_KEYWORDS):
        return LLMErrorClassification(fatal=False, retryable=True, error_type="api", provider_error_type="network")

    # Size-class recoveries: media rejections and prompt-too-long should enter the
    # withholding ladder (strip media / emergency compact) rather than generic API fail.
    if _contains_any(
        text,
        (
            "image exceeds",
            "image too large",
            "image dimensions exceed",
            "many-image",
            "media size",
            "media too large",
            "pdf specified was not valid",
        ),
    ) or ("maximum" in text and ("image" in text or "media" in text or "pdf" in text)):
        return LLMErrorClassification(
            fatal=False,
            retryable=True,
            error_type="media_size",
            provider_error_type="media_size",
        )
    if 413 in status_codes or _contains_any(
        text,
        (
            "prompt is too long",
            "prompt too long",
            "context_length",
            "maximum context",
            "context window",
            "request_too_large",
            "request entity too large",
            "request body too large",
            "payload too large",
            "content too large",
        ),
    ):
        return LLMErrorClassification(
            fatal=False,
            retryable=True,
            error_type="prompt_too_long",
            provider_error_type="prompt_too_long",
        )

    return LLMErrorClassification(fatal=False, retryable=False, error_type="api", provider_error_type="unknown")


def is_fatal_llm_error(message: str | BaseException | None) -> bool:
    return classify_llm_error(message).fatal


def is_retryable_llm_error(message: str | BaseException | None) -> bool:
    return classify_llm_error(message).retryable


def llm_error_status_code(message: str | BaseException | None) -> int | None:
    """Return the first structured HTTP status carried by an LLM failure."""

    for item in _error_chain(message):
        for value in (
            getattr(item, "status_code", None),
            getattr(getattr(item, "response", None), "status_code", None),
        ):
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
    codes = sorted(_extract_status_codes(message))
    return codes[0] if codes else None


def retry_after_seconds(
    message: str | BaseException | None,
    *,
    maximum: float = 60.0,
) -> float:
    """Parse HTTP ``Retry-After`` metadata from an exception chain.

    Both the delay-seconds and HTTP-date forms from RFC 9110 are supported.
    The default 60s maximum matches pi's DEFAULT_MAX_RETRY_DELAY_MS so a
    provider cannot make the Agent sleep indefinitely.
    """

    limit = max(0.0, float(maximum))
    for item in _error_chain(message):
        direct = getattr(item, "retry_after_seconds", None)
        if direct is not None:
            try:
                return max(0.0, min(float(direct), limit))
            except (TypeError, ValueError):
                pass
        response = getattr(item, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            headers = getattr(item, "headers", None)
        raw = None
        if headers is not None:
            try:
                raw = headers.get("retry-after")
                if raw is None:
                    raw = headers.get("Retry-After")
            except (AttributeError, TypeError):
                raw = None
        if raw is None:
            continue
        try:
            return max(0.0, min(float(raw), limit))
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(str(raw))
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                delay = (target - datetime.now(UTC)).total_seconds()
                return max(0.0, min(delay, limit))
            except (TypeError, ValueError, OverflowError):
                continue
    return 0.0


_ADAPTER_ERROR_RETRY_AFTER_MAXIMUM = 300.0

_PROVIDER_SECRET_TEXT_RE = re.compile(
    r"(?i)"
    r"(?:bearer\s+|(?:api[_ -]?key|authorization)\s*[:=]\s*)[^\s,;]+"
    r"|\bsk(?:-[A-Za-z0-9_-]+|_[A-Za-z0-9_-]+)\b"
)


def _provider_response_body(exc: BaseException) -> str:
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


def _safe_provider_diagnostic_text(value: Any, *, limit: int = 400) -> str:
    """Bound and redact provider text before it enters a trace or UI event."""

    from backend.secret_redaction import redact_secrets

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    text = redact_secrets(text)
    text = _PROVIDER_SECRET_TEXT_RE.sub("[redacted]", text)
    return text[: max(1, limit)]


def _provider_error_body_details(body: str) -> dict[str, Any]:
    """Extract only compact, non-body fields from a provider error payload."""

    try:
        payload = json.loads(body) if body else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    error_obj = payload.get("error")
    if not isinstance(error_obj, dict):
        error_obj = payload
    details: dict[str, Any] = {}
    for key in (
        "message",
        "code",
        "type",
        "param",
        "status",
        "status_code",
        "request_id",
    ):
        value = error_obj.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        if key == "message":
            value = _safe_provider_diagnostic_text(value)
        elif isinstance(value, str):
            value = _safe_provider_diagnostic_text(value, limit=160)
        details[key] = value
    return details


def _structured_provider_error_signal(
    message: str | BaseException | None,
) -> LLMErrorClassification | None:
    """Classify stable provider error fields without depending on wording."""

    for item in _error_chain(message):
        body = _provider_response_body(item) if isinstance(item, BaseException) else str(item or "")
        details = _provider_error_body_details(body)
        if not details:
            continue
        code = str(details.get("code") or "").strip().casefold()
        schema_type = str(details.get("type") or "").strip().casefold()
        signal = f"{code} {schema_type}"
        if "convert_request_failed" in signal:
            return LLMErrorClassification(True, False, "provider_protocol", "protocol")
        if any(token in signal for token in ("invalid_api_key", "authentication_error", "unauthorized")):
            return LLMErrorClassification(True, False, "auth", "auth")
        if any(token in signal for token in ("insufficient_quota", "quota_exceeded", "billing_error")):
            return LLMErrorClassification(True, False, "billing", "billing")
        if any(token in signal for token in ("rate_limit", "rate_limited", "too_many_requests")):
            return LLMErrorClassification(False, True, "api", "rate_limit")
        if any(token in signal for token in ("model_not_found", "invalid_model")):
            return LLMErrorClassification(True, False, "model", "model")
        if any(token in signal for token in ("content_filter", "safety_filter")):
            return LLMErrorClassification(True, False, "blocked", "content_filter")
        if any(token in signal for token in ("context_length_exceeded", "prompt_too_long")):
            return LLMErrorClassification(False, True, "prompt_too_long", "prompt_too_long")
    return None


def llm_error_raw(exc: BaseException, provider: str) -> dict[str, Any]:
    """Build the normalized ``StreamEvent.raw`` payload for a failed request.

    ``backend/agent/provider_stream_error_event.py`` classifies the failure and
    schedules the retry ladder from ``status_code``, ``provider_error_code``,
    ``provider_error_schema_type`` and ``retry_after_seconds``. Every ERROR
    event that crosses the provider boundary must therefore carry them, so all
    adapters and the shared stream wrapper build the shape here rather than
    each assembling a partial dict of their own.
    """

    classification = classify_llm_error(exc)
    raw: dict[str, Any] = {
        "provider": provider,
        "exception_type": type(exc).__name__,
        "provider_error_type": classification.provider_error_type,
        "error_type": classification.error_type,
    }
    status = llm_error_status_code(exc)
    if status is not None:
        raw["status_code"] = status
    retry_after = retry_after_seconds(
        exc,
        maximum=_ADAPTER_ERROR_RETRY_AFTER_MAXIMUM,
    )
    if retry_after > 0:
        raw["retry_after_seconds"] = retry_after
    body_details = _provider_error_body_details(_provider_response_body(exc))
    provider_error_code = str(body_details.get("code") or "").strip()
    provider_error_schema_type = str(body_details.get("type") or "").strip()
    provider_error_message = str(body_details.get("message") or "").strip()
    if provider_error_code:
        raw["provider_error_code"] = provider_error_code
    if provider_error_schema_type:
        raw["provider_error_schema_type"] = provider_error_schema_type
    if provider_error_message:
        raw["provider_error_message"] = provider_error_message
    return raw


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
        for match in _HTTP_STATUS_PATTERN.finditer(str(item)):
            codes.add(int(match.group(1)))
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
    *,
    include_provider_details: bool = True,
) -> str:
    """Return user-facing model error text without leaking provider internals."""
    kind = (classification or classify_llm_error(message)).provider_error_type
    suffix = _safe_provider_error_suffix(message, kind) if include_provider_details else ""
    if kind in {"busy", "rate_limit"}:
        return "\u6a21\u578b\u6682\u65f6\u7e41\u5fd9\u6216\u8fbe\u5230\u5e76\u53d1\u9650\u5236\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002" + suffix
    if kind == "auth":
        return "\u6a21\u578b\u9274\u6743\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5 API Key \u548c\u6a21\u578b\u8bbe\u7f6e\u3002" + suffix
    if kind == "billing":
        return "\u6a21\u578b\u670d\u52a1\u989d\u5ea6\u6216\u8ba1\u8d39\u4e0d\u53ef\u7528\uff0c\u8bf7\u68c0\u67e5\u8d26\u6237\u72b6\u6001\u3002" + suffix
    if kind == "content_filter":
        return "\u6a21\u578b\u670d\u52a1\u5546\u56e0\u5185\u5bb9\u5b89\u5168\u7b56\u7565\u62d2\u7edd\u4e86\u672c\u6b21\u8bf7\u6c42\u3002\u8bf7\u7f16\u8f91\u4e0a\u4e00\u6761\u6d88\u606f\u6216\u65b0\u5efa\u4f1a\u8bdd\u540e\u91cd\u8bd5\uff1b\u8054\u7f51\u67e5\u8be2\u65f6\u53ef\u7f29\u5c0f\u8303\u56f4\u6216\u66f4\u6362\u6765\u6e90\uff0c\u53cd\u590d\u53d1\u751f\u65f6\u53ef\u624b\u52a8\u5207\u6362\u6a21\u578b\u3002" + suffix
    if kind == "blocked":
        return "\u6a21\u578b\u8bf7\u6c42\u88ab\u670d\u52a1\u5546\u6216\u7f51\u5173\u62e6\u622a\uff0c\u8bf7\u68c0\u67e5\u6a21\u578b\u3001Base URL\u3001\u7f51\u5173\u89c4\u5219\u6216\u8bf7\u6c42\u5185\u5bb9\u3002" + suffix
    if kind == "proxy":
        return "\u8054\u7f51\u8bf7\u6c42\u5931\u8d25\uff1a\u4ee3\u7406\u8ba4\u8bc1\u5931\u8d25\uff08407 Proxy Authentication Required\uff09\u3002\u8bf7\u68c0\u67e5 HTTP_PROXY / HTTPS_PROXY \u6216\u4ee3\u7406\u8ba4\u8bc1\u4fe1\u606f\u3002" + suffix
    if kind == "model":
        return "\u6a21\u578b\u540d\u6216\u6a21\u578b\u914d\u7f6e\u65e0\u6548\uff0c\u8bf7\u68c0\u67e5 provider\u3001Base URL \u548c model \u8bbe\u7f6e\u3002" + suffix
    if kind == "unsupported_capability":
        return "\u5f53\u524d\u6a21\u578b\u4e0d\u652f\u6301\u56fe\u7247\u8f93\u5165\uff0c\u8bf7\u5207\u6362\u5230\u652f\u6301\u89c6\u89c9\u8f93\u5165\u7684\u6a21\u578b\u3002" + suffix
    if kind == "protocol":
        return "\u6240\u9009 API \u683c\u5f0f\u65e0\u6cd5\u88ab\u5f53\u524d\u670d\u52a1\u5546\u6216\u7f51\u5173\u5904\u7406\uff0c\u8bf7\u68c0\u67e5 API \u683c\u5f0f\u4e0e Base URL\u3002MiniCode \u672a\u5207\u6362\u5230\u5176\u4ed6\u534f\u8bae\u3002" + suffix
    if kind == "network":
        return "\u6a21\u578b\u670d\u52a1\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002" + suffix
    return "\u6a21\u578b\u8c03\u7528\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u5207\u6362\u6a21\u578b\u3002" + suffix


def _safe_provider_error_suffix(message: str | BaseException | None, provider_error_type: str) -> str:
    """Append safe provider diagnostics such as HTTP status/code.

    Keep provider response bodies out of user-facing text, but preserve compact
    metadata that explains what actually happened (for example HTTP 403).
    """
    text = _normalize_error_text(message)
    parts: list[str] = []
    if provider_error_type and provider_error_type != "unknown":
        parts.append(f"provider={provider_error_type}")

    status_codes = sorted(_extract_status_codes(message))
    for match in re.finditer(r"\bstatus=(\d{3})\b|\bhttp\s*(\d{3})\b", text, re.IGNORECASE):
        raw = match.group(1) or match.group(2)
        try:
            status_codes.append(int(raw))
        except (TypeError, ValueError):
            continue
    for status in sorted(set(status_codes)):
        parts.append(f"HTTP {status}")

    code_match = re.search(r"\bprovider_error_code=([A-Za-z0-9._:-]{1,80})", text)
    if code_match:
        parts.append(f"code={code_match.group(1)}")
    schema_match = re.search(r"\bprovider_error_schema_type=([A-Za-z0-9._:-]{1,80})", text)
    if schema_match:
        parts.append(f"type={schema_match.group(1)}")

    return f"\uff08{', '.join(parts)}\uff09" if parts else ""
