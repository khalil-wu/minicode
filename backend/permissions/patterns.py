"""Shared normalization for user/session permission pattern lists."""

from __future__ import annotations

from typing import Any
from collections.abc import Iterable
from fnmatch import fnmatchcase
from urllib.parse import quote
import re


def mcp_policy_identity(server_name: str, tool_name: str) -> str:
    """A durable raw pair; the model's flattened wire name is not authority."""
    return f"mcp::{quote(server_name, safe='')}/{quote(tool_name, safe='')}"


def legacy_mcp_allow_aliases(server_name: str, tool_name: str) -> tuple[str, ...]:
    # Old normalized/flattened names containing underscores could have been
    # authored for punctuation or a different server/tool split. They require
    # a new explicit grant; guessing from today's catalog reassigns consent.
    if re.fullmatch(r"[A-Za-z0-9-]+", server_name) and re.fullmatch(r"[A-Za-z0-9-]+", tool_name):
        return (f"mcp__{server_name}__{tool_name}",)
    return ()


def legacy_mcp_allow_warning(pattern: str) -> str:
    if not pattern.startswith("mcp__") or re.fullmatch(r"mcp__[A-Za-z0-9-]+__[A-Za-z0-9-]+", pattern):
        return ""
    return (
        f"Legacy MCP allow {pattern!r} does not establish an exact raw tool permission. "
        "Ambiguous normalized or flattened aliases are not reused for consent; "
        "reauthorize the actual tool using its mcp::server/tool identity."
    )


def legacy_mcp_allow_warnings(patterns: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        warning for pattern in patterns
        if (warning := legacy_mcp_allow_warning(pattern.split("(", 1)[0].strip()))
    ))


def tool_policy_pattern_matches(name: str, pattern: str) -> bool:
    pattern = pattern.strip()
    if fnmatchcase(name, pattern):
        return True
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return False
    if pattern.startswith("mcp::") and "/" not in pattern:
        return name.startswith(pattern + "/")
    return pattern.startswith("mcp__") and name.startswith(pattern + "__")


def normalize_tool_patterns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        pattern = str(item or "").strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        normalized.append(pattern)
    return normalized
