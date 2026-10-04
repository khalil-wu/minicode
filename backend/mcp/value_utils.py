"""Shared MCP configuration value predicates."""

from __future__ import annotations

import os
import re
from typing import Any


def has_nonempty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return bool(value)
    return True


def resolve_env_placeholders(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    # Supports ${VAR} and ${VAR:-default}. Missing variables without an explicit
    # default are configuration errors at the configuration admission boundary.
    pattern = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)(?::-([^}]*))?\}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if default is not None:
            return os.getenv(name, default)
        if name not in os.environ:
            raise ValueError(f"MCP configuration references missing environment variable '{name}'")
        return os.environ[name]

    return pattern.sub(replace, value)
