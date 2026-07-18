from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.ws.handler import ConversationWebSocketHandler

CommandHandler = Callable[[dict[str, Any]], Awaitable[bool | None]]
SlashCommandHandler = Callable[['ConversationWebSocketHandler', str, Any], Awaitable[bool | tuple[bool, str] | None]]
logger = logging.getLogger(__name__)


class CommandRegistry:
    """Async command dispatcher for websocket/system commands."""

    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}
        self._slash_handlers: dict[str, SlashCommandHandler] = {}

    def register(self, name: str, handler: CommandHandler) -> None:
        if name in self._handlers:
            logger.warning("Command name conflict detected for '%s'; overriding previous registration", name)
        self._handlers[name] = handler

    def register_slash(self, name: str, handler: SlashCommandHandler) -> None:
        if not name.startswith("/"):
            name = f"/{name}"
        if name in self._slash_handlers:
            logger.warning("Slash command name conflict for '%s'", name)
        self._slash_handlers[name] = handler

    def clear_slash_handlers(self) -> None:
        self._slash_handlers.clear()

    def dispatch_slash_sync(self, name: str) -> bool:
        return name in self._slash_handlers

    async def dispatch_slash(self, handler_obj: 'ConversationWebSocketHandler', name: str, arg: str, attachments: Any) -> tuple[bool, str]:
        handler = self._slash_handlers.get(name)
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
            return False
        result = await handler(payload)
        return bool(True if result is None else result)
