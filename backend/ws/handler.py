from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

from backend.agent.context import ContextBuilder
from backend.agent.loop import run_agent_loop
from backend.agent.query_engine import QueryEngine
from backend.agent.message import AgentEvent, UserCommand
from backend.attachments.store import AttachmentStore
from backend.artifact.store import ArtifactStore
from backend.checkpoint import CheckpointManager
from backend.commands.registry import CommandRegistry
from backend.config import AppConfig, get_available_models, get_llm_provider, load_config
from backend.conversations.repository import CONVERSATION_DATA_DIR, ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tasks.manager import TaskManager
from backend.terminal.session import TerminalSessionManager
from backend.terminal.manager import BackgroundCommandManager, BackgroundCommand
from backend.tools.base import PermissionLevel
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import SessionAgentRunnerMixin
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.command_handlers import SessionCommandHandlersMixin
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.permission_runtime import SessionPermissionRuntimeMixin
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
from backend.workspace.state import get_active_workspace_root

logger = logging.getLogger(__name__)

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

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
        self._event_connection_generation: ContextVar[int | None] = ContextVar(
            f"ws_event_generation_{session_id}",
            default=None,
        )
        self.llm = llm
        self.artifact_store = artifact_store
        self.tool_registry = tool_registry
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
        self._interrupted = False
        self._agent_run_lock = asyncio.Lock()
        self._ws_send_lock = asyncio.Lock()
        self._use_control_protocol = bool(use_control_protocol)
        self._is_connected = True
        # Streaming reconnection support
        self._streaming_conversation_id: str | None = None
        self._streaming_message_id: str | None = None
        self._streaming_accumulated_text: str = ""
        self.provider = get_llm_provider()
        self.available_models = get_available_models(self.provider)
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
            mode="default",
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
        self.background_manager = BackgroundCommandManager(
            on_completed=self._on_background_command_completed
        )
        self._workspace_context = None
        self._start_file_watcher()
        self._workspace_context_task: asyncio.Task[None] | None = None

    async def _init_workspace_context(self):
        try:
            from backend.workspace.context import WorkspaceContext

            ctx = WorkspaceContext(self._current_workspace_root())
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
            pass

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
            pass

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
        try:
            workspace_root = self._current_workspace_root()
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

    def _current_workspace_root(self) -> Path:
        workspace_context = getattr(self, "_workspace_context", None)
        if workspace_context is not None:
            workspace_root = getattr(workspace_context, "root_path", None)
            if workspace_root is not None:
                return Path(workspace_root).resolve()
        return get_active_workspace_root(Path.cwd())

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
        return self._connection_generation

    def attach_websocket(self, websocket: WebSocket) -> tuple[WebSocket, int]:
        previous = self.ws
        self.ws = websocket
        self._connection_generation += 1
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
        return {
            "session_id": self.session_id,
            "parent_session_id": None,
            "active_conversation_id": self.active_conversation_id,
            "workspace_root": str(self._current_workspace_root()),
            "active_conversation": self.active_conversation.to_meta_dict()
            if self.active_conversation is not None
            else None,
            "active_task_id": self._active_task_id,
            "selected_model": self.selected_model or None,
            "invoked_skill_names": invoked_skill_names,
            "permission_mode": self.permission_context.mode,
            "permission_source": self.permission_context.source,
            "task_summary": task_summary,
            "running_tasks": running_tasks[:5],
            "pending_approval_count": len(self._pending_approvals),
        }

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

            mcp_status = get_mcp_status()
            if mcp_status:
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
                if command.type == "ping":
                    await self._send_ws_payload({"type": "pong"}, log_context="pong")
                else:
                    task = asyncio.create_task(
                        self._handle_command(
                            command,
                            connection_generation=active_generation,
                        )
                    )
                    task.add_done_callback(self._on_command_task_done)

        except WebSocketDisconnect:
            logger.info("session %s disconnected", self.session_id)
        except Exception as exc:
            logger.error("session %s failed: %s", self.session_id, exc, exc_info=True)
        finally:
            if active_generation == self._connection_generation:
                self.artifact_store.clear()

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

    async def _handle_command_inner(self, command: UserCommand) -> None:
        if command.type == "user_message":
            content = str(command.data.get("content", ""))
            attachments = normalize_attachment_payloads(command.data.get("attachments", []))
            requested_workspace_root = str(
                command.data.get("workspace_root") or command.data.get("workspaceRoot") or ""
            ).strip()
            if requested_workspace_root:
                from backend.workspace.path_utils import normalize_project_import_path

                try:
                    requested_workspace_path = normalize_project_import_path(requested_workspace_root)
                except Exception as exc:
                    await self._send_event(
                        AgentEvent.error(f"Invalid workspace path: {exc}", recoverable=True)
                    )
                    return
                if not requested_workspace_path.exists() or not requested_workspace_path.is_dir():
                    await self._send_event(
                        AgentEvent.error(
                            f"Workspace does not exist: {requested_workspace_root}",
                            recoverable=True,
                        )
                    )
                    return

                current_workspace_path = self._current_workspace_root().resolve()
                if requested_workspace_path.resolve() != current_workspace_path:
                    activated = await self._activate_workspace_path(
                        str(requested_workspace_path),
                        announce=False,
                    )
                    if not activated:
                        return

                if not self.active_conversation_id:
                    self._ensure_active_conversation()
                if self.active_conversation_id:
                    self.conversation_repo.update_workspace_binding(
                        str(self.active_conversation_id),
                        workspace_root=str(requested_workspace_path),
                        git_branch=self._git_branch_for(requested_workspace_path),
                        worktree_path="",
                        git_isolated=False,
                    )

            requested_permission_mode = normalize_permission_mode(
                str(command.data.get("permission_mode") or command.data.get("permissionMode") or "")
            )
            if requested_permission_mode is not None:
                if not self.active_conversation_id:
                    self._ensure_active_conversation()
                if self.active_conversation_id:
                    self.conversation_repo.update_permission_mode(
                        str(self.active_conversation_id),
                        requested_permission_mode,
                    )
                if self._set_permission_context_mode(requested_permission_mode, source="user_message"):
                    await self._emit_permission_mode_updated()

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
                if self._agent_run_lock.locked() or (self._active_run_task and not self._active_run_task.done()):
                    await self._send_event(
                        AgentEvent.error(
                            "A response is already running. Please wait, approve/reject the pending tool request, or stop it before sending another message.",
                            recoverable=True,
                            error_type="tool",
                            error_code="agent.busy",
                        )
                    )
                    return

                if retry_from_message_id:
                    current = self.active_conversation
                    if current is None:
                        self._ensure_active_conversation()
                        current = self.active_conversation
                    if current is None:
                        return
                    prepared = self._prepare_retry_from_message(
                        conversation=current,
                        retry_from_message_id=retry_from_message_id,
                    )
                    if prepared is None:
                        await self._send_event(
                            AgentEvent.error(
                                f"Cannot regenerate from message '{retry_from_message_id}'",
                                recoverable=True,
                                error_type="tool",
                            )
                        )
                        return
                managed_run = self.task_manager.create(
                    "agent.run",
                    self._run_agent(content, attachments=attachments),
                )
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
                        await self._send_event(
                            AgentEvent.error(
                                f"Chat run failed: {e}",
                                recoverable=True,
                                error_type="api",
                            )
                        )
                    finally:
                        if self._active_run_task is managed_run.task:
                            self._active_run_task = None
                        if self._active_task_id == managed_run.id:
                            self._active_task_id = None
                        self._schedule_task_runtime_update()

                asyncio.create_task(_wait_and_cleanup())
            return

        if command.type == "approval":
            tool_call_id = command.data.get("tool_call_id", "")
            if not self._resolve_pending_approval(tool_call_id, dict(command.data)):
                await self._send_event(
                    AgentEvent.error(
                        f"Approval request '{tool_call_id}' is no longer pending",
                        recoverable=True,
                        error_type="tool",
                    )
                )
            return

        if command.type == "answer":
            tool_call_id = command.data.get("tool_call_id", "")
            if not self._resolve_pending_approval(tool_call_id, dict(command.data)):
                await self._send_event(
                    AgentEvent.error(
                        f"Question request '{tool_call_id}' is no longer pending",
                        recoverable=True,
                        error_type="tool",
                    )
                )
            return

        if command.type == "control_response":
            request_id, payload = self._normalize_control_response(command.data)
            if payload:
                self._resolve_pending_approval(request_id, payload)
            return

        if command.type == "control_cancel_request":
            request_id = str(command.data.get("request_id") or "").strip()
            self._resolve_pending_approval(
                request_id,
                {
                    "action": "reject",
                    "guidance": "control request cancelled by client",
                },
            )
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
                await self.ws.send_json(payload)
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
            },
            log_context="conversation.list",
        )

    async def _send_event(self, event: AgentEvent) -> None:
        target_conversation_id = (
            str(event.data.get("conversation_id") or "").strip()
            or self._streaming_conversation_id
            or self.active_conversation_id
        )
        if event.type == "text_chunk":
            self._streaming_accumulated_text += str(event.data.get("content", ""))

        payload = self._build_ws_payload(event)
        if event.type != "mcp_status" and target_conversation_id:
            payload.setdefault("conversation_id", target_conversation_id)

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
