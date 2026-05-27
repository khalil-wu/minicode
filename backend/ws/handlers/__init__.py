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

HANDLERS: dict[str, Callable[..., Any]] = {
    **_terminal,
    **_mcp,
    **_session,
    **_workspace,
    **_conversation,
    **_misc,
    **_diff,
    **_preview,
}


def register_domain_handlers(session: "WebSocketSession") -> None:
    for cmd, fn in HANDLERS.items():
        session.command_registry.register(cmd, partial(fn, session))
