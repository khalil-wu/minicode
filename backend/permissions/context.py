from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from backend.tools.base import PermissionLevel
from backend.permissions.profiles import WorkspaceScope

if TYPE_CHECKING:
    from backend.tasks.manager import TaskManager
    from backend.terminal.manager import BackgroundCommandManager
    from backend.permissions.checker import PermissionChecker

PermissionMode = Literal["default", "plan", "confirm", "bypass", "auto", "accept_edits"]
EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class PermissionDecision:
    permission_level: PermissionLevel
    decision: Literal["allow", "ask", "deny"]
    capability_allowed: bool
    capability_reason: str
    approval_policy: Literal["auto", "confirm", "diff_review", "deny"]
    matched_rule_source: str
    matched_rule: str
    risk: Literal["low", "medium", "high", "critical"]
    scope: dict[str, Any]
    expiry: Literal["call", "session", "policy"]


@dataclass(frozen=True)
class PermissionContext:
    """Structured permission state carried through one runtime/session."""

    mode: PermissionMode = "default"
    session_overrides: dict[str, PermissionLevel] = field(default_factory=dict)
    tool_deny_rules: list[str] = field(default_factory=list)
    filesystem_constraints: dict[str, list[str]] = field(default_factory=dict)
    workspace_scope: WorkspaceScope = "project"
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
    approval_handler: Callable[[str], Any] | None = None
    stream_callback: Callable[..., Awaitable[None]] | None = None
    workspace_root: Path | None = None  # Workspace root directory for path resolution
    allow_network: bool = False
    task_manager: "TaskManager | None" = None  # Requirement 6.1: first-class field
    background_manager: "BackgroundCommandManager | None" = None
    terminal_manager: Any | None = None
    checkpoint_manager: Any | None = None
    permission_checker: "PermissionChecker | None" = None
    conversation_id: str = ""
    llm: Any | None = None  # LLMAdapter for tools that need model calls (e.g. web_fetch prompt extraction)
    artifact_store: Any | None = None
    # Absolute monotonic deadline owned by the enclosing turn supervisor.
    # Tool-level timeouts are capped to this value so a tool started near the
    # end of a turn cannot silently extend the whole turn by minutes.
    deadline_monotonic: float | None = None
