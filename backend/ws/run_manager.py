from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path
from typing import Any

from backend.agent.parent_notification_outbox import (
    ParentNotification,
    claim_parent_notification_wake,
    load_parent_outbox,
    release_parent_notification_wake,
    subscribe_parent_notification_enqueued,
)
from backend.agent.message import UserCommand
from backend.agent.turn_input import TurnInput, TurnInputQueue
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    await_with_deadline,
    cancel_and_drain,
    cancel_and_drain_receipt,
    cancel_and_drain_to_completion,
)
from backend.conversations.repository import CONVERSATION_DATA_DIR
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.turn_wait_state import TurnWaitState

logger = logging.getLogger(__name__)


class SessionRunManager:
    """Owns per-session agent run bookkeeping.

    Run handles, cancellation signals, queue state, and delivery fences belong
    to this manager.  The websocket session only exposes the composition owner
    and never stores a second copy of the run registries.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        wait_state = TurnWaitState.for_session(session)
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._run_task_ids: dict[str, str] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._active_run_task: asyncio.Task[Any] | None = None
        self._active_task_id: str | None = None
        self._active_run_cancel_event: asyncio.Event | None = None
        session_id = str(getattr(session, "session_id", "") or "").strip()
        # The session repository owns the active conversation storage root.
        # Derive the durable queue beside that repository so a session cannot
        # load commands from a different runtime or test data root.
        conversation_repo = getattr(session, "conversation_repo", None)
        conversation_dir = getattr(conversation_repo, "_base_dir", None)
        queue_root = Path(conversation_dir or CONVERSATION_DATA_DIR).parent / "user-message-queue"
        queue_store = (
            DurableUserMessageQueue(session_id=session_id, root_dir=queue_root)
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
        self._queue_owned_runs: dict[str, str] = {}
        self._queue_owned_run_releases: dict[str, asyncio.Future[bool]] = {}
        self._queue_steering: set[str] = set()
        # TurnWaitState is the owner of turn-local interaction state.  The
        # manager provides durable queue operations, but it must not create a
        # second queue registry beside the session's wait state.
        self._turn_input_queues: dict[str, TurnInputQueue] = wait_state.turn_input_queues
        self._terminal_statuses: dict[str, str] = {}
        self._delivery_complete: set[tuple[str, str]] = set()
        self._watched_notification_conversations: set[str] = set()
        self._notification_request_generation: dict[str, int] = {}
        self._notification_attempt_generation: dict[str, int] = {}
        self._notification_wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._retired_notification_wake_tasks: set[asyncio.Task[Any]] = set()
        self._notification_run_task_ids: dict[str, str] = {}
        self._notification_wakes_closed = False
        self._notification_wake_owner_token = f"{session_id}:{id(self)}"
        try:
            self._notification_loop: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            self._notification_loop = None
        self._unsubscribe_parent_notifications = (
            subscribe_parent_notification_enqueued(
                self._on_parent_notification_enqueued
            )
        )

    @property
    def durable_queue(self) -> DurableUserMessageQueue | None:
        return self._durable_queue

    def close_durable_queue(self) -> None:
        queue = self._durable_queue
        if queue is None:
            return
        queue.close()

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

    @staticmethod
    def _delivery_key(conversation_id: str, run_id: str = "") -> tuple[str, str]:
        return (
            str(conversation_id or "").strip(),
            str(run_id or "").strip(),
        )

    def mark_delivery_complete(self, conversation_id: str, run_id: str = "") -> None:
        conversation_id = str(conversation_id or "").strip()
        if conversation_id:
            self._delivery_complete.add(self._delivery_key(conversation_id, run_id))

    def is_delivery_complete(self, conversation_id: str, run_id: str = "") -> bool:
        conversation_id, run_id = self._delivery_key(conversation_id, run_id)
        if not conversation_id:
            return False
        if run_id:
            # New runners fence terminal delivery to the concrete managed-task
            # id.  Legacy/integration runners can only publish the
            # conversation-scoped marker; registration clears every older
            # marker for the conversation, so that marker can only belong to
            # the currently registered run and is safe to accept here.
            return (
                (conversation_id, run_id) in self._delivery_complete
                or (conversation_id, "") in self._delivery_complete
            )
        # Conversation-level callers are asking whether the current/most
        # recent run crossed its terminal delivery fence.  A run-scoped marker
        # must therefore be visible to them as well; otherwise cleanup and
        # reconnect code incorrectly keep the conversation busy after DONE.
        return any(
            owner == conversation_id
            for owner, _owned_run_id in self._delivery_complete
        )

    def enqueue_user_message(self, conversation_id: str, command: Any) -> int:
        queue = self._user_message_queues.setdefault(conversation_id, deque())
        queue.append(command)
        self._persist_user_queues()
        return len(queue)

    def dequeue_user_message(self, conversation_id: str) -> Any | None:
        if self._durable_queue is not None:
            local_commands = list(self._user_message_queues.get(conversation_id) or ())
            claimed = self._durable_queue.claim_user_message(conversation_id)
            remaining = self._durable_queue.pending_user_messages(conversation_id)
            command = next(
                (candidate for candidate in local_commands if candidate == claimed),
                claimed,
            )
            remaining = [
                next(
                    (candidate for candidate in local_commands if candidate == persisted),
                    persisted,
                )
                for persisted in remaining
            ]
            if remaining:
                self._user_message_queues[conversation_id] = deque(remaining)
            else:
                self._user_message_queues.pop(conversation_id, None)
            if command is None:
                return None
            self._inflight_user_messages[conversation_id] = command
            return command
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
        if self._durable_queue is not None:
            if not isinstance(command, UserCommand):
                return
            local_commands = list(self._user_message_queues.get(conversation_id) or ())
            if not self._durable_queue.settle_user_message(
                conversation_id,
                command,
                succeeded=succeeded,
            ):
                return
            self._inflight_user_messages.pop(conversation_id, None)
            remaining = self._durable_queue.pending_user_messages(conversation_id)
            remaining = [
                next(
                    (candidate for candidate in local_commands if candidate == persisted),
                    persisted,
                )
                for persisted in remaining
            ]
            if remaining:
                self._user_message_queues[conversation_id] = deque(remaining)
            else:
                self._user_message_queues.pop(conversation_id, None)
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
        self.watch_conversation_notifications(conversation_id)
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

    def enqueue_user_message_as_steer(
        self,
        conversation_id: str,
        command: Any,
        *,
        target_message_id: str = "",
    ) -> TurnInput | None:
        """Atomically accept a newly arrived prompt into the active turn.

        This is the direct-prompt counterpart to promoting an item that is
        already in the follow-up queue. The original command is made durable
        only after the turn-local queue accepts it, so callers can safely fall
        back to normal FIFO enqueue when the current turn has crossed its final
        boundary.
        """
        queue = self._turn_input_queues.get(conversation_id)
        if queue is None or queue.sealed:
            return None
        item = queue.enqueue_command(
            command,
            mode="steer",
            target_message_id=target_message_id,
        )
        if item is None:
            return None
        self._durable_turn_inputs.setdefault(conversation_id, []).append(command)
        self._persist_user_queues()
        return item

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

    def forget_conversation(self, conversation_id: str) -> None:
        """Drop all stopped runtime bookkeeping for a deleted conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return
        self.clear_user_message_queue(owner)
        self._delivery_complete = {
            key for key in self._delivery_complete if key[0] != owner
        }
        self._terminal_statuses.pop(owner, None)
        self.run_tasks.pop(owner, None)
        self.run_task_ids.pop(owner, None)
        self.cancel_events.pop(owner, None)
        self._watched_notification_conversations.discard(owner)
        self._notification_request_generation.pop(owner, None)
        self._notification_attempt_generation.pop(owner, None)
        self._notification_run_task_ids.pop(owner, None)
        release_parent_notification_wake(
            owner,
            self._notification_wake_owner_token,
        )
        wake_task = self._notification_wake_tasks.pop(owner, None)
        if wake_task is not None and not wake_task.done():
            wake_task.cancel()

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

    def has_inflight_user_message(self, conversation_id: str) -> bool:
        return conversation_id in self._inflight_user_messages

    def mark_queue_owned_run(self, conversation_id: str, task_id: str) -> None:
        owner = str(conversation_id or "").strip()
        run_id = str(task_id or "").strip()
        if not owner or not run_id:
            raise ValueError("queue-owned runs require conversation_id and task_id")
        existing = self._queue_owned_runs.get(owner)
        if existing and existing != run_id:
            raise RuntimeError(
                f"Conversation {owner} already has queue-owned run {existing}"
            )
        if existing == run_id:
            return
        self._queue_owned_runs[owner] = run_id
        self._queue_owned_run_releases[run_id] = (
            asyncio.get_running_loop().create_future()
        )

    def is_queue_owned_run(self, task_id: str) -> bool:
        return str(task_id or "").strip() in self._queue_owned_run_releases

    def finish_queue_owned_run(self, task_id: str, *, succeeded: bool) -> None:
        run_id = str(task_id or "").strip()
        release = self._queue_owned_run_releases.get(run_id)
        if release is None:
            return
        if not release.done():
            release.set_result(bool(succeeded))

    async def wait_for_queue_owned_run(self, conversation_id: str) -> bool:
        owner = str(conversation_id or "").strip()
        run_id = self._queue_owned_runs.get(owner)
        if not run_id:
            return True
        release = self._queue_owned_run_releases.get(run_id)
        if release is None:
            raise RuntimeError(
                f"Queue-owned run {run_id} has no release signal"
            )
        succeeded = await asyncio.shield(release)
        if self._queue_owned_runs.get(owner) == run_id:
            self._queue_owned_runs.pop(owner, None)
        self._queue_owned_run_releases.pop(run_id, None)
        return bool(succeeded)

    def begin_queue_dispatch(self, conversation_id: str) -> bool:
        if conversation_id in self._queue_dispatching:
            return False
        self._queue_dispatching.add(conversation_id)
        return True

    def finish_queue_dispatch(self, conversation_id: str) -> None:
        self._queue_dispatching.discard(conversation_id)

    @property
    def run_tasks(self) -> dict[str, asyncio.Task[Any]]:
        return self._run_tasks

    @property
    def run_task_ids(self) -> dict[str, str]:
        return self._run_task_ids

    @property
    def cancel_events(self) -> dict[str, asyncio.Event]:
        return self._cancel_events

    @property
    def active_run_task(self) -> asyncio.Task[Any] | None:
        return self._active_run_task

    @property
    def active_task_id(self) -> str | None:
        return self._active_task_id

    @property
    def active_run_cancel_event(self) -> asyncio.Event | None:
        return self._active_run_cancel_event

    def has_active_run(self) -> bool:
        return bool(self.run_tasks)

    def running_task_for(self, conversation_id: str) -> asyncio.Task[Any] | None:
        if self.is_delivery_complete(conversation_id) and not self._user_message_queues.get(
            conversation_id
        ):
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
            # One conversation owns at most one live run. Overwriting the three
            # handles while the previous task is unfinished orphaned it: cancel()
            # and _cancel_run_tree resolve a conversation through these dicts
            # only, so Stop and interrupt could no longer reach the first run
            # while both loops streamed into the same conversation. The busy /
            # queue check upstream does not cover every entrypoint (slash
            # dispatch runs before it), so the ownership boundary refuses here.
            previous = self.run_tasks.get(conversation_id)
            if previous is not None and previous is not task and not previous.done():
                raise RuntimeError(
                    "This conversation already has a live agent run "
                    f"({self.run_task_ids.get(conversation_id) or 'unknown'}); "
                    "queue the message or cancel the active run first."
                )
            self.watch_conversation_notifications(conversation_id)
            # A new concrete run starts a fresh delivery fence. Retain the
            # previous marker after cleanup for reconnect diagnostics, but do
            # not let it satisfy this conversation's new run.
            self._delivery_complete = {
                key for key in self._delivery_complete if key[0] != conversation_id
            }
            self.run_tasks[conversation_id] = task
            self.run_task_ids[conversation_id] = task_id
            self.cancel_events[conversation_id] = cancel_event
        if conversation_id == active_conversation_id:
            self._active_run_task = task
            self._active_task_id = task_id
            self._active_run_cancel_event = cancel_event
        self._session.session_lifecycle.schedule_task_runtime_update()

    def watch_conversation_notifications(self, conversation_id: str) -> None:
        """Bind one conversation to this session's CC-style queue subscriber."""

        owner = str(conversation_id or "").strip()
        if not owner or self._notification_wakes_closed:
            return
        if self._notification_loop is None:
            try:
                self._notification_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        is_new = owner not in self._watched_notification_conversations
        self._watched_notification_conversations.add(owner)
        if is_new:
            # A durable item may predate this process/session, so first bind is
            # also a replay recheck rather than waiting for a fresh enqueue.
            self.request_parent_notification_wake(owner)

    def recheck_watched_parent_notifications(self) -> None:
        """Recheck durable state after reconnect without trusting old signals."""

        for conversation_id in tuple(self._watched_notification_conversations):
            self.request_parent_notification_wake(conversation_id)

    def request_parent_notification_wake(self, conversation_id: str) -> None:
        owner = str(conversation_id or "").strip()
        loop = self._notification_loop
        if (
            not owner
            or owner not in self._watched_notification_conversations
            or self._notification_wakes_closed
            or loop is None
            or loop.is_closed()
        ):
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            self._request_parent_notification_wake_on_loop(owner)
        else:
            loop.call_soon_threadsafe(
                self._request_parent_notification_wake_on_loop,
                owner,
            )

    def recheck_parent_notification_wake(self, conversation_id: str) -> None:
        """Retry a previously signalled wake after a busy/user-queue fence."""

        owner = str(conversation_id or "").strip()
        loop = self._notification_loop
        if (
            not owner
            or owner not in self._watched_notification_conversations
            or self._notification_wakes_closed
            or loop is None
            or loop.is_closed()
        ):
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            self._schedule_parent_notification_wake(owner)
        else:
            loop.call_soon_threadsafe(
                self._schedule_parent_notification_wake,
                owner,
            )

    def _on_parent_notification_enqueued(
        self,
        notification: ParentNotification,
    ) -> None:
        # Enqueue can complete in a background hook/subagent thread.  Never
        # mutate asyncio/session state from that producer; hand the durable
        # signal back to the WebSocket session's event loop.
        conversation_id = str(notification.conversation_id or "").strip()
        if self._notification_owned_by_other_live_session(
            str(notification.session_id or "").strip()
        ):
            return
        self.request_parent_notification_wake(conversation_id)

    def _notification_owned_by_other_live_session(
        self,
        notification_session_id: str,
    ) -> bool:
        owner_session_id = str(notification_session_id or "").strip()
        current_session_id = str(self._session.session_id or "").strip()
        if not owner_session_id or owner_session_id == current_session_id:
            return False
        ws_manager = self._session.ws_manager
        if ws_manager is None:
            return False
        owner_session = ws_manager.get_session(owner_session_id)
        return bool(
            owner_session is not None
            and owner_session is not self._session
            and not owner_session.run_manager._notification_wakes_closed
        )

    def _request_parent_notification_wake_on_loop(
        self,
        conversation_id: str,
    ) -> None:
        if (
            self._notification_wakes_closed
            or conversation_id not in self._watched_notification_conversations
        ):
            return
        self._notification_request_generation[conversation_id] = (
            self._notification_request_generation.get(conversation_id, 0) + 1
        )
        self._schedule_parent_notification_wake(conversation_id)

    def _schedule_parent_notification_wake(self, conversation_id: str) -> None:
        if self._notification_wakes_closed:
            return
        requested = self._notification_request_generation.get(conversation_id, 0)
        attempted = self._notification_attempt_generation.get(conversation_id, 0)
        if requested <= attempted:
            return
        existing = self._notification_wake_tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._dispatch_parent_notification_wake(conversation_id),
            name=f"parent-notification-wake:{conversation_id}",
        )
        self._notification_wake_tasks[conversation_id] = task

        def _discard(completed: asyncio.Task[None]) -> None:
            if self._notification_wake_tasks.get(conversation_id) is completed:
                self._notification_wake_tasks.pop(conversation_id, None)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                logger.exception(
                    "Parent notification wake failed for conversation %s",
                    conversation_id,
                )

        task.add_done_callback(_discard)

    def _has_replayable_parent_notification(self, conversation_id: str) -> bool:
        try:
            notifications = load_parent_outbox(
                conversation_id=conversation_id,
            ).replayable()
        except Exception:
            logger.exception(
                "Failed to inspect parent notifications for conversation %s",
                conversation_id,
            )
            return False
        return any(
            str(item.status or "pending") in {"pending", "delivered", "failed"}
            and not self._notification_owned_by_other_live_session(
                str(item.session_id or "")
            )
            for item in notifications
        )

    async def _dispatch_parent_notification_wake(
        self,
        conversation_id: str,
    ) -> None:
        # Match CC's queue processor: yield once so already-enqueued human input
        # can take its higher-priority path before a later task notification.
        await asyncio.sleep(0)
        lifecycle_lock = self._session.conversation_lifecycle_lock()
        async with lifecycle_lock:
            requested = self._notification_request_generation.get(
                conversation_id,
                0,
            )
            attempted = self._notification_attempt_generation.get(
                conversation_id,
                0,
            )
            if (
                self._notification_wakes_closed
                or requested <= attempted
                or conversation_id not in self._watched_notification_conversations
            ):
                return
            # Registration is the ownership fence until cleanup.  Do not use
            # Task.done(): the completed task may still be committing DONE,
            # transcript and turn-input state.
            if (
                conversation_id in self.run_tasks
                or conversation_id in self._queue_dispatching
                or conversation_id in self._queue_steering
                or bool(self._user_message_queues.get(conversation_id))
                or conversation_id in self._inflight_user_messages
            ):
                return

            if not self._has_replayable_parent_notification(conversation_id):
                self._notification_attempt_generation[conversation_id] = requested
                return
            if not claim_parent_notification_wake(
                conversation_id,
                self._notification_wake_owner_token,
            ):
                # Another live session has the QueryGuard-style reservation.
                # Its run or cleanup owns the next recheck.
                self._notification_attempt_generation[conversation_id] = requested
                return
            conversation_repo = getattr(self._session, "conversation_repo", None)
            get_conversation = getattr(conversation_repo, "get_conversation", None)
            conversation = (
                get_conversation(conversation_id)
                if callable(get_conversation)
                else None
            )
            if (
                conversation is None
                or bool(getattr(conversation, "archived", False))
                or str(getattr(conversation, "conversation_type", "main"))
                != "main"
            ):
                self._notification_attempt_generation[conversation_id] = requested
                release_parent_notification_wake(
                    conversation_id,
                    self._notification_wake_owner_token,
                )
                return
            try:
                task_id = await self._session.start_agent_run(
                    "",
                    attachments=[],
                    conversation_id=conversation_id,
                    metadata={
                        "_parent_notification_only": True,
                        "query_source": "task-notification",
                        "notification_wake_generation": requested,
                    },
                )
            except BaseException:
                self._notification_attempt_generation[conversation_id] = requested
                release_parent_notification_wake(
                    conversation_id,
                    self._notification_wake_owner_token,
                )
                raise
            self._notification_attempt_generation[conversation_id] = requested
            self._notification_run_task_ids[conversation_id] = str(task_id)

    def stop_notification_wake_intake(self) -> None:
        """Synchronously stop new CC-style queue signals during teardown."""

        if self._notification_wakes_closed:
            return
        self._notification_wakes_closed = True
        unsubscribe = self._unsubscribe_parent_notifications
        self._unsubscribe_parent_notifications = None
        if callable(unsubscribe):
            unsubscribe()

    async def shutdown_notification_wakes(self) -> None:
        """Detach the process signal and drain session-owned wake tasks."""

        self.stop_notification_wake_intake()
        tasks = [
            task
            for task in self._notification_wake_tasks.values()
            if not task.done() and task is not asyncio.current_task()
        ]
        self._notification_wake_tasks.clear()
        for conversation_id in tuple(self._notification_run_task_ids):
            release_parent_notification_wake(
                conversation_id,
                self._notification_wake_owner_token,
            )
        self._notification_run_task_ids.clear()
        if tasks:
            await cancel_and_drain_receipt(
                tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="parent notification wake shutdown",
                owner=self._retired_notification_wake_tasks,
            )

    def cleanup(
        self,
        *,
        conversation_id: str,
        task: asyncio.Task[Any],
        task_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        if self._notification_run_task_ids.get(conversation_id) == task_id:
            self._notification_run_task_ids.pop(conversation_id, None)
            release_parent_notification_wake(
                conversation_id,
                self._notification_wake_owner_token,
            )
        registered_task = self.run_tasks.get(conversation_id) if conversation_id else None
        newer_run_registered = registered_task is not None and registered_task is not task
        if conversation_id and registered_task is task:
            self.run_tasks.pop(conversation_id, None)
        if conversation_id and self.run_task_ids.get(conversation_id) == task_id:
            self.run_task_ids.pop(conversation_id, None)
        if conversation_id and self.cancel_events.get(conversation_id) is cancel_event:
            self.cancel_events.pop(conversation_id, None)
        if self._active_run_task is task:
            self._active_run_task = None
        if self._active_task_id == task_id:
            self._active_task_id = None
        if self._active_run_cancel_event is cancel_event:
            self._active_run_cancel_event = None
        if not newer_run_registered:
            self._seal_turn_input_queue(conversation_id)
            self._terminal_statuses.pop(conversation_id, "")
        # The canonical agent.run.completed event is emitted by the agent loop.
        # Cleanup only releases manager bookkeeping; recording another terminal
        # event here produced a second, run-id-less completion after DONE.
        self._session.session_lifecycle.schedule_task_runtime_update()

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
                await self._cancel_run_tree(str(task_id), reason=reason)

            cancel_event = self.cancel_events.get(cid)
            if isinstance(cancel_event, asyncio.Event):
                cancel_event.set()
            task = self.run_tasks.get(cid)
            if task is not None:
                seen_tasks.add(task)
                tasks_to_wait.add(task)
                cancelled_any = True
                if not task.done():
                    task.cancel()

        active_task = self._active_run_task
        active_task_id = self._active_task_id
        active_cancel_event = self._active_run_cancel_event

        if not target_conversation_id:
            if isinstance(active_cancel_event, asyncio.Event):
                active_cancel_event.set()
            if active_task is not None and active_task not in seen_tasks and not active_task.done():
                active_task.cancel()
                tasks_to_wait.add(active_task)
                cancelled_any = True
            if active_task_id and active_task_id not in seen_task_ids:
                await self._cancel_run_tree(str(active_task_id), reason=reason)

        await await_with_deadline(
            self._session.cancel_pending_approvals(
                reason=reason,
                conversation_id=target_conversation_id or None,
            ),
            timeout=0.25,
            label="pending approval cancellation",
            owner=self._session.cleanup_tasks,
        )
        current = asyncio.current_task()
        waitable = [task for task in tasks_to_wait if task is not current and not task.done()]
        if waitable:
            await cancel_and_drain_to_completion(
                waitable,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="agent run cancellation",
            )
        self._session.session_lifecycle.schedule_task_runtime_update()
        return cancelled_any

    async def _cancel_run_tree(self, task_id: str, *, reason: str) -> None:
        from backend.agent.runtime import default_runtime

        try:
            # The runtime owns the child stop: it cancels, drains within the
            # cancellation deadline and retains cleanup ownership of whatever
            # survived. A failure here means children may still be running, so
            # it must be visible rather than reduced to a debug line.
            await default_runtime().stop_subagent_tasks_for_task(task_id, reason=reason)
        except Exception:
            logger.exception(
                "Failed to stop child subagents for task_id=%s session=%s",
                task_id,
                getattr(self._session, "session_id", ""),
            )
        finally:
            # The user-visible task record is cancelled either way; leaving it
            # running would claim an active turn that nothing is driving.
            self._session.task_manager.cancel(task_id)
