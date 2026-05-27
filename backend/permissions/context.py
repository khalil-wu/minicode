from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from backend.tools.base import PermissionLevel

if TYPE_CHECKING:
    from backend.tasks.manager import TaskManager
    from backend.terminal.manager import BackgroundCommandManager

PermissionMode = Literal["default", "plan", "confirm", "bypass", "auto", "accept_edits"]
EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class PermissionContext:
    """Structured permission state carried through one runtime/session."""

    mode: PermissionMode = "default"
    session_overrides: dict[str, PermissionLevel] = field(default_factory=dict)
    tool_deny_rules: list[str] = field(default_factory=list)
    filesystem_constraints: dict[str, list[str]] = field(default_factory=dict)
    source: str = "runtime"


@dataclass
class ToolExecutionContext:
    """Execution context shared with tools and the orchestration runtime."""

    permission: PermissionContext
    session_id: str = ""
    task_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    cancel_event: asyncio.Event | None = None
    emit_event: EventEmitter | None = None
    stream_callback: Callable[[str], Awaitable[None]] | None = None
    workspace_root: Path | None = None  # Workspace root directory for path resolution
    task_manager: "TaskManager | None" = None  # Requirement 6.1: first-class field
    background_manager: "BackgroundCommandManager | None" = None
    checkpoint_manager: Any | None = None
    conversation_id: str = ""
