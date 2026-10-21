from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import ReferenceType, WeakKeyDictionary, WeakValueDictionary, ref

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
import httpx

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
from backend.ws.event_outbox import EventOutbox
from backend.ws.execution_context import (
    DEFAULT_THREAD_UNLOAD_DELAY_SECONDS,
    ConversationExecutionContext,
    ConversationExecutionRegistry,
    ExecutionHost,
    ExecutionUnload,
    ThreadUnloadingState,
)

if TYPE_CHECKING:
    from backend.agent.message import AgentEvent
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)

_SESSION_MCP_MANAGER_UNSET = object()
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


async def _verified_window_binding(websocket: WebSocket, window_instance_id: str, window_owner_id: str) -> str:
    protocols = websocket.headers.get("sec-websocket-protocol", "").split(",")
    binding_token = next((protocol.strip()[len("minicode-window."):] for protocol in protocols
                          if protocol.strip().startswith("minicode-window.")), "")
    if not (window_instance_id or window_owner_id or binding_token):
        return ""
    endpoint = os.environ.get("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "")
    if not endpoint or not (window_instance_id and window_owner_id and binding_token):
        await websocket.close(code=1008, reason="invalid window binding")
        raise WebSocketDisconnect(code=1008)
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        response = await client.post(endpoint.rstrip("/") + "/v1/window-binding",
            headers={"Authorization": "Bearer " + os.environ.get("MINICODE_EMBEDDED_BROWSER_TOKEN", "")},
            json={"window_instance_id": window_instance_id, "window_owner_id": window_owner_id, "window_binding_token": binding_token})
    if response.status_code != 200 or response.json().get("ok") is not True:
        await websocket.close(code=1008, reason="invalid window binding")
        raise WebSocketDisconnect(code=1008)
    return binding_token


def _invalidate_runtime_status_cache() -> None:
    from backend.api import _state

    _state.invalidate_status_cache()


async def _dispose_unadopted_connection_resources(
    llm: LLMAdapter | None,
    artifact_store: ArtifactStore | None,
    *,
    adopted_session: WebSocketSession | None = None,
) -> None:
    """Release resources created for a connection that was not adopted.

    Connection-owned resources exist before the manager decides whether a
    reconnect attaches to an existing session. Adapter ownership is optional:
    the desktop transport starts unbound, while an embedding caller may supply
    an actual adapter. A reconnect releases only objects it did not adopt.
    """

    try:
        adopted_llm = getattr(adopted_session, "llm", None)
        if llm is not adopted_llm:
            close = getattr(llm, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.exception("Failed to close an unadopted LLM adapter")
    finally:
        adopted_artifact_store = getattr(adopted_session, "artifact_store", None)
        if artifact_store is not adopted_artifact_store:
            try:
                await artifact_store.aclose()
            finally:
                artifact_store.clear()


class WebSocketManager:
    def __init__(self, *, thread_unload_delay_seconds: float = DEFAULT_THREAD_UNLOAD_DELAY_SECONDS) -> None:
        self._sessions: dict[str, WebSocketSession] = {}
        self._connecting_websockets: dict[str, WebSocket] = {}
        self._disconnect_tasks: dict[str, asyncio.Task] = {}
        # Destructive conversation teardown belongs to the process-wide
        # manager, not to the websocket that happened to request it. A
        # renderer may disconnect while the tombstone/replay/worktree cleanup
        # is still running.
        self._conversation_delete_tasks: set[asyncio.Task[Any]] = set()
        self._conversation_delete_cleanup_tasks: dict[str, set[Any]] = {}
        self._conversation_delete_release_tasks: dict[str, asyncio.Task[Any]] = {}
        self._conversation_lifecycle_loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_resource_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._lifecycle_condition = asyncio.Condition()
        self._lifecycle_readers = 0
        self._lifecycle_writer = False
        self._lifecycle_waiting_writers = 0
        self._conversation_projection_loop: asyncio.AbstractEventLoop | None = None
        self._shared_conversation_projection_locks: dict[str, asyncio.Lock] = {}
        self._conversation_resource_lock = threading.RLock()
        self._attachment_upload_owners: dict[str, str] = {}
        self._conversation_delete_fences: dict[str, str] = {}
        self.execution_contexts = ConversationExecutionRegistry()
        self._thread_unload_delay_seconds = thread_unload_delay_seconds
        self._execution_watch_tasks: dict[ConversationExecutionContext, asyncio.Task[None]] = {}
        self._execution_watch_events: dict[ConversationExecutionContext, asyncio.Event] = {}
        self._execution_host_shutdown_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutting_down = False
        self._conversation_observers: WeakKeyDictionary[WebSocketSession, set[str]] = WeakKeyDictionary()
        self._conversation_stream_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._heartbeat_renderer_states: dict[str, tuple[ReferenceType, int, bool, str | None, float]] = {}
        self._heartbeat_renderer_requests: dict[tuple[str, int], set[str]] = {}

    def record_heartbeat_renderer_state(self, session: WebSocketSession, generation: int, data: dict[str, Any]) -> bool:
        if (self._shutting_down or self._sessions.get(session.session_id) is not session
                or not session.is_connected or session.connection_generation != generation):
            return False
        conversation_id = data["conversation_id"]
        allowed = (conversation_id == session.active_conversation_id
            or conversation_id in self._conversation_observers.get(session, ())
            or conversation_id in self._heartbeat_renderer_requests.get((session.session_id, generation), ()))
        if not allowed:
            return False
        self._heartbeat_renderer_states[conversation_id] = (
            ref(session), generation, data["is_eligible"], data["reason"], time.time(),
        )
        return True

    def heartbeat_renderer_block_reason(self, conversation_id: str) -> str | None:
        state = self._heartbeat_renderer_states.get(conversation_id)
        if state is None:
            return None
        reporter, generation, eligible, reason, received_at = state
        session = reporter()
        if (session is None or time.time() - received_at > 120 or self._sessions.get(session.session_id) is not session
                or not session.is_connected or session.connection_generation != generation):
            self._heartbeat_renderer_states.pop(conversation_id)
            return None
        return None if eligible else reason or "renderer_ineligible"

    def _discard_heartbeat_reporter(self, session: WebSocketSession, generation: int) -> None:
        self._heartbeat_renderer_requests.pop((session.session_id, generation), None)
        for conversation_id, state in tuple(self._heartbeat_renderer_states.items()):
            if state[0]() is session and state[1] == generation:
                self._heartbeat_renderer_states.pop(conversation_id)

    def request_heartbeat_renderer_state(self, conversation_id: str, *, workspace_root: str) -> None:
        from backend.async_cleanup import retain_cleanup_task
        from backend.services.workspace_service import main_worktree_root

        for session in tuple(self._sessions.values()):
            if not session.is_connected or session.session_lifecycle.is_shutting_down:
                continue
            observed = conversation_id == session.active_conversation_id or conversation_id in self._conversation_observers.get(session, ())
            current_root = session.session_lifecycle.current_workspace_root()
            if not observed and (current_root is None or main_worktree_root(current_root) != main_worktree_root(Path(workspace_root))):
                continue
            generation = session.connection_generation
            self._heartbeat_renderer_requests.setdefault((session.session_id, generation), set()).add(conversation_id)
            task = asyncio.create_task(session.event_outbox.send_payload({
                "type": "scheduler.heartbeat.renderer_state.request", "conversation_id": conversation_id,
            }, connection_generation=generation, log_context="scheduler.heartbeat.renderer_state.request", wait_for_delivery=False))
            retain_cleanup_task(task, session.cleanup_tasks)

    def conversation_stream_lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._conversation_stream_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_stream_locks[conversation_id] = lock
        return lock

    def register_conversation_execution_owner(self, conversation_id: str, session: WebSocketSession) -> None:
        self.load_conversation_execution(conversation_id, session)
        if session.is_connected and self._sessions.get(session.session_id) is session:
            self._conversation_observers.setdefault(session, set()).add(conversation_id)
        self.note_execution_activity(conversation_id)
        self.schedule_observer_runtime_updates(session)

    def load_conversation_execution(self, conversation_id: str, session: WebSocketSession) -> ConversationExecutionContext:
        if session.session_lifecycle.is_shutting_down:
            raise RuntimeError("The original execution host is shutting down")
        current = self.execution_contexts.context_for(conversation_id)
        context = self.execution_contexts.load(
            conversation_id, session, has_subscribers=self._has_conversation_subscribers(conversation_id),
            is_active=self._execution_is_active(current) if current is not None else session.run_manager.running_task_for(conversation_id) is not None,
            now=asyncio.get_running_loop().time(), unload_delay=self._thread_unload_delay_seconds,
        )
        self.note_execution_activity(conversation_id)
        return context

    def retain_execution_admission(self, session: WebSocketSession, task: asyncio.Task[Any], *, conversation_id: str = "") -> None:
        admission = self.execution_contexts.retain_admission(session, task)
        if conversation_id:
            self.bind_execution_admission(session, task, conversation_id)

        def completed(_task: asyncio.Task[Any]) -> None:
            if self.execution_contexts.finish_admission(admission):
                if admission.context is not None:
                    self.note_execution_activity(admission.context.conversation_id)
                self._schedule_detached_host_release(admission.owner)

        task.add_done_callback(completed)

    def bind_execution_admission(self, session: WebSocketSession, task: asyncio.Task[Any], conversation_id: str) -> None:
        admission = self.execution_contexts.admission_for(task)
        if admission is None:
            # Embedded/queued callers can enter the dispatcher without a wire
            # scheduler; they still retain their actual submitting task.
            self.retain_execution_admission(session, task)
            admission = self.execution_contexts.admission_for(task)
        owner = self.execution_session_for_conversation(conversation_id) or session
        context = self.load_conversation_execution(conversation_id, owner)
        self.execution_contexts.bind_admission(admission, context)
        self.note_execution_activity(conversation_id)

    def track_execution_cleanup(self, conversation_id: str, session: WebSocketSession, task: asyncio.Task[Any]) -> None:
        context = self.load_conversation_execution(conversation_id, session)
        context.track_lifecycle_task(task)
        task.add_done_callback(lambda _task: self.note_execution_activity(conversation_id))
        self.note_execution_activity(conversation_id)

    def _has_conversation_subscribers(self, conversation_id: str) -> bool:
        return any(conversation_id in subscriptions and observer.is_connected
            and self._sessions.get(observer.session_id) is observer
            for observer, subscriptions in tuple(self._conversation_observers.items()))

    def _remove_transport_subscriptions(self, session: WebSocketSession) -> None:
        for conversation_id in self._conversation_observers.pop(session, set()):
            self.note_execution_activity(conversation_id)

    def note_host_execution_activity(self, session: WebSocketSession) -> None:
        owner = self.execution_contexts.retained_host(session.session_id)
        if owner is not None and owner.session is session:
            for context in self.execution_contexts.contexts_for_host(owner):
                self.note_execution_activity(context.conversation_id)

    def _execution_is_active(self, context: ConversationExecutionContext) -> bool:
        return context.has_active_execution() or bool(self.conversation_delete_fence(context.conversation_id))

    def note_execution_activity(self, conversation_id: str) -> None:
        context = self.execution_contexts.context_for(conversation_id)
        if context is None or self._shutting_down:
            return
        context.unloading.sync(has_subscribers=self._has_conversation_subscribers(conversation_id),
            is_active=self._execution_is_active(context), now=asyncio.get_running_loop().time())
        event = self._execution_watch_events.get(context)
        if event is not None:
            event.set()
        if context.shutting_down or context in self._execution_watch_tasks:
            return
        if event is None:
            event = asyncio.Event()
            self._execution_watch_events[context] = event
            context.unloading = ThreadUnloadingState.create(
                has_subscribers=self._has_conversation_subscribers(conversation_id),
                is_active=self._execution_is_active(context), now=asyncio.get_running_loop().time(),
                delay=self._thread_unload_delay_seconds,
            )
        task = asyncio.create_task(self._watch_execution_context(context, event), name=f"thread-unload-watch:{conversation_id}")
        self._execution_watch_tasks[context] = task
        task.add_done_callback(_consume_task_result)

    async def _watch_execution_context(self, context: ConversationExecutionContext, event: asyncio.Event) -> None:
        try:
            while self.execution_contexts.is_current(context) and not self._shutting_down:
                event.clear()
                now = asyncio.get_running_loop().time()
                context.unloading.sync(has_subscribers=self._has_conversation_subscribers(context.conversation_id),
                    is_active=self._execution_is_active(context), now=now)
                target = context.unloading.unloading_target()
                if target is None:
                    await event.wait()
                    continue
                if target > now:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=target - now)
                    except TimeoutError:
                        pass
                    continue
                async with self.conversation_lifecycle_scope((context.conversation_id,)):
                    request = self.execution_contexts.try_begin_idle_unload(context,
                        has_subscribers=self._has_conversation_subscribers(context.conversation_id),
                        is_active=self._execution_is_active(context), now=asyncio.get_running_loop().time())
                    if request is not None:
                        await self._unload_execution_context(request, reason="thread_idle_unload")
                        return
        finally:
            if self._execution_watch_tasks.get(context) is asyncio.current_task():
                self._execution_watch_tasks.pop(context, None)
                self._execution_watch_events.pop(context, None)

    async def _unload_execution_context(self, request: ExecutionUnload, *, reason: str) -> bool:
        context = request.context
        try:
            current = self.execution_contexts.admission_for(asyncio.current_task())
            if current is not None and current.context is context:
                # A native /clear caller still owns its durable response, but
                # is no longer an input waiting to start the old core.
                self.execution_contexts.unbind_admission(current)
            if not await self.cancel_conversation_admissions(context.conversation_id):
                self.execution_contexts.unload_failed(request)
                return False
            await context.host.session_lifecycle.unload_conversation(context.conversation_id, reason=reason)
            pending = context.pending_tasks()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=10.0)
                if pending:
                    self.execution_contexts.unload_failed(request)
                    logger.warning("Thread %s unload timed out; its original execution remains loaded", context.conversation_id)
                    return False
            await context.thread_runtime.close_environments()
            removed = self.execution_contexts.finish_unload(request)
            if removed:
                self.execution_contexts.release_thread_runtime(context.thread_runtime)
        except BaseException:
            self.execution_contexts.unload_failed(request)
            raise
        if removed:
            for subscriptions in self._conversation_observers.values():
                subscriptions.discard(context.conversation_id)
            self._schedule_detached_host_release(context.owner)
        return removed

    async def cancel_conversation_admissions(self, conversation_id: str) -> bool:
        context = self.execution_contexts.context_for(conversation_id)
        if context is None:
            return True
        current = asyncio.current_task()
        tasks = {task for task in context.admission_tasks if task is not current and not task.done()}
        if not tasks:
            return True
        receipt = await cancel_and_drain_receipt(
            tasks, timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"accepted input for closing conversation {conversation_id}", owner=context.admission_tasks,
        )
        return receipt.pending == 0

    def core_borrows_reader_hydration(self, session: WebSocketSession) -> bool:
        owner = self.execution_contexts.retained_host(session.session_id)
        if owner is None:
            return False
        reader_id = session.active_conversation_id
        return (any(admission.context is None for admission in self.execution_contexts.admissions_for_host(owner))
            or any(context.conversation_id == reader_id and context.has_active_execution()
                for context in self.execution_contexts.contexts_for_host(owner)))

    async def unload_conversation_execution(self, conversation_id: str, *, reason: str) -> bool:
        context = self.execution_contexts.context_for(conversation_id)
        if context is None:
            return True
        request = self.execution_contexts.begin_unload(context)
        if request is None:
            return False
        watcher = self._execution_watch_tasks.get(context)
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        return await self._unload_execution_context(request, reason=reason)

    def _schedule_detached_host_release(self, owner: ExecutionHost) -> None:
        session_id = owner.origin.session_id
        if (self._shutting_down or owner.session.session_lifecycle.is_shutting_down
                or self._sessions.get(session_id) is owner.session
                or self.execution_contexts.contexts_for_host(owner) or self.execution_contexts.admissions_for_host(owner)
                or session_id in self._execution_host_shutdown_tasks):
            return
        borrowers = owner.session.session_lifecycle.llm_borrower_tasks()
        if borrowers:
            # Core unload does not turn an independent memory task into
            # foreground work or invalidate its original model generation.
            # There is no blocking host-shutdown task, so reconnect can still
            # attach to the original channel while this real borrower runs.
            for task in borrowers:
                task.add_done_callback(lambda _task: self._schedule_detached_host_release(owner))
            return

        async def release() -> None:
            try:
                retiring = self._disconnect_tasks.get(session_id)
                if retiring is not None and retiring is not asyncio.current_task():
                    await asyncio.shield(retiring)
                if (self.execution_contexts.retained_host(session_id) is not owner
                        or self._sessions.get(session_id) is owner.session
                        or self.execution_contexts.contexts_for_host(owner) or self.execution_contexts.admissions_for_host(owner)):
                    return
                await owner.session.session_lifecycle.shutdown(reason="execution_host_idle")
                tail = owner.session.event_outbox.persistence_tail
                if tail is not None:
                    await asyncio.shield(tail)
                self.execution_contexts.release_host(owner)
            finally:
                if self._execution_host_shutdown_tasks.get(session_id) is asyncio.current_task():
                    self._execution_host_shutdown_tasks.pop(session_id, None)
                if self._disconnect_tasks.get(session_id) is asyncio.current_task():
                    self._disconnect_tasks.pop(session_id, None)

        task = asyncio.create_task(release(), name=f"execution-host-release:{session_id}")
        self._execution_host_shutdown_tasks[session_id] = task
        task.add_done_callback(_consume_task_result)

    def execution_session_for_conversation(self, conversation_id: str) -> WebSocketSession | None:
        running = self.running_session_for_conversation(conversation_id)
        if running is not None:
            return running
        context = self.execution_contexts.context_for(conversation_id)
        if context is not None and not context.host.session_lifecycle.is_shutting_down:
            return context.host
        return None

    def session_for_input_origin(self, origin: str) -> WebSocketSession | None:
        return next((session for session in self.resource_sessions()
            if session.run_manager.durable_client_commands.path.stem == origin), None)

    def observed_conversation_owners(self, session: WebSocketSession) -> dict[str, WebSocketSession]:
        conversation_ids = set(self._conversation_observers.get(session, ()))
        if session.active_conversation_id:
            conversation_ids.add(session.active_conversation_id)
        return {conversation_id: owner for conversation_id in conversation_ids
            if (owner := self.execution_session_for_conversation(conversation_id)) is not None and owner is not session}

    def schedule_observer_runtime_updates(self, source: WebSocketSession) -> None:
        for observer, subscriptions in tuple(self._conversation_observers.items()):
            if observer is not source and any(self.execution_session_for_conversation(cid) is source for cid in subscriptions):
                observer.session_lifecycle.schedule_task_runtime_update()

    async def observe_conversation_stream(self, session: WebSocketSession, conversation_id: str) -> None:
        """Order the public history/current turn snapshot and subscription together."""
        from backend.ws.conversation_stream import frozen_stream_pages, public_thread_payload

        async with self.conversation_stream_lock(conversation_id):
            self._conversation_observers.setdefault(session, set()).add(conversation_id)
            self.note_execution_activity(conversation_id)
            owner = self.execution_session_for_conversation(conversation_id)
            if owner is None or owner is session:
                return
            # Re-read public history within the stream ordering boundary. A
            # completion between restore's disk read and subscription must not
            # disappear when no further delta will arrive.
            await self.send_observed_conversation_snapshot(session, conversation_id)
            stream = owner._conversation_streams.get(conversation_id)
            if stream and not stream.get("terminal_fenced") and owner.run_manager.running_task_for(conversation_id) is not None:
                for page in frozen_stream_pages(conversation_id, stream):
                    await session.event_outbox.send_payload(page.to_ws_message(),
                        connection_generation=session.connection_generation, log_context="thread.observe.snapshot", wait_for_delivery=False)
            for pending in tuple(owner.turn_wait_state.pending_approval_payloads.values()):
                if pending.get("conversation_id") != conversation_id:
                    continue
                projection = public_thread_payload(pending)
                if projection is not None:
                    await session.event_outbox.send_payload(projection, connection_generation=session.connection_generation,
                        log_context="thread.observe.request", wait_for_delivery=False)

    async def send_observed_conversation_snapshot(self, session: WebSocketSession, conversation_id: str) -> None:
        from backend.services.conversation_payload_service import restore_tool_artifact_sources
        from backend.services.session_restore_service import build_session_synced_payload

        if session.active_conversation_id != conversation_id:
            return
        view = await asyncio.to_thread(session.conversation_repo.get_conversation_view, conversation_id)
        if view is not None:
            view = await asyncio.to_thread(restore_tool_artifact_sources, view, session.artifact_store)
        if session.active_conversation_id != conversation_id:
            return
        snapshot = session.runtime_snapshot()
        provider = snapshot["capabilities"]["provider_capabilities"]
        cursor = session.event_outbox.current_replay_seq
        payload = build_session_synced_payload(
            {"session_id": session.session_id, "synced": True, "session": snapshot},
            protocol_version="1.0.0", active_conversation=view,
            active_conversation_id=conversation_id,
            workspace_root=session.session_lifecycle.current_workspace_root(),
            selected_model=session.selected_model, provider=session.provider,
            available_models=session.available_models, models_source=session.models_source,
            last_seq=cursor, current_seq=cursor, provider_id=provider.get("provider_id", ""),
            base_url=provider.get("base_url", ""), wire_api=provider.get("wire_api", ""),
        )
        await session.event_outbox.send_payload(payload, connection_generation=session.connection_generation,
            log_context="thread.observe.history", wait_for_delivery=False)

    async def publish_conversation_payload(self, source: WebSocketSession, payload: dict[str, Any]) -> None:
        from backend.ws.conversation_stream import public_thread_payload

        conversation_id = str(payload.get("conversation_id") or "")
        if not conversation_id or self.execution_session_for_conversation(conversation_id) is not source:
            return
        projection = public_thread_payload(payload)
        if projection is None:
            return
        for observer, subscriptions in tuple(self._conversation_observers.items()):
            if observer is source or conversation_id not in subscriptions or self._sessions.get(observer.session_id) is not observer:
                continue
            # Bypass send_event: observers do not acquire a run slot, execute
            # hooks, register waiters or mutate the original tool snapshot.
            await observer.event_outbox.send_payload(projection, connection_generation=observer.connection_generation,
                log_context="thread.observe.event", wait_for_delivery=False)
        if payload.get("type") == "user_message.queue.updated":
            source.session_lifecycle.schedule_task_runtime_update()

    async def publish_turn_input_admission(self, source: WebSocketSession, conversation_id: str) -> None:
        from backend.ws.conversation_stream import frozen_stream_pages

        if not any(observer is not source and conversation_id in subscriptions
                   for observer, subscriptions in self._conversation_observers.items()):
            return
        async with self.conversation_stream_lock(conversation_id):
            for observer, subscriptions in tuple(self._conversation_observers.items()):
                if conversation_id not in subscriptions or self._sessions.get(observer.session_id) is not observer:
                    continue
                await self.send_observed_conversation_snapshot(observer, conversation_id)
                state = source._conversation_streams.get(conversation_id)
                if state and not state.get("terminal_fenced") and observer.active_conversation_id == conversation_id:
                    for page in frozen_stream_pages(conversation_id, state):
                        await observer.event_outbox.send_payload(page.to_ws_message(), connection_generation=observer.connection_generation,
                            log_context="thread.observe.admitted", wait_for_delivery=False)

    @asynccontextmanager
    async def conversation_lifecycle_scope(self, conversation_ids: tuple[str, ...], *, exclusive: bool = False):
        """Serialize task resources; only global maintenance excludes all tasks."""
        # Reuse the manager's loop lifetime, including test-loop replacement.
        loop = asyncio.get_running_loop()
        if self._conversation_lifecycle_loop is not loop:
            if self._lifecycle_readers or self._lifecycle_writer or self._lifecycle_waiting_writers:
                raise RuntimeError("Conversation lifecycle loop changed during a mutation")
            self._conversation_lifecycle_loop = loop
            self._lifecycle_condition = asyncio.Condition()
            self._lifecycle_resource_locks.clear()
        condition = self._lifecycle_condition
        async with condition:
            if exclusive:
                self._lifecycle_waiting_writers += 1
                try:
                    await condition.wait_for(lambda: not self._lifecycle_writer and self._lifecycle_readers == 0)
                    self._lifecycle_writer = True
                finally:
                    self._lifecycle_waiting_writers -= 1
                    condition.notify_all()
            else:
                await condition.wait_for(lambda: not self._lifecycle_writer and self._lifecycle_waiting_writers == 0)
                self._lifecycle_readers += 1
        try:
            async with AsyncExitStack() as stack:
                for conversation_id in sorted(set(conversation_ids)):
                    lock = self._lifecycle_resource_locks.get(conversation_id)
                    if lock is None:
                        lock = asyncio.Lock()
                        self._lifecycle_resource_locks[conversation_id] = lock
                    await stack.enter_async_context(lock)
                yield
        finally:
            async with condition:
                if exclusive:
                    self._lifecycle_writer = False
                else:
                    self._lifecycle_readers -= 1
                condition.notify_all()

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
        self.note_execution_activity(owner)

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
        llm: LLMAdapter | None,
        artifact_store: ArtifactStore,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        config: AppConfig,
        skill_manager: Any | None = None,
        runtime_skill_roots: Any | None = None,
        skill_executor: Any | None = None,
        memory_manager: Any | None = None,
        mcp_manager: Any = _SESSION_MCP_MANAGER_UNSET,
        environment_manager: Any | None = None,
    ) -> tuple[WebSocketSession, int]:
        from backend.ws.handler import (
            CONVERSATION_DATA_DIR,
            WS_EVENT_REPLAY_MAX,
            WebSocketSession,
        )

        session = None
        session_id = ""
        try:
            try:
                await websocket.accept(subprotocol=_websocket_accept_subprotocol(websocket))
                requested_session_id = (websocket.query_params.get("session_id") or "").strip()
                window_instance_id = str(websocket.query_params.get("window_instance_id") or "")
                window_owner_id = str(websocket.query_params.get("window_owner_id") or "")
                if requested_session_id and not SESSION_ID_PATTERN.fullmatch(requested_session_id):
                    await websocket.close(code=1008, reason="invalid session_id")
                    raise WebSocketDisconnect(code=1008)
                window_binding_token = await _verified_window_binding(websocket, window_instance_id, window_owner_id)
                session_id = requested_session_id or f"session_{uuid.uuid4().hex}"
                existing_session = self._sessions.get(session_id)
                retained = self.execution_contexts.retained_host(session_id)
                if ((existing_session is not None and (existing_session.window_instance_id, existing_session.window_owner_id)
                        != (window_instance_id, window_owner_id))
                        or (retained is not None and (retained.origin.window_instance_id, retained.origin.window_owner_id)
                            != (window_instance_id, window_owner_id))):
                    await websocket.close(code=1008, reason="session belongs to another window")
                    raise WebSocketDisconnect(code=1008)
                self._connecting_websockets[session_id] = websocket
                replay_state = None
                if session_id not in self._sessions:
                    retiring_task = self._disconnect_tasks.get(session_id)
                    if retiring_task is not None:
                        await asyncio.shield(retiring_task)
                    shutdown_task = self._execution_host_shutdown_tasks.get(session_id)
                    if shutdown_task is not None:
                        await asyncio.shield(shutdown_task)
                    retained_session = self.execution_contexts.host_for_reconnect(session_id,
                        window_instance_id=window_instance_id, window_owner_id=window_owner_id)
                    if retained_session is not None:
                        if retained_session.session_lifecycle.is_shutting_down:
                            await websocket.close(code=1013, reason="original execution resources are still closing")
                            raise WebSocketDisconnect(code=1013)
                        self._sessions[session_id] = retained_session
                    else:
                        replay_state = await asyncio.to_thread(
                            EventOutbox.load_replay_state,
                            session_id=session_id,
                            replay_root=Path(CONVERSATION_DATA_DIR).parent / "ws-event-log",
                            replay_limit=WS_EVENT_REPLAY_MAX,
                        )
                if self._connecting_websockets.get(session_id) is not websocket:
                    await websocket.close(code=1012, reason="replaced by newer connection")
                    raise WebSocketDisconnect(code=1012)

                session = self._sessions.get(session_id)
                if session is not None and (session.window_instance_id, session.window_owner_id) != (window_instance_id, window_owner_id):
                    await websocket.close(code=1008, reason="session belongs to another window")
                    raise WebSocketDisconnect(code=1008)
                if session_id in self._disconnect_tasks:
                    self._disconnect_tasks[session_id].cancel()
                    del self._disconnect_tasks[session_id]
                    logger.info(f"Cancelled disconnect cleanup task for session {session_id} due to reconnection")

                if session is not None:
                    self._discard_heartbeat_reporter(session, session.connection_generation)
                    previous_ws, generation = session.attach_websocket(websocket,
                        window_instance_id=window_instance_id, window_owner_id=window_owner_id,
                        window_binding_token=window_binding_token)
                    watcher = session.session_lifecycle.file_watcher
                    if watcher is None or not watcher.is_running():
                        session.session_lifecycle.start_file_watcher()
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
                    return session, generation

                session = WebSocketSession(
                    session_id=session_id,
                    websocket=websocket,
                    llm=llm,
                    artifact_store=artifact_store,
                    tool_registry=tool_registry,
                    permission_checker=permission_checker,
                    config=config,
                    skill_manager=skill_manager,
                    runtime_skill_roots=runtime_skill_roots,
                    skill_executor=skill_executor,
                    memory_manager=memory_manager,
                    mcp_manager=mcp_manager,
                    environment_manager=environment_manager,
                    ws_manager=self,
                    replay_state=replay_state,
                    window_instance_id=window_instance_id,
                    window_owner_id=window_owner_id,
                    window_binding_token=window_binding_token,
                )
                self._sessions[session_id] = session
                _invalidate_runtime_status_cache()
                return session, session.connection_generation
            finally:
                if self._connecting_websockets.get(session_id) is websocket:
                    self._connecting_websockets.pop(session_id)
                await _dispose_unadopted_connection_resources(
                    llm,
                    artifact_store,
                    adopted_session=session,
                )
        except BaseException:
            if session is not None and session.ws is websocket:
                self.disconnect(session_id, connection_generation=session.connection_generation)
            raise

    def get_session(self, session_id: str) -> WebSocketSession | None:
        return self._sessions.get(session_id)

    def iter_sessions(self) -> tuple[WebSocketSession, ...]:
        return tuple(self._sessions.values())

    def resource_sessions(self) -> tuple[WebSocketSession, ...]:
        """Actual resource owners, including executors whose transport retired."""
        return tuple(dict.fromkeys([*self._sessions.values(),
            *(owner.session for owner in self.execution_contexts.retained_hosts())]))

    def execution_host_for_session(self, session_id: str) -> WebSocketSession | None:
        owner = self.execution_contexts.retained_host(session_id)
        return owner.session if owner is not None else self._sessions.get(session_id)

    def running_session_for_conversation(self, conversation_id: str) -> WebSocketSession | None:
        for session in self.resource_sessions():
            if not session.session_lifecycle.is_shutting_down and conversation_id in session.run_manager.run_tasks:
                return session
        return None

    def disconnect(self, session_id: str, *, connection_generation: int | None = None) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if connection_generation is not None and session.connection_generation != connection_generation:
            return
        self._discard_heartbeat_reporter(session, session.connection_generation)
        session.mark_disconnected()
        self._remove_transport_subscriptions(session)
        _invalidate_runtime_status_cache()

        async def delayed_cleanup():
            try:
                await asyncio.sleep(30.0)
                if session_id in self._sessions and self._sessions[session_id] is session:
                    # The socket expires at the same 30s boundary. Its loaded
                    # core is retained separately, with the original writer.
                    self._sessions.pop(session_id, None)
                    _invalidate_runtime_status_cache()
                    await self._retire_session_transport(session, reason="disconnect_timeout")
                    persistence_task = session.event_outbox.persistence_tail
                    if persistence_task is not None:
                        await asyncio.shield(persistence_task)
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

    async def _retire_session_transport(self, session: WebSocketSession, *, reason: str) -> None:
        owner = self.execution_contexts.retained_host(session.session_id)
        if owner is None:
            owner = self.execution_contexts.retain_host(session)
        if (not self.execution_contexts.contexts_for_host(owner)
                and not self.execution_contexts.admissions_for_host(owner)
                and not session.session_lifecycle.llm_borrower_tasks()):
            await session.session_lifecycle.shutdown(reason=reason)
            tail = session.event_outbox.persistence_tail
            if tail is not None:
                await asyncio.shield(tail)
            self.execution_contexts.release_host(owner)
            return
        await session.session_lifecycle.retire_transport()
        self._schedule_detached_host_release(owner)

    async def shutdown_session(
        self,
        session_id: str,
        *,
        reason: str = "session_shutdown",
    ) -> bool:
        """Close one transport; loaded core execution keeps its original owner."""

        clean_id = str(session_id or "").strip()
        if not clean_id:
            return False
        session = self._sessions.pop(clean_id, None)
        disconnect_task = self._disconnect_tasks.get(clean_id)
        if session is None:
            if disconnect_task is not None and not disconnect_task.done():
                await asyncio.shield(asyncio.gather(disconnect_task, return_exceptions=True))
            return False
        if disconnect_task is not None and not disconnect_task.done():
            disconnect_task.cancel()
        session.mark_disconnected()
        self._remove_transport_subscriptions(session)
        _invalidate_runtime_status_cache()

        async def shutdown_and_close() -> None:
            try:
                try:
                    await self._retire_session_transport(session, reason=reason)
                except Exception:
                    logger.exception("Failed to shut down websocket session %s", clean_id)
                persistence_task = session.event_outbox.persistence_tail
                if persistence_task is not None:
                    await asyncio.shield(persistence_task)
                websocket = session.ws
                try:
                    await websocket.close(code=1000, reason=reason)
                except Exception:
                    logger.debug(
                        "Websocket close failed after session shutdown for %s",
                        clean_id,
                        exc_info=True,
                    )
            finally:
                if self._disconnect_tasks.get(clean_id) is asyncio.current_task():
                    self._disconnect_tasks.pop(clean_id, None)

        shutdown_task = asyncio.create_task(
            shutdown_and_close(), name=f"session-shutdown:{clean_id}",
        )
        self._disconnect_tasks[clean_id] = shutdown_task
        await asyncio.shield(shutdown_task)
        return True

    async def shutdown_execution_host(self, session_id: str, *, reason: str = "execution_shutdown") -> bool:
        """Explicit harness/app shutdown, separate from connection retirement."""
        existing = self._execution_host_shutdown_tasks.get(session_id)
        if existing is not None:
            await asyncio.shield(existing)
        session = self.execution_host_for_session(session_id)
        if session is None:
            return False
        # A retained core can outlive the ASGI transport. Protocol states may
        # still say CONNECTED after its channel has closed; only the currently
        # attached, live transport belongs to this shutdown's close operation.
        websocket = session.ws if self._sessions.get(session_id) is session and session.is_connected else None
        retiring = self._disconnect_tasks.get(session_id)
        if retiring is not None and not retiring.done():
            if self._sessions.get(session_id) is session:
                retiring.cancel()
            await asyncio.gather(retiring, return_exceptions=True)
        if self._sessions.get(session_id) is session:
            self._sessions.pop(session_id)
        session.mark_disconnected()
        self._remove_transport_subscriptions(session)
        owner = self.execution_contexts.retain_host(session)

        async def shutdown_and_close() -> None:
            try:
                contexts = self.execution_contexts.contexts_for_host(owner)
                watchers = [self._execution_watch_tasks[context] for context in contexts
                    if context in self._execution_watch_tasks]
                for watcher in watchers:
                    watcher.cancel()
                if watchers:
                    await asyncio.gather(*watchers, return_exceptions=True)
                requests = [context.unloading_request or self.execution_contexts.begin_unload(context) for context in contexts]
                try:
                    await session.session_lifecycle.shutdown(reason=reason)
                except BaseException:
                    for request in requests:
                        if request is not None:
                            self.execution_contexts.unload_failed(request)
                    raise
                pending = set().union(*(context.pending_tasks() for context in contexts))
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=10.0)
                if pending:
                    for request in requests:
                        if request is not None:
                            self.execution_contexts.unload_failed(request)
                    raise RuntimeError(f"Execution host {session_id} still has undrained core tasks")
                for request in requests:
                    if request is not None:
                        await request.context.thread_runtime.close_environments()
                        self.execution_contexts.finish_unload(request)
                        self.execution_contexts.release_thread_runtime(request.context.thread_runtime)
                tail = session.event_outbox.persistence_tail
                if tail is not None:
                    await asyncio.shield(tail)
                self.execution_contexts.release_host(owner)
                if (websocket is not None and websocket.client_state == WebSocketState.CONNECTED
                        and websocket.application_state == WebSocketState.CONNECTED):
                    await websocket.close(code=1000, reason=reason)
            finally:
                if self._execution_host_shutdown_tasks.get(session_id) is asyncio.current_task():
                    self._execution_host_shutdown_tasks.pop(session_id, None)
                if self._disconnect_tasks.get(session_id) is asyncio.current_task():
                    self._disconnect_tasks.pop(session_id, None)

        task = asyncio.create_task(shutdown_and_close(), name=f"execution-host-shutdown:{session_id}")
        self._execution_host_shutdown_tasks[session_id] = task
        self._disconnect_tasks[session_id] = task
        _invalidate_runtime_status_cache()
        await asyncio.shield(task)
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
        """Drain attached and detached execution hosts before loop teardown."""
        self._shutting_down = True
        cleanup_tasks = dict(self._disconnect_tasks)
        errors: list[BaseException] = []
        for session_id, task in cleanup_tasks.items():
            if session_id in self._sessions and not task.done():
                task.cancel()
        if cleanup_tasks:
            cleanup_results = await asyncio.shield(asyncio.gather(*cleanup_tasks.values(), return_exceptions=True))
            errors.extend(result for result in cleanup_results if isinstance(result, Exception))

        # Session shutdown cancels session-owned command tasks. Destructive
        # conversation deletion is manager-owned and must be settled before
        # those session resources disappear.
        await self._drain_conversation_delete_tasks()

        releases = tuple(self._execution_host_shutdown_tasks.values())
        if releases:
            release_results = await asyncio.gather(*releases, return_exceptions=True)
            errors.extend(result for result in release_results if isinstance(result, BaseException))
        session_ids = list(dict.fromkeys([*self._sessions,
            *(owner.origin.session_id for owner in self.execution_contexts.retained_hosts())]))
        if session_ids:
            results = await asyncio.gather(
                *(
                    self.shutdown_execution_host(session_id, reason=reason)
                    for session_id in session_ids
                ),
                return_exceptions=True,
            )
            for session_id, result in zip(session_ids, results):
                if isinstance(result, BaseException):
                    result.add_note(f"Original execution host: {session_id}")
                    errors.append(result)
        if errors:
            raise BaseExceptionGroup("Could not drain execution hosts", errors)
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
        self._connecting_websockets.clear()
        for task in list(self._disconnect_tasks.values()):
            if not task.done():
                task.cancel()
        self._disconnect_tasks.clear()
        for task in (*self._execution_watch_tasks.values(), *self._execution_host_shutdown_tasks.values()):
            if not task.done():
                task.cancel()
        self._execution_watch_tasks.clear()
        self._execution_watch_events.clear()
        self._execution_host_shutdown_tasks.clear()
        self.execution_contexts = ConversationExecutionRegistry()
        self._shutting_down = False
        from backend.agent.conversation_query_guard import conversation_query_guards

        conversation_query_guards().open_admission()
        self._heartbeat_renderer_states.clear()
        self._heartbeat_renderer_requests.clear()
        for task in list(self._conversation_delete_tasks):
            if not task.done():
                task.cancel()
        self._conversation_delete_tasks.clear()
        self._conversation_delete_release_tasks.clear()
        self._conversation_delete_cleanup_tasks.clear()
        self._conversation_observers.clear()
        with self._conversation_resource_lock:
            self._attachment_upload_owners.clear()
            self._conversation_delete_fences.clear()
        if not (self._lifecycle_readers or self._lifecycle_writer or self._lifecycle_waiting_writers):
            self._conversation_lifecycle_loop = None
            self._lifecycle_condition = asyncio.Condition()
            self._lifecycle_resource_locks.clear()
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
