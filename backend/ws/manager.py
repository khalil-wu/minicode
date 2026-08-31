from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from backend.api.auth import _websocket_accept_subprotocol
from backend.artifact.store import ArtifactStore
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    _consume_task_result,
    cancel_and_drain_receipt,
)
from backend.config import AppConfig
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from backend.agent.message import AgentEvent
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)

_SESSION_MCP_MANAGER_UNSET = object()
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _invalidate_runtime_status_cache() -> None:
    from backend.api import _state

    _state.invalidate_status_cache()


async def _dispose_unadopted_connection_resources(
    llm: LLMAdapter,
    artifact_store: ArtifactStore,
    *,
    adopted_session: WebSocketSession | None = None,
) -> None:
    """Release resources created for a connection that was not adopted.

    ``websocket_endpoint`` creates these objects before the manager can decide
    whether a reconnect will attach to an existing session.  Ownership moves
    only when a new ``WebSocketSession`` is constructed; a reconnect must close
    its discarded objects here without touching the live session's objects.
    """

    adopted_llm = getattr(adopted_session, "llm", None)
    if llm is not adopted_llm:
        close = getattr(llm, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                logger.exception("Failed to close an unadopted LLM adapter")

    adopted_artifact_store = getattr(adopted_session, "artifact_store", None)
    if artifact_store is adopted_artifact_store:
        return
    try:
        await artifact_store.flush()
    except Exception:
        logger.exception("Failed to flush an unadopted artifact store")
    finally:
        artifact_store.shutdown()
        artifact_store.clear()


class WebSocketManager:
    def __init__(self) -> None:
        self._sessions: dict[str, WebSocketSession] = {}
        self._disconnect_tasks: dict[str, asyncio.Task] = {}
        # Destructive conversation teardown belongs to the process-wide
        # manager, not to the websocket that happened to request it. A
        # renderer may disconnect while the tombstone/replay/worktree cleanup
        # is still running.
        self._conversation_delete_tasks: set[asyncio.Task[Any]] = set()
        self._conversation_delete_cleanup_tasks: dict[str, set[Any]] = {}
        self._conversation_delete_release_tasks: dict[str, asyncio.Task[Any]] = {}
        self._conversation_lifecycle_loop: asyncio.AbstractEventLoop | None = None
        self._shared_conversation_lifecycle_lock: asyncio.Lock | None = None
        self._conversation_projection_loop: asyncio.AbstractEventLoop | None = None
        self._shared_conversation_projection_locks: dict[str, asyncio.Lock] = {}
        self._conversation_resource_lock = threading.RLock()
        self._attachment_upload_owners: dict[str, str] = {}
        self._conversation_delete_fences: dict[str, str] = {}

    def conversation_lifecycle_lock(self) -> asyncio.Lock:
        """Return the one lifecycle lock shared by every live renderer.

        Test clients can recreate their event loop while retaining the global
        manager. Replace an idle lock when that happens, but never detach a
        lock that still protects an in-flight mutation.
        """

        loop = asyncio.get_running_loop()
        lock = self._shared_conversation_lifecycle_lock
        if lock is None or self._conversation_lifecycle_loop is not loop:
            if lock is not None and lock.locked():
                raise RuntimeError("Conversation lifecycle loop changed during a mutation")
            lock = asyncio.Lock()
            self._shared_conversation_lifecycle_lock = lock
            self._conversation_lifecycle_loop = loop
        return lock

    def conversation_projection_lock(self, conversation_id: str) -> asyncio.Lock:
        owner = str(conversation_id or "").strip()
        if not owner:
            raise ValueError("conversation_id is required")
        loop = asyncio.get_running_loop()
        if self._conversation_projection_loop is not loop:
            if any(lock.locked() for lock in self._shared_conversation_projection_locks.values()):
                raise RuntimeError("Conversation projection loop changed during a mutation")
            self._shared_conversation_projection_locks.clear()
            self._conversation_projection_loop = loop
        lock = self._shared_conversation_projection_locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            self._shared_conversation_projection_locks[owner] = lock
        return lock

    def reserve_attachment_upload(self, conversation_id: str) -> str | None:
        owner = str(conversation_id or "").strip()
        if not owner:
            return None
        with self._conversation_resource_lock:
            if owner in self._conversation_delete_fences:
                return None
            token = uuid.uuid4().hex
            self._attachment_upload_owners[token] = owner
            return token

    def release_attachment_upload(self, token: str) -> None:
        clean_token = str(token or "").strip()
        if not clean_token:
            return
        with self._conversation_resource_lock:
            self._attachment_upload_owners.pop(clean_token, None)

    def begin_conversation_delete(self, conversation_id: str) -> tuple[str | None, str, int]:
        owner = str(conversation_id or "").strip()
        if not owner:
            return None, "invalid_conversation", 0
        with self._conversation_resource_lock:
            if owner in self._conversation_delete_fences:
                return None, "delete_in_progress", 0
            upload_count = sum(
                1
                for upload_owner in self._attachment_upload_owners.values()
                if upload_owner == owner
            )
            if upload_count:
                return None, "attachment_upload_active", upload_count
            token = uuid.uuid4().hex
            self._conversation_delete_fences[owner] = token
            self._conversation_delete_cleanup_tasks[owner] = set()
            return token, "", 0

    def end_conversation_delete(self, conversation_id: str, token: str) -> None:
        owner = str(conversation_id or "").strip()
        clean_token = str(token or "").strip()
        if not owner or not clean_token:
            return
        with self._conversation_resource_lock:
            if self._conversation_delete_fences.get(owner) == clean_token:
                self._conversation_delete_fences.pop(owner, None)
                self._conversation_delete_cleanup_tasks.pop(owner, None)
                release_task = self._conversation_delete_release_tasks.pop(owner, None)
                if (
                    release_task is not None
                    and release_task is not asyncio.current_task()
                    and not release_task.done()
                ):
                    release_task.cancel()

    def conversation_delete_fence(self, conversation_id: str) -> str | None:
        owner = str(conversation_id or "").strip()
        if not owner:
            return None
        with self._conversation_resource_lock:
            return self._conversation_delete_fences.get(owner)

    def conversation_delete_fenced_ids(self) -> tuple[str, ...]:
        """Return the active delete owners for global maintenance commands."""

        with self._conversation_resource_lock:
            return tuple(self._conversation_delete_fences)

    def conversation_delete_cleanup_owner(self, conversation_id: str) -> set[Any] | None:
        owner = str(conversation_id or "").strip()
        if not owner:
            return None
        with self._conversation_resource_lock:
            return self._conversation_delete_cleanup_tasks.get(owner)

    def track_conversation_delete_task(self, task: asyncio.Task[Any]) -> None:
        """Retain one delete task until it settles, independent of a session."""

        self._conversation_delete_tasks.add(task)

        def _finished(completed: asyncio.Task[Any]) -> None:
            self._conversation_delete_tasks.discard(completed)
            _consume_task_result(completed)

        task.add_done_callback(_finished)

    def finish_conversation_delete(self, conversation_id: str, token: str) -> None:
        """Release a delete fence only after its detached cleanup writes settle."""

        owner = str(conversation_id or "").strip()
        clean_token = str(token or "").strip()
        cleanup_owner = self.conversation_delete_cleanup_owner(owner)
        pending = {
            task
            for task in (cleanup_owner or set())
            if task is not asyncio.current_task() and not task.done()
        }
        if not pending:
            self.end_conversation_delete(owner, clean_token)
            return
        if owner in self._conversation_delete_release_tasks:
            return

        async def _wait_for_cleanup() -> None:
            was_cancelled = False
            while True:
                current = asyncio.current_task()
                pending_now = {
                    task
                    for task in (cleanup_owner or set())
                    if task is not current and not task.done()
                }
                if not pending_now:
                    self.end_conversation_delete(owner, clean_token)
                    if was_cancelled:
                        raise asyncio.CancelledError
                    return
                try:
                    # The waiter observes cleanup completion but never owns
                    # cancellation of the side-effecting children. If a
                    # manager shutdown cancels this waiter, it records that
                    # request and continues until the fence can be released.
                    await asyncio.shield(
                        asyncio.gather(*pending_now, return_exceptions=True)
                    )
                except asyncio.CancelledError:
                    was_cancelled = True

        release_task = asyncio.create_task(
            _wait_for_cleanup(),
            name=f"conversation-delete-release:{owner}",
        )
        self._conversation_delete_release_tasks[owner] = release_task
        self.track_conversation_delete_task(release_task)

    async def connect(
        self,
        websocket: WebSocket,
        llm: LLMAdapter,
        artifact_store: ArtifactStore,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        config: AppConfig,
        skill_manager: Any | None = None,
        skill_executor: Any | None = None,
        memory_manager: Any | None = None,
        mcp_manager: Any = _SESSION_MCP_MANAGER_UNSET,
    ) -> tuple[WebSocketSession, int]:
        from backend.ws.handler import WebSocketSession

        try:
            await websocket.accept(subprotocol=_websocket_accept_subprotocol(websocket))
        except BaseException:
            await _dispose_unadopted_connection_resources(llm, artifact_store)
            raise

        requested_session_id = (websocket.query_params.get("session_id") or "").strip()
        if requested_session_id and not SESSION_ID_PATTERN.fullmatch(requested_session_id):
            try:
                await websocket.close(code=1008, reason="invalid session_id")
            finally:
                await _dispose_unadopted_connection_resources(llm, artifact_store)
            raise WebSocketDisconnect(code=1008)
        session_id = requested_session_id or f"session_{uuid.uuid4().hex}"

        if session_id in self._disconnect_tasks:
            self._disconnect_tasks[session_id].cancel()
            del self._disconnect_tasks[session_id]
            logger.info(f"Cancelled disconnect cleanup task for session {session_id} due to reconnection")

        existing_session = self._sessions.get(session_id)
        if existing_session:
            try:
                previous_ws, generation = existing_session.attach_websocket(websocket)
                if previous_ws is not websocket:
                    try:
                        await previous_ws.close(code=1012, reason="replaced by newer connection")
                    except Exception:
                        logger.debug(
                            "session %s previous websocket close failed",
                            session_id,
                            exc_info=True,
                        )
                _invalidate_runtime_status_cache()
                return existing_session, generation
            finally:
                await _dispose_unadopted_connection_resources(
                    llm,
                    artifact_store,
                    adopted_session=existing_session,
                )

        session = WebSocketSession(
            session_id=session_id,
            websocket=websocket,
            llm=llm,
            artifact_store=artifact_store,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            config=config,
            skill_manager=skill_manager,
            skill_executor=skill_executor,
            memory_manager=memory_manager,
            mcp_manager=mcp_manager,
            ws_manager=self,
        )
        self._sessions[session_id] = session
        _invalidate_runtime_status_cache()
        return session, session.connection_generation

    def get_session(self, session_id: str) -> WebSocketSession | None:
        return self._sessions.get(session_id)

    def iter_sessions(self) -> tuple[WebSocketSession, ...]:
        return tuple(self._sessions.values())

    def disconnect(self, session_id: str, *, connection_generation: int | None = None) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if connection_generation is not None and session.connection_generation != connection_generation:
            return
        session.mark_disconnected()
        _invalidate_runtime_status_cache()

        async def delayed_cleanup():
            try:
                await asyncio.sleep(30.0)
                if session_id in self._sessions and self._sessions[session_id] is session:
                    # Fence the expired owner before awaiting cleanup. A socket
                    # that reconnects after the grace deadline gets a fresh
                    # session instead of attaching to one being destroyed.
                    self._sessions.pop(session_id, None)
                    if self._disconnect_tasks.get(session_id) is asyncio.current_task():
                        self._disconnect_tasks.pop(session_id, None)
                    _invalidate_runtime_status_cache()
                    await session.session_lifecycle.shutdown(reason="disconnect_timeout")
                    logger.info("Session %s cleaned up after disconnect timeout", session_id)
            except asyncio.CancelledError:
                logger.info(
                    "Session %s cleanup cancelled due to successful reconnection",
                    session_id,
                )
            finally:
                if self._disconnect_tasks.get(session_id) is asyncio.current_task():
                    self._disconnect_tasks.pop(session_id, None)

        old_task = self._disconnect_tasks.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._disconnect_tasks[session_id] = asyncio.create_task(delayed_cleanup())

    async def shutdown_session(
        self,
        session_id: str,
        *,
        reason: str = "session_shutdown",
    ) -> bool:
        """Dispose one WebSocket-owned runtime after a MiniCode shutdown request."""

        clean_id = str(session_id or "").strip()
        if not clean_id:
            return False
        session = self._sessions.pop(clean_id, None)
        disconnect_task = self._disconnect_tasks.pop(clean_id, None)
        if disconnect_task is not None and not disconnect_task.done():
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)
        if session is None:
            return False
        session.mark_disconnected()
        _invalidate_runtime_status_cache()
        try:
            await session.session_lifecycle.shutdown(reason=reason)
        except Exception:
            logger.exception("Failed to shut down websocket session %s", clean_id)
        websocket = getattr(session, "ws", None)
        close = getattr(websocket, "close", None)
        if callable(close):
            try:
                await close(code=1000, reason="extension shutdown")
            except Exception:
                logger.debug(
                    "Websocket close failed after session shutdown for %s",
                    clean_id,
                    exc_info=True,
                )
        return True

    async def _drain_conversation_delete_tasks(self) -> None:
        """Drain destructive teardown before session resources are retired."""

        for _ in range(2):
            tasks = {
                task
                for task in self._conversation_delete_tasks
                if not task.done()
            }
            for cleanup_tasks in self._conversation_delete_cleanup_tasks.values():
                tasks.update(task for task in cleanup_tasks if not task.done())
            if not tasks:
                return
            await cancel_and_drain_receipt(
                tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="conversation delete tasks",
                owner=self._conversation_delete_tasks,
            )
        if self._conversation_delete_tasks:
            logger.warning(
                "Conversation delete cleanup remains pending during manager shutdown: %d task(s)",
                len(self._conversation_delete_tasks),
            )

    async def shutdown(self, *, reason: str = "application_shutdown") -> None:
        """Drain disconnect timers and all live sessions before loop teardown."""
        cleanup_tasks = list(self._disconnect_tasks.values())
        self._disconnect_tasks.clear()
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        # Session shutdown cancels session-owned command tasks. Destructive
        # conversation deletion is manager-owned and must be settled before
        # those session resources disappear.
        await self._drain_conversation_delete_tasks()

        sessions = list(self._sessions.values())
        self._sessions.clear()
        if sessions:
            await asyncio.gather(
                *(
                    session.session_lifecycle.shutdown(reason=reason)
                    for session in sessions
                ),
                return_exceptions=True,
            )
        _invalidate_runtime_status_cache()

    async def broadcast_event(self, event: AgentEvent) -> None:
        sessions = [session for session in self._sessions.values() if session.is_connected]
        for session in sessions:
            await session.send_event(event)

    def runtime_snapshot(self) -> dict[str, Any]:
        sessions = [session for session in self._sessions.values() if session.is_connected]
        session_snapshots = [session.runtime_snapshot() for session in sessions]

        total_running = 0
        total_pending = 0
        total_completed = 0
        total_failed = 0
        total_cancelled = 0
        for snapshot in session_snapshots:
            summary = snapshot.get("task_summary", {})
            total_running += int(summary.get("running", 0))
            total_pending += int(summary.get("pending", 0))
            total_completed += int(summary.get("completed", 0))
            total_failed += int(summary.get("failed", 0))
            total_cancelled += int(summary.get("cancelled", 0))

        return {
            "active_sessions": len(sessions),
            "running_tasks": total_running,
            "pending_tasks": total_pending,
            "completed_tasks": total_completed,
            "failed_tasks": total_failed,
            "cancelled_tasks": total_cancelled,
            # Manager snapshots feed unauthenticated/local health and Doctor
            # endpoints. Never aggregate per-session workspace, conversation,
            # approval, queue, or task payloads across connected clients.
        }

    @property
    def active_count(self) -> int:
        return sum(1 for session in self._sessions.values() if session.is_connected)

    def reset_for_tests(self) -> None:
        """Drop retained sessions so test cases cannot leak runtime state."""
        for task in list(self._disconnect_tasks.values()):
            if not task.done():
                task.cancel()
        self._disconnect_tasks.clear()
        for task in list(self._conversation_delete_tasks):
            if not task.done():
                task.cancel()
        self._conversation_delete_tasks.clear()
        self._conversation_delete_release_tasks.clear()
        self._conversation_delete_cleanup_tasks.clear()
        with self._conversation_resource_lock:
            self._attachment_upload_owners.clear()
            self._conversation_delete_fences.clear()
        lifecycle_lock = self._shared_conversation_lifecycle_lock
        if lifecycle_lock is None or not lifecycle_lock.locked():
            self._shared_conversation_lifecycle_lock = None
            self._conversation_lifecycle_loop = None
        if not any(
            lock.locked() for lock in self._shared_conversation_projection_locks.values()
        ):
            self._shared_conversation_projection_locks.clear()
            self._conversation_projection_loop = None

        for session in list(self._sessions.values()):
            session.mark_disconnected()

            active_task = session.run_manager.active_run_task
            if active_task and not active_task.done():
                active_cancel_event = session.run_manager.active_run_cancel_event
                if isinstance(active_cancel_event, asyncio.Event):
                    active_cancel_event.set()
                active_task.cancel()

            workspace_context_task = session.session_lifecycle.workspace_context_task
            if workspace_context_task and not workspace_context_task.done():
                workspace_context_task.cancel()

            session.turn_wait_state.clear_pending_waiters()
            session.approval_diff_cache.clear()

            file_watcher = session.session_lifecycle.file_watcher
            if file_watcher and file_watcher.is_running():
                file_watcher.stop()
            session.session_lifecycle.file_watcher = None

        self._sessions.clear()
