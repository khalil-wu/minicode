"""Shared numeric projections for provider and public payloads."""

from __future__ import annotations

import math
from typing import Any


def nonnegative_int(value: Any, *, maximum: int | float | None = None) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    if maximum is not None and numeric > maximum:
        return None
    return int(numeric)


def finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return int(numeric) if numeric.is_integer() else numeric
