from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

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
        self._user_message_queues: dict[str, deque[Any]] = {}
        self._queue_dispatching: set[str] = set()
        self._queue_steering: set[str] = set()
        self._terminal_statuses: dict[str, str] = {}
        self._delivery_complete: set[str] = set()

    def mark_terminal_status(self, conversation_id: str, status: str) -> None:
        conversation_id = str(conversation_id or "").strip()
        status = str(status or "").strip()
        if conversation_id and status:
            self._terminal_statuses[conversation_id] = status

    def mark_delivery_complete(self, conversation_id: str) -> None:
        conversation_id = str(conversation_id or "").strip()
        if conversation_id:
            self._delivery_complete.add(conversation_id)

    def enqueue_user_message(self, conversation_id: str, command: Any) -> int:
        queue = self._user_message_queues.setdefault(conversation_id, deque())
        if len(queue) >= MAX_QUEUED_USER_MESSAGES_PER_CONVERSATION:
            return 0
        queue.append(command)
        return len(queue)

    def dequeue_user_message(self, conversation_id: str) -> Any | None:
        queue = self._user_message_queues.get(conversation_id)
        if not queue:
            return None
        command = queue.popleft()
        if not queue:
            self._user_message_queues.pop(conversation_id, None)
        return command

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
        return removed

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
        return commands

    def clear_user_message_queue(self, conversation_id: str) -> None:
        self._user_message_queues.pop(conversation_id, None)
        self._queue_dispatching.discard(conversation_id)
        self._queue_steering.discard(conversation_id)

    def clear_all_user_message_queues(self) -> None:
        self._user_message_queues.clear()
        self._queue_dispatching.clear()
        self._queue_steering.clear()

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
        self._record_run_lifecycle_event(
            "agent.run.started",
            conversation_id=conversation_id,
            task_id=task_id,
            status="running",
            reason="registered",
        )
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
            self._terminal_statuses.pop(conversation_id, "")
            self._delivery_complete.discard(conversation_id)
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

        for cid in target_ids:
            task_id = self.run_task_ids.get(cid)
            if task_id:
                seen_task_ids.add(str(task_id))
                self._record_run_lifecycle_event(
                    "agent.run.updated",
                    conversation_id=cid,
                    task_id=str(task_id),
                    status="cancelling",
                    reason=reason,
                )
                self._cancel_run_tree(str(task_id), reason=reason)

            cancel_event = self.cancel_events.get(cid)
            if isinstance(cancel_event, asyncio.Event):
                cancel_event.set()
            if cid:
                self.mark_terminal_status(cid, "cancelled")

            task = self.run_tasks.get(cid)
            if task is not None:
                seen_tasks.add(task)
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
                cancelled_any = True
            if active_task_id and active_task_id not in seen_task_ids:
                self._record_run_lifecycle_event(
                    "agent.run.updated",
                    conversation_id=target_conversation_id,
                    task_id=str(active_task_id),
                    status="cancelling",
                    reason=reason,
                )
                self._cancel_run_tree(str(active_task_id), reason=reason)
            self._session._active_run_task = None
            self._session._active_task_id = None
            self._session._active_run_cancel_event = None
        elif active_task in seen_tasks:
            self._session._active_run_task = None
            self._session._active_task_id = None
            self._session._active_run_cancel_event = None

        await self._session._cancel_pending_approvals(
            reason=reason,
            conversation_id=target_conversation_id or None,
        )
        self._session._schedule_task_runtime_update()
        return cancelled_any

    def _record_run_lifecycle_event(
        self,
        event_type: str,
        *,
        conversation_id: str,
        task_id: str,
        status: str,
        reason: str,
    ) -> None:
        conversation_id = str(conversation_id or "").strip()
        task_id = str(task_id or "").strip()
        if not conversation_id:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "status": status,
            "reason": reason,
            "source": "run_manager",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        try:
            envelope = self._session._envelope_ws_payload(payload)
            if self._session._is_replayable_ws_payload(envelope):
                self._session._record_ws_event(envelope)
        except Exception:
            logger.debug(
                "Failed to record run lifecycle event type=%s conversation=%s session=%s",
                event_type,
                conversation_id,
                getattr(self._session, "session_id", ""),
                exc_info=True,
            )

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
