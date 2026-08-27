from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from backend.tools.base import PermissionLevel
from backend.permissions.profiles import WorkspaceScope

if TYPE_CHECKING:
    from backend.agent.turn_diff_tracker import TurnDiffTracker
    from backend.sandbox.policy import SandboxPolicy
    from backend.tasks.manager import TaskManager
    from backend.terminal.manager import BackgroundCommandManager
    from backend.permissions.checker import PermissionChecker

PermissionMode = Literal[
    "plan",
    "confirm",
    "bypass",
    "auto",
]
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
    # SHA-256 over the provider-neutral final tool name + canonical args.
    # Every approval and execution boundary binds to this exact request.
    request_digest: str = ""


@dataclass(frozen=True)
class PermissionContext:
    """Structured permission state carried through one runtime/session."""

    mode: PermissionMode = "confirm"
    session_overrides: dict[str, PermissionLevel] = field(default_factory=dict)
    # Command prompt grants are stored in the session permission context,
    # separate from literal command-content rules. Evaluation is
    # classifier-backed when the host exposes that canonical capability.
    command_prompt_allow_rules: tuple[str, ...] = ()
    tool_deny_rules: list[str] = field(default_factory=list)
    filesystem_constraints: dict[str, list[str]] = field(default_factory=dict)
    workspace_scope: WorkspaceScope = "project"
    source: str = "runtime"
    # Preserve the exact mode that was active before plan mode. It travels with
    # the live context so plan exit never trusts stale run metadata or invents
    # a lifecycle state.
    pre_plan_mode: PermissionMode | None = None
    approval_policy: str = "on-request"
    sandbox_mode: str = ""
    requirements_source: str = ""
    allow_unsandboxed_commands: bool = True
    sandbox_fail_if_unavailable: bool = True
    sandbox_auto_allow_commands: bool = False
    sandbox_excluded_commands: tuple[str, ...] = ()
    # Immutable owner for one live turn/session. Permission checks must not
    # infer cross-conversation capabilities from process-global paths.
    conversation_id: str = ""
    workspace_root: Path | None = None

    def __post_init__(self) -> None:
        # frozen=True prevents field replacement but does not protect nested
        # caller-owned containers.  Detach them at the turn boundary so a
        # policy update or subagent cannot mutate another turn's context via a
        # shared dict/list reference.
        object.__setattr__(self, "session_overrides", deepcopy(self.session_overrides))
        object.__setattr__(self, "tool_deny_rules", deepcopy(self.tool_deny_rules))
        object.__setattr__(self, "filesystem_constraints", deepcopy(self.filesystem_constraints))


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
    sandbox_policy: "SandboxPolicy | None" = None
    task_manager: "TaskManager | None" = None  # Requirement 6.1: first-class field
    background_manager: "BackgroundCommandManager | None" = None
    terminal_manager: Any | None = None
    checkpoint_manager: Any | None = None
    permission_checker: "PermissionChecker | None" = None
    conversation_id: str = ""
    llm: Any | None = None  # LLMAdapter for tools that need model calls (e.g. web_fetch prompt extraction)
    artifact_store: Any | None = None
    # One aggregate diff tracker is owned by the turn. File mutation tools feed
    # exact before/after content into it and emit authoritative snapshots.
    turn_diff_tracker: "TurnDiffTracker | None" = None
    # Absolute monotonic deadline owned by the enclosing turn supervisor.
    # Tool-level timeouts are capped to this value so a tool started near the
    # end of a turn cannot silently extend the whole turn by minutes.
    deadline_monotonic: float | None = None
    # Explicit owner for cancellation-resistant tool tasks.  A tool may have
    # already produced its user-visible timeout/cancel result while its
    # coroutine is still unwinding side effects; the enclosing turn/session
    # retains these tasks until they settle instead of relying on an anonymous
    # done callback.
    pending_cleanup_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    # Per-call cleanup evidence is shared with the parallel batch coordinator
    # so a cancelled wrapper cannot replace a concrete call's receipt with a
    # batch-wide fallback result.
    cleanup_receipts: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    cleanup_tasks_by_call: dict[str, asyncio.Task[Any]] = field(default_factory=dict, repr=False)
