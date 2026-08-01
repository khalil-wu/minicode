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

from backend.agent.message import AgentEvent
from backend.agent.checkpoint import (
    MAX_CHECKPOINT_HISTORY_MESSAGES,
    MAX_CHECKPOINT_TEXT_CHARS,
    clear_checkpoints,
    save_run_checkpoint,
)
from backend.agent.runtime import (
    AgentRunRecord,
    AgentRunStatus,
    AgentRuntime,
    default_runtime,
)
from backend.agent.runtime_spans import epoch_ms, runtime_span
from backend.agent.state import AgentState, TerminalReason, TerminalStatus
from backend.agent.turn_input import TurnInput, TurnInputQueue
from backend.agent.provider_attempt import ProviderAttempt
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
    ) -> None:
        self.runtime = runtime
        self.run_record = run_record
        self.metadata = metadata
        self.emit_event = emit_event
        self.state = state
        self._run_started_emitted = False
        self._run_completed_emitted = False
        self._completion_event: AgentEvent | None = None
        self._tool_context: ToolExecutionContext | None = None
        self._turn_input_queue = metadata.get("turn_input_queue")
        if self._turn_input_queue is None:
            self._turn_input_queue = TurnInputQueue()
            metadata["turn_input_queue"] = self._turn_input_queue
        begin_turn = getattr(self._turn_input_queue, "begin_turn", None)
        if callable(begin_turn):
            begin_turn(run_record.run_id)
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
    ) -> "TurnKernel":
        runtime_value = metadata.get("agent_runtime")
        runtime = runtime_value if isinstance(runtime_value, AgentRuntime) else default_runtime()
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
        )
        metadata.setdefault("run_id", run_record.run_id)
        metadata.setdefault("agent_runtime", runtime)
        if session_id:
            metadata.setdefault("session_id", session_id)
            metadata.setdefault("minicode_session_id", session_id)
        if run_record.conversation_id:
            metadata.setdefault("conversation_id", run_record.conversation_id)
        return cls(
            runtime=runtime,
            run_record=run_record,
            metadata=metadata,
            emit_event=emit_event,
            initial_user_message=initial_user_message,
            state=state,
        )

    @property
    def completion_emitted(self) -> bool:
        return self._run_completed_emitted

    @property
    def completion_event(self) -> AgentEvent | None:
        return self._completion_event

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
        error: str = "",
    ) -> AgentEvent | None:
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
        record = self.runtime.complete_run(
            self.run_record.run_id,
            status,
            summary=summary,
            error=error,
        )
        if record is not None:
            self.run_record = record
        self._run_completed_emitted = True
        self._completion_event = AgentEvent.agent_run_completed(record or self.run_record)
        return self._completion_event

    def defer_mailbox_to_next_turn(self) -> bool:
        defer = getattr(self._turn_input_queue, "defer_mailbox_to_next_turn", None)
        if not callable(defer):
            return False
        return bool(defer(self.run_record.run_id))

    def complete_for_terminal_reason(self, reason: str | None) -> AgentEvent | None:
        terminal_status = self.state.terminal_status
        return self.complete_run_record(
            _terminal_run_status(reason, terminal_status),
            summary=_terminal_run_summary(reason, terminal_status),
            error=_terminal_run_error(reason, terminal_status),
        )

    def interrupt(
        self,
        *,
        context_builder: Any,
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
            partial_item = stream_text.complete_active_agent_message(
                cancelled_final_text or stream_text.active_agent_message_text,
                source="partial",
                status="partial",
            )
            if partial_item is not None:
                events.append(partial_item)
        reconcile = getattr(context_builder, "reconcile_dangling_tool_calls", None)
        if callable(reconcile):
            try:
                reconcile()
            except Exception as exc:
                logger.debug("Cancel-path tool trajectory reconcile failed: %s", exc)
        append_user = getattr(context_builder, "append_user", None)
        if callable(append_user):
            try:
                append_user("[Request interrupted by user]")
            except Exception as exc:
                logger.debug("Cancel-path interruption marker append failed: %s", exc)
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

    def refresh_live_permission_context(self) -> bool:
        tool_context = self._tool_context
        if tool_context is None:
            return False
        provider = tool_context.metadata.get("permission_context_provider")
        if not callable(provider):
            return False
        try:
            current = provider()
        except Exception as exc:
            logger.debug("Live permission context refresh failed: %s", exc)
            return False
        if not isinstance(current, PermissionContext) or current == tool_context.permission:
            return False
        tool_context.permission = current
        # Derived capabilities must change atomically with the permission mode.
        # Otherwise bypass -> default/plan leaves the old network grant cached
        # on the live tool context.
        tool_context.allow_network = current.mode == "bypass"
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
                or ""
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

    async def start_provider_attempt(
        self,
        *,
        iteration_id: str,
        retry_index: int,
        started_at: int | None = None,
    ) -> ProviderAttempt:
        attempt = ProviderAttempt(
            iteration_id=iteration_id,
            retry_index=retry_index,
            span_id=f"provider:{iteration_id}:{retry_index + 1}",
            started_at=started_at if started_at is not None else epoch_ms(),
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
            data={"stream_attempt": attempt.attempt_number},
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
            data={"stream_attempt": attempt.attempt_number},
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
        payload = {"stream_attempt": attempt.attempt_number, **dict(data or {})}
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
        return True

    def pop_turn_steer(self) -> TurnInput | None:
        pop_steer = getattr(self._turn_input_queue, "pop_steer", None)
        if not callable(pop_steer):
            return None
        try:
            item = pop_steer()
        except Exception as exc:
            logger.debug("Turn-local steer queue read failed: %s", exc)
            return None
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
        if item.selected_skills:
            existing_skills = self.state.prompt_context.get("selected_skills")
            merged_skills = {
                (str(skill.get("name") or ""), str(skill.get("path") or "")): dict(skill)
                for skill in existing_skills or []
                if isinstance(skill, dict)
            }
            for skill in item.selected_skills:
                merged_skills[(skill["name"], skill["path"])] = dict(skill)
            self.state.prompt_context["selected_skills"] = list(merged_skills.values())
        if item.selected_plugins:
            from backend.services.plugin_settings_service import resolve_enabled_plugin_mentions

            resolved = resolve_enabled_plugin_mentions(
                item.selected_plugins,
                connected_mcp_servers=self.metadata.get("connected_mcp_servers") or (),
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
        persist = self.metadata.get("persist_consumed_turn_input")
        if callable(persist):
            try:
                result = persist(item)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("Failed to persist consumed turn steer: %s", exc)
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
        acknowledge = self.metadata.get("acknowledge_consumed_turn_input")
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
        context_builder: Any,
    ) -> str:
        if not session_id:
            return "none"
        reason = state.stopped_reason
        terminal_status = state.terminal_status
        if _terminal_run_status(reason, terminal_status) != "completed":
            try:
                snapshot_exporter = getattr(context_builder, "export_snapshot", None)
                snapshot: dict[str, Any] = {}
                if callable(snapshot_exporter):
                    parameters = inspect.signature(snapshot_exporter).parameters
                    accepts_bounds = (
                        "max_messages" in parameters
                        or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in parameters.values()
                        )
                    )
                    snapshot = (
                        snapshot_exporter(
                            max_messages=MAX_CHECKPOINT_HISTORY_MESSAGES,
                            max_chars=MAX_CHECKPOINT_TEXT_CHARS * 4,
                        )
                        if accepts_bounds
                        else snapshot_exporter()
                    )
                save_run_checkpoint(
                    session_id=session_id,
                    user_message=user_message,
                    iterations=state.iterations,
                    reply=state.reply,
                    messages=snapshot.get("history", []),
                    tool_calls=state.tool_calls,
                    active_skills=state.active_skills,
                    disabled_tools=state.disabled_tools,
                    stopped_reason=reason,
                    last_mutation_index=state._last_mutation_index,
                    run_id=self.run_record.run_id,
                    conversation_id=str(getattr(state, "conversation_id", "") or ""),
                    resume_payload={
                        "run_id": self.run_record.run_id,
                        "conversation_id": str(
                            getattr(state, "conversation_id", "") or ""
                        ),
                        "role": self.run_record.role,
                    },
                )
                return "saved"
            except Exception as exc:
                logger.debug("Checkpoint save failed: %s", exc)
                return "save_failed"
        if _terminal_run_status(reason, terminal_status) == "completed":
            try:
                clear_checkpoints(
                    session_id,
                    conversation_id=str(
                        getattr(state, "conversation_id", "")
                        or self.run_record.conversation_id
                        or ""
                    ),
                )
                return "cleared"
            except Exception as exc:
                logger.debug("Checkpoint clear failed: %s", exc)
                return "clear_failed"
        return "none"
