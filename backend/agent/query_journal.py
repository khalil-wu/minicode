"""Execution-journal projection owned by one QueryEngine turn."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.agent.execution_journal import ExecutionJournal, JournalEvent
from backend.agent.message import AgentEvent

if TYPE_CHECKING:
    from backend.agent.context import ContextBuilder
    from backend.agent.state import AgentState
    from backend.agent.turn_kernel import TurnKernel


@dataclass(slots=True)
class QueryJournalRecorder:
    """Project one query's ordered runtime facts into its durable journal."""

    journal: ExecutionJournal | None
    metadata: dict[str, Any]
    state: AgentState
    context_builder: ContextBuilder
    turn_kernel: TurnKernel | None
    conversation_id: str
    terminal_recorded: bool = False
    runtime_terminal_receipt_recorded: bool = False
    terminal_intent_event: JournalEvent | None = None
    terminal_intent_key: tuple[str, str] | None = None
    tool_names: dict[str, str] = field(default_factory=dict)
    agent_messages: dict[str, dict[str, str]] = field(default_factory=dict)
    agent_message_receipts: dict[str, tuple[str, float]] = field(
        default_factory=dict
    )

    @property
    def terminal_intent_event_id(self) -> str:
        return str(
            self.terminal_intent_event.event_id
            if self.terminal_intent_event is not None
            else ""
        )

    def lifecycle(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
    ) -> JournalEvent | None:
        if self.journal is None:
            return None
        return self.journal.append_lifecycle(name, payload)

    def record_turn_started(self) -> None:
        self.lifecycle(
            "turn_started",
            {
                "conversation_id": self.conversation_id,
                "context_snapshot": self.context_builder.export_snapshot(),
            },
        )
        self.lifecycle(
            "provider_claimed",
            {"conversation_id": self.conversation_id},
        )

    def record_terminal_intent(self, event: AgentEvent) -> None:
        """Persist recovery input without claiming a committed runtime terminal."""

        if self.journal is None:
            return
        status = str(event.data.get("status") or "completed")
        reason = str(event.data.get("reason") or "")
        next_key = (status, reason)
        if self.terminal_intent_key == next_key:
            return
        message_id = str(self.metadata.get("assistant_message_id") or "")
        self.terminal_intent_event = self.lifecycle(
            "terminal_intent",
            {
                "run_id": str(self.metadata.get("run_id") or ""),
                "conversation_id": self.conversation_id,
                "message_id": message_id,
                "status": status,
                "reason": reason,
                "assistant_message": {
                    "id": message_id,
                    "role": "assistant",
                    "content": str(self.state.reply or ""),
                    "terminal_status": status,
                    "termination_reason": reason,
                },
                "context_snapshot": self.context_builder.export_snapshot(),
                "checkpoint": self._checkpoint_evidence(),
                "supersedes_terminal_intent_event_id": (
                    self.terminal_intent_event_id
                ),
            },
        )
        self.terminal_intent_key = next_key

    def record_event(self, event: AgentEvent) -> None:
        if self.journal is None:
            return
        data = dict(event.data or {})
        if event.type == "item.started":
            item = data.get("item") if isinstance(data.get("item"), dict) else {}
            if item.get("type") == "agent_message":
                item_id = str(item.get("id") or "agent-message").strip()
                self.agent_messages[item_id] = {
                    "content": "",
                    "source": str(item.get("source") or "pending"),
                }
            return
        if event.type == "agent_message.delta":
            self._record_agent_message_delta(data)
            return
        if event.type == "item.completed":
            self._record_completed_item(data)
            return
        if event.type == "tool_call" and str(
            data.get("status") or "running"
        ) != "pending":
            call_id = str(data.get("id") or data.get("tool_call_id") or "").strip()
            tool_name = str(data.get("name") or data.get("tool_name") or "").strip()
            if call_id and tool_name:
                self.tool_names[call_id] = tool_name
            self.journal.append_tool_use(data)
            return
        if event.type == "tool_result":
            call_id = str(data.get("id") or data.get("tool_call_id") or "").strip()
            tool_name = self.tool_names.get(call_id) or str(
                data.get("name") or data.get("tool_name") or ""
            ).strip()
            if not tool_name:
                unresolved = {
                    str(item.get("tool_call_id") or ""): str(
                        item.get("tool_name") or ""
                    )
                    for item in self.journal.unresolved_tool_uses()
                }
                tool_name = unresolved.get(call_id, "")
            self.journal.append_tool_result(data, tool_name=tool_name)
            return
        if event.type == "context_compacted":
            self.lifecycle(
                "compaction_committed",
                {
                    **data,
                    "context_snapshot": self.context_builder.export_snapshot(),
                },
            )
            return
        if event.type in {"approval_request", "ask_user"}:
            call_id = str(data.get("tool_call_id") or data.get("id") or "").strip()
            tool_name = str(data.get("tool_name") or data.get("name") or "").strip()
            if call_id and tool_name:
                self.tool_names[call_id] = tool_name
            self.lifecycle("approval_waiting", data)
            return
        if event.type == "error":
            self.lifecycle(
                "error",
                {
                    "message": str(data.get("message") or ""),
                    "error_type": str(data.get("error_type") or ""),
                    "error_code": str(data.get("error_code") or ""),
                    "recoverable": bool(data.get("recoverable", False)),
                },
            )
            if data.get("terminal_commit_failed") is True:
                self.lifecycle(
                    "runtime_terminal_commit_failed",
                    {
                        "run_id": str(data.get("run_id") or ""),
                        "failure_kind": str(data.get("failure_kind") or ""),
                    },
                )
            return
        if event.type == "agent.terminal.intent":
            self.record_terminal_intent(
                AgentEvent.done(
                    status=str(data.get("status") or "completed"),
                    reason=str(data.get("reason") or ""),
                )
            )
            return
        if event.type == "agent.run.completed" and not self.runtime_terminal_receipt_recorded:
            self.lifecycle(
                "runtime_terminal_committed",
                {
                    "run_id": str(data.get("run_id") or ""),
                    "status": str(data.get("status") or "completed"),
                    "terminal_intent_event_id": self.terminal_intent_event_id,
                },
            )
            self.runtime_terminal_receipt_recorded = True

    def record_terminal(self, event: AgentEvent) -> None:
        if self.turn_kernel is not None:
            event.data.setdefault("checkpoint", self._checkpoint_evidence())
        if (
            self.journal is None
            or self.terminal_recorded
            or not self.runtime_terminal_receipt_recorded
        ):
            return
        status = str(event.data.get("status") or "completed")
        reason = str(event.data.get("reason") or "")
        self.lifecycle(
            "provider_completed",
            {"status": status, "reason": reason},
        )
        assistant_content = str(self.state.reply or "")
        if not assistant_content:
            assistant_content = str(
                self.state.prompt_context.get("last_completed_assistant_text", "")
            )
        terminal_context_snapshot = self.context_builder.export_snapshot()
        self.journal.append(
            "assistant",
            {
                "content": assistant_content,
                "status": status,
                "reason": reason,
                "conversation_id": self.conversation_id,
                "message_id": str(self.metadata.get("assistant_message_id") or ""),
                "context_snapshot": terminal_context_snapshot,
            },
        )
        self.journal.close_unresolved_tool_uses(
            reason=reason or status,
            content="[Tool result missing because the turn reached terminal state]",
        )
        unresolved = self.journal.unresolved_tool_uses()
        tool_execution_context = self.metadata.get("_tool_execution_context")
        raw_cleanup_receipts = (
            tool_execution_context.cleanup_receipts
            if tool_execution_context is not None
            else {}
        )
        cleanup_receipts = {
            str(tool_call_id): dict(receipt)
            for tool_call_id, receipt in raw_cleanup_receipts.items()
            if isinstance(receipt, dict)
        }
        cleanup_pending_count = sum(
            int(receipt.get("pending") or 0)
            for receipt in cleanup_receipts.values()
        )
        self.journal.append_terminal(
            status=status,
            summary=assistant_content,
            reason=reason,
            extra={
                "runtime_terminal_commit": "committed",
                "run_id": str(self.metadata.get("run_id") or ""),
                "terminal_intent_event_id": self.terminal_intent_event_id,
                "conversation_id": self.conversation_id,
                "message_id": str(self.metadata.get("assistant_message_id") or ""),
                "unresolved_tool_uses": unresolved,
                "manual_recovery_required": any(
                    item.get("recovery_policy") == "manual" for item in unresolved
                )
                or any(
                    bool(receipt.get("manual_recovery_required"))
                    for receipt in cleanup_receipts.values()
                ),
                "cleanup_receipts": cleanup_receipts,
                "cleanup_pending_count": cleanup_pending_count,
                "checkpoint": self._checkpoint_evidence(),
            },
        )
        self.terminal_recorded = True

    def _record_agent_message_delta(self, data: dict[str, Any]) -> None:
        item_id = str(data.get("item_id") or "agent-message").strip()
        delta = str(data.get("delta") or "")
        if not delta:
            return
        message = self.agent_messages.setdefault(
            item_id,
            {"content": "", "source": "pending"},
        )
        message["content"] += delta
        if data.get("source"):
            message["source"] = str(data["source"])
        previous_content, previous_at = self.agent_message_receipts.get(
            item_id,
            ("", 0.0),
        )
        now = time.monotonic()
        if (
            not previous_content
            or len(message["content"]) - len(previous_content) >= 128
            or now - previous_at >= 0.12
        ):
            if self.journal is None:
                return
            self.journal.append(
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
            self.agent_message_receipts[item_id] = (message["content"], now)

    def _record_completed_item(self, data: dict[str, Any]) -> None:
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if item.get("type") != "agent_message":
            return
        item_id = str(item.get("id") or "agent-message").strip()
        content = str(item.get("text") or "")
        source = str(item.get("source") or "model_final")
        self.agent_messages[item_id] = {"content": content, "source": source}
        if self.journal is None:
            return
        self.journal.append(
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
        self.agent_message_receipts[item_id] = (content, time.monotonic())

    def _checkpoint_evidence(self) -> dict[str, Any]:
        if self.turn_kernel is None:
            return {"status": "none"}
        return self.turn_kernel.checkpoint_evidence()


def record_setup_failure(
    journal: ExecutionJournal | None,
    *,
    conversation_id: str,
    completion_event: AgentEvent | None,
    terminal_event: AgentEvent,
) -> None:
    """Record setup failure evidence without inventing a committed terminal."""

    if journal is None:
        return
    journal.append_lifecycle(
        "startup_failed",
        {"conversation_id": conversation_id},
    )
    if terminal_commit_failed(completion_event):
        journal.append_lifecycle(
            "runtime_terminal_commit_failed",
            {
                "run_id": str(completion_event.data.get("run_id") or ""),
                "failure_kind": str(
                    completion_event.data.get("failure_kind") or ""
                ),
            },
        )
        return
    if completion_event is not None and completion_event.type == "agent.run.completed":
        journal.append_lifecycle(
            "runtime_terminal_committed",
            {
                "run_id": str(completion_event.data.get("run_id") or ""),
                "status": str(completion_event.data.get("status") or "failed"),
                "terminal_intent_event_id": "",
            },
        )
    journal.close_unresolved_tool_uses(
        reason=str(terminal_event.data.get("reason") or "startup_failed"),
        content="[Tool result missing because startup failed]",
    )
    journal.append_terminal(
        status="failed",
        reason=str(terminal_event.data.get("reason") or "startup_failed"),
        extra={"unresolved_tool_uses": journal.unresolved_tool_uses()},
    )


def terminal_commit_failed(event: AgentEvent | None) -> bool:
    return bool(
        event is not None
        and event.type == "error"
        and event.data.get("terminal_commit_failed") is True
    )


def terminal_journal_failure_event(error: BaseException) -> AgentEvent:
    event = AgentEvent.error(
        "MiniCode could not record this run's terminal evidence in the "
        f"execution journal: {error}",
        recoverable=False,
        error_type="journal",
        error_code="runtime.terminal_journal_failed",
    )
    event.data["terminal_journal_failed"] = True
    return event


__all__ = [
    "QueryJournalRecorder",
    "record_setup_failure",
    "terminal_commit_failed",
    "terminal_journal_failure_event",
]
