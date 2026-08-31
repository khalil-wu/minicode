"""Git tool helpers.

Extracted from ``backend/tools/git_tools.py`` so subprocess communication and
path/deny checks are independent of the tool classes.
"""

from __future__ import annotations

import logging

from backend.subprocesses import communicate_bounded
from pathlib import Path
from typing import Any
import asyncio


logger = logging.getLogger(__name__)


_GIT_TRANSPORT_LIMIT_BYTES = 20 * 1024 * 1024
async def _communicate_git(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    return await communicate_bounded(
        proc,
        stdout_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
        stderr_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
    )


def _raise_if_cancelled(context: Any) -> None:
    cancel_event = getattr(context, "cancel_event", None) if context is not None else None
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError


def _workspace_root(context: Any, fallback: Path | None) -> Path | None:
    if context and getattr(context, "workspace_root", None):
        return Path(context.workspace_root).resolve()
    return fallback.resolve() if fallback is not None else None


def _resolve_work_dir(root: Path, path_value: Any) -> Path:
    raw = str(path_value or ".").strip() or "."
    resolved = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    # Confine to the workspace: an absolute or ../-escaping path must not let a
    # read-only AUTO tool inspect arbitrary repos on the host. Escapes fall back
    # to the workspace root rather than running git in an unrelated directory.
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return root_resolved
    return resolved


def _is_denied_path(context: Any, file_path: str) -> bool:
    checker = getattr(context, "permission_checker", None) if context is not None else None
    if checker is None:
        return False
    permission = getattr(context, "permission", None)
    try:
        return not checker.is_path_allowed(str(file_path), context=permission)
    except Exception:
        return False


