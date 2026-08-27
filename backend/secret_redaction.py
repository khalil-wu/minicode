"""Best-effort secret redaction ported from Codex's secrets sanitizer."""

from __future__ import annotations

import math
import re
from typing import Any


_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,512}\b")
_UNDERSCORE_GATEWAY_KEY_RE = re.compile(r"\bsk_[A-Za-z0-9_-]{20,512}\b")
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_BEARER_TOKEN_RE = re.compile(
    r"(?i:\bBearer)[ \t]+[A-Za-z0-9._~+/-]{16,}=*"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)([\"']?)[^\s\"']{8,}"
)
_JSON_SECRET_FIELD_RE = re.compile(
    r'"(session_ingress_token|environment_secret|access_token|secret|token)"\s*:\s*"([^"]*)"',
)

_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "headers",
        "defaultheaders",
        "apikey",
        "accesskey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "csrftoken",
        "token",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "privatekey",
        "sshkey",
        "credential",
        "credentials",
        "environmentsecret",
        "sessioningresstoken",
        "oauthcode",
        "codeverifier",
        "runtimeownertoken",
        "runtimeinstanceid",
        "runtimeprocessid",
        "runtimeprocessstartidentity",
        "requestbody",
        "responsebody",
        "rawbody",
        "systemprompt",
        "traceback",
        "stack",
        "stacktrace",
    }
)


def redact_secrets(value: str) -> str:
    redacted = _BEARER_TOKEN_RE.sub("Bearer [REDACTED_SECRET]", str(value or ""))
    redacted = _OPENAI_KEY_RE.sub("[REDACTED_SECRET]", redacted)
    redacted = _UNDERSCORE_GATEWAY_KEY_RE.sub("[REDACTED_SECRET]", redacted)
    redacted = _AWS_ACCESS_KEY_RE.sub("[REDACTED_SECRET]", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}[REDACTED_SECRET]",
        redacted,
    )
    return _JSON_SECRET_FIELD_RE.sub(
        lambda match: f'"{match.group(1)}":"[REDACTED_SECRET]"',
        redacted,
    )


def is_sensitive_field_name(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    return normalized in _SENSITIVE_FIELD_NAMES


def redact_json_secrets(
    value: Any,
    *,
    preserve_data_urls: bool = False,
    max_depth: int = 32,
    _depth: int = 0,
) -> Any:
    """Recursively redact public JSON while preserving its useful structure.

    Credential-shaped and runtime-ownership fields are omitted by exact field
    name. Ordinary strings are pattern-redacted, and non-JSON values are
    converted to redacted strings rather than reaching a serializer by
    accident.
    """

    if _depth > max_depth:
        return "[public value omitted: nesting limit exceeded]"
    if isinstance(value, str):
        if preserve_data_urls and value[:5].casefold() == "data:":
            return value
        return redact_secrets(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [
            redact_json_secrets(
                item,
                preserve_data_urls=preserve_data_urls,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if is_sensitive_field_name(raw_key):
                continue
            key = redact_secrets(str(raw_key))
            result[key] = redact_json_secrets(
                item,
                preserve_data_urls=preserve_data_urls,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        return result
    return redact_secrets(str(value))


__all__ = [
    "is_sensitive_field_name",
    "redact_json_secrets",
    "redact_secrets",
]
