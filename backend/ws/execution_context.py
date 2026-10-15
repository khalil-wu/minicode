"""Loaded conversation ownership, independent from a WebSocket binding.

This registry is owned by the manager's event loop. It retains the original
executor; it does not clone a session, its tool context, or its resources.
Transport authentication, subscription locking and actual shutdown/drain stay
with their existing callers. No timers or transport mutations happen here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


# codex-rs/core/src/config/mod.rs: thread_unload_delay_secs defaults to 60.
DEFAULT_THREAD_UNLOAD_DELAY_SECONDS = 60.0


@dataclass(frozen=True)
class ExecutionOrigin:
    session_id: str
    window_instance_id: str
    window_owner_id: str

    def require_same_window(self, *, window_instance_id: str, window_owner_id: str) -> None:
        # The caller has already verified the native binding token. An owner
        # name alone must not let a new window instance reuse this executor.
        if (window_instance_id, window_owner_id) != (self.window_instance_id, self.window_owner_id):
            raise ValueError("The retained execution belongs to another window instance")


@dataclass(eq=False, frozen=True)
class ExecutionHost:
    session: WebSocketSession
    origin: ExecutionOrigin


@dataclass(eq=False)
class ExecutionAdmission:
    owner: ExecutionHost
    task: asyncio.Task[Any]
    context: ConversationExecutionContext | None = None


@dataclass
class ThreadUnloadingState:
    """Source thread_lifecycle.rs UnloadingState, using monotonic seconds."""

    delay: float
    has_subscribers: bool
    subscribers_changed_at: float
    is_active: bool
    activity_changed_at: float

    @classmethod
    def create(
        cls, *, has_subscribers: bool, is_active: bool, now: float,
        delay: float = DEFAULT_THREAD_UNLOAD_DELAY_SECONDS,
    ) -> ThreadUnloadingState:
        return cls(delay, has_subscribers, now, is_active, now)

    def sync(self, *, has_subscribers: bool, is_active: bool, now: float) -> None:
        if self.has_subscribers != has_subscribers:
            self.has_subscribers = has_subscribers
            self.subscribers_changed_at = now
        if self.is_active != is_active:
            self.is_active = is_active
            self.activity_changed_at = now

    def unloading_target(self) -> float | None:
        if self.has_subscribers or self.is_active:
            return None
        return max(self.subscribers_changed_at, self.activity_changed_at) + self.delay

    def should_unload_now(self, *, now: float) -> bool:
        target = self.unloading_target()
        return target is not None and target <= now

    def note_thread_activity_observed(self, *, now: float) -> None:
        # Source rechecks the actual agent when its watch still says inactive.
        # Observed running work restarts that inactive interval, not the socket
        # retirement deadline and not another thread's interval.
        if not self.is_active:
            self.activity_changed_at = now


@dataclass(eq=False, frozen=True)
class ExecutionUnload:
    context: ConversationExecutionContext


@dataclass(eq=False)
class ConversationExecutionContext:
    conversation_id: str
    owner: ExecutionHost
    unloading: ThreadUnloadingState
    _unload: ExecutionUnload | None = field(default=None, init=False, repr=False)
    _admission_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    _lifecycle_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)

    @property
    def host(self) -> WebSocketSession:
        return self.owner.session

    @property
    def unloading_request(self) -> ExecutionUnload | None:
        return self._unload

    @property
    def shutting_down(self) -> bool:
        return self._unload is not None

    def track_admission_task(self, task: asyncio.Task[Any]) -> None:
        if self.shutting_down:
            raise RuntimeError("This conversation execution is closing")
        self._admission_tasks.add(task)
        task.add_done_callback(self._admission_tasks.discard)

    @property
    def admission_tasks(self) -> set[asyncio.Task[Any]]:
        return self._admission_tasks

    def track_lifecycle_task(self, task: asyncio.Task[Any]) -> None:
        self._lifecycle_tasks.add(task)
        task.add_done_callback(self._lifecycle_tasks.discard)

    def has_active_execution(self) -> bool:
        return self.conversation_id in self.host.run_manager.run_tasks or bool(self.pending_tasks())

    def pending_tasks(self) -> frozenset[asyncio.Task[Any]]:
        # SessionRunManager remains the sole owner of the concrete run handles
        # and their borrowed callbacks. These are references to those objects,
        # not a second registry of task ids or cancellation state.
        tasks = self._admission_tasks | self._lifecycle_tasks | self.host.run_manager.lifecycle_cleanup_tasks_for(self.conversation_id)
        tasks.update(self.host.run_manager.notification_tasks_for(self.conversation_id))
        from backend.agent.runtime import default_runtime_if_initialized

        runtime = default_runtime_if_initialized()
        if runtime is not None:
            tasks.update(runtime.subagent_tasks_for_conversation(self.conversation_id))
        running = self.host.run_manager.run_tasks.get(self.conversation_id)
        if running is not None:
            tasks.add(running)
        return frozenset(task for task in tasks if not task.done())


class ConversationExecutionRegistry:
    """Strong loaded-thread/host ownership with identity-scoped release."""

    def __init__(self) -> None:
        self._hosts: dict[str, ExecutionHost] = {}
        self._conversations: dict[str, ConversationExecutionContext] = {}
        self._admissions: dict[asyncio.Task[Any], ExecutionAdmission] = {}

    def retain_admission(self, session: WebSocketSession, task: asyncio.Task[Any]) -> ExecutionAdmission:
        current = self._admissions.get(task)
        if current is not None:
            if current.owner.session is not session:
                raise RuntimeError("This admission task already belongs to another submitting host")
            return current
        admission = ExecutionAdmission(self.retain_host(session), task)
        self._admissions[task] = admission
        return admission

    def admission_for(self, task: asyncio.Task[Any]) -> ExecutionAdmission | None:
        return self._admissions.get(task)

    def bind_admission(self, admission: ExecutionAdmission, context: ConversationExecutionContext) -> None:
        if self._admissions.get(admission.task) is not admission or not self.is_current(context):
            raise RuntimeError("This admission no longer owns its execution boundary")
        if admission.context is not None and admission.context is not context:
            admission.context._admission_tasks.discard(admission.task)
        context.track_admission_task(admission.task)
        admission.context = context

    def finish_admission(self, admission: ExecutionAdmission) -> bool:
        if self._admissions.get(admission.task) is not admission:
            return False
        self._admissions.pop(admission.task)
        if admission.context is not None:
            admission.context._admission_tasks.discard(admission.task)
        return True

    def unbind_admission(self, admission: ExecutionAdmission) -> None:
        if self._admissions.get(admission.task) is not admission:
            raise RuntimeError("This input no longer owns its admission")
        if admission.context is not None:
            admission.context._admission_tasks.discard(admission.task)
            admission.context = None

    def admissions_for_host(self, owner: ExecutionHost) -> tuple[ExecutionAdmission, ...]:
        return tuple(admission for admission in self._admissions.values() if admission.owner is owner)

    def retain_host(self, session: WebSocketSession) -> ExecutionHost:
        owner = self._hosts.get(session.session_id)
        if owner is not None:
            if owner.session is not session:
                raise RuntimeError("This session id already owns a different execution host")
            owner.origin.require_same_window(window_instance_id=session.window_instance_id, window_owner_id=session.window_owner_id)
            return owner
        owner = ExecutionHost(session, ExecutionOrigin(session.session_id, session.window_instance_id, session.window_owner_id))
        self._hosts[session.session_id] = owner
        return owner

    def host_for_reconnect(
        self, session_id: str, *, window_instance_id: str, window_owner_id: str,
    ) -> WebSocketSession | None:
        owner = self._hosts.get(session_id)
        if owner is None:
            return None
        owner.origin.require_same_window(window_instance_id=window_instance_id, window_owner_id=window_owner_id)
        # Reuse this exact host/outbox/durable origin. The caller attaches the
        # newly authenticated socket; this method never replaces a run's token.
        return owner.session

    def load(
        self, conversation_id: str, session: WebSocketSession, *,
        has_subscribers: bool, is_active: bool, now: float,
        unload_delay: float = DEFAULT_THREAD_UNLOAD_DELAY_SECONDS,
    ) -> ConversationExecutionContext:
        current = self._conversations.get(conversation_id)
        if current is not None:
            if current.host is not session:
                raise RuntimeError("This conversation already has a different execution owner")
            if current.shutting_down:
                raise RuntimeError("This conversation execution is closing")
            current.owner.origin.require_same_window(window_instance_id=session.window_instance_id, window_owner_id=session.window_owner_id)
            current.unloading.sync(has_subscribers=has_subscribers, is_active=is_active, now=now)
            return current
        context = ConversationExecutionContext(conversation_id, self.retain_host(session), ThreadUnloadingState.create(
            has_subscribers=has_subscribers, is_active=is_active, now=now, delay=unload_delay))
        self._conversations[conversation_id] = context
        return context

    def context_for(self, conversation_id: str) -> ConversationExecutionContext | None:
        return self._conversations.get(conversation_id)

    def is_current(self, context: ConversationExecutionContext) -> bool:
        return self._conversations.get(context.conversation_id) is context

    def contexts(self) -> tuple[ConversationExecutionContext, ...]:
        return tuple(self._conversations.values())

    def retained_hosts(self) -> tuple[ExecutionHost, ...]:
        return tuple(self._hosts.values())

    def retained_host(self, session_id: str) -> ExecutionHost | None:
        return self._hosts.get(session_id)

    def contexts_for_host(self, owner: ExecutionHost) -> tuple[ConversationExecutionContext, ...]:
        return tuple(context for context in self._conversations.values() if context.owner is owner)

    def begin_unload(self, context: ConversationExecutionContext) -> ExecutionUnload | None:
        if not self.is_current(context) or context.shutting_down:
            return None
        request = ExecutionUnload(context)
        context._unload = request
        return request

    def try_begin_idle_unload(
        self, context: ConversationExecutionContext, *, has_subscribers: bool,
        is_active: bool, now: float,
    ) -> ExecutionUnload | None:
        if not self.is_current(context) or context.shutting_down:
            return None
        context.unloading.sync(has_subscribers=has_subscribers, is_active=is_active, now=now)
        if not context.unloading.should_unload_now(now=now):
            return None
        if context.has_active_execution():
            context.unloading.note_thread_activity_observed(now=now)
            return None
        return self.begin_unload(context)

    def unload_failed(self, request: ExecutionUnload) -> bool:
        """Source SubmitFailed/TimedOut retains the original loaded runtime."""
        context = request.context
        if not self.is_current(context) or context._unload is not request:
            return False
        # Clear only this pending unload. A newly installed listener creates its
        # own UnloadingState snapshot; this is not an automatic retry/timer.
        context._unload = None
        return True

    def finish_unload(self, request: ExecutionUnload) -> bool:
        context = request.context
        if not self.is_current(context) or context._unload is not request:
            return False
        if context.pending_tasks():
            raise RuntimeError("Execution unload has undrained tasks")
        self._conversations.pop(context.conversation_id)
        return True

    def release_host(self, owner: ExecutionHost) -> bool:
        """Release only after the caller drained that exact host's resources."""
        if self._hosts.get(owner.origin.session_id) is not owner:
            return False
        if self.contexts_for_host(owner):
            raise RuntimeError("The execution host still owns loaded conversations")
        if self.admissions_for_host(owner):
            raise RuntimeError("The execution host still owns accepted admissions")
        self._hosts.pop(owner.origin.session_id)
        return True
