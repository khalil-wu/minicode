from __future__ import annotations

import re
from backend.llm.errors import llm_error_status_code as _error_status_code


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
