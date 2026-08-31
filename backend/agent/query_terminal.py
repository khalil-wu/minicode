"""Canonical terminal transaction for one QueryEngine turn."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.agent.query_journal import (
    QueryJournalRecorder,
    terminal_commit_failed,
    terminal_journal_failure_event,
)
from backend.agent.runtime_records import AgentRunStatus
from backend.agent.terminal_validation import validate_terminal_outcome

if TYPE_CHECKING:
    from backend.agent.query_engine import QueryTurnContext


logger = logging.getLogger(__name__)

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


@dataclass(frozen=True, slots=True)
class QueryTerminalResult:
    status: str
    reason: str
    terminal_event: AgentEvent
    completion_event: AgentEvent | None
    evidence_events: tuple[AgentEvent, ...] = ()


@dataclass(slots=True)
class QueryTerminalTransaction:
    """Validate, durably commit, and project one terminal outcome exactly once."""

    turn_ctx: QueryTurnContext
    journal: QueryJournalRecorder
    observed_tool_statuses: list[str] = field(default_factory=list)
    observed_visible_result: bool = False
    _result: QueryTerminalResult | None = None

    @property
    def finalized(self) -> bool:
        return self._result is not None

    def observe_runner_event(self, event: AgentEvent) -> None:
        if event.type == "tool_result":
            self.observed_tool_statuses.append(
                str(
                    event.data.get("status")
                    or ("failed" if event.data.get("is_error") else "success")
                )
            )
            return
        if event.type == "image_chunk":
            self.observed_visible_result = True
            return
        if event.type != "item.completed":
            return
        item = event.data.get("item")
        if isinstance(item, dict):
            visible_text = item.get("text")
            source = str(item.get("source") or "")
        else:
            visible_text = event.data.get("text")
            source = ""
        if source == "model_final" and str(visible_text or "").strip():
            self.observed_visible_result = True

    def accept_done(self, event: AgentEvent) -> AgentEvent:
        status = str(event.data.get("status") or "completed")
        if status in {"completed", "partial", "cancelled", "failed"}:
            self.turn_ctx.state.terminal_status = status
        return event

    def default_terminal(self) -> AgentEvent:
        state = self.turn_ctx.state
        reason = state.stopped_reason
        has_usable_result = bool(state.reply.strip())
        status = (
            state.terminal_status
            if state.terminal_status is not None
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
        return AgentEvent.done(status=status, reason=str(reason or ""))

    def commit(
        self,
        terminal_event: AgentEvent,
        *,
        validate: bool,
    ) -> QueryTerminalResult:
        if self._result is not None:
            return self._result

        evidence_events: list[AgentEvent] = []
        status = str(terminal_event.data.get("status") or "completed")
        reason = str(terminal_event.data.get("reason") or "")
        if validate:
            validation = validate_terminal_outcome(
                status=status,
                reason=reason,
                reply=(
                    self.turn_ctx.state.reply
                    or ("observed" if self.observed_visible_result else "")
                ),
                tool_statuses=(
                    [str(record.status or "") for record in self.turn_ctx.state.tool_calls]
                    or self.observed_tool_statuses
                ),
                has_non_text_result=(
                    self.observed_visible_result
                    and not bool(self.turn_ctx.state.reply.strip())
                ),
            )
            if validation.changed:
                status = validation.status
                reason = validation.reason
                self._apply_status(terminal_event, status, reason)
                evidence_events.append(
                    AgentEvent.error(
                        validation.message,
                        recoverable=validation.recoverable,
                        error_type="missing_final_answer",
                        error_code="agent.missing_final_answer",
                    )
                )

        if status in {"completed", "partial", "cancelled", "failed"}:
            self.turn_ctx.state.terminal_status = status
        if (
            reason in _USABLE_PARTIAL_REASONS
            and self.turn_ctx.state.reply.strip()
            and status != "cancelled"
        ):
            status = "partial"
            self._apply_status(terminal_event, status, reason)

        status, reason = self._finalize_retained_checkpoint(
            terminal_event,
            status=status,
            reason=reason,
        )

        journal_errors: list[BaseException] = []
        try:
            self.journal.record_terminal_intent(terminal_event)
        except Exception as exc:
            journal_errors.append(exc)
            logger.error(
                "Execution journal could not record terminal intent for run %s: %s",
                str(self.turn_ctx.metadata.get("run_id") or ""),
                exc,
                exc_info=True,
            )

        completion_event = self._commit_runtime(status=status, reason=reason)
        commit_failed = terminal_commit_failed(completion_event)
        if not commit_failed:
            self._finalize_completed_checkpoint(status)

        try:
            if completion_event is not None:
                self.journal.record_event(completion_event)
            self.journal.record_terminal(terminal_event)
        except Exception as exc:
            journal_errors.append(exc)
            logger.error(
                "Execution journal could not record the terminal for run %s: %s",
                str(self.turn_ctx.metadata.get("run_id") or ""),
                exc,
                exc_info=True,
            )

        if journal_errors:
            evidence_events.append(terminal_journal_failure_event(journal_errors[0]))

        if commit_failed:
            status = "failed"
            reason = "terminal_commit_failed"
            self.turn_ctx.state.terminal_status = status
            self.turn_ctx.state.stopped_reason = reason
            terminal_event = AgentEvent.done(status=status, reason=reason)

        if self.turn_ctx.turn_kernel is not None:
            checkpoint_error = self.turn_ctx.turn_kernel.checkpoint_failure_event()
            if checkpoint_error is not None:
                evidence_events.append(checkpoint_error)
            terminal_event.data["checkpoint"] = (
                self.turn_ctx.turn_kernel.checkpoint_evidence()
            )

        self._result = QueryTerminalResult(
            status=status,
            reason=reason,
            terminal_event=terminal_event,
            completion_event=completion_event,
            evidence_events=tuple(evidence_events),
        )
        return self._result

    def record_post_commit_event(self, event: AgentEvent) -> AgentEvent | None:
        try:
            self.journal.record_event(event)
        except Exception as exc:
            logger.error(
                "Execution journal could not record post-terminal evidence for run %s: %s",
                str(self.turn_ctx.metadata.get("run_id") or ""),
                exc,
                exc_info=True,
            )
            return terminal_journal_failure_event(exc)
        return None

    def _finalize_retained_checkpoint(
        self,
        terminal_event: AgentEvent,
        *,
        status: str,
        reason: str,
    ) -> tuple[str, str]:
        turn_kernel = self.turn_ctx.turn_kernel
        if (
            status != "completed"
            or turn_kernel is None
            or not bool(self.turn_ctx.metadata.get("retain_completed_checkpoint"))
        ):
            return status, reason
        checkpoint_status = str(
            turn_kernel.checkpoint_evidence().get("status") or "none"
        )
        if checkpoint_status != "save_failed":
            checkpoint_status = turn_kernel.finalize_checkpoint(
                session_id=self.turn_ctx.session_id,
                user_message=self.turn_ctx.user_message,
                state=self.turn_ctx.state,
                context_builder=self.turn_ctx.context_builder,
            )
        if checkpoint_status != "save_failed":
            return status, reason
        status = "partial" if self.turn_ctx.state.reply.strip() else "failed"
        reason = "checkpoint_save_failed"
        self.turn_ctx.state.mark_transition("checkpoint_save_failed")
        self._apply_status(terminal_event, status, reason)
        return status, reason

    def _commit_runtime(self, *, status: str, reason: str) -> AgentEvent | None:
        turn_kernel = self.turn_ctx.turn_kernel
        if turn_kernel is None:
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
        self.turn_ctx.state.terminal_status = run_status
        event = turn_kernel.complete_run_record(
            run_status,
            summary=reason,
            terminal_reason=reason,
            error=reason if run_status == "failed" else "",
        )
        return event or turn_kernel.completion_event

    def _finalize_completed_checkpoint(self, status: str) -> None:
        turn_kernel = self.turn_ctx.turn_kernel
        if (
            status != "completed"
            or turn_kernel is None
            or bool(self.turn_ctx.metadata.get("retain_completed_checkpoint"))
        ):
            return
        turn_kernel.finalize_checkpoint(
            session_id=self.turn_ctx.session_id,
            user_message=self.turn_ctx.user_message,
            state=self.turn_ctx.state,
            context_builder=self.turn_ctx.context_builder,
        )

    def _apply_status(
        self,
        terminal_event: AgentEvent,
        status: str,
        reason: str,
    ) -> None:
        self.turn_ctx.state.terminal_status = status
        self.turn_ctx.state.stopped_reason = reason
        terminal_event.data["status"] = status
        terminal_event.data["reason"] = reason


__all__ = ["QueryTerminalResult", "QueryTerminalTransaction"]
