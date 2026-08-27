"""Conversation persistence primitives for chat sessions."""

from typing import Any

from .models import ConversationRecord, ConversationSummary


def __getattr__(name: str) -> Any:
    # ``turn_state`` uses the public projection submodule, while the repository
    # imports ``turn_state`` for block helpers. Loading repository eagerly from
    # the package initializer creates a cycle for every projection consumer.
    if name in {"CONVERSATION_DATA_DIR", "ConversationRepository"}:
        from .repository import CONVERSATION_DATA_DIR, ConversationRepository

        return {
            "CONVERSATION_DATA_DIR": CONVERSATION_DATA_DIR,
            "ConversationRepository": ConversationRepository,
        }[name]
    raise AttributeError(name)

__all__ = [
    "CONVERSATION_DATA_DIR",
    "ConversationRecord",
    "ConversationRepository",
    "ConversationSummary",
]
