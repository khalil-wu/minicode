from __future__ import annotations

from typing import Any


def normalize_skill_name(value: Any) -> str:
    """Return a usable skill name, or an empty string for invalid input."""
    if not isinstance(value, str):
        return ""
    return value.strip()
