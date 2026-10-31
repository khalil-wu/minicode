"""Small shared coercions used at hook configuration and execution boundaries."""

from __future__ import annotations

import json
from typing import Any


def parse_json_object(value: str) -> dict[str, Any] | None:
    stripped = value.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped, parse_constant=_reject_json_constant)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default
