from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path
from typing import Any

from backend.agent.turn_input import TurnInput, TurnInputQueue
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    await_with_deadline,
    cancel_and_drain,
)
from backend.conversations.repository import CONVERSATION_DATA_DIR
from backend.ws.durable_user_queue import DurableUserMessageQueue

logger = logging.getLogger(__name__)
MAX_QUEUED_USER_MESSAGES_PER_CONVERSATION = 20


class SessionRunManager:
    """Owns per-session agent run bookkeeping.

    WebSocketSession keeps the legacy attributes for compatibility with older
    handlers/tests, but all mutation should flow through this manager. That gives
    us one place to evolve run-tree cancellation and durable event correlation.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        session_id = str(getattr(session, "session_id", "") or "").strip()
        queue_store = (
            DurableUserMessageQueue(
                session_id=session_id,
                root_dir=Path(CONVERSATION_DATA_DIR).parent / "user-message-queue",
            )
            if session_id
            else None
        )
        self._durable_queue = queue_store
        loaded_queues, loaded_inflight = queue_store.load() if queue_store is not None else ({}, {})
        self._user_message_queues: dict[str, deque[Any]] = {
            conversation_id: deque(commands)
            for conversation_id, commands in loaded_queues.items()
            if commands
        }
        self._inflight_user_messages: dict[str, Any] = dict(loaded_inflight)
        self._durable_turn_inputs: dict[str, list[Any]] = {}
        self._queue_dispatching: set[str] = set()
        self._queue_steering: set[str] = set()
        self._turn_input_queues: dict[str, TurnInputQueue] = {}
        self._terminal_statuses: dict[str, str] = {}
        self._delivery_complete: set[str] = set()

    @property
    def durable_queue(self) -> DurableUserMessageQueue | None:
        return self._durable_queue

    def _persist_user_queues(self) -> None:
        if self._durable_queue is None:
            return
        self._durable_queue.save(
            {conversation_id: list(commands) for conversation_id, commands in self._user_message_queues.items()},
            self._inflight_user_messages,
            self._durable_turn_inputs,
        )

    def mark_terminal_status(self, conversation_id: str, status: str) -> None:
        conversation_id = str(conversation_id or "").strip()
        status = str(status or "").strip()
        if conversation_id and status:
            self._terminal_statuses[conversation_id] = status

    def mark_delivery_complete(self, conversation_id: str) -> None:
        conversation_id = str(conversation_id or "").strip()
        if conversation_id:
            self._delivery_complete.add(conversation_id)

    def is_delivery_complete(self, conversation_id: str) -> bool:
        return str(conversation_id or "").strip() in self._delivery_complete

    def enqueue_user_message(self, conversation_id: str, command: Any) -> int:
        queue = self._user_message_queues.setdefault(conversation_id, deque())
        if len(queue) >= MAX_QUEUED_USER_MESSAGES_PER_CONVERSATION:
            return 0
        queue.append(command)
        self._persist_user_queues()
        return len(queue)

    def dequeue_user_message(self, conversation_id: str) -> Any | None:
        queue = self._user_message_queues.get(conversation_id)
        if not queue:
            return None
        command = queue.popleft()
        self._inflight_user_messages[conversation_id] = command
        if not queue:
            self._user_message_queues.pop(conversation_id, None)
        self._persist_user_queues()
        return command

    def finish_user_message_dispatch(self, conversation_id: str, command: Any, *, succeeded: bool) -> None:
        inflight = self._inflight_user_messages.get(conversation_id)
        if inflight is not command:
            return
        self._inflight_user_messages.pop(conversation_id, None)
        if not succeeded:
            self._user_message_queues.setdefault(conversation_id, deque()).appendleft(command)
        self._persist_user_queues()

    def remove_queued_user_message(self, conversation_id: str, message_id: str) -> bool:
        queue = self._user_message_queues.get(conversation_id)
        if not queue:
            return False
        kept = deque(
            command
            for command in queue
            if str(getattr(command, "data", {}).get("assistant_message_id") or "").strip() != message_id
        )
        removed = len(kept) != len(queue)
        if kept:
            self._user_message_queues[conversation_id] = kept
        else:
            self._user_message_queues.pop(conversation_id, None)
        self._persist_user_queues()
        return removed

    def pop_queued_user_message(self, conversation_id: str, message_id: str) -> Any | None:
        """Remove and return one queued prompt without disturbing FIFO order."""
        queue = self._user_message_queues.get(conversation_id)
        if not queue:
            return None
        commands = list(queue)
        index = next(
            (
                item_index
                for item_index, command in enumerate(commands)
                if str(getattr(command, "data", {}).get("assistant_message_id") or "").strip() == message_id
            ),
            -1,
        )
        if index < 0:
            return None
        command = commands.pop(index)
        if commands:
            self._user_message_queues[conversation_id] = deque(commands)
        else:
            self._user_message_queues.pop(conversation_id, None)
        self._durable_turn_inputs.setdefault(conversation_id, []).append(command)
        self._persist_user_queues()
        return command

    def queued_user_messages(self, conversation_id: str) -> list[Any]:
        return list(self._user_message_queues.get(conversation_id) or ())

    def queued_user_message_snapshot(self, conversation_id: str = "") -> list[dict[str, Any]]:
        """Return replay-safe metadata for queued follow-ups without internals."""
        target = str(conversation_id or "").strip()
        result: list[dict[str, Any]] = []
        for cid, commands in self._user_message_queues.items():
            if target and cid != target:
                continue
            for position, command in enumerate(commands, 1):
                data = dict(getattr(command, "data", {}) or {})
                result.append({
                    "conversation_id": cid,
                    "message_id": str(data.get("assistant_message_id") or ""),
                    "user_message_id": str(data.get("user_message_id") or ""),
                    "content": str(data.get("content") or ""),
                    "position": position,
                })
        return result

    def replace_user_message_queue(self, conversation_id: str, commands: list[Any]) -> None:
        if commands:
            self._user_message_queues[conversation_id] = deque(commands)
        else:
            self._user_message_queues.pop(conversation_id, None)
        self._persist_user_queues()

    def _discard_durable_turn_input(self, conversation_id: str, command: Any) -> bool:
        pending = self._durable_turn_inputs.get(conversation_id)
        if not pending:
            return False
        index = next(
            (item_index for item_index, item in enumerate(pending) if item is command),
            -1,
        )
        if index < 0:
            message_id = str(
                getattr(command, "data", {}).get("assistant_message_id") or ""
            ).strip()
            index = next(
                (
                    item_index
                    for item_index, item in enumerate(pending)
                    if message_id
                    and str(
                        getattr(item, "data", {}).get("assistant_message_id") or ""
                    ).strip()
                    == message_id
                ),
                -1,
            )
        if index < 0:
            return False
        pending.pop(index)
        if not pending:
            self._durable_turn_inputs.pop(conversation_id, None)
        return True

    def acknowledge_turn_input(self, conversation_id: str, command: Any) -> bool:
        """Acknowledge one promoted steer after it entered model context."""
        if not self._discard_durable_turn_input(conversation_id, command):
            return False
        self._persist_user_queues()
        return True

    def restore_turn_input_as_follow_up(
        self,
        conversation_id: str,
        command: Any,
    ) -> list[Any]:
        """Atomically return a failed steer promotion to the FIFO queue."""
        queue = [command, *self.queued_user_messages(conversation_id)]
        self._user_message_queues[conversation_id] = deque(queue)
        self._discard_durable_turn_input(conversation_id, command)
        self._persist_user_queues()
        return queue

    def turn_input_queue(self, conversation_id: str) -> TurnInputQueue:
        queue = self._turn_input_queues.get(conversation_id)
        if queue is None or queue.sealed:
            queue = TurnInputQueue()
            self._turn_input_queues[conversation_id] = queue
        return queue

    def enqueue_turn_steer(
        self,
        conversation_id: str,
        command: Any,
        *,
        target_message_id: str = "",
    ) -> TurnInput | None:
        queue = self._turn_input_queues.get(conversation_id)
        if queue is None or queue.sealed:
            return None
        return queue.enqueue_command(
            command,
            mode="steer",
            target_message_id=target_message_id,
        )

    def pending_turn_input_snapshot(self) -> list[dict[str, Any]]:
        """Return non-destructive turn-local input state for session restore."""
        result: list[dict[str, Any]] = []
        for conversation_id, queue in self._turn_input_queues.items():
            if queue.sealed:
                continue
            for position, item in enumerate(queue.snapshot(), 1):
                result.append({
                    "conversation_id": conversation_id,
                    "mode": item.mode,
                    "message_id": item.message_id,
                    "user_message_id": item.user_message_id,
                    "target_message_id": item.target_message_id,
                    "content": item.content,
                    "attachments": [dict(attachment) for attachment in item.attachments],
                    "position": position,
                    "queued_at_ms": item.queued_at_ms,
                })
        return result

    def turn_execution_snapshot(self, conversation_id: str = "") -> list[dict[str, Any]]:
        """Expose phase ownership for restore diagnostics without queue internals."""
        target = str(conversation_id or "").strip()
        snapshots: list[dict[str, Any]] = []
        for current_id, queue in self._turn_input_queues.items():
            if target and current_id != target:
                continue
            snapshot = queue.phase_snapshot()
            snapshots.append({"conversation_id": current_id, **snapshot})
        return snapshots

    def _seal_turn_input_queue(self, conversation_id: str) -> None:
        queue = self._turn_input_queues.pop(conversation_id, None)
        unconsumed = queue.seal_and_drain_commands() if queue is not None else []
        durable_pending = self._durable_turn_inputs.pop(conversation_id, [])
        restored = list(durable_pending)
        restored.extend(
            command
            for command in unconsumed
            if not any(command is pending for pending in durable_pending)
        )
        if not restored:
            return
        existing = list(self._user_message_queues.get(conversation_id) or ())
        self._user_message_queues[conversation_id] = deque([*restored, *existing])
        self._persist_user_queues()

    def promote_queued_user_message(self, conversation_id: str, message_id: str) -> list[Any] | None:
        """Move one queued prompt to the front and return the new queue order."""
        queue = self._user_message_queues.get(conversation_id)
        if not queue:
            return None
        commands = list(queue)
        index = next(
            (
                item_index
                for item_index, command in enumerate(commands)
                if str(getattr(command, "data", {}).get("assistant_message_id") or "").strip() == message_id
            ),
            -1,
        )
        if index < 0:
            return None
        command = commands.pop(index)
        commands.insert(0, command)
        self._user_message_queues[conversation_id] = deque(commands)
        self._persist_user_queues()
        return commands

    def clear_user_message_queue(self, conversation_id: str) -> None:
        self._user_message_queues.pop(conversation_id, None)
        self._inflight_user_messages.pop(conversation_id, None)
        self._queue_dispatching.discard(conversation_id)
        self._queue_steering.discard(conversation_id)
        self._durable_turn_inputs.pop(conversation_id, None)
        queue = self._turn_input_queues.pop(conversation_id, None)
        if queue is not None:
            queue.seal_and_drain_commands()
        self._persist_user_queues()

    def clear_all_user_message_queues(self) -> None:
        self._user_message_queues.clear()
        self._queue_dispatching.clear()
        self._queue_steering.clear()
        for queue in self._turn_input_queues.values():
            queue.seal_and_drain_commands()
        self._turn_input_queues.clear()
        self._durable_turn_inputs.clear()
        self._inflight_user_messages.clear()
        self._persist_user_queues()

    def begin_queue_steering(self, conversation_id: str) -> bool:
        """Pause automatic dequeue while one queued prompt is being promoted."""
        if not conversation_id or conversation_id in self._queue_steering:
            return False
        self._queue_steering.add(conversation_id)
        return True

    def finish_queue_steering(self, conversation_id: str) -> None:
        self._queue_steering.discard(conversation_id)

    def is_queue_steering(self, conversation_id: str) -> bool:
        return conversation_id in self._queue_steering

    def is_queue_dispatching(self, conversation_id: str) -> bool:
        return conversation_id in self._queue_dispatching

    def begin_queue_dispatch(self, conversation_id: str) -> bool:
        if conversation_id in self._queue_dispatching:
            return False
        self._queue_dispatching.add(conversation_id)
        return True

    def finish_queue_dispatch(self, conversation_id: str) -> None:
        self._queue_dispatching.discard(conversation_id)

    @property
    def run_tasks(self) -> dict[str, asyncio.Task[Any]]:
        return getattr(self._session, "_conversation_run_tasks")

    @property
    def run_task_ids(self) -> dict[str, str]:
        return getattr(self._session, "_conversation_run_task_ids")

    @property
    def cancel_events(self) -> dict[str, asyncio.Event]:
        return getattr(self._session, "_conversation_run_cancel_events")

    @property
    def locks(self) -> dict[str, asyncio.Lock]:
        return getattr(self._session, "_conversation_run_locks")

    def has_active_run(self) -> bool:
        active = getattr(self._session, "_active_run_task", None)
        if active is not None and not active.done():
            return True
        return bool(self.run_tasks)

    def running_task_for(self, conversation_id: str) -> asyncio.Task[Any] | None:
        if conversation_id in self._delivery_complete and not self._user_message_queues.get(conversation_id):
            return None
        task = self.run_tasks.get(conversation_id)
        # Registration, not Task.done(), owns the conversation until cleanup.
        # This keeps a just-cancelled run from letting a new message jump ahead
        # of follow-ups that are already queued.
        return task

    def register(
        self,
        *,
        conversation_id: str,
        task: asyncio.Task[Any],
        task_id: str,
        cancel_event: asyncio.Event,
        active_conversation_id: str | None,
    ) -> None:
        if conversation_id:
            self._delivery_complete.discard(conversation_id)
            self.run_tasks[conversation_id] = task
            self.run_task_ids[conversation_id] = task_id
            self.cancel_events[conversation_id] = cancel_event
        if conversation_id == active_conversation_id:
            self._session._active_run_task = task
            self._session._active_task_id = task_id
            self._session._active_run_cancel_event = cancel_event
        self._session._schedule_task_runtime_update()

    def cleanup(
        self,
        *,
        conversation_id: str,
        task: asyncio.Task[Any],
        task_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        registered_task = self.run_tasks.get(conversation_id) if conversation_id else None
        newer_run_registered = registered_task is not None and registered_task is not task
        if conversation_id and registered_task is task:
            self.run_tasks.pop(conversation_id, None)
        if conversation_id and self.run_task_ids.get(conversation_id) == task_id:
            self.run_task_ids.pop(conversation_id, None)
        if conversation_id and self.cancel_events.get(conversation_id) is cancel_event:
            self.cancel_events.pop(conversation_id, None)
        if conversation_id and not newer_run_registered:
            self.locks.pop(conversation_id, None)
        if getattr(self._session, "_active_run_task", None) is task:
            self._session._active_run_task = None
        if getattr(self._session, "_active_task_id", None) == task_id:
            self._session._active_task_id = None
        if getattr(self._session, "_active_run_cancel_event", None) is cancel_event:
            self._session._active_run_cancel_event = None
        if not newer_run_registered:
            self._seal_turn_input_queue(conversation_id)
            self._terminal_statuses.pop(conversation_id, "")
            # Keep the terminal-delivery fence until the next run registers.
            # Interrupt handling may finish after task cleanup and still needs
            # to know that DONE was already emitted.
        # The canonical agent.run.completed event is emitted by the agent loop.
        # Cleanup only releases manager bookkeeping; recording another terminal
        # event here produced a second, run-id-less completion after DONE.
        self._session._schedule_task_runtime_update()

    async def cancel(
        self,
        *,
        conversation_id: str | None = None,
        reason: str = "run_cancelled",
    ) -> bool:
        target_conversation_id = str(conversation_id or "").strip()
        target_ids = [target_conversation_id] if target_conversation_id else list(self.run_tasks.keys())

        cancelled_any = False
        seen_tasks: set[asyncio.Task[Any]] = set()
        seen_task_ids: set[str] = set()
        tasks_to_wait: set[asyncio.Task[Any]] = set()

        for cid in target_ids:
            task_id = self.run_task_ids.get(cid)
            if task_id:
                seen_task_ids.add(str(task_id))
                self._cancel_run_tree(str(task_id), reason=reason)

            cancel_event = self.cancel_events.get(cid)
            if isinstance(cancel_event, asyncio.Event):
                cancel_event.set()
            if cid:
                self.mark_terminal_status(cid, "cancelled")

            task = self.run_tasks.get(cid)
            if task is not None:
                seen_tasks.add(task)
                tasks_to_wait.add(task)
                cancelled_any = True
                if not task.done():
                    task.cancel()

        active_task = getattr(self._session, "_active_run_task", None)
        active_task_id = getattr(self._session, "_active_task_id", None)
        active_cancel_event = getattr(self._session, "_active_run_cancel_event", None)

        if not target_conversation_id:
            if isinstance(active_cancel_event, asyncio.Event):
                active_cancel_event.set()
            if active_task is not None and active_task not in seen_tasks and not active_task.done():
                active_task.cancel()
                tasks_to_wait.add(active_task)
                cancelled_any = True
            if active_task_id and active_task_id not in seen_task_ids:
                self._cancel_run_tree(str(active_task_id), reason=reason)
            self._session._active_run_task = None
            self._session._active_task_id = None
            self._session._active_run_cancel_event = None
        elif active_task in seen_tasks:
            self._session._active_run_task = None
            self._session._active_task_id = None
            self._session._active_run_cancel_event = None

        await await_with_deadline(
            self._session._cancel_pending_approvals(
                reason=reason,
                conversation_id=target_conversation_id or None,
            ),
            timeout=0.25,
            label="pending approval cancellation",
        )
        current = asyncio.current_task()
        waitable = [task for task in tasks_to_wait if task is not current and not task.done()]
        if waitable:
            await cancel_and_drain(
                waitable,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="agent run cancellation",
            )
        self._session._schedule_task_runtime_update()
        return cancelled_any

    def _cancel_run_tree(self, task_id: str, *, reason: str) -> None:
        try:
            self._session._cancel_child_subagents_for_task_id(task_id, reason=reason)
            self._session.task_manager.cancel(task_id)
        except Exception:
            logger.debug(
                "Failed to cancel run tree task_id=%s session=%s",
                task_id,
                getattr(self._session, "session_id", ""),
                exc_info=True,
            )
