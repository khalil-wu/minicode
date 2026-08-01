from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_TRANSIENT_ERROR_SUBSTRINGS = (
    "concurrency limit exceeded",
    "retry later",
    "rate limit",
    "too many requests",
    "429",
    "timeout",
    "temporarily unavailable",
)


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


def _clean_error_message(exc: Exception) -> str:
    msg = re.sub(r"<[^>]+>", " ", str(exc))
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:200] + "..." if len(msg) > 200 else msg


def _is_invalid_tool_schema_error(exc: Exception) -> bool:
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


def _is_reasoning_visibility_unsupported_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    mentions_reasoning_visibility = any(
        token in text
        for token in (
            "reasoning.summary",
            "reasoning_summary",
            "reasoning summary",
            "reasoning",
            "summary",
            "reasoning.content",
            "reasoning_content",
            "reasoning text",
            "reasoning_text",
            "reasoning.encrypted_content",
            "encrypted_content",
            "encrypted content",
            "include",
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
            "unknown field",
            "extra inputs",
            "badrequest",
            "bad request",
        )
    )
    return bool(status_code in {400, 422} and mentions_reasoning_visibility and mentions_incompatibility)


def _is_request_metadata_unsupported_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    mentions_request_metadata = any(
        token in text
        for token in (
            "metadata",
            "store",
            "stored completion",
            "unknown parameter: 'metadata'",
            "unknown parameter: 'store'",
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
            "unknown field",
            "extra inputs",
            "badrequest",
            "bad request",
        )
    )
    return bool(status_code in {400, 422} and mentions_request_metadata and mentions_incompatibility)


def _is_stream_options_unsupported_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    mentions_stream_options = any(
        token in text
        for token in ("stream_options", "stream options", "include_usage")
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
            "unknown field",
            "extra inputs",
            "badrequest",
            "bad request",
        )
    )
    return bool(
        status_code in {400, 422}
        and mentions_stream_options
        and mentions_incompatibility
    )


def _is_prompt_cache_retention_unsupported_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    mentions_prompt_cache_retention = any(
        token in text
        for token in (
            "prompt_cache_retention",
            "prompt cache retention",
            "cache retention",
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
            "unknown field",
            "extra inputs",
            "badrequest",
            "bad request",
        )
    )
    return bool(status_code in {400, 422} and mentions_prompt_cache_retention and mentions_incompatibility)


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


def _retry_after_seconds(exc: Exception, *, maximum: float = 300.0) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = None
    if headers is not None:
        raw = headers.get("retry-after")
        if raw is None:
            raw = headers.get("Retry-After")
    if raw is None:
        return 0.0
    try:
        return max(0.0, min(float(raw), maximum))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            return max(0.0, min((target - datetime.now(UTC)).total_seconds(), maximum))
        except (TypeError, ValueError, OverflowError):
            return 0.0
