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
from backend.agent.loop import run_agent_loop, _mcp_registry_version
from backend.agent.query_engine import QueryEngine
from backend.agent.message import AgentEvent, UserCommand
from backend.attachments.store import AttachmentStore
from backend.artifact.store import ArtifactStore
from backend.checkpoint import CheckpointManager
from backend.commands.registry import CommandRegistry
from backend.config import AppConfig, get_available_models, get_llm_provider
from backend.conversations.models import DEFAULT_CONVERSATION_PERMISSION_MODE
from backend.conversations.repository import CONVERSATION_DATA_DIR, ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.profiles import permission_profile_for_mode, sandbox_status_for, workspace_scope_for
from backend.tasks.manager import TaskManager
from backend.terminal.session import TerminalSessionManager
from backend.terminal.manager import BackgroundCommandManager, BackgroundCommand
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import SessionAgentRunnerMixin
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.command_handlers import SessionCommandHandlersMixin
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin
from backend.ws.stream_state import append_stream_text
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

logger = logging.getLogger(__name__)

MAX_PENDING_COMMAND_TASKS = 100
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _invalidate_runtime_status_cache() -> None:
    try:
        from backend.api import _state

        _state.invalidate_status_cache()
    except Exception:
        logger.debug("Failed to invalidate status cache after websocket runtime change", exc_info=True)


CONVERSATION_SCOPED_EVENT_TYPES = {
    "text_chunk",
    "final_answer_started",
    "final_answer_delta",
    "final_answer_retracted",
    "final_answer_committed",
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
        rag_pipeline: Any | None = None,
        memory_manager: Any | None = None,
        vector_memory: Any | None = None,
        use_control_protocol: bool = False,
    ) -> None:
        self.session_id = session_id
        self.ws = websocket
        self._connection_generation = 1
        self._event_instance_id = uuid.uuid4().hex
        self._ws_event_seq = 0
        self._event_connection_generation: ContextVar[int | None] = ContextVar(
            f"ws_event_generation_{session_id}",
            default=None,
        )
        self.llm = llm
        self.artifact_store = artifact_store
        self.tool_registry = tool_registry
        # Snapshot of the MCP registry generation this tool_registry reflects.
        # Used to hot-reload MCP tools into a live session (see
        # refresh_tool_registry_if_mcp_changed) without restarting the backend.
        self._mcp_registry_version_snapshot = _mcp_registry_version()
        self.permission_checker = permission_checker
        self.config = config
        self.skill_executor = skill_executor
        self.memory_manager = memory_manager
        self.vector_memory = vector_memory
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._pending_approval_payloads: dict[str, dict[str, Any]] = {}
        self._approval_diff_cache: dict[str, dict[str, Any]] = {}
        self._active_run_task: asyncio.Task[None] | None = None
        self._active_task_id: str | None = None
        self._conversation_run_tasks: dict[str, asyncio.Task[None]] = {}
        self._conversation_run_task_ids: dict[str, str] = {}
        self._conversation_run_locks: dict[str, asyncio.Lock] = {}
        self._interrupted = False
        self._agent_run_lock = asyncio.Lock()
        self._command_semaphore = asyncio.Semaphore(20)  # max 20 concurrent commands
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._max_command_tasks = MAX_PENDING_COMMAND_TASKS
        self._ws_send_lock = asyncio.Lock()
        self._use_control_protocol = bool(use_control_protocol)
        self._is_connected = True
        # Streaming reconnection support
        self._conversation_streams: dict[str, dict[str, Any]] = {}
        self._resolve_llm_provider = get_llm_provider
        self._resolve_available_models = get_available_models
        self.provider = self._resolve_llm_provider()
        self.available_models = list(self._resolve_available_models(self.provider))
        self.selected_model = getattr(config.llm, "model", "").strip()
        if self.available_models and self.selected_model not in self.available_models:
            self.selected_model = ""
        if not self.selected_model and self.available_models:
            self.selected_model = self.available_models[0]
        self._model_override_active = False

        if skill_manager is not None:
            from backend.skills.manager import SkillManager

            self.skill_manager = SkillManager(loader=skill_manager._loader)
            self.skill_manager._discovered = skill_manager._discovered
        else:
            self.skill_manager = None

        self.context_builder = ContextBuilder(
            token_budget=config.token_budget,
            agent_settings=config.agent,
            skill_executor=skill_executor,
            rag_pipeline=rag_pipeline,
            memory_manager=memory_manager,
            llm=llm,
            skill_manager=self.skill_manager,
            vector_memory=vector_memory,
        )
        self.attachment_store = AttachmentStore()
        self.checkpoint_manager = CheckpointManager()
        self.conversation_repo = ConversationRepository(CONVERSATION_DATA_DIR)
        self.query_engine = QueryEngine(runner=run_agent_loop)
        self.task_manager = TaskManager(on_change=self._schedule_task_runtime_update)
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
            on_completed=self._on_background_command_completed
        )
        self._workspace_context = None
        self._start_file_watcher()
        self._workspace_context_task: asyncio.Task[None] | None = None

        # State objects (P4.3: refactoring to reduce god-object complexity)
        from backend.ws.session_state import (
            ConnectionState,
            ConversationState,
            StreamState,
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
        self._stream_state = StreamState(
            streams=self._conversation_streams,
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

    async def _on_terminal_output(self, session_id: str, data: str) -> None:
        try:
            await self._send_ws_payload(
                {
                    "type": "terminal.output",
                    "session_id": session_id,
                    "data": data,
                },
                log_context="terminal.output",
            )
        except Exception:
            logger.exception("Failed to send terminal.output for session %s", session_id)

    async def _on_terminal_exit(self, session_id: str, exit_code: int) -> None:
        try:
            await self._send_ws_payload(
                {
                    "type": "terminal.exit",
                    "session_id": session_id,
                    "exit_code": exit_code,
                },
                log_context="terminal.exit",
            )
        except Exception:
            logger.exception("Failed to send terminal.exit for session %s", session_id)

    async def _on_background_command_completed(self, bg_cmd: BackgroundCommand) -> None:
        try:
            output_preview = bg_cmd.output[:2000] if bg_cmd.output else ""
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
                },
                log_context="background.completed",
            )
        except Exception:
            pass

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

            active_previews = running_preview_processes()
            if active_previews:
                await self._send_ws_payload(
                    {
                        "type": "preview.refreshed",
                        "path": relative_path,
                        "url": active_previews[0].effective_url,
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
        invoked_skill_names = (
            sorted(self.skill_manager.get_active_names())
            if self.skill_manager is not None
            else []
        )
        pending_approvals = self._pending_approval_runtime_items()
        workspace_root = self._workspace_root_for_conversation()
        active = self.active_conversation
        workspace_scope = workspace_scope_for(
            workspace_root=getattr(active, "workspace_root", "") if active is not None else "",
            worktree_path=getattr(active, "worktree_path", "") if active is not None else "",
        )
        permission_profile = permission_profile_for_mode(self.permission_context.mode)
        return {
            "session_id": self.session_id,
            "parent_session_id": None,
            "active_conversation_id": self.active_conversation_id,
            "workspace_root": str(workspace_root) if workspace_root is not None else None,
            "active_conversation": active.to_meta_dict()
            if active is not None
            else None,
            "active_task_id": self._active_task_id,
            "selected_model": self.selected_model or None,
            "invoked_skill_names": invoked_skill_names,
            "permission_mode": self.permission_context.mode,
            "permission_profile": permission_profile,
            "permission_source": self.permission_context.source,
            "workspace_scope": workspace_scope,
            "sandbox_status": sandbox_status_for(permission_profile),
            "mcp": self._mcp_summary(),
            "task_summary": task_summary,
            "running_tasks": running_tasks[:5],
            "pending_approval_count": len(pending_approvals),
            "pending_approvals": pending_approvals[:5],
        }

    def _mcp_summary(self) -> dict[str, Any]:
        """Compact MCP summary for the runtime snapshot: counts + short statuses.

        Kept intentionally small (no tools/errors/full dicts) so the snapshot
        stays light. Reads the in-memory manager status; empty when no bootstrap.
        """
        try:
            from backend.main import get_mcp_status

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
        task = self._active_run_task
        if task is not None and not task.done():
            return True
        for run_task in self._conversation_run_tasks.values():
            if run_task is not None and not run_task.done():
                return True
        return False

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
        current_version = _mcp_registry_version()
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
        loop.create_task(self._send_task_runtime_update())

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
            from backend.main import get_mcp_status

            mcp_status = get_mcp_status() or []
            await self._send_event(AgentEvent(type="mcp_status", data={"servers": mcp_status}))
            await self._send_llm_state()

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
                await self._send_client_command_ack(command)
                if command.type == "ping":
                    await self._send_ws_payload({"type": "pong"}, log_context="pong")
                else:
                    self._prune_command_tasks()
                    if len(self._command_tasks) >= self._max_command_tasks:
                        await self._send_event(
                            AgentEvent.error(
                                "Too many pending commands; please wait for current work to finish.",
                                recoverable=True,
                                error_type="rate_limit",
                            )
                        )
                        continue

                    async def _guarded_handle(command, connection_generation):
                        async with self._command_semaphore:
                            return await self._handle_command(
                                command,
                                connection_generation=connection_generation,
                            )

                    task = asyncio.create_task(
                        _guarded_handle(command, active_generation)
                    )
                    self._track_command_task(task)

        except WebSocketDisconnect:
            logger.info("session %s disconnected", self.session_id)
        except Exception as exc:
            logger.error("session %s failed: %s", self.session_id, exc, exc_info=True)
        finally:
            if active_generation == self._connection_generation:
                self.artifact_store.clear()

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

    async def _send_client_command_ack(self, command: UserCommand) -> None:
        client_command_id = command.data.get("client_command_id")
        if not isinstance(client_command_id, str) or not client_command_id.strip():
            return
        await self._send_ws_payload(
            {
                "type": "client.command.ack",
                "client_command_id": client_command_id.strip()[:128],
                "command_type": command.type,
            },
            log_context="client.command.ack",
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
            await self._send_event(AgentEvent.error("Workspace path is required", recoverable=True))
            return
        activated = await self._activate_workspace_path(
            requested_workspace_root,
            announce=True,
            wait_for_initialize=True,
        )
        if not activated:
            return
        if not self.active_conversation_id:
            self._ensure_active_conversation()
        if self.active_conversation_id:
            workspace_path = str(self._current_workspace_root())
            self.conversation_repo.update_workspace_binding(
                str(self.active_conversation_id),
                workspace_root=workspace_path,
                git_branch=self._git_branch_for(Path(workspace_path)),
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
        from backend.workspace.path_utils import normalize_project_import_path

        try:
            requested_workspace_path = normalize_project_import_path(requested_workspace_root)
        except Exception as exc:
            error_event = AgentEvent.error(f"Invalid workspace path: {exc}", recoverable=True)
            if target_conversation_id:
                error_event.data["conversation_id"] = target_conversation_id
            await self._send_event(error_event)
            return False, target_conversation_id

        if not requested_workspace_path.exists() or not requested_workspace_path.is_dir():
            error_event = AgentEvent.error(
                f"Workspace does not exist: {requested_workspace_root}",
                recoverable=True,
            )
            if target_conversation_id:
                error_event.data["conversation_id"] = target_conversation_id
            await self._send_event(error_event)
            return False, target_conversation_id

        current_workspace_path = self._current_workspace_root().resolve()
        if requested_workspace_path.resolve() != current_workspace_path:
            activated = await self._activate_workspace_path(
                str(requested_workspace_path),
                announce=False,
                wait_for_initialize=True,
            )
            if not activated:
                return False, target_conversation_id

        updated_target = target_conversation_id
        if not updated_target:
            self._ensure_active_conversation()
            updated_target = self.active_conversation_id or ""

        if updated_target:
            self.conversation_repo.update_workspace_binding(
                str(updated_target),
                workspace_root=str(requested_workspace_path),
                git_branch=self._git_branch_for(requested_workspace_path),
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

            message_metadata = {
                key: str(command.data.get(key) or "").strip()
                for key in ("primaryFile", "primary_file", "activeTabPath", "active_tab_path")
                if str(command.data.get(key) or "").strip()
            }

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
                running_for_target = self._conversation_run_tasks.get(target_conversation_id)
                if running_for_target and not running_for_target.done():
                    await self._send_event(
                        AgentEvent(
                            type="error",
                            data={
                                "message": (
                                    "A response is already running in this conversation. "
                                    "Please wait, approve/reject the pending tool request, or stop it before sending another message."
                                ),
                                "recoverable": True,
                                "error_type": "tool",
                                "error_code": "agent.busy",
                                "conversation_id": target_conversation_id,
                            },
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
                managed_run = self.task_manager.create(
                    "agent.run",
                    self._run_agent(
                        content,
                        attachments=attachments,
                        conversation_id=target_conversation_id,
                        metadata=message_metadata,
                    ),
                )
                if target_conversation_id:
                    self._conversation_run_tasks[target_conversation_id] = managed_run.task
                    self._conversation_run_task_ids[target_conversation_id] = managed_run.id
                if target_conversation_id == self.active_conversation_id:
                    self._active_run_task = managed_run.task
                    self._active_task_id = managed_run.id
                self._schedule_task_runtime_update()

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
                            recoverable=True,
                            error_type="api",
                        )
                        if target_conversation_id:
                            error_event.data["conversation_id"] = target_conversation_id
                        await self._send_event(
                            error_event
                        )
                    finally:
                        if target_conversation_id and self._conversation_run_tasks.get(target_conversation_id) is managed_run.task:
                            self._conversation_run_tasks.pop(target_conversation_id, None)
                        if target_conversation_id and self._conversation_run_task_ids.get(target_conversation_id) == managed_run.id:
                            self._conversation_run_task_ids.pop(target_conversation_id, None)
                        # Clean up conversation run lock
                        self._conversation_run_locks.pop(target_conversation_id, None)
                        if self._active_run_task is managed_run.task:
                            self._active_run_task = None
                        if self._active_task_id == managed_run.id:
                            self._active_task_id = None
                        self._schedule_task_runtime_update()

                asyncio.create_task(_wait_and_cleanup())
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
            await self._send_event(
                AgentEvent.error(f"Unsupported command '{command.type}'", recoverable=True, error_type="tool")
            )


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
    ) -> bool:
        generation = self._resolve_event_connection_generation() if connection_generation is None else connection_generation
        if not self._can_send_for_generation(generation):
            logger.debug(
                "Skipping %s for stale or disconnected websocket in session %s",
                log_context,
                self.session_id,
            )
            return False

        try:
            async with self._ws_send_lock:
                await self.ws.send_json(self._envelope_ws_payload(payload))
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
        enveloped.setdefault("seq", seq)
        enveloped.setdefault("event_id", f"{self.session_id}:{self._event_instance_id}:{seq}")
        enveloped.setdefault("timestamp", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        return enveloped

    async def _send_conversation_list(self) -> None:
        conversations = [item.to_dict() for item in self.conversation_repo.list_conversations()]
        active = self.active_conversation
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

    async def _send_event(self, event: AgentEvent) -> None:
        target_conversation_id = str(event.data.get("conversation_id") or "").strip()
        if event.type in CONVERSATION_SCOPED_EVENT_TYPES and not target_conversation_id:
            logger.warning(
                "Dropping conversation-scoped event without conversation_id: type=%s session=%s keys=%s",
                event.type,
                self.session_id,
                sorted(event.data.keys()),
            )
            return
        if event.type in {"text_chunk", "final_answer_delta"}:
            chunk = str(event.data.get("content", ""))
            append_stream_text(
                getattr(self, "_conversation_streams", {}),
                str(target_conversation_id or ""),
                chunk,
            )
        elif event.type == "final_answer_retracted":
            stream_state = getattr(self, "_conversation_streams", {}).get(str(target_conversation_id or ""))
            if stream_state is not None:
                stream_state["accumulated_text"] = ""

        payload = self._build_ws_payload(event)
        if target_conversation_id:
            payload.setdefault("conversation_id", target_conversation_id)
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
        rag_pipeline: Any | None = None,
        memory_manager: Any | None = None,
        vector_memory: Any | None = None,
    ) -> tuple[WebSocketSession, int]:
        await websocket.accept()

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
            rag_pipeline=rag_pipeline,
            memory_manager=memory_manager,
            vector_memory=vector_memory,
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

        # Immediately cancel pending approvals so the agent loop
        # is not blocked for up to 5 minutes waiting for a response
        # from a disconnected client.
        if hasattr(session, "_cancel_pending_approvals"):
            try:
                cancel_result = session._cancel_pending_approvals(reason="websocket_disconnect")
                if asyncio.iscoroutine(cancel_result):
                    cancel_task = asyncio.create_task(cancel_result)

                    def _log_cancel_error(task: asyncio.Task) -> None:
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            logger.debug("Error cancelling approvals for %s on disconnect", session_id)

                    cancel_task.add_done_callback(_log_cancel_error)
            except Exception:
                logger.debug("Error cancelling approvals for %s on disconnect", session_id)

        async def delayed_cleanup():
            try:
                await asyncio.sleep(30.0)
                if session_id in self._sessions and self._sessions[session_id] is session:
                    # Cancel active agent run to stop consuming LLM tokens.
                    active_task = getattr(session, "_active_run_task", None)
                    if active_task and not active_task.done():
                        active_task.cancel()
                        logger.info(
                            "Cancelled active agent task for session %s after disconnect timeout",
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
                active_task.cancel()

            workspace_context_task = getattr(session, "_workspace_context_task", None)
            if workspace_context_task and not workspace_context_task.done():
                workspace_context_task.cancel()

            for future in getattr(session, "_pending_approvals", {}).values():
                if not future.done():
                    future.cancel()
            getattr(session, "_pending_approvals", {}).clear()
            getattr(session, "_pending_approval_payloads", {}).clear()
            getattr(session, "_approval_diff_cache", {}).clear()

            if session.file_watcher and session.file_watcher.is_running():
                session.file_watcher.stop()
            session.file_watcher = None

        self._sessions.clear()
