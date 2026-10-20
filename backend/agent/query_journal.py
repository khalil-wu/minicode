"""Execution-journal projection owned by one QueryEngine turn."""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.agent.execution_journal import ExecutionJournal, JournalEvent, conversation_projection_owner_fields
from backend.agent.message import AgentEvent
from backend.async_cleanup import to_thread_cancel_safe

if TYPE_CHECKING:
    from backend.agent.code_execution_store import CodeExecutionStore
    from backend.agent.context import ContextBuilder
    from backend.agent.state import AgentState
    from backend.agent.turn_kernel import TurnKernel


def _live_thread_code_calls(code_store: CodeExecutionStore | None, *, status: str, turn_id: str) -> frozenset[str]:
    if code_store is None:
        return frozenset()
    return frozenset(call_id for call_id, cell in code_store.live_tool_calls().items()
        if status == "completed" or cell.parent.turn_id != turn_id)


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
    last_completed_answer_source: str = "retained"
    last_completed_answer_item_id: str = ""
    agent_message_receipts: dict[str, tuple[int, float]] = field(
        default_factory=dict
    )
    projection_base_snapshot: dict[str, Any] = field(default_factory=dict)

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
        self.projection_base_snapshot = self.context_builder.export_snapshot()
        self.lifecycle(
            "turn_started",
            {
                "conversation_id": self.conversation_id,
                "run_id": str(self.metadata.get("run_id") or ""),
                "turn_id": str(self.metadata.get("turn_id") or self.metadata.get("run_id") or ""),
                "context_snapshot": self.projection_base_snapshot,
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
        from backend.conversations.context_delta import context_snapshot_delta
        snapshot = self.context_builder.export_snapshot()
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
                "context_snapshot": snapshot,
                "context_delta": context_snapshot_delta(self.projection_base_snapshot, snapshot),
                **conversation_projection_owner_fields(self.metadata, snapshot),
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
        if event.type == "agent.lifecycle.cleanup":
            self.lifecycle("lifecycle_cleanup", data)
            return
        if event.type == "turn.diff.updated":
            self.lifecycle("turn_diff_updated", data)
            return
        if event.type == "artifact.preview":
            self.lifecycle("artifact_preview", data)
            return
        if event.type == 'runtime.span' and data.get('event') == 'tool.started':
            self.journal.append_tool_execution_started(data)
        item = data.get("item", {})
        async_user_message = event.type in {"item.started", "item.completed"} and item.get("type") == "agent_message" and item.get("delivery") == "async"
        if (data.get("call_source") or {}).get("kind") in {"code_mode", "extension"} and event.type not in {"tool_call", "tool_result", "approval_request", "ask_user", "request_user_input"} and not async_user_message:
            self.lifecycle("nested_tool_event", {"event_type": event.type, **data})
            return
        if event.type == "item.started":
            item = data.get("item") if isinstance(data.get("item"), dict) else {}
            if item.get("type") == "agent_message":
                item_id = str(item.get("id") or "agent-message").strip()
                self.agent_messages[item_id] = {
                    "content": "",
                    "source": str(item.get("source") or "pending"),
                }
                self.agent_message_receipts.pop(item_id, None)
            elif item.get("type") == "context_compaction":
                self.lifecycle("context_compaction_item", {"event_type": event.type, "item": dict(item), "transcript_only": True})
            return
        if event.type == "agent_message.delta":
            self._record_agent_message_delta(data)
            return
        if event.type == "item.completed":
            self._record_completed_item(data)
            return
        if event.type == "tool_call":
            if data.get('streaming_announcement') is True:
                self.lifecycle('tool_announcement', {**data, 'transcript_only': True})
                return
            if str(data.get('status') or 'running') == 'pending' and data.get('command_presentation') is None:
                return
            call_id = str(data.get("id") or data.get("tool_call_id") or "").strip()
            tool_name = str(data.get("name") or data.get("tool_name") or "").strip()
            if call_id and tool_name:
                self.tool_names[call_id] = tool_name
            self.journal.append_tool_use(data)
            return
        if event.type == "tool_result":
            call_id = str(data.get("id") or data.get("tool_call_id") or "").strip()
            message = next((item for item in reversed(self.context_builder._history)
                if item.role == "tool" and item.tool_call_id == call_id), None)
            if message is not None:
                for field in ("mcp_output", "code_mode_output", "tool_search_output", "history_notes_output", "native_tool_output"):
                    body = getattr(message, field)
                    if body is not None:
                        data[field] = deepcopy(body)
                        data["model_content"] = message.content
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
        if event.type in {"approval_request", "ask_user", "request_user_input"}:
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
                    **{key: data[key] for key in ("started_at", "completed_at", "duration_ms")
                       if data.get(key) is not None},
                },
            )
            self.runtime_terminal_receipt_recorded = True

    async def record_event_async(self, event: AgentEvent) -> None:
        """Keep memory-only stream reduction on the owning event loop.

        Tool claims, results and terminal evidence still await their durable
        write. Text deltas only cross to a worker when the next progress
        receipt is due; ignored transport/thinking frames require no worker.
        """
        if self.journal is None:
            return
        data = event.data
        nested = (data.get("call_source") or {}).get("kind") in {"code_mode", "extension"}
        if event.type == "runtime.span" and data.get("event") == "tool.started":
            await to_thread_cancel_safe(self.record_event, event)
        elif nested and event.type not in {"tool_call", "tool_result", "approval_request", "ask_user", "request_user_input"}:
            await to_thread_cancel_safe(self.record_event, event)
        elif event.type == "item.started":
            if event.data["item"]["type"] == "context_compaction":
                await to_thread_cancel_safe(self.record_event, event)
            else:
                self.record_event(event)
        elif event.type == "agent_message.delta":
            receipt = self._agent_message_delta_receipt(data)
            if receipt is not None:
                payload, now = receipt
                await to_thread_cancel_safe(self.journal.append, "progress", payload)
                self.agent_message_receipts[payload["item_id"]] = (
                    payload["content_offset"] + len(payload["content_delta"]), now,
                )
        elif event.type in {
            "agent.lifecycle.cleanup", "turn.diff.updated", "artifact.preview",
            "item.completed", "tool_call", "tool_result", "context_compacted",
            "approval_request", "ask_user", "request_user_input", "error", "agent.terminal.intent",
            "agent.run.completed",
        }:
            await to_thread_cancel_safe(self.record_event, event)

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
        from backend.conversations.context_delta import context_snapshot_delta
        projection_owner = conversation_projection_owner_fields(self.metadata, terminal_context_snapshot)
        completed_item = self.agent_messages.get(self.last_completed_answer_item_id)
        item_projection = (
            {"item_id": self.last_completed_answer_item_id}
            if completed_item is not None and completed_item["content"] == assistant_content
            else {"message_only": True} if completed_item is not None else {}
        )
        self.journal.append(
            "assistant",
            {
                "content": assistant_content,
                "answer_source": self.last_completed_answer_source,
                **item_projection,
                "status": status,
                "reason": reason,
                "conversation_id": self.conversation_id,
                "message_id": str(self.metadata.get("assistant_message_id") or ""),
                "context_snapshot": terminal_context_snapshot,
                "context_delta": context_snapshot_delta(self.projection_base_snapshot, terminal_context_snapshot),
                **projection_owner,
            },
        )
        open_uses = {item["tool_call_id"]: item for item in self.journal.unresolved_tool_uses()}
        claims = {event.event_id: event.payload.get("tool_call", {})
                  for event in self.journal.read_events() if event.event_type == "tool_use"}
        for record in self.state.committed_tool_results(str(self.metadata.get("run_id") or "")):
            use = open_uses.get(record.tool_call_id)
            claim = claims.get(use["event_id"], {}) if use is not None else {}
            if (use is not None and use["tool_name"] == record.tool_name
                    and claim.get("turn_id") == record.turn_id
                    and claim.get("iteration_id") == record.iteration_id
                    and use["request_digest"] == record.request_digest):
                self.journal.append_tool_result(record.result_payload, tool_name=record.tool_name)
        code_store = self.turn_kernel.run_context.code_store if self.turn_kernel is not None else None
        turn_id = str(self.metadata.get("turn_id") or self.metadata.get("run_id") or "")
        retained_calls = _live_thread_code_calls(code_store, status=status, turn_id=turn_id)
        # The thread still owns these admitted calls. A completed turn cannot
        # invent a failed result that would seal out their real late result.
        # A later interrupted turn also cannot terminate an older live cell.
        uncertain = [use for use in self.journal.unresolved_tool_uses()
            if use["tool_call_id"] not in retained_calls]
        self.journal.close_unresolved_tool_uses(
            reason=reason or status,
            content="[Tool result missing because the turn reached terminal state]",
            preserve_call_ids=retained_calls,
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
                "uncertain_tool_uses": uncertain,
                "tool_pairs_complete": not unresolved,
                "manual_recovery_required": any(
                    item.get("recovery_policy") == "manual" for item in uncertain
                )
                or any(
                    bool(receipt.get("manual_recovery_required"))
                    for receipt in cleanup_receipts.values()
                ),
                "cleanup_receipts": cleanup_receipts,
                "cleanup_pending_count": cleanup_pending_count,
                **{
                    key: event.data[key]
                    for key in ("lifecycle_cleanup_receipts", "lifecycle_cleanup_pending_count")
                    if key in event.data
                },
                "checkpoint": self._checkpoint_evidence(),
                **projection_owner,
            },
        )
        self.terminal_recorded = True

    def _record_agent_message_delta(self, data: dict[str, Any]) -> None:
        receipt = self._agent_message_delta_receipt(data)
        if receipt is None or self.journal is None:
            return
        payload, now = receipt
        self.journal.append("progress", payload)
        self.agent_message_receipts[payload["item_id"]] = (
            payload["content_offset"] + len(payload["content_delta"]), now,
        )

    def _agent_message_delta_receipt(self, data: dict[str, Any]) -> tuple[dict[str, Any], float] | None:
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
        previous_length, previous_at = self.agent_message_receipts.get(
            item_id,
            (0, 0.0),
        )
        now = time.monotonic()
        if (
            not previous_length
            or now - previous_at >= 0.12
        ):
            return ({
                    "kind": "assistant_message",
                    "item_id": item_id,
                    "content_delta": message["content"][previous_length:],
                    "content_offset": previous_length,
                    "source": message["source"],
                    "status": "running",
                    "transcript_only": True,
                }, now)
        return None

    def _record_completed_item(self, data: dict[str, Any]) -> None:
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if item.get("type") == "context_compaction":
            self.lifecycle("context_compaction_item", {"event_type": "item.completed", "item": dict(item), "transcript_only": True})
            return
        if item.get("type") != "agent_message":
            return
        item_id = str(item.get("id") or "agent-message").strip()
        content = str(item.get("text") or "")
        source = str(item.get("source") or "model_final")
        if item.get("delivery") != "async":
            self.last_completed_answer_source = source
            self.last_completed_answer_item_id = item_id
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
                **{field: item[field] for field in ("delivery", "questions", "phase") if field in item},
                "status": str(item.get("status") or "completed"),
                "transcript_only": True,
            },
        )
        self.agent_message_receipts[item_id] = (len(content), time.monotonic())

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
    code_store: CodeExecutionStore | None,
    turn_id: str,
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
                **{key: completion_event.data[key] for key in ("started_at", "completed_at", "duration_ms")
                   if completion_event.data.get(key) is not None},
            },
        )
    retained_calls = _live_thread_code_calls(code_store, status="failed", turn_id=turn_id)
    uncertain = [use for use in journal.unresolved_tool_uses() if use["tool_call_id"] not in retained_calls]
    journal.close_unresolved_tool_uses(
        reason=str(terminal_event.data.get("reason") or "startup_failed"),
        content="[Tool result missing because startup failed]",
        preserve_call_ids=retained_calls,
    )
    journal.append_terminal(
        status="failed",
        reason=str(terminal_event.data.get("reason") or "startup_failed"),
        extra={
            "unresolved_tool_uses": journal.unresolved_tool_uses(),
            "uncertain_tool_uses": uncertain,
            "manual_recovery_required": any(item["recovery_policy"] == "manual" for item in uncertain),
        },
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
