from __future__ import annotations

import re

from backend.llm.errors import retry_after_seconds

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
            "client_metadata",
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
    if mentions_request_metadata and any(
        token in text
        for token in (
            "unexpected keyword argument",
            "unexpected argument",
            "unexpected keyword",
        )
    ):
        return True
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


def _unsupported_responses_tool_control_fields(exc: Exception) -> frozenset[str]:
    """Return explicitly rejected optional Responses tool-control fields.

    Compatibility relays vary here: some accept the Responses endpoint but
    reject the otherwise standard ``tool_choice`` or
    ``parallel_tool_calls`` controls, and a smaller set rejects an empty
    ``tools`` array.  Only a concrete 400/422 field error is eligible for a
    downgrade; transient failures and vague model/tool errors must continue to
    surface unchanged.
    """

    if _error_status_code(exc) not in {400, 422}:
        return frozenset()
    text = _error_text(exc)
    mentions_incompatibility = any(
        token in text
        for token in (
            "invalid parameter",
            "unsupported",
            "not supported",
            "not support",
            "unrecognized",
            "unknown parameter",
            "unknown field",
            "unexpected field",
            "unexpected parameter",
            "extra field",
            "extra inputs",
            "extra_forbidden",
            "not permitted",
            "not allowed",
            "badrequest",
            "bad request",
        )
    )
    if not mentions_incompatibility:
        return frozenset()

    aliases = {
        "tools": ("tools",),
        "tool_choice": ("tool_choice", "tool choice"),
        "parallel_tool_calls": ("parallel_tool_calls", "parallel tool calls"),
    }
    rejected: set[str] = set()
    for field, field_aliases in aliases.items():
        if any(
            re.search(
                rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])",
                text,
            )
            for alias in field_aliases
        ):
            rejected.add(field)
    return frozenset(rejected)


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


def _is_prompt_cache_breakpoint_unsupported_error(exc: Exception) -> bool:
    text = _error_text(exc)
    status_code = _error_status_code(exc)
    mentions_breakpoint = any(
        token in text
        for token in (
            "prompt_cache_options",
            "prompt cache options",
            "prompt_cache_breakpoint",
            "prompt cache breakpoint",
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
    return bool(
        status_code in {400, 422}
        and mentions_breakpoint
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


def _retry_after_seconds(exc: Exception, *, maximum: float = 300.0) -> float:
    return retry_after_seconds(exc, maximum=maximum)
