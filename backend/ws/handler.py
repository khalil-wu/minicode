from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

from backend.atomic_io import canonical_file_path_key
from backend.agent.context import ContextBuilder
from backend.agent.loop_session import mcp_registry_version
from backend.agent.query_engine import QueryEngine
from backend.agent.message import AgentEvent, UserCommand
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    await_with_deadline,
    cancel_and_drain,
    cancel_and_drain_receipt,
    _consume_task_result,
)
from backend.attachments.store import AttachmentStore
from backend.artifact.store import ArtifactStore
from backend.checkpoint import CheckpointManager
from backend.commands.registry import CommandRegistry
from backend.config import AppConfig, get_available_models, get_llm_provider, get_models_source
from backend.conversations.models import DEFAULT_CONVERSATION_PERMISSION_MODE
from backend.conversations.public_projection import (
    project_public_conversation,
    project_public_conversation_summary,
)
from backend.conversations.repository import CONVERSATION_DATA_DIR, ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.profiles import (
    permission_profile_for_mode,
    sandbox_capability_for_context,
    sandbox_status_for,
    workspace_scope_for,
)
from backend.tasks.manager import TaskManager
from backend.terminal.session import TerminalSessionManager
from backend.terminal.manager import BackgroundCommandManager, BackgroundCommand
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import (
    SessionAgentRunnerMixin,
    _TURN_MESSAGE_SCOPED_EVENT_TYPES,
    _llm_adapter_cache_key,
)
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.client_command_log import ClientCommandDedupStore, _clean_command_id
from backend.ws.command_handlers import SessionCommandHandlersMixin
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.event_log import (
    WebSocketReplayEventStore,
    is_raw_provider_reasoning_event,
    sanitize_ws_live_payload,
    sanitize_ws_replay_payload,
)
from backend.ws.fork_registry import ForkRegistry
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin
from backend.ws.run_manager import SessionRunManager
from backend.ws.stream_state import apply_stream_event
from backend.ws.payload_contracts import (
    is_non_replayable_event_type,
    validate_session_projection_payload,
)
from backend.ws.utils import (
    build_effective_transcript_content,
    build_effective_user_message,
    build_summary_from_transcript,
    normalize_attachment_payloads,
    normalize_permission_mode,
)
from backend.workspace.file_watcher import WorkspaceFileWatcher
from backend.workspace.state import clear_active_workspace_root, get_active_workspace_root
from backend.api.auth import _websocket_accept_subprotocol

logger = logging.getLogger(__name__)
_SESSION_MCP_MANAGER_UNSET = object()

MAX_PENDING_COMMAND_TASKS = 100
COMMAND_BACKLOG_ERROR_INTERVAL_SECONDS = 2.0
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
WS_EVENT_REPLAY_MAX = 1000
# Claude Code's WebSocket/bridge duplicate-delivery safety net uses a bounded
# 2,000-entry UUID ring.  Client command ids serve the same replay boundary in
# MiniCode's WebSocket host, so keep the capacity aligned rather than using an
# unrelated local default.
RECENT_CLIENT_COMMAND_IDS_MAX = 2_000

_NOTIFICATION_HOOK_EVENT_TYPES = frozenset({
    "system_notice",
    "error",
    "approval_request",
    "approval.file_diff",
    "ask_user",
    "approval.cancelled",
    "idle_prompt",
    "auth_success",
    "elicitation_dialog",
    "elicitation_complete",
    "elicitation_response",
})

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
        # The delete handler acquires its own process-wide fence atomically;
        # rejecting here would turn a useful duplicate-delete response into a
        # generic ingress error.
        return ()
    if command_type == "memory.reset":
        manager = getattr(session, "_ws_manager", None)
        fenced_ids = getattr(manager, "conversation_delete_fenced_ids", None)
        if callable(fenced_ids):
            return tuple(str(item or "").strip() for item in fenced_ids() if str(item or "").strip())
        active = str(getattr(session, "active_conversation_id", "") or "").strip()
        return (active,) if active else ()
    if command_type == "conversation.create":
        # Creation without activation has no dependency on an existing
        # conversation. Activation does: it can clear the current runtime and
        # switch a renderer away from the conversation being deleted.
        if not bool(data.get("activate")):
            return ()
        active = str(getattr(session, "active_conversation_id", "") or "").strip()
        return (active,) if active else ()
    if not _is_conversation_lifecycle_command(command_type):
        return ()

    targets: set[str] = set()
    for key in (
        "conversation_id",
        "conversationId",
        "preferred_conversation_id",
        "preferredConversationId",
        "source_conversation_id",
        "sourceConversationId",
        "target_conversation_id",
        "targetConversationId",
        "parent_conversation_id",
        "parentConversationId",
    ):
        value = str(data.get(key) or "").strip()
        if value:
            targets.add(value)
    if not targets:
        active = str(getattr(session, "active_conversation_id", "") or "").strip()
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

def _invalidate_runtime_status_cache() -> None:
    try:
        from backend.api import _state

        _state.invalidate_status_cache()
    except Exception:
        logger.debug("Failed to invalidate status cache after websocket runtime change", exc_info=True)


CONVERSATION_SCOPED_EVENT_TYPES = {
    "artifact_content",
    "item.started",
    "agent_message.delta",
    "item.completed",
    "agent.run.started",
    "agent.run.completed",
    "agent.item",
    "user_message.queue.updated",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "tool_result",
    "agent.progress",
    "runtime.span",
    "task.update",
    "approval_request",
    "permission.decision",
    "approval.cancelled",
    "approval.file_diff",
    "ask_user",
    "context_usage",
    "context_compacted",
    "context_forked",
    "context_ledger",
    "context_side_query_result",
    "budget_update",
    "budget.warning",
    "command_output_chunk",
    "artifact.preview",
    "done",
    "stream_resume",
    "stream_event",
    "rate_limit",
    "session.state_changed",
    "conversation.hydration.updated",
    "conversation.compaction.updated",
    "conversation.summary.updated",
    "goal.updated",
    "turn.plan.updated",
    "turn.diff.updated",
    "subagent.start",
    "subagent.event",
    "subagent.progress",
    "subagent.done",
    "citation.add",
    "inspector.update",
    "control_request",
    "checkpoint.created",
    "checkpoint.list",
    "checkpoint.rewound",
    "checkpoint.run.list",
    "checkpoint.run.resume",
    "file.changed",
    "git.pr_status",
    "terminal.output",
    "terminal.exit",
    "terminal.created",
    "terminal.killed",
    "terminal.list",
    "terminal.snapshot",
    "terminal.resized",
    "background.started",
    "background.stalled",
    "background.completed",
    "workspace.imported",
    "system_notice",
    "parent.notifications",
    "preview.servers.updated",
    "preview.server.detected",
    "preview.server.stopped",
    "preview.navigated",
    "preview.refreshed",
    "preview.launch.config",
    "preview.launch.started",
    "preview.launch.stopped",
    "preview.server.ready",
    "preview.server.output",
    "preview.server.crashed",
    "preview.server.unhealthy",
    "preview.verified",
}


def _requires_conversation_owner(event_type: str, payload: dict[str, Any]) -> bool:
    """Return whether this concrete wire shape must carry a conversation owner.

    ``task.update`` has two intentional variants: turn-owned todo mutations and
    a session-wide runtime snapshot emitted by ``TaskManager``.  Treating the
    event name alone as conversation-scoped silently dropped the latter during
    restore and task lifecycle changes.  Keep strict owner enforcement for the
    todo form while allowing only the explicit ``session`` snapshot form to be
    global.
    """

    if event_type not in CONVERSATION_SCOPED_EVENT_TYPES:
        return False
    if event_type == "task.update" and isinstance(payload.get("session"), dict):
        return False
    return True

WORKSPACE_SCOPED_EVENT_TYPES = {
    "checkpoint.created",
    "checkpoint.list",
    "checkpoint.rewound",
    "checkpoint.run.list",
    "checkpoint.run.resume",
    "file.changed",
    "git.pr_status",
    "diff.git_working_tree",
    "diff.git_staged",
    "diff.git_stage_file",
    "diff.git_unstage_file",
    "diff.git_stage_all",
    "diff.git_unstage_all",
    "diff.git_revert_file",
    "workspace.imported",
    "preview.servers.updated",
    "preview.server.detected",
    "preview.server.stopped",
    "preview.navigated",
    "preview.refreshed",
    "preview.launch.config",
    "preview.launch.started",
    "preview.launch.stopped",
    "preview.server.ready",
    "preview.server.output",
    "preview.server.crashed",
    "preview.server.unhealthy",
    "preview.verified",
}

# Backward-compatible re-export for existing tests and callers that still
# import the helper from backend.ws.handler after the ws.utils extraction.
_build_effective_user_message = build_effective_user_message


class WebSocketSession(
    SessionCommandHandlersMixin,
    SessionPermissionRuntimeMixin,
    SessionApprovalRuntimeMixin,
    SessionAgentRunnerMixin,
):
    """A websocket-bound runtime session with a persistent conversation store."""

    def __init__(
        self,
        session_id: str,
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
    ) -> None:
        self.session_id = session_id
        self.ws = websocket
        self._connection_generation = 1
        self._event_instance_id = uuid.uuid4().hex
        self._ws_event_store = WebSocketReplayEventStore(
            session_id=session_id,
            root_dir=Path(CONVERSATION_DATA_DIR).parent / "ws-event-log",
        )
        self._client_command_store = ClientCommandDedupStore(
            session_id=session_id,
            root_dir=Path(CONVERSATION_DATA_DIR).parent / "client-command-log",
        )
        self._ws_event_log: list[dict[str, Any]] = self._ws_event_store.load(limit=WS_EVENT_REPLAY_MAX)
        self._ws_event_seq = self._max_replay_event_seq(self._ws_event_log)
        # ``_ws_event_seq`` orders every wire envelope, including transient
        # control traffic such as pong/capability snapshots. Restore cursors
        # acknowledge only durable conversation events, so keep a separate
        # high-water mark for the replayable stream.
        self._ws_replay_cursor = self._ws_event_seq
        self._event_connection_generation: ContextVar[int | None] = ContextVar(
            f"ws_event_generation_{session_id}",
            default=None,
        )
        self._client_command_context: ContextVar[str] = ContextVar(
            f"ws_client_command_{session_id}",
            default="",
        )
        self._client_command_type_context: ContextVar[str] = ContextVar(
            f"ws_client_command_type_{session_id}",
            default="",
        )
        self.llm = llm
        self.artifact_store = artifact_store
        self.tool_registry = tool_registry
        if mcp_manager is _SESSION_MCP_MANAGER_UNSET:
            from backend.api.routes_health import get_mcp_manager

            mcp_manager = get_mcp_manager()
        self.mcp_manager = mcp_manager
        # Snapshot of the MCP registry generation this tool_registry reflects.
        # Used to hot-reload MCP tools into a live session (see
        # refresh_tool_registry_if_mcp_changed) without restarting the backend.
        self._mcp_registry_version_snapshot = mcp_registry_version(mcp_manager)
        self._mcp_manager_snapshot_id = id(mcp_manager)
        self.permission_checker = permission_checker
        self.config = config
        self.skill_executor = skill_executor
        self.memory_manager = memory_manager
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._pending_approval_payloads: dict[str, dict[str, Any]] = {}
        # Client responses can arrive immediately after a request is sent,
        # before _approval_handler has installed its Future.
        self._pending_approval_responses: dict[str, dict[str, Any]] = {}
        self._approval_diff_cache: dict[str, dict[str, Any]] = {}
        self._active_run_task: asyncio.Task[None] | None = None
        self._active_task_id: str | None = None
        self._active_run_cancel_event: asyncio.Event | None = None
        # Set True when agent events are silently dropped during a
        # socket disconnect while a run is active. Reset on session.restore.
        self._events_dropped_during_disconnect = False
        self._conversation_run_tasks: dict[str, asyncio.Task[None]] = {}
        self._conversation_run_task_ids: dict[str, str] = {}
        self._conversation_run_cancel_events: dict[str, asyncio.Event] = {}
        # Pi's ExtensionRunner/AgentSessionRuntime and Codex's thread runtime
        # are session-owner scoped.  A websocket can host several MiniCode
        # conversations, so each conversation owns its own registry and
        # extension generation instead of sharing the last-bound closures.
        self._conversation_tool_registries: dict[str, tuple[int, str, Any]] = {}
        self._extension_runtime_states: dict[str, dict[str, Any]] = {}
        self._extension_shutdown_requested = False
        self._extension_requested_shutdown_task: asyncio.Task[Any] | None = None
        self._run_manager = SessionRunManager(self)
        self._interrupted = False
        self._interrupted_conversation_ids: set[str] = set()
        # Conversation lifecycle commands are received concurrently so slow
        # workspace activation cannot let a later delete/create/user message
        # overtake an earlier switch. Keep their observable order per session.
        self._conversation_lifecycle_lock = asyncio.Lock()
        # HTTP uploads run outside the WebSocket command queue. Reserve their
        # conversation owner synchronously under one process lock so concurrent
        # files in the same paste/drop batch cannot each create a different
        # conversation before their bodies are read.
        self._attachment_upload_lock = threading.RLock()
        self._command_semaphore = asyncio.Semaphore(20)  # max 20 concurrent commands
        self._command_tasks: set[asyncio.Task[Any]] = set()
        # Notification hooks observe already-committed wire events. They are
        # owned by the session but must never delay live projection.
        self._notification_hook_tasks: set[asyncio.Task[Any]] = set()
        self._active_client_command_ids: set[str] = set()
        # Dedupe key for the durable-queue read-failure report; see
        # _report_durable_queue_load_error.
        self._reported_durable_queue_load_error = ""
        self._max_command_tasks = MAX_PENDING_COMMAND_TASKS
        self._last_command_backlog_error_at = 0.0
        self._recent_client_command_ids: list[str] = self._load_recent_client_command_ids()
        self._recent_client_command_id_set: set[str] = set(self._recent_client_command_ids)
        self._ws_send_lock = asyncio.Lock()
        self._sandbox_capability_payload: dict[str, Any] | None = None
        self._sandbox_capability_task: asyncio.Task[Any] | None = None
        self._sandbox_capability_generation = 0
        self._ws_event_persist_tail: asyncio.Task[None] | None = None
        # Replay events are staged before the live send so wire order remains
        # stable.  A failed durable append must remain visible and must make
        # the affected replay window unusable until a later rewrite repairs it.
        self._ws_replay_persistence_errors: list[dict[str, Any]] = []
        self._ws_replay_persistence_failed_seqs: set[int] = set()
        self._is_connected = True
        # Streaming reconnection support
        self._conversation_streams: dict[str, dict[str, Any]] = {}
        self._resolve_llm_provider = get_llm_provider
        self._resolve_available_models = get_available_models
        self._resolve_models_source = get_models_source
        self._llm_adapter_cache: dict[tuple[Any, ...], LLMAdapter] = {}
        self._llm_close_tasks: set[asyncio.Task[Any]] = set()
        self._llm_adapter_leases: dict[int, set[asyncio.Task[Any]]] = {}
        self._retired_llm_adapters: dict[int, LLMAdapter] = {}
        self._background_recovery_lock = asyncio.Lock()
        self._background_recovery_loaded = False
        self._pending_background_recovery: list[Any] = []
        self.provider = self._resolve_llm_provider()
        self.available_models = list(self._resolve_available_models(self.provider))
        self.models_source = self._resolve_models_source(self.provider)
        self.selected_model = getattr(config.llm, "model", "").strip()
        # Preserve an explicit configured model even when discovery no longer
        # advertises it. The run admission boundary must report that mismatch;
        # selecting the first catalog entry would change user intent silently.
        self._llm_adapter_cache[
            _llm_adapter_cache_key(
                config=config,
                provider=self.provider,
                model=self.selected_model,
            )
        ] = llm
        self._model_override_active = False
        self._provider_override_active = False

        if skill_manager is not None:
            from backend.skills.loader import SkillLoader
            from backend.skills.manager import SkillManager

            self.skill_manager = SkillManager(
                loader=SkillLoader(project_root=get_active_workspace_root()),
            )
            self.skill_manager.discover()
        else:
            self.skill_manager = None

        self.context_builder = ContextBuilder(
            token_budget=config.token_budget,
            agent_settings=config.agent,
            skill_executor=skill_executor,
            memory_manager=memory_manager,
            llm=llm,
            skill_manager=self.skill_manager,
        )
        self.attachment_store = AttachmentStore()
        self.checkpoint_manager = CheckpointManager()
        self.conversation_repo = ConversationRepository(CONVERSATION_DATA_DIR)
        # The WebSocket host owns transport/session concerns only. QueryEngine
        # owns the loop implementation and the complete setup/run/finalize
        # lifecycle, so no host-level loop injection can bypass that boundary.
        self.query_engine = QueryEngine()
        from backend.agent.diagnostic_store import DiagnosticPayloadStore

        self._diagnostic_store = DiagnosticPayloadStore()
        self.task_manager = TaskManager(on_change=self._schedule_task_runtime_update)
        self._task_runtime_update_task: asyncio.Task[None] | None = None
        self.permission_context = self.permission_checker.build_context(
            mode=DEFAULT_CONVERSATION_PERMISSION_MODE,
            workspace_scope="computer",
            source="websocket",
        )
        self.command_registry = CommandRegistry()
        self.conversation_runtime = ConversationRuntime(
            conversation_repo=self.conversation_repo,
            context_builder=self.context_builder,
            build_effective_transcript_content=build_effective_transcript_content,
            build_summary_from_transcript=build_summary_from_transcript,
        )
        self._register_command_handlers()
        # Wait for the client to restore or select the preferred conversation.
        # self._create_fresh_active_conversation()

        self.file_watcher: Optional[WorkspaceFileWatcher] = None
        self.terminal_manager = TerminalSessionManager()
        self.active_terminal_session_id: str | None = None
        self.background_manager = BackgroundCommandManager(
            on_completed=self._on_background_command_completed,
            on_started=self._on_background_command_started,
            session_id=session_id,
        )
        self.background_manager._on_stalled = self._on_background_command_stalled
        self._fork_registry = ForkRegistry(
            session_id=session_id,
            root_dir=Path(CONVERSATION_DATA_DIR).parent / "session-forks",
        )
        self._workspace_context = None
        self._start_file_watcher()
        self._workspace_context_task: asyncio.Task[None] | None = None
        self._workspace_mcp_task: asyncio.Task[None] | None = None
        self._retired_workspace_tasks: set[asyncio.Task[Any]] = set()
        # Keep deadline-surviving cleanup wrappers owned by this session until
        # their underlying side effects actually settle.
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._workspace_generation = 0

        # Workspace root tracking. Connection/conversation state lives in
        # the session's own fields (self.ws, self._connection_generation,
        # self._conversation_run_tasks) rather than in wrapper objects.
        self._workspace_root = get_active_workspace_root()

    @property
    def conversation_run_tasks(self) -> dict[str, asyncio.Task[None]]:
        return self._conversation_run_tasks

    @property
    def conversation_run_task_ids(self) -> dict[str, str]:
        return self._conversation_run_task_ids

    @property
    def conversation_run_cancel_events(self) -> dict[str, asyncio.Event]:
        return self._conversation_run_cancel_events

    async def _init_workspace_context(self):
        try:
            from backend.workspace.context import WorkspaceContext

            workspace_root = self._workspace_root_for_conversation()
            if workspace_root is None:
                logger.info("No workspace bound for session %s; workspace context not initialized", self.session_id)
                return
            ctx = WorkspaceContext(workspace_root)
            await ctx.initialize()
            self._workspace_context = ctx
            logger.info("Workspace context initialized for session %s", self.session_id)
        except Exception as exc:
            logger.warning("Failed to initialize workspace context: %s", exc)

    def _ensure_workspace_context_task(self) -> None:
        if self._workspace_context is not None:
            return
        task = getattr(self, "_workspace_context_task", None)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._workspace_context_task = loop.create_task(self._init_workspace_context())

    async def _on_terminal_output(self, session_id: str, data: str, conversation_id: str = "") -> None:
        try:
            await self._send_ws_payload(
                {
                    "type": "terminal.output",
                    "session_id": session_id,
                    "data": data,
                    "conversation_id": str(conversation_id or ""),
                },
                log_context="terminal.output",
            )
        except Exception:
            logger.exception("Failed to send terminal.output for session %s", session_id)

    async def _on_terminal_exit(self, session_id: str, exit_code: int, conversation_id: str = "") -> None:
        try:
            await self._send_ws_payload(
                {
                    "type": "terminal.exit",
                    "session_id": session_id,
                    "exit_code": exit_code,
                    "conversation_id": str(conversation_id or ""),
                },
                log_context="terminal.exit",
            )
        except Exception:
            logger.exception("Failed to send terminal.exit for session %s", session_id)

    async def _on_background_command_completed(self, bg_cmd: BackgroundCommand) -> None:
        try:
            output_preview = bg_cmd.output[:2000] if bg_cmd.output else ""
            lifecycle = bg_cmd.to_dict()
            await self._send_ws_payload(
                {
                    "type": "background.completed",
                    "command_id": bg_cmd.command_id,
                    "command": bg_cmd.command[:100],
                    "description": bg_cmd.description,
                    "exit_code": bg_cmd.exit_code,
                    "status": bg_cmd.status,
                    "output": output_preview,
                    "duration": round(bg_cmd.completed_at - bg_cmd.started_at, 1)
                    if bg_cmd.completed_at
                    else 0,
                    "started_at": bg_cmd.started_at,
                    "completed_at": bg_cmd.completed_at,
                    "conversation_id": bg_cmd.conversation_id,
                    **{key: lifecycle[key] for key in (
                        "run_id", "task_id", "parent_run_id", "incarnation", "seq",
                        "kind", "phase", "updated_at", "started_at_ms", "completed_at_ms",
                        "result", "error",
                        # The manager records these when nothing proved the
                        # process tree exited. Omitting them told the user
                        # "cancelled" for a process that may still be running,
                        # while the backend kept the PID for a later reaper.
                        # The recovery payload below already sends both.
                        "cleanup_pending", "cleanup_reason",
                    )},
                },
                log_context="background.completed",
            )
        except Exception as exc:
            # A dropped completion leaves the UI showing the task as running
            # forever; the recovery path re-queues on failure for this reason.
            logger.error(
                "Failed to send background.completed for %s: %s",
                bg_cmd.command_id,
                exc,
                exc_info=True,
            )

    async def _on_background_command_stalled(self, bg_cmd: BackgroundCommand, tail: str) -> None:
        """One-shot notice when a background command blocks on an interactive prompt."""
        try:
            from backend.agent.message import AgentEvent

            await self._send_event(
                AgentEvent(
                    type="background.stalled",
                    data={
                        "command_id": bg_cmd.command_id,
                        "command": bg_cmd.command[:200],
                        "description": bg_cmd.description,
                        "conversation_id": bg_cmd.conversation_id,
                        "tail": tail[-512:],
                        "advice": (
                            "The command is likely blocked on an interactive prompt. "
                            "Kill this task and re-run with piped input "
                            "(e.g., `echo y | command`) or a non-interactive flag."
                        ),
                    },
                )
            )
        except Exception as exc:
            logger.debug("Failed to send background.stalled for %s: %s", bg_cmd.command_id, exc)

    async def _on_background_command_started(self, bg_cmd: BackgroundCommand) -> None:
        try:
            lifecycle = bg_cmd.to_dict()
            await self._send_ws_payload(
                {
                    "type": "background.started",
                    "command_id": bg_cmd.command_id,
                    "command": bg_cmd.command[:100],
                    "description": bg_cmd.description,
                    "cwd": bg_cmd.cwd,
                    "status": bg_cmd.status,
                    "started_at": bg_cmd.started_at,
                    "conversation_id": bg_cmd.conversation_id,
                    **{key: lifecycle[key] for key in (
                        "run_id", "task_id", "parent_run_id", "incarnation", "seq",
                        "kind", "phase", "updated_at", "started_at_ms", "completed_at_ms",
                        "result", "error",
                    )},
                },
                log_context="background.started",
            )
        except Exception as exc:
            logger.debug("Failed to send background.started for %s: %s", bg_cmd.command_id, exc)

    async def _recover_orphaned_background_commands(self) -> None:
        """Reconcile and project durable commands left by a dead process."""

        async with self._background_recovery_lock:
            if not self._background_recovery_loaded:
                try:
                    recovered = await asyncio.to_thread(
                        self.background_manager.cleanup_orphaned_tasks_on_startup
                    )
                except Exception as exc:
                    logger.exception(
                        "Background command recovery failed for session %s",
                        self.session_id,
                    )
                    event = AgentEvent.error(
                        "MiniCode could not reconcile background commands from the previous process.",
                        recoverable=True,
                        error_type="background_recovery",
                        error_code="background.recovery_failed",
                    )
                    event.data["detail"] = str(exc)
                    await self._send_event(event)
                    return
                self._pending_background_recovery = list(recovered)
                self._background_recovery_loaded = True

            pending: list[Any] = []
            for task in self._pending_background_recovery:
                conversation_id = str(getattr(task, "conversation_id", "") or "").strip()
                if not conversation_id:
                    event = AgentEvent.error(
                        "A recovered background command has no conversation owner and was not projected.",
                        recoverable=True,
                        error_type="background_recovery",
                        error_code="background.owner_missing",
                    )
                    event.data.update(
                        {
                            "task_id": str(getattr(task, "task_id", "") or ""),
                            "cleanup_pending": bool(
                                getattr(task, "cleanup_pending", False)
                            ),
                        }
                    )
                    await self._send_event(event)
                    continue
                completed_at = float(
                    getattr(task, "cleanup_completed_at", None) or time.time()
                )
                payload = {
                    "type": "background.completed",
                    "command_id": str(getattr(task, "task_id", "") or ""),
                    "command": str(getattr(task, "command", "") or "")[:100],
                    "description": str(getattr(task, "description", "") or ""),
                    "status": "interrupted",
                    "output": "",
                    "duration": max(
                        0.0,
                        round(
                            completed_at
                            - float(getattr(task, "started_at", 0.0) or 0.0),
                            1,
                        ),
                    ),
                    "started_at": float(getattr(task, "started_at", 0.0) or 0.0),
                    "completed_at": completed_at,
                    "conversation_id": conversation_id,
                    "task_id": str(getattr(task, "owner_task_id", "") or ""),
                    "parent_run_id": str(getattr(task, "parent_run_id", "") or ""),
                    "cleanup_pending": bool(getattr(task, "cleanup_pending", False)),
                    "cleanup_reason": str(
                        getattr(task, "cleanup_reason", "")
                        or "background_owner_exited"
                    ),
                    "error": {
                        "code": "background_owner_exited",
                        "message": (
                            "The previous MiniCode process exited before this "
                            "background command completed."
                        ),
                    },
                }
                sent = await self._send_ws_payload(
                    payload,
                    log_context="background.completed.recovered",
                )
                if not sent and self._is_connected:
                    pending.append(task)
            self._pending_background_recovery = pending

    def _start_file_watcher(self):
        workspace_root = self._workspace_root_for_conversation()
        if workspace_root is None:
            logger.info("No workspace bound for session %s; file watcher not started", self.session_id)
            return
        conversation_id = str(self.active_conversation_id or "").strip()

        async def on_change(path: Path, event_type: str) -> None:
            await self._on_file_changed(
                path,
                event_type,
                workspace_root=workspace_root,
                conversation_id=conversation_id,
            )

        try:
            self.file_watcher = WorkspaceFileWatcher(
                workspace_root=workspace_root,
                on_change=on_change,
                stability_threshold=0.5,
            )
            self.file_watcher.start()
            logger.info("File watcher started for session %s", self.session_id)
        except Exception as exc:
            logger.error("Failed to start file watcher: %s", exc, exc_info=True)

    def _restart_file_watcher(self, workspace_root: Path) -> None:
        if self.file_watcher is not None:
            self.file_watcher.stop()
            self.file_watcher = None

        resolved_workspace_root = workspace_root.resolve()
        conversation_id = str(self.active_conversation_id or "").strip()

        async def on_change(path: Path, event_type: str) -> None:
            await self._on_file_changed(
                path,
                event_type,
                workspace_root=resolved_workspace_root,
                conversation_id=conversation_id,
            )

        try:
            self.file_watcher = WorkspaceFileWatcher(
                workspace_root=resolved_workspace_root,
                on_change=on_change,
                stability_threshold=0.5,
            )
            self.file_watcher.start()
            logger.info(
                "File watcher restarted for session %s: %s",
                self.session_id,
                workspace_root,
            )
        except Exception as exc:
            logger.error("Failed to restart file watcher: %s", exc, exc_info=True)

    def _clear_workspace_runtime(self) -> None:
        self._workspace_generation += 1
        self._retire_workspace_task("_workspace_context_task")
        self._retire_workspace_task("_workspace_mcp_task")
        self._workspace_context = None
        self.mcp_manager = None
        refresh_registry = getattr(self, "refresh_tool_registry_if_mcp_changed", None)
        if callable(refresh_registry):
            refresh_registry(allow_when_busy=False)
        if self.file_watcher is not None:
            self.file_watcher.stop()
            self.file_watcher = None
        clear_active_workspace_root()

    def _current_workspace_root(self) -> Path:
        """Get current workspace root (mirrors the active conversation binding)."""
        # Prefer workspace_context if available (for active conversation)
        workspace_context = getattr(self, "_workspace_context", None)
        if workspace_context is not None:
            workspace_root = getattr(workspace_context, "root_path", None)
            if workspace_root is not None:
                resolved = Path(workspace_root).resolve()
                self._workspace_root = resolved
                return resolved

        if getattr(self, "_workspace_root", None) is not None:
            return self._workspace_root

        # No fallback to cwd - only return workspace if explicitly bound
        # This matches MiniCode behavior: chat-only mode shows no Git info
        return Path("")

    def _workspace_path_for_conversation(self, conversation: Any | None = None) -> str:
        target = conversation if conversation is not None else self.active_conversation
        return str(
            getattr(target, "worktree_path", "")
            or getattr(target, "workspace_root", "")
            or ""
        ).strip()

    def _workspace_root_for_conversation(self, conversation: Any | None = None) -> Path | None:
        workspace_path = self._workspace_path_for_conversation(conversation)
        if not workspace_path:
            return None
        return Path(workspace_path).expanduser().resolve()

    def _workspace_context_for_conversation(self, conversation: Any | None = None) -> Any | None:
        workspace_root = self._workspace_root_for_conversation(conversation)
        if workspace_root is None:
            return None
        workspace_context = getattr(self, "_workspace_context", None)
        context_root = getattr(workspace_context, "root_path", None) if workspace_context is not None else None
        if context_root is not None and Path(context_root).resolve() == workspace_root:
            return workspace_context
        return None

    async def _on_file_changed(
        self,
        path: Path,
        event_type: str,
        *,
        workspace_root: Path | None = None,
        conversation_id: str = "",
    ):
        try:
            owner_workspace_root = (workspace_root or self._current_workspace_root()).resolve()
            owner_conversation_id = str(conversation_id or self.active_conversation_id or "").strip()
            if not owner_conversation_id:
                return
            try:
                relative_path = str(path.relative_to(owner_workspace_root))
            except ValueError:
                relative_path = path.name
            await self._send_ws_payload(
                {
                    "type": "file.changed",
                    "path": relative_path,
                    "event": event_type,
                    "conversation_id": owner_conversation_id,
                    "workspace_root": str(owner_workspace_root),
                },
                log_context="file.changed",
            )

            from backend.preview.launcher import running_preview_processes

            active_previews = running_preview_processes(
                session_id=self.session_id,
                conversation_id=owner_conversation_id,
                workspace_root=owner_workspace_root,
            )
            if active_previews:
                await self._send_ws_payload(
                    {
                        "type": "preview.refreshed",
                        "path": relative_path,
                        "url": active_previews[0].effective_url,
                        "conversation_id": owner_conversation_id,
                        "workspace_root": str(owner_workspace_root),
                    },
                    log_context="preview.refreshed",
                )

            from backend.agent.instruction_discovery import clear_guideline_cache, guideline_change_metadata

            guideline_change = guideline_change_metadata(path)
            if guideline_change is not None:
                logger.info("Guideline source changed, clearing guideline cache: %s", path)
                clear_guideline_cache()
                await self._send_ws_payload(
                    {
                        "type": "guidelines.updated",
                        "message": "Project guidelines have been updated",
                        "conversation_id": owner_conversation_id,
                        "workspace_root": str(owner_workspace_root),
                        "path": relative_path,
                        "cache_cleared": True,
                        "effective_from": "next_turn",
                        "source_kind": guideline_change["source_kind"],
                        **(
                            {"parent_path": guideline_change["parent_path"]}
                            if guideline_change.get("parent_path")
                            else {}
                        ),
                    },
                    log_context="guidelines.updated",
                )
        except Exception as exc:
            logger.error("Error handling file change: %s", exc, exc_info=True)

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def attach_websocket(self, websocket: WebSocket) -> tuple[WebSocket, int]:
        previous = self.ws
        self.ws = websocket
        self._connection_generation += 1
        self._is_connected = True
        self._run_manager.recheck_watched_parent_notifications()
        return previous, self._connection_generation

    @property
    def active_conversation_id(self) -> str | None:
        return self.conversation_runtime.active_conversation_id

    @active_conversation_id.setter
    def active_conversation_id(self, value: str | None) -> None:
        self.conversation_runtime.active_conversation_id = value
        run_manager = getattr(self, "_run_manager", None)
        if run_manager is not None and value:
            run_manager.watch_conversation_notifications(value)

    def _create_fresh_active_conversation(self) -> None:
        self.conversation_runtime.create_fresh_active_conversation()
        if self.active_conversation_id:
            self._run_manager.watch_conversation_notifications(
                self.active_conversation_id
            )
        self._sync_permission_mode_with_active_conversation(source="conversation.create")

    def _ensure_active_conversation(
        self, preferred_id: str | None = None
    ) -> None:
        self.conversation_runtime.ensure_active_conversation(preferred_id)
        if self.active_conversation_id:
            self._run_manager.watch_conversation_notifications(
                self.active_conversation_id
            )
        self._sync_permission_mode_with_active_conversation(source="conversation.ensure")

    @property
    def active_conversation(self):
        return self.conversation_runtime.active_conversation

    def _pending_approval_runtime_items(self) -> list[dict[str, Any]]:
        pending_payloads = getattr(self, "_pending_approval_payloads", {})
        request_ids = list(dict.fromkeys([
            *getattr(self, "_pending_approvals", {}).keys(),
            *pending_payloads.keys(),
        ]))
        items: list[dict[str, Any]] = []
        for request_id in request_ids:
            payload = dict(pending_payloads.get(request_id) or {})
            # Every prompt reaches the client as one ``control_request``. A
            # request id without a payload is a waiter registered before its
            # request was built, so it is reported as pending without a subtype.
            item: dict[str, Any] = {
                "request_id": request_id,
                "type": str(payload.get("type") or "approval_pending"),
            }
            conversation_id = str(payload.get("conversation_id") or "").strip()
            if conversation_id:
                item["conversation_id"] = conversation_id
            request = payload.get("request")
            if isinstance(request, dict):
                item["subtype"] = str(request.get("subtype") or "").strip()
                tool_name = str(request.get("tool_name") or "").strip()
                if tool_name:
                    item["tool_name"] = tool_name
            items.append(item)
        return items

    def runtime_snapshot(self) -> dict[str, Any]:
        task_summary = self.task_manager.summary()
        running_tasks = [
            task.to_dict()
            for task in self.task_manager.list()
            if task.status in {"pending", "running"}
        ]
        running_tasks.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        # Explicit Skills belong to an AgentState turn, never to the session.
        invoked_skill_names: list[str] = []
        pending_approvals = self._pending_approval_runtime_items()
        fork_records = self._fork_registry.list(
            parent_conversation_id=str(self.active_conversation_id or ""),
        )
        queued_user_messages = self._run_manager.queued_user_message_snapshot()
        pending_turn_inputs = self._run_manager.pending_turn_input_snapshot()
        active_stream_conversation_ids = sorted(
            str(conversation_id)
            for conversation_id, task in getattr(self, "_conversation_run_tasks", {}).items()
            if conversation_id and task is not None and not task.done()
        )
        active = self.active_conversation
        if active is not None and (
            getattr(active, "archived", False)
            or getattr(active, "conversation_type", "main") != "main"
        ):
            active = None
            self.active_conversation_id = None
        workspace_root = self._workspace_root_for_conversation()
        workspace_scope = workspace_scope_for(
            workspace_root=getattr(active, "workspace_root", "") if active is not None else "",
            worktree_path=getattr(active, "worktree_path", "") if active is not None else "",
        )
        permission_payload = self._runtime_permission_payload(workspace_scope=workspace_scope)
        if (
            active is not None
            and workspace_scope == "computer"
            and self.permission_context.mode == DEFAULT_CONVERSATION_PERMISSION_MODE
        ):
            permission_payload = {
                **permission_payload,
                "profile": "auto",
                "sandbox_status": sandbox_status_for("auto"),
            }
        return {
            "session_id": self.session_id,
            "parent_session_id": None,
            "active_conversation_id": self.active_conversation_id,
            "workspace_root": str(workspace_root) if workspace_root is not None else None,
            "active_conversation": project_public_conversation_summary(active)
            if active is not None
            else None,
            "active_task_id": self._active_task_id,
            "active_stream_conversation_ids": active_stream_conversation_ids,
            "selected_model": self.selected_model or None,
            "invoked_skill_names": invoked_skill_names,
            "permission_mode": self.permission_context.mode,
            "permission_profile": permission_payload["profile"],
            "permission_source": self.permission_context.source,
            "approval_policy": self.permission_context.approval_policy,
            "sandbox_mode": self.permission_context.sandbox_mode,
            "managed_requirements_source": self.permission_context.requirements_source or None,
            "workspace_scope": workspace_scope,
            "sandbox_status": permission_payload["sandbox_status"],
            "mcp": self._mcp_summary(),
            "capabilities": self.runtime_capability_summary(permission_payload=permission_payload),
            "task_summary": task_summary,
            "running_tasks": running_tasks[:5],
            "pending_approval_count": len(pending_approvals),
            "pending_approvals": pending_approvals[:5],
            "forks": [record.to_dict() for record in fork_records[-20:]],
            "queued_user_messages": queued_user_messages,
            "pending_turn_inputs": pending_turn_inputs,
            "websocket_replay": {
                "log_read_status": getattr(
                    getattr(self, "_ws_event_store", None),
                    "read_status",
                    None,
                ).to_payload()
                if getattr(getattr(self, "_ws_event_store", None), "read_status", None)
                is not None
                else {},
                "persistence_failed_sequences": sorted(
                    getattr(self, "_ws_replay_persistence_failed_seqs", set())
                ),
                "persistence_errors": list(
                    getattr(self, "_ws_replay_persistence_errors", [])[-20:]
                ),
            },
        }

    def _runtime_permission_payload(
        self,
        *,
        workspace_scope: str | None = None,
    ) -> dict[str, Any]:
        profile = permission_profile_for_mode(self.permission_context.mode)
        sandbox_status = self._sandbox_capability_payload or {
            "policy_configured": True,
            "probe_status": "pending",
            "enforcement": "unknown",
            "backend_available": None,
            "backend": "unknown",
            "filesystem_isolated": None,
            "network_isolated": None,
            "deny_read_isolated": None,
            "protected_paths_isolated": None,
            "fail_closed": True,
            "unavailable_action": "await_probe",
            "reason": "Sandbox capability probe has not completed",
        }
        return {
            "mode": self.permission_context.mode,
            "profile": profile,
            "source": self.permission_context.source,
            "approval_policy": self.permission_context.approval_policy,
            "sandbox_mode": self.permission_context.sandbox_mode,
            "managed_requirements_source": self.permission_context.requirements_source or None,
            "workspace_scope": workspace_scope or getattr(self.permission_context, "workspace_scope", "computer"),
            "sandbox_status": sandbox_status,
        }

    def runtime_capability_summary(
        self,
        *,
        permission_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compact per-session capability contract for runtime snapshots."""
        self.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
        permission = permission_payload or self._runtime_permission_payload()
        registry = self.tool_registry
        summary_error: dict[str, str] | None = None
        try:
            registry_for_conversation = getattr(
                self,
                "_conversation_tool_registry",
                None,
            )
            if callable(registry_for_conversation) and self.active_conversation_id:
                registry = registry_for_conversation(self.active_conversation_id)
            summary = registry.build_capability_summary(
                permission_checker=self.permission_checker,
                permission_context=self.permission_context,
            )
        except Exception as exc:
            logger.debug("session %s capability summary failed: %s", self.session_id, exc)
            summary_error = {
                "type": "capability_summary_failed",
                "detail": type(exc).__name__,
            }
            summary = {
                "tools_total": 0,
                "direct_tools": 0,
                "core_tools": 0,
                "deferred_tools": 0,
                "hidden_tools": 0,
                "mcp_proxy_tools": 0,
                "commands": 0,
                "skills": 0,
                "mcp_resource_bridge": False,
                "deferred_bridge": False,
                "skill_catalog": False,
                "status": "error",
                "error": summary_error,
            }
        return {
            "version": getattr(registry, "version", 0),
            "summary": summary,
            "permission": permission,
            "mcp_registry_version": self._mcp_registry_version_snapshot,
            "provider_capabilities": self._provider_capabilities_payload(),
            **({"status": "error", "error": summary_error} if summary_error else {}),
        }

    def _provider_capabilities_payload(self) -> dict[str, Any]:
        try:
            from backend.llm.capabilities import capabilities_for_adapter

            return capabilities_for_adapter(self.llm).to_dict()
        except Exception as exc:
            logger.debug("session %s provider capability snapshot failed: %s", self.session_id, exc)
            return {}

    def runtime_capability_snapshot(self) -> dict[str, Any]:
        """Full per-session capability contract, including current permissions."""
        from backend.commands.catalog import get_enabled_composer_command_catalog
        from backend.feature_flags import feature_flags_payload

        self.refresh_tool_registry_if_mcp_changed()
        registry = self.tool_registry
        try:
            registry_for_conversation = getattr(
                self,
                "_conversation_tool_registry",
                None,
            )
            if callable(registry_for_conversation) and self.active_conversation_id:
                registry = registry_for_conversation(self.active_conversation_id)
            snapshot = registry.build_snapshot(
                permission_checker=self.permission_checker,
                permission_context=self.permission_context,
                mcp_registry_version=self._mcp_registry_version_snapshot,
            )
        except Exception as exc:
            logger.warning(
                "session %s capability snapshot failed: %s",
                self.session_id,
                exc,
            )
            snapshot = {
                "version": getattr(registry, "version", 0),
                "tools": [],
                "commands": [],
                "skills": [],
                "summary": {
                    "tools_total": 0,
                    "direct_tools": 0,
                    "core_tools": 0,
                    "deferred_tools": 0,
                    "hidden_tools": 0,
                    "mcp_proxy_tools": 0,
                    "commands": 0,
                    "skills": 0,
                    "status": "error",
                    "error": {
                        "type": "capability_snapshot_failed",
                        "detail": type(exc).__name__,
                    },
                },
                "status": "error",
                "error": {
                    "type": "capability_snapshot_failed",
                    "detail": type(exc).__name__,
                },
            }
        extension_commands = self.command_registry.list_extension_slash_commands(
            scope_id=self.active_conversation_id
        )
        active_conversation = (
            self.conversation_repo.get_conversation(self.active_conversation_id)
            if self.active_conversation_id
            else None
        )
        active_workspace_root = (
            self._workspace_root_for_conversation(active_conversation)
            if active_conversation is not None
            else None
        )
        snapshot["composer_commands"] = [
            *extension_commands,
            *get_enabled_composer_command_catalog(
                active_workspace_root,
                resolve_active_workspace=False,
            ),
        ]
        snapshot["feature_flags"] = feature_flags_payload()
        if self.skill_manager is not None and not snapshot.get("skills"):
            list_all = getattr(self.skill_manager, "list_all", None)
            skills = list_all() if callable(list_all) else []
            if skills:
                snapshot["skills"] = skills
                snapshot["summary"] = {
                    **dict(snapshot.get("summary") or {}),
                    "skills": len(skills),
                    "skill_catalog": True,
                }
        snapshot["permission"] = self._runtime_permission_payload()
        snapshot["mcp_registry_version"] = self._mcp_registry_version_snapshot
        snapshot["provider_capabilities"] = self._provider_capabilities_payload()
        return snapshot

    def runtime_capabilities_payload(self, *, source: str = "session") -> dict[str, Any]:
        return {
            "type": "runtime.capabilities",
            "session_id": self.session_id,
            "source": source,
            "capabilities": self.runtime_capability_snapshot(),
        }

    async def _send_runtime_capabilities(self, *, source: str = "session") -> None:
        try:
            workspace = self._workspace_root_for_conversation()
            if workspace:
                probe_task = self._sandbox_capability_task
                if self._sandbox_capability_payload is None and (
                    probe_task is None or probe_task.done()
                ):
                    self._sandbox_capability_generation += 1
                    probe_generation = self._sandbox_capability_generation
                    probe_workspace = canonical_file_path_key(workspace)
                    probe_task = asyncio.create_task(
                        asyncio.to_thread(
                            sandbox_capability_for_context,
                            workspace,
                            self.permission_context,
                        )
                    )
                    self._sandbox_capability_task = probe_task

                    def publish_probe_result(task: asyncio.Task[Any]) -> None:
                        if task.cancelled():
                            return
                        if (
                            probe_generation != self._sandbox_capability_generation
                            or probe_workspace
                            != canonical_file_path_key(self._workspace_root_for_conversation())
                        ):
                            return
                        try:
                            self._sandbox_capability_payload = task.result()
                        except Exception as exc:
                            logger.debug("Sandbox capability probe failed: %s", exc)
                            self._sandbox_capability_payload = {
                                "policy_configured": True,
                                "probe_status": "error",
                                "enforcement": "unknown",
                                "backend_available": False,
                                "backend": "unavailable",
                                "filesystem_isolated": False,
                                "network_isolated": False,
                                "deny_read_isolated": False,
                                "protected_paths_isolated": False,
                                "fail_closed": True,
                                "unavailable_action": "reject_command",
                                "reason": str(exc),
                            }
                        if getattr(self, "_is_connected", False):
                            asyncio.create_task(
                                self._send_runtime_capabilities(source="sandbox.probe")
                            )

                    probe_task.add_done_callback(publish_probe_result)

                # Workspace activation is a latency-sensitive command (for
                # example, deleting the active conversation may immediately
                    # activate its fallback workspace).  Match MiniCode's eventual
                # capability projection: expose a pending snapshot now and
                # publish the probed result when the background task completes.
                if self._sandbox_capability_payload is None and not source.startswith(
                    "workspace.activate"
                ):
                    self._sandbox_capability_payload = await probe_task
        except Exception as exc:
            logger.debug("Sandbox capability probe failed: %s", exc)
            self._sandbox_capability_payload = {
                "policy_configured": True,
                "probe_status": "error",
                "enforcement": "unknown",
                "backend_available": False,
                "backend": "unavailable",
                "filesystem_isolated": False,
                "network_isolated": False,
                "deny_read_isolated": False,
                "protected_paths_isolated": False,
                "fail_closed": True,
                "unavailable_action": "reject_command",
                "reason": str(exc),
            }
        await self._send_ws_payload(
            self.runtime_capabilities_payload(source=source),
            log_context="runtime.capabilities",
        )

    def _mcp_summary(self) -> dict[str, Any]:
        """Compact MCP summary for the runtime snapshot: counts + short statuses.

        Kept intentionally small (no tools/errors/full dicts) so the snapshot
        stays light. Reads the in-memory manager status; empty when no bootstrap.
        """
        try:
            from backend.api.routes_health import get_mcp_status

            servers = get_mcp_status() or []
        except Exception:  # pragma: no cover - manager unavailable / not started
            servers = []
        return {
            "connected": sum(1 for s in servers if s.get("status") == "connected"),
            "failed": sum(1 for s in servers if s.get("phase") == "failed"),
            "auth_required": sum(1 for s in servers if s.get("phase") in {"auth_required", "expired"}),
            "servers": [
                {
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "phase": s.get("phase"),
                    "auth_status": s.get("auth_status"),
                }
                for s in servers
            ],
        }

    def _has_active_run(self) -> bool:
        """True when an agent run is currently in flight in this session."""
        return self._run_manager.has_active_run()

    def _running_agent_task_for(self, conversation_id: str) -> asyncio.Task[None] | None:
        return self._run_manager.running_task_for(conversation_id)

    def _register_agent_run(
        self,
        *,
        conversation_id: str,
        task: asyncio.Task[None],
        task_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        self._run_manager.register(
            conversation_id=conversation_id,
            task=task,
            task_id=task_id,
            cancel_event=cancel_event,
            active_conversation_id=self.active_conversation_id,
        )

    def _cleanup_agent_run(
        self,
        *,
        conversation_id: str,
        task: asyncio.Task[None],
        task_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        self._run_manager.cleanup(
            conversation_id=conversation_id,
            task=task,
            task_id=task_id,
            cancel_event=cancel_event,
        )
        self._schedule_next_queued_user_message(conversation_id)
        self._run_manager.recheck_parent_notification_wake(conversation_id)

    async def _start_agent_run(
        self,
        content: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Schedule one agent run through the session's canonical run manager.

        The WebSocket command path and local host adapters must share exactly
        one scheduling boundary.  Keeping task registration, cancellation
        bookkeeping, and queued-message cleanup here prevents an integration
        transport from accidentally creating a second agent loop or leaving a
        run invisible to ``turn/interrupt``/session shutdown.

        ``metadata`` is owned by the caller and is copied before the runner can
        add lifecycle fields.  Local protocol adapters may provide a stable
        ``run_id`` so their response turn id matches the first streamed event;
        ordinary WebSocket commands do not set it.
        """
        target_conversation_id = str(conversation_id or "").strip()
        if not target_conversation_id:
            raise ValueError("conversation_id is required")

        self._run_manager.turn_input_queue(target_conversation_id)
        run_cancel_event = asyncio.Event()
        run_metadata = dict(metadata or {})
        event_generation_token = self._event_connection_generation.set(None)
        try:
            managed_run = self.task_manager.create(
                "agent.run",
                self._run_agent(
                    content,
                    attachments=list(attachments or []),
                    conversation_id=target_conversation_id,
                    metadata=run_metadata,
                    cancel_event=run_cancel_event,
                ),
            )
            # Terminal delivery is fenced to this concrete run. Task creation
            # does not yield control, so the runner observes this id before its
            # first coroutine step.
            run_metadata["_run_task_id"] = str(managed_run.id)
        finally:
            self._event_connection_generation.reset(event_generation_token)

        try:
            self._register_agent_run(
                conversation_id=target_conversation_id,
                task=managed_run.task,
                task_id=managed_run.id,
                cancel_event=run_cancel_event,
            )
        except RuntimeError as exc:
            # The conversation already owns a live run. The task exists but is
            # unregistered, so nothing else can ever reach it — tear it down
            # here rather than leaking a second loop into this conversation.
            logger.error(
                "Refusing a second agent run for conversation %s: %s",
                target_conversation_id,
                exc,
            )
            run_cancel_event.set()
            await cancel_and_drain_receipt(
                [managed_run.task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"rejected duplicate run for {target_conversation_id}",
                owner=self._cleanup_tasks,
            )
            busy_error = AgentEvent.error(
                str(exc),
                recoverable=True,
                error_type="conversation_busy",
                error_code="agent.busy",
            )
            busy_error.data["conversation_id"] = target_conversation_id
            await self._send_event(busy_error)
            raise

        async def _wait_and_cleanup() -> None:
            # A task that returns without the runner's delivery fence is not a
            # successful run: it means an early-return/setup path escaped the
            # canonical terminal projection.  Treat that integrity gap as a
            # failure unless cancellation or an exception gives us a more
            # specific outcome.
            terminal_status = "failed"
            terminal_reason = "terminal_delivery_missing"

            async def _ensure_durable_terminal() -> Any | None:
                nonlocal terminal_reason
                runtime = run_metadata.get("agent_runtime")
                run_id = str(run_metadata.get("run_id") or "").strip()
                get_run = getattr(runtime, "get_run", None)
                record = get_run(run_id) if run_id and callable(get_run) else None
                if record is None:
                    return None
                if str(getattr(record, "status", "")) != "running":
                    return record
                commit_terminal = getattr(runtime, "commit_terminal", None)
                if not callable(commit_terminal):
                    terminal_reason = "terminal_commit_unavailable"
                    return None
                try:
                    committed = commit_terminal(
                        run_id,
                        terminal_status,
                        summary=terminal_reason or terminal_status,
                        terminal_reason=terminal_reason or terminal_status,
                        error=terminal_reason if terminal_status == "failed" else "",
                    )
                except Exception as exc:
                    terminal_reason = "terminal_commit_failed"
                    logging.error(
                        "Scheduler could not durably close run %s: %s",
                        run_id,
                        exc,
                        exc_info=True,
                    )
                    commit_error = AgentEvent.error(
                        "MiniCode could not durably commit the run terminal state.",
                        recoverable=False,
                        error_type="terminal_commit_failed",
                        error_code="runtime.scheduler_terminal_commit_failed",
                    )
                    commit_error.data.update({
                        "conversation_id": target_conversation_id,
                        "run_id": run_id,
                        "terminal_commit_failed": True,
                    })
                    await self._send_event(commit_error)
                    return None
                return committed

            try:
                if managed_run.task is not None:
                    await managed_run.task
            except asyncio.CancelledError:
                terminal_status = "cancelled"
                terminal_reason = "cancelled"
            except Exception as exc:
                terminal_status = "failed"
                terminal_reason = "runtime"
                logging.error("Chat run failed: %s", exc, exc_info=True)
                error_event = AgentEvent.error(
                    f"Chat run failed: {exc}",
                    recoverable=False,
                    error_type="runtime",
                )
                error_event.data["conversation_id"] = target_conversation_id
                await self._send_event(error_event)
            finally:
                # Terminal delivery for a durably admitted run belongs to
                # QueryEngine's terminal transaction, and the pre-admission
                # setup zone is closed by the runner's own startup_rejected /
                # startup_failed / startup_cancelled branches.  This net covers
                # only the remaining gap: an admitted run whose delivery marker
                # never landed.  It is guarded by that marker so ordinary runs
                # are never duplicated.
                is_complete = getattr(self._run_manager, "is_delivery_complete", None)
                delivery_complete = bool(
                    callable(is_complete)
                    and is_complete(target_conversation_id, str(managed_run.id))
                )
                if not delivery_complete:
                    durable_record = await _ensure_durable_terminal()
                    if durable_record is not None:
                        durable_status = str(
                            getattr(durable_record, "status", "") or ""
                        ).strip().lower()
                        if durable_status in {
                            "completed",
                            "partial",
                            "failed",
                            "cancelled",
                            "interrupted",
                        }:
                            terminal_status = (
                                "cancelled"
                                if durable_status == "interrupted"
                                else durable_status
                            )
                        durable_reason = str(
                            getattr(durable_record, "terminal_reason", "")
                            or getattr(durable_record, "summary", "")
                            or getattr(durable_record, "error", "")
                            or ""
                        ).strip()
                        if durable_reason:
                            terminal_reason = durable_reason
                        # The runner may durably commit before losing its
                        # delivery marker. Re-project the exact record instead
                        # of fabricating a local failure terminal.
                        await self._send_event(
                            AgentEvent.agent_run_completed(durable_record)
                        )
                    durable_terminal = durable_record is not None
                    done_delivered = False
                    try:
                        if not durable_terminal and not str(run_metadata.get("run_id") or "").strip():
                            # Validation rejected the command before durable
                            # admission. There is no run to complete and no
                            # terminal event should be fabricated for one.
                            self._cleanup_agent_run(
                                conversation_id=target_conversation_id,
                                task=managed_run.task,
                                task_id=managed_run.id,
                                cancel_event=run_cancel_event,
                            )
                            return
                        done_event = AgentEvent.done(
                            status=terminal_status,
                            reason=terminal_reason or terminal_status,
                        )
                        done_event.data["conversation_id"] = target_conversation_id
                        stream_state = getattr(self, "_conversation_streams", {}).get(
                            target_conversation_id
                        )
                        message_id = str(
                            (stream_state or {}).get("message_id")
                            or run_metadata.get("assistant_message_id")
                            or ""
                        ).strip()
                        if message_id:
                            done_event.data["message_id"] = message_id
                        await self._send_event(done_event)
                        done_delivered = True

                        mark_terminal = getattr(self._run_manager, "mark_terminal_status", None)
                        if callable(mark_terminal):
                            mark_terminal(target_conversation_id, terminal_status)
                        mark_delivery = getattr(self._run_manager, "mark_delivery_complete", None)
                        if callable(mark_delivery):
                            mark_delivery(target_conversation_id, str(managed_run.id))

                        await self._send_event(
                            AgentEvent.session_state_changed(
                                state="idle",
                                conversation_id=target_conversation_id,
                                reason=terminal_reason or terminal_status,
                            )
                        )
                    except Exception:
                        logging.exception(
                            "Failed to emit fallback terminal events for conversation %s",
                            target_conversation_id,
                        )
                    if not done_delivered:
                        logging.error(
                            "Fallback terminal delivery remained incomplete for conversation %s",
                            target_conversation_id,
                        )
                self._cleanup_agent_run(
                    conversation_id=target_conversation_id,
                    task=managed_run.task,
                    task_id=managed_run.id,
                    cancel_event=run_cancel_event,
                )

        event_generation_token = self._event_connection_generation.set(None)
        try:
            cleanup_task = asyncio.create_task(_wait_and_cleanup())
            self._track_command_task(cleanup_task)
        finally:
            self._event_connection_generation.reset(event_generation_token)
        return str(managed_run.id)

    def _schedule_next_queued_user_message(self, conversation_id: str) -> None:
        if (
            not conversation_id
            or self._running_agent_task_for(conversation_id)
            or self._run_manager.is_queue_steering(conversation_id)
        ):
            return
        if not self._run_manager.begin_queue_dispatch(conversation_id):
            return
        command = self._run_manager.dequeue_user_message(conversation_id)
        if command is None:
            self._run_manager.finish_queue_dispatch(conversation_id)
            return
        command.data["_queued_user_message_dispatch"] = True

        async def _dispatch() -> None:
            succeeded = False
            try:
                await self._send_event(
                    AgentEvent.user_message_queue_updated(
                        status="dequeued",
                        conversation_id=conversation_id,
                        message_id=str(command.data.get("assistant_message_id") or ""),
                        user_message_id=str(command.data.get("user_message_id") or ""),
                        target_message_id=str(command.data.get("assistant_message_id") or ""),
                        turn_mode="follow_up",
                    )
                )
                await self._handle_command(command)
                succeeded = True
            finally:
                self._run_manager.finish_user_message_dispatch(
                    conversation_id,
                    command,
                    succeeded=succeeded,
                )
                self._run_manager.finish_queue_dispatch(conversation_id)
                if not self._running_agent_task_for(conversation_id):
                    self._schedule_next_queued_user_message(conversation_id)
                    self._run_manager.recheck_parent_notification_wake(
                        conversation_id
                    )

        task = asyncio.create_task(_dispatch())
        self._track_command_task(task)

    async def _cancel_agent_runs(
        self,
        *,
        conversation_id: str | None = None,
        reason: str = "run_cancelled",
    ) -> bool:
        """Cancel one conversation run, or every run owned by this session."""
        return await self._run_manager.cancel(conversation_id=conversation_id, reason=reason)

    def refresh_tool_registry_if_mcp_changed(self, *, allow_when_busy: bool = True) -> bool:
        """Rebuild this session's tool registry when the MCP registry changed.

        A WS session holds a single ``tool_registry``; bumping
        ``mcp_registry_version`` only invalidates the schema cache, so a newly
        connected/removed MCP server stays invisible until the registry itself is
        rebuilt. This compares the live MCP registry version against the session
        snapshot and, on a change, rebuilds via ``bootstrap.create_tool_registry``
        (which re-registers the currently connected MCP proxies) reusing this
        session's existing ``artifact_store``.

        The rebuild always yields a NEW registry object, so an in-flight run that
        already captured the previous registry is never disturbed. MCP status
        hooks pass ``allow_when_busy=False`` to defer to the next idle refresh
        rather than swap the registry while a run is active.

        Returns True iff a rebuild happened.
        """
        manager = getattr(self, "mcp_manager", None)
        current_version = mcp_registry_version(manager)
        manager_snapshot_id = id(manager)
        if (
            current_version == self._mcp_registry_version_snapshot
            and manager_snapshot_id == getattr(self, "_mcp_manager_snapshot_id", 0)
        ):
            return False
        if not allow_when_busy and self._has_active_run():
            return False

        from backend.api import _state

        bootstrap = getattr(_state, "bootstrap", None)
        if bootstrap is None:
            # No bootstrap (e.g. before lifespan startup): leave the snapshot
            # stale so a later call retries once bootstrap is available.
            return False
        try:
            try:
                self.tool_registry = bootstrap.create_tool_registry(
                    self.artifact_store,
                    mcp_manager=manager,
                )
            except TypeError as exc:
                if "mcp_manager" not in str(exc):
                    raise
                self.tool_registry = bootstrap.create_tool_registry(self.artifact_store)
        except Exception as exc:  # pragma: no cover - never break a run/inspect
            logger.warning("Failed to rebuild tool registry after MCP change: %s", exc)
            return False
        self._mcp_registry_version_snapshot = current_version
        self._mcp_manager_snapshot_id = manager_snapshot_id
        return True

    def _schedule_task_runtime_update(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        existing = self._task_runtime_update_task
        if existing is not None and not existing.done():
            return
        coroutine = self._send_task_runtime_update()
        try:
            task = loop.create_task(coroutine)
        except RuntimeError:
            # Event-loop teardown can race a final TaskManager callback. Close
            # the already-created coroutine explicitly instead of leaking it.
            coroutine.close()
            return
        self._task_runtime_update_task = task

        def _clear_runtime_update(finished: asyncio.Task[None]) -> None:
            if self._task_runtime_update_task is finished:
                self._task_runtime_update_task = None

        task.add_done_callback(_clear_runtime_update)

    async def _send_task_runtime_update(self) -> None:
        try:
            await self._send_event(
                AgentEvent(
                    type="task.update",
                    data={"session": self.runtime_snapshot()},
                )
            )
        except Exception:
            logger.debug(
                "session %s task runtime update failed",
                self.session_id,
                exc_info=True,
            )

    def _load_active_conversation_snapshot(
        self,
        conversation_id: str,
        snapshot: dict[str, Any] | None,
        *,
        notify: bool = False,
        defer_start: bool = False,
    ) -> bool:
        return self.conversation_runtime.load_active_conversation_snapshot(
            conversation_id,
            snapshot,
            notify=notify,
            defer_start=defer_start,
            on_hydration_complete=self._on_conversation_hydration_complete,
        )

    def _start_active_conversation_hydration(self, conversation_id: str) -> bool:
        return self.conversation_runtime.start_hydration(conversation_id)

    async def _on_conversation_hydration_complete(self, conversation_id: str) -> None:
        await self._send_ws_payload(
            {
                "type": "conversation.hydration.updated",
                "conversation_id": conversation_id,
                "is_hydrating": False,
            },
            log_context="conversation.hydration.updated",
        )

    def _rebuild_context_from_transcript(
        self,
        conversation: Any,
        transcript: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.conversation_runtime.rebuild_context_from_transcript(conversation, transcript)

    def _prepare_retry_from_message(
        self,
        *,
        conversation: Any,
        retry_from_message_id: str,
    ) -> dict[str, Any] | None:
        return self.conversation_runtime.rewind_to_user_turn(
            conversation=conversation,
            retry_from_message_id=retry_from_message_id,
        )

    async def handle(self, *, connection_generation: int | None = None) -> None:
        active_generation = connection_generation or self._connection_generation
        if active_generation != self._connection_generation:
            return

        try:
            self._ensure_workspace_context_task()
            await self._recover_orphaned_background_commands()
            from backend.api.routes_health import get_mcp_status

            mcp_status = get_mcp_status() or []
            await self._send_event(AgentEvent(type="mcp_status", data={"servers": mcp_status}))
            await self._send_llm_state(force=True)
            await self._replay_pending_client_commands(active_generation)

            while True:
                if active_generation != self._connection_generation:
                    return

                raw = await self.ws.receive_text()
                try:
                    # Strip null bytes before JSON parsing to avoid downstream parser issues.
                    msg = json.loads(raw.replace("\x00", ""))
                except json.JSONDecodeError:
                    await self._send_event(AgentEvent.error("Invalid JSON message", recoverable=True))
                    continue

                command = UserCommand.from_ws_message(msg)
                if self._client_command_seen(command):
                    await self._send_client_command_ack(command, duplicate=True)
                    logger.info(
                        "Skipping duplicate client command %s in session %s",
                        command.data.get("client_command_id"),
                        self.session_id,
                    )
                    continue
                if command.type == "ping":
                    self._mark_client_command_seen(command)
                    await self._send_client_command_ack(command)
                    await self._send_ws_payload({"type": "pong"}, log_context="pong")
                else:
                    self._prune_command_tasks()
                    command_backlog_full = len(self._command_tasks) >= self._max_command_tasks
                    command_can_bypass_backlog = command.type in COMMAND_BACKLOG_BYPASS_TYPES
                    command_is_droppable_refresh = command.type in COMMAND_BACKLOG_DROPPABLE_TYPES
                    if command_backlog_full and not command_can_bypass_backlog:
                        reason = "command.dropped_refresh" if command_is_droppable_refresh else "command.backlog"
                        await self._send_client_command_ack(command, accepted=False, reason=reason)
                        if command_is_droppable_refresh:
                            logger.debug(
                                "Dropping refresh command %s during backlog in session %s",
                                command.type,
                                self.session_id,
                            )
                            continue
                        now = asyncio.get_running_loop().time()
                        if now - self._last_command_backlog_error_at >= COMMAND_BACKLOG_ERROR_INTERVAL_SECONDS:
                            self._last_command_backlog_error_at = now
                            await self._send_event(
                                AgentEvent.error(
                                    "Too many pending commands; please wait for current work to finish.",
                                    recoverable=True,
                                    error_type="rate_limit",
                                    error_code="command.backlog",
                                )
                            )
                        continue

                    command_id = self._client_command_id(command)
                    durable_queue = self._run_manager.durable_queue
                    if command_id and durable_queue is not None:
                        if durable_queue.has_client_command(command_id):
                            await self._send_client_command_ack(command, duplicate=True)
                            self._schedule_durable_client_command(command_id, active_generation)
                            continue
                        try:
                            persisted = durable_queue.persist_client_command(command)
                        except Exception:
                            logger.exception(
                                "Failed to persist client command %s in session %s",
                                command_id,
                                self.session_id,
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
                        # ACK means the server durably owns the command. A crash
                        # before task creation leaves it pending for replay.
                        await self._send_client_command_ack(command)
                        self._schedule_durable_client_command(command_id, active_generation)
                        continue

                    self._schedule_transient_client_command(command, active_generation)

        except WebSocketDisconnect:
            logger.info("session %s disconnected", self.session_id)
        except Exception as exc:
            logger.error("session %s failed: %s", self.session_id, exc, exc_info=True)
        finally:
            if active_generation == self._connection_generation:
                await self.artifact_store.flush()
                self.artifact_store.clear()

    async def shutdown(self, *, reason: str = "session_shutdown") -> None:
        """Cancel and drain every resource owned by this websocket session."""
        self._is_connected = False
        self._run_manager.stop_notification_wake_intake()
        self._run_manager.clear_all_user_message_queues()
        self._workspace_generation += 1
        self._sandbox_capability_generation += 1
        try:
            await self._cancel_agent_runs(reason=reason)
        except Exception:
            logger.debug("Failed to cancel agent runs during session shutdown", exc_info=True)
        await self._run_manager.shutdown_notification_wakes()
        # Notification hooks are observational and session-owned. Drain them
        # before extension/session teardown so no hook runs against a retired
        # lifecycle generation.
        current = asyncio.current_task()
        notification_tasks = [
            task
            for task in list(self._notification_hook_tasks)
            if task is not current and not task.done()
        ]
        if notification_tasks:
            await cancel_and_drain_receipt(
                notification_tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket notification hooks",
                owner=self._notification_hook_tasks,
            )
        retained_notification_tasks = {
            task for task in self._notification_hook_tasks
            if task is not current and not task.done()
        }
        self._notification_hook_tasks.intersection_update(retained_notification_tasks)
        background_tasks: set[asyncio.Task[Any]] = set(self._retired_workspace_tasks)
        for task in (
            self._workspace_context_task,
            self._workspace_mcp_task,
            self._sandbox_capability_task,
            self._extension_requested_shutdown_task,
        ):
            if task is not None:
                background_tasks.add(task)
        if background_tasks:
            await cancel_and_drain_receipt(
                background_tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="session workspace and capability tasks",
                owner=self._retired_workspace_tasks,
            )
            self._retired_workspace_tasks.intersection_update(
                task
                for task in self._retired_workspace_tasks
                if not task.done()
            )
        self._workspace_context_task = None
        self._workspace_mcp_task = None
        self._sandbox_capability_task = None
        self._extension_requested_shutdown_task = None
        try:
            await self.conversation_runtime.shutdown()
        except Exception:
            logger.debug("Failed to drain conversation hydration", exc_info=True)
        # MiniCode extension generations are session-owned. Shut them down only
        # after active turns have been cancelled so in-flight tool adapters are
        # never invalidated underneath a running turn.
        shutdown_extensions = getattr(self, "_shutdown_lifecycle_runtimes", None)
        if callable(shutdown_extensions):
            try:
                await await_with_deadline(
                    shutdown_extensions(reason),
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label="MiniCode extension shutdown",
                    owner=self._cleanup_tasks,
                )
            except Exception:
                logger.debug("Failed to shut down MiniCode extensions during session shutdown", exc_info=True)
        try:
            from backend.hooks.runtime import run_session_end_hook

            await await_with_deadline(
                run_session_end_hook(session_id=self.session_id, reason=reason),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="session end hook shutdown",
                owner=self._cleanup_tasks,
            )
        except Exception:
            logger.debug("Failed to finalize session hooks during shutdown", exc_info=True)
        try:
            from backend.agent.runtime import default_runtime_if_initialized

            runtime = default_runtime_if_initialized()
            if runtime is not None:
                # A process that never ran a turn owns no child tasks; anything
                # else must go through the draining stop so a surviving child
                # keeps its cleanup owner.
                await runtime.stop_subagent_tasks_for_session(self.session_id, reason=reason)
        except Exception:
            logger.exception("Failed to stop subagents during session %s shutdown", self.session_id)

        current = asyncio.current_task()
        command_tasks = [
            task
            for task in list(self._command_tasks)
            if task is not current and not task.done()
        ]
        if command_tasks:
            await cancel_and_drain_receipt(
                command_tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket command tasks",
                owner=self._command_tasks,
            )
        self._prune_command_tasks()

        try:
            await await_with_deadline(
                self.task_manager.cancel_all_and_wait(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="managed task shutdown",
                owner=self._cleanup_tasks,
            )
        except Exception:
            logger.debug("Failed to drain managed tasks during session shutdown", exc_info=True)
        runtime_update_task = self._task_runtime_update_task
        if runtime_update_task is not None and runtime_update_task is not current:
            await cancel_and_drain_receipt(
                [runtime_update_task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="runtime update task",
                owner=self._cleanup_tasks,
            )
        self._task_runtime_update_task = None
        if self.file_watcher and self.file_watcher.is_running():
            self.file_watcher.stop()
        try:
            await await_with_deadline(
                self.terminal_manager.destroy_all(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="terminal shutdown",
                owner=self._cleanup_tasks,
            )
        except Exception as exc:
            # destroy_all/destroy_sessions_for_conversation raise naming the
            # shells whose exit was never proven. Terminals keep no durable
            # record, so a debug-level log was the only trace of a leaked
            # process tree.
            logger.error(
                "Terminals could not be proven stopped during session %s shutdown: %s",
                self.session_id,
                exc,
                exc_info=True,
            )
        try:
            from backend.preview import stop_preview_launches_for_session

            await await_with_deadline(
                stop_preview_launches_for_session(self.session_id),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="preview shutdown",
                owner=self._cleanup_tasks,
            )
        except Exception as exc:
            # _stop_preview_processes raises with the surviving preview ids; a
            # dev server that outlives the session keeps writing to the
            # workspace.
            logger.error(
                "Previews could not be proven stopped during session %s shutdown: %s",
                self.session_id,
                exc,
                exc_info=True,
            )
        try:
            await await_with_deadline(
                self.background_manager.shutdown(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="background command shutdown",
                owner=self._cleanup_tasks,
            )
        except Exception as exc:
            logger.error(
                "Background commands could not be proven stopped during session %s shutdown: %s",
                self.session_id,
                exc,
                exc_info=True,
            )
        try:
            from backend.ws.agent_runner import (
                _clear_session_llm_cache,
                _schedule_session_llm_close,
            )

            _clear_session_llm_cache(self)
            for adapter in list(self._retired_llm_adapters.values()):
                _schedule_session_llm_close(self, adapter)
            self._retired_llm_adapters.clear()
            self._llm_adapter_leases.clear()
            if self._llm_close_tasks:
                await await_with_deadline(
                    asyncio.gather(
                        *tuple(self._llm_close_tasks),
                        return_exceptions=True,
                    ),
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label="LLM adapter close tasks",
                    owner=self._cleanup_tasks,
                )
                self._llm_close_tasks = {
                    task for task in self._llm_close_tasks if not task.done()
                }
        except Exception:
            logger.debug("Failed to close session LLM adapters", exc_info=True)
        try:
            await self._drain_ws_event_persistence()
            await self.artifact_store.flush()
            self.artifact_store.shutdown()
            self.artifact_store.clear()
        finally:
            self._run_manager.close_durable_queue()

    async def _replay_pending_client_commands(self, connection_generation: int) -> None:
        durable_queue = self._run_manager.durable_queue
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

        evidence = getattr(durable_queue, "load_error", None)
        if not isinstance(evidence, dict) or not evidence:
            return
        signature = str(
            evidence.get("quarantined_to") or evidence.get("path") or evidence.get("reason") or ""
        )
        if signature and signature == self._reported_durable_queue_load_error:
            return
        self._reported_durable_queue_load_error = signature
        await self._emit_command_result(
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

    def _schedule_transient_client_command(
        self,
        command: UserCommand,
        connection_generation: int,
    ) -> None:
        async def _guarded_handle() -> None:
            async with self._command_semaphore:
                token = self._client_command_context.set(self._client_command_id(command))
                type_token = self._client_command_type_context.set(command.type)
                try:
                    await self._handle_command(
                        command,
                        connection_generation=connection_generation,
                    )
                finally:
                    self._client_command_type_context.reset(type_token)
                    self._client_command_context.reset(token)

        self._track_command_task(asyncio.create_task(_guarded_handle()))

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
        self._track_command_task(task)

    async def _run_durable_client_command(
        self,
        client_command_id: str,
        connection_generation: int,
    ) -> None:
        durable_queue = self._run_manager.durable_queue
        if durable_queue is None:
            return
        command = durable_queue.claim_client_command(client_command_id)
        if command is None:
            return
        try:
            async with self._command_semaphore:
                token = self._client_command_context.set(client_command_id)
                type_token = self._client_command_type_context.set(command.type)
                try:
                    await self._handle_command(
                        command,
                        connection_generation=connection_generation,
                    )
                finally:
                    self._client_command_type_context.reset(type_token)
                    self._client_command_context.reset(token)
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
            durable_queue.complete_client_command(client_command_id)
            self._mark_client_command_seen(command)

    def _prune_command_tasks(self) -> None:
        for task in list(self._command_tasks):
            if task.done():
                self._command_tasks.discard(task)

    def _track_command_task(self, task: asyncio.Task[Any]) -> None:
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)
        task.add_done_callback(self._on_command_task_done)

    @staticmethod
    def _on_command_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            if WebSocketSession._is_expected_disconnect_exception(exc):
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
            logger.debug("Failed to load recent client command ids for %s: %s", self.session_id, exc)
            return []

    def _client_command_id(self, command: UserCommand) -> str:
        client_command_id = command.data.get("client_command_id")
        if not isinstance(client_command_id, str):
            return ""
        return _clean_command_id(client_command_id)

    def _client_command_seen(self, command: UserCommand) -> bool:
        client_command_id = self._client_command_id(command)
        return bool(client_command_id and client_command_id in self._recent_client_command_id_set)

    def _mark_client_command_seen(self, command: UserCommand) -> bool:
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
            logger.debug("Failed to persist client command id for %s: %s", self.session_id, exc)
        pruned = False
        while len(self._recent_client_command_ids) > RECENT_CLIENT_COMMAND_IDS_MAX:
            removed = self._recent_client_command_ids.pop(0)
            self._recent_client_command_id_set.discard(removed)
            pruned = True
        if pruned:
            try:
                self._client_command_store.rewrite_ids(self._recent_client_command_ids)
            except Exception as exc:
                logger.debug("Failed to compact client command ids for %s: %s", self.session_id, exc)
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
        await self._send_ws_payload(
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
    ) -> None:
        token = self._event_connection_generation.set(
            self._connection_generation
            if connection_generation is None
            else connection_generation
        )
        try:
            if _is_conversation_lifecycle_command(command.type):
                manager = getattr(self, "_ws_manager", None)
                shared_lock_factory = getattr(
                    manager,
                    "conversation_lifecycle_lock",
                    None,
                )
                lifecycle_lock = (
                    shared_lock_factory()
                    if callable(shared_lock_factory)
                    else self._conversation_lifecycle_lock
                )
                async with lifecycle_lock:
                    manager = getattr(self, "_ws_manager", None)
                    fence_lookup = getattr(
                        manager,
                        "conversation_delete_fence",
                        None,
                    )
                    if callable(fence_lookup):
                        fenced_conversation_id = next(
                            (
                                conversation_id
                                for conversation_id in _command_targets_conversation_delete_fence(
                                    self,
                                    command,
                                )
                                if fence_lookup(conversation_id)
                            ),
                            "",
                        )
                        if fenced_conversation_id:
                            from backend.ws.command_results import emit_command_error

                            await emit_command_error(
                                self,
                                command.type,
                                "This conversation is being deleted; wait for deletion to finish before changing it.",
                                data={
                                    "conversation_id": fenced_conversation_id,
                                    "reason": "delete_in_progress",
                                    "retryable": True,
                                },
                            )
                            return
                    await self._handle_command_inner(command)
            else:
                await self._handle_command_inner(command)
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

                await emit_command_error(self, command.type, exc)
            except Exception:
                logger.error(
                    "Could not report the failure of command %s to the client",
                    command.type,
                    exc_info=True,
                )
        finally:
            self._event_connection_generation.reset(token)

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

        request_id, payload = self._normalize_control_response(command.data)
        if not payload:
            logger.debug("Ignoring empty control response for %s", request_id or "<missing>")
            return
        oauth_pending = getattr(self, "_provider_oauth_pending", {})
        oauth_future = oauth_pending.get(request_id)
        if oauth_future is not None:
            conversation_id = str(payload.get("conversation_id") or payload.get("conversationId") or "").strip()
            expected_conversation = str(getattr(oauth_future, "conversation_id", "") or "").strip()
            if expected_conversation and expected_conversation != conversation_id:
                logger.debug("Ignoring OAuth control response with invalid owner: %s", request_id)
                return
            if not oauth_future.done():
                oauth_future.set_result(payload)
            return
        owner_error = self._approval_response_owner_error(request_id, payload)
        if owner_error:
            logger.debug("Ignoring control response with invalid owner: %s", owner_error)
            return
        if not self._resolve_pending_approval(request_id, payload):
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
            await self._send_event(request.error_event)
            return False, target_conversation_id
        requested_workspace_path = request.project_path
        if requested_workspace_path is None:
            return False, target_conversation_id

        if workspace_path_needs_activation(requested_workspace_path, self._current_workspace_root()):
            activated = await self._activate_workspace_path(
                str(requested_workspace_path),
                announce=False,
                wait_for_initialize=True,
                error_command=None,
            )
            if not activated:
                return False, target_conversation_id

        updated_target = target_conversation_id
        if not updated_target:
            self._ensure_active_conversation()
            updated_target = self.active_conversation_id or ""

        if updated_target:
            git_branch = await asyncio.to_thread(self._git_branch_for, requested_workspace_path)
            await asyncio.to_thread(
                self.conversation_repo.update_workspace_binding,
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
            await self._send_event(
                AgentEvent.error(str(exc), recoverable=True, error_type="tool")
            )
            return False

        if target_conversation_id:
            self.conversation_repo.update_permission_mode(
                str(target_conversation_id),
                requested_permission_mode,
            )

        if not target_conversation_id or target_conversation_id == self.active_conversation_id:
            changed = self._set_permission_context_mode(requested_permission_mode, source="user_message")
            if changed:
                await self._emit_permission_mode_updated()
            if requested_permission_mode == "bypass":
                await self._auto_approve_pending_tool_approvals(
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
            stream_state = getattr(self, "_conversation_streams", {}).get(
                target_conversation_id
            )
            clean_message_id = str((stream_state or {}).get("message_id") or "").strip()
        if clean_message_id:
            done_event.data["message_id"] = clean_message_id
        await self._send_event(done_event)
        await self._send_event(
            AgentEvent.session_state_changed(
                state="idle",
                conversation_id=target_conversation_id,
                reason=reason,
            )
        )

    async def _handle_control_cancel(self, command: UserCommand) -> None:
        """Handle control_cancel_request command."""
        request_id = str(command.data.get("request_id") or "").strip()
        oauth_future = getattr(self, "_provider_oauth_pending", {}).get(request_id)
        if oauth_future is not None:
            supplied_conversation_id = str(
                command.data.get("conversation_id")
                or command.data.get("conversationId")
                or ""
            ).strip()
            expected_conversation_id = str(
                getattr(oauth_future, "conversation_id", "") or ""
            ).strip()
            if expected_conversation_id != supplied_conversation_id:
                logger.debug("Ignoring OAuth control cancellation with invalid owner: %s", request_id)
                return
        else:
            owner_error = self._approval_response_owner_error(request_id, command.data)
            if owner_error:
                logger.debug("Ignoring control cancellation with invalid owner: %s", owner_error)
                return
        self._resolve_pending_approval(
            request_id,
            {
                "action": "reject",
                "guidance": "control request cancelled by client",
            },
        )

    async def _handle_command_inner(self, command: UserCommand) -> None:
        if bool(getattr(self, "_extension_shutdown_requested", False)) and command.type not in {
            "control_response",
            "control_cancel_request",
            "interrupt",
        }:
            from backend.ws.command_results import emit_command_error

            await emit_command_error(
                self,
                command.type,
                "Session shutdown was requested by an extension; no new work is accepted.",
            )
            return

        if command.type == "user_message":
            content = str(command.data.get("content", ""))
            attachments = normalize_attachment_payloads(command.data.get("attachments", []))
            requested_conversation_id = str(
                command.data.get("conversation_id") or command.data.get("conversationId") or ""
            ).strip()
            target_conversation_id = requested_conversation_id or self.active_conversation_id or ""
            if requested_conversation_id:
                target = self.conversation_repo.get_conversation(requested_conversation_id)
                if target is None:
                    await emit_conversation_not_found(self, requested_conversation_id)
                    return
                target_conversation_id = target.id

            # Handle workspace switching if requested
            requested_workspace_root = str(
                command.data.get("workspace_root") or command.data.get("workspaceRoot") or ""
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
                            command.data.get("assistant_message_id")
                            or command.data.get("assistantMessageId")
                            or ""
                        ),
                    )
                    return

            # Handle permission mode update if requested
            requested_permission_mode = normalize_permission_mode(
                str(command.data.get("permission_mode") or command.data.get("permissionMode") or "")
            )
            permission_result = await self._handle_user_message_permission(
                requested_permission_mode, target_conversation_id
            )
            # Keep compatibility with host/test overrides written against the
            # pre-veto API, whose successful coroutine returned None.  Only an
            # explicit False is a policy veto; a truthy value or legacy None
            # continues the turn.
            if permission_result is False:
                await self._seal_unstarted_user_message(
                    target_conversation_id,
                    reason="permission_mode_rejected",
                    message_id=str(
                        command.data.get("assistant_message_id")
                        or command.data.get("assistantMessageId")
                        or ""
                    ),
                )
                return

            message_metadata: dict[str, Any] = {
                key: str(command.data.get(key) or "").strip()
                for key in ("primaryFile", "primary_file", "activeTabPath", "active_tab_path")
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
                        or item.get("configName")
                        or item.get("name")
                        or ""
                    ).strip(),
                    "path": str(item.get("path") or "").strip(),
                }
                for item in (command.data.get("plugins") or [])
                if isinstance(item, dict)
                and (
                    str(item.get("config_name") or item.get("configName") or item.get("name") or "").strip()
                    or str(item.get("path") or "").strip().startswith("plugin://")
                )
            ]
            if selected_plugins:
                message_metadata["selected_plugins"] = selected_plugins
            for source_key, target_key in (
                ("agent_mode", "agent_mode"),
                ("agentMode", "agent_mode"),
                ("agent_role", "agent_role"),
                ("agentRole", "agent_role"),
            ):
                value = str(command.data.get(source_key) or "").strip()
                if value:
                    message_metadata[target_key] = value
            assistant_message_id = str(
                command.data.get("assistant_message_id")
                or command.data.get("assistantMessageId")
                or ""
            ).strip() or f"assistant_{uuid.uuid4().hex}"
            message_metadata["assistant_message_id"] = assistant_message_id
            command.data["assistant_message_id"] = assistant_message_id
            user_message_id = str(
                command.data.get("user_message_id")
                or command.data.get("userMessageId")
                or ""
            ).strip() or f"user_{uuid.uuid4().hex}"
            message_metadata["user_message_id"] = user_message_id
            command.data["user_message_id"] = user_message_id

            stripped = content.lstrip()
            if stripped.startswith("/") and not stripped.startswith("//"):
                ensure_extension_commands = getattr(
                    self,
                    "_ensure_extension_commands_for_conversation",
                    None,
                )
                if callable(ensure_extension_commands) and target_conversation_id:
                    await ensure_extension_commands(target_conversation_id)
                parts = stripped.split(maxsplit=1)
                cmd_name = parts[0].lower()
                cmd_arg = parts[1] if len(parts) > 1 else ""
                if self.command_registry.dispatch_slash_sync(
                    cmd_name,
                    scope_id=target_conversation_id,
                ):
                    handled, content_override = await self.command_registry.dispatch_slash(
                        self,
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
                    self._ensure_active_conversation()
                    target_conversation_id = self.active_conversation_id or ""
                    if requested_permission_mode is not None and target_conversation_id:
                        self.conversation_repo.update_permission_mode(
                            str(target_conversation_id),
                            requested_permission_mode,
                        )
                queued_dispatch = bool(command.data.pop("_queued_user_message_dispatch", False))
                if retry_from_message_id:
                    # Regeneration is one server-owned state transition, matching
                    # MiniCode thread rollback followed by turn/start. Drain the old
                    # run and discard follow-ups before rewriting the transcript;
                    # otherwise this command can be mistaken for an ordinary busy
                    # follow-up and remain queued behind the answer it replaces.
                    current = self.conversation_repo.get_conversation(target_conversation_id)
                    if current is None:
                        # The client is showing a spinner it can only clear on a
                        # terminal event. Returning silently strands it, so
                        # report the vanished conversation through the same
                        # not-found path every other lookup failure uses.
                        await emit_conversation_not_found(self, target_conversation_id)
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
                        await self._send_event(error_event)
                        return
                    self._run_manager.clear_user_message_queue(target_conversation_id)
                    if self._running_agent_task_for(target_conversation_id):
                        await self._cancel_agent_runs(
                            conversation_id=target_conversation_id,
                            reason="user_regenerated",
                        )
                    current = self.conversation_repo.get_conversation(target_conversation_id)
                    if current is None:
                        # Deleted while the old run was being drained. The
                        # cancel above emits no client-visible terminal for the
                        # regenerate request itself, so close it here too.
                        await emit_conversation_not_found(self, target_conversation_id)
                        return
                    prepared = self._prepare_retry_from_message(
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
                        await self._send_event(error_event)
                        return
                running_for_target = self._running_agent_task_for(target_conversation_id)
                if (running_for_target or self._run_manager.is_queue_dispatching(target_conversation_id)) and not queued_dispatch:
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
                        stream_state = getattr(self, "_conversation_streams", {}).get(target_conversation_id) or {}
                        target_message_id = str(stream_state.get("message_id") or "").strip()
                        steered = self._run_manager.enqueue_user_message_as_steer(
                            target_conversation_id,
                            queued_command,
                            target_message_id=target_message_id,
                        )
                        if steered is not None:
                            await self._send_event(
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
                            await self._reject_pending_approvals(
                                reason="user_steer",
                                guidance=(
                                    "The user redirected the current task; this pending action was superseded."
                                ),
                                conversation_id=target_conversation_id,
                            )
                            return
                    position = self._run_manager.enqueue_user_message(target_conversation_id, queued_command)
                    await self._send_event(
                        AgentEvent.user_message_queue_updated(
                            status="queued",
                            conversation_id=target_conversation_id,
                            message_id=assistant_message_id,
                            user_message_id=str(command.data.get("user_message_id") or ""),
                            position=position,
                        )
                    )
                    return

                await self._start_agent_run(
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

        handled = await self.command_registry.dispatch(command.type, command.data)
        if not handled:
            from backend.ws.command_results import emit_command_error
            await emit_command_error(self, command.type, f"Unsupported command '{command.type}'")


    def _resolve_event_connection_generation(self) -> int | None:
        generation = self._event_connection_generation.get()
        return self._connection_generation if generation is None else generation

    def _can_send_for_generation(self, connection_generation: int | None = None) -> bool:
        generation = self._resolve_event_connection_generation() if connection_generation is None else connection_generation
        if generation != self._connection_generation:
            return False
        if not self._is_connected:
            return False

        application_state = getattr(self.ws, "application_state", None)
        client_state = getattr(self.ws, "client_state", None)
        return (
            application_state != WebSocketState.DISCONNECTED
            and client_state != WebSocketState.DISCONNECTED
        )

    @staticmethod
    def _is_expected_disconnect_exception(exc: Exception) -> bool:
        if isinstance(exc, (WebSocketDisconnect, ConnectionClosed)):
            return True
        if isinstance(exc, RuntimeError):
            message = str(exc).lower()
            return (
                "websocket is not connected" in message
                or "close message has been sent" in message
                or "after sending websocket.close" in message
                or 'cannot call "send"' in message
            )
        return False

    async def _send_ws_payload(
        self,
        payload: dict[str, Any],
        *,
        connection_generation: int | None = None,
        log_context: str,
        envelope: bool = True,
    ) -> bool:
        generation = self._resolve_event_connection_generation() if connection_generation is None else connection_generation
        payload = dict(payload)
        event_type = str(payload.get("type") or "").strip()
        command_context = getattr(self, "_client_command_context", None)
        command_type_context = getattr(self, "_client_command_type_context", None)
        command_id = command_context.get() if command_context is not None else ""
        command_type = command_type_context.get() if command_type_context is not None else ""
        if command_id:
            payload.setdefault("client_command_id", command_id)
        if command_type:
            payload.setdefault("client_command_type", command_type)
        try:
            validate_session_projection_payload(payload)
        except ValueError as exc:
            logger.warning(
                "Dropping invalid session/conversation websocket payload before sanitization: "
                "type=%s session=%s error=%s",
                event_type,
                self.session_id,
                exc,
            )
            return False
        payload = sanitize_ws_live_payload(payload)
        event_type = str(payload.get("type") or "").strip()
        if is_raw_provider_reasoning_event(payload):
            logger.warning(
                "Dropping raw provider reasoning websocket payload: type=%s session=%s",
                event_type,
                self.session_id,
            )
            return False
        # No second validation pass after sanitization: sanitize rewrites
        # string values only (secret redaction) and cannot introduce a
        # structural violation, so the pre-sanitize gate above already
        # covers the wire contract without doubling the recursive walk on
        # high-frequency delta events.
        if (
            _requires_conversation_owner(event_type, payload)
            and not str(payload.get("conversation_id") or "").strip()
        ):
            logger.warning(
                "Dropping conversation-scoped payload without conversation_id: type=%s session=%s keys=%s",
                event_type,
                self.session_id,
                sorted(payload.keys()),
            )
            return False
        if event_type in WORKSPACE_SCOPED_EVENT_TYPES and not str(payload.get("workspace_root") or "").strip():
            logger.warning(
                "Dropping workspace-scoped payload without workspace_root: type=%s session=%s keys=%s",
                event_type,
                self.session_id,
                sorted(payload.keys()),
            )
            return False
        try:
            async with self._ws_send_lock:
                # Allocate, stage and send under one lock so sequence, in-memory
                # replay order and wire order cannot diverge. Disk persistence is
                # chained separately; a slow filesystem must not hold up live or
                # terminal websocket events.
                enveloped = self._envelope_ws_payload(payload) if envelope else dict(payload)
                if self._is_replayable_ws_payload(enveloped):
                    replay_payload, rewrite_events = self._stage_ws_event(enveloped)
                    persist_snapshot = [
                        dict(event) for event in self._ws_event_log
                    ]
                    persist_task = asyncio.create_task(
                        self._persist_ws_event_after(
                            self._ws_event_persist_tail,
                            replay_payload,
                            rewrite_events,
                            persist_snapshot,
                        )
                    )
                    self._ws_event_persist_tail = persist_task
                if not self._can_send_for_generation(generation):
                    if self._has_active_run():
                        self._events_dropped_during_disconnect = True
                    logger.debug(
                        "Skipping %s for stale or disconnected websocket in session %s",
                        log_context,
                        self.session_id,
                    )
                    return False
                await self.ws.send_json(enveloped)
            return True
        except (AssertionError, RuntimeError) as exc:
            logger.debug(
                "Dropping %s due to websocket write contention in session %s: %s",
                log_context,
                self.session_id,
                exc,
            )
            return False
        except Exception as exc:
            if self._is_expected_disconnect_exception(exc):
                if self._has_active_run():
                    self._events_dropped_during_disconnect = True
                logger.debug(
                    "Dropping %s after websocket disconnect in session %s: %s",
                    log_context,
                    self.session_id,
                    exc,
                )
                return False
            raise

    def _envelope_ws_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ws_event_seq += 1
        seq = self._ws_event_seq
        enveloped = dict(payload)
        # Replay uses one transport-owned, session-global sequence. Turn-local
        # envelope sequences reset for every query and cannot be high-water marks.
        enveloped["seq"] = seq
        enveloped.setdefault("event_id", f"{self.session_id}:{self._event_instance_id}:{seq}")
        enveloped.setdefault("timestamp", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        return enveloped

    @staticmethod
    def _is_replayable_ws_payload(payload: dict[str, Any]) -> bool:
        event_type = str(payload.get("type") or "").strip()
        if is_non_replayable_event_type(event_type):
            return False
        if event_type.startswith("session."):
            return False
        return bool(str(payload.get("conversation_id") or "").strip())

    def _stage_ws_event(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        # Put the durable predecessor on the live wire as well as in the replay
        # log. A client can then distinguish a legitimate transient seq gap
        # from a durable event that was staged but never delivered.
        payload["previous_replay_seq"] = self._ws_replay_cursor
        replay_candidate = dict(payload)
        replay_payload = sanitize_ws_replay_payload(replay_candidate)
        try:
            self._ws_replay_cursor = int(replay_payload.get("seq") or self._ws_replay_cursor)
        except (TypeError, ValueError):
            pass
        self._ws_event_log.append(replay_payload)
        rewrite_events: list[dict[str, Any]] | None = None
        if len(self._ws_event_log) > WS_EVENT_REPLAY_MAX:
            del self._ws_event_log[: len(self._ws_event_log) - WS_EVENT_REPLAY_MAX]
            rewrite_events = [dict(event) for event in self._ws_event_log]
        return replay_payload, rewrite_events

    async def _persist_ws_event_after(
        self,
        previous: asyncio.Task[None] | None,
        replay_payload: dict[str, Any],
        rewrite_events: list[dict[str, Any]] | None,
        persist_snapshot: list[dict[str, Any]],
    ) -> None:
        failed_sequences = getattr(self, "_ws_replay_persistence_failed_seqs", None)
        if not isinstance(failed_sequences, set):
            failed_sequences = set()
            self._ws_replay_persistence_failed_seqs = failed_sequences
        predecessor_failed = False
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except asyncio.CancelledError:
                predecessor_failed = True
            except Exception:
                # A failed append must not poison the serialized tail. Rewrite
                # the complete staged prefix for this event so the durable log
                # catches up instead of silently skipping the failed predecessor.
                predecessor_failed = True
                logger.debug(
                    "Repairing websocket replay persistence after a failed predecessor",
                    exc_info=True,
                )
        try:
            if rewrite_events is not None or predecessor_failed:
                await asyncio.to_thread(
                    self._ws_event_store.rewrite,
                    rewrite_events if rewrite_events is not None else persist_snapshot,
                )
            else:
                await asyncio.to_thread(self._ws_event_store.append, replay_payload)
            if rewrite_events is not None or predecessor_failed:
                repaired_events = rewrite_events if rewrite_events is not None else persist_snapshot
                repaired_seqs = {
                    repaired_seq
                    for repaired in repaired_events
                    if (repaired_seq := WebSocketSession._replay_seq_value(repaired)) is not None
                }
                failed_sequences.difference_update(repaired_seqs)
            else:
                seq = WebSocketSession._replay_seq_value(replay_payload)
                if seq is not None:
                    failed_sequences.discard(seq)
        except Exception as exc:
            seq = WebSocketSession._replay_seq_value(replay_payload)
            if seq is not None:
                failed_sequences.add(seq)
            evidence = {
                "kind": "websocket_replay_persistence",
                "session_id": self.session_id,
                "seq": seq,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            errors = getattr(self, "_ws_replay_persistence_errors", None)
            if not isinstance(errors, list):
                errors = []
                self._ws_replay_persistence_errors = errors
            errors.append(evidence)
            del errors[:-20]
            logger.error(
                "Failed to persist websocket replay event for session %s (seq=%s)",
                self.session_id,
                seq,
                exc_info=True,
            )

    async def _drain_ws_event_persistence(self) -> None:
        """Flush the ordered replay tail without delaying live websocket sends."""
        tail = self._ws_event_persist_tail
        if tail is None or tail.done() or tail is asyncio.current_task():
            return
        try:
            await await_with_deadline(
                tail,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket replay persistence",
                owner=self._cleanup_tasks,
            )
        except Exception:
            logger.debug(
                "Failed to drain websocket replay persistence for session %s",
                self.session_id,
                exc_info=True,
            )

    async def _delete_replay_events_for_conversation(self, conversation_id: str) -> int:
        """Remove deleted-conversation replay state without racing queued writes."""

        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        async with self._ws_send_lock:
            await self._drain_ws_event_persistence()
            retained = [
                event
                for event in self._ws_event_log
                if str(event.get("conversation_id") or "").strip() != owner
            ]
            removed = len(self._ws_event_log) - len(retained)
            if not removed:
                return 0
            self._ws_event_log = retained
            try:
                await asyncio.to_thread(self._ws_event_store.rewrite, retained)
            except Exception:
                logger.debug(
                    "Failed to rewrite websocket replay after deleting conversation %s in session %s",
                    owner,
                    self.session_id,
                    exc_info=True,
                )
                raise
            return removed

    @staticmethod
    def _max_replay_event_seq(events: list[dict[str, Any]]) -> int:
        max_seq = 0
        for payload in events:
            try:
                seq = int(payload.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            max_seq = max(max_seq, seq)
        return max_seq

    @staticmethod
    def _replay_seq_value(payload: dict[str, Any], field: str = "seq") -> int | None:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value <= 0 or value > 9_007_199_254_740_991:
            return None
        return value

    def _replay_window_after(self, last_seq: int) -> tuple[list[dict[str, Any]], bool]:
        """Return one proven-complete durable replay chain after ``last_seq``.

        Wire sequence numbers also cover transient envelopes, so numeric gaps
        are valid.  The durable chain is instead defined by
        ``previous_replay_seq``.  Every link must be proven before any event is
        replayed: applying a valid prefix from a corrupt/incomplete window after
        an authoritative session snapshot can regress the UI just as surely as
        silently skipping the missing event.

        Legacy records predate the explicit link. They are compatible only when
        the immediately preceding retained durable record proves the link; the
        returned wire copies are upgraded with ``previous_replay_seq`` so the
        renderer never has to infer continuity from global sequence arithmetic.
        """

        try:
            current_seq = int(self._ws_replay_cursor or 0)
        except (TypeError, ValueError):
            return [], True
        if last_seq <= 0:
            return [], False
        if last_seq > current_seq:
            # A persisted client cursor can be ahead after a replay-log purge or
            # rollback. The authoritative restore snapshot must explicitly
            # rebase it; a normal replay cannot move a cursor backwards.
            return [], True
        if last_seq == current_seq:
            return [], False
        if not self._ws_event_log:
            return [], True

        first_after_index: int | None = None
        for index, payload in enumerate(self._ws_event_log):
            seq = self._replay_seq_value(payload)
            if seq is not None and seq > last_seq:
                first_after_index = index
                break
        if first_after_index is None:
            return [], True

        expected_previous = last_seq
        materialized: list[dict[str, Any]] = []
        for index in range(first_after_index, len(self._ws_event_log)):
            payload = self._ws_event_log[index]
            seq = self._replay_seq_value(payload)
            if seq is None or seq <= expected_previous:
                return [], True
            if seq in getattr(self, "_ws_replay_persistence_failed_seqs", set()):
                return [], True

            if "previous_replay_seq" in payload:
                previous_replay_seq = self._replay_seq_value(
                    payload,
                    "previous_replay_seq",
                )
                # Zero is the valid root link for the first ever durable event,
                # but restore replay is requested only for a positive cursor.
                if payload.get("previous_replay_seq") == 0:
                    previous_replay_seq = 0
                if previous_replay_seq != expected_previous:
                    return [], True
            else:
                # A legacy first record needs its acknowledged predecessor to be
                # physically retained. Later legacy records are safe because this
                # loop visits every intervening durable record in storage order.
                if index == first_after_index:
                    if index <= 0:
                        return [], True
                    retained_previous = self._replay_seq_value(
                        self._ws_event_log[index - 1]
                    )
                    if retained_previous != expected_previous:
                        return [], True

            replay_event = dict(payload)
            replay_event["previous_replay_seq"] = expected_previous
            materialized.append(replay_event)
            expected_previous = seq

        if expected_previous != current_seq:
            return [], True
        return materialized, False

    async def _replay_missed_events(
        self,
        last_seq: int,
        *,
        events: list[dict[str, Any]] | None = None,
        current_seq: int | None = None,
    ) -> int:
        if events is None:
            events, has_gap = self._replay_window_after(last_seq)
            if has_gap:
                return 0
        else:
            events = [dict(event) for event in events]
        if not events:
            return 0
        sent = await self._send_ws_payload(
            {
                "type": "session.replay",
                "last_seq": last_seq,
                "current_seq": self._ws_replay_cursor if current_seq is None else current_seq,
                "replayed_events": len(events),
                "events": events,
            },
            log_context="session.replay",
        )
        return len(events) if sent else 0

    async def _send_conversation_list(self) -> None:
        list_with_revision = getattr(
            self.conversation_repo,
            "list_conversations_with_revision",
            None,
        )
        inventory_instance_id: str | None = None
        inventory_revision: int | None = None
        if callable(list_with_revision):
            versioned_listing = list_with_revision()
            if isinstance(versioned_listing, tuple) and len(versioned_listing) == 3:
                raw_instance_id, raw_revision, summaries = versioned_listing
                if not isinstance(raw_instance_id, str) or not raw_instance_id.strip():
                    raise ValueError(
                        "list_conversations_with_revision returned an invalid inventory instance id"
                    )
                if (
                    isinstance(raw_revision, bool)
                    or not isinstance(raw_revision, int)
                    or raw_revision < 0
                    or raw_revision > 9_007_199_254_740_991
                ):
                    raise ValueError(
                        "list_conversations_with_revision returned an invalid inventory revision"
                    )
                inventory_instance_id = raw_instance_id.strip()
                inventory_revision = raw_revision
            elif isinstance(versioned_listing, tuple) and len(versioned_listing) == 2:
                # Compatibility for older repository doubles. They cannot
                # identify a durable inventory epoch, so keep the projection
                # explicitly legacy instead of inventing an empty instance.
                _legacy_revision, summaries = versioned_listing
            else:
                raise ValueError("list_conversations_with_revision returned an invalid result")
        else:
            summaries = self.conversation_repo.list_conversations()
        conversations = [
            item.to_dict()
            for item in summaries
            if getattr(item, "conversation_type", "main") == "main"
        ]
        snapshot_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        active = self.active_conversation
        if active is not None and (
            getattr(active, "archived", False)
            or getattr(active, "conversation_type", "main") != "main"
        ):
            active = None
            self.active_conversation_id = None
        payload = {
            "type": "conversation.list",
            "conversation_id": self.active_conversation_id,
            "active_conversation_id": self.active_conversation_id,
            "conversations": conversations,
            "active_conversation": project_public_conversation(active) if active is not None else None,
            "session": self.runtime_snapshot(),
            "snapshot_at": snapshot_at,
        }
        if inventory_instance_id is not None and inventory_revision is not None:
            payload["inventory_instance_id"] = inventory_instance_id
            payload["inventory_revision"] = inventory_revision
        await self._send_ws_payload(
            payload,
            log_context="conversation.list",
        )

    async def _send_event(self, event: AgentEvent) -> None:
        # Raw provider frames are an in-process SDK/extension surface. The
        # renderer receives typed text, tool, citation, usage, and Inspector
        # projections instead; forwarding ``sdk_only`` frames would put raw
        # reasoning, tool arguments, or provider bodies in browser memory even
        # when the UI deliberately ignores them.
        if event.type == "stream_event" and bool(
            event.data.get("sdk_only", True)
        ):
            return
        # Notices are transcript state, not session-global toasts. Producers in
        # the agent loop already stamp their immutable run owner; command-side
        # notices use the server-owned active conversation at the transport
        # boundary so a replay cannot attach them to whichever conversation the
        # renderer happens to have open later.
        if event.type == "system_notice" and not str(
            event.data.get("conversation_id") or ""
        ).strip():
            active_conversation_id = str(self.active_conversation_id or "").strip()
            if active_conversation_id:
                event.data["conversation_id"] = active_conversation_id
        if event.type == "command.result":
            command_id = self._client_command_context.get()
            details = event.data.get("data")
            result_data = dict(details) if isinstance(details, dict) else {}
            if command_id:
                result_data.setdefault("client_command_id", command_id)
            conversation_id = str(
                event.data.get("conversation_id")
                or result_data.get("conversation_id")
                or self.active_conversation_id
                or ""
            ).strip()
            if conversation_id:
                event.data.setdefault("conversation_id", conversation_id)
                result_data.setdefault("conversation_id", conversation_id)
            if result_data:
                event.data["data"] = result_data
        # Raw provider/tool traces are retained server-side and fetched only
        # when the user opens a concrete Inspector entry. This keeps the live
        # chat stream and client store compact without losing diagnostics.
        if event.type == "inspector.update" and not event.data.get("diagnostics_loaded"):
            target_kind = str(event.data.get("target_kind") or "message")
            target_id = str(event.data.get("target_id") or "").strip()
            diagnostic_payload = event.data.get("payload")
            if target_id and isinstance(diagnostic_payload, dict):
                event.data["payload"] = self._diagnostic_store.put(
                    target_kind,
                    target_id,
                    diagnostic_payload,
                    conversation_id=str(event.data.get("conversation_id") or ""),
                )
        elif event.type == "item.completed":
            # ``tool_calls`` is adapter-local metadata consumed by Pi before
            # the event reaches this transport boundary. Sending it again to
            # the renderer duplicates tool arguments that already have typed
            # tool_call events and creates a second, unused projection path.
            event.data.pop("tool_calls", None)
            provider_raw = event.data.get("provider_raw")
            if isinstance(provider_raw, dict) and provider_raw:
                item = event.data.get("item")
                item_id = str(item.get("id") or "") if isinstance(item, dict) else ""
                trace_id = str(
                    provider_raw.get("trace_id")
                    or f"{event.data.get('message_id') or event.data.get('conversation_id') or item_id or 'provider'}:provider:item"
                )
                event.data["provider_raw"] = self._diagnostic_store.put(
                    "provider",
                    trace_id,
                    provider_raw,
                    conversation_id=str(event.data.get("conversation_id") or ""),
                )
        elif event.type == "done":
            provider_raw = event.data.get("providerRaw")
            if isinstance(provider_raw, dict) and provider_raw:
                trace_id = str(
                    provider_raw.get("trace_id")
                    or f"{event.data.get('message_id') or event.data.get('conversation_id') or 'provider'}:provider:done"
                )
                event.data["providerRaw"] = self._diagnostic_store.put(
                    "provider",
                    trace_id,
                    provider_raw,
                    conversation_id=str(event.data.get("conversation_id") or ""),
                )
        target_conversation_id = str(event.data.get("conversation_id") or "").strip()
        if (
            _requires_conversation_owner(event.type, event.data)
            and not target_conversation_id
        ):
            logger.warning(
                "Dropping conversation-scoped event without conversation_id: type=%s session=%s keys=%s",
                event.type,
                self.session_id,
                sorted(event.data.keys()),
            )
            return
        target_workspace_root = str(event.data.get("workspace_root") or "").strip()
        if event.type in WORKSPACE_SCOPED_EVENT_TYPES and not target_workspace_root:
            logger.warning(
                "Dropping workspace-scoped event without workspace_root: type=%s session=%s keys=%s",
                event.type,
                self.session_id,
                sorted(event.data.keys()),
            )
            return
        apply_stream_event(
            getattr(self, "_conversation_streams", {}),
            target_conversation_id,
            event.type,
            event.data,
        )

        payload = self._build_ws_payload(event)
        if target_conversation_id:
            payload.setdefault("conversation_id", target_conversation_id)
            if event.type in _TURN_MESSAGE_SCOPED_EVENT_TYPES:
                stream_state = getattr(self, "_conversation_streams", {}).get(target_conversation_id)
                message_id = str((stream_state or {}).get("message_id") or "").strip()
                if message_id:
                    payload.setdefault("message_id", message_id)
        if event.type in {"approval_request", "ask_user"}:
            request_id = str(
                payload.get("request_id")
                or payload.get("tool_call_id")
                or event.data.get("request_id")
                or event.data.get("tool_call_id")
                or ""
            ).strip()
            if request_id:
                self._pending_approval_payloads[request_id] = dict(payload)

        await self._send_ws_payload(payload, log_context=f"event:{event.type}")
        if event.type not in _NOTIFICATION_HOOK_EVENT_TYPES:
            return
        # Notification is observational work. It must not hold up the
        # authoritative WebSocket projection or make a started child appear
        # only after it has completed. Keep the task session-owned so shutdown
        # can cancel and drain it with explicit cleanup accounting.
        hook_task = asyncio.create_task(
            self._run_notification_hook_for_event(event, dict(payload)),
            name=f"notification-hook:{event.type}",
        )
        self._notification_hook_tasks.add(hook_task)
        hook_task.add_done_callback(self._notification_hook_tasks.discard)
        hook_task.add_done_callback(_consume_task_result)

    async def _run_notification_hook_for_event(self, event: AgentEvent, payload: dict[str, Any]) -> None:
        from backend.hooks.runtime import run_notification_hook_for_event

        await run_notification_hook_for_event(event_type=event.type, payload=payload)

    def _build_ws_payload(self, event: AgentEvent) -> dict[str, Any]:
        if event.type == "approval_request":
            return self._build_approval_request_payload(event)

        if event.type == "ask_user":
            request_id = str(event.data.get("tool_call_id", "")).strip()
            question = str(event.data.get("question", "")).strip()
            request: dict[str, Any] = {
                "subtype": "elicitation",
                "tool_use_id": request_id,
                "prompt": question,
                "question": question,
            }
            for key in ("options", "choices", "allowed_values"):
                values = event.data.get(key)
                if isinstance(values, list):
                    request[key] = list(values)
            schema = event.data.get("schema")
            if isinstance(schema, dict):
                request["schema"] = dict(schema)
            return {
                "type": "control_request",
                "request_id": request_id,
                "request": request,
            }

        return event.to_ws_message()

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
        )
        session._ws_manager = self
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
        session._is_connected = False
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
                    await session.shutdown(reason="disconnect_timeout")
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
        session._is_connected = False
        _invalidate_runtime_status_cache()
        try:
            await session.shutdown(reason=reason)
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
                *(session.shutdown(reason=reason) for session in sessions),
                return_exceptions=True,
            )
        _invalidate_runtime_status_cache()

    async def broadcast_event(self, event: AgentEvent) -> None:
        sessions = [session for session in self._sessions.values() if session._is_connected]
        for session in sessions:
            await session._send_event(event)

    def runtime_snapshot(self) -> dict[str, Any]:
        sessions = [session for session in self._sessions.values() if session._is_connected]
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
        return sum(1 for session in self._sessions.values() if session._is_connected)

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

        for session in list(self._sessions.values()):
            session._is_connected = False

            active_task = getattr(session, "_active_run_task", None)
            if active_task and not active_task.done():
                active_cancel_event = getattr(session, "_active_run_cancel_event", None)
                if isinstance(active_cancel_event, asyncio.Event):
                    active_cancel_event.set()
                active_task.cancel()

            workspace_context_task = getattr(session, "_workspace_context_task", None)
            if workspace_context_task and not workspace_context_task.done():
                workspace_context_task.cancel()

            for future in getattr(session, "_pending_approvals", {}).values():
                if not future.done():
                    future.cancel()
            for future in getattr(session, "_provider_oauth_pending", {}).values():
                if not future.done():
                    future.cancel()
            getattr(session, "_pending_approvals", {}).clear()
            getattr(session, "_pending_approval_payloads", {}).clear()
            getattr(session, "_pending_approval_responses", {}).clear()
            getattr(session, "_provider_oauth_pending", {}).clear()
            getattr(session, "_approval_diff_cache", {}).clear()

            if session.file_watcher and session.file_watcher.is_running():
                session.file_watcher.stop()
            session.file_watcher = None

        self._sessions.clear()
