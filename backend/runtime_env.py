"""Environment helpers for subprocess boundaries."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_SENSITIVE_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
}

_SENSITIVE_ENV_SUFFIXES = (
    "_API_KEY",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_SECRET",
)


def sanitized_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    allow: set[str] | None = None,
) -> dict[str, str]:
    """Return an env copy safe to hand to user-controlled subprocesses.

    MiniCode keeps provider credentials in process memory/settings for direct
    LLM calls. Hooks, terminals, preview servers, and MCP child processes should
    not inherit those credentials by accident.
    """
    allowed = allow or set()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or not _is_sensitive_env_name(key)
    }
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def sanitized_git_env(cwd: str | Path | None = None) -> dict[str, str]:
    """Return a sanitized environment for git subprocesses."""
    env = sanitized_subprocess_env()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    if cwd is not None:
        try:
            root = Path(cwd).resolve()
            env["GIT_CEILING_DIRECTORIES"] = str(root.parent)
        except OSError:
            pass
    return env


def _is_sensitive_env_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _SENSITIVE_ENV_NAMES or normalized.endswith(_SENSITIVE_ENV_SUFFIXES)
