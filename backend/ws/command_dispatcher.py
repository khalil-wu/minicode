from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe
from backend.atomic_io import await_task_despite_cancellation

import asyncio
from collections import deque
import hashlib
import json
import logging
import time
import uuid
from contextlib import AsyncExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.agent.message import AgentEvent, UserCommand
from backend.agent.execution_journal import execution_journal_owner, turn_user_prompt_id, turn_input_consumption_id, turn_input_rejection_id
from backend.services.misc_command_service import is_conversation_effort_command
from backend.ws.client_command_log import ClientCommandDedupStore, RECENT_CLIENT_COMMAND_IDS_MAX, _clean_command_id
from backend.ws.command_results import emit_command_error
from backend.ws.command_scope import CommandScope, bind_execution_command_scope
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.event_outbox import DurableResultDelivery, EventOutbox, capture_durable_results
from backend.ws.conversation_stream import bind_response_origin
from backend.ws.lifecycle_scope import bind_command_lifecycle_prefix
from backend.ws.utils import normalize_attachment_payloads, normalize_permission_mode, normalize_user_input_metadata
from backend.ws.turn_settings import PreparedTurnSettings
from backend.agent.conversation_query_guard import ConversationTransition, conversation_query_guards

logger = logging.getLogger(__name__)

MAX_PENDING_COMMAND_TASKS = 100
COMMAND_BACKLOG_ERROR_INTERVAL_SECONDS = 2.0


def _admitted_input_origin(admission: dict[str, Any], projection: dict[str, Any] | None, runtime: Any) -> str:
    origin = str(admission.get("client_queue_key") or "")
    if origin:
        return origin
    # A legacy steer is consumed by another window's run. Its parent run's
    # session is not evidence of who submitted that input.
    if projection is None or projection.get("steered"):
        return ""
    run = runtime.get_run(str(admission.get("run_id") or ""))
    if run is None or not run.session_id:
        return ""
    from backend.ws.durable_user_queue import DurableUserMessageQueue
    return DurableUserMessageQueue.queue_key_for_session(run.session_id)


def _same_admitted_input(
    projection: dict[str, Any], submitted_content: str,
    attachments: list[dict[str, Any]], input_metadata: dict[str, Any],
) -> bool:
    return (
        str(projection.get("submitted_content", projection.get("content", ""))) == submitted_content
        and list(projection.get("attachments") or []) == attachments
        and all(projection.get(key) == value for key, value in input_metadata.items())
    )


@dataclass
class _UserMessageAdmission:
    command: UserCommand
    original_command: UserCommand | None = None
    interrupted: bool = False
    cancel_persistence: asyncio.Task[None] | None = None
    stop_target: dict[str, Any] | None = None
    stop_acceptance: asyncio.Future[None] | None = None

COMMAND_BACKLOG_BYPASS_TYPES = {
    "control_response",
    "control_cancel_request",
    "interrupt",
    "mcp.inventory.cancel",
    "thread/realtime/appendAudio", "thread/realtime/stop",
}

_FORWARDED_CONVERSATION_COMMAND_TYPES = frozenset({
    "interrupt", "control_response", "control_cancel_request", "approval.file_diff",
    "user_message.queue.cancel", "user_message.queue.steer", "user_message.queue.start", "user_message.queue.update", "user_message.queue.add", "user_message.queue.reorder", "send_message", "followup_task",
    "thread/approveGuardianDeniedAction", "context.compact",
    "subagent.cancel", "subagent.plan_review",
    "thread/realtime/start", "thread/realtime/appendAudio", "thread/realtime/appendText",
    "thread/realtime/appendSpeech", "thread/realtime/stop", "thread/realtime/listVoices",
})


def _normalize_command_owner(command: UserCommand) -> None:
    # These are the existing explicit-owner spellings. Normalize once before
    # lifecycle locking and routing so a later reader cannot reinterpret them.
    owner = command.data.get("owner_conversation_id") or command.data.get("ownerConversationId")
    # Scheduler conversation_id is the heartbeat target; its caller remains
    # in the explicit resource-owner field throughout admission and replay.
    if owner and not command.type.startswith("scheduler."):
        command.data["conversation_id"] = str(owner).strip()
    if command.type.startswith("thread/realtime/") and "conversation_id" not in command.data:
        command.data["conversation_id"] = command.data.get("threadId", "")

_CONVERSATION_LIFECYCLE_COMMAND_TYPES = {
    "llm.model.set",
    "llm.config.set",
    "memory.reset",
    "session.restore",
    "session.sync",
    "workspace.import",
    "workspace.set",
    "workspace.switch",
    "context.compact",
    "context.fork",
    "thread/realtime/start", "thread/realtime/stop",
    "thread/approveGuardianDeniedAction",
    "user_message.queue.cancel",
    "user_message.queue.steer",
    "user_message.queue.start",
    "user_message.queue.update",
    "user_message.queue.add",
    "user_message.queue.reorder",
    "send_message", "followup_task", "subagent.cancel", "subagent.plan_review",
}


def _requires_selected_conversation(command_type: str) -> bool:
    return command_type.startswith(("subagent.", "terminal.", "preview.", "checkpoint.", "inspector.")) or command_type in {
        "send_message", "followup_task", "agent.resume", "approval.file_diff", "context.compact", "context.fork", "context.side_query",
    }


def _is_conversation_lifecycle_command(command_type: str) -> bool:
    normalized = str(command_type or "").strip()
    return (
        normalized == "user_message"
        or normalized.startswith("conversation.")
        or normalized.startswith("terminal.")
        or normalized.startswith("preview.")
        or normalized.startswith("scheduler.")
        or normalized in _CONVERSATION_LIFECYCLE_COMMAND_TYPES
    )


_CONVERSATION_DELETE_FENCE_READ_COMMANDS = {
    "conversation.export",
    "conversation.permission.rules.list",
    "conversation.worktree.handoff.preflight",
    "context.side_query",
    "context.ledger",
}


def _command_targets_conversation_delete_fence(
    session: Any,
    command: UserCommand,
) -> tuple[str, ...]:
    """Resolve every conversation a lifecycle mutation can address."""

    command_type = str(command.type or "").strip()
    data = command.data if isinstance(command.data, dict) else {}
    if command_type in _CONVERSATION_DELETE_FENCE_READ_COMMANDS:
        return ()
    if command_type == "conversation.delete":
        return ()
    if command_type == "memory.reset":
        manager = session.ws_manager
        if manager is not None:
            return tuple(
                str(item or "").strip()
                for item in manager.conversation_delete_fenced_ids()
                if str(item or "").strip()
            )
        active = str(session.active_conversation_id or "").strip()
        return (active,) if active else ()
    if command_type == "conversation.create":
        if not bool(data.get("activate")):
            return ()
        active = str(session.active_conversation_id or "").strip()
        return (active,) if active else ()
    if not _is_conversation_lifecycle_command(command_type):
        return ()

    targets: set[str] = set()
    for key in (
        "conversation_id",
        "owner_conversation_id", "ownerConversationId",
        "preferred_conversation_id",
        "source_conversation_id",
        "target_conversation_id",
        "parent_conversation_id",
    ):
        value = str(data.get(key) or "").strip()
        if value:
            targets.add(value)
    if not targets:
        active = str(session.active_conversation_id or "").strip()
        if active:
            targets.add(active)
    return tuple(sorted(targets))


def _command_lifecycle_conversations(session: Any, command: UserCommand) -> tuple[str, ...]:
    """Lock every addressed task, independently of delete-fence exemptions."""
    targets = {
        str(command.data.get(key) or "").strip()
        for key in (
            "conversation_id", "preferred_conversation_id", "source_conversation_id",
            "owner_conversation_id", "ownerConversationId",
            "target_conversation_id", "parent_conversation_id",
        )
    }
    if command.type == "session.restore":
        targets.add(str(command.data.get("last_conversation_id") or "").strip())
    targets.discard("")
    active = str(session.active_conversation_id or "").strip()
    if active and (not targets or command.type in {"conversation.activate", "conversation.list", "session.restore", "session.sync"}):
        targets.add(active)
    return tuple(sorted(targets))


COMMAND_BACKLOG_DROPPABLE_TYPES = {
    "commands.list",
    "conversation.list",
    "diff.git_staged",
    "diff.git_working_tree",
    "env.list",
    "mcp.list",
    "preview.detect",
    "runtime.capabilities.inspect",
    "scheduler.list",
    "session.usage.inspect",
    "skills.list",
    "skills.marketplace.list",
}


class SessionCommandDispatcher:
    """Owns client-command admission, deduplication, and command execution."""

    def __init__(
        self,
        session: Any,
        *,
        root_dir: Path,
    ) -> None:
        self._session = session
        self._client_command_store = ClientCommandDedupStore(
            session_id=session.session_id,
            root_dir=root_dir,
        )
        self._command_semaphore = asyncio.Semaphore(20)
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._execution_command_tasks: set[asyncio.Task[Any]] = set()
        self._user_message_admissions: dict[asyncio.Task[Any], _UserMessageAdmission] = {}
        self._regeneration_transitions: dict[asyncio.Task[Any] | None, ConversationTransition] = {}
        self._active_client_command_ids: set[str] = set()
        self._interrupt_groups: dict[tuple[str, str, str, str], deque[tuple[UserCommand, str, int]]] = {}
        self._pending_client_completions: dict[str, tuple[UserCommand, list[DurableResultDelivery]]] = {}
        self._result_replay_generation: int | None = None
        self._result_replay_lock = asyncio.Lock()
        self._result_replay_task: asyncio.Task[Any] | None = None
        self._reported_durable_queue_load_errors: dict[str, tuple[str, ...]] = {}
        self._max_command_tasks = MAX_PENDING_COMMAND_TASKS
        self._last_command_backlog_error_at = 0.0
        self._recent_client_command_ids = self._load_recent_client_command_ids()
        self._recent_client_command_id_set = set(self._recent_client_command_ids)

    @property
    def client_command_store(self) -> ClientCommandDedupStore:
        return self._client_command_store

    @property
    def command_semaphore(self) -> asyncio.Semaphore:
        return self._command_semaphore

    @command_semaphore.setter
    def command_semaphore(self, value: asyncio.Semaphore) -> None:
        self._command_semaphore = value

    @property
    def command_tasks(self) -> set[asyncio.Task[Any]]:
        return self._command_tasks

    @property
    def active_client_command_ids(self) -> set[str]:
        return self._active_client_command_ids

    @property
    def max_command_tasks(self) -> int:
        return self._max_command_tasks

    @max_command_tasks.setter
    def max_command_tasks(self, value: int) -> None:
        self._max_command_tasks = value

    @property
    def last_command_backlog_error_at(self) -> float:
        return self._last_command_backlog_error_at

    @last_command_backlog_error_at.setter
    def last_command_backlog_error_at(self, value: float) -> None:
        self._last_command_backlog_error_at = value

    @property
    def recent_client_command_ids(self) -> list[str]:
        return self._recent_client_command_ids

    @property
    def recent_client_command_id_set(self) -> set[str]:
        return self._recent_client_command_id_set

    async def _replay_pending_client_commands(self, connection_generation: int, *, replay_results: bool = True) -> None:
        await self._report_durable_queue_load_error(self._client_command_store.last_load_error,
            source="client_log", connection_generation=connection_generation)
        durable_queue = self._session.run_manager.durable_client_commands
        if durable_queue is None:
            return
        await self._report_durable_queue_load_error(durable_queue.load_error,
            source="client_queue", connection_generation=connection_generation)
        await self._report_durable_queue_load_error(self._session.run_manager.durable_queue.load_error,
            source="user_queue", connection_generation=connection_generation)
        for command_id, (command, _deliveries) in tuple(self._pending_client_completions.items()):
            self._schedule_durable_client_command(command_id, connection_generation, command_type=command.type, command=command)
        for command in durable_queue.pending_client_commands():
            command_id = self._client_command_id(command)
            if not command_id:
                continue
            if self._client_command_seen(command):
                # Reconcile a stale pending entry beside a completion record.
                try:
                    reconciled = durable_queue.reconcile_archived_client_result(command_id)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    await self._report_durable_queue_load_error(
                        {"reason": "command_result_unreadable", "path": str(durable_queue._result_receipt_path(command_id)), "detail": str(exc)},
                        source="client_result:" + str(durable_queue._result_receipt_path(command_id)),
                        connection_generation=connection_generation,
                    )
                    continue
                if not reconciled:
                    durable_queue.discard_pending_client_command(command_id)
                await self._send_client_command_ack(command, duplicate=True)
                continue
            if not self._recovered_command_owner_ready(command):
                # The durable ACK survives a cold session, but its selected
                # conversation does not. Keep owner-dependent work pending
                # until restore/switch has committed that owner.
                continue
            await self._send_client_command_ack(command, duplicate=True)
            self._schedule_durable_client_command(command_id, connection_generation, command_type=command.type, command=command, recovered=True)
        if not replay_results:
            return
        if self._session.connection_generation != connection_generation:
            return
        self._result_replay_generation = connection_generation
        await self._replay_pending_client_results(connection_generation)

    async def _replay_pending_client_results(self, connection_generation: int) -> None:
        durable_queue = self._session.run_manager.durable_client_commands
        async with self._result_replay_lock:
            while self._session.connection_generation == connection_generation:
                records, errors = durable_queue.pending_client_results()
                damaged_sources = {"client_result:" + str(error["path"]) for error in errors}
                for source in list(self._reported_durable_queue_load_errors):
                    if source.startswith("client_result:") and source not in damaged_sources:
                        self._reported_durable_queue_load_errors.pop(source)
                for error in errors:
                    await self._report_durable_queue_load_error(error, source="client_result:" + str(error["path"]),
                                                                connection_generation=connection_generation)
                if not records:
                    return
                for record in records:
                    for payload in record["events"]:
                        sent = await self._session.event_outbox.send_payload(payload, connection_generation=connection_generation,
                            log_context="durable.command.result", envelope=False, wait_for_delivery=True)
                        if not sent or self._session.connection_generation != connection_generation:
                            return
                        durable_queue.acknowledge_client_result(record["client_command_id"], payload["event_id"])

    def _schedule_late_client_results(self, deliveries: list[DurableResultDelivery]) -> None:
        generation = self._result_replay_generation
        outbox = self._session.event_outbox
        if (generation is None or not outbox.connected or self._session.connection_generation != generation
                or not any(item.outbox is not outbox or item.generation != generation for item in deliveries)):
            return
        if self._result_replay_task is None or self._result_replay_task.done():
            self._result_replay_task = asyncio.create_task(self._replay_pending_client_results(generation))
            self.track_command_task(self._result_replay_task)

    def _recovered_command_owner_ready(self, command: UserCommand) -> bool:
        # These commands establish a conversation or address a target rather
        # than requiring it to be the current command scope.
        if command.type == "user_message":
            owner = str(command.data.get("conversation_id") or self._session.active_conversation_id or "").strip()
            return not owner or self._session.session_lifecycle.recovered_input_workspace_ready(owner)
        if command.type in {"session.restore", "session.sync", "thread/approveGuardianDeniedAction"} or command.type.startswith("conversation."):
            return True
        if command.type.startswith("scheduler.") and (
            command.data.get("owner_conversation_id") or command.data.get("ownerConversationId")
        ):
            # Resource commands validate their saved owner and cwd in the
            # handler; restoring an unrelated reader cannot retarget them.
            return True
        owner = str(command.data.get("owner_conversation_id")
            or command.data.get("ownerConversationId") or command.data.get("conversation_id") or "").strip()
        active_owner = str(self._session.active_conversation_id or "").strip()
        if owner:
            return owner == active_owner
        return bool(active_owner) if _requires_selected_conversation(command.type) else True

    async def _resume_recovered_workspace(self, conversation_id: str, *, previously_failed: bool) -> None:
        manager = self._session.ws_manager
        sessions = manager.resource_sessions() if manager is not None else (self._session,)
        blocked = [session for session in sessions if session is not self._session
            and session.session_lifecycle.workspace_activation_failed(conversation_id)]
        if blocked:
            store_identity = self._session.conversation_repo.store_instance_id()
            blocked = [session for session in blocked if session.conversation_repo.store_instance_id() == store_identity]
        if not previously_failed and not blocked:
            return
        for session in blocked:
            session.session_lifecycle.record_workspace_activation(conversation_id, True)
            if session.is_connected:
                await session.command_dispatcher._replay_pending_client_commands(
                    session.connection_generation, replay_results=False,
                )
        owner = manager.execution_session_for_conversation(conversation_id) if manager is not None else None
        owner = owner or self._session
        owner.schedule_next_queued_user_message(conversation_id)
        owner.run_manager.recheck_parent_notification_wake(conversation_id)

    async def _report_durable_queue_load_error(self, evidence: dict[str, Any] | None, *, source: str = "",
                                               connection_generation: int | None = None) -> None:
        """Surface preserved queue or completion-record read errors on reconnect.

        The evidence path identifies the failed read without treating a damaged
        completion log as an empty set of completed requests.
        """

        if not isinstance(evidence, dict) or not evidence:
            self._reported_durable_queue_load_errors.pop(source, None)
            return
        signature = tuple(str(evidence.get(key) or "") for key in (
            "reason", "detail", "path", "line", "quarantined_to", "quarantine_error",
        ))
        if signature == self._reported_durable_queue_load_errors.get(source):
            return
        result = AgentEvent.error(
            "Queued commands or completed request records could not be fully restored. "
            "The unreadable data was preserved for recovery instead of overwritten.",
            recoverable=False, error_type="storage", error_code="user_message.queue.restore",
        ).to_ws_message()
        result["error_details"] = {
                "reason": str(evidence.get("reason") or "unreadable"),
                "detail": str(evidence.get("detail") or ""),
                "path": str(evidence.get("path") or ""),
                "line": str(evidence.get("line") or ""),
                "quarantined_to": str(evidence.get("quarantined_to") or ""),
                "quarantine_error": str(evidence.get("quarantine_error") or ""),
        }
        if not await self._session.send_payload(result, connection_generation=connection_generation,
                                                log_context="durable.queue.restore"):
            raise ConnectionError("Could not deliver the durable queue recovery error")
        if connection_generation is not None and self._session.connection_generation != connection_generation:
            raise ConnectionError("The queue recovery notification belonged to an older connection")
        self._reported_durable_queue_load_errors[source] = signature

    async def run(self, connection_generation: int) -> None:
        """Receive and admit commands for one attached websocket generation."""

        while True:
            if not self._session.is_connected or connection_generation != self._session.connection_generation:
                return

            raw = await self._session.ws.receive_text()
            if not self._session.is_connected or connection_generation != self._session.connection_generation:
                return
            try:
                message = json.loads(raw.replace("\x00", ""))
            except json.JSONDecodeError:
                await self._session.send_event(
                    AgentEvent.error("Invalid JSON message", recoverable=True)
                )
                continue

            try:
                command = UserCommand.from_ws_message(message)
            except ValueError as exc:
                await self._session.send_event(
                    AgentEvent.error(str(exc), recoverable=True)
                )
                continue

            if command.type == "scheduler.heartbeat.renderer_state":
                self._session.ws_manager.record_heartbeat_renderer_state(
                    self._session, connection_generation, command.data,
                )
                continue
            if command.type.startswith("thread/realtime/"):
                # Voice control and PCM belong to the current live transport.
                # A cold replay must never restart a microphone or replay audio.
                self._schedule_transient_client_command(command, connection_generation)
                continue

            if command.type == "user_message":
                for field in ("_client_queue_key", "_legacy_user_message_identity", "_queue_entry_id", "_queued_user_message_dispatch", "_submitted_content", "_guardian_acceptance_order",
                              "_permission_mode_selection_revision", "_workspace_binding_selection_revision"):
                    command.data.pop(field, None)
                command.data.pop("_implicit_new_conversation_id", None)
            if command.type == "interrupt" and not (
                command.data.get("owner_conversation_id") or command.data.get("ownerConversationId") or command.data.get("conversation_id")
            ) and self._session.active_conversation_id:
                command.data["conversation_id"] = self._session.active_conversation_id
            command_id = self._client_command_id(command)
            if command.type == "user_message" and command_id:
                command.data["_client_queue_key"] = self._session.run_manager.durable_client_commands.path.stem
                cancellation = await self.cancelled_user_message(command_id)
                if cancellation is not None:
                    await self._send_client_command_ack(command, duplicate=True)
                    await self.emit_cancelled_user_message(command)
                    continue
            if self._client_command_seen(command):
                await self._send_client_command_ack(command, duplicate=True)
                logger.info(
                    "Skipping duplicate client command %s in session %s",
                    command.data.get("client_command_id"),
                    self._session.session_id,
                )
                continue

            if command.type == "ping":
                self._mark_client_command_seen(command)
                await self._send_client_command_ack(command)
                await self._session.send_payload({"type": "pong"}, log_context="pong")
                continue

            self.prune_command_tasks()
            command_backlog_full = len(self._command_tasks) + len(self._execution_command_tasks) >= self._max_command_tasks
            command_can_bypass_backlog = command.type in COMMAND_BACKLOG_BYPASS_TYPES
            command_is_droppable_refresh = command.type in COMMAND_BACKLOG_DROPPABLE_TYPES
            if command_backlog_full and not command_can_bypass_backlog:
                reason = (
                    "command.dropped_refresh"
                    if command_is_droppable_refresh
                    else "command.backlog"
                )
                await self._send_client_command_ack(
                    command,
                    accepted=False,
                    reason=reason,
                )
                if command_is_droppable_refresh:
                    logger.debug(
                        "Dropping refresh command %s during backlog in session %s",
                        command.type,
                        self._session.session_id,
                    )
                    continue
                now = asyncio.get_running_loop().time()
                if now - self._last_command_backlog_error_at >= COMMAND_BACKLOG_ERROR_INTERVAL_SECONDS:
                    self._last_command_backlog_error_at = now
                    await self._session.send_event(
                        AgentEvent.error(
                            "Too many pending commands; please wait for current work to finish.",
                            recoverable=True,
                            error_type="rate_limit",
                            error_code="command.backlog",
                        )
                    )
                continue

            command_id = self._client_command_id(command)
            durable_queue = self._session.run_manager.durable_client_commands
            if command_id and durable_queue is not None:
                if durable_queue.has_client_command(command_id):
                    await self._send_client_command_ack(command, duplicate=True)
                    if self._recovered_command_owner_ready(command):
                        self._schedule_durable_client_command(command_id, connection_generation, command_type=command.type, command=command, recovered=True)
                    continue
                if (_requires_selected_conversation(command.type)
                        and not (command.data.get("owner_conversation_id") or command.data.get("ownerConversationId")
                                 or command.data.get("conversation_id"))
                        and self._session.active_conversation_id
                        and not getattr(self._session.session_lifecycle, "_foreground_navigation_pending", False)):
                    # Persist the selected owner with commands whose wire
                    # shape permits omission. Replay must not reinterpret a
                    # formerly owner-relative action in another conversation.
                    command.data["owner_conversation_id"] = self._session.active_conversation_id
                try:
                    if command.type == "user_message":
                        self._bind_user_message_input_owner(command)
                    persisted = durable_queue.persist_client_command(command)
                except Exception:
                    logger.exception(
                        "Failed to persist client command %s in session %s",
                        command_id,
                        self._session.session_id,
                    )
                    await self._send_client_command_ack(
                        command,
                        accepted=False,
                        reason="command.persistence",
                        envelope=False,
                    )
                    continue
                if not persisted:
                    await self._send_client_command_ack(
                        command,
                        accepted=False,
                        reason="command.not_serializable",
                    )
                    continue
                # ACK means the server durably owns the command. A crash before
                # task creation leaves it pending for replay.
                await self._send_client_command_ack(command)
                self._schedule_durable_client_command(command_id, connection_generation, command_type=command.type, command=command)
                continue

            if command.type == "user_message":
                self._bind_user_message_input_owner(command)
            self._schedule_transient_client_command(command, connection_generation)

    def _bind_user_message_input_owner(self, command: UserCommand) -> None:
        explicit_owner = str(command.data.get("owner_conversation_id") or command.data.get("ownerConversationId")
                             or command.data.get("conversation_id") or "")
        if not explicit_owner and getattr(self._session.session_lifecycle, "_foreground_navigation_pending", False):
            # A selection still in flight decides this input's owner at admission.
            return
        input_owner = explicit_owner or str(self._session.active_conversation_id or "")
        record = self._session.conversation_repo.get_conversation_summary(input_owner) if input_owner else None
        if record is not None:
            command.data["conversation_id"] = input_owner
            command.data["_permission_mode_selection_revision"] = record.permission_mode_revision
            command.data["_workspace_binding_selection_revision"] = record.workspace_binding_revision
        elif not input_owner:
            new_owner = f"conv_{uuid.uuid4().hex[:12]}"
            command.data.update(conversation_id=new_owner, _implicit_new_conversation_id=new_owner,
                _permission_mode_selection_revision=0, _workspace_binding_selection_revision=0)

    def _schedule_transient_client_command(
        self,
        command: UserCommand,
        connection_generation: int,
    ) -> None:
        if self._enqueue_scoped_interrupt(command, "", connection_generation):
            return
        async def _guarded_handle() -> None:
            await self._dispatch_client_command(command, connection_generation)

        task = asyncio.create_task(_guarded_handle())
        if command.type == "user_message":
            self.track_execution_command_task(task)
        else:
            self.track_command_task(task)

    def _schedule_durable_client_command(
        self,
        client_command_id: str,
        connection_generation: int,
        *,
        command_type: str,
        command: UserCommand | None = None,
        recovered: bool = False,
    ) -> None:
        command_id = _clean_command_id(client_command_id)
        if not command_id or command_id in self._active_client_command_ids:
            return
        self._active_client_command_ids.add(command_id)
        if command is not None and self._enqueue_scoped_interrupt(command, command_id, connection_generation):
            return
        task = asyncio.create_task(
            self._run_durable_client_command(command_id, connection_generation, recovered=recovered)
        )

        def _release_active(_task: asyncio.Task[Any]) -> None:
            self._active_client_command_ids.discard(command_id)

        task.add_done_callback(_release_active)
        if command_type == "user_message":
            self.track_execution_command_task(task)
        else:
            self.track_command_task(task)

    def _enqueue_scoped_interrupt(self, command: UserCommand, command_id: str, generation: int) -> bool:
        if command.type != "interrupt":
            return False
        data = command.data
        target = tuple(str(data.get(key) or "").strip() for key in ("turn_id", "message_id", "task_id"))
        if not any(target):
            return False
        owner = str(data.get("owner_conversation_id") or data.get("ownerConversationId")
                    or data.get("conversation_id") or self._session.active_conversation_id or "").strip()
        key = (owner, *target)
        group = self._interrupt_groups.get(key)
        if group is None:
            group = deque()
            self._interrupt_groups[key] = group
            group.append((command, command_id, generation))
            self.track_command_task(asyncio.create_task(self._drain_interrupt_group(key, group)))
        else:
            group.append((command, command_id, generation))
        return True

    async def _drain_interrupt_group(self, key: tuple[str, str, str, str],
                                     group: deque[tuple[UserCommand, str, int]]) -> None:
        try:
            while group:
                command, command_id, generation = group.popleft()
                try:
                    if command_id:
                        await self._run_durable_client_command(command_id, generation)
                    else:
                        await self._dispatch_client_command(command, generation)
                except Exception:
                    # A failed command must not strand other accepted Stops in
                    # the same group. Its durable completion remains available
                    # for the existing reconnect recovery path.
                    logger.exception("Failed to handle grouped interrupt %s", command_id)
                finally:
                    if command_id:
                        self._active_client_command_ids.discard(command_id)
        finally:
            self._interrupt_groups.pop(key, None)
            for _command, command_id, _generation in group:
                if command_id:
                    self._active_client_command_ids.discard(command_id)

    async def _run_durable_client_command(
        self,
        client_command_id: str,
        connection_generation: int,
        *,
        recovered: bool = False,
    ) -> None:
        durable_queue = self._session.run_manager.durable_client_commands
        if durable_queue is None:
            return
        completed_command = self._pending_client_completions.get(client_command_id)
        if completed_command is not None:
            command, deliveries = completed_command
            self._commit_client_completion(durable_queue, command, client_command_id, deliveries)
            return
        cancellation = await self.cancelled_user_message(client_command_id)
        if cancellation is not None:
            await self.emit_cancelled_user_message(UserCommand(**cancellation["command"]))
            return
        command = durable_queue.claim_client_command(client_command_id)
        if command is None:
            return
        try:
            if recovered and command.type == "user_message" and not await self._validate_executor_input_workspace(command):
                durable_queue.release_client_command(client_command_id)
                return
            with capture_durable_results(client_command_id) as capture:
                handled = await self._dispatch_client_command(command, connection_generation)
            if handled is False:
                # The handler reported a failure; retain the original command
                # until its delivery and replay semantics can be resolved.
                durable_queue.release_client_command(client_command_id)
                return
        except asyncio.CancelledError:
            try:
                durable_queue.release_client_command(client_command_id)
            except Exception:
                logger.exception("Failed to release cancelled client command %s", client_command_id)
            raise
        except Exception:
            try:
                durable_queue.release_client_command(client_command_id)
            except Exception:
                logger.exception("Failed to release failed client command %s", client_command_id)
            raise
        else:
            deliveries = [delivery for delivery in capture.deliveries if not self._delivery_succeeded(delivery)]
            self._pending_client_completions[client_command_id] = (command, deliveries)
            self._commit_client_completion(durable_queue, command, client_command_id, deliveries)

    async def _validate_executor_input_workspace(self, command: UserCommand, *, already_locked: bool = False) -> bool:
        from backend.environments.errors import ExecutorError
        from backend.services.workspace_service import conversation_execution_path
        from backend.workspace.executor_activation import validate_executor_workspace

        conversation_id = str(command.data.get("conversation_id") or self._session.active_conversation_id or "").strip()
        if not conversation_id:
            return True
        conversation = self._session.conversation_repo.get_conversation_summary(conversation_id)
        if conversation is None or not conversation_execution_path(conversation).startswith("file:"):
            return True
        manager = self._session.ws_manager
        async with AsyncExitStack() as scope:
            if not already_locked:
                if manager is not None:
                    await scope.enter_async_context(manager.conversation_maintenance_scope())
                await scope.enter_async_context(self._session.conversation_lifecycle_lock())
                if manager is not None:
                    await scope.enter_async_context(manager.conversation_resource_scope((conversation_id,)))
            if self._session.session_lifecycle.workspace_activation_failed(conversation_id):
                return False
            conversation = self._session.conversation_repo.get_conversation_summary(conversation_id)
            if conversation is None:
                return True
            workspace = conversation_execution_path(conversation)
            if not workspace.startswith("file:"):
                return self._session.session_lifecycle.recovered_input_workspace_ready(conversation_id)
            try:
                await validate_executor_workspace(self._session, conversation)
            except ExecutorError as exc:
                self._session.session_lifecycle.record_workspace_activation(conversation_id, False)
                with self._session.event_outbox.bind_client_command(
                    self._client_command_id(command), command.type, owner=(conversation_id, workspace),
                ):
                    await emit_command_error(self._session, "workspace.activate", f"Failed to restore workspace: {exc}",
                        data={"conversation_id": conversation_id, "workspace_root": workspace})
                return False
        return True

    @staticmethod
    def _delivery_succeeded(delivery: DurableResultDelivery) -> bool:
        receipt = delivery.receipt
        if receipt is None or not receipt.done():
            return False
        try:
            return bool(receipt.result() and delivery.outbox.connected
                        and delivery.outbox.connection_generation == delivery.generation)
        except (asyncio.CancelledError, Exception):
            return False

    def _commit_client_completion(self, durable_queue: Any, command: UserCommand, command_id: str,
                                  deliveries: list[DurableResultDelivery]) -> None:
        def owner_of(delivery: DurableResultDelivery) -> str:
            data = delivery.payload.get("data")
            return str(delivery.payload.get("conversation_id") or (data.get("conversation_id") if isinstance(data, dict) else "") or "").strip()

        owners = tuple(owner for delivery in deliveries if (owner := owner_of(delivery)))
        publication = self._session.conversation_repo.command_result_publication(owners) if owners else nullcontext(set())
        with publication as deleted:
            retained = [delivery for delivery in deliveries
                        if command.type == "conversation.delete" or owner_of(delivery) not in deleted]
            if retained:
                durable_queue.archive_client_results(command_id, [delivery.payload for delivery in retained])
            if not durable_queue.complete_client_command(command_id):
                raise RuntimeError(f"Could not complete durable client command {command_id}")
        self._pending_client_completions.pop(command_id, None)
        self._schedule_late_client_results(retained)
        # The queue already committed the completion atomically; the existing
        # recent-id log is a secondary index.
        self._mark_client_command_seen(command)
        for delivery in retained:
            payload, receipt = delivery.payload, delivery.receipt
            if receipt is None:
                continue
            if receipt.done():
                if self._delivery_succeeded(delivery):
                    durable_queue.acknowledge_client_result(command_id, payload["event_id"])
                continue

            async def settle_late_delivery(pending=receipt, event=delivery):
                try:
                    delivered = await asyncio.shield(pending)
                except Exception:
                    return
                if delivered and event.outbox.connected and event.outbox.connection_generation == event.generation:
                    durable_queue.acknowledge_client_result(command_id, event.payload["event_id"])

            self.track_command_task(asyncio.create_task(settle_late_delivery()))

    async def _dispatch_client_command(self, command: UserCommand, connection_generation: int) -> bool:
        _normalize_command_owner(command)
        navigation_receipt = self._session.session_lifecycle.admit_foreground_navigation(command)
        if command.type == "user_message" and self._client_command_id(command) and not command.data.get("_client_queue_key"):
            command.data["_client_queue_key"] = self._session.run_manager.durable_client_commands.path.stem
        task = asyncio.current_task()
        admission = _UserMessageAdmission(command, UserCommand(type=command.type,
            data=json.loads(json.dumps({key: value for key, value in command.data.items() if not key.startswith("_")}, ensure_ascii=False)))) if command.type == "user_message" else None
        if admission is not None:
            self._user_message_admissions[task] = admission
        try:
            conversation_id = str(command.data.get("owner_conversation_id")
                or command.data.get("ownerConversationId") or command.data.get("conversation_id")
                or self._session.active_conversation_id or "")
            try:
                if "workspace_root" in command.data:
                    workspace_root = command.data["workspace_root"]
                elif conversation_id and conversation_id != self._session.active_conversation_id:
                    conversation = self._session.conversation_repo.get_conversation_summary(conversation_id)
                    workspace_root = (
                        self._session.session_lifecycle.workspace_root_for_conversation(conversation)
                        if conversation is not None else ""
                    )
                else:
                    workspace_root = self._session.session_lifecycle.workspace_root_for_conversation()
            except Exception as exc:
                if not _is_conversation_lifecycle_command(command.type):
                    raise
                logger.error("Command %s owner resolution failed: %s", command.type, exc, exc_info=True)
                with self._session.event_outbox.bind_client_command(
                    self._client_command_id(command), command.type,
                    owner=(conversation_id, ""),
                ):
                    await emit_command_error(self._session, command.type, exc)
                return False
            owner = (conversation_id, str(workspace_root or ""))
            async with (nullcontext() if command.type in COMMAND_BACKLOG_BYPASS_TYPES else self._command_semaphore):
                with self._session.event_outbox.bind_client_command(
                    self._client_command_id(command), command.type, owner=owner,
                ), self._session.session_lifecycle.bind_foreground_navigation(navigation_receipt):
                    return await self._handle_command(command, connection_generation=connection_generation)
        except asyncio.CancelledError:
            if admission is None or not admission.interrupted:
                raise
            if admission.cancel_persistence is not None:
                await await_task_despite_cancellation(admission.cancel_persistence)
            await self._seal_unstarted_user_message(
                str(command.data.get("conversation_id") or self._session.active_conversation_id or ""),
                reason="user_interrupted", status="cancelled",
                message_id=str(command.data.get("assistant_message_id") or ""),
            )
            if self._client_command_id(command):
                await self.emit_cancelled_user_message(command)
            return True
        finally:
            with self._session.session_lifecycle.bind_foreground_navigation(navigation_receipt):
                self._session.session_lifecycle.finish_foreground_navigation()
            self._user_message_admissions.pop(task, None)

    async def interrupt_user_message_admission(self, conversation_id: str, message_id: str) -> bool:
        for task, admission in self._user_message_admissions.items():
            data = admission.command.data
            owner = str(data.get("conversation_id") or self._session.active_conversation_id or "")
            if owner != conversation_id or str(data.get("assistant_message_id") or "") != message_id:
                continue
            # Bind the durable Stop fence before signalling the concrete task.
            # Workspace cleanup and the receipt await this same operation.
            if not admission.interrupted:
                admission.cancel_persistence = asyncio.create_task(to_thread_cancel_safe(
                    self._persist_user_admission_stop, admission.command,
                    conversation_id=conversation_id, message_id=message_id,
                ))
                admission.interrupted = True
                task.cancel()
            await asyncio.shield(task)
            return True
        return False

    def _persist_user_admission_stop(
        self, command: UserCommand, *, conversation_id: str, message_id: str,
    ) -> None:
        if self._client_command_id(command):
            self.record_cancelled_user_message(command, reason="user_interrupted")
        self._session.run_manager.set_user_queue_paused(conversation_id, message_id)

    def record_cancelled_user_message(self, command: UserCommand, *, reason: str) -> None:
        client = self._session.run_manager.durable_client_commands
        shared = self._session.run_manager.durable_queue
        origin = str(command.data.get("_client_queue_key") or client.path.stem)
        record = shared.cancelled_client_command(self._client_command_id(command), client_queue_key=origin) if shared is not None else None
        if record is None:
            record = client.prepare_client_cancellation(command, reason=reason, client_queue_key=origin)
        if "client_queue_key" not in record:
            record = {**record, "client_queue_key": origin}
        original = UserCommand(type=record["command"]["type"], data={**record["command"]["data"], "_client_queue_key": origin})
        if shared is not None:
            record = shared.cancel_client_command(original, reason=reason, cancellation=record)
        if origin == client.path.stem:
            client.cancel_client_command(original, reason=reason, cancellation=record)

    async def cancelled_user_message(self, command_id: str, *, client_queue_key: str = "") -> dict[str, Any] | None:
        """The conversation queue's Stop intent is authoritative after a crash."""
        def resolve() -> dict[str, Any] | None:
            shared = self._session.run_manager.durable_queue
            client = self._session.run_manager.durable_client_commands
            origin = client_queue_key or client.path.stem
            record = shared.cancelled_client_command(command_id, client_queue_key=origin) if shared is not None else None
            if record is not None:
                if origin == client.path.stem:
                    client.cancel_client_command(UserCommand(**record["command"]), reason=record["reason"], cancellation=record)
                return record
            record = client.cancelled_client_command(command_id) if origin == client.path.stem else None
            if record is not None and shared is not None:
                record = {**record, "client_queue_key": origin}
                shared.cancel_client_command(UserCommand(**record["command"]), reason=record["reason"], cancellation=record)
            return record
        return await to_thread_cancel_safe(resolve)

    async def emit_cancelled_user_message(self, command: UserCommand) -> None:
        command_id = self._client_command_id(command)
        if not command_id:
            return
        cancellation = await self.cancelled_user_message(command_id, client_queue_key=str(command.data.get("_client_queue_key") or ""))
        if cancellation is None:
            raise RuntimeError(f"Cancelled input {command_id} has no durable Stop receipt")
        data = cancellation["command"]["data"]
        await self._session.send_event(AgentEvent(type="command.result", data={
            "command": "user_message", "level": "success", "message": "",
            "client_command_id": data["client_command_id"], "conversation_id": data.get("conversation_id", ""),
            "data": {"admitted": False, "cancelled": True, "reason": cancellation["reason"],
                     "conversation_id": data.get("conversation_id", ""),
                     "user_message_id": data.get("user_message_id", command.data.get("user_message_id", "")),
                     "assistant_message_id": data.get("assistant_message_id", command.data.get("assistant_message_id", ""))},
        }))

    def prune_command_tasks(self) -> None:
        for task in list(self._command_tasks):
            if task.done():
                self._command_tasks.discard(task)

    def track_command_task(self, task: asyncio.Task[Any]) -> None:
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)
        task.add_done_callback(self._on_command_task_done)

    @property
    def execution_command_tasks(self) -> set[asyncio.Task[Any]]:
        return self._execution_command_tasks

    def track_execution_command_task(self, task: asyncio.Task[Any], *, conversation_id: str = "") -> None:
        """Retain accepted input before its first coroutine step, including Home."""
        self._execution_command_tasks.add(task)
        task.add_done_callback(self._execution_command_tasks.discard)
        manager = self._session.ws_manager
        if manager is not None:
            manager.retain_execution_admission(self._session, task, conversation_id=conversation_id)
        task.add_done_callback(self._on_command_task_done)

    @staticmethod
    def _on_command_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            if EventOutbox.is_expected_disconnect_exception(exc):
                logger.debug(
                    "Ignoring expected websocket disconnect while handling command: %s",
                    exc,
                )
                return
            logger.error("Unhandled error in _handle_command: %s", exc, exc_info=exc)

    def _load_recent_client_command_ids(self) -> list[str]:
        return self._client_command_store.load_ids(limit=RECENT_CLIENT_COMMAND_IDS_MAX)

    def _client_command_id(self, command: UserCommand) -> str:
        client_command_id = command.data.get("client_command_id")
        if not isinstance(client_command_id, str):
            return ""
        return _clean_command_id(client_command_id)

    def _client_command_seen(self, command: UserCommand) -> bool:
        client_command_id = self._client_command_id(command)
        if not client_command_id:
            return False
        if client_command_id in self._recent_client_command_id_set:
            return True
        durable_queue = self._session.run_manager.durable_client_commands
        return durable_queue is not None and durable_queue.is_client_command_completed(client_command_id)

    def _mark_client_command_seen(
        self,
        command: UserCommand,
    ) -> bool:
        client_command_id = self._client_command_id(command)
        if not client_command_id:
            return False
        if client_command_id in self._recent_client_command_id_set:
            return True
        self._recent_client_command_id_set.add(client_command_id)
        self._recent_client_command_ids.append(client_command_id)
        try:
            self._client_command_store.append(client_command_id, command_type=command.type)
        except Exception as exc:
            logger.debug("Failed to persist client command id for %s: %s", self._session.session_id, exc)
        pruned = False
        while len(self._recent_client_command_ids) > RECENT_CLIENT_COMMAND_IDS_MAX:
            removed = self._recent_client_command_ids.pop(0)
            self._recent_client_command_id_set.discard(removed)
            pruned = True
        if pruned:
            try:
                self._client_command_store.rewrite_ids(self._recent_client_command_ids)
            except Exception as exc:
                logger.debug("Failed to compact client command ids for %s: %s", self._session.session_id, exc)
        return False

    async def _send_client_command_ack(
        self,
        command: UserCommand,
        *,
        duplicate: bool = False,
        accepted: bool = True,
        reason: str = "",
        envelope: bool = True,
    ) -> None:
        client_command_id = self._client_command_id(command)
        if not client_command_id:
            return
        await self._session.send_payload(
            {
                "type": "client.command.ack",
                "client_command_id": client_command_id,
                "command_type": command.type,
                **({"duplicate": True} if duplicate else {}),
                **({"accepted": False} if not accepted else {}),
                **({"reason": reason} if reason else {}),
            },
            log_context="client.command.ack",
            envelope=envelope,
        )

    async def _handle_command(
        self,
        command: UserCommand,
        connection_generation: int | None = None,
    ) -> bool:
        _normalize_command_owner(command)
        generation = (
            self._session.connection_generation
            if connection_generation is None
            else connection_generation
        )
        with self._session.event_outbox.bind_connection_generation(generation):
          try:
            recovering_workspace = False
            if _is_conversation_lifecycle_command(command.type):
                manager = self._session.ws_manager
                async with AsyncExitStack() as maintenance, AsyncExitStack() as scope:
                    # A window's model/project selection and its following send
                    # remain ordered. Separate windows only share task resources.
                    if manager is not None:
                        await maintenance.enter_async_context(manager.conversation_maintenance_scope(
                            exclusive=command.type == "memory.reset" or (
                                command.type == "llm.config.set" and not is_conversation_effort_command(
                                    command.data, command.data.get("conversation_id") or self._session.active_conversation_id,
                                )
                            ),
                        ))
                    await scope.enter_async_context(self._session.conversation_lifecycle_lock())
                    if manager is not None:
                        await scope.enter_async_context(manager.conversation_resource_scope(
                            _command_lifecycle_conversations(self._session, command)))
                    if manager is not None:
                        fenced_conversation_id = next(
                            (
                                conversation_id
                                for conversation_id in _command_targets_conversation_delete_fence(
                                    self._session,
                                    command,
                                )
                                if manager.conversation_delete_fence(conversation_id)
                            ),
                            "",
                        )
                        if fenced_conversation_id:
                            await emit_command_error(self._session,
                                command.type,
                                "This conversation is being deleted; wait for deletion to finish before changing it.",
                                data={
                                    "conversation_id": fenced_conversation_id,
                                    "reason": "delete_in_progress",
                                    "retryable": True,
                                },
                            )
                            return True
                    with bind_command_lifecycle_prefix(scope):
                        if command.type in {"conversation.switch", "session.restore"}:
                            recovery_target = (command.data.get("last_conversation_id") if command.type == "session.restore"
                                               else command.data.get("conversation_id"))
                            recovering_workspace = self._session.session_lifecycle.workspace_activation_failed(
                                str(recovery_target or "")
                            )
                        await self._handle_command_inner(command)
            else:
                await self._handle_command_inner(command)
            if command.type in {"session.restore", "session.sync", "conversation.switch", "conversation.activate"}:
                # Handlers return only after publishing the canonical owner
                # and its restore/replay projection. A pending scoped command
                # may now resume with its original idempotency key.
                await self._replay_pending_client_commands(generation,
                    replay_results=command.type in {"session.restore", "session.sync"})
                if command.type in {"conversation.switch", "session.restore"}:
                    target = (command.data.get("last_conversation_id") if command.type == "session.restore"
                              else command.data.get("conversation_id"))
                    owner = str(target or "")
                    if (owner and owner == self._session.active_conversation_id
                            and not self._session.session_lifecycle.workspace_activation_failed(owner)
                            and self._session.session_lifecycle.foreground_navigation_is_current()):
                        await self._resume_recovered_workspace(owner, previously_failed=recovering_workspace)
            return True
          except asyncio.CancelledError:
              raise
          except Exception as exc:
            # A handler that raises must still answer the client. Every caller
            # runs this as a bare task whose only failure path is
            # _on_command_task_done's logger.error, so without this the command
            # produced no response at all and any pending/spinner state in the
            # UI waited forever. The sibling path for a handler that *returns*
            # falsy already reports through emit_command_error.
              logger.error(
                  "Command %s failed: %s",
                  command.type,
                  exc,
                  exc_info=True,
              )
              try:
                  if command.type == "user_message":
                      await self._reject_user_message(command, str(command.data.get("conversation_id") or ""), str(exc))
                  else:
                      await emit_command_error(self._session, command.type, exc)
              except Exception:
                  logger.error(
                      "Could not report the failure of command %s to the client",
                      command.type,
                      exc_info=True,
                  )
              return False
          finally:
              self._finish_command_transition()

    def _finish_command_transition(self) -> None:
        transition = self._regeneration_transitions.pop(asyncio.current_task(), None)
        if transition is not None and conversation_query_guards().release_transition(transition):
            self._session.schedule_next_queued_user_message(transition.conversation_id)

    async def _execute_forwarded_command(self, command: UserCommand) -> None:
        # The transport/admission remains in the submitting dispatcher. Any
        # transition created by this execution dispatcher retains its own
        # finally, including refusal, exception and pre-run cancellation.
        try:
            await self._handle_command_inner(command)
        finally:
            self._finish_command_transition()

    async def _handle_control_response(self, command: UserCommand) -> None:
        """Acknowledge decision acceptance, not completion of the approved tool."""

        request_id, payload = self._session._normalize_control_response(command.data)
        if not payload:
            await emit_command_error(self._session, command.type, "Control response is empty")
            return
        oauth_pending = self._session.turn_wait_state.provider_oauth_pending
        oauth_future = oauth_pending.get(request_id)
        if oauth_future is not None:
            conversation_id = str(payload.get("conversation_id") or "").strip()
            expected_conversation = str(
                self._session.turn_wait_state.pending_approval_payloads.get(
                    request_id,
                    {},
                ).get("conversation_id")
                or ""
            ).strip()
            if expected_conversation and expected_conversation != conversation_id:
                await emit_command_error(
                    self._session, command.type,
                    "Approval response does not belong to the pending conversation",
                )
                return
            if oauth_future.done():
                await emit_command_error(
                    self._session, command.type,
                    f"Approval request '{request_id}' is stale or no longer pending",
                )
                return
            oauth_future.set_result(payload)
            await self._session.send_event(AgentEvent.command_result(command.type, "", level="success"))
            return
        owner_error = self._session.approval_response_owner_error(request_id, payload)
        if owner_error:
            await emit_command_error(self._session, command.type, owner_error)
            return
        pending_request = self._session.turn_wait_state.pending_approval_payloads[request_id].get("request", {})
        if pending_request.get("subtype") == "request_user_input" and payload.get("action") == "cancel":
            from backend.ws.handlers.misc import handle_interrupt_command

            await handle_interrupt_command(self._session, payload)
            await self._session.send_event(AgentEvent.command_result(command.type, "", level="success"))
            return
        if not self._session._resolve_pending_approval(request_id, payload):
            await emit_command_error(
                self._session, command.type,
                f"Control decision for '{request_id}' was not accepted: the request is stale or the response is invalid",
            )
            return
        await self._session.send_event(AgentEvent.command_result(command.type, "", level="success"))

    async def _handle_user_message_workspace(
        self,
        requested_workspace_root: str,
        target_conversation_id: str
    ) -> tuple[bool, str]:
        """Validate the input's workspace without publishing new thread settings."""
        from backend.services.workspace_service import (
            conversation_execution_path,
            parse_user_message_workspace_request,
            workspace_path_needs_activation,
        )

        target_conversation = (
            await to_thread_cancel_safe(self._session.conversation_repo.get_conversation_summary, target_conversation_id)
            if target_conversation_id
            else self._session.active_conversation
        )
        bound_workspace = conversation_execution_path(target_conversation)
        if requested_workspace_root.startswith("file:") or bound_workspace.startswith("file:"):
            from backend.workspace.executor_paths import ExecutorPath

            if (target_conversation is not None and requested_workspace_root.startswith("file:")
                    and bound_workspace.startswith("file:")
                    and ExecutorPath.parse(requested_workspace_root) == ExecutorPath.parse(bound_workspace)):
                return True, target_conversation.id
            event = AgentEvent.error(
                "This message targets a different project from its conversation. Open the project in a new conversation.",
                recoverable=True, error_type="workspace", error_code="workspace_owner_mismatch",
            )
            event.data["conversation_id"] = target_conversation_id
            await self._session.send_event(event)
            return False, target_conversation_id

        request = parse_user_message_workspace_request(
            requested_workspace_root,
            conversation_id=target_conversation_id,
        )
        if request.error_event is not None:
            await self._session.send_event(request.error_event)
            return False, target_conversation_id
        requested_workspace_path = request.project_path
        if requested_workspace_path is None:
            return False, target_conversation_id

        if bound_workspace:
            if not workspace_path_needs_activation(requested_workspace_path, Path(bound_workspace)):
                return True, target_conversation.id
            event = AgentEvent.error(
                "This message targets a different project from its conversation. Open the project in a new conversation.",
                recoverable=True, error_type="workspace", error_code="workspace_owner_mismatch",
            )
            event.data["conversation_id"] = target_conversation.id
            await self._session.send_event(event)
            return False, target_conversation.id
        return True, target_conversation_id

    async def _prepare_turn_settings(self, conversation_id: str, *, workspace_root: str,
                                     permission_mode: str | None, metadata: dict[str, Any]) -> PreparedTurnSettings:
        from backend.services.workspace_service import conversation_execution_path, create_workspace_context

        conversation = self._session.conversation_repo.get_conversation_summary(conversation_id)
        if conversation is None:
            raise ValueError(f"Conversation '{conversation_id}' was not found")
        binding = None
        context = None
        if workspace_root and not conversation_execution_path(conversation):
            path = Path(workspace_root).expanduser().resolve()
            context = create_workspace_context(path)
            await context.initialize()
            branch = await asyncio.to_thread(self._session.git_branch_for, path)
            binding = {"workspace_root": str(path), "execution_cwd": str(path),
                       "git_branch": branch, "worktree_path": "", "git_isolated": False}
        return PreparedTurnSettings(
            permission_mode=permission_mode,
            permission_revision=metadata.get("_permission_mode_selection_revision", conversation.permission_mode_revision),
            workspace_binding=binding,
            workspace_revision=metadata.get("_workspace_binding_selection_revision", conversation.workspace_binding_revision),
            execution_cwd=str(Path.cwd().resolve()) if not conversation.execution_cwd and binding is None else "",
            workspace_context=context,
            legacy_scope_cwd=(conversation.execution_cwd or str(Path.cwd().resolve()))
                if binding is None and not conversation.workspace_root and not conversation.projectless_output_dir else "",
        )

    async def _handle_user_message_permission(
        self,
        requested_permission_mode: str | None,
        target_conversation_id: str
    ) -> bool:
        """Project permissions which were committed with an admitted user item."""
        if requested_permission_mode is None:
            return True

        if not target_conversation_id or target_conversation_id == self._session.active_conversation_id:
            changed = self._session.set_permission_context_mode(requested_permission_mode, source="user_message")
            if changed:
                await self._session.emit_permission_mode_updated()
            if requested_permission_mode == "bypass":
                await self._session.auto_approve_pending_tool_approvals(
                    reason="permission_mode_bypass",
                    conversation_id=target_conversation_id,
                )
        return True

    async def _seal_unstarted_user_message(
        self,
        target_conversation_id: str,
        *,
        reason: str,
        message_id: str = "",
        status: str = "failed",
    ) -> None:
        """Publish the terminal fence for a turn that never started a run.

        The client clears its spinner on ``done``, or on an ``error`` whose
        ``recoverable`` is not true; a recoverable error deliberately keeps it
        spinning to wait for the ``done`` that normally follows.  Rejections in
        this handler happen before ``_start_agent_run``, so the run-task
        fallback in ``_wait_and_cleanup`` cannot reach them and that ``done``
        would never arrive.  Emitting it here keeps one invariant true for every
        accepted ``user_message``: exactly one terminal envelope, always.
        """
        done_event = AgentEvent.done(status=status, reason=reason)
        if target_conversation_id:
            done_event.data["conversation_id"] = target_conversation_id
        clean_message_id = str(message_id or "").strip()
        if not clean_message_id:
            stream_state = self._session._conversation_streams.get(target_conversation_id)
            clean_message_id = str((stream_state or {}).get("message_id") or "").strip()
        if clean_message_id:
            done_event.data["message_id"] = clean_message_id
        await self._session.send_event(done_event)
        await self._session.send_event(
            AgentEvent.session_state_changed(
                state="idle",
                conversation_id=target_conversation_id,
                reason=reason,
            )
        )

    async def _handle_control_cancel(self, command: UserCommand) -> None:
        """Handle control_cancel_request command."""
        request_id = str(command.data.get("request_id") or "").strip()
        oauth_future = self._session.turn_wait_state.provider_oauth_pending.get(request_id)
        if oauth_future is not None:
            supplied_conversation_id = str(
                command.data.get("conversation_id")
                or ""
            ).strip()
            expected_conversation_id = str(
                self._session.turn_wait_state.pending_approval_payloads.get(
                    request_id,
                    {},
                ).get("conversation_id")
                or ""
            ).strip()
            if expected_conversation_id != supplied_conversation_id:
                await emit_command_error(
                    self._session, command.type,
                    "Approval response does not belong to the pending conversation",
                )
                return
        else:
            owner_error = self._session.approval_response_owner_error(request_id, command.data)
            if owner_error:
                await emit_command_error(self._session, command.type, owner_error)
                return
        if not self._session._resolve_pending_approval(
            request_id,
            {
                "action": "reject",
                "guidance": "control request cancelled by client",
            },
        ):
            await emit_command_error(
                self._session, command.type,
                f"Control cancellation for '{request_id}' was not accepted: the request is stale or already settled",
            )
            return
        await self._session.send_event(AgentEvent.command_result(command.type, "", level="success"))

    def _bind_user_message_owner(self, command: UserCommand, conversation_id: str) -> None:
        command_id = self._client_command_id(command)
        if command_id:
            durable_queue = self._session.run_manager.durable_client_commands
            if (durable_queue is not None and command.data.get("_client_queue_key") == durable_queue.path.stem
                    and durable_queue.has_client_command(command_id)):
                if not durable_queue.bind_client_command_owner(command_id, conversation_id,
                        selection_revisions={key: command.data[key] for key in (
                            "_permission_mode_selection_revision", "_workspace_binding_selection_revision",
                        ) if key in command.data}):
                    raise RuntimeError("The current dispatcher does not own this durable input")
        command.data["conversation_id"] = conversation_id
        manager = self._session.ws_manager
        if manager is not None and conversation_id:
            manager.bind_execution_admission(self._session, asyncio.current_task(), conversation_id)

    async def _reject_user_message(self, command: UserCommand, conversation_id: str, message: str, *, reason: str = "") -> None:
        await self._session.send_event(AgentEvent(type="command.result", data={
            "command": "user_message", "level": "error", "message": message,
            "client_command_id": self._client_command_id(command),
            "conversation_id": conversation_id,
            "data": {"admitted": False, "conversation_id": conversation_id,
                     **({"reason": reason} if reason else {}),
                     "user_message_id": command.data.get("user_message_id", ""),
                     "assistant_message_id": command.data.get("assistant_message_id", ""),
                     "retry_from_message_id": command.data.get("retry_from_message_id", "")},
        }))

    async def _reject_steer(self, command: UserCommand, rejection: dict[str, Any], *,
                            message: str = "The requested turn is not available for steering; your input was retained.") -> None:
        await self._session.send_event(AgentEvent(type="command.result", data={
            "command": "user_message", "level": "error",
            "message": message,
            "client_command_id": self._client_command_id(command),
            "conversation_id": rejection["conversation_id"],
            "data": {**rejection, "user_message_id": command.data.get("user_message_id", ""),
                     "assistant_message_id": command.data.get("assistant_message_id", "")},
        }))

    async def _emit_native_user_message_completion(
        self, command: UserCommand, slash_command: str, results: list[dict[str, str]],
    ) -> None:
        command_id = self._client_command_id(command)
        if not command_id:
            return
        negative = next((result for result in results if result["level"].lower() in {"error", "failed"}), None)
        if negative is None:
            negative = next((result for result in results if result["level"].lower() == "warning"), None)
        completion = "refused" if negative is not None else "completed" if results else "unknown"
        outcome = negative if negative is not None else results[-1] if results else None
        conversation_id = str(command.data.get("conversation_id") or self._session.active_conversation_id or "")
        self._bind_user_message_owner(command, conversation_id)
        await self._session.send_event(AgentEvent(type="command.result", data={
            "command": "user_message", "client_command_id": command_id,
            "conversation_id": conversation_id,
            "level": outcome["level"] if outcome is not None else "warning",
            "message": outcome["message"] if negative is not None else (
                "本地命令完成结果尚未确认，输入已保留。" if completion == "unknown" else ""
            ),
            "data": {
                "admitted": False, "native_completion": completion,
                "native_completed": completion == "completed", "slash_command": slash_command,
                "conversation_id": conversation_id, "user_message_id": command.data.get("user_message_id", ""),
                "assistant_message_id": command.data.get("assistant_message_id", ""),
                "command_results": results,
            },
        }))

    async def _forward_conversation_command(self, command: UserCommand, conversation_id: str) -> bool:
        manager = self._session.ws_manager
        owner = manager.execution_session_for_conversation(conversation_id) if manager is not None else None
        if owner is None or owner is self._session:
            return False
        command.data["conversation_id"] = conversation_id
        workspace = self._session.session_lifecycle.workspace_root_for_conversation(
            self._session.conversation_repo.get_conversation_summary(conversation_id))
        with (bind_response_origin(self._session.session_id, self._client_command_id(command)),
              bind_execution_command_scope(owner.session_id, CommandScope(conversation_id, str(workspace or ""), self._client_command_id(command))),
              owner.event_outbox.bind_connection_generation(None),
              owner.event_outbox.bind_client_command(self._client_command_id(command), command.type,
                  owner=(conversation_id, str(workspace or "")))):
            await owner.command_dispatcher._execute_forwarded_command(command)
        return True

    async def _handle_command_inner(self, command: UserCommand) -> None:
        if command.type in _FORWARDED_CONVERSATION_COMMAND_TYPES:
            conversation_id = str(command.data.get("conversation_id") or (
                "" if command.type in {"user_message.queue.start", "user_message.queue.steer"}
                else self._session.active_conversation_id) or "")
            if await self._forward_conversation_command(command, conversation_id):
                return
        if self._session._extension_shutdown_requested and command.type not in {
            "control_response",
            "control_cancel_request",
            "interrupt",
        }:
            message = "Session shutdown was requested by an extension; no new work is accepted."
            if command.type == "user_message":
                await self._reject_user_message(command, str(command.data.get("conversation_id") or ""), message)
            else:
                await emit_command_error(self._session, command.type, message)
            return

        if command.type == "user_message":
            if self._client_command_id(command) and not command.data.get("_client_queue_key"):
                command.data["_client_queue_key"] = self._session.run_manager.durable_client_commands.path.stem
            content = str(command.data.get("content", ""))
            requested_conversation_id = str(
                command.data.get("conversation_id") or ""
            ).strip()
            target_conversation_id = requested_conversation_id or self._session.active_conversation_id or ""
            if requested_conversation_id:
                target = self._session.conversation_repo.get_conversation_summary(requested_conversation_id)
                implicit_new = command.data.get("_implicit_new_conversation_id") == requested_conversation_id
                if target is None and not implicit_new:
                    if command.data.get("retry_from_message_id"):
                        await self._reject_user_message(command, requested_conversation_id,
                            "The conversation no longer exists; regeneration has not been admitted.")
                    else:
                        await emit_conversation_not_found(self._session, requested_conversation_id)
                        await self._reject_user_message(command, requested_conversation_id,
                            "The conversation no longer exists; your input was retained.")
                    return
                if target is not None:
                    target_conversation_id = target.id

            try:
                attachments = normalize_attachment_payloads(command.data.get("attachments", []))
            except ValueError as exc:
                error_event = AgentEvent.error(
                    str(exc), recoverable=False, error_type="validation", error_code="invalid_attachments",
                )
                error_event.data["conversation_id"] = target_conversation_id
                await self._session.send_event(error_event)
                await self._reject_user_message(command, target_conversation_id, str(exc), reason="attachments_rejected")
                await self._seal_unstarted_user_message(
                    target_conversation_id, reason="attachments_rejected",
                    message_id=str(command.data.get("assistant_message_id") or ""),
                )
                return

            try:
                input_metadata = normalize_user_input_metadata(command.data)
            except ValueError as exc:
                await self._reject_user_message(command, target_conversation_id, str(exc), reason="input_metadata_rejected")
                await self._seal_unstarted_user_message(target_conversation_id,
                    reason="input_metadata_rejected", message_id=str(command.data.get("assistant_message_id") or ""))
                return
            command.data.update(input_metadata)

            from backend.conversations.models import normalize_conversation_agent_mode
            requested_agent_mode = (
                normalize_conversation_agent_mode(command.data["agent_mode"])
                if "agent_mode" in command.data and "code_review" not in input_metadata else None
            )
            raw_permission_mode = command.data.get("permission_mode")
            requested_permission_mode: str | None = None
            if raw_permission_mode is not None and not (
                isinstance(raw_permission_mode, str)
                and not raw_permission_mode.strip()
            ):
                requested_permission_mode = normalize_permission_mode(
                    str(raw_permission_mode)
                )
                if requested_permission_mode is None:
                    error_event = AgentEvent.error(
                        "Invalid permission mode. Use one of: plan, confirm, auto, bypass.",
                        recoverable=False,
                        error_type="validation",
                        error_code="invalid_permission_mode",
                    )
                    if target_conversation_id:
                        error_event.data["conversation_id"] = target_conversation_id
                    await self._session.send_event(error_event)
                    await self._reject_user_message(command, target_conversation_id, error_event.data["message"], reason="permission_mode_rejected")
                    await self._seal_unstarted_user_message(
                        target_conversation_id,
                        reason="permission_mode_rejected",
                        message_id=str(
                            command.data.get("assistant_message_id") or ""
                        ),
                    )
                    return

            # Validate prompt settings before admission; queued and rejected
            # input must not alter the running turn's workspace or permissions.
            if requested_permission_mode is not None:
                from backend.services.conversation_permission_service import plan_permission_mode_update

                permission_plan = plan_permission_mode_update(
                    {"mode": requested_permission_mode, "conversation_id": target_conversation_id},
                    active_conversation_id=self._session.active_conversation_id or "",
                    config=self._session.config,
                )
                if permission_plan.error_event is not None:
                    await self._session.send_event(permission_plan.error_event)
                    await self._reject_user_message(command, target_conversation_id, permission_plan.error_event.data["message"], reason="permission_mode_rejected")
                    await self._seal_unstarted_user_message(
                        target_conversation_id,
                        reason="permission_mode_rejected",
                        message_id=str(command.data.get("assistant_message_id") or ""),
                    )
                    return
            requested_workspace_root = str(
                command.data.get("workspace_root") or ""
            ).strip()

            if (command.data.get("_implicit_new_conversation_id") == target_conversation_id
                    and (content or attachments)
                    and self._session.conversation_repo.get_conversation_summary(target_conversation_id) is None):
                self._session.conversation_repo.create_conversation(conversation_id=target_conversation_id,
                    creation_command_id=(f"{self._session.run_manager.durable_client_commands.path.stem}:{self._client_command_id(command)}"
                                         if self._client_command_id(command) else ""))
                lifecycle = self._session.session_lifecycle
                if (not self._session.active_conversation_id and lifecycle.foreground_navigation_is_current()
                        and (not lifecycle._foreground_navigation_pending
                             or lifecycle._foreground_navigation_target_id == target_conversation_id)):
                    self._session._ensure_active_conversation(preferred_id=target_conversation_id)

            manager = self._session.ws_manager
            owner = manager.execution_session_for_conversation(target_conversation_id) if manager is not None else None
            if owner is not None and owner is not self._session:
                # The sending window admits only references granted to this
                # actual conversation/cwd. No attachment bytes, permission
                # cache, browser identity or session runtime is transferred.
                target = self._session.conversation_repo.get_conversation_summary(target_conversation_id)
                attachment_workspace = requested_workspace_root or str(
                    self._session.session_lifecycle.workspace_root_for_conversation(target) or "")
                unavailable = [item for item in attachments if self._session.attachment_store.get_payload(
                    item["artifact_id"], conversation_id=target_conversation_id,
                    workspace_root=attachment_workspace) is None]
                if unavailable:
                    from backend.agent.attachment_policy import AttachmentUnavailableError
                    await self._reject_user_message(command, target_conversation_id,
                        str(AttachmentUnavailableError(unavailable)), reason="attachments_rejected")
                    return
                command.data["attachments"] = attachments
                self._bind_user_message_owner(command, target_conversation_id)
                if await self._forward_conversation_command(command, target_conversation_id):
                    return

            message_metadata: dict[str, Any] = {
                key: str(command.data.get(key) or "").strip()
                for key in ("primary_file", "active_tab_path")
                if str(command.data.get(key) or "").strip()
            }
            from backend.agent.turn_input import ScheduledInputCommand, user_input_origin_from_trigger

            if isinstance(command, ScheduledInputCommand):
                message_metadata["input_origin"] = user_input_origin_from_trigger(command.turn_trigger)
                message_metadata["_source_responses_metadata"] = {"turn_trigger": command.turn_trigger}
                message_metadata["_host_scheduled_input"] = command
            message_metadata.update(input_metadata)
            if command.data.get("_client_queue_key"):
                message_metadata["_client_queue_key"] = command.data["_client_queue_key"]
            if "_guardian_acceptance_order" in command.data:
                message_metadata["_guardian_acceptance_order"] = command.data["_guardian_acceptance_order"]
            selected_skills = [
                {
                    "name": str(item.get("name") or "").strip(),
                    "path": str(item.get("path") or "").strip(),
                }
                for item in (command.data.get("skills") or [])
                if isinstance(item, dict)
                and str(item.get("name") or "").strip()
                and str(item.get("path") or "").strip()
            ]
            if selected_skills:
                message_metadata["selected_skills"] = selected_skills
            selected_plugins = [
                {
                    "config_name": str(
                        item.get("config_name")
                        or item.get("name")
                        or ""
                    ).strip(),
                    "path": str(item.get("path") or "").strip(),
                }
                for item in (command.data.get("plugins") or [])
                if isinstance(item, dict)
                and (
                    str(item.get("config_name") or item.get("name") or "").strip()
                    or str(item.get("path") or "").strip().startswith("plugin://")
                )
            ]
            if selected_plugins:
                message_metadata["selected_plugins"] = selected_plugins
            for source_key, target_key in (
                ("agent_mode", "agent_mode"),
                ("agent_role", "agent_role"),
            ):
                value = str(command.data.get(source_key) or "").strip()
                if value and not (source_key == "agent_mode" and "code_review" in input_metadata):
                    message_metadata[target_key] = value
            client_command_id = self._client_command_id(command)
            origin = str(message_metadata.get("_client_queue_key") or "")
            if client_command_id and not origin:
                raise ValueError("The queued input has no provable submitting window. Automatic replay stopped; its original input and attachments were retained.")
            stable_command_suffix = hashlib.sha256(
                f"{origin}\0{client_command_id}".encode("utf-8")
            ).hexdigest() if client_command_id else uuid.uuid4().hex
            user_message_id = str(command.data.get("user_message_id") or "").strip()
            assistant_message_id = str(command.data.get("assistant_message_id") or "").strip()
            current_for_admission = (
                self._session.conversation_repo.get_conversation(target_conversation_id)
                if (client_command_id or requested_agent_mode is not None or requested_permission_mode is not None
                    or requested_workspace_root) and target_conversation_id else None
            )
            if requested_agent_mode is not None:
                if command.data.get("_queued_user_message_dispatch"):
                    if "_agent_mode_selection_revision" in command.data:
                        message_metadata["_agent_mode_selection_revision"] = command.data["_agent_mode_selection_revision"]
                elif current_for_admission is not None:
                    message_metadata["_agent_mode_selection_revision"] = current_for_admission.agent_mode_revision
            for selection, field in (("_permission_mode_selection_revision", "permission_mode_revision"),
                                     ("_workspace_binding_selection_revision", "workspace_binding_revision")):
                if selection in command.data:
                    message_metadata[selection] = command.data[selection]
                elif current_for_admission is not None:
                    message_metadata[selection] = getattr(current_for_admission, field)
                if selection in message_metadata:
                    command.data[selection] = message_metadata[selection]
            journal = None
            existing_user = None
            prompt = None
            if client_command_id:
                message_metadata["client_command_id"] = client_command_id
                from backend.agent.runtime import default_runtime
                runtime = default_runtime()
                store_id = self._session.conversation_repo.store_instance_id()
                journal = runtime.execution_journal(execution_journal_owner(
                    "conversation", store_id, target_conversation_id,
                )) if target_conversation_id else None
                snapshot = current_for_admission.context_snapshot if current_for_admission is not None else {}
                users = {str(item.get("id") or ""): item for item in (
                    current_for_admission.transcript if current_for_admission is not None else []
                ) if isinstance(item, dict) and item.get("role") == "user"}
                candidates = [user_message_id or f"user_{stable_command_suffix}"]
                legacy_suffix = "".join(character for character in client_command_id
                    if character.isascii() and (character.isalnum() or character in {"-", "_"}))
                if not user_message_id and legacy_suffix:
                    candidates.append(f"user_{legacy_suffix}")
                if not user_message_id and command.data.get("_legacy_user_message_identity"):
                    # Old request IDs may contain only Unicode letters, so R6
                    # used random message IDs. Inspect its recorded admission,
                    # only in this explicit persisted upgrade window.
                    candidates.extend(str(message_id) for message_id, boundary in (snapshot.get("turn_admissions") or {}).items()
                        if boundary.get("client_command_id") == client_command_id)
                    if journal is not None:
                        candidates.extend(str(event.payload["user_message_id"]) for event in journal.read_events()
                            if event.event_type == "user_prompt" and event.payload.get("client_command_id") == client_command_id)
                candidates = list(dict.fromkeys(candidates))
                for candidate in candidates:
                    candidate_user = users.get(candidate)
                    candidate_prompt = journal.get_event(turn_user_prompt_id(store_id, target_conversation_id, candidate)) if journal is not None else None
                    candidate_admission = dict((snapshot.get("turn_admissions") or {}).get(candidate) or {})
                    if not candidate_admission and candidate_prompt is not None:
                        candidate_admission = dict((candidate_prompt.payload.get("context_snapshot", {}).get("turn_admissions") or {}).get(candidate) or {})
                    projection = candidate_user or (candidate_prompt.payload if candidate_prompt is not None else None)
                    if projection is None:
                        continue
                    same_payload = _same_admitted_input(projection,
                        str(command.data.get("_submitted_content", content)), attachments, input_metadata)
                    recorded_origin = _admitted_input_origin(candidate_admission, candidate_user, runtime)
                    same_owner = str(candidate_admission.get("client_command_id") or "") == client_command_id and recorded_origin == origin
                    if not (same_payload and same_owner):
                        if candidate == candidates[0] or same_owner:
                            raise ValueError("This user_message_id was already admitted with different content or ownership; the input was retained.")
                        if same_payload and not recorded_origin and command.data.get("_legacy_user_message_identity"):
                            raise ValueError("The legacy admitted input has no provable submitting window. Automatic replay stopped; its original input and attachments were retained.")
                        continue
                    user_message_id = candidate
                    assistant_message_id = assistant_message_id or str(candidate_admission.get("assistant_message_id")
                        or (candidate_prompt.payload.get("assistant_message_id") if candidate_prompt is not None else "")
                        or (f"assistant_{legacy_suffix}" if "client_queue_key" not in candidate_admission and legacy_suffix else ""))
                    if not assistant_message_id and "client_queue_key" not in candidate_admission:
                        raise ValueError("The legacy admitted input's original assistant identity cannot be recovered. Automatic replay stopped; its original input and attachments were retained.")
                    existing_user, prompt = candidate_user, candidate_prompt
                    break
            user_message_id = user_message_id or f"user_{stable_command_suffix}"
            assistant_message_id = assistant_message_id or f"assistant_{stable_command_suffix}"
            message_metadata.update(user_message_id=user_message_id, assistant_message_id=assistant_message_id)
            command.data.update(user_message_id=user_message_id, assistant_message_id=assistant_message_id)
            if journal is not None:
                rejection = journal.get_event(turn_input_rejection_id(user_message_id, client_command_id))
                if rejection is not None:
                    await self._session.send_event(AgentEvent(type="command.result", data=rejection.payload["result"]))
                    return
                consumption = journal.get_event(turn_input_consumption_id(user_message_id, client_command_id))
                if consumption is not None and (existing_user is not None or prompt is not None):
                    await self._session.send_event(AgentEvent(type="command.result", data={
                        "command": "user_message", "level": "success", "message": "",
                        "client_command_id": client_command_id,
                        "conversation_id": target_conversation_id,
                        "data": {"admitted": True, "consumed": True, "replayed": True,
                                 "conversation_id": target_conversation_id,
                                 "user_message_id": user_message_id,
                                 "assistant_message_id": assistant_message_id,
                                 "run_id": consumption.payload["run_id"]},
                    }))
                    return
                if existing_user is not None:
                    message_metadata["_turn_admission_restored"] = True
                    content = str(existing_user.get("content") or "")
                    if "submitted_content" in existing_user:
                        message_metadata["submitted_content"] = existing_user["submitted_content"]

            steer_requested = str(command.data.get("streaming_behavior") or command.data.get("streamingBehavior") or "").strip().lower() == "steer" and not command.data.get("_queued_user_message_dispatch")
            expected_turn_id = str(command.data.get("expected_turn_id") or "").strip()
            if steer_requested:
                rejection = self._session.run_manager.steer_target_rejection(target_conversation_id, expected_turn_id)
                if rejection is not None:
                    await self._reject_steer(command, rejection)
                    return

            if "_submitted_content" in command.data:
                message_metadata["submitted_content"] = command.data["_submitted_content"]
            message_metadata["_submitted_command"] = {
                "type": command.type,
                "data": {**{key: value for key, value in command.data.items() if not key.startswith("_")},
                         "content": command.data.get("_submitted_content", command.data.get("content", ""))},
            }

            retry_from_message_id = str(command.data.get("retry_from_message_id", "")).strip()
            slash_input = str(input_metadata.get("display_content", content)).strip()
            stripped = slash_input.lstrip()
            if (not message_metadata.get("_turn_admission_restored")
                    and not retry_from_message_id and content.rstrip().endswith(slash_input)
                    and stripped.startswith("/") and not stripped.startswith("//")):
                if target_conversation_id:
                    await self._session._ensure_extension_commands_for_conversation(
                        target_conversation_id
                    )
                parts = stripped.split(maxsplit=1)
                cmd_name = parts[0].lower()
                cmd_arg = parts[1] if len(parts) > 1 else ""
                registry = self._session.command_registry
                handler = registry.get_extension_slash(cmd_name, scope_id=target_conversation_id)
                builtin_review = False
                if handler is None:
                    from backend.commands.catalog import get_composer_command_catalog
                    from backend.commands.slash_commands import build_slash_command_handler
                    from backend.services.workspace_service import parse_user_message_workspace_request

                    if requested_workspace_root.startswith("file:"):
                        success, _ = await self._handle_user_message_workspace(requested_workspace_root, target_conversation_id)
                        if not success:
                            await self._reject_user_message(command, target_conversation_id,
                                "The workspace could not be activated; your input was retained.", reason="workspace_activation_failed")
                            await self._seal_unstarted_user_message(target_conversation_id,
                                reason="workspace_activation_failed", message_id=assistant_message_id)
                            return
                        command_root = None
                    elif requested_workspace_root:
                        request = parse_user_message_workspace_request(
                            requested_workspace_root, conversation_id=target_conversation_id,
                        )
                        if request.error_event is not None:
                            await self._session.send_event(request.error_event)
                            await self._reject_user_message(command, target_conversation_id,
                                request.error_event.data["message"], reason="workspace_activation_failed")
                            await self._seal_unstarted_user_message(target_conversation_id,
                                reason="workspace_activation_failed", message_id=assistant_message_id)
                            return
                        command_root = request.project_path
                    else:
                        owner = self._session.conversation_repo.get_conversation_summary(target_conversation_id) if target_conversation_id else None
                        command_root = self._session.session_lifecycle.workspace_root_for_conversation(owner) if owner is not None else None
                    catalog = await to_thread_cancel_safe(get_composer_command_catalog,
                        command_root, resolve_active_workspace=False)
                    entry = next((entry for entry in catalog
                                  if f"/{entry['command']}".casefold() == cmd_name.casefold()), None)
                    builtin_review = entry is not None and cmd_name == "/review" and entry.get("source") == "builtin"
                    handler = build_slash_command_handler(entry) if entry is not None else None
                if handler is not None:
                    with self._session.event_outbox.capture_command_results(include_data=builtin_review) as command_results:
                        handled, content_override = await self._session.command_registry.dispatch_slash(
                            self._session,
                            cmd_name,
                            cmd_arg,
                            attachments,
                            scope_id=target_conversation_id,
                            handler=handler,
                        )
                    if handled:
                        await self._emit_native_user_message_completion(command, cmd_name, command_results.results)
                        return
                    if builtin_review:
                        review_scope = command_results.results[-1]["data"]["code_review"]
                        prepared_content = content.rstrip()[:-len(slash_input)] + content_override
                        review_request = content_override.rsplit("## My request:", 1)[1].strip()
                        from backend.services.git_settings_service import get_git_preferences

                        if get_git_preferences().review_delivery == "detached":
                            from backend.services.review_delivery_service import queue_detached_review

                            review_thread_id = await queue_detached_review(self._session, target_conversation_id,
                                review_scope, prompt=prepared_content, request_message=review_request, input_id=user_message_id)
                            with self._session.event_outbox.capture_command_results(include_data=True) as review_results:
                                await self._session.emit_command_result("review", "Separate review input queued.", level="success",
                                    data={"reviewThreadId": review_thread_id, "sourceConversationId": target_conversation_id,
                                          "delivery": "detached", "queued": True})
                            await self._emit_native_user_message_completion(command, cmd_name, review_results.results)
                            return
                        command.data["code_review"] = review_scope
                        message_metadata["code_review"] = review_scope
                        message_metadata["display_content"] = review_request
                        command.data["display_content"] = message_metadata["display_content"]
                        requested_agent_mode = None
                        message_metadata.pop("agent_mode", None)
                        message_metadata.pop("_agent_mode_selection_revision", None)
                        command.data.pop("agent_mode", None)
                    prepared_content = content.rstrip()[:-len(slash_input)] + content_override
                    if prepared_content != content:
                        command.data["_submitted_content"] = content
                        message_metadata["submitted_content"] = content
                    content = prepared_content
                    if builtin_review:
                        command.data["content"] = prepared_content

            if content or attachments:
                if not target_conversation_id:
                    self._session._ensure_active_conversation()
                    target_conversation_id = self._session.active_conversation_id or ""
                    created = self._session.conversation_repo.get_conversation(target_conversation_id)
                    if requested_agent_mode is not None:
                        message_metadata["_agent_mode_selection_revision"] = created.agent_mode_revision
                    message_metadata["_permission_mode_selection_revision"] = created.permission_mode_revision
                    message_metadata["_workspace_binding_selection_revision"] = created.workspace_binding_revision
                    command.data["_permission_mode_selection_revision"] = created.permission_mode_revision
                    command.data["_workspace_binding_selection_revision"] = created.workspace_binding_revision
                self._bind_user_message_owner(command, target_conversation_id)
                queued_dispatch = bool(command.data.pop("_queued_user_message_dispatch", False))
                message_metadata["_queued_user_message_dispatch"] = queued_dispatch
                if retry_from_message_id:
                    # Reserve cancellation, private rewind and native admission as
                    # one transition. Accepted history and follow-ups stay durable
                    # until the replacement input is committed.
                    current = self._session.conversation_repo.get_conversation(target_conversation_id)
                    if current is None:
                        await self._reject_user_message(command, target_conversation_id,
                            "The conversation no longer exists; regeneration has not been admitted.")
                        return
                    retry_exists = any(
                        str(message.get("id") or "").strip() == retry_from_message_id
                        and str(message.get("role") or "").strip() == "user"
                        for message in list(current.transcript or [])
                        if isinstance(message, dict)
                    )
                    if not retry_exists:
                        await self._reject_user_message(command, target_conversation_id,
                            f"Cannot regenerate from message '{retry_from_message_id}'")
                        return
                    task_id = self._session.run_manager.run_task_ids.get(target_conversation_id, "")
                    query_guards = conversation_query_guards()
                    transition = query_guards.try_transition(
                        target_conversation_id,
                        owner_id=f"regenerate:{self._session.session_id}:{self._client_command_id(command)}",
                        replacing_owner_id=f"ws:{self._session.session_id}:{task_id}" if task_id else "",
                    )
                    if transition is None:
                        await self._reject_user_message(command, target_conversation_id,
                            "Another run is active on this conversation; stop it before regenerating.")
                        return
                    self._regeneration_transitions[asyncio.current_task()] = transition
                    if self._session.running_agent_task_for(target_conversation_id):
                        await self._session.cancel_agent_runs(
                            conversation_id=target_conversation_id,
                            reason="user_regenerated",
                        )
                    current = self._session.conversation_repo.get_conversation(target_conversation_id)
                    if current is None:
                        await self._reject_user_message(command, target_conversation_id,
                            "The conversation no longer exists; regeneration has not been admitted.")
                        return
                    if not query_guards.transition_ready(transition):
                        await self._reject_user_message(command, target_conversation_id,
                            "The previous run is still cleaning up; regeneration has not been admitted.")
                        return
                    prepared = self._session._prepare_retry_from_message(
                        conversation=current,
                        retry_from_message_id=retry_from_message_id,
                    )
                    message_metadata["_conversation_transition"] = transition
                    message_metadata["retry_from_message_id"] = retry_from_message_id
                    if prepared is None:
                        await self._reject_user_message(command, target_conversation_id,
                            f"Cannot regenerate from message '{retry_from_message_id}'")
                        return
                    message_metadata["_retry_projection"] = prepared
                running_for_target = self._session.running_agent_task_for(target_conversation_id)
                if steer_requested:
                    rejection = self._session.run_manager.steer_target_rejection(target_conversation_id, expected_turn_id)
                    if rejection is not None:
                        await self._reject_steer(command, rejection)
                        return
                pause_token = self._session.run_manager.user_queue_pause_token(target_conversation_id)
                if not queued_dispatch and pause_token:
                    if (
                        not self._session.run_manager.queued_user_messages(target_conversation_id)
                        and running_for_target is None
                        and not self._session.run_manager.has_pending_lifecycle_cleanup(target_conversation_id)
                    ):
                        self._session.run_manager.set_user_queue_paused(target_conversation_id, "")
                    else:
                        # This newly submitted input is explicit consent to its
                        # own execution, without releasing older paused work.
                        command.data["_queue_explicit_send"] = pause_token
                if (
                    running_for_target
                    or self._session.run_manager.is_queue_dispatching(target_conversation_id)
                    or self._session.run_manager.has_pending_lifecycle_cleanup(target_conversation_id)
                ) and not queued_dispatch:
                    accepted_conversation = self._session.conversation_repo.get_conversation(target_conversation_id)
                    guardian_order = self._session.guardian_host_for_conversation(target_conversation_id,
                        evidence_checkpoint=accepted_conversation.context_snapshot.get("guardian_evidence")).evidence.reserve_order()
                    from dataclasses import replace

                    queued_command = replace(command,
                        type="user_message",
                        data={
                            **command.data,
                            **({"_agent_mode_selection_revision": message_metadata["_agent_mode_selection_revision"]}
                               if "_agent_mode_selection_revision" in message_metadata else {}),
                            **{key: message_metadata[key] for key in (
                                "_permission_mode_selection_revision", "_workspace_binding_selection_revision",
                            ) if key in message_metadata},
                            "content": content,
                            "conversation_id": target_conversation_id,
                            "_guardian_acceptance_order": guardian_order,
                            **({"assistant_message_id": assistant_message_id} if assistant_message_id else {}),
                        },
                    )
                    streaming_behavior = str(
                        command.data.get("streaming_behavior")
                        or command.data.get("streamingBehavior")
                        or ""
                    ).strip().lower()
                    if streaming_behavior == "steer":
                        stream_state = self._session._conversation_streams[target_conversation_id]
                        changed_setting = ""
                        if requested_agent_mode is not None and requested_agent_mode != stream_state.get("agent_mode"):
                            changed_setting = "agent_mode"
                        elif requested_permission_mode is not None and requested_permission_mode != accepted_conversation.permission_mode:
                            changed_setting = "permission_mode"
                        elif requested_workspace_root:
                            from backend.services.workspace_service import parse_user_message_workspace_request, workspace_path_needs_activation

                            if requested_workspace_root.startswith("file:"):
                                from backend.services.workspace_service import conversation_execution_path
                                from backend.workspace.executor_paths import ExecutorPath

                                current_workspace = conversation_execution_path(accepted_conversation)
                                if (not current_workspace.startswith("file:")
                                        or ExecutorPath.parse(requested_workspace_root) != ExecutorPath.parse(current_workspace)):
                                    changed_setting = "workspace_root"
                            else:
                                workspace_request = parse_user_message_workspace_request(
                                    requested_workspace_root, conversation_id=target_conversation_id,
                                )
                                current_workspace = self._session.session_lifecycle.workspace_root_for_conversation(accepted_conversation)
                                if (workspace_request.error_event is not None or current_workspace is None
                                        or workspace_path_needs_activation(workspace_request.project_path, current_workspace)):
                                    changed_setting = "workspace_root"
                        if changed_setting:
                            await self._reject_steer(command, {
                                "conversation_id": target_conversation_id, "expected_turn_id": expected_turn_id,
                                "active_turn_id": stream_state["turn_id"], "reason": "steer_configuration_mismatch",
                                "error_code": "steer_configuration_mismatch", "field": changed_setting,
                                "admitted": False, "queued": False,
                            }, message="Steering cannot change the running turn's execution settings; your input was retained.")
                            return
                    if streaming_behavior == "steer" and running_for_target is not None:
                        stream_state = self._session._conversation_streams.get(target_conversation_id) or {}
                        target_message_id = str(stream_state.get("message_id") or "").strip()
                        steered = self._session.run_manager.enqueue_user_message_as_steer(
                            target_conversation_id,
                            queued_command,
                            expected_turn_id=expected_turn_id,
                            target_message_id=target_message_id,
                        )
                        if steered is not None:
                            replayed = steered.original_command is not queued_command
                            await self._session.send_event(AgentEvent(type="command.result", data={
                                "command": "user_message", "level": "success", "message": "",
                                "client_command_id": client_command_id, "conversation_id": target_conversation_id,
                                "data": {"admitted": False, "queued": True, "turn_mode": "steer",
                                         "conversation_id": target_conversation_id, "user_message_id": user_message_id,
                                         "assistant_message_id": assistant_message_id,
                                         **({"replayed": True} if replayed else {})},
                            }))
                            if replayed:
                                return
                            await self._session.send_event(
                                AgentEvent.user_message_queue_updated(
                                    status="dequeued",
                                    conversation_id=target_conversation_id,
                                    message_id=steered.message_id or assistant_message_id,
                                    user_message_id=steered.user_message_id,
                                    reason="steered_current_turn",
                                    target_message_id=steered.target_message_id,
                                    turn_mode="steer",
                                )
                            )
                            await self._session._reject_pending_approvals(
                                reason="user_steer",
                                guidance=(
                                    "The user redirected the current task; this pending action was superseded."
                                ),
                                conversation_id=target_conversation_id,
                            )
                            return
                        rejection = self._session.run_manager.steer_target_rejection(target_conversation_id, expected_turn_id)
                        await self._reject_steer(command, rejection or {
                            "conversation_id": target_conversation_id, "expected_turn_id": expected_turn_id,
                            "active_turn_id": str(stream_state.get("turn_id") or ""),
                            "reason": "steer_target_unavailable", "error_code": "steer_target_unavailable",
                            "admitted": False, "queued": False,
                        })
                        return
                    position = self._session.run_manager.enqueue_user_message(target_conversation_id, queued_command)
                    if (requested_agent_mode is not None and not message_metadata.get("_turn_admission_restored")
                            and not str(message_metadata.get("agent_role") or "main").startswith("subagent:")):
                        # This durable input is accepted now. Replaying it later
                        # controls its own turn, never newer conversation defaults.
                        self._session.conversation_repo.update_agent_mode(target_conversation_id, requested_agent_mode,
                            expected_agent_mode_revision=message_metadata["_agent_mode_selection_revision"])
                    await self._session.send_event(
                        AgentEvent.user_message_queue_updated(
                            status="queued",
                            conversation_id=target_conversation_id,
                            message_id=assistant_message_id,
                            user_message_id=str(command.data.get("user_message_id") or ""),
                            position=position,
                        )
                    )
                    await self._session.send_event(AgentEvent(type="command.result", data={
                        "command": "user_message", "level": "success", "message": "",
                        "client_command_id": client_command_id,
                        "conversation_id": target_conversation_id,
                        "data": {"admitted": False, "queued": True, "conversation_id": target_conversation_id,
                                 "user_message_id": user_message_id, "assistant_message_id": assistant_message_id},
                    }))
                    return

                if requested_workspace_root:
                    success, target_conversation_id = await self._handle_user_message_workspace(
                        requested_workspace_root, target_conversation_id,
                    )
                    if not success:
                        await self._reject_user_message(command, target_conversation_id,
                            "The workspace could not be activated; your input was retained.", reason="workspace_activation_failed")
                        await self._seal_unstarted_user_message(
                            target_conversation_id, reason="workspace_activation_failed", message_id=assistant_message_id,
                        )
                        return
                if not self._session.session_lifecycle.recovered_input_workspace_ready(target_conversation_id):
                    await self._reject_user_message(command, target_conversation_id,
                        "The conversation workspace is unavailable; reopen or trust it before starting work.", reason="workspace_unavailable")
                    await self._seal_unstarted_user_message(target_conversation_id,
                        reason="workspace_unavailable", message_id=assistant_message_id)
                    return
                if not await self._validate_executor_input_workspace(command, already_locked=True):
                    if queued_dispatch:
                        raise RuntimeError("The conversation's executor workspace is unavailable; queued input was retained.")
                    await self._reject_user_message(command, target_conversation_id,
                        "The conversation workspace is unavailable; reopen or trust it before starting work.", reason="workspace_unavailable")
                    await self._seal_unstarted_user_message(target_conversation_id,
                        reason="workspace_unavailable", message_id=assistant_message_id)
                    return
                try:
                    turn_settings = await self._prepare_turn_settings(target_conversation_id,
                        workspace_root=requested_workspace_root, permission_mode=requested_permission_mode,
                        metadata=message_metadata)
                except (OSError, ValueError) as exc:
                    await self._reject_user_message(command, target_conversation_id, str(exc), reason="turn_settings_rejected")
                    await self._seal_unstarted_user_message(target_conversation_id,
                        reason="turn_settings_rejected", message_id=assistant_message_id)
                    return
                self._user_message_admissions.pop(asyncio.current_task(), None)
                await self._session.start_agent_run(
                    content,
                    attachments=attachments,
                    conversation_id=target_conversation_id,
                    metadata=message_metadata,
                    turn_settings=turn_settings,
                )
            else:
                await self._reject_user_message(command, target_conversation_id, "The message is empty; no turn was started.")
            return

        if command.type == "control_response":
            await self._handle_control_response(command)
            return

        if command.type == "control_cancel_request":
            await self._handle_control_cancel(command)
            return

        handled = await self._session.command_registry.dispatch(command.type, command.data)
        if not handled:
            await emit_command_error(self._session, command.type, f"Unsupported command '{command.type}'")
        elif command.type == "interrupt":
            await self._session.send_event(AgentEvent.command_result(command.type, "", level="success"))
