"""
State objects for WebSocketSession to reduce god-object complexity.

Each state object manages a specific concern:
- ConnectionState: WebSocket connection lifecycle
- ConversationState: Active conversation and run tasks
- StreamState: Streaming response state per conversation
- WorkspaceState: Workspace root and git integration
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from git import Repo
    from starlette.websockets import WebSocket

    from backend.conversations.repository import ConversationRepository, ConversationRecord


@dataclass
class ConnectionState:
    """Manages WebSocket connection lifecycle."""

    ws: WebSocket
    generation: int  # Connection generation for stale event detection
    disconnect_event: asyncio.Event = field(default_factory=asyncio.Event)

    def is_connected(self) -> bool:
        """Check if connection is still active."""
        return not self.disconnect_event.is_set()

    def mark_disconnected(self) -> None:
        """Mark connection as disconnected."""
        self.disconnect_event.set()


@dataclass
class ConversationState:
    """Manages active conversation and run tasks."""

    repo: ConversationRepository
    active_id: str | None = None
    run_tasks: dict[str, asyncio.Task] = field(default_factory=dict)  # conversation_id -> task
    run_task_ids: dict[str, str] = field(default_factory=dict)  # conversation_id -> task_id

    def get_active_conversation(self) -> ConversationRecord | None:
        """Get the currently active conversation record."""
        if not self.active_id:
            return None
        return self.repo.get_conversation(self.active_id)

    def switch_to(self, conversation_id: str) -> None:
        """Switch active conversation."""
        self.active_id = conversation_id

    def get_run_task(self, conversation_id: str) -> asyncio.Task | None:
        """Get the run task for a conversation."""
        return self.run_tasks.get(conversation_id)

    def set_run_task(self, conversation_id: str, task: asyncio.Task, task_id: str) -> None:
        """Set the run task for a conversation."""
        self.run_tasks[conversation_id] = task
        self.run_task_ids[conversation_id] = task_id

    def clear_run_task(self, conversation_id: str) -> None:
        """Clear the run task for a conversation."""
        self.run_tasks.pop(conversation_id, None)
        self.run_task_ids.pop(conversation_id, None)


@dataclass
class ConversationStream:
    """Stream state for a single conversation."""

    message_id: str | None = None
    accumulated_text: str = ""
    tool_calls: dict = field(default_factory=dict)  # index -> tool_call

    def clear(self) -> None:
        """Clear all stream state."""
        self.message_id = None
        self.accumulated_text = ""
        self.tool_calls.clear()


@dataclass
class StreamState:
    """Manages streaming response state per conversation."""

    streams: dict[str, ConversationStream] = field(default_factory=dict)  # conversation_id -> stream

    def get_stream(self, conversation_id: str) -> ConversationStream:
        """Get stream for a conversation, creating if needed."""
        if conversation_id not in self.streams:
            self.streams[conversation_id] = ConversationStream()
        return self.streams[conversation_id]

    def clear_stream(self, conversation_id: str) -> None:
        """Clear stream state for a conversation."""
        if conversation_id in self.streams:
            self.streams[conversation_id].clear()

    def remove_stream(self, conversation_id: str) -> None:
        """Remove stream for a conversation."""
        self.streams.pop(conversation_id, None)


@dataclass
class WorkspaceState:
    """Manages workspace root and git integration."""

    root: Path
    default_root: Path
    _git_repo: Repo | None = field(default=None, init=False)

    def get_git_repo(self) -> Repo | None:
        """Get git repo for current workspace, lazy loading."""
        if self._git_repo is None:
            git_dir = self.root / ".git"
            if git_dir.exists():
                try:
                    from git import Repo
                    self._git_repo = Repo(self.root)
                except Exception:
                    pass
        return self._git_repo

    def get_git_branch(self) -> str:
        """Get current git branch name."""
        repo = self.get_git_repo()
        if repo and not repo.head.is_detached:
            return repo.active_branch.name
        return ""

    def switch_to(self, new_root: Path) -> None:
        """Switch to a new workspace root."""
        self.root = new_root.resolve()
        self._git_repo = None  # Clear cached repo

    def reset_to_default(self) -> None:
        """Reset to default workspace."""
        self.switch_to(self.default_root)
