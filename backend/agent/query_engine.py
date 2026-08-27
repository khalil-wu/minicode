from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.event_envelope import EventEnvelope
from backend.agent.execution_journal import (
    tool_result_journal_payload,
    tool_use_journal_payload,
)
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.loop_session import prepare_turn_state
from backend.agent.iteration_budget import resolve_turn_max_iterations
from backend.agent.run_events import should_emit_event
from backend.agent.runtime import (
    AgentRuntime,
    AgentRunStatus,
    TerminalCommitError,
    default_runtime,
)
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.state import AgentState
from backend.agent.turn_budget import TurnBudgetController
from backend.agent.terminal_validation import validate_terminal_outcome
from backend.agent.turn_input import TurnInputQueue
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.agent.lifecycle_observer import (
    LifecycleObserverFactory,
    install_lifecycle_runtime,
    null_lifecycle_observer_factory,
    resolve_lifecycle_runtime,
)
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)

# Resource limits that have retained usable output are partial results, never
# fabricated completions. Keep this projection policy in one place: the loop
# terminal reason remains the cause; this set only decides public status.
_USABLE_PARTIAL_REASONS = frozenset(
    {
        "max_iterations",
        "max_tool_calls",
        "max_turn_seconds",
        "max_turn_tokens",
        "max_turn_cost_usd",
        "budget_exceeded",
        "incomplete_tool_stream",
    }
)

_OBSERVER_FINISH_TIMEOUT_SECONDS = 5.0


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
    lifecycle_observer_factory: LifecycleObserverFactory | None = None


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
    ) -> None:
        self.cause = cause
        self.completion_event = completion_event
        self.metadata = metadata
        self.state = state
        self.conversation_id = conversation_id
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
            with suppress(Exception):
                await active_stream.aclose()
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
            envelope = EventEnvelope(
                task_id=str(submission.runtime.task_id or ""),
                conversation_id=exc.conversation_id,
            )
            journal = exc.metadata.get("_execution_journal")
            append_lifecycle = getattr(journal, "append_lifecycle", None)
            if callable(append_lifecycle):
                with suppress(Exception):
                    append_lifecycle(
                        "startup_failed",
                        {"conversation_id": exc.conversation_id},
                    )
            completion = exc.completion_event
            commit_failed = self._terminal_commit_failed(completion)
            if not commit_failed:
                startup_error = AgentEvent.error(
                    "MiniCode could not initialize the agent turn.",
                    recoverable=False,
                    error_type="startup",
                    error_code="startup_failed",
                )
                envelope.stamp(startup_error)
                yield startup_error
            if completion is not None:
                envelope.stamp(completion)
                yield completion
            terminal = AgentEvent.done(
                status="failed",
                reason="terminal_commit_failed" if commit_failed else "startup_failed",
            )
            envelope.stamp(terminal)
            if journal is not None:
                with suppress(Exception):
                    close_unresolved = getattr(
                        journal,
                        "close_unresolved_tool_uses",
                        None,
                    )
                    if callable(close_unresolved):
                        close_unresolved(
                            reason=terminal.data["reason"],
                            content="[Tool result missing because startup failed]",
                        )
                    append_terminal = getattr(journal, "append_terminal", None)
                    if callable(append_terminal):
                        append_terminal(
                            status="failed",
                            reason=terminal.data["reason"],
                            extra={"unresolved_tool_uses": journal.unresolved_tool_uses()},
                        )
            yield terminal
            return
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
        runner: AsyncIterator[AgentEvent] | None = None
        # The lifecycle observer receives the assembled base prompt before any
        # extension replacement is applied. Publish that canonical preview into
        # turn metadata before the observer starts.
        if isinstance(turn_ctx.metadata, dict) and not str(
            turn_ctx.metadata.get("system_prompt") or ""
        ).strip():
            base_prompt = getattr(turn_ctx.context_builder, "base_system_prompt", None)
            if callable(base_prompt):
                try:
                    rendered_prompt = str(base_prompt(turn_ctx.state) or "").strip()
                except Exception:
                    rendered_prompt = ""
                if rendered_prompt:
                    turn_ctx.metadata["system_prompt"] = rendered_prompt
        extension_bridge: Any | None = None
        query_finalized = False
        journal = (
            turn_ctx.metadata.get("_execution_journal")
            if isinstance(turn_ctx.metadata, dict)
            else None
        )
        journal_terminal_recorded = False
        runtime_terminal_receipt_recorded = False
        terminal_journal_error: BaseException | None = None
        terminal_intent_event: Any | None = None
        terminal_intent_key: tuple[str, str] | None = None
        journal_tool_names: dict[str, str] = {}
        journal_tool_claimed: set[str] = set()
        journal_agent_messages: dict[str, dict[str, str]] = {}
        journal_agent_message_receipts: dict[str, tuple[str, float]] = {}

        def journal_lifecycle(name: str, payload: dict[str, Any] | None = None) -> Any | None:
            append_lifecycle = getattr(journal, "append_lifecycle", None)
            if callable(append_lifecycle):
                return append_lifecycle(name, payload)
            return None

        def journal_terminal_intent(event: AgentEvent) -> None:
            """Persist recovery input without claiming the run committed terminal."""

            nonlocal terminal_intent_event, terminal_intent_key
            status = str(event.data.get("status") or "completed")
            reason = str(event.data.get("reason") or "")
            next_key = (status, reason)
            if terminal_intent_key == next_key or journal is None:
                return
            previous_intent_id = str(
                getattr(terminal_intent_event, "event_id", "") or ""
            )
            message_id = str(turn_ctx.metadata.get("assistant_message_id") or "")
            terminal_intent_event = journal_lifecycle(
                "terminal_intent",
                {
                    "run_id": str(turn_ctx.metadata.get("run_id") or ""),
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "status": status,
                    "reason": reason,
                    "assistant_message": {
                        "id": message_id,
                        "role": "assistant",
                        "content": str(getattr(turn_ctx.state, "reply", "") or ""),
                        "terminal_status": status,
                        "termination_reason": reason,
                    },
                    "context_snapshot": turn_ctx.context_builder.export_snapshot(),
                    "checkpoint": (
                        turn_ctx.turn_kernel.checkpoint_evidence()
                        if turn_ctx.turn_kernel is not None
                        else {"status": "none"}
                    ),
                    "supersedes_terminal_intent_event_id": previous_intent_id,
                },
            )
            terminal_intent_key = next_key

        def journal_event(event: AgentEvent) -> None:
            nonlocal runtime_terminal_receipt_recorded
            if journal is None:
                return
            data = dict(event.data or {})
            if event.type == "item.started":
                item = data.get("item") if isinstance(data.get("item"), dict) else {}
                if item.get("type") == "agent_message":
                    item_id = str(item.get("id") or "agent-message").strip()
                    journal_agent_messages[item_id] = {
                        "content": "",
                        "source": str(item.get("source") or "pending"),
                    }
            elif event.type == "agent_message.delta":
                item_id = str(data.get("item_id") or "agent-message").strip()
                delta = str(data.get("delta") or "")
                if not delta:
                    return
                message = journal_agent_messages.setdefault(
                    item_id,
                    {"content": "", "source": "pending"},
                )
                message["content"] += delta
                if data.get("source"):
                    message["source"] = str(data["source"])
                previous_content, previous_at = journal_agent_message_receipts.get(
                    item_id,
                    ("", 0.0),
                )
                now = time.monotonic()
                if (
                    not previous_content
                    or len(message["content"]) - len(previous_content) >= 128
                    or now - previous_at >= 0.12
                ):
                    journal.append(
                        "progress",
                        {
                            "kind": "assistant_message",
                            "item_id": item_id,
                            "content": message["content"],
                            "source": message["source"],
                            "status": "running",
                            "transcript_only": True,
                        },
                    )
                    journal_agent_message_receipts[item_id] = (
                        message["content"],
                        now,
                    )
            elif event.type == "item.completed":
                item = data.get("item") if isinstance(data.get("item"), dict) else {}
                if item.get("type") == "agent_message":
                    item_id = str(item.get("id") or "agent-message").strip()
                    content = str(item.get("text") or "")
                    source = str(item.get("source") or "model_final")
                    journal_agent_messages[item_id] = {
                        "content": content,
                        "source": source,
                    }
                    journal.append(
                        "progress",
                        {
                            "kind": "assistant_message",
                            "item_id": item_id,
                            "content": content,
                            "source": source,
                            "status": str(item.get("status") or "completed"),
                            "transcript_only": True,
                        },
                    )
                    journal_agent_message_receipts[item_id] = (
                        content,
                        time.monotonic(),
                    )
            elif event.type == "tool_call" and str(data.get("status") or "running") != "pending":
                payload = tool_use_journal_payload(data)
                tool_call = payload["tool_call"]
                journal_tool_names[str(tool_call["id"])] = str(tool_call["name"])
                journal_tool_claimed.add(str(tool_call["id"]))
                append_tool_use = getattr(journal, "append_tool_use", None)
                if callable(append_tool_use):
                    append_tool_use(data)
                else:
                    journal.append("tool_use", payload)
            elif event.type == "tool_result":
                call_id = str(data.get("id") or data.get("tool_call_id") or "").strip()
                tool_name = journal_tool_names.get(call_id) or str(
                    data.get("name") or data.get("tool_name") or ""
                ).strip()
                if not tool_name:
                    unresolved = {
                        str(item.get("tool_call_id") or ""): str(item.get("tool_name") or "")
                        for item in journal.unresolved_tool_uses()
                        if isinstance(item, dict)
                    }
                    tool_name = unresolved.get(call_id, "")
                append_tool_result = getattr(journal, "append_tool_result", None)
                if callable(append_tool_result):
                    append_tool_result(data, tool_name=tool_name)
                else:
                    unresolved = {
                        str(item.get("tool_call_id") or ""): str(item.get("tool_name") or "")
                        for item in journal.unresolved_tool_uses()
                        if isinstance(item, dict)
                    }
                    if (
                        call_id
                        and tool_name
                        and call_id not in journal_tool_claimed
                        and call_id not in unresolved
                    ):
                        journal.append(
                            "tool_use",
                            tool_use_journal_payload(
                                {
                                    "id": call_id,
                                    "name": tool_name,
                                    "args": {},
                                    "status": "cancelled",
                                    "announcement_only": True,
                                    "arguments_complete": False,
                                    "request_digest": data.get("request_digest"),
                                }
                            ),
                        )
                        journal_tool_names[call_id] = tool_name
                    payload = tool_result_journal_payload(data, tool_name=tool_name)
                    journal.append("tool_result", payload)
            elif event.type == "context_compacted":
                journal_lifecycle(
                    "compaction_committed",
                    {
                        **data,
                        "context_snapshot": turn_ctx.context_builder.export_snapshot(),
                    },
                )
            elif event.type in {"approval_request", "ask_user"}:
                call_id = str(data.get("tool_call_id") or data.get("id") or "").strip()
                tool_name = str(data.get("tool_name") or data.get("name") or "").strip()
                if call_id and tool_name:
                    journal_tool_names[call_id] = tool_name
                journal_lifecycle("approval_waiting", data)
            elif event.type == "error":
                journal_lifecycle(
                    "error",
                    {
                        "message": str(data.get("message") or ""),
                        "error_type": str(data.get("error_type") or ""),
                        "error_code": str(data.get("error_code") or ""),
                        "recoverable": bool(data.get("recoverable", False)),
                    },
                )
                if data.get("terminal_commit_failed") is True:
                    journal_lifecycle(
                        "runtime_terminal_commit_failed",
                        {
                            "run_id": str(data.get("run_id") or ""),
                            "failure_kind": str(data.get("failure_kind") or ""),
                        },
                    )
            elif event.type == "agent.terminal.intent":
                journal_terminal_intent(
                    AgentEvent.done(
                        status=str(data.get("status") or "completed"),
                        reason=str(data.get("reason") or ""),
                    )
                )
            elif event.type == "agent.run.completed":
                if runtime_terminal_receipt_recorded:
                    return
                journal_lifecycle(
                    "runtime_terminal_committed",
                    {
                        "run_id": str(data.get("run_id") or ""),
                        "status": str(data.get("status") or "completed"),
                        "terminal_intent_event_id": str(
                            getattr(terminal_intent_event, "event_id", "") or ""
                        ),
                    },
                )
                runtime_terminal_receipt_recorded = True

        def journal_terminal(event: AgentEvent) -> None:
            nonlocal journal_terminal_recorded
            if turn_ctx.turn_kernel is not None:
                event.data.setdefault(
                    "checkpoint",
                    turn_ctx.turn_kernel.checkpoint_evidence(),
                )
            if (
                journal_terminal_recorded
                or journal is None
                or not runtime_terminal_receipt_recorded
            ):
                return
            status = str(event.data.get("status") or "completed")
            reason = str(event.data.get("reason") or "")
            journal_lifecycle("provider_completed", {"status": status, "reason": reason})
            assistant_content = str(getattr(turn_ctx.state, "reply", "") or "")
            if not assistant_content:
                assistant_content = str(
                    getattr(turn_ctx.state, "prompt_context", {}).get(
                        "last_completed_assistant_text",
                        "",
                    )
                )
            terminal_context_snapshot = turn_ctx.context_builder.export_snapshot()
            journal.append(
                "assistant",
                {
                    "content": assistant_content,
                    "status": status,
                    "reason": reason,
                    "conversation_id": conversation_id,
                    "message_id": str(
                        turn_ctx.metadata.get("assistant_message_id") or ""
                    ),
                    "context_snapshot": terminal_context_snapshot,
                },
            )
            close_unresolved = getattr(journal, "close_unresolved_tool_uses", None)
            if callable(close_unresolved):
                close_unresolved(
                    reason=reason or status,
                    content=(
                        "[Tool result missing because the turn reached terminal state]"
                    ),
                )
            unresolved = journal.unresolved_tool_uses()
            tool_execution_context = turn_ctx.metadata.get("_tool_execution_context")
            raw_cleanup_receipts = getattr(
                tool_execution_context,
                "cleanup_receipts",
                turn_ctx.metadata.get("_tool_cleanup_receipts"),
            )
            cleanup_receipts = {
                str(tool_call_id): dict(receipt)
                for tool_call_id, receipt in (
                    (raw_cleanup_receipts or {}).items()
                    if isinstance(raw_cleanup_receipts, dict)
                    else []
                )
                if isinstance(receipt, dict)
            }
            cleanup_pending_count = sum(
                int(receipt.get("pending") or 0)
                for receipt in cleanup_receipts.values()
            )
            checkpoint_evidence = (
                turn_ctx.turn_kernel.checkpoint_evidence()
                if turn_ctx.turn_kernel is not None
                else {"status": "none"}
            )
            journal.append_terminal(
                status=status,
                summary=assistant_content,
                reason=reason,
                extra={
                    "runtime_terminal_commit": "committed",
                    "run_id": str(turn_ctx.metadata.get("run_id") or ""),
                    "terminal_intent_event_id": str(
                        getattr(terminal_intent_event, "event_id", "") or ""
                    ),
                    "conversation_id": conversation_id,
                    "message_id": str(
                        turn_ctx.metadata.get("assistant_message_id") or ""
                    ),
                    "unresolved_tool_uses": unresolved,
                    "manual_recovery_required": any(
                        item.get("recovery_policy") == "manual"
                        for item in unresolved
                    ) or any(
                        bool(receipt.get("manual_recovery_required"))
                        for receipt in cleanup_receipts.values()
                    ),
                    "cleanup_receipts": cleanup_receipts,
                    "cleanup_pending_count": cleanup_pending_count,
                    "checkpoint": checkpoint_evidence,
                },
            )
            journal_terminal_recorded = True

        def finalize_query_with_journal(
            status: str,
            reason: str,
            terminal: AgentEvent | None = None,
        ) -> AgentEvent | None:
            nonlocal terminal_journal_error
            intent = terminal or AgentEvent.done(status=status, reason=reason)
            if (
                status == "completed"
                and turn_ctx.turn_kernel is not None
                and bool(turn_ctx.metadata.get("retain_completed_checkpoint"))
            ):
                checkpoint_status = turn_ctx.turn_kernel.finalize_checkpoint(
                    session_id=turn_ctx.session_id,
                    user_message=turn_ctx.user_message,
                    state=turn_ctx.state,
                    context_builder=turn_ctx.context_builder,
                )
                if checkpoint_status == "save_failed":
                    status = "partial" if turn_ctx.state.reply.strip() else "failed"
                    reason = "checkpoint_save_failed"
                    turn_ctx.state.terminal_status = status
                    turn_ctx.state.stopped_reason = reason
                    turn_ctx.state.mark_transition("checkpoint_save_failed")
                    intent.data["status"] = status
                    intent.data["reason"] = reason
            journal_terminal_intent(intent)
            completed_event = self._finalize_query(turn_ctx, status, reason)
            # Journal writes after the durable commit record evidence; they are
            # not the terminal itself. Letting one propagate from here aborted
            # _submit_active before it could yield the terminal, so the run was
            # durably committed while the client received nothing at all.
            try:
                if completed_event is not None:
                    journal_event(completed_event)
                journal_terminal(intent)
            except Exception as exc:
                terminal_journal_error = exc
                logger.error(
                    "Execution journal could not record the terminal for run %s: %s",
                    str(turn_ctx.metadata.get("run_id") or ""),
                    exc,
                    exc_info=True,
                )
            return completed_event

        terminal_event: AgentEvent | None = None
        observed_tool_statuses: list[str] = []
        observed_visible_result = False
        startup_failed = False
        startup_cancelled: asyncio.CancelledError | None = None
        try:
            observer_factory = (
                turn_ctx.lifecycle_observer_factory
                or session.lifecycle_observer_factory
                or null_lifecycle_observer_factory
            )
            extension_bridge = observer_factory(
                runner=resolve_lifecycle_runtime(turn_ctx.metadata),
                user_message=turn_ctx.user_message,
                metadata=turn_ctx.metadata,
                images=(
                    getattr(turn_ctx.state, "attachments", None)
                    if isinstance(getattr(turn_ctx.state, "attachments", None), list)
                    else None
                ),
            )
            journal_lifecycle(
                "turn_started",
                {
                    "conversation_id": conversation_id,
                    "context_snapshot": turn_ctx.context_builder.export_snapshot(),
                },
            )
            append_journal = getattr(journal, "append", None)
            if callable(append_journal):
                journal_user_metadata = turn_ctx.metadata.get("_journal_user_metadata")
                if not isinstance(journal_user_metadata, dict):
                    journal_user_metadata = {}
                append_journal(
                    "user_prompt",
                    {
                        **journal_user_metadata,
                        "content": str(
                            turn_ctx.metadata.get("_journal_user_message")
                            or turn_ctx.user_message
                        ),
                        "conversation_id": conversation_id,
                    },
                )
            journal_lifecycle("provider_claimed", {"conversation_id": conversation_id})
            observer_start_error = await self._start_observer_safely(extension_bridge)
            if observer_start_error is not None:
                observer_error = AgentEvent.error(
                    observer_start_error,
                    recoverable=False,
                    error_type="projection",
                    error_code="lifecycle_observer.start_failed",
                )
                journal_event(observer_error)
                envelope.stamp(observer_error)
                yield observer_error
            # Capture loop dependencies after the observer returns so session
            # model changes affect the first provider request and later ones.
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
                session_context=submission.runtime,
                turn_kernel=turn_ctx.turn_kernel,
                state_prepared=True,
                initial_max_iterations_limit=turn_ctx.state.max_iterations,
                turn_budget_controller=TurnBudgetController.from_settings(
                    session.agent_settings,
                    max_iterations=turn_ctx.state.max_iterations,
                ),
            )
            if turn_ctx.turn_kernel is not None:
                for started_event in turn_ctx.turn_kernel.start_events():
                    envelope.stamp(started_event)
                    yield started_event
                for startup_event in turn_ctx.startup_events:
                    envelope.stamp(startup_event)
                    yield startup_event
        except asyncio.CancelledError as exc:
            startup_failed = True
            startup_cancelled = exc
            terminal_event = AgentEvent.done(
                status="cancelled", reason="startup_cancelled"
            )
        except Exception:
            startup_failed = True
            logger.exception("MiniCode turn startup failed")
            startup_error = AgentEvent.error(
                "MiniCode could not initialize the agent turn.",
                recoverable=False,
                error_type="startup",
                error_code="startup_failed",
            )
            envelope.stamp(startup_error)
            yield startup_error
            terminal_event = AgentEvent.done(status="failed", reason="startup_failed")
        finally:
            if sys.exc_info()[0] is GeneratorExit and not query_finalized:
                finalize_query_with_journal("cancelled", "consumer_closed")
                query_finalized = True
                observer_finish_error = await self._finish_observer_safely(
                    extension_bridge,
                    status="cancelled", reason="consumer_closed"
                )
                if observer_finish_error is not None:
                    journal_lifecycle(
                        "projection_error",
                        {"phase": "finish", "message": observer_finish_error},
                    )
                close = getattr(runner, "aclose", None) if runner is not None else None
                if callable(close):
                    with suppress(Exception):
                        await close()
                if isinstance(submission.runtime.metadata, dict):
                    submission.runtime.metadata.update(turn_ctx.metadata)
            elif startup_failed:
                close = getattr(runner, "aclose", None) if runner is not None else None
                if callable(close):
                    with suppress(Exception):
                        await close()
                if isinstance(submission.runtime.metadata, dict):
                    submission.runtime.metadata.update(turn_ctx.metadata)

        if startup_failed:
            startup_status = str(terminal_event.data.get("status") or "failed") if terminal_event else "failed"
            startup_reason = str(terminal_event.data.get("reason") or "startup_failed") if terminal_event else "startup_failed"
            completed = finalize_query_with_journal(
                startup_status,
                startup_reason,
                terminal_event,
            )
            query_finalized = True
            if completed is not None:
                envelope.stamp(completed)
                yield completed
                if completed.type == "error" and completed.data.get("terminal_commit_failed"):
                    startup_status = "failed"
                    startup_reason = "terminal_commit_failed"
                    terminal_event = AgentEvent.done(
                        status=startup_status,
                        reason=startup_reason,
                    )
            observer_finish_error = await self._finish_observer_safely(
                extension_bridge,
                status=startup_status,
                reason=startup_reason,
            )
            if observer_finish_error is not None:
                observer_error = self._observer_projection_error(
                    observer_finish_error,
                    phase="finish",
                )
                journal_event(observer_error)
                envelope.stamp(observer_error)
                yield observer_error
            if terminal_event is None:
                terminal_event = AgentEvent.done(
                    status=startup_status,
                    reason=startup_reason,
                )
            terminal_event.data["status"] = startup_status
            terminal_event.data["reason"] = startup_reason
            envelope.stamp(terminal_event)
            yield terminal_event
            if startup_cancelled is not None:
                raise startup_cancelled
            return

        try:
            if runner is None:
                raise RuntimeError("agent runner was not initialized")
            async for event in runner:
                journal_event(event)
                if event.type == "tool_result":
                    observed_tool_statuses.append(
                        str(
                            event.data.get("status")
                            or ("failed" if event.data.get("is_error") else "success")
                        )
                    )
                elif event.type == "image_chunk":
                    observed_visible_result = True
                elif event.type == "item.completed":
                    item = event.data.get("item")
                    visible_text = (
                        item.get("text") if isinstance(item, dict) else event.data.get("text")
                    )
                    source = str(item.get("source") or "") if isinstance(item, dict) else ""
                    if source == "model_final" and str(visible_text or "").strip():
                        observed_visible_result = True
                if event.type in {
                    "agent.run.started",
                    "agent.run.completed",
                    "agent.terminal.intent",
                }:
                    continue
                if not should_emit_event(event):
                    continue
                observer_error_text = await self._observe_observer_safely(
                    extension_bridge,
                    event,
                )
                if observer_error_text is not None:
                    observer_error = AgentEvent.error(
                        observer_error_text,
                        recoverable=False,
                        error_type="projection",
                        error_code="lifecycle_observer.observe_failed",
                    )
                    journal_event(observer_error)
                    envelope.stamp(observer_error)
                    yield observer_error
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
            terminal_event = AgentEvent.done(
                status="cancelled", reason="user_interrupted"
            )
            completed = finalize_query_with_journal(
                "cancelled",
                "user_interrupted",
                terminal_event,
            )
            query_finalized = True
            commit_failed = self._terminal_commit_failed(completed)
            if commit_failed:
                terminal_event = AgentEvent.done(
                    status="failed",
                    reason="terminal_commit_failed",
                )
            observer_finish_error = await self._finish_observer_safely(
                extension_bridge,
                status="failed" if commit_failed else "cancelled",
                reason="terminal_commit_failed" if commit_failed else "user_interrupted",
            )
            if observer_finish_error is not None:
                observer_error = self._observer_projection_error(
                    observer_finish_error,
                    phase="finish",
                )
                journal_event(observer_error)
                envelope.stamp(observer_error)
                yield observer_error
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
                finalize_query_with_journal("cancelled", "consumer_closed")
                query_finalized = True
                observer_finish_error = await self._finish_observer_safely(
                    extension_bridge,
                    status="cancelled", reason="consumer_closed"
                )
                if observer_finish_error is not None:
                    journal_lifecycle(
                        "projection_error",
                        {"phase": "finish", "message": observer_finish_error},
                    )
            close = getattr(runner, "aclose", None) if runner is not None else None
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
                    reason in _USABLE_PARTIAL_REASONS and has_usable_result
                    or str(reason or "").startswith(("partial_", "recovered_"))
                )
                else "completed"
                if reason in {None, "completed"}
                else "failed"
            )
            terminal_event = AgentEvent.done(status=status, reason=str(reason or ""))
        status = str(terminal_event.data.get("status") or "completed")
        reason = str(terminal_event.data.get("reason") or "")
        state_tool_statuses = [
            str(getattr(record, "status", "") or "")
            for record in getattr(turn_ctx.state, "tool_calls", ())
        ]
        validation = validate_terminal_outcome(
            status=status,
            reason=reason,
            reply=(turn_ctx.state.reply or ("observed" if observed_visible_result else "")),
            tool_statuses=state_tool_statuses or observed_tool_statuses,
            has_non_text_result=observed_visible_result and not bool(turn_ctx.state.reply.strip()),
        )
        if validation.changed:
            status = validation.status
            reason = validation.reason
            turn_ctx.state.terminal_status = status
            turn_ctx.state.stopped_reason = reason
            terminal_event.data["status"] = status
            terminal_event.data["reason"] = reason
            validation_error = AgentEvent.error(
                validation.message,
                recoverable=validation.recoverable,
                error_type="missing_final_answer",
                error_code="agent.missing_final_answer",
            )
            envelope.stamp(validation_error)
            yield validation_error
        if status in {"completed", "partial", "cancelled", "failed"}:
            turn_ctx.state.terminal_status = status
        has_usable_result = bool(turn_ctx.state.reply.strip())
        if (
            reason
            in _USABLE_PARTIAL_REASONS
            and has_usable_result
            and status != "cancelled"
        ):
            status = "partial"
            terminal_event.data["status"] = "partial"
            turn_ctx.state.terminal_status = "partial"
        checkpoint_evidence = (
            turn_ctx.turn_kernel.checkpoint_evidence()
            if turn_ctx.turn_kernel is not None
            else {"status": "none"}
        )
        if (
            status == "completed"
            and bool(turn_ctx.metadata.get("retain_completed_checkpoint"))
            and checkpoint_evidence.get("status") == "save_failed"
        ):
            status = "partial" if turn_ctx.state.reply.strip() else "failed"
            reason = "checkpoint_save_failed"
            turn_ctx.state.terminal_status = status
            turn_ctx.state.stopped_reason = reason
            terminal_event.data["status"] = status
            terminal_event.data["reason"] = reason
            turn_ctx.state.mark_transition("checkpoint_save_failed")
        completed = finalize_query_with_journal(status, reason, terminal_event)
        status = str(terminal_event.data.get("status") or status)
        reason = str(terminal_event.data.get("reason") or reason)
        query_finalized = True
        if terminal_journal_error is not None:
            journal_error_event = AgentEvent.error(
                "MiniCode could not record this run's terminal evidence in the "
                f"execution journal: {terminal_journal_error}",
                recoverable=False,
                error_type="journal",
                error_code="runtime.terminal_journal_failed",
            )
            journal_error_event.data["terminal_journal_failed"] = True
            envelope.stamp(journal_error_event)
            yield journal_error_event
        if self._terminal_commit_failed(completed):
            status = "failed"
            reason = "terminal_commit_failed"
            turn_ctx.state.terminal_status = status
            terminal_event = AgentEvent.done(status=status, reason=reason)
        checkpoint_error = (
            turn_ctx.turn_kernel.checkpoint_failure_event()
            if turn_ctx.turn_kernel is not None
            else None
        )
        if checkpoint_error is not None:
            envelope.stamp(checkpoint_error)
            yield checkpoint_error
        if turn_ctx.turn_kernel is not None:
            terminal_event.data["checkpoint"] = (
                turn_ctx.turn_kernel.checkpoint_evidence()
            )
        # The durable terminal is committed before observer finalization.
        observer_finish_error = await self._finish_observer_safely(
            extension_bridge,
            status=status,
            reason=reason,
        )
        if observer_finish_error is not None:
            observer_error = self._observer_projection_error(
                observer_finish_error,
                phase="finish",
            )
            journal_event(observer_error)
            envelope.stamp(observer_error)
            yield observer_error
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
        runtime_value = metadata.get("agent_runtime")
        if runtime_value is None:
            metadata["agent_runtime"] = default_runtime()
        elif not isinstance(runtime_value, AgentRuntime):
            raise TypeError("Query metadata agent_runtime must be an AgentRuntime")
        if sc.workspace_root is None and submission.workspace_root is not None:
            from pathlib import Path

            sc.workspace_root = Path(submission.workspace_root).expanduser().resolve()
        lifecycle_runtime = resolve_lifecycle_runtime(metadata, session_context=sc)
        if lifecycle_runtime is None:
            lifecycle_runtime = session.lifecycle_runtime
        install_lifecycle_runtime(metadata, lifecycle_runtime)
        # Mutable turn-owned sink.  The loop and adapters share this exact list
        # through copied metadata so side calls remain observable after submit.
        metadata["_side_calls"] = []
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
            llm=session.llm,
        )
        bind_llm = getattr(ctx, "bind_llm", None)
        if callable(bind_llm):
            bind_llm(session.llm)
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
        if sc.workspace_root:
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
            )
        except Exception as exc:
            # ``TurnKernel.create`` itself crosses the durable running boundary
            # before constructing the kernel.  If construction fails, recover
            # the terminal fact here so callers still receive one canonical
            # startup failure envelope.
            completion_event: AgentEvent | None = None
            runtime = metadata.get("agent_runtime")
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
            if recovery.restored:
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
            ) from exc

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
            "cancelled"
            if status == "cancelled"
            else "failed"
            if status == "failed"
            else "partial"
            if status == "partial"
            else "completed"
        )
        retain_completed_checkpoint = bool(
            turn_ctx.metadata.get("retain_completed_checkpoint")
        )
        if run_status == "completed" and retain_completed_checkpoint:
            checkpoint_status = turn_ctx.turn_kernel.finalize_checkpoint(
                session_id=turn_ctx.session_id,
                user_message=turn_ctx.user_message,
                state=turn_ctx.state,
                context_builder=turn_ctx.context_builder,
            )
            if checkpoint_status == "save_failed":
                run_status = "partial" if turn_ctx.state.reply.strip() else "failed"
                reason = "checkpoint_save_failed"
                turn_ctx.state.stopped_reason = reason
        turn_ctx.state.terminal_status = (
            "cancelled" if run_status in {"cancelled", "interrupted"} else run_status
        )
        event = turn_ctx.turn_kernel.complete_run_record(
            run_status,
            summary=reason,
            terminal_reason=reason,
            error=reason if run_status == "failed" else "",
        )
        if (
            run_status == "completed"
            and event is not None
            and event.type == "agent.run.completed"
            and not retain_completed_checkpoint
        ):
            turn_ctx.turn_kernel.finalize_checkpoint(
                session_id=turn_ctx.session_id,
                user_message=turn_ctx.user_message,
                state=turn_ctx.state,
                context_builder=turn_ctx.context_builder,
            )
        return event or turn_ctx.turn_kernel.completion_event


    @staticmethod
    async def _start_observer_safely(observer: Any | None) -> str | None:
        start = getattr(observer, "start", None)
        if not callable(start):
            return None
        try:
            await start()
        except Exception as exc:
            logger.warning(
                "Lifecycle observer start failed; continuing canonical run",
                exc_info=True,
            )
            return f"Lifecycle observer start failed: {exc}"
        return None

    @staticmethod
    async def _observe_observer_safely(
        observer: Any | None,
        event: AgentEvent,
    ) -> str | None:
        observe = getattr(observer, "observe", None)
        if not callable(observe):
            return None
        try:
            await observe(event)
        except Exception as exc:
            logger.warning(
                "Lifecycle observer event failed; continuing canonical run",
                exc_info=True,
            )
            return f"Lifecycle observer event projection failed: {exc}"
        return None

    @staticmethod
    def _terminal_commit_failed(event: AgentEvent | None) -> bool:
        return bool(
            event is not None
            and event.type == "error"
            and event.data.get("terminal_commit_failed") is True
        )

    @staticmethod
    async def _finish_observer_safely(
        observer: Any | None,
        *,
        status: str,
        reason: str,
    ) -> str | None:
        """Finish an optional lifecycle observer without owning terminal state."""

        finish = getattr(observer, "finish", None)
        if not callable(finish):
            return None
        try:
            result = finish(status=status, reason=reason)
            if inspect.isawaitable(result):
                await asyncio.wait_for(
                    result,
                    timeout=_OBSERVER_FINISH_TIMEOUT_SECONDS,
                )
        except Exception as exc:
            # Observers are projections/telemetry. Their failure must never
            # suppress the already-committed canonical terminal or cleanup,
            # but the failure is returned as a structured projection error.
            logger.warning(
                "Lifecycle observer finish failed after canonical terminal",
                exc_info=True,
            )
            return f"Lifecycle observer finish failed: {exc}"
        return None

    @staticmethod
    def _observer_projection_error(
        message: str,
        *,
        phase: str,
    ) -> AgentEvent:
        event = AgentEvent.error(
            message,
            recoverable=False,
            error_type="projection",
            error_code=f"lifecycle_observer.{phase}_failed",
        )
        event.data["projection_phase"] = phase
        return event
