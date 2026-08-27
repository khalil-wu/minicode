"""Provider-neutral content projections owned by the MiniCode core."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def normalise_content(value: Any) -> str:
    """Project structured tool/provider content into MiniCode text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if value.get("type") == "text" and value.get("text") is not None:
            return str(value.get("text"))
        return str(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = []
        for item in value:
            if isinstance(item, Mapping) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(value)


__all__ = ["normalise_content"]
