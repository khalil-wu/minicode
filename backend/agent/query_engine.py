from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.coordinator import maybe_enable_coordinator_from_user_message
from backend.agent.event_envelope import EventEnvelope
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.prompting import clear_loaded_prompt_packs
from backend.agent.run_events import should_emit_event
from backend.agent.runtime import AgentRunRecord, AgentRunStatus, AgentRuntime, default_runtime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry


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
    session: AgentSession
    state: AgentState | None = None
    runtime: AgentLoopSessionContext = field(default_factory=AgentLoopSessionContext)


@dataclass(slots=True)
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
    vector_memory: Any | None = None
    permission_context: PermissionContext | None = None
    session_id: str = ""
    task_id: str = ""
    task_manager: Any | None = None
    background_manager: Any | None = None
    cancel_event: asyncio.Event | None = None
    stream_callback: Any | None = None
    emit_event: Any | None = None
    agent_runtime: AgentRuntime | None = None
    run_record: AgentRunRecord | None = None


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
            vector_memory=turn_ctx.vector_memory,
            permission_context=turn_ctx.permission_context,
            session_id=turn_ctx.session_id,
            task_id=turn_ctx.task_id,
            task_manager=turn_ctx.task_manager,
            background_manager=turn_ctx.background_manager,
            stream_callback=turn_ctx.stream_callback,
            emit_event=turn_ctx.emit_event,
            metadata=turn_ctx.metadata,
            session_context=submission.runtime,
        )
        if turn_ctx.run_record is not None:
            started_event = AgentEvent.agent_run_started(turn_ctx.run_record)
            envelope.stamp(started_event)
            yield started_event
            phase_event = AgentEvent.agent_phase_updated(
                turn_ctx.run_record.run_id,
                "plan",
                summary="Preparing agent context",
                role=turn_ctx.run_record.role,
                conversation_id=turn_ctx.run_record.conversation_id,
            )
            envelope.stamp(phase_event)
            yield phase_event
        terminal_event: AgentEvent | None = None
        published_error_message = ""
        try:
            async for event in runner:
                if event.type in {"agent.run.started", "agent.run.completed"}:
                    continue
                if not should_emit_event(event):
                    continue
                if event.type == "done":
                    terminal_event = event
                    # Drain the generator: checkpoint persistence and terminal
                    # cleanup run after the loop yields its done event.
                    continue
                if event.type == "error":
                    published_error_message = str(event.data.get("message") or "run_error")
                envelope.stamp(event)
                yield event
        except asyncio.CancelledError:
            terminal_event = AgentEvent.done(status="cancelled", reason="user_interrupted")
            completed = self._finalize_query(turn_ctx, "cancelled", "user_interrupted")
            if completed is not None:
                envelope.stamp(completed)
                yield completed
            envelope.stamp(terminal_event)
            yield terminal_event
            raise
        except Exception as exc:
            err_event = AgentEvent.error(
                f"Agent run failed: {exc}",
                recoverable=True,
                error_type="api",
            )
            envelope.stamp(err_event)
            yield err_event
            terminal_event = AgentEvent.done(status="failed", reason="run_error")
        finally:
            close = getattr(runner, "aclose", None)
            if callable(close):
                with suppress(Exception):
                    await close()
            if isinstance(submission.runtime.metadata, dict):
                submission.runtime.metadata.update(turn_ctx.metadata)

        if terminal_event is None:
            reason = turn_ctx.state.stopped_reason
            has_usable_reply = bool(turn_ctx.state.reply.strip())
            status = (
                "cancelled"
                if reason == "interrupted"
                else "partial"
                if reason == "max_iterations" and has_usable_reply
                else "completed"
                if reason in {None, "completed"}
                else "failed"
            )
            terminal_event = AgentEvent.done(status=status, reason=str(reason or ""))
        status = str(terminal_event.data.get("status") or "completed")
        reason = str(terminal_event.data.get("reason") or "")
        if published_error_message and status == "completed":
            status = "failed"
            reason = reason or "run_error"
            terminal_event.data["status"] = status
            terminal_event.data["reason"] = reason
        if (
            reason == "max_iterations"
            and turn_ctx.state.reply.strip()
            and status != "cancelled"
        ):
            status = "partial"
            terminal_event.data["status"] = "partial"
        completed = self._finalize_query(turn_ctx, status, reason)
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
        settings/budget resolution, coordinator mode detection, state
        initialization, and per-turn ephemeral clearing that was previously
        the first phase of ``run_agent_loop``.
        """
        from backend.agent.loop import _resolve_turn_max_iterations

        session = submission.session
        sc = submission.runtime

        # Unpack session_context — the loop kernel no longer needs to do this.
        skill_manager = sc.skill_manager
        vector_memory = sc.vector_memory
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
        if session_id:
            metadata.setdefault("session_id", session_id)
            metadata.setdefault("minicode_session_id", session_id)
        if sc.workspace_root:
            metadata.setdefault("workspace_root", str(sc.workspace_root))

        # Coordinator mode detection (plan §8.3 — move metadata normalization
        # out of loop).  This was previously the first metadata mutation in
        # run_agent_loop.
        metadata = maybe_enable_coordinator_from_user_message(metadata, submission.user_message)

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
            vector_memory=vector_memory,
        )

        # Resolve max iterations and create/reuse state.
        max_iterations_limit = _resolve_turn_max_iterations(submission.user_message, settings)
        state = submission.state or AgentState(
            user_message=submission.user_message,
            max_iterations=max_iterations_limit,
        )

        # Clear per-turn ephemeral state that should not leak across user
        # messages (plan §8.3 — move state initialization out of loop).
        # Only touch real AgentState instances; tests may pass mock objects.
        if isinstance(state, AgentState):
            state.max_total_retries = max(0, int(settings.turn_error_budget))
            state.loop_guidance.clear()
            state.disabled_tools.clear()
            state.blocked_repeat_calls = 0
            state.empty_reply_retries = 0
            state.stop_hook_feedback_used = False
            state.verify_attempts = 0
            state.max_output_recovery_count = 0
            state.max_output_recovered_text = ""
            state.heal_attempts = 0
            state.clear_transition()
            if not isinstance(getattr(state, "prompt_context", None), dict):
                state.prompt_context = {}
            clear_loaded_prompt_packs(state.prompt_context)

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

        agent_runtime = metadata.get("agent_runtime")
        if not isinstance(agent_runtime, AgentRuntime):
            agent_runtime = default_runtime()
        run_record = agent_runtime.start_run(
            conversation_id=str(getattr(state, "conversation_id", "") or metadata.get("conversation_id", "") or ""),
            parent_run_id=str(metadata.get("parent_run_id", "") or ""),
            role=str(metadata.get("agent_role", "main") or "main"),
            task_id=task_id,
            session_id=session_id,
            budget=budget,
            run_id=str(metadata.get("run_id", "") or "") or None,
        )
        metadata["run_id"] = run_record.run_id
        metadata["agent_runtime"] = agent_runtime
        metadata["_query_engine_lifecycle"] = True
        metadata["_query_engine_run_record"] = run_record
        if run_record.conversation_id:
            metadata.setdefault("conversation_id", run_record.conversation_id)

        return QueryTurnContext(
            user_message=submission.user_message,
            session=prepared_session,
            state=state,
            metadata=metadata,
            settings=settings,
            budget=budget,
            context_builder=ctx,
            skill_manager=skill_manager,
            vector_memory=vector_memory,
            permission_context=permission_context,
            session_id=session_id,
            task_id=task_id,
            task_manager=task_manager,
            background_manager=background_manager,
            cancel_event=cancel_event,
            stream_callback=stream_callback,
            emit_event=emit_event,
            agent_runtime=agent_runtime,
            run_record=run_record,
        )

    def _finalize_query(
        self,
        turn_ctx: QueryTurnContext,
        status: str,
        reason: str,
    ) -> AgentEvent | None:
        """Complete the durable run exactly once after the loop kernel exits."""
        if turn_ctx.agent_runtime is None or turn_ctx.run_record is None:
            return None
        run_status: AgentRunStatus = (
            "cancelled" if status == "cancelled"
            else "failed" if status == "failed"
            else "partial" if status == "partial"
            else "completed"
        )
        record = turn_ctx.agent_runtime.complete_run(
            turn_ctx.run_record.run_id,
            run_status,
            summary=reason,
            error=reason if run_status == "failed" else "",
        )
        return AgentEvent.agent_run_completed(record or turn_ctx.run_record)

    def _prepare_session(self, submission: QuerySubmission) -> AgentSession:
        """Backward-compatible session preparation (delegates to _setup_query)."""
        return self._setup_query(submission).session


# Privatize run_agent_loop (plan §8.3).
# External callers should use QueryEngine.submit, not import run_agent_loop.
_run_agent_loop = run_agent_loop
