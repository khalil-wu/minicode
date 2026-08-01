from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.event_envelope import EventEnvelope
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.loop_session import prepare_turn_state
from backend.agent.iteration_budget import resolve_turn_max_iterations
from backend.agent.run_events import should_emit_event
from backend.agent.runtime import AgentRunStatus
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.state import AgentState
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.turn_input import TurnInputQueue
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentSession:
    """Long-lived dependencies shared by query turns in one agent session."""

    llm: LLMAdapter
    tool_registry: ToolRegistry
    artifact_store: ArtifactStore
    permission_checker: PermissionChecker
    agent_settings: AgentSettings
    token_budget: TokenBudget
    context_builder: ContextBuilder | None = None
    approval_handler: Callable[[str], Any] | None = None


@dataclass(slots=True)
class QuerySubmission:
    """One turn's input and runtime overrides."""

    user_message: str
    session: AgentSession | None = None
    state: AgentState | None = None
    runtime: AgentLoopSessionContext = field(default_factory=AgentLoopSessionContext)
    # Compatibility fields for callers that predate AgentSession.  Keeping
    # these here lets SDK extensions and older integrations migrate without
    # bypassing QueryEngine's lifecycle owner.
    llm: LLMAdapter | None = None
    tool_registry: ToolRegistry | None = None
    artifact_store: ArtifactStore | None = None
    permission_checker: PermissionChecker | None = None
    agent_settings: AgentSettings | None = None
    token_budget: TokenBudget | None = None
    context_builder: ContextBuilder | None = None
    approval_handler: Callable[[str], Any] | None = None
    workspace_root: Any | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            missing = [
                name
                for name, value in (
                    ("llm", self.llm),
                    ("tool_registry", self.tool_registry),
                    ("artifact_store", self.artifact_store),
                    ("permission_checker", self.permission_checker),
                    ("agent_settings", self.agent_settings),
                    ("token_budget", self.token_budget),
                )
                if value is None
            ]
            if missing:
                raise TypeError(
                    "QuerySubmission requires session=AgentSession(...) or legacy fields: "
                    + ", ".join(missing)
                )
            self.session = AgentSession(
                llm=self.llm,
                tool_registry=self.tool_registry,
                artifact_store=self.artifact_store,
                permission_checker=self.permission_checker,
                agent_settings=self.agent_settings,
                token_budget=self.token_budget,
                context_builder=self.context_builder,
                approval_handler=self.approval_handler,
            )
        if self.workspace_root is not None and self.runtime.workspace_root is None:
            from pathlib import Path

            self.runtime.workspace_root = Path(self.workspace_root).resolve()


@dataclass(frozen=True, slots=True)
class QueryTurnContext:
    """Prepared state for a single query turn, built by QueryEngine._setup_query.

    This replaces the ad-hoc setup block that was previously the first ~80 lines
    of ``run_agent_loop``.  By owning the setup here, QueryEngine becomes the
    single lifecycle owner: callers go through ``submit`` → ``_setup_query`` →
    runner → ``_finalize_query``, and ``run_agent_loop`` is reduced to a pure
    ReAct iteration kernel.
    """

    user_message: str
    session: AgentSession
    state: AgentState
    metadata: dict[str, Any]
    settings: AgentSettings
    budget: TokenBudget
    context_builder: ContextBuilder
    skill_manager: Any | None = None
    permission_context: PermissionContext | None = None
    session_id: str = ""
    task_id: str = ""
    task_manager: Any | None = None
    background_manager: Any | None = None
    cancel_event: asyncio.Event | None = None
    stream_callback: Any | None = None
    emit_event: Any | None = None
    turn_kernel: TurnKernel | None = None
    startup_events: tuple[AgentEvent, ...] = ()


class QueryEngine:
    """Stable entry point for a single user-query lifecycle.

    Owns the setup → run → finalize lifecycle.  ``run_agent_loop`` is now an
    implementation detail accessed via ``_run_agent_loop``; all external callers
    should go through ``QueryEngine.submit``.
    """

    def __init__(self, runner: Callable[..., AsyncIterator[AgentEvent]] | None = None) -> None:
        # The runner is the private loop kernel.  External code should not
        # import or call it directly.
        self._runner = runner or run_agent_loop

    async def submit_filtered(self, submission: QuerySubmission) -> AsyncIterator[AgentEvent]:
        """Compatibility stream containing only the legacy public events.

        ``submit`` now includes durable run lifecycle events.  Older SDK
        consumers used ``submit_filtered`` and expect those bookkeeping
        records to stay internal, so retain that narrow adapter while routing
        all execution through the canonical lifecycle.
        """
        hidden = {"agent.run.started", "agent.run.completed"}
        async for event in self.submit(submission):
            if event.type not in hidden:
                yield event

    async def submit(self, submission: QuerySubmission) -> AsyncIterator[AgentEvent]:
        """Run one query and emit one public terminal event.

        Each turn-scoped event is stamped with an :class:`EventEnvelope`
        carrying ``task_id``, ``turn_id`` (captured from the first
        ``agent.run.started``) and a monotonic ``seq``.  This provides the
        stable correlation fields the frontend needs for canonical
        ``ActivityItem`` migration (plan §7.2, §20.4).
        """
        turn_ctx = self._setup_query(submission)
        session = turn_ctx.session
        conversation_id = str(
            getattr(turn_ctx.state, "conversation_id", "")
            or turn_ctx.metadata.get("conversation_id", "")
            or ""
        )
        envelope = EventEnvelope(
            task_id=turn_ctx.task_id,
            conversation_id=conversation_id,
        )
        runner = self._runner(
            user_message=turn_ctx.user_message,
            llm=session.llm,
            tool_registry=session.tool_registry,
            artifact_store=session.artifact_store,
            permission_checker=session.permission_checker,
            agent_settings=turn_ctx.settings,
            token_budget=turn_ctx.budget,
            context_builder=turn_ctx.context_builder,
            state=turn_ctx.state,
            approval_handler=session.approval_handler,
            skill_manager=turn_ctx.skill_manager,
            permission_context=turn_ctx.permission_context,
            session_id=turn_ctx.session_id,
            task_id=turn_ctx.task_id,
            task_manager=turn_ctx.task_manager,
            background_manager=turn_ctx.background_manager,
            stream_callback=turn_ctx.stream_callback,
            emit_event=turn_ctx.emit_event,
            metadata=turn_ctx.metadata,
            session_context=submission.runtime,
            turn_kernel=turn_ctx.turn_kernel,
            state_prepared=True,
            initial_max_iterations_limit=turn_ctx.state.max_iterations,
            turn_budget_controller=TurnBudgetController.from_settings(
                turn_ctx.settings,
                max_iterations=turn_ctx.state.max_iterations,
            ),
        )
        query_finalized = False
        try:
            if turn_ctx.turn_kernel is not None:
                for started_event in turn_ctx.turn_kernel.start_events():
                    envelope.stamp(started_event)
                    yield started_event
                for startup_event in turn_ctx.startup_events:
                    envelope.stamp(startup_event)
                    yield startup_event
        finally:
            if sys.exc_info()[0] is GeneratorExit and not query_finalized:
                self._finalize_query(turn_ctx, "cancelled", "consumer_closed")
                query_finalized = True
                close = getattr(runner, "aclose", None)
                if callable(close):
                    with suppress(Exception):
                        await close()
                if isinstance(submission.runtime.metadata, dict):
                    submission.runtime.metadata.update(turn_ctx.metadata)
        terminal_event: AgentEvent | None = None
        try:
            async for event in runner:
                if event.type in {"agent.run.started", "agent.run.completed"}:
                    continue
                if not should_emit_event(event):
                    continue
                if event.type == "done":
                    terminal_event = event
                    event_status = str(event.data.get("status") or "completed")
                    if event_status in {"completed", "partial", "cancelled", "failed"}:
                        turn_ctx.state.terminal_status = event_status
                    # Drain the generator: checkpoint persistence and terminal
                    # cleanup run after the loop yields its done event.
                    continue
                # Error events are evidence, not terminal authority. The
                # loop's explicit done status decides whether recovery
                # produced a completed or partial result.
                envelope.stamp(event)
                yield event
        except asyncio.CancelledError:
            terminal_event = AgentEvent.done(status="cancelled", reason="user_interrupted")
            completed = self._finalize_query(turn_ctx, "cancelled", "user_interrupted")
            query_finalized = True
            if completed is not None:
                envelope.stamp(completed)
                yield completed
            envelope.stamp(terminal_event)
            yield terminal_event
            raise
        except Exception:
            logger.exception("Agent runtime failed outside provider recovery")
            err_event = AgentEvent.error(
                "MiniCode internal runtime processing failed. The turn was stopped "
                "without retrying it as a model API error.",
                recoverable=False,
                error_type="runtime",
            )
            envelope.stamp(err_event)
            yield err_event
            terminal_event = AgentEvent.done(status="failed", reason="runtime_error")
        finally:
            if sys.exc_info()[0] is GeneratorExit and not query_finalized:
                self._finalize_query(turn_ctx, "cancelled", "consumer_closed")
                query_finalized = True
            close = getattr(runner, "aclose", None)
            if callable(close):
                with suppress(Exception):
                    await close()
            if isinstance(submission.runtime.metadata, dict):
                submission.runtime.metadata.update(turn_ctx.metadata)

        if terminal_event is None:
            reason = turn_ctx.state.stopped_reason
            has_usable_result = bool(turn_ctx.state.reply.strip())
            status = (
                turn_ctx.state.terminal_status
                if turn_ctx.state.terminal_status is not None
                else "cancelled"
                if reason == "interrupted"
                else "partial"
                if (
                    (reason in {"max_iterations", "max_tool_calls", "max_turn_seconds", "max_turn_tokens", "max_turn_cost_usd", "budget_exceeded", "incomplete_tool_stream"} and has_usable_result)
                    or str(reason or "").startswith(("partial_", "recovered_"))
                )
                else "completed"
                if reason in {None, "completed"}
                else "failed"
            )
            terminal_event = AgentEvent.done(status=status, reason=str(reason or ""))
        status = str(terminal_event.data.get("status") or "completed")
        reason = str(terminal_event.data.get("reason") or "")
        if status in {"completed", "partial", "cancelled", "failed"}:
            turn_ctx.state.terminal_status = status
        has_usable_result = bool(turn_ctx.state.reply.strip())
        if (
            reason in {"max_iterations", "max_tool_calls", "max_turn_seconds", "max_turn_tokens", "max_turn_cost_usd", "budget_exceeded", "incomplete_tool_stream"}
            and has_usable_result
            and status != "cancelled"
        ):
            status = "partial"
            terminal_event.data["status"] = "partial"
            turn_ctx.state.terminal_status = "partial"
        completed = self._finalize_query(turn_ctx, status, reason)
        query_finalized = True
        if completed is not None:
            envelope.stamp(completed)
            yield completed
        envelope.stamp(terminal_event)
        yield terminal_event

    # ------------------------------------------------------------------
    # Lifecycle: setup & finalize (plan §8.2 — move setup out of loop)
    # ------------------------------------------------------------------

    def _setup_query(self, submission: QuerySubmission) -> QueryTurnContext:
        """Prepare all turn-level state before invoking the runner.

        This consolidates the session-context unpacking, metadata merge,
        settings/budget resolution, state
        initialization, and per-turn ephemeral clearing that was previously
        the first phase of ``run_agent_loop``.
        """
        session = submission.session
        if session is None:  # guarded by QuerySubmission.__post_init__
            raise TypeError("QuerySubmission session was not initialized")
        sc = submission.runtime

        # Unpack session_context — the loop kernel no longer needs to do this.
        skill_manager = sc.skill_manager
        permission_context = sc.permission_context
        session_id = sc.session_id
        task_id = sc.task_id
        task_manager = sc.task_manager
        background_manager = sc.background_manager
        cancel_event = sc.cancel_event
        stream_callback = sc.stream_callback
        emit_event = sc.emit_event

        # Build metadata from session_context.
        metadata: dict[str, Any] = dict(sc.metadata or {})
        # Every execution path, including SDK and subagent turns, receives the
        # same atomic turn/mailbox owner. WebSocket sessions inject their
        # conversation-scoped instance; other callers get a turn-scoped owner.
        metadata.setdefault("turn_input_queue", TurnInputQueue())
        if session_id:
            metadata.setdefault("session_id", session_id)
            metadata.setdefault("minicode_session_id", session_id)
        if sc.workspace_root:
            metadata.setdefault("workspace_root", str(sc.workspace_root))

        # Resolve cancel_event from metadata if not already set.
        if cancel_event is None:
            raw_cancel = metadata.get("cancel_event")
            if isinstance(raw_cancel, asyncio.Event):
                cancel_event = raw_cancel

        # Resolve settings from the session/config snapshot.
        settings = session.agent_settings or AgentSettings()

        budget = session.token_budget or TokenBudget()

        # Build context builder if not provided.
        ctx = session.context_builder or ContextBuilder(
            token_budget=budget,
            agent_settings=settings,
        )

        # Resolve max iterations and create/reuse state.
        max_iterations_limit = resolve_turn_max_iterations(settings)
        state = submission.state or AgentState(
            user_message=submission.user_message,
            max_iterations=max_iterations_limit,
        )

        # Clear per-turn ephemeral state in the lifecycle owner. Only touch
        # real AgentState instances; compatibility tests may pass mock objects.
        if isinstance(state, AgentState):
            prepare_turn_state(
                state,
                settings=settings,
            )
        # Permission checker with workspace root.
        permission_checker = session.permission_checker
        if sc.workspace_root:
            permission_checker = permission_checker.with_workspace_root(sc.workspace_root)

        prepared_session = AgentSession(
            llm=session.llm,
            tool_registry=session.tool_registry,
            artifact_store=session.artifact_store,
            permission_checker=permission_checker,
            agent_settings=settings,
            token_budget=budget,
            context_builder=ctx,
            approval_handler=session.approval_handler,
        )

        turn_kernel = TurnKernel.create(
            metadata=metadata,
            state=state,
            budget=budget,
            task_id=task_id,
            session_id=session_id,
            emit_event=emit_event,
            initial_user_message=submission.user_message,
        )
        run_record = turn_kernel.run_record
        if run_record.conversation_id:
            metadata.setdefault("conversation_id", run_record.conversation_id)

        recovery = prepare_query_recovery(
            session_id=session_id,
            conversation_id=str(
                run_record.conversation_id
                or getattr(state, "conversation_id", "")
                or metadata.get("conversation_id", "")
                or ""
            ),
            metadata=metadata,
            state=state,
            context_builder=ctx,
            max_iterations_budget=max_iterations_limit,
            current_run_id=run_record.run_id,
            skill_manager=skill_manager,
        )
        if recovery.restored:
            turn_kernel.discard_scheduled_user_input()

        return QueryTurnContext(
            user_message=submission.user_message,
            session=prepared_session,
            state=state,
            metadata=metadata,
            settings=settings,
            budget=budget,
            context_builder=ctx,
            skill_manager=skill_manager,
            permission_context=permission_context,
            session_id=session_id,
            task_id=task_id,
            task_manager=task_manager,
            background_manager=background_manager,
            cancel_event=cancel_event,
            stream_callback=stream_callback,
            emit_event=emit_event,
            turn_kernel=turn_kernel,
            startup_events=recovery.startup_events,
        )

    def _finalize_query(
        self,
        turn_ctx: QueryTurnContext,
        status: str,
        reason: str,
    ) -> AgentEvent | None:
        """Complete the durable run exactly once after the loop kernel exits."""
        if turn_ctx.turn_kernel is None:
            return None
        run_status: AgentRunStatus = (
            "cancelled" if status == "cancelled"
            else "failed" if status == "failed"
            else "partial" if status == "partial"
            else "completed"
        )
        turn_ctx.state.terminal_status = (
            "cancelled"
            if run_status in {"cancelled", "interrupted"}
            else run_status
        )
        event = turn_ctx.turn_kernel.complete_run_record(
            run_status,
            summary=reason,
            error=reason if run_status == "failed" else "",
        )
        return event or turn_ctx.turn_kernel.completion_event

    def _prepare_session(self, submission: QuerySubmission) -> AgentSession:
        """Backward-compatible session preparation (delegates to _setup_query)."""
        return self._setup_query(submission).session


# Privatize run_agent_loop (plan §8.3).
# External callers should use QueryEngine.submit, not import run_agent_loop.
_run_agent_loop = run_agent_loop
