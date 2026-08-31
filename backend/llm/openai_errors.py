from __future__ import annotations

import re


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
def _is_blocked_gateway_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    if status_code is not None and 500 <= status_code <= 599:
        return False
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
    return bool(
        status_code in {408, 409, 425, 429}
        or (status_code is not None and 500 <= status_code <= 599)
    ) or any(
        token in text for token in _TRANSIENT_ERROR_SUBSTRINGS
    )
