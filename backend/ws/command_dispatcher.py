from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from backend.agent.message import AgentEvent, UserCommand
from backend.ws.client_command_log import ClientCommandDedupStore, _clean_command_id
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.event_outbox import EventOutbox
from backend.ws.utils import normalize_attachment_payloads, normalize_permission_mode

logger = logging.getLogger(__name__)

MAX_PENDING_COMMAND_TASKS = 100
COMMAND_BACKLOG_ERROR_INTERVAL_SECONDS = 2.0
RECENT_CLIENT_COMMAND_IDS_MAX = 2_000

COMMAND_BACKLOG_BYPASS_TYPES = {
    "control_response",
    "control_cancel_request",
    "interrupt",
    "mcp.inventory.cancel",
}

_CONVERSATION_LIFECYCLE_COMMAND_TYPES = {
    "memory.reset",
    "session.restore",
    "session.sync",
    "workspace.import",
    "workspace.set",
    "workspace.switch",
    "context.compact",
    "context.fork",
    "user_message.queue.cancel",
    "user_message.queue.steer",
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
        self._active_client_command_ids: set[str] = set()
        self._reported_durable_queue_load_error = ""
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
    def reported_durable_queue_load_error(self) -> str:
        return self._reported_durable_queue_load_error

    @reported_durable_queue_load_error.setter
    def reported_durable_queue_load_error(self, value: str) -> None:
        self._reported_durable_queue_load_error = value

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

    async def _replay_pending_client_commands(self, connection_generation: int) -> None:
        durable_queue = self._session.run_manager.durable_queue
        if durable_queue is None:
            return
        await self._report_durable_queue_load_error(durable_queue)
        for command in durable_queue.pending_client_commands():
            command_id = self._client_command_id(command)
            if not command_id:
                continue
            if self._client_command_seen(command):
                # The dedup log is the completion record.  Reconcile a stale
                # pending entry left beside it so reconnect does not replay an
                # already-applied command forever.
                durable_queue.discard_pending_client_command(command_id)
                await self._send_client_command_ack(command, duplicate=True)
                continue
            await self._send_client_command_ack(command, duplicate=True)
            self._schedule_durable_client_command(command_id, connection_generation)

    async def _report_durable_queue_load_error(self, durable_queue: Any) -> None:
        """Surface an unreadable durable queue file to the client.

        The queue quarantines a corrupt file instead of overwriting it, so the
        commands still exist on disk but were not loaded. Reporting only through
        a server-side log made the user's queued messages disappear silently.
        The evidence path is the dedupe key, so a reconnect does not repeat a
        warning the client already has, while a fresh failure is reported again.
        """

        evidence = durable_queue.load_error
        if not isinstance(evidence, dict) or not evidence:
            return
        signature = str(
            evidence.get("quarantined_to") or evidence.get("path") or evidence.get("reason") or ""
        )
        if signature and signature == self._reported_durable_queue_load_error:
            return
        self._reported_durable_queue_load_error = signature
        await self._session.emit_command_result(
            "user_message.queue.restore",
            "Queued messages could not be restored: the durable queue file was unreadable. "
            "It has been preserved for recovery instead of overwritten.",
            level="error",
            data={
                "reason": str(evidence.get("reason") or "unreadable"),
                "detail": str(evidence.get("detail") or ""),
                "path": str(evidence.get("path") or ""),
                "quarantined_to": str(evidence.get("quarantined_to") or ""),
                "quarantine_error": str(evidence.get("quarantine_error") or ""),
                "recoverable": False,
            },
        )

    async def run(self, connection_generation: int) -> None:
        """Receive and admit commands for one attached websocket generation."""

        while True:
            if connection_generation != self._session.connection_generation:
                return

            raw = await self._session.ws.receive_text()
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
            command_backlog_full = len(self._command_tasks) >= self._max_command_tasks
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
            durable_queue = self._session.run_manager.durable_queue
            if command_id and durable_queue is not None:
                if durable_queue.has_client_command(command_id):
                    await self._send_client_command_ack(command, duplicate=True)
                    self._schedule_durable_client_command(command_id, connection_generation)
                    continue
                try:
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
                self._schedule_durable_client_command(command_id, connection_generation)
                continue

            self._schedule_transient_client_command(command, connection_generation)

    def _schedule_transient_client_command(
        self,
        command: UserCommand,
        connection_generation: int,
    ) -> None:
        async def _guarded_handle() -> None:
            async with self._command_semaphore:
                with self._session.event_outbox.bind_client_command(
                    self._client_command_id(command),
                    command.type,
                ):
                    await self._handle_command(
                        command,
                        connection_generation=connection_generation,
                    )

        self.track_command_task(asyncio.create_task(_guarded_handle()))

    def _schedule_durable_client_command(
        self,
        client_command_id: str,
        connection_generation: int,
    ) -> None:
        command_id = _clean_command_id(client_command_id)
        if not command_id or command_id in self._active_client_command_ids:
            return
        self._active_client_command_ids.add(command_id)
        task = asyncio.create_task(
            self._run_durable_client_command(command_id, connection_generation)
        )

        def _release_active(_task: asyncio.Task[Any]) -> None:
            self._active_client_command_ids.discard(command_id)

        task.add_done_callback(_release_active)
        self.track_command_task(task)

    async def _run_durable_client_command(
        self,
        client_command_id: str,
        connection_generation: int,
    ) -> None:
        durable_queue = self._session.run_manager.durable_queue
        if durable_queue is None:
            return
        command = durable_queue.claim_client_command(client_command_id)
        if command is None:
            return
        try:
            async with self._command_semaphore:
                with self._session.event_outbox.bind_client_command(
                    client_command_id,
                    command.type,
                ):
                    handled = await self._handle_command(
                        command,
                        connection_generation=connection_generation,
                    )
            if handled is False:
                # ``_handle_command`` reports the failure to the client and
                # returns False.  It is not a successful durable completion:
                # keep the command pending so reconnect can retry it.
                durable_queue.release_client_command(client_command_id)
                return
            # Record the completed side effect before removing the durable
            # command.  A crash between those writes must replay as a
            # duplicate acknowledgement, never execute the command twice.
            self._mark_client_command_seen(command, require_persistence=True)
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
            if not durable_queue.complete_client_command(client_command_id):
                raise RuntimeError(
                    f"Could not complete durable client command {client_command_id}"
                )

    def prune_command_tasks(self) -> None:
        for task in list(self._command_tasks):
            if task.done():
                self._command_tasks.discard(task)

    def track_command_task(self, task: asyncio.Task[Any]) -> None:
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)
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
        try:
            return self._client_command_store.load_ids(limit=RECENT_CLIENT_COMMAND_IDS_MAX)
        except Exception as exc:
            logger.debug("Failed to load recent client command ids for %s: %s", self._session.session_id, exc)
            return []

    def _client_command_id(self, command: UserCommand) -> str:
        client_command_id = command.data.get("client_command_id")
        if not isinstance(client_command_id, str):
            return ""
        return _clean_command_id(client_command_id)

    def _client_command_seen(self, command: UserCommand) -> bool:
        client_command_id = self._client_command_id(command)
        return bool(client_command_id and client_command_id in self._recent_client_command_id_set)

    def _mark_client_command_seen(
        self,
        command: UserCommand,
        *,
        require_persistence: bool = False,
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
            if require_persistence:
                self._recent_client_command_id_set.discard(client_command_id)
                self._recent_client_command_ids.remove(client_command_id)
                raise
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
        generation = (
            self._session.connection_generation
            if connection_generation is None
            else connection_generation
        )
        with self._session.event_outbox.bind_connection_generation(generation):
          try:
            if _is_conversation_lifecycle_command(command.type):
                manager = self._session.ws_manager
                lifecycle_lock = (
                    manager.conversation_lifecycle_lock()
                    if manager is not None
                    else self._session.conversation_lifecycle_lock()
                )
                async with lifecycle_lock:
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
                            from backend.ws.command_results import emit_command_error

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
                    await self._handle_command_inner(command)
            else:
                await self._handle_command_inner(command)
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
                  from backend.ws.command_results import emit_command_error

                  await emit_command_error(self._session, command.type, exc)
              except Exception:
                  logger.error(
                      "Could not report the failure of command %s to the client",
                      command.type,
                      exc_info=True,
                  )
              return False

    async def _handle_control_response(self, command: UserCommand) -> None:
        """Resolve a control response without creating a user-visible result.

        Control responses are a low-level approval protocol.  Codex treats
        malformed or stale responses as idempotent no-ops (logged, never
        surfaced as a user-visible result); the originating control request
        (or its timeout) is the observable state transition.  Emitting a ``command.result`` for a stale response races
        ordinary control traffic such as ``ping`` and can make a client bind
        the wrong response to its request.  Keep ownership validation and the
        fail-closed resolver, but log and discard rejected responses.
        """

        request_id, payload = self._session._normalize_control_response(command.data)
        if not payload:
            logger.debug("Ignoring empty control response for %s", request_id or "<missing>")
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
                logger.debug("Ignoring OAuth control response with invalid owner: %s", request_id)
                return
            if not oauth_future.done():
                oauth_future.set_result(payload)
            return
        owner_error = self._session.approval_response_owner_error(request_id, payload)
        if owner_error:
            logger.debug("Ignoring control response with invalid owner: %s", owner_error)
            return
        if not self._session._resolve_pending_approval(request_id, payload):
            logger.debug("Ignoring stale control response for %s", request_id or "<missing>")

    async def _handle_user_message_workspace(
        self,
        requested_workspace_root: str,
        target_conversation_id: str
    ) -> tuple[bool, str]:
        """
        Handle workspace switching for user_message command.

        Returns:
            (success, updated_target_conversation_id)
        """
        from backend.services.workspace_service import (
            parse_user_message_workspace_request,
            workspace_path_needs_activation,
        )

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

        target_conversation = (
            self._session.conversation_repo.get_conversation(target_conversation_id)
            if target_conversation_id
            else self._session.active_conversation
        )
        current_workspace_root = self._session.session_lifecycle.workspace_root_for_conversation(
            target_conversation
        )
        if (
            current_workspace_root is None
            or workspace_path_needs_activation(
                requested_workspace_path,
                current_workspace_root,
            )
        ):
            activated = await self._session.activate_workspace_path(
                str(requested_workspace_path),
                announce=False,
                wait_for_initialize=True,
                error_command=None,
            )
            if not activated:
                return False, target_conversation_id

        updated_target = target_conversation_id
        if not updated_target:
            self._session._ensure_active_conversation()
            updated_target = self._session.active_conversation_id or ""

        if updated_target:
            git_branch = await asyncio.to_thread(self._session.git_branch_for, requested_workspace_path)
            await asyncio.to_thread(
                self._session.conversation_repo.update_workspace_binding,
                str(updated_target),
                workspace_root=str(requested_workspace_path),
                git_branch=git_branch,
                worktree_path="",
                git_isolated=False,
            )

        return True, updated_target

    async def _handle_user_message_permission(
        self,
        requested_permission_mode: str | None,
        target_conversation_id: str
    ) -> bool:
        """Handle permission mode update for user_message command."""
        if requested_permission_mode is None:
            return True

        from backend.config import get_config_requirements
        from backend.config_requirements import RequirementViolation

        try:
            get_config_requirements().ensure_permission_mode(requested_permission_mode)
        except RequirementViolation as exc:
            await self._session.send_event(
                AgentEvent.error(str(exc), recoverable=True, error_type="tool")
            )
            return False

        if target_conversation_id:
            self._session.conversation_repo.update_permission_mode(
                str(target_conversation_id),
                requested_permission_mode,
            )

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
        done_event = AgentEvent.done(status="failed", reason=reason)
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
                logger.debug("Ignoring OAuth control cancellation with invalid owner: %s", request_id)
                return
        else:
            owner_error = self._session.approval_response_owner_error(request_id, command.data)
            if owner_error:
                logger.debug("Ignoring control cancellation with invalid owner: %s", owner_error)
                return
        self._session._resolve_pending_approval(
            request_id,
            {
                "action": "reject",
                "guidance": "control request cancelled by client",
            },
        )

    async def _handle_command_inner(self, command: UserCommand) -> None:
        if self._session._extension_shutdown_requested and command.type not in {
            "control_response",
            "control_cancel_request",
            "interrupt",
        }:
            from backend.ws.command_results import emit_command_error

            await emit_command_error(self._session,
                command.type,
                "Session shutdown was requested by an extension; no new work is accepted.",
            )
            return

        if command.type == "user_message":
            content = str(command.data.get("content", ""))
            attachments = normalize_attachment_payloads(command.data.get("attachments", []))
            requested_conversation_id = str(
                command.data.get("conversation_id") or ""
            ).strip()
            target_conversation_id = requested_conversation_id or self._session.active_conversation_id or ""
            if requested_conversation_id:
                target = self._session.conversation_repo.get_conversation(requested_conversation_id)
                if target is None:
                    await emit_conversation_not_found(self._session, requested_conversation_id)
                    return
                target_conversation_id = target.id

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
                    await self._seal_unstarted_user_message(
                        target_conversation_id,
                        reason="permission_mode_rejected",
                        message_id=str(
                            command.data.get("assistant_message_id") or ""
                        ),
                    )
                    return

            # Handle workspace switching if requested
            requested_workspace_root = str(
                command.data.get("workspace_root") or ""
            ).strip()
            if requested_workspace_root:
                success, target_conversation_id = await self._handle_user_message_workspace(
                    requested_workspace_root,
                    target_conversation_id
                )
                if not success:
                    await self._seal_unstarted_user_message(
                        target_conversation_id,
                        reason="workspace_activation_failed",
                        message_id=str(
                            command.data.get("assistant_message_id") or ""
                        ),
                    )
                    return

            # Handle permission mode update if requested
            permission_result = await self._handle_user_message_permission(
                requested_permission_mode, target_conversation_id
            )
            if not permission_result:
                await self._seal_unstarted_user_message(
                    target_conversation_id,
                    reason="permission_mode_rejected",
                    message_id=str(
                        command.data.get("assistant_message_id") or ""
                    ),
                )
                return

            message_metadata: dict[str, Any] = {
                key: str(command.data.get(key) or "").strip()
                for key in ("primary_file", "active_tab_path")
                if str(command.data.get(key) or "").strip()
            }
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
                if value:
                    message_metadata[target_key] = value
            client_command_id = self._client_command_id(command)
            stable_command_suffix = "".join(
                character
                for character in client_command_id
                if character.isascii() and (character.isalnum() or character in {"-", "_"})
            )
            assistant_message_id = str(
                command.data.get("assistant_message_id") or ""
            ).strip() or (
                f"assistant_{stable_command_suffix}"
                if stable_command_suffix
                else f"assistant_{uuid.uuid4().hex}"
            )
            message_metadata["assistant_message_id"] = assistant_message_id
            command.data["assistant_message_id"] = assistant_message_id
            user_message_id = str(
                command.data.get("user_message_id")
                or ""
            ).strip() or (
                f"user_{stable_command_suffix}"
                if stable_command_suffix
                else f"user_{uuid.uuid4().hex}"
            )
            message_metadata["user_message_id"] = user_message_id
            command.data["user_message_id"] = user_message_id
            if client_command_id:
                message_metadata["client_command_id"] = client_command_id
                current_for_admission = (
                    self._session.conversation_repo.get_conversation(target_conversation_id)
                    if target_conversation_id
                    else None
                )
                snapshot = dict(
                    current_for_admission.context_snapshot if current_for_admission is not None else {}
                )
                admission = dict(
                    (snapshot.get("turn_admissions") or {}).get(user_message_id) or {}
                )
                existing_user = next(
                    (
                        item
                        for item in list(
                            current_for_admission.transcript if current_for_admission is not None else []
                        )
                        if isinstance(item, dict)
                        and str(item.get("id") or "").strip() == user_message_id
                        and str(item.get("role") or "").strip() == "user"
                    ),
                    None,
                )
                if existing_user is not None:
                    same_payload = (
                        str(existing_user.get("content") or "") == content
                        and list(existing_user.get("attachments") or []) == attachments
                    )
                    same_owner = (
                        str(admission.get("client_command_id") or "")
                        == client_command_id
                    )
                    if not (same_payload and same_owner):
                        conflict = AgentEvent.error(
                            "This user_message_id was already admitted with different content or ownership.",
                            recoverable=True,
                            error_type="conversation",
                            error_code="turn_admission_conflict",
                        )
                        conflict.data["conversation_id"] = target_conversation_id
                        conflict.data["user_message_id"] = user_message_id
                        await self._session.send_event(conflict)
                        return
                    message_metadata["_turn_admission_restored"] = True

            stripped = content.lstrip()
            if stripped.startswith("/") and not stripped.startswith("//"):
                if target_conversation_id:
                    await self._session._ensure_extension_commands_for_conversation(
                        target_conversation_id
                    )
                parts = stripped.split(maxsplit=1)
                cmd_name = parts[0].lower()
                cmd_arg = parts[1] if len(parts) > 1 else ""
                if self._session.command_registry.dispatch_slash_sync(
                    cmd_name,
                    scope_id=target_conversation_id,
                ):
                    handled, content_override = await self._session.command_registry.dispatch_slash(
                        self._session,
                        cmd_name,
                        cmd_arg,
                        attachments,
                        scope_id=target_conversation_id,
                    )
                    if handled:
                        return
                    content = content_override

            retry_from_message_id = str(command.data.get("retry_from_message_id", "")).strip()
            if content or attachments:
                if not target_conversation_id:
                    self._session._ensure_active_conversation()
                    target_conversation_id = self._session.active_conversation_id or ""
                    if requested_permission_mode is not None and target_conversation_id:
                        self._session.conversation_repo.update_permission_mode(
                            str(target_conversation_id),
                            requested_permission_mode,
                        )
                queued_dispatch = bool(command.data.pop("_queued_user_message_dispatch", False))
                message_metadata["_queued_user_message_dispatch"] = queued_dispatch
                if retry_from_message_id:
                    # Regeneration is one server-owned state transition, matching
                    # MiniCode thread rollback followed by turn/start. Drain the old
                    # run and discard follow-ups before rewriting the transcript;
                    # otherwise this command can be mistaken for an ordinary busy
                    # follow-up and remain queued behind the answer it replaces.
                    current = self._session.conversation_repo.get_conversation(target_conversation_id)
                    if current is None:
                        # The client is showing a spinner it can only clear on a
                        # terminal event. Returning silently strands it, so
                        # report the vanished conversation through the same
                        # not-found path every other lookup failure uses.
                        await emit_conversation_not_found(self._session, target_conversation_id)
                        return
                    retry_exists = any(
                        str(message.get("id") or "").strip() == retry_from_message_id
                        and str(message.get("role") or "").strip() == "user"
                        for message in list(current.transcript or [])
                        if isinstance(message, dict)
                    )
                    if not retry_exists:
                        error_event = AgentEvent.error(
                            f"Cannot regenerate from message '{retry_from_message_id}'",
                            # Terminal for this request: no run starts, so no
                            # `done` follows. A recoverable error is treated by
                            # the client as non-terminal evidence, which left
                            # the conversation permanently "running" and
                            # blocked every later send until a reload.
                            recoverable=False,
                            error_type="tool",
                        )
                        error_event.data["conversation_id"] = target_conversation_id
                        await self._session.send_event(error_event)
                        return
                    self._session.run_manager.clear_user_message_queue(target_conversation_id)
                    if self._session.running_agent_task_for(target_conversation_id):
                        await self._session.cancel_agent_runs(
                            conversation_id=target_conversation_id,
                            reason="user_regenerated",
                        )
                    current = self._session.conversation_repo.get_conversation(target_conversation_id)
                    if current is None:
                        # Deleted while the old run was being drained. The
                        # cancel above emits no client-visible terminal for the
                        # regenerate request itself, so close it here too.
                        await emit_conversation_not_found(self._session, target_conversation_id)
                        return
                    prepared = self._session._prepare_retry_from_message(
                        conversation=current,
                        retry_from_message_id=retry_from_message_id,
                    )
                    if prepared is None:
                        error_event = AgentEvent.error(
                            f"Cannot regenerate from message '{retry_from_message_id}'",
                            # Terminal for this request: no run starts, so no
                            # `done` follows. A recoverable error is treated by
                            # the client as non-terminal evidence, which left
                            # the conversation permanently "running" and
                            # blocked every later send until a reload.
                            recoverable=False,
                            error_type="tool",
                        )
                        error_event.data["conversation_id"] = target_conversation_id
                        await self._session.send_event(error_event)
                        return
                running_for_target = self._session.running_agent_task_for(target_conversation_id)
                if (running_for_target or self._session.run_manager.is_queue_dispatching(target_conversation_id)) and not queued_dispatch:
                    queued_command = UserCommand(
                        type="user_message",
                        data={
                            **command.data,
                            "content": content,
                            "conversation_id": target_conversation_id,
                            **({"assistant_message_id": assistant_message_id} if assistant_message_id else {}),
                        },
                    )
                    streaming_behavior = str(
                        command.data.get("streaming_behavior")
                        or command.data.get("streamingBehavior")
                        or ""
                    ).strip().lower()
                    if streaming_behavior == "steer" and running_for_target is not None:
                        stream_state = self._session._conversation_streams.get(target_conversation_id) or {}
                        target_message_id = str(stream_state.get("message_id") or "").strip()
                        steered = self._session.run_manager.enqueue_user_message_as_steer(
                            target_conversation_id,
                            queued_command,
                            target_message_id=target_message_id,
                        )
                        if steered is not None:
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
                    position = self._session.run_manager.enqueue_user_message(target_conversation_id, queued_command)
                    await self._session.send_event(
                        AgentEvent.user_message_queue_updated(
                            status="queued",
                            conversation_id=target_conversation_id,
                            message_id=assistant_message_id,
                            user_message_id=str(command.data.get("user_message_id") or ""),
                            position=position,
                        )
                    )
                    return

                await self._session.start_agent_run(
                    content,
                    attachments=attachments,
                    conversation_id=target_conversation_id,
                    metadata=message_metadata,
                )
            return

        if command.type == "control_response":
            await self._handle_control_response(command)
            return

        if command.type == "control_cancel_request":
            await self._handle_control_cancel(command)
            return

        handled = await self._session.command_registry.dispatch(command.type, command.data)
        if not handled:
            from backend.ws.command_results import emit_command_error
            await emit_command_error(self._session, command.type, f"Unsupported command '{command.type}'")
