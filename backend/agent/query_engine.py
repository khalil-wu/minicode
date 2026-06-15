from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.run_events import should_emit_event
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.tasks.manager import TaskManager


@dataclass(slots=True)
class QuerySubmission:
    user_message: str
    llm: LLMAdapter
    tool_registry: ToolRegistry
    artifact_store: ArtifactStore
    permission_checker: PermissionChecker
    agent_settings: AgentSettings
    token_budget: TokenBudget
    context_builder: ContextBuilder | None = None
    approval_handler: Callable[[str], Any] | None = None
    skill_manager: Any | None = None
    vector_memory: Any | None = None
    state: AgentState | None = None
    permission_context: PermissionContext | None = None
    workspace_root: Path | None = None
    session_id: str = ""
    task_id: str = ""
    task_manager: TaskManager | None = None
    background_manager: Any | None = None
    terminal_manager: Any | None = None
    emit_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    metadata: dict[str, Any] | None = None
    stream_callback: Callable[..., Awaitable[None]] | None = None

    def to_session_context(self) -> AgentLoopSessionContext:
        return AgentLoopSessionContext(
            skill_manager=self.skill_manager,
            vector_memory=self.vector_memory,
            permission_context=self.permission_context,
            workspace_root=self.workspace_root,
            session_id=self.session_id,
            task_id=self.task_id,
            task_manager=self.task_manager,
            background_manager=self.background_manager,
            terminal_manager=self.terminal_manager,
            emit_event=self.emit_event,
            metadata=dict(self.metadata or {}),
            stream_callback=self.stream_callback,
        )


class QueryEngine:
    """Stable entry point for a single user-query lifecycle."""

    def __init__(self, runner: Callable[..., AsyncIterator[AgentEvent]] | None = None) -> None:
        self._runner = runner or run_agent_loop

    async def submit(self, submission: QuerySubmission) -> AsyncIterator[AgentEvent]:
        permission_checker = submission.permission_checker.with_workspace_root(submission.workspace_root)
        async for event in self._runner(
            user_message=submission.user_message,
            llm=submission.llm,
            tool_registry=submission.tool_registry,
            artifact_store=submission.artifact_store,
            permission_checker=permission_checker,
            agent_settings=submission.agent_settings,
            token_budget=submission.token_budget,
            context_builder=submission.context_builder,
            approval_handler=submission.approval_handler,
            state=submission.state,
            session_context=submission.to_session_context(),
        ):
            yield event

    async def submit_filtered(self, submission: QuerySubmission) -> AsyncIterator[AgentEvent]:
        """Submit one query and emit the filtered event stream.

        Adapter-only events (``tool_call_start``, ``tool_call_delta``) and
        tool-lifecycle progress noise are silently dropped so that the UI
        receives only stable, meaningful events.
        """
        async for event in self.submit(submission):
            if should_emit_event(event):
                yield event
