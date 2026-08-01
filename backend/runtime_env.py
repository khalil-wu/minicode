"""Environment helpers for subprocess boundaries."""
from __future__ import annotations

import logging
import os
import sys
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
    "_TOKEN",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_SECRET",
    "_SECRET_KEY",
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
    """Return a sanitized environment for git subprocesses.

    Drops inherited GIT_DIR/GIT_WORK_TREE so git resolves the repo from cwd.
    Repo discovery may ascend (a workspace that is a subdirectory of a repo
    must find its enclosing repository) but stops at the user's home directory
    so a stray ~/.git never masquerades as the workspace repo.
    """
    env = sanitized_subprocess_env()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    try:
        env["GIT_CEILING_DIRECTORIES"] = str(Path.home())
    except OSError:
        pass
    return env


def _is_sensitive_env_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _SENSITIVE_ENV_NAMES or normalized.endswith(_SENSITIVE_ENV_SUFFIXES)


_console_utf8_configured = False


def ensure_utf8_console_logging(level: int = logging.INFO) -> None:
    """Force UTF-8 stdout/stderr and a UTF-8 log handler. Idempotent.

    On Windows the default console codepage (e.g. cp936) garbles the Chinese log
    messages this codebase emits, even though the underlying strings are valid
    UTF-8. Reconfiguring the streams to UTF-8 fixes the display without touching
    any data. Safe to call once at process startup.
    """
    global _console_utf8_configured
    if _console_utf8_configured:
        return
    _console_utf8_configured = True

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    root = logging.getLogger()
    # Only install our own handler when nothing else has — otherwise we would
    # double-log alongside a pre-existing handler (e.g. rich/uvicorn). The
    # stream reconfigure above is what actually fixes mojibake; an existing
    # handler writing to the now-UTF-8 stream renders Chinese correctly too.
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
