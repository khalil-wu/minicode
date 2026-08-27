"""Flat command handler dispatch for WebSocket sessions.

Each domain module exports a HANDLERS dict mapping command names to
standalone async functions with signature: (session, data) -> bool.
"""
from __future__ import annotations

from functools import partial
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

from backend.ws.handlers.terminal import HANDLERS as _terminal
from backend.ws.handlers.mcp import HANDLERS as _mcp
from backend.ws.handlers.session import HANDLERS as _session
from backend.ws.handlers.workspace import HANDLERS as _workspace
from backend.ws.handlers.conversation import HANDLERS as _conversation
from backend.ws.handlers.misc import HANDLERS as _misc
from backend.ws.handlers.diff import HANDLERS as _diff
from backend.ws.handlers.preview import HANDLERS as _preview

def _merge_domain_handlers(
    domains: tuple[tuple[str, dict[str, Callable[..., Any]]], ...],
) -> dict[str, Callable[..., Any]]:
    """Flatten the domain tables, refusing to let one shadow another.

    A duplicated command name would silently give one dispatcher two owners and
    the merge order would decide which one runs.
    """
    merged: dict[str, Callable[..., Any]] = {}
    owners: dict[str, str] = {}
    for domain, handlers in domains:
        for command, handler in handlers.items():
            if command in merged:
                raise RuntimeError(
                    f"WebSocket command '{command}' is registered by both "
                    f"'{owners[command]}' and '{domain}'"
                )
            merged[command] = handler
            owners[command] = domain
    return merged


HANDLERS: dict[str, Callable[..., Any]] = _merge_domain_handlers(
    (
        ("terminal", _terminal),
        ("mcp", _mcp),
        ("session", _session),
        ("workspace", _workspace),
        ("conversation", _conversation),
        ("misc", _misc),
        ("diff", _diff),
        ("preview", _preview),
    )
)


def register_domain_handlers(session: "WebSocketSession") -> None:
    for cmd, fn in HANDLERS.items():
        session.command_registry.register(cmd, partial(fn, session))
