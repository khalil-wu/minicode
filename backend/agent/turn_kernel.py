"""Turn-scoped lifecycle and input arbitration for the agent loop.

The provider loop remains responsible for model/tool sequencing.  This module
owns the state that must be consistent for the whole turn: the runtime record,
terminal projection, live permission refresh, runtime spans, and consumption
of user input promoted into the active turn.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.checkpoint import (
    MAX_CHECKPOINT_HISTORY_MESSAGES,
    MAX_CHECKPOINT_TEXT_CHARS,
    clear_checkpoints,
    context_snapshot_revision,
    save_run_checkpoint,
)
from backend.agent.runtime import (
    AgentRuntime,
    TerminalCommitError,
)
from backend.agent.runtime_records import AgentRunRecord, AgentRunStatus
from backend.agent.run_context import RunContext
from backend.agent.runtime_spans import epoch_ms, runtime_span
from backend.agent.state import AgentState, TerminalReason, TerminalStatus
from backend.agent.turn_input import TurnInput, TurnInputQueue
from backend.agent.provider_attempt import ProviderAttempt, provider_progress_id
from backend.config import TokenBudget
from backend.permissions.context import PermissionContext, ToolExecutionContext


logger = logging.getLogger(__name__)


def _set_terminal_reason(
    state: AgentState,
    reason: TerminalReason,
    *,
    status: TerminalStatus | None = None,
) -> TerminalReason:
    state.stopped_reason = reason
    if status is not None:
        state.terminal_status = status
    return reason


def _terminal_run_status(
    reason: str | None,
    status: TerminalStatus | None = None,
) -> AgentRunStatus:
    if status is not None:
        return status
    if reason == "completed":
        return "completed"
    if str(reason or "").startswith(("partial_", "recovered_")):
        return "partial"
    if reason == "interrupted":
        return "cancelled"
    if not str(reason or "").strip():
        return "partial"
    return "failed"


def _terminal_run_summary(
    reason: str | None,
    status: TerminalStatus | None = None,
) -> str:
    resolved_status = _terminal_run_status(reason, status)
    if reason == "completed":
        return "Final answer committed"
    if str(reason or "").startswith("partial_"):
        return "Partial answer committed after provider interruption"
    if str(reason or "").startswith("recovered_"):
        return "Recovered answer committed"
    if reason == "interrupted":
        return "Interrupted"
    if resolved_status == "partial":
        return f"Partial result retained: {reason or 'unknown'}"
    return f"Run ended: {reason or 'unknown'}"


def _terminal_run_error(
    reason: str | None,
    status: TerminalStatus | None = None,
) -> str:
    resolved_status = _terminal_run_status(reason, status)
    if resolved_status in {"completed", "partial"}:
        return ""
    if resolved_status == "cancelled":
        return "cancelled"
    return reason or "unknown"


@dataclass(frozen=True, slots=True)
class TurnBoundaryInput:
    content: str
    attachments: tuple[dict[str, Any], ...] | None
    consumed_steer: TurnInput | None
    should_start_turn: bool


class TurnKernel:
    """Own one turn's runtime identity and safe-boundary control input."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        run_record: AgentRunRecord,
        metadata: dict[str, Any],
        emit_event: Any | None,
        initial_user_message: str,
        state: AgentState,
        run_context: RunContext | None = None,
    ) -> None:
        self.runtime = runtime
        self.run_record = run_record
        self.metadata = metadata
        self.emit_event = emit_event
        self.state = state
        self.run_context = run_context or RunContext()
        self._run_started_emitted = False
        self._run_completed_emitted = False
        self._completion_event: AgentEvent | None = None
        self._terminal_commit_failure_event: AgentEvent | None = None
        self._tool_context: ToolExecutionContext | None = None
        turn_input_queue = self.run_context.turn_input_queue
        self._turn_input_queue = (
            turn_input_queue if isinstance(turn_input_queue, TurnInputQueue) else TurnInputQueue()
        )
        self._turn_input_queue.begin_turn(run_record.run_id)
        self._next_user_message = initial_user_message
        self._next_user_attachments: tuple[dict[str, Any], ...] | None = None
        self._scheduled_steer: TurnInput | None = None
        self._provider_call_count = 0

    @classmethod
    def create(
        cls,
        *,
        metadata: dict[str, Any],
        state: AgentState,
        budget: TokenBudget,
        task_id: str,
        session_id: str,
        emit_event: Any | None,
        initial_user_message: str,
        run_context: RunContext | None = None,
    ) -> "TurnKernel":
        owner_context = run_context or RunContext()
        runtime_value = owner_context.agent_runtime
        if not isinstance(runtime_value, AgentRuntime):
            raise RuntimeError(
                "RunContext does not own an AgentRuntime"
            )
        runtime = runtime_value
        run_record = runtime.start_run(
            conversation_id=str(
                getattr(state, "conversation_id", "")
                or metadata.get("conversation_id", "")
                or ""
            ),
            parent_run_id=str(metadata.get("parent_run_id", "") or ""),
            role=str(metadata.get("agent_role", "main") or "main"),
            task_id=task_id,
            session_id=session_id,
            budget=budget,
            run_id=str(metadata.get("run_id", "") or "") or None,
            mailbox_epoch=int(metadata.get("mailbox_epoch") or 0),
        )
        # ``run_id`` owns the durable runtime record. ``turn_id`` remains the
        # host/app-server turn identity when one was supplied; the two are
        # intentionally distinct so runtime ownership cannot overwrite the
        # transport identity used to route a turn's UI events.
        metadata["run_id"] = run_record.run_id
        metadata.setdefault(
            "turn_id",
            str(metadata.get("assistant_message_id") or run_record.run_id),
        )
        if session_id:
            metadata.setdefault("session_id", session_id)
            metadata.setdefault("minicode_session_id", session_id)
        if run_record.conversation_id:
            metadata.setdefault("conversation_id", run_record.conversation_id)
        try:
            return cls(
                runtime=runtime,
                run_record=run_record,
                metadata=metadata,
                emit_event=emit_event,
                initial_user_message=initial_user_message,
                state=state,
                run_context=owner_context,
            )
        except Exception:
            # ``start_run`` has already crossed the durable running boundary.
            # If turn construction fails, close that run before propagating the
            # setup error; otherwise recovery sees an unexplained permanent
            # ``running`` record.
            try:
                runtime.commit_terminal(
                    run_record.run_id,
                    "failed",
                    summary="startup_failed",
                    terminal_reason="startup_failed",
                    error="startup_failed",
                )
            except Exception:
                logger.warning(
                    "Unable to abort run after TurnKernel construction failed",
                    exc_info=True,
                )
            raise

    @property
    def completion_emitted(self) -> bool:
        return self._run_completed_emitted

    @property
    def completion_event(self) -> AgentEvent | None:
        return self._completion_event

    @property
    def terminal_commit_failure_event(self) -> AgentEvent | None:
        """Return the explicit evidence for a failed durable terminal commit."""

        return self._terminal_commit_failure_event

    def _record_terminal_commit_failure(self, error: TerminalCommitError) -> AgentEvent:
        if self._terminal_commit_failure_event is not None:
            return self._terminal_commit_failure_event
        self.state.stopped_reason = "terminal_commit_failed"
        self.state.terminal_status = "failed"
        event = AgentEvent.error(
            "MiniCode could not durably commit the run terminal state.",
            recoverable=False,
            error_type="terminal_commit_failed",
            error_code=f"runtime.{error.failure_kind}",
        )
        event.data.update(
            {
                "run_id": self.run_record.run_id,
                "terminal_commit_failed": True,
                "failure_kind": error.failure_kind,
            }
        )
        self._terminal_commit_failure_event = event
        return event

    def start_events(self) -> tuple[AgentEvent, ...]:
        if self._run_started_emitted:
            return ()
        self._run_started_emitted = True
        return (AgentEvent.agent_run_started(self.run_record),)

    def complete_run_record(
        self,
        status: AgentRunStatus,
        *,
        summary: str = "",
        terminal_reason: str = "",
        error: str = "",
    ) -> AgentEvent | None:
        if self._terminal_commit_failure_event is not None:
            return None
        normalized_status: TerminalStatus = (
            "cancelled"
            if status in {"cancelled", "interrupted"}
            else "partial"
            if status == "partial"
            else "failed"
            if status == "failed"
            else "completed"
        )
        self.state.terminal_status = normalized_status
        self.defer_mailbox_to_next_turn()
        if self._run_completed_emitted:
            return None
        try:
            record = self.runtime.commit_terminal(
                self.run_record.run_id,
                status,
                summary=summary,
                terminal_reason=terminal_reason,
                error=error,
            )
        except TerminalCommitError as exc:
            return self._record_terminal_commit_failure(exc)
        self.run_record = record
        self._run_completed_emitted = True
        self._completion_event = AgentEvent.agent_run_completed(record or self.run_record)
        return self._completion_event

    def defer_mailbox_to_next_turn(self) -> bool:
        return bool(self._turn_input_queue.defer_mailbox_to_next_turn(self.run_record.run_id))

    def complete_for_terminal_reason(self, reason: str | None) -> AgentEvent | None:
        terminal_status = self.state.terminal_status
        return self.complete_run_record(
            _terminal_run_status(reason, terminal_status),
            summary=_terminal_run_summary(reason, terminal_status),
            terminal_reason=str(reason or ""),
            error=_terminal_run_error(reason, terminal_status),
        )

    def abort_startup(self, *, reason: str = "startup_failed") -> AgentEvent | None:
        """Close a run that failed before the provider loop became usable."""

        _set_terminal_reason(self.state, reason, status="failed")
        return self.complete_run_record(
            "failed",
            summary=reason,
            terminal_reason=reason,
            error=reason,
        )

    def interrupt(
        self,
        *,
        context_builder: ContextBuilder,
        stream_text: Any,
        scrub_text: Callable[[str], str],
    ) -> tuple[AgentEvent, ...]:
        """Commit the resumable history contract for a cancelled turn."""

        _set_terminal_reason(self.state, "interrupted", status="cancelled")
        cancelled_final_text = (
            scrub_text(stream_text.final_candidate_text)
            if stream_text.saw_final_answer_phase
            and stream_text.final_candidate_text.strip()
            else ""
        )
        if cancelled_final_text:
            context_builder.append_assistant(cancelled_final_text)
        events: list[AgentEvent] = []
        if stream_text.agent_message_started:
            # Only text the provider actually phased as the final answer is a
            # truncated answer. Unphased narration interrupted mid-flight is
            # process text, and marking it ``partial`` promoted it into the
            # persisted/copyable reply. cc, codex and pi all leave such text
            # classified as it was and record the interruption separately.
            interrupted_item = (
                stream_text.complete_active_agent_message(
                    cancelled_final_text,
                    source="partial",
                    status="partial",
                )
                if cancelled_final_text
                else stream_text.cancel_active_agent_message()
            )
            if interrupted_item is not None:
                events.append(interrupted_item)
        try:
            context_builder.reconcile_dangling_tool_calls()
        except Exception as exc:
            # The cancelled trajectory is the resumption contract for the
            # next turn; a failed repair must be visible evidence, not a
            # debug-only footnote.
            logger.warning("Cancel-path tool trajectory reconcile failed: %s", exc)
            events.append(
                AgentEvent(
                    type="system_notice",
                    data={
                        "title": "Interrupt recovery incomplete",
                        "message": (
                            "Dangling tool calls could not be reconciled while "
                            "cancelling; the next request may be rejected by the "
                            "provider until the history is repaired."
                        ),
                        "severity": "error",
                        "cancel_recovery_failure": "tool_call_reconcile_failed",
                        "detail": str(exc),
                    },
                )
            )
        try:
            context_builder.append_user("[Request interrupted by user]")
        except Exception as exc:
            logger.warning("Cancel-path interruption marker append failed: %s", exc)
            events.append(
                AgentEvent(
                    type="system_notice",
                    data={
                        "title": "Interrupt marker not recorded",
                        "message": (
                            "The interruption marker could not be appended to "
                            "the conversation history."
                        ),
                        "severity": "error",
                        "cancel_recovery_failure": "interrupt_marker_append_failed",
                        "detail": str(exc),
                    },
                )
            )
        return tuple(events)

    @property
    def next_provider_call_count(self) -> int:
        return self._provider_call_count + 1

    def commit_provider_call(self, iteration_id: str) -> tuple[int, str]:
        self._provider_call_count += 1
        return (
            self._provider_call_count,
            f"{iteration_id}:provider:{self._provider_call_count}",
        )

    def bind_tool_context(self, tool_context: ToolExecutionContext) -> None:
        self._tool_context = tool_context
        if tool_context.run_context is not None:
            self.run_context = tool_context.run_context

    def refresh_live_permission_context(self) -> bool:
        tool_context = self._tool_context
        if tool_context is None:
            return False
        provider = self.run_context.permission_context_provider
        if not callable(provider):
            return False
        try:
            current = provider()
        except Exception as exc:
            logger.debug("Live permission context refresh failed: %s", exc)
            return False
        if not isinstance(current, PermissionContext) or current == tool_context.permission:
            return False
        committer = tool_context.permission_context_committer
        if not callable(committer):
            # Subagent tool contexts carry a provider without a committer;
            # fail closed the same way as a committer that raises.
            logger.warning(
                "Managed permission context refresh failed closed: no turn-owned committer"
            )
            return False
        try:
            committer(current)
        except Exception as exc:
            logger.warning("Managed permission context refresh failed closed: %s", exc)
            return False
        return True

    async def emit_runtime_span(
        self,
        event: str,
        *,
        span_id: str,
        iteration_id: str = "",
        phase: str = "",
        status: str = "running",
        label: str = "",
        summary: str = "",
        started_at: int | None = None,
        ended_at: int | None = None,
        duration_ms: int | None = None,
        ui_visible: bool = True,
        debug_only: bool = False,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self.emit_event is None:
            return
        event_value = runtime_span(
            event,
            span_id=span_id,
            run_id=self.run_record.run_id,
            turn_id=str(
                self.metadata.get("turn_id")
                or self.metadata.get("assistant_message_id")
                or self.run_record.run_id
            ),
            iteration_id=iteration_id,
            phase=phase,
            status=status,
            label=label,
            summary=summary,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            agent_id=self.run_record.role,
            ui_visible=ui_visible,
            debug_only=debug_only,
            data=data,
        )
        await self.emit_event(event_value.type, dict(event_value.data))

    async def emit_provider_progress(
        self,
        message: str,
        *,
        iteration_id: str,
        progress_id: str = "",
        status: str = "running",
        phase: str = "model",
        detail: str = "",
        count: int | None = None,
        summary: str = "",
        retry_attempt: int | None = None,
        max_retries: int | None = None,
        retry_after_ms: int | None = None,
        error_message: str = "",
        provider_state: str | None = None,
    ) -> None:
        """Project provider liveness through the single typed UI event path."""
        if self.emit_event is None:
            return
        event = AgentEvent.progress(
            message,
            stage="status",
            status=status,
            id=progress_id or provider_progress_id(iteration_id),
            phase=phase,
            label="provider",
            summary=summary or message,
            detail=detail,
            visibility="timeline",
            count=count,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            retry_after_ms=retry_after_ms,
            error_message=error_message,
            provider_state=provider_state,
            operation_id=progress_id,
        )
        await self.emit_event(event.type, dict(event.data))

    async def start_provider_attempt(
        self,
        *,
        iteration_id: str,
        retry_index: int,
        started_at: int | None = None,
        total_attempts: int = 0,
        max_retries: int | None = None,
        progress_id: str = "",
    ) -> ProviderAttempt:
        resolved_max_retries = max(
            0,
            int(
                total_attempts
                if max_retries is None
                else max_retries
            ),
        )
        resolved_progress_id = progress_id or provider_progress_id(iteration_id)
        span_owner = str(self.run_record.run_id or "").strip()
        resolved_span_id = (
            f"provider:{span_owner}:{iteration_id}:{retry_index + 1}"
            if span_owner
            else f"provider:{iteration_id}:{retry_index + 1}"
        )
        attempt = ProviderAttempt(
            iteration_id=iteration_id,
            retry_index=retry_index,
            span_id=resolved_span_id,
            started_at=started_at if started_at is not None else epoch_ms(),
            progress_id=resolved_progress_id,
            max_retries=resolved_max_retries,
        )
        self.metadata["provider_max_retries"] = resolved_max_retries
        # The initial request is not a retry.  It has no user-facing counter;
        # retry attempts are rendered by their retry ordinal (1/N … N/N).
        if attempt.retry_attempt > 0 and resolved_max_retries > 0:
            attempt_message = (
                f"正在重连（第 {attempt.retry_attempt}/{resolved_max_retries} 次）"
            )
            attempt_count: int | None = attempt.retry_attempt
        else:
            attempt_message = "正在连接提供商"
            attempt_count = None
        await self.emit_provider_progress(
            attempt_message,
            iteration_id=iteration_id,
            progress_id=resolved_progress_id,
            provider_state=(
                "reconnecting" if attempt.retry_attempt > 0 else "connecting"
            ),
            phase="model",
            count=attempt_count,
            detail="等待提供商首个响应事件",
            retry_attempt=attempt.retry_attempt,
            max_retries=resolved_max_retries,
        )
        await self.emit_runtime_span(
            "provider.request.started",
            span_id=attempt.span_id,
            iteration_id=iteration_id,
            phase="provider",
            status="running",
            label="provider",
            summary="Provider request started",
            started_at=attempt.started_at,
            ui_visible=False,
            data={
                "stream_attempt": attempt.attempt_number,
                "retry_attempt": attempt.retry_attempt,
                "max_retries": resolved_max_retries,
            },
        )
        return attempt

    async def observe_provider_first_event(
        self,
        attempt: ProviderAttempt,
        *,
        progress_origin_ms: int,
        observed_at: int | None = None,
    ) -> int | None:
        if attempt.first_byte_at is None:
            attempt.first_byte_at = observed_at if observed_at is not None else epoch_ms()
        if attempt.first_event_reported:
            return None
        attempt.first_event_reported = True
        wait_ms = max(0, attempt.first_byte_at - progress_origin_ms)
        await self.emit_provider_progress(
            (
                "已连接，模型正在响应"
                if attempt.retry_attempt == 0
                else f"已连接，模型正在响应（重试 {attempt.retry_attempt}/{attempt.max_retries}）"
            ),
            iteration_id=attempt.iteration_id,
            progress_id=attempt.progress_id,
            provider_state="responding",
            status="running",
            phase="model",
            count=attempt.retry_attempt or None,
            detail=f"首个响应事件等待 {wait_ms}ms",
            summary="Provider connection established",
            retry_attempt=attempt.retry_attempt,
            max_retries=attempt.max_retries,
        )
        await self.emit_runtime_span(
            "provider.first_event",
            span_id=attempt.span_id,
            iteration_id=attempt.iteration_id,
            phase="provider",
            status="completed",
            label="provider",
            summary=f"First provider event after {wait_ms}ms",
            started_at=attempt.started_at,
            ended_at=attempt.first_byte_at,
            duration_ms=max(0, attempt.first_byte_at - attempt.started_at),
            ui_visible=False,
            data={
                "stream_attempt": attempt.attempt_number,
                "retry_attempt": attempt.retry_attempt,
                "max_retries": attempt.max_retries,
            },
        )
        return wait_ms

    async def close_provider_attempt(
        self,
        attempt: ProviderAttempt | None,
        *,
        status: str,
        summary: str,
        event: str | None = None,
        ended_at: int | None = None,
        data: dict[str, Any] | None = None,
        project_progress: bool = True,
    ) -> bool:
        if attempt is None or attempt.closed:
            return False
        attempt.closed = True
        resolved_ended_at = ended_at if ended_at is not None else epoch_ms()
        resolved_event = event or (
            "provider.request.completed"
            if status == "completed"
            else "provider.request.cancelled"
            if status in {"cancelled", "superseded"}
            else "provider.request.failed"
        )
        payload = {
            "stream_attempt": attempt.attempt_number,
            "retry_attempt": attempt.retry_attempt,
            "max_retries": attempt.max_retries,
            **dict(data or {}),
        }
        await self.emit_runtime_span(
            resolved_event,
            span_id=attempt.span_id,
            iteration_id=attempt.iteration_id,
            phase="provider",
            status=status,
            label="provider",
            summary=summary,
            started_at=attempt.started_at,
            ended_at=resolved_ended_at,
            duration_ms=max(0, resolved_ended_at - attempt.started_at),
            ui_visible=False,
            data=payload,
        )
        if project_progress:
            max_retries = max(
                0,
                int(
                    attempt.max_retries
                    or self.metadata.get("provider_max_retries")
                    or 0
                ),
            )
            retry_attempt = min(attempt.retry_attempt, max_retries)
            detail_parts: list[str] = []
            for key, label in (
                ("status_code", "HTTP"),
                ("provider_error_code", "code"),
                ("provider_error_schema_type", "type"),
                ("provider_error_type", "provider"),
                ("error_type", "error"),
            ):
                value = payload.get(key)
                if value not in (None, ""):
                    detail_parts.append(f"{label}={value}")
            if status == "completed":
                progress_status = "completed"
                provider_state = "completed"
                progress_message = (
                    "提供商响应完成"
                    if retry_attempt == 0
                    else f"提供商响应完成（重试 {retry_attempt}/{max_retries}）"
                )
            elif status in {"cancelled", "superseded", "interrupted"}:
                progress_status = "partial"
                provider_state = "interrupted"
                progress_message = "提供商请求已取消"
            else:
                progress_status = "failed"
                provider_state = "failed"
                progress_message = (
                    "提供商请求失败"
                    if retry_attempt == 0
                    else f"提供商请求失败（重试 {retry_attempt}/{max_retries} 后）"
                )
            await self.emit_provider_progress(
                progress_message,
                iteration_id=attempt.iteration_id,
                progress_id=attempt.progress_id,
                status=progress_status,
                phase="model",
                count=retry_attempt or None,
                detail=" · ".join(detail_parts),
                summary=summary,
                retry_attempt=retry_attempt,
                max_retries=max_retries,
                provider_state=provider_state,
            )
        return True

    def pop_turn_steer(self) -> TurnInput | None:
        item = self._turn_input_queue.pop_steer()
        return item if isinstance(item, TurnInput) else None

    def schedule_user_input(
        self,
        content: str,
        attachments: tuple[dict[str, Any], ...] | None = None,
        *,
        steer: TurnInput | None = None,
    ) -> None:
        self._next_user_message = content
        self._next_user_attachments = attachments
        self._scheduled_steer = steer

    def discard_scheduled_user_input(self) -> None:
        """Drop bootstrap input when durable history already owns the turn."""
        self._next_user_message = ""
        self._next_user_attachments = None
        self._scheduled_steer = None

    async def accept_turn_steer(self, item: TurnInput) -> None:
        # Skill selections belong to one user message. A steered message owns
        # its exact selection and cannot inherit the prior message's skills.
        if item.selected_skills:
            self.state.prompt_context["selected_skills"] = [
                dict(skill) for skill in item.selected_skills
            ]
        else:
            self.state.prompt_context.pop("selected_skills", None)
        self.state.prompt_context.pop("skill_injections", None)
        if item.selected_plugins:
            from backend.services.plugin_settings_service import resolve_enabled_plugin_mentions

            resolved = resolve_enabled_plugin_mentions(
                item.selected_plugins,
                connected_mcp_servers=self.run_context.connected_mcp_servers,
            )
            if resolved:
                existing_plugins = self.state.prompt_context.get("plugin_injections")
                merged_plugins = {
                    str(plugin.get("config_name") or "").casefold(): dict(plugin)
                    for plugin in existing_plugins or []
                    if isinstance(plugin, dict) and str(plugin.get("config_name") or "").strip()
                }
                for plugin in resolved:
                    merged_plugins[str(plugin.get("config_name") or "").casefold()] = dict(plugin)
                self.state.prompt_context["plugin_injections"] = list(merged_plugins.values())
        persist = self.run_context.persist_consumed_turn_input
        if callable(persist):
            try:
                result = persist(item)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                # The original command remains in the durable turn-input set.
                # Fail before scheduling it into model context so run cleanup
                # can restore it as a follow-up without duplicating a message
                # that the model already observed but history lost.
                raise RuntimeError(
                    "Failed to persist the steered user message before delivery"
                ) from exc
        self.schedule_user_input(item.content, item.attachments, steer=item)

    async def take_boundary_input(self, *, initial_turn_pending: bool) -> TurnBoundaryInput:
        if (
            not self._next_user_message
            and not self._next_user_attachments
            and self._scheduled_steer is None
            and not initial_turn_pending
        ):
            queued_steer = self.pop_turn_steer()
            if queued_steer is not None:
                await self.accept_turn_steer(queued_steer)
        content = self._next_user_message
        attachments = self._next_user_attachments
        consumed_steer = self._scheduled_steer
        self._next_user_message = ""
        self._next_user_attachments = None
        self._scheduled_steer = None
        return TurnBoundaryInput(
            content=content,
            attachments=attachments,
            consumed_steer=consumed_steer,
            should_start_turn=initial_turn_pending or bool(content) or bool(attachments),
        )

    async def acknowledge_boundary_input(self, boundary_input: TurnBoundaryInput) -> None:
        item = boundary_input.consumed_steer
        if item is None:
            return
        acknowledge = self.run_context.acknowledge_consumed_turn_input
        if not callable(acknowledge):
            return
        try:
            result = acknowledge(item)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # Keep the durable inflight item for at-least-once replay. The
            # current run can continue because the input is already in context.
            logger.warning("Failed to acknowledge consumed turn steer: %s", exc)

    def finalize_checkpoint(
        self,
        *,
        session_id: str,
        user_message: str,
        state: AgentState,
        context_builder: ContextBuilder,
        defer_completed_clear: bool = False,
    ) -> str:
        if not session_id:
            self.metadata["checkpoint_status"] = "none"
            return "none"
        reason = state.stopped_reason
        terminal_status = state.terminal_status
        resolved_status = _terminal_run_status(reason, terminal_status)
        retain_completed = bool(self.metadata.get("retain_completed_checkpoint"))
        if resolved_status == "completed" and retain_completed:
            current_status = str(self.metadata.get("checkpoint_status") or "")
            if current_status in {"saved", "save_failed"}:
                return current_status
            return self._save_checkpoint(
                session_id=session_id,
                user_message=user_message,
                state=state,
                context_builder=context_builder,
                reason="idle",
            )
        if resolved_status != "completed":
            return self._save_checkpoint(
                session_id=session_id,
                user_message=user_message,
                state=state,
                context_builder=context_builder,
                reason=reason,
            )
        if defer_completed_clear:
            # A completed turn's checkpoint is cleared only after its terminal
            # commit lands: ``_finalize_query`` calls this again without the
            # deferral once the commit succeeds. A failed commit therefore leaves
            # the turn's mid-run checkpoints in place as the resume handle.
            self.metadata["checkpoint_status"] = "pending_clear"
            self.metadata.pop("checkpoint_error", None)
            return "pending_clear"
        self.metadata.pop("checkpoint_error", None)
        try:
            clear_checkpoints(
                session_id,
                conversation_id=str(
                    getattr(state, "conversation_id", "")
                    or self.run_record.conversation_id
                    or ""
                ),
            )
            self.metadata["checkpoint_status"] = "cleared"
            self.metadata.pop("checkpoint_context_revision", None)
            self.metadata.pop("checkpoint_sequence", None)
            self.metadata.pop("checkpoint_schema_version", None)
            return "cleared"
        except Exception as exc:
            self.metadata["checkpoint_status"] = "clear_failed"
            self.metadata["checkpoint_error"] = type(exc).__name__
            logger.warning("Checkpoint clear failed: %s", exc)
            return "clear_failed"

    def _save_checkpoint(
        self,
        *,
        session_id: str,
        user_message: str,
        state: AgentState,
        context_builder: ContextBuilder,
        reason: str,
    ) -> str:
        """Persist one resumable checkpoint and report the durable outcome."""
        self.metadata.pop("checkpoint_error", None)
        self.metadata.pop("checkpoint_context_revision", None)
        self.metadata.pop("checkpoint_sequence", None)
        self.metadata.pop("checkpoint_schema_version", None)
        try:
            snapshot = context_builder.export_snapshot(
                max_messages=MAX_CHECKPOINT_HISTORY_MESSAGES,
                max_chars=MAX_CHECKPOINT_TEXT_CHARS * 4,
            )
            receipt: dict[str, Any] = {}
            context_revision = context_snapshot_revision(snapshot)
            checkpoint_origin = self.metadata.get("checkpoint_origin")
            resume_payload: dict[str, Any] = {
                "run_id": self.run_record.run_id,
                "conversation_id": str(getattr(state, "conversation_id", "") or ""),
                "role": self.run_record.role,
            }
            if isinstance(checkpoint_origin, dict) and checkpoint_origin:
                resume_payload["parent_checkpoint"] = dict(checkpoint_origin)
            save_run_checkpoint(
                receipt=receipt,
                session_id=session_id,
                user_message=user_message,
                iterations=state.iterations,
                reply=state.reply,
                messages=snapshot.get("history", []),
                context_snapshot=snapshot,
                tool_calls=state.tool_calls,
                active_skills=state.active_skills,
                disabled_tools=state.disabled_tools,
                loaded_deferred_tools=state.loaded_deferred_tools,
                stopped_reason=reason,
                last_mutation_index=state._last_mutation_index,
                run_id=self.run_record.run_id,
                conversation_id=str(getattr(state, "conversation_id", "") or ""),
                resume_payload=resume_payload,
            )
            self.metadata["checkpoint_status"] = "saved"
            self.metadata["checkpoint_context_revision"] = str(
                receipt.get("context_revision") or context_revision
            )
            self.metadata["checkpoint_sequence"] = int(receipt.get("sequence") or 0)
            self.metadata["checkpoint_schema_version"] = int(
                receipt.get("schema_version") or 0
            )
            return "saved"
        except Exception as exc:
            self.metadata["checkpoint_status"] = "save_failed"
            self.metadata["checkpoint_error"] = type(exc).__name__
            logger.warning("Checkpoint save failed: %s", exc)
            return "save_failed"

    def checkpoint_failure_event(self) -> AgentEvent | None:
        """Project a failed checkpoint cleanup without changing run authority."""
        status = str(self.metadata.get("checkpoint_status") or "none")
        if status not in {"save_failed", "clear_failed"}:
            return None
        operation = "save" if status == "save_failed" else "clear"
        return AgentEvent.error(
            (
                "MiniCode could not save a resumable checkpoint for this stopped turn."
                if operation == "save"
                else "MiniCode completed the turn, but could not clear its stale run checkpoint."
            ),
            recoverable=False,
            error_type="checkpoint",
            error_code=f"checkpoint.{status}",
        )

    def checkpoint_evidence(self) -> dict[str, Any]:
        """Return the public-safe receipt for this turn's resume boundary."""

        status = str(self.metadata.get("checkpoint_status") or "none")
        evidence: dict[str, Any] = {"status": status}
        revision = str(
            self.metadata.get("checkpoint_context_revision") or ""
        ).strip()
        if revision:
            evidence["context_revision"] = revision
        sequence = int(self.metadata.get("checkpoint_sequence") or 0)
        if sequence:
            evidence["sequence"] = sequence
        schema_version = int(
            self.metadata.get("checkpoint_schema_version") or 0
        )
        if schema_version:
            evidence["schema_version"] = schema_version
        error_type = str(self.metadata.get("checkpoint_error") or "").strip()
        if error_type:
            evidence["error_type"] = error_type
        return evidence
