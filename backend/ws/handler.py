from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

from backend.agent.context import ContextBuilder
from backend.agent.loop import run_agent_loop
from backend.agent.loop_session import mcp_registry_version
from backend.agent.query_engine import QueryEngine
from backend.agent.message import AgentEvent, UserCommand
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    await_with_deadline,
    cancel_and_drain,
)
from backend.attachments.store import AttachmentStore
from backend.artifact.store import ArtifactStore
from backend.checkpoint import CheckpointManager
from backend.commands.registry import CommandRegistry
from backend.config import AppConfig, get_available_models, get_llm_provider, get_models_source
from backend.conversations.models import DEFAULT_CONVERSATION_PERMISSION_MODE
from backend.conversations.repository import CONVERSATION_DATA_DIR, ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.profiles import permission_profile_for_mode, sandbox_status_for, workspace_scope_for
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
from backend.ws.event_log import WebSocketReplayEventStore, sanitize_ws_replay_payload
from backend.ws.fork_registry import ForkRegistry
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin
from backend.ws.run_manager import SessionRunManager
from backend.ws.stream_state import apply_stream_event
from backend.ws.utils import (
    build_effective_transcript_content,
    build_effective_user_message,
    build_inherited_memory_note,
    build_summary_from_facts,
    build_summary_from_transcript,
    inherit_conversation_fact,
    merge_conversation_facts,
    normalize_attachment_payloads,
    normalize_permission_mode,
    rebuild_local_facts_from_transcript,
    uses_control_protocol,
)
from backend.workspace.file_watcher import WorkspaceFileWatcher
from backend.workspace.state import clear_active_workspace_root, get_active_workspace_root
from backend.api.auth import _websocket_accept_subprotocol

logger = logging.getLogger(__name__)

MAX_PENDING_COMMAND_TASKS = 100
COMMAND_BACKLOG_ERROR_INTERVAL_SECONDS = 2.0
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
WS_EVENT_REPLAY_MAX = 1000
RECENT_CLIENT_COMMAND_IDS_MAX = 1024

COMMAND_BACKLOG_BYPASS_TYPES = {
    "approval",
    "answer",
    "control_response",
    "control_cancel_request",
    "interrupt",
}

COMMAND_BACKLOG_DROPPABLE_TYPES = {
    "commands.list",
    "connectors.marketplace.list",
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

NON_REPLAYABLE_WS_EVENT_TYPES = {
    "conversation.list",
    "conversation.switched",
    "llm.model.updated",
    "mcp_status",
    "pong",
    "runtime.capabilities",
    "session.restored",
    "session.synced",
    "stream_resume",
}


def _invalidate_runtime_status_cache() -> None:
    try:
        from backend.api import _state

        _state.invalidate_status_cache()
    except Exception:
        logger.debug("Failed to invalidate status cache after websocket runtime change", exc_info=True)


CONVERSATION_SCOPED_EVENT_TYPES = {
    "item.started",
    "agent_message.delta",
    "item.completed",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "tool_result",
    "agent.progress",
    "approval_request",
    "approval.file_diff",
    "ask_user",
    "context_usage",
    "context_compacted",
    "budget_update",
    "budget.warning",
    "command_output_chunk",
    "artifact.preview",
    "done",
    "stream_resume",
    "conversation.hydration.updated",
    "conversation.compaction.updated",
    "conversation.summary.updated",
    "goal.updated",
    "plan_step_updated",
    "plan_updated",
    "subagent.start",
    "subagent.event",
    "subagent.progress",
    "subagent.done",
    "citation.add",
    "inspector.update",
    "control_request",
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
        use_control_protocol: bool = False,
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
        self._event_connection_generation: ContextVar[int | None] = ContextVar(
            f"ws_event_generation_{session_id}",
            default=None,
        )
        self._client_command_context: ContextVar[str] = ContextVar(
            f"ws_client_command_{session_id}",
            default="",
        )
        self.llm = llm
        self.artifact_store = artifact_store
        self.tool_registry = tool_registry
        # Snapshot of the MCP registry generation this tool_registry reflects.
        # Used to hot-reload MCP tools into a live session (see
        # refresh_tool_registry_if_mcp_changed) without restarting the backend.
        self._mcp_registry_version_snapshot = mcp_registry_version()
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
        self._conversation_run_locks: dict[str, asyncio.Lock] = {}
        self._run_manager = SessionRunManager(self)
        self._interrupted = False
        self._interrupted_conversation_ids: set[str] = set()
        self._agent_run_lock = asyncio.Lock()
        # Conversation lifecycle commands are received concurrently so slow
        # workspace activation cannot let a later delete/create/user message
        # overtake an earlier switch. Keep their observable order per session.
        self._conversation_lifecycle_lock = asyncio.Lock()
        self._command_semaphore = asyncio.Semaphore(20)  # max 20 concurrent commands
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._active_client_command_ids: set[str] = set()
        self._max_command_tasks = MAX_PENDING_COMMAND_TASKS
        self._last_command_backlog_error_at = 0.0
        self._recent_client_command_ids: list[str] = self._load_recent_client_command_ids()
        self._recent_client_command_id_set: set[str] = set(self._recent_client_command_ids)
        self._ws_send_lock = asyncio.Lock()
        self._ws_event_persist_tail: asyncio.Task[None] | None = None
        self._use_control_protocol = bool(use_control_protocol)
        self._is_connected = True
        # Streaming reconnection support
        self._conversation_streams: dict[str, dict[str, Any]] = {}
        self._resolve_llm_provider = get_llm_provider
        self._resolve_available_models = get_available_models
        self._resolve_models_source = get_models_source
        self._llm_adapter_cache: dict[tuple[Any, ...], LLMAdapter] = {}
        self._llm_close_tasks: set[asyncio.Task[Any]] = set()
        self.provider = self._resolve_llm_provider()
        self.available_models = list(self._resolve_available_models(self.provider))
        self.models_source = self._resolve_models_source(self.provider)
        self.selected_model = getattr(config.llm, "model", "").strip()
        if self.available_models and self.selected_model not in self.available_models:
            self.selected_model = ""
        if not self.selected_model and self.available_models:
            self.selected_model = self.available_models[0]
        self._llm_adapter_cache[
            _llm_adapter_cache_key(
                config=config,
                provider=self.provider,
                model=self.selected_model,
            )
        ] = llm
        self._model_override_active = False

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
        self.query_engine = QueryEngine(runner=run_agent_loop)
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
            load_profile_memory=self._load_profile_memory,
            inherit_fact=inherit_conversation_fact,
            merge_facts=merge_conversation_facts,
            build_summary_from_facts=build_summary_from_facts,
            build_inherited_memory_note=build_inherited_memory_note,
            build_effective_transcript_content=build_effective_transcript_content,
            build_summary_from_transcript=build_summary_from_transcript,
            rebuild_local_facts_from_transcript=rebuild_local_facts_from_transcript,
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
        )
        self._fork_registry = ForkRegistry(
            session_id=session_id,
            root_dir=Path(CONVERSATION_DATA_DIR).parent / "session-forks",
        )
        self._workspace_context = None
        self._start_file_watcher()
        self._workspace_context_task: asyncio.Task[None] | None = None

        # State objects (P4.3: refactoring to reduce god-object complexity)
        from backend.ws.session_state import (
            ConnectionState,
            ConversationState,
            WorkspaceState,
        )

        self._connection_state = ConnectionState(
            ws=websocket,
            generation=self._connection_generation,
        )
        self._conversation_state = ConversationState(
            repo=self.conversation_repo,
            active_id=None,
            run_tasks=self._conversation_run_tasks,
            run_task_ids=self._conversation_run_task_ids,
        )
        default_workspace = get_active_workspace_root()
        self._workspace_state = WorkspaceState(
            root=default_workspace,
            default_root=default_workspace,
        )

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
                    )},
                },
                log_context="background.completed",
            )
        except Exception as exc:
            logger.debug("Failed to send background.completed for %s: %s", bg_cmd.command_id, exc)

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

    def _start_file_watcher(self):
        workspace_root = self._workspace_root_for_conversation()
        if workspace_root is None:
            logger.info("No workspace bound for session %s; file watcher not started", self.session_id)
            return
        try:
            self.file_watcher = WorkspaceFileWatcher(
                workspace_root=workspace_root,
                on_change=self._on_file_changed,
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

        try:
            self.file_watcher = WorkspaceFileWatcher(
                workspace_root=workspace_root.resolve(),
                on_change=self._on_file_changed,
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
        task = getattr(self, "_workspace_context_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._workspace_context_task = None
        self._workspace_context = None
        if self.file_watcher is not None:
            self.file_watcher.stop()
            self.file_watcher = None
        clear_active_workspace_root()

    def _current_workspace_root(self) -> Path:
        """Get current workspace root. Uses WorkspaceState when available."""
        # Prefer workspace_context if available (for active conversation)
        workspace_context = getattr(self, "_workspace_context", None)
        if workspace_context is not None:
            workspace_root = getattr(workspace_context, "root_path", None)
            if workspace_root is not None:
                resolved = Path(workspace_root).resolve()
                # Sync to workspace state
                if hasattr(self, '_workspace_state') and self._workspace_state.root != resolved:
                    self._workspace_state.switch_to(resolved)
                return resolved

        # Otherwise use workspace state
        if hasattr(self, '_workspace_state'):
            return self._workspace_state.root

        # No fallback to cwd - only return workspace if explicitly bound
        # This matches Codex behavior: chat-only mode shows no Git info
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

    async def _on_file_changed(self, path: Path, event_type: str):
        try:
            workspace_root = self._current_workspace_root()
            try:
                relative_path = str(path.relative_to(workspace_root))
            except ValueError:
                relative_path = path.name
            await self._send_ws_payload(
                {
                    "type": "file.changed",
                    "path": relative_path,
                    "event": event_type,
                    "timestamp": asyncio.get_running_loop().time(),
                },
                log_context="file.changed",
            )

            from backend.preview.launcher import running_preview_processes

            active_previews = running_preview_processes(
                session_id=self.session_id,
                conversation_id=str(self.active_conversation_id or ""),
                workspace_root=workspace_root,
            )
            if active_previews:
                await self._send_ws_payload(
                    {
                        "type": "preview.refreshed",
                        "path": relative_path,
                        "url": active_previews[0].effective_url,
                        "conversation_id": str(self.active_conversation_id or ""),
                    },
                    log_context="preview.refreshed",
                )

            if path.name == "CLAUDE.md" or path.name.endswith(".md"):
                if "CLAUDE" in path.name or ".claude" in str(path):
                    logger.info("CLAUDE.md changed, clearing guideline cache")
                    from backend.agent.claude_md import clear_guideline_cache

                    clear_guideline_cache()
                    await self._send_ws_payload(
                        {
                            "type": "guidelines.updated",
                            "message": "Project guidelines have been updated",
                        },
                        log_context="guidelines.updated",
                    )
        except Exception as exc:
            logger.error("Error handling file change: %s", exc, exc_info=True)

    @property
    def connection_generation(self) -> int:
        return self._connection_state.generation

    def attach_websocket(self, websocket: WebSocket) -> tuple[WebSocket, int]:
        previous = self.ws
        self.ws = websocket
        self._connection_generation += 1
        self._connection_state.ws = websocket
        self._connection_state.generation = self._connection_generation
        self._is_connected = True
        return previous, self._connection_generation

    def set_control_protocol(self, enabled: bool) -> None:
        self._use_control_protocol = bool(enabled)

    @property
    def active_conversation_id(self) -> str | None:
        return self.conversation_runtime.active_conversation_id

    @active_conversation_id.setter
    def active_conversation_id(self, value: str | None) -> None:
        self.conversation_runtime.active_conversation_id = value

    def _create_fresh_active_conversation(self) -> None:
        self.conversation_runtime.create_fresh_active_conversation()
        self._sync_permission_mode_with_active_conversation(source="conversation.create")

    def _ensure_active_conversation(
        self, preferred_id: str | None = None
    ) -> None:
        self.conversation_runtime.ensure_active_conversation(preferred_id)
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
            item: dict[str, Any] = {
                "request_id": request_id,
                "type": str(payload.get("type") or "approval_pending"),
            }
            conversation_id = str(payload.get("conversation_id") or "").strip()
            if conversation_id:
                item["conversation_id"] = conversation_id
            if item["type"] == "control_request" and isinstance(payload.get("request"), dict):
                request = payload["request"]
                item["subtype"] = str(request.get("subtype") or "").strip()
                tool_name = str(request.get("tool_name") or "").strip()
                if tool_name:
                    item["tool_name"] = tool_name
            else:
                tool_name = str(payload.get("tool_name") or "").strip()
                if tool_name:
                    item["tool_name"] = tool_name
                if item["type"] == "ask_user":
                    item["subtype"] = "elicitation"
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
        if active is not None and getattr(active, "archived", False):
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
            "active_conversation": active.to_meta_dict()
            if active is not None
            else None,
            "active_task_id": self._active_task_id,
            "active_stream_conversation_ids": active_stream_conversation_ids,
            "selected_model": self.selected_model or None,
            "invoked_skill_names": invoked_skill_names,
            "permission_mode": self.permission_context.mode,
            "permission_profile": permission_payload["profile"],
            "permission_source": self.permission_context.source,
            "workspace_scope": workspace_scope,
            "sandbox_status": permission_payload["sandbox_status"],
            "mcp": self._mcp_summary(),
            "capabilities": self.runtime_capability_summary(permission_payload=permission_payload),
            "task_summary": task_summary,
            "running_tasks": running_tasks[:5],
            "pending_approval_count": len(pending_approvals),
            "pending_approvals": pending_approvals[:5],
            "forks": [record.to_dict() for record in fork_records[-20:]],
            "queued_user_messages": queued_user_messages[:20],
            "pending_turn_inputs": pending_turn_inputs[:20],
        }

    def _runtime_permission_payload(
        self,
        *,
        workspace_scope: str | None = None,
    ) -> dict[str, Any]:
        profile = permission_profile_for_mode(self.permission_context.mode)
        return {
            "mode": self.permission_context.mode,
            "profile": profile,
            "source": self.permission_context.source,
            "workspace_scope": workspace_scope or getattr(self.permission_context, "workspace_scope", "computer"),
            "sandbox_status": sandbox_status_for(profile),
        }

    def runtime_capability_summary(
        self,
        *,
        permission_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compact per-session capability contract for runtime snapshots."""
        self.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
        permission = permission_payload or self._runtime_permission_payload()
        try:
            summary = self.tool_registry.build_capability_summary(
                permission_checker=self.permission_checker,
                permission_context=self.permission_context,
            )
        except Exception as exc:
            logger.debug("session %s capability summary failed: %s", self.session_id, exc)
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
            }
        return {
            "version": self.tool_registry.version,
            "summary": summary,
            "permission": permission,
            "mcp_registry_version": self._mcp_registry_version_snapshot,
            "provider_capabilities": self._provider_capabilities_payload(),
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
        budget = int(getattr(getattr(self.config, "token_budget", None), "tool_schemas", 6000) or 6000)
        snapshot = self.tool_registry.build_snapshot(
            budget=budget,
            permission_checker=self.permission_checker,
            permission_context=self.permission_context,
            mcp_registry_version=self._mcp_registry_version_snapshot,
        )
        snapshot["composer_commands"] = get_enabled_composer_command_catalog()
        snapshot["feature_flags"] = feature_flags_payload()
        if self.skill_manager is not None and not snapshot.get("skills"):
            list_all = getattr(self.skill_manager, "list_all", None)
            skills = list_all() if callable(list_all) else []
            if skills:
                snapshot["skills"] = skills
                snapshot["summary"] = {
                    **dict(snapshot.get("summary") or {}),
                    "skills": len(skills),
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
                {"name": s.get("name"), "status": s.get("status"), "phase": s.get("phase")}
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

        task = asyncio.create_task(_dispatch())
        self._track_command_task(task)

    def _cancel_child_subagents_for_task_id(self, task_id: str | None, *, reason: str) -> None:
        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            return
        try:
            from backend.agent.runtime import default_runtime

            runtime = default_runtime()
            cancel_children = getattr(runtime, "cancel_child_subagent_tasks_for_task", None)
            if callable(cancel_children):
                cancel_children(clean_task_id, reason=reason)
        except Exception:
            logger.debug(
                "Failed to cancel child subagents for task %s in session %s",
                clean_task_id,
                self.session_id,
                exc_info=True,
            )

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
        current_version = mcp_registry_version()
        if current_version == self._mcp_registry_version_snapshot:
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
            self.tool_registry = bootstrap.create_tool_registry(self.artifact_store)
        except Exception as exc:  # pragma: no cover - never break a run/inspect
            logger.warning("Failed to rebuild tool registry after MCP change: %s", exc)
            return False
        self._mcp_registry_version_snapshot = current_version
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
    ) -> bool:
        return self.conversation_runtime.load_active_conversation_snapshot(
            conversation_id,
            snapshot,
            notify=notify,
            on_hydration_complete=self._on_conversation_hydration_complete,
        )

    async def _on_conversation_hydration_complete(self, conversation_id: str) -> None:
        await self._send_ws_payload(
            {
                "type": "conversation.hydration.updated",
                "conversation_id": conversation_id,
                "is_hydrating": False,
            },
            log_context="conversation.hydration.updated",
        )

    def _build_inherited_snapshot(
        self,
        memory_mode: str,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        return self.conversation_runtime.build_inherited_snapshot(memory_mode)

    def _load_profile_memory(self) -> str:
        if self.memory_manager is None:
            return ""
        try:
            raw = self.memory_manager.read_file("user_profile.md") or ""
        except Exception:
            return ""
        lines = [line.rstrip() for line in raw.splitlines() if "<!--" not in line]
        content = "\n".join(line for line in lines if line.strip()).strip()
        return content

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
            from backend.api.routes_health import get_mcp_status

            mcp_status = get_mcp_status() or []
            await self._send_event(AgentEvent(type="mcp_status", data={"servers": mcp_status}))
            await self._send_llm_state()
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
        self._run_manager.clear_all_user_message_queues()
        try:
            await self._cancel_agent_runs(reason=reason)
        except Exception:
            logger.debug("Failed to cancel agent runs during session shutdown", exc_info=True)
        try:
            from backend.agent.runtime import default_runtime

            runtime = default_runtime()
            cancel_session_children = getattr(runtime, "cancel_subagent_tasks_for_session", None)
            if callable(cancel_session_children):
                cancel_session_children(self.session_id, reason=reason)
        except Exception:
            logger.debug("Failed to cancel subagents during session shutdown", exc_info=True)

        current = asyncio.current_task()
        command_tasks = [
            task
            for task in list(self._command_tasks)
            if task is not current and not task.done()
        ]
        if command_tasks:
            await cancel_and_drain(
                command_tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket command tasks",
            )
        self._command_tasks.clear()

        try:
            await await_with_deadline(
                self.task_manager.cancel_all_and_wait(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="managed task shutdown",
            )
        except Exception:
            logger.debug("Failed to drain managed tasks during session shutdown", exc_info=True)
        runtime_update_task = self._task_runtime_update_task
        if runtime_update_task is not None and runtime_update_task is not current:
            await cancel_and_drain(
                [runtime_update_task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="runtime update task",
            )
        self._task_runtime_update_task = None
        if self.file_watcher and self.file_watcher.is_running():
            self.file_watcher.stop()
        try:
            await await_with_deadline(
                self.terminal_manager.destroy_all(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="terminal shutdown",
            )
        except Exception:
            logger.debug("Failed to destroy terminals during session shutdown", exc_info=True)
        try:
            from backend.preview import stop_preview_launches_for_session

            await await_with_deadline(
                stop_preview_launches_for_session(self.session_id),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="preview shutdown",
            )
        except Exception:
            logger.debug("Failed to stop previews during session shutdown", exc_info=True)
        try:
            await await_with_deadline(
                self.background_manager.shutdown(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="background command shutdown",
            )
        except Exception:
            logger.debug("Failed to stop background commands during session shutdown", exc_info=True)
        try:
            from backend.ws.agent_runner import _clear_session_llm_cache

            _clear_session_llm_cache(self)
            if self._llm_close_tasks:
                await cancel_and_drain(
                    tuple(self._llm_close_tasks),
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label="LLM adapter close tasks",
                )
                self._llm_close_tasks.clear()
        except Exception:
            logger.debug("Failed to close session LLM adapters", exc_info=True)
        await self.artifact_store.flush()
        self.artifact_store.shutdown()
        self.artifact_store.clear()

    async def _replay_pending_client_commands(self, connection_generation: int) -> None:
        durable_queue = self._run_manager.durable_queue
        if durable_queue is None:
            return
        for command in durable_queue.pending_client_commands():
            command_id = self._client_command_id(command)
            if not command_id or self._client_command_seen(command):
                continue
            await self._send_client_command_ack(command, duplicate=True)
            self._schedule_durable_client_command(command_id, connection_generation)

    def _schedule_transient_client_command(
        self,
        command: UserCommand,
        connection_generation: int,
    ) -> None:
        async def _guarded_handle() -> None:
            async with self._command_semaphore:
                token = self._client_command_context.set(self._client_command_id(command))
                try:
                    await self._handle_command(
                        command,
                        connection_generation=connection_generation,
                    )
                finally:
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
                try:
                    await self._handle_command(
                        command,
                        connection_generation=connection_generation,
                    )
                finally:
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
            if (
                command.type == "user_message"
                or command.type == "session.restore"
                or command.type.startswith("conversation.")
            ):
                async with self._conversation_lifecycle_lock:
                    await self._handle_command_inner(command)
            else:
                await self._handle_command_inner(command)
        finally:
            self._event_connection_generation.reset(token)

    async def _handle_workspace_set(self, command: UserCommand) -> None:
        """Handle workspace.set command."""
        requested_workspace_root = str(
            command.data.get("path")
            or command.data.get("workspace_root")
            or command.data.get("workspaceRoot")
            or ""
        ).strip()
        if not requested_workspace_root:
            from backend.ws.command_results import emit_command_error
            await emit_command_error(self, "workspace.set", "Workspace path is required")
            return
        activated = await self._activate_workspace_path(
            requested_workspace_root,
            announce=True,
            wait_for_initialize=True,
            error_command="workspace.set",
        )
        if not activated:
            return
        if not self.active_conversation_id:
            self._ensure_active_conversation()
        if self.active_conversation_id:
            workspace_path = str(self._current_workspace_root())
            git_branch = await asyncio.to_thread(self._git_branch_for, Path(workspace_path))
            await asyncio.to_thread(
                self.conversation_repo.update_workspace_binding,
                str(self.active_conversation_id),
                workspace_root=workspace_path,
                git_branch=git_branch,
                worktree_path="",
                git_isolated=False,
            )

    async def _handle_approval(self, command: UserCommand) -> None:
        """Handle approval command."""
        tool_call_id = command.data.get("tool_call_id", "")
        if not self._resolve_pending_approval(tool_call_id, dict(command.data)):
            logger.debug("Ignoring stale approval response for %s", tool_call_id)

    async def _handle_answer(self, command: UserCommand) -> None:
        """Handle answer command."""
        tool_call_id = command.data.get("tool_call_id", "")
        if not self._resolve_pending_approval(tool_call_id, dict(command.data)):
            logger.debug("Ignoring stale question response for %s", tool_call_id)

    async def _handle_control_response(self, command: UserCommand) -> None:
        """Handle control_response command."""
        request_id, payload = self._normalize_control_response(command.data)
        if payload:
            self._resolve_pending_approval(request_id, payload)

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
    ) -> None:
        """Handle permission mode update for user_message command."""
        if requested_permission_mode is None:
            return

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

    async def _handle_control_cancel(self, command: UserCommand) -> None:
        """Handle control_cancel_request command."""
        request_id = str(command.data.get("request_id") or "").strip()
        self._resolve_pending_approval(
            request_id,
            {
                "action": "reject",
                "guidance": "control request cancelled by client",
            },
        )

    async def _handle_command_inner(self, command: UserCommand) -> None:
        if command.type == "workspace.set":
            await self._handle_workspace_set(command)
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
                    return

            # Handle permission mode update if requested
            requested_permission_mode = normalize_permission_mode(
                str(command.data.get("permission_mode") or command.data.get("permissionMode") or "")
            )
            await self._handle_user_message_permission(requested_permission_mode, target_conversation_id)

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
                parts = stripped.split(maxsplit=1)
                cmd_name = parts[0].lower()
                cmd_arg = parts[1] if len(parts) > 1 else ""
                if self.command_registry.dispatch_slash_sync(cmd_name):
                    handled, content_override = await self.command_registry.dispatch_slash(
                        self,
                        cmd_name,
                        cmd_arg,
                        attachments,
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
                    position = self._run_manager.enqueue_user_message(target_conversation_id, queued_command)
                    if position <= 0:
                        await self._send_event(
                            AgentEvent.user_message_queue_updated(
                                status="cancelled",
                                conversation_id=target_conversation_id,
                                message_id=assistant_message_id,
                                user_message_id=str(command.data.get("user_message_id") or ""),
                                reason="queue_full",
                            )
                        )
                        error = AgentEvent.error(
                            "This conversation already has 20 queued messages. Wait for one to start or cancel a queued message.",
                            recoverable=True,
                            error_type="rate_limit",
                        )
                        error.data.update({
                            "error_code": "agent.queue_full",
                            "conversation_id": target_conversation_id,
                            **({"message_id": assistant_message_id} if assistant_message_id else {}),
                        })
                        await self._send_event(error)
                        return
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

                if retry_from_message_id:
                    current = self.conversation_repo.get_conversation(target_conversation_id)
                    if current is None:
                        return
                    prepared = self._prepare_retry_from_message(
                        conversation=current,
                        retry_from_message_id=retry_from_message_id,
                    )
                    if prepared is None:
                        error_event = AgentEvent.error(
                            f"Cannot regenerate from message '{retry_from_message_id}'",
                            recoverable=True,
                            error_type="tool",
                        )
                        error_event.data["conversation_id"] = target_conversation_id
                        await self._send_event(
                            error_event
                        )
                        return
                # Install the turn-local queue before creating the task. A
                # steer can arrive immediately after the user command is
                # accepted; pre-installing removes the narrow race that used
                # to fall back to cancelling the whole run.
                self._run_manager.turn_input_queue(target_conversation_id)
                run_cancel_event = asyncio.Event()
                event_generation_token = self._event_connection_generation.set(None)
                try:
                    managed_run = self.task_manager.create(
                        "agent.run",
                        self._run_agent(
                            content,
                            attachments=attachments,
                            conversation_id=target_conversation_id,
                            metadata=message_metadata,
                            cancel_event=run_cancel_event,
                        ),
                    )
                finally:
                    self._event_connection_generation.reset(event_generation_token)
                self._register_agent_run(
                    conversation_id=target_conversation_id,
                    task=managed_run.task,
                    task_id=managed_run.id,
                    cancel_event=run_cancel_event,
                )

                async def _wait_and_cleanup():
                    try:
                        if managed_run.task is not None:
                            await managed_run.task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logging.error("Chat run failed: %s", e, exc_info=True)
                        error_event = AgentEvent.error(
                            f"Chat run failed: {e}",
                            recoverable=False,
                            error_type="runtime",
                        )
                        if target_conversation_id:
                            error_event.data["conversation_id"] = target_conversation_id
                        await self._send_event(
                            error_event
                        )
                    finally:
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
            return

        if command.type == "approval":
            await self._handle_approval(command)
            return

        if command.type == "answer":
            await self._handle_answer(command)
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
        persist_task: asyncio.Task[None] | None = None
        try:
            async with self._ws_send_lock:
                # Allocate, stage and send under one lock so sequence, in-memory
                # replay order and wire order cannot diverge. Disk persistence is
                # chained separately; a slow filesystem must not hold up live or
                # terminal websocket events.
                enveloped = self._envelope_ws_payload(payload) if envelope else dict(payload)
                if self._is_replayable_ws_payload(enveloped):
                    replay_payload, rewrite_events = self._stage_ws_event(enveloped)
                    persist_task = asyncio.create_task(
                        self._persist_ws_event_after(
                            self._ws_event_persist_tail,
                            replay_payload,
                            rewrite_events,
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
            if persist_task is not None:
                await asyncio.shield(persist_task)
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
        if not event_type or event_type in NON_REPLAYABLE_WS_EVENT_TYPES:
            return False
        if event_type.startswith("session."):
            return False
        return bool(str(payload.get("conversation_id") or "").strip())

    def _stage_ws_event(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        replay_payload = sanitize_ws_replay_payload(payload)
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
    ) -> None:
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except asyncio.CancelledError:
                pass
        try:
            if rewrite_events is not None:
                await asyncio.to_thread(self._ws_event_store.rewrite, rewrite_events)
            else:
                await asyncio.to_thread(self._ws_event_store.append, replay_payload)
        except Exception as exc:
            logger.debug("Failed to persist websocket replay event for session %s: %s", self.session_id, exc)

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

    def _replayable_events_after(self, last_seq: int) -> list[dict[str, Any]]:
        if last_seq <= 0:
            return []
        events: list[dict[str, Any]] = []
        for payload in self._ws_event_log:
            try:
                seq = int(payload.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq > last_seq:
                events.append(dict(payload))
        return events

    def _event_log_has_gap_after(self, last_seq: int) -> bool:
        if last_seq <= 0 or last_seq >= self._ws_event_seq:
            return False
        if not self._ws_event_log:
            return True
        try:
            first_seq = int(self._ws_event_log[0].get("seq") or 0)
        except (TypeError, ValueError):
            return True
        return first_seq > last_seq + 1

    async def _replay_missed_events(
        self,
        last_seq: int,
        *,
        events: list[dict[str, Any]] | None = None,
        current_seq: int | None = None,
    ) -> int:
        events = self._replayable_events_after(last_seq) if events is None else [dict(event) for event in events]
        if not events:
            return 0
        sent = await self._send_ws_payload(
            {
                "type": "session.replay",
                "last_seq": last_seq,
                "current_seq": self._ws_event_seq if current_seq is None else current_seq,
                "replayed_events": len(events),
                "events": events,
            },
            log_context="session.replay",
        )
        return len(events) if sent else 0

    async def _send_conversation_list(self) -> None:
        conversations = [item.to_dict() for item in self.conversation_repo.list_conversations()]
        active = self.active_conversation
        if active is not None and getattr(active, "archived", False):
            active = None
            self.active_conversation_id = None
        await self._send_ws_payload(
            {
                "type": "conversation.list",
                "conversation_id": self.active_conversation_id,
                "active_conversation_id": self.active_conversation_id,
                "conversations": conversations,
                "active_conversation": active.to_dict() if active is not None else None,
                "session": self.runtime_snapshot(),
            },
            log_context="conversation.list",
        )

    async def _send_event(self, event: AgentEvent | dict[str, Any]) -> None:
        # A few declarative conversation handlers historically emitted the
        # lightweight ``{"type": ..., "data": ...}`` shape directly. Normalize
        # it at the transport boundary so those handlers receive the same
        # conversation scoping, notification hooks, replay envelope, and
        # flattened wire payload as regular AgentEvent producers.
        if isinstance(event, dict):
            event_type = str(event.get("type") or "error")
            raw_data = event.get("data")
            if isinstance(raw_data, dict):
                event_data = dict(raw_data)
                event_data.update({
                    key: value
                    for key, value in event.items()
                    if key not in {"type", "data"}
                })
            else:
                event_data = {
                    key: value
                    for key, value in event.items()
                    if key != "type"
                }
            event = AgentEvent(type=event_type, data=event_data)  # type: ignore[arg-type]
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
        if event.type in CONVERSATION_SCOPED_EVENT_TYPES and not target_conversation_id:
            logger.warning(
                "Dropping conversation-scoped event without conversation_id: type=%s session=%s keys=%s",
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

        await self._run_notification_hook_for_event(event, payload)
        await self._send_ws_payload(payload, log_context=f"event:{event.type}")

    async def _run_notification_hook_for_event(self, event: AgentEvent, payload: dict[str, Any]) -> None:
        from backend.hooks.runtime import run_notification_hook_for_event

        await run_notification_hook_for_event(event_type=event.type, payload=payload)

    def _build_ws_payload(self, event: AgentEvent) -> dict[str, Any]:
        if event.type == "approval_request":
            return self._build_approval_request_payload(event)

        if not self._use_control_protocol:
            return event.to_ws_message()

        if event.type == "ask_user":
            request_id = str(event.data.get("tool_call_id", "")).strip()
            question = str(event.data.get("question", "")).strip()
            if request_id:
                return {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {
                        "subtype": "elicitation",
                        "tool_use_id": request_id,
                        "prompt": question,
                        "question": question,
                    },
                }

        return event.to_ws_message()

class WebSocketManager:
    def __init__(self) -> None:
        self._sessions: dict[str, WebSocketSession] = {}
        self._disconnect_tasks: dict[str, asyncio.Task] = {}

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
    ) -> tuple[WebSocketSession, int]:
        await websocket.accept(subprotocol=_websocket_accept_subprotocol(websocket))

        requested_session_id = (websocket.query_params.get("session_id") or "").strip()
        use_control_protocol = uses_control_protocol(websocket.query_params.get("protocol"))
        if requested_session_id and not SESSION_ID_PATTERN.fullmatch(requested_session_id):
            await websocket.close(code=1008, reason="invalid session_id")
            raise WebSocketDisconnect(code=1008)
        session_id = requested_session_id or f"session_{uuid.uuid4().hex}"

        if session_id in self._disconnect_tasks:
            self._disconnect_tasks[session_id].cancel()
            del self._disconnect_tasks[session_id]
            logger.info(f"Cancelled disconnect cleanup task for session {session_id} due to reconnection")

        existing_session = self._sessions.get(session_id)
        if existing_session:
            existing_session.set_control_protocol(use_control_protocol)
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
            use_control_protocol=use_control_protocol,
        )
        self._sessions[session_id] = session
        _invalidate_runtime_status_cache()
        return session, session.connection_generation

    def get_session(self, session_id: str) -> WebSocketSession | None:
        return self._sessions.get(session_id)

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
                    # Preserve pending approvals during the reconnect grace
                    # period. A successfully reconnected client receives them
                    # again through _reemit_pending_state. Only reject them once
                    # the session is genuinely being torn down.
                    if hasattr(session, "_cancel_pending_approvals"):
                        try:
                            await session._cancel_pending_approvals(reason="websocket_disconnect_timeout")
                        except Exception:
                            logger.debug("Error cancelling approvals for %s after disconnect timeout", session_id)
                    # Cancel all agent runs owned by this session to stop
                    # background conversations from consuming tokens or tools.
                    session._run_manager.clear_all_user_message_queues()
                    cancelled = False
                    cancel_runs = getattr(session, "_cancel_agent_runs", None)
                    if callable(cancel_runs):
                        cancelled = await cancel_runs(reason="websocket_disconnect")
                    else:
                        active_task = getattr(session, "_active_run_task", None)
                        if active_task and not active_task.done():
                            active_cancel_event = getattr(session, "_active_run_cancel_event", None)
                            if isinstance(active_cancel_event, asyncio.Event):
                                active_cancel_event.set()
                            active_task.cancel()
                            cancelled = True
                    if cancelled:
                        logger.info(
                            "Cancelled agent task(s) for session %s after disconnect timeout",
                            session_id,
                        )

                    if session.file_watcher and session.file_watcher.is_running():
                        session.file_watcher.stop()
                        logger.info(
                            "File watcher stopped for session %s after timeout",
                            session_id,
                        )

                    if hasattr(session, "terminal_manager"):
                        await session.terminal_manager.destroy_all()
                    if hasattr(session, "background_manager"):
                        await session.background_manager.shutdown()

                    from backend.hooks.runtime import run_session_end_hook

                    await run_session_end_hook(session_id=session_id, reason="disconnect_timeout")

                    self._sessions.pop(session_id, None)
                    logger.info("Session %s cleaned up after disconnect timeout", session_id)
            except asyncio.CancelledError:
                logger.info(
                    "Session %s cleanup cancelled due to successful reconnection",
                    session_id,
                )
            finally:
                self._disconnect_tasks.pop(session_id, None)

        old_task = self._disconnect_tasks.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._disconnect_tasks[session_id] = asyncio.create_task(delayed_cleanup())

    async def shutdown(self, *, reason: str = "application_shutdown") -> None:
        """Drain disconnect timers and all live sessions before loop teardown."""
        cleanup_tasks = list(self._disconnect_tasks.values())
        self._disconnect_tasks.clear()
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

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
            "sessions": session_snapshots,
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
            getattr(session, "_pending_approvals", {}).clear()
            getattr(session, "_pending_approval_payloads", {}).clear()
            getattr(session, "_pending_approval_responses", {}).clear()
            getattr(session, "_approval_diff_cache", {}).clear()

            if session.file_watcher and session.file_watcher.is_running():
                session.file_watcher.stop()
            session.file_watcher = None

        self._sessions.clear()
