from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from backend.agent.context import ContextBuilder
from backend.agent.event_envelope import EventEnvelope
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.loop_session import prepare_turn_state
from backend.agent.iteration_budget import resolve_turn_max_iterations
from backend.agent.query_journal import (
    QueryJournalRecorder,
    record_setup_failure,
    terminal_commit_failed,
    terminal_journal_failure_event,
)
from backend.agent.run_events import should_emit_event
from backend.agent.runtime import (
    AgentRuntime,
    TerminalCommitError,
)
from backend.agent.run_context import RunContext
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.query_terminal import QueryTerminalTransaction
from backend.agent.state import AgentState
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.turn_input import TurnInputQueue
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.agent.lifecycle_observer import (
    LifecycleObserverOwner,
    LifecycleObserverFactory,
    resolve_lifecycle_runtime,
)
from backend.llm.base import LLMAdapter, LLMTurnContext
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
    lifecycle_observer_factory: LifecycleObserverFactory | None = None
    lifecycle_runtime: Any | None = None
    active_turn: bool = False
    active_tool_names: tuple[str, ...] | None = None


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
    lifecycle_observer_factory: LifecycleObserverFactory | None = None
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
                lifecycle_observer_factory=self.lifecycle_observer_factory,
            )
        if self.workspace_root is not None:
            from pathlib import Path

            requested_root = Path(self.workspace_root).expanduser().resolve()
            existing_root = self.runtime.workspace_root
            if existing_root is not None and Path(existing_root).expanduser().resolve() != requested_root:
                raise ValueError(
                    "QuerySubmission workspace_root conflicts with its runtime workspace_root"
                )
            self.runtime.workspace_root = requested_root


@dataclass(frozen=True, slots=True)
class QueryTurnContext:
    """Prepared state for a single query turn, built by QueryEngine._setup_query.

    This replaces the ad-hoc setup block that was previously the first ~80 lines
    of ``run_agent_loop``.  By owning the setup here, QueryEngine becomes the
    single lifecycle owner: callers go through ``submit`` → ``_setup_query`` →
    runner → terminal transaction, and ``run_agent_loop`` is reduced to a pure
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
    lifecycle_observer_factory: LifecycleObserverFactory | None = None
    run_context: RunContext = field(default_factory=RunContext)


class QuerySetupError(RuntimeError):
    """A setup failure after the durable run crossed the running boundary."""

    def __init__(
        self,
        cause: BaseException,
        *,
        completion_event: AgentEvent | None,
        metadata: dict[str, Any],
        state: AgentState,
        conversation_id: str,
        run_context: RunContext | None = None,
    ) -> None:
        self.cause = cause
        self.completion_event = completion_event
        self.metadata = metadata
        self.state = state
        self.conversation_id = conversation_id
        self.run_context = run_context or RunContext()
        super().__init__("MiniCode turn setup failed after durable run start")


class QueryEngine:
    """Stable entry point for a single user-query lifecycle.

    Owns the setup → run → finalize lifecycle. ``run_agent_loop`` is the private
    iteration kernel behind it; every caller goes through ``QueryEngine.submit``.
    """

    def __init__(
        self, runner: Callable[..., AsyncIterator[AgentEvent]] | None = None
    ) -> None:
        # The runner is the private loop kernel.  External code should not
        # import or call it directly.
        # Keep the default unresolved until submit-time. Besides preserving a
        # narrow test seam, this avoids copying the kernel reference into every
        # long-lived host session. QueryEngine remains its sole authority.
        self._runner = runner

    async def submit_filtered(
        self, submission: QuerySubmission
    ) -> AsyncIterator[AgentEvent]:
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
        """Run one query while enforcing MiniCode session single-flight."""
        session = submission.session
        if session is None:  # guarded by QuerySubmission.__post_init__
            raise TypeError("QuerySubmission session was not initialized")
        if session.active_turn:
            raise RuntimeError(
                "Agent session is already processing a prompt. Queue the message "
                "as steering/follow-up input or wait for the active turn to finish."
            )
        session.active_turn = True
        active_stream = self._submit_active(submission)
        try:
            async for event in active_stream:
                yield event
        finally:
            try:
                await active_stream.aclose()
            finally:
                session.active_turn = False

    def _resolve_runner(self) -> Callable[..., AsyncIterator[AgentEvent]]:
        return self._runner or run_agent_loop

    async def _submit_active(
        self, submission: QuerySubmission
    ) -> AsyncIterator[AgentEvent]:
        """Run one query and emit one public terminal event.

        Each turn-scoped event is stamped with an :class:`EventEnvelope`
        carrying ``task_id``, ``turn_id`` (captured from the first
        ``agent.run.started``) and a monotonic ``seq``.  This provides the
        stable correlation fields the frontend needs for canonical
        ``ActivityItem`` migration (plan §7.2, §20.4).
        """
        try:
            turn_ctx = self._setup_query(submission)
        except QuerySetupError as exc:
            for event in self._setup_failure_events(submission, exc):
                yield event
            return
        session = turn_ctx.session
        conversation_id = str(
            turn_ctx.state.conversation_id
            or turn_ctx.metadata.get("conversation_id", "")
            or ""
        )
        envelope = EventEnvelope(
            task_id=turn_ctx.task_id,
            conversation_id=conversation_id,
        )
        journal = QueryJournalRecorder(
            journal=turn_ctx.run_context.execution_journal,
            metadata=turn_ctx.metadata,
            state=turn_ctx.state,
            context_builder=turn_ctx.context_builder,
            turn_kernel=turn_ctx.turn_kernel,
            conversation_id=conversation_id,
        )
        terminal = QueryTerminalTransaction(turn_ctx=turn_ctx, journal=journal)
        lifecycle = LifecycleObserverOwner()
        runner: AsyncGenerator[AgentEvent, None] | None = None
        terminal_event: AgentEvent | None = None
        try:
            try:
                self._publish_system_prompt(turn_ctx)
                lifecycle = LifecycleObserverOwner.create(
                    turn_ctx.lifecycle_observer_factory
                    or session.lifecycle_observer_factory,
                    runner=turn_ctx.run_context.lifecycle_runtime,
                    user_message=turn_ctx.user_message,
                    metadata=turn_ctx.metadata,
                    run_context=turn_ctx.run_context,
                    images=turn_ctx.state.attachments or None,
                )
                journal.record_turn_started()
                observer_error = await lifecycle.start()
                if observer_error is not None:
                    journal.record_event(observer_error)
                    envelope.stamp(observer_error)
                    yield observer_error
                runner = self._create_runner(turn_ctx, submission)
                if turn_ctx.turn_kernel is not None:
                    for started_event in turn_ctx.turn_kernel.start_events():
                        envelope.stamp(started_event)
                        yield started_event
                for startup_event in turn_ctx.startup_events:
                    envelope.stamp(startup_event)
                    yield startup_event
            except asyncio.CancelledError:
                async for event in self._terminal_events(
                    terminal,
                    lifecycle,
                    envelope,
                    AgentEvent.done(
                        status="cancelled",
                        reason="startup_cancelled",
                    ),
                    validate=False,
                ):
                    yield event
                raise
            except Exception:
                logger.exception("MiniCode turn startup failed")
                async for event in self._terminal_events(
                    terminal,
                    lifecycle,
                    envelope,
                    AgentEvent.done(status="failed", reason="startup_failed"),
                    leading_events=(
                        AgentEvent.error(
                            "MiniCode could not initialize the agent turn.",
                            recoverable=False,
                            error_type="startup",
                            error_code="startup_failed",
                        ),
                    ),
                    validate=False,
                ):
                    yield event
                return

            try:
                async for event in runner:
                    journal.record_event(event)
                    terminal.observe_runner_event(event)
                    if event.type in {
                        "agent.run.started",
                        "agent.run.completed",
                        "agent.terminal.intent",
                    }:
                        continue
                    if not should_emit_event(event):
                        continue
                    observer_error = await lifecycle.observe(event)
                    if observer_error is not None:
                        journal.record_event(observer_error)
                        envelope.stamp(observer_error)
                        yield observer_error
                    if event.type == "done":
                        terminal_event = terminal.accept_done(event)
                        continue
                    envelope.stamp(event)
                    yield event
            except asyncio.CancelledError:
                async for event in self._terminal_events(
                    terminal,
                    lifecycle,
                    envelope,
                    AgentEvent.done(
                        status="cancelled",
                        reason="user_interrupted",
                    ),
                    validate=False,
                ):
                    yield event
                raise
            except Exception:
                logger.exception("Agent runtime failed outside provider recovery")
                async for event in self._terminal_events(
                    terminal,
                    lifecycle,
                    envelope,
                    AgentEvent.done(status="failed", reason="runtime_error"),
                    leading_events=(
                        AgentEvent.error(
                            "MiniCode internal runtime processing failed. The turn "
                            "was stopped without retrying it as a model API error.",
                            recoverable=False,
                            error_type="runtime",
                        ),
                    ),
                    validate=False,
                ):
                    yield event
                return

            async for event in self._terminal_events(
                terminal,
                lifecycle,
                envelope,
                terminal_event or terminal.default_terminal(),
                validate=True,
            ):
                yield event
        finally:
            if not terminal.finalized:
                closed = terminal.commit(
                    AgentEvent.done(
                        status="cancelled",
                        reason="consumer_closed",
                    ),
                    validate=False,
                )
                observer_error = await lifecycle.finish(
                    status=closed.status,
                    reason=closed.reason,
                )
                if observer_error is not None:
                    terminal.record_post_commit_event(observer_error)
            if runner is not None:
                await runner.aclose()
            if submission.runtime.metadata is not None:
                submission.runtime.metadata.update(turn_ctx.metadata)

    def _setup_failure_events(
        self,
        submission: QuerySubmission,
        error: QuerySetupError,
    ) -> tuple[AgentEvent, ...]:
        envelope = EventEnvelope(
            task_id=str(submission.runtime.task_id or ""),
            conversation_id=error.conversation_id,
        )
        completion = error.completion_event
        commit_failed = terminal_commit_failed(completion)
        terminal = AgentEvent.done(
            status="failed",
            reason="terminal_commit_failed" if commit_failed else "startup_failed",
        )
        events: list[AgentEvent] = []
        if not commit_failed:
            events.append(
                AgentEvent.error(
                    "MiniCode could not initialize the agent turn.",
                    recoverable=False,
                    error_type="startup",
                    error_code="startup_failed",
                )
            )
        if completion is not None:
            events.append(completion)
        try:
            record_setup_failure(
                error.run_context.execution_journal,
                conversation_id=error.conversation_id,
                completion_event=completion,
                terminal_event=terminal,
            )
        except Exception as exc:
            logger.error(
                "Execution journal could not record setup failure: %s",
                exc,
                exc_info=True,
            )
            events.append(terminal_journal_failure_event(exc))
        events.append(terminal)
        for event in events:
            envelope.stamp(event)
        return tuple(events)

    @staticmethod
    def _publish_system_prompt(turn_ctx: QueryTurnContext) -> None:
        if str(turn_ctx.metadata.get("system_prompt") or "").strip():
            return
        rendered_prompt = str(
            turn_ctx.context_builder.base_system_prompt(turn_ctx.state) or ""
        ).strip()
        if rendered_prompt:
            turn_ctx.metadata["system_prompt"] = rendered_prompt

    def _create_runner(
        self,
        turn_ctx: QueryTurnContext,
        submission: QuerySubmission,
    ) -> AsyncGenerator[AgentEvent, None]:
        session = turn_ctx.session
        runner = self._resolve_runner()(
            user_message=turn_ctx.user_message,
            llm=session.llm,
            tool_registry=session.tool_registry,
            artifact_store=session.artifact_store,
            permission_checker=session.permission_checker,
            agent_settings=session.agent_settings,
            token_budget=session.token_budget,
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
            run_context=turn_ctx.run_context,
            session_context=submission.runtime,
            turn_kernel=turn_ctx.turn_kernel,
            state_prepared=True,
            initial_max_iterations_limit=turn_ctx.state.max_iterations,
            turn_budget_controller=TurnBudgetController.from_settings(
                session.agent_settings,
                max_iterations=turn_ctx.state.max_iterations,
            ),
        )
        return cast(AsyncGenerator[AgentEvent, None], runner)

    @staticmethod
    async def _terminal_events(
        terminal: QueryTerminalTransaction,
        lifecycle: LifecycleObserverOwner,
        envelope: EventEnvelope,
        terminal_event: AgentEvent,
        *,
        leading_events: tuple[AgentEvent, ...] = (),
        validate: bool,
    ) -> AsyncIterator[AgentEvent]:
        result = terminal.commit(terminal_event, validate=validate)
        observer_error = await lifecycle.finish(
            status=result.status,
            reason=result.reason,
        )
        events = [*leading_events, *result.evidence_events]
        if observer_error is not None:
            events.append(observer_error)
            journal_error = terminal.record_post_commit_event(observer_error)
            if journal_error is not None:
                events.append(journal_error)
        if result.completion_event is not None:
            events.append(result.completion_event)
        events.append(result.terminal_event)
        for event in events:
            envelope.stamp(event)
            yield event

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
        run_context = sc.run_context or RunContext()
        runtime_value = run_context.agent_runtime
        if runtime_value is None:
            runtime_value = AgentRuntime()
        elif not isinstance(runtime_value, AgentRuntime):
            raise TypeError("RunContext agent_runtime must be an AgentRuntime")
        run_context.agent_runtime = runtime_value
        if sc.workspace_root is None and submission.workspace_root is not None:
            from pathlib import Path

            sc.workspace_root = Path(submission.workspace_root).expanduser().resolve()
        lifecycle_runtime = resolve_lifecycle_runtime(
            session_context=sc,
            run_context=run_context,
        )
        if lifecycle_runtime is None:
            lifecycle_runtime = session.lifecycle_runtime
        run_context.lifecycle_runtime = lifecycle_runtime
        # Mutable turn-owned sink.  The loop and adapters share this exact list
        # through copied metadata so side calls remain observable after submit.
        side_call_records: list[dict[str, Any]] = []
        metadata["_side_calls"] = side_call_records
        run_context.llm_turn_context = LLMTurnContext(
            side_call_records=side_call_records,
            cost_session_id=str(run_context.cost_session_id or session_id or ""),
            lifecycle_runtime=lifecycle_runtime,
        )
        # Every execution path, including SDK and subagent turns, receives the
        # same atomic turn/mailbox owner. WebSocket sessions inject their
        # conversation-scoped instance; other callers get a turn-scoped owner.
        if run_context.turn_input_queue is None:
            run_context.turn_input_queue = TurnInputQueue()
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
            llm=session.llm,
        )
        ctx.bind_llm_turn_context(run_context.llm_turn_context)
        session.context_builder = ctx

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
        if isinstance(permission_checker, PermissionChecker):
            permission_checker = permission_checker.with_workspace_root(
                sc.workspace_root
            )

        # Keep the exact mutable session owner supplied by the host. Extension
        # context changes it in place; cloning it would leave the loop with
        # stale model and budget dependencies.
        session.permission_checker = permission_checker
        session.agent_settings = settings
        session.token_budget = budget
        session.context_builder = ctx
        submission.runtime.agent_session = session

        try:
            turn_kernel = TurnKernel.create(
                metadata=metadata,
                state=state,
                budget=budget,
                task_id=task_id,
                session_id=session_id,
                emit_event=emit_event,
                initial_user_message=submission.user_message,
                run_context=run_context,
            )
        except Exception as exc:
            # ``TurnKernel.create`` itself crosses the durable running boundary
            # before constructing the kernel.  If construction fails, recover
            # the terminal fact here so callers still receive one canonical
            # startup failure envelope.
            completion_event: AgentEvent | None = None
            runtime = run_context.agent_runtime
            run_id = str(metadata.get("run_id") or "").strip()
            get_run = getattr(runtime, "get_run", None)
            record = get_run(run_id) if run_id and callable(get_run) else None
            if record is not None and str(getattr(record, "status", "")) == "running":
                try:
                    committed = runtime.commit_terminal(
                        run_id,
                        "failed",
                        summary="startup_failed",
                        terminal_reason="startup_failed",
                        error="startup_failed",
                    )
                    completion_event = AgentEvent.agent_run_completed(committed)
                except TerminalCommitError as commit_error:
                    completion_event = AgentEvent.error(
                        "MiniCode could not durably commit the startup failure.",
                        recoverable=False,
                        error_type="terminal_commit_failed",
                        error_code=f"runtime.{commit_error.failure_kind}",
                    )
                    completion_event.data.update(
                        {
                            "run_id": run_id,
                            "terminal_commit_failed": True,
                            "failure_kind": commit_error.failure_kind,
                        }
                    )
            elif record is not None:
                completion_event = AgentEvent.agent_run_completed(record)
            raise QuerySetupError(
                exc,
                completion_event=completion_event,
                metadata=metadata,
                state=state,
                conversation_id=str(
                    getattr(state, "conversation_id", "")
                    or metadata.get("conversation_id", "")
                    or ""
                ),
                run_context=run_context,
            ) from exc
        try:
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
            if recovery.restored or metadata.get("_turn_admission_restored"):
                turn_kernel.discard_scheduled_user_input()

            return QueryTurnContext(
                user_message=submission.user_message,
                session=session,
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
                lifecycle_observer_factory=(
                    submission.lifecycle_observer_factory
                    or session.lifecycle_observer_factory
                ),
                run_context=run_context,
            )
        except Exception as exc:
            # The durable run crossed the running boundary before recovery and
            # context preparation.  Close it on every setup failure so startup
            # cannot strand a record that recovery later misclassifies.
            completion_event: AgentEvent | None = None
            try:
                completion_event = turn_kernel.abort_startup(reason="startup_failed")
            except Exception:
                logger.warning(
                    "Unable to commit startup failure after query setup error",
                    exc_info=True,
                )
            raise QuerySetupError(
                exc,
                completion_event=completion_event,
                metadata=metadata,
                state=state,
                conversation_id=str(
                    getattr(state, "conversation_id", "")
                    or metadata.get("conversation_id", "")
                    or ""
                ),
                run_context=run_context,
            ) from exc
