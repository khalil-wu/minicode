"""Explicit owners for one live agent run.

``metadata`` is still used for transport and durable projection fields.  The
objects in this container are process-local capabilities and must not be
looked up by string keys after the turn boundary has been assembled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.agent.execution_journal import ExecutionJournal


@dataclass(slots=True)
class RunContext:
    """Mutable, turn-owned runtime capabilities shared by one agent run."""

    lifecycle_runtime: Any | None = None
    execution_journal: ExecutionJournal | None = None
    mcp_manager: Any | None = None
    mcp_owner_session_id: str = ""
    subagent_parent_runtime: dict[str, Any] = field(default_factory=dict)
    extension_thinking_level: str = ""
    turn_model_snapshot: dict[str, Any] = field(default_factory=dict)
    agent_runtime: Any | None = None
    llm_turn_context: Any | None = None
    hook_manager: Any | None = None
    workspace_context: Any | None = None
    cost_session_id: str = ""
    requires_explicit_workspace: bool = False
    connected_mcp_servers: tuple[str, ...] = ()
    permission_mode_setter: Callable[..., Any] | None = None
    permission_context_provider: Callable[..., Any] | None = None
    command_prompt_allow_rules_setter: Callable[..., Any] | None = None
    teammate_plan_approval_requester: Callable[..., Any] | None = None
    conversation_repository: Any | None = None
    turn_input_queue: Any | None = None
    persist_consumed_turn_input: Callable[..., Any] | None = None
    acknowledge_consumed_turn_input: Callable[..., Any] | None = None
    previous_turn_aborted: bool = False
    toolset_policy: Any | None = None
    session_toolset_policy: Any | None = None

__all__ = ["RunContext"]
