from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

CommandHandler = Callable[[dict[str, Any]], Awaitable[bool | None]]
SlashCommandHandler = Callable[['WebSocketSession', str, Any], Awaitable[bool | tuple[bool, str] | None]]
logger = logging.getLogger(__name__)


class CommandRegistry:
    """Async command dispatcher for websocket/system commands."""

    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}
        self._slash_handlers: dict[str, SlashCommandHandler] = {}
        # MiniCode keeps extension commands on the session-owned lifecycle
        # runner and checks them before built-in/template commands. Keep that
        # layering scoped to the owning conversation instead of flattening
        # extension commands into the protocol command map.
        self._extension_slash_handlers: dict[
            str, dict[str, tuple[SlashCommandHandler, dict[str, Any]]]
        ] = {}
        self._extension_slash_tokens: dict[str, object] = {}

    @staticmethod
    def _slash_name(name: str) -> str:
        normalized = str(name or "").strip().lower()
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized

    @staticmethod
    def _slash_scope(scope_id: str | None) -> str:
        return str(scope_id or "*").strip() or "*"

    def register(self, name: str, handler: CommandHandler) -> None:
        if name in self._handlers:
            logger.warning("Command name conflict detected for '%s'; overriding previous registration", name)
        self._handlers[name] = handler

    def register_slash(self, name: str, handler: SlashCommandHandler) -> None:
        name = self._slash_name(name)
        if name in self._slash_handlers:
            # cc resolves commands by first match; an earlier (more specific,
            # e.g. project-scoped) registration must not be displaced.
            return
        self._slash_handlers[name] = handler

    def clear_slash_handlers(self) -> None:
        """Clear host/catalog commands without disturbing extension scopes."""

        self._slash_handlers.clear()

    def replace_extension_slash_handlers(
        self,
        scope_id: str,
        registrations: Mapping[
            str, tuple[SlashCommandHandler, Mapping[str, Any] | None]
        ],
    ) -> object:
        """Atomically replace one MiniCode session command projection."""

        scope = self._slash_scope(scope_id)
        resolved: dict[str, tuple[SlashCommandHandler, dict[str, Any]]] = {}
        for raw_name, registration in registrations.items():
            handler, metadata = registration
            name = self._slash_name(raw_name)
            resolved[name] = (handler, dict(metadata or {}))
        token = object()
        self._extension_slash_handlers[scope] = resolved
        self._extension_slash_tokens[scope] = token
        return token

    def clear_extension_slash_handlers(
        self,
        scope_id: str,
        *,
        token: object | None = None,
    ) -> bool:
        """Remove a scoped projection only when it is still owned by ``token``."""

        scope = self._slash_scope(scope_id)
        if token is not None and self._extension_slash_tokens.get(scope) is not token:
            return False
        removed = self._extension_slash_handlers.pop(scope, None) is not None
        self._extension_slash_tokens.pop(scope, None)
        return removed

    def _extension_scope_handlers(
        self, scope_id: str | None
    ) -> dict[str, tuple[SlashCommandHandler, dict[str, Any]]]:
        scope = self._slash_scope(scope_id)
        if scope in self._extension_slash_handlers:
            return self._extension_slash_handlers[scope]
        return self._extension_slash_handlers.get("*", {})

    def get_slash(
        self, name: str, *, scope_id: str | None = None
    ) -> SlashCommandHandler | None:
        normalized = self._slash_name(name)
        extension = self._extension_scope_handlers(scope_id).get(normalized)
        if extension is not None:
            return extension[0]
        return self._slash_handlers.get(normalized)

    def list_extension_slash_commands(
        self, *, scope_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            dict(metadata)
            for _handler, metadata in self._extension_scope_handlers(scope_id).values()
        ]

    def dispatch_slash_sync(self, name: str, *, scope_id: str | None = None) -> bool:
        return self.get_slash(name, scope_id=scope_id) is not None

    async def dispatch_slash(
        self,
        handler_obj: 'WebSocketSession',
        name: str,
        arg: str,
        attachments: Any,
        *,
        scope_id: str | None = None,
    ) -> tuple[bool, str]:
        handler = self.get_slash(name, scope_id=scope_id)
        if handler is None:
            return False, arg
        result = await handler(handler_obj, arg, attachments)

        if isinstance(result, tuple):
            return result[0], result[1]

        return bool(True if result is None else result), arg

    def unregister(self, name: str) -> bool:
        return self._handlers.pop(name, None) is not None

    def get(self, name: str) -> CommandHandler | None:
        return self._handlers.get(name)

    def list_commands(self) -> list[str]:
        return list(self._handlers.keys())

    async def dispatch(self, name: str, payload: dict[str, Any]) -> bool:
        handler = self._handlers.get(name)
        if handler is None:
            # Extension slash commands are dispatched only through
            # ``dispatch_slash``, which carries the owning scope and the session
            # object. Running them from here would drop both.
            return False
        result = handler(payload)
        if inspect.isawaitable(result):
            result = await result
        return bool(True if result is None else result)
