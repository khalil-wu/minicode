"""Canonical filesystem roots owned by the MiniCode runtime."""

from __future__ import annotations

import os
from pathlib import Path


def agent_runtime_root(base_dir: Path | None = None) -> Path:
    """Return the durable root for agent-owned runtime state.

    ``base_dir`` is an exact test/caller override. ``MINICODE_STATE_ROOT`` is
    the application state root and receives the standard ``data/agent-runtime``
    suffix. Without either override, MiniCode owns state below ``~/.minicode``.
    """

    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    configured = str(os.environ.get("MINICODE_STATE_ROOT") or "").strip()
    state_root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".minicode"
    )
    return state_root.resolve() / "data" / "agent-runtime"
