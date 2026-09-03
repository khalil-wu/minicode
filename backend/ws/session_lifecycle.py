from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocketDisconnect

from backend.agent.message import AgentEvent
from backend.atomic_io import canonical_file_path_key
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    await_with_deadline,
    cancel_and_drain_receipt,
    cancel_and_retire,
)
from backend.permissions.profiles import sandbox_capability_for_context
from backend.terminal.manager import BackgroundCommand
from backend.workspace.file_watcher import WorkspaceFileWatcher

logger = logging.getLogger(__name__)


class SessionLifecycle:
    """Own workspace, connection, and shutdown lifecycle for one WS session."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._workspace_context: Any | None = None
        self._workspace_context_task: asyncio.Task[Any] | None = None
        self._workspace_mcp_task: asyncio.Task[Any] | None = None
        self._retired_workspace_tasks: set[asyncio.Task[Any]] = set()
        self._workspace_generation = 0
        self._workspace_root: Path | None = None
        self.file_watcher: WorkspaceFileWatcher | None = None
        self._background_recovery_lock = asyncio.Lock()
        self._background_recovery_loaded = False
        self._pending_background_recovery: list[Any] = []
        self._sandbox_capability_payload: dict[str, Any] | None = None
        self._sandbox_capability_task: asyncio.Task[Any] | None = None
        self._sandbox_capability_generation = 0
        self._task_runtime_update_task: asyncio.Task[Any] | None = None

    @property
    def workspace_context(self) -> Any | None:
        return self._workspace_context

    @workspace_context.setter
    def workspace_context(self, value: Any | None) -> None:
        self._workspace_context = value

    @property
    def workspace_context_task(self) -> asyncio.Task[Any] | None:
        return self._workspace_context_task

    @workspace_context_task.setter
    def workspace_context_task(self, value: asyncio.Task[Any] | None) -> None:
        self._workspace_context_task = value

    @property
    def workspace_mcp_task(self) -> asyncio.Task[Any] | None:
        return self._workspace_mcp_task

    @workspace_mcp_task.setter
    def workspace_mcp_task(self, value: asyncio.Task[Any] | None) -> None:
        self._workspace_mcp_task = value

    @property
    def retired_workspace_tasks(self) -> set[asyncio.Task[Any]]:
        return self._retired_workspace_tasks

    @property
    def workspace_generation(self) -> int:
        return self._workspace_generation

    @workspace_generation.setter
    def workspace_generation(self, value: int) -> None:
        self._workspace_generation = value

    @property
    def workspace_root(self) -> Path | None:
        return self._workspace_root

    @workspace_root.setter
    def workspace_root(self, value: Path | None) -> None:
        self._workspace_root = value

    @property
    def background_recovery_lock(self) -> asyncio.Lock:
        return self._background_recovery_lock

    @property
    def background_recovery_loaded(self) -> bool:
        return self._background_recovery_loaded

    @background_recovery_loaded.setter
    def background_recovery_loaded(self, value: bool) -> None:
        self._background_recovery_loaded = value

    @property
    def pending_background_recovery(self) -> list[Any]:
        return self._pending_background_recovery

    @pending_background_recovery.setter
    def pending_background_recovery(self, value: list[Any]) -> None:
        self._pending_background_recovery = value

    @property
    def sandbox_capability_payload(self) -> dict[str, Any] | None:
        return self._sandbox_capability_payload

    @sandbox_capability_payload.setter
    def sandbox_capability_payload(self, value: dict[str, Any] | None) -> None:
        self._sandbox_capability_payload = value

    @property
    def sandbox_capability_task(self) -> asyncio.Task[Any] | None:
        return self._sandbox_capability_task

    @sandbox_capability_task.setter
    def sandbox_capability_task(self, value: asyncio.Task[Any] | None) -> None:
        self._sandbox_capability_task = value

    @property
    def sandbox_capability_generation(self) -> int:
        return self._sandbox_capability_generation

    @sandbox_capability_generation.setter
    def sandbox_capability_generation(self, value: int) -> None:
        self._sandbox_capability_generation = value

    @property
    def file_watcher(self) -> WorkspaceFileWatcher | None:
        return self._file_watcher

    @file_watcher.setter
    def file_watcher(self, value: WorkspaceFileWatcher | None) -> None:
        self._file_watcher = value

    @property
    def task_runtime_update_task(self) -> asyncio.Task[Any] | None:
        return self._task_runtime_update_task

    @task_runtime_update_task.setter
    def task_runtime_update_task(self, value: asyncio.Task[Any] | None) -> None:
        self._task_runtime_update_task = value

    async def initialize_workspace_context(self):
        try:
            from backend.workspace.context import WorkspaceContext

            workspace_root = self.workspace_root_for_conversation()
            if workspace_root is None:
                logger.info("No workspace bound for session %s; workspace context not initialized", self._session.session_id)
                return
            ctx = WorkspaceContext(workspace_root)
            await ctx.initialize()
            self._workspace_context = ctx
            logger.info("Workspace context initialized for session %s", self._session.session_id)
        except Exception as exc:
            logger.warning("Failed to initialize workspace context: %s", exc)

    def ensure_workspace_context_task(self) -> None:
        if self._workspace_context is not None:
            return
        task = self._workspace_context_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._workspace_context_task = loop.create_task(self.initialize_workspace_context())

    async def on_terminal_output(self, session_id: str, data: str, conversation_id: str = "") -> None:
        try:
            await self._session.send_payload(
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

    async def on_terminal_exit(self, session_id: str, exit_code: int, conversation_id: str = "") -> None:
        try:
            await self._session.send_payload(
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

    async def on_background_command_completed(self, bg_cmd: BackgroundCommand) -> None:
        try:
            output_preview = bg_cmd.output[:2000] if bg_cmd.output else ""
            lifecycle = bg_cmd.to_dict()
            await self._session.send_payload(
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

    async def on_background_command_stalled(self, bg_cmd: BackgroundCommand, tail: str) -> None:
        """One-shot notice when a background command blocks on an interactive prompt."""
        try:
            from backend.agent.message import AgentEvent

            await self._session.send_event(
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

    async def on_background_command_started(self, bg_cmd: BackgroundCommand) -> None:
        try:
            lifecycle = bg_cmd.to_dict()
            await self._session.send_payload(
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

    async def recover_orphaned_background_commands(self) -> None:
        """Reconcile and project durable commands left by a dead process."""

        async with self._background_recovery_lock:
            if not self._background_recovery_loaded:
                try:
                    recovered = await asyncio.to_thread(
                        self._session.background_manager.cleanup_orphaned_tasks_on_startup
                    )
                except Exception as exc:
                    logger.exception(
                        "Background command recovery failed for session %s",
                        self._session.session_id,
                    )
                    event = AgentEvent.error(
                        "MiniCode could not reconcile background commands from the previous process.",
                        recoverable=True,
                        error_type="background_recovery",
                        error_code="background.recovery_failed",
                    )
                    event.data["detail"] = str(exc)
                    await self._session.send_event(event)
                    return
                self._pending_background_recovery = list(recovered)
                self._background_recovery_loaded = True

            pending: list[Any] = []
            for task in self._pending_background_recovery:
                conversation_id = str(task.conversation_id or "").strip()
                if not conversation_id:
                    event = AgentEvent.error(
                        "A recovered background command has no conversation owner and was not projected.",
                        recoverable=True,
                        error_type="background_recovery",
                        error_code="background.owner_missing",
                    )
                    event.data.update(
                        {
                            "task_id": str(task.task_id or ""),
                            "cleanup_pending": bool(task.cleanup_pending),
                        }
                    )
                    await self._session.send_event(event)
                    continue
                completed_at = float(task.cleanup_completed_at or time.time())
                payload = {
                    "type": "background.completed",
                    "command_id": str(task.task_id or ""),
                    "command": str(task.command or "")[:100],
                    "description": str(task.description or ""),
                    "status": "interrupted",
                    "output": "",
                    "duration": max(
                        0.0,
                        round(
                            completed_at
                            - float(task.started_at or 0.0),
                            1,
                        ),
                    ),
                    "started_at": float(task.started_at or 0.0),
                    "completed_at": completed_at,
                    "conversation_id": conversation_id,
                    "task_id": str(task.owner_task_id or ""),
                    "parent_run_id": str(task.parent_run_id or ""),
                    "cleanup_pending": bool(task.cleanup_pending),
                    "cleanup_reason": str(task.cleanup_reason or "background_owner_exited"),
                    "error": {
                        "code": "background_owner_exited",
                        "message": (
                            "The previous MiniCode process exited before this "
                            "background command completed."
                        ),
                    },
                }
                sent = await self._session.send_payload(
                    payload,
                    log_context="background.completed.recovered",
                )
                if not sent and self._session.is_connected:
                    pending.append(task)
            self._pending_background_recovery = pending

    def start_file_watcher(self):
        workspace_root = self.workspace_root_for_conversation()
        if workspace_root is None:
            logger.info("No workspace bound for session %s; file watcher not started", self._session.session_id)
            return
        captured_workspace_root = workspace_root.resolve()

        async def on_change(path: Path, event_type: str) -> None:
            current_workspace_root = self.workspace_root_for_conversation()
            if current_workspace_root is None or current_workspace_root != captured_workspace_root:
                return
            await self.on_file_changed(
                path,
                event_type,
                workspace_root=current_workspace_root,
                conversation_id=str(self._session.active_conversation_id or "").strip(),
            )

        try:
            self.file_watcher = WorkspaceFileWatcher(
                workspace_root=workspace_root,
                on_change=on_change,
                stability_threshold=0.5,
            )
            self.file_watcher.start()
            logger.info("File watcher started for session %s", self._session.session_id)
        except Exception as exc:
            logger.error("Failed to start file watcher: %s", exc, exc_info=True)

    def restart_file_watcher(self, workspace_root: Path) -> None:
        if self.file_watcher is not None:
            self.file_watcher.stop()
            self.file_watcher = None

        resolved_workspace_root = workspace_root.resolve()

        async def on_change(path: Path, event_type: str) -> None:
            current_workspace_root = self.workspace_root_for_conversation()
            if current_workspace_root is None or current_workspace_root != resolved_workspace_root:
                return
            await self.on_file_changed(
                path,
                event_type,
                workspace_root=current_workspace_root,
                conversation_id=str(self._session.active_conversation_id or "").strip(),
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
                self._session.session_id,
                workspace_root,
            )
        except Exception as exc:
            logger.error("Failed to restart file watcher: %s", exc, exc_info=True)

    def clear_workspace_runtime(self) -> None:
        self._workspace_generation += 1
        self._retire_workspace_task(self._workspace_context_task)
        self._workspace_context_task = None
        self._retire_workspace_task(self._workspace_mcp_task)
        self._workspace_mcp_task = None
        self._workspace_context = None
        self._session.mcp_manager = None
        self._session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
        if self.file_watcher is not None:
            self.file_watcher.stop()
            self.file_watcher = None
        self._workspace_root = None

    def _retire_workspace_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        cancel_and_retire(task, owner=self._retired_workspace_tasks)

    async def create_isolated_conversation_worktree(self, conversation: Any) -> Any | None:
        from backend.services.conversation_payload_service import create_isolated_worktree_binding

        source_workspace_root = self.workspace_root_for_conversation(conversation)
        result = create_isolated_worktree_binding(
            conversation,
            current_workspace_root=source_workspace_root,
            main_worktree_root=self._session.main_worktree_root,
        )
        if result.error_event is not None:
            from backend.ws.command_results import emit_command_error
            await emit_command_error(self._session, "conversation.create", result.error_event)
            # The record was persisted with git_isolated=True before the
            # attempt.  Remove that provisional record instead of silently
            # downgrading the requested isolation to a shared workspace.
            # ``create_conversation`` allocates a fresh id, so this cannot
            # delete an older user conversation.
            try:
                self._session.conversation_repo.delete_conversation(conversation.id)
            except Exception:
                logger.error(
                    "Failed to remove provisional isolated conversation %s after worktree failure",
                    conversation.id,
                    exc_info=True,
                )
            return None

        if not result.created:
            return conversation

        updated = self._session.conversation_repo.update_workspace_binding(
            conversation.id,
            workspace_root=result.workspace_root,
            git_branch=result.git_branch,
            worktree_path=result.worktree_path,
            git_isolated=True,
        )
        if result.notice_event is not None:
            result.notice_event.data.setdefault(
                "conversation_id",
                str(result.conversation_id or conversation.id).strip(),
            )
            await self._session.send_event(result.notice_event)
        return updated or conversation

    async def switch_workspace_for_conversation(
        self,
        conversation: Any,
        *,
        announce: bool,
        wait_for_initialize: bool = False,
    ) -> bool:
        from backend.services.workspace_service import conversation_workspace_path, workspace_matches_context

        workspace_path = conversation_workspace_path(conversation)
        if not workspace_path:
            self.clear_workspace_runtime()
            return True

        if workspace_matches_context(workspace_path, self._workspace_context):
            return True

        return await self.activate_workspace_path(
            workspace_path,
            announce=announce,
            wait_for_initialize=wait_for_initialize,
            conversation_id=str(conversation.id or "").strip(),
        )

    async def activate_workspace_path(
        self,
        path_str: str,
        *,
        announce: bool = False,
        wait_for_initialize: bool = False,
        error_command: str | None = "workspace.activate",
        conversation_id: str | None = None,
    ) -> bool:
        from backend.services.workspace_service import (
            create_workspace_context,
            parse_workspace_activation_request,
            record_recent_workspace_project,
            workspace_context_root,
            workspace_imported_payload,
        )

        request = parse_workspace_activation_request(path_str)
        if request.error_event is not None:
            await self._session.send_event(request.error_event)
            return False
        project_path = request.project_path
        if project_path is None:
            return False

        owner_conversation_id = str(
            conversation_id or self._session.active_conversation_id or ""
        ).strip()
        # Explicit activation is user-visible conversation state. On a
        # first-run session create the ordinary active conversation only after
        # path validation, so the success event is never unowned.
        if announce and not owner_conversation_id:
            self._session._ensure_active_conversation()
            owner_conversation_id = str(self._session.active_conversation_id or "").strip()
        request_id = self._session.event_outbox.client_command_id

        old_workspace_context = self._workspace_context
        old_workspace_root = workspace_context_root(old_workspace_context)
        self._workspace_generation = int(self._workspace_generation) + 1
        activation_generation = self._workspace_generation
        ctx: Any | None = None
        restart_file_watcher = self.restart_file_watcher
        from backend.commands.slash_commands import refresh_slash_commands

        def refresh_commands() -> None:
            refresh_slash_commands(self._session.command_registry)

        def activation_is_current() -> bool:
            return (
                self._workspace_generation == activation_generation
                and self._workspace_context is ctx
            )

        async def reload_workspace_mcp(
            workspace_root: Path | None,
            *,
            enforce_generation: bool = True,
        ) -> None:
            from backend.api import _state

            bootstrap = _state.bootstrap
            if bootstrap is not None:
                manager = await bootstrap.activate_mcp_workspace(workspace_root)
            else:
                from backend.api.routes_health import get_mcp_manager

                manager = get_mcp_manager()
                if manager is not None:
                    await manager.reload_config()
            if manager is None:
                return
            if enforce_generation and ctx is not None and not activation_is_current():
                return
            self._session.mcp_manager = manager
            refresh_registry = self._session.refresh_tool_registry_if_mcp_changed
            refresh_registry(allow_when_busy=False)

        refresh_registry = self._session.refresh_tool_registry_if_mcp_changed

        def publish_mcp_manager(manager: Any | None) -> None:
            self._session.mcp_manager = manager
            refresh_registry(allow_when_busy=False)

        async def begin_workspace_mcp(workspace_root: Path) -> None:
            from backend.api import _state

            bootstrap = _state.bootstrap
            ready_task: asyncio.Task[Any] | None = None
            try:
                if bootstrap is None:
                    await reload_workspace_mcp(workspace_root)
                    return

                manager, ready_task = await bootstrap.begin_mcp_workspace_activation(workspace_root)
                if not activation_is_current():
                    ready_task.cancel()
                    await asyncio.gather(ready_task, return_exceptions=True)
                    return
                publish_mcp_manager(manager)

                ready_manager = await ready_task
                if not activation_is_current():
                    return
                publish_mcp_manager(ready_manager)
            except asyncio.CancelledError:
                if ready_task is not None and not ready_task.done():
                    ready_task.cancel()
                    await asyncio.gather(ready_task, return_exceptions=True)
                raise
            except Exception:
                logger.warning(
                    "Workspace MCP activation failed for %s",
                    workspace_root,
                    exc_info=True,
                )

        async def rollback_workspace() -> None:
            """Restore every session surface changed before indexing completes."""

            if ctx is None or not activation_is_current():
                return
            self._workspace_context = old_workspace_context
            restored_root = Path(old_workspace_root) if old_workspace_root else None
            if restored_root is not None:
                self._workspace_root = restored_root
                restart_file_watcher(restored_root)
            else:
                self._workspace_root = None
                watcher = self.file_watcher
                if watcher is not None:
                    watcher.stop()
                    self.file_watcher = None

            await reload_workspace_mcp(restored_root, enforce_generation=False)

            skill_manager = self._session.skill_manager
            if skill_manager is not None:
                skill_manager.set_project_root(restored_root)
            refresh_commands()
            try:
                await self._session._run_cwd_changed_hook(
                    old_cwd=str(project_path),
                    new_cwd=str(restored_root) if restored_root is not None else "",
                )
            except Exception:
                logger.debug("Workspace rollback cwd hook failed", exc_info=True)
            await self.send_runtime_capabilities(source="workspace.activate.rollback")

        try:
            ctx = create_workspace_context(project_path)
            # Superseded initialization remains session-owned until it settles,
            # but must never delay this user-visible switch or publish into the
            # new generation.
            self._retire_workspace_task(self._workspace_context_task)
            self._workspace_context_task = None
            self._retire_workspace_task(self._workspace_mcp_task)
            self._workspace_mcp_task = None
            self._workspace_context = ctx
            self._workspace_root = project_path
            # Capability probes are scoped to the active workspace.  Invalidate
            # the previous projection, but let activation publish a pending
            # snapshot while the new probe runs in the background.
            self._sandbox_capability_payload = None
            if wait_for_initialize:
                publish_mcp_manager(None)
                await reload_workspace_mcp(project_path)
            else:
                # Never expose the previous workspace's MCP tools while the
                # new manager is being prepared. The preparation itself may
                # wait on config locks or external servers, so it is fully
                # background-owned in the non-blocking activation path.
                publish_mcp_manager(None)
                mcp_task = asyncio.create_task(begin_workspace_mcp(project_path))
                self._workspace_mcp_task = mcp_task

                def clear_mcp_task(completed: asyncio.Task[Any]) -> None:
                    if self._workspace_mcp_task is completed:
                        self._workspace_mcp_task = None

                mcp_task.add_done_callback(clear_mcp_task)

            async def prepare_workspace_projection() -> None:
                """Finish non-index workspace projections for this generation."""

                refresh_commands()
                if not activation_is_current():
                    return
                skill_manager = self._session.skill_manager
                if skill_manager is not None:
                    skill_manager.set_project_root(project_path)
                if not activation_is_current():
                    return
                await self._session._run_cwd_changed_hook(
                    old_cwd=old_workspace_root,
                    new_cwd=str(project_path),
                )
                if not activation_is_current():
                    return
                restart_file_watcher(project_path)
            await self.send_runtime_capabilities(source="workspace.activate")

            async def prepare_workspace_projection_background() -> None:
                try:
                    await prepare_workspace_projection()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Workspace projection preparation failed for %s",
                        project_path,
                        exc_info=True,
                    )

            async def initialize_workspace() -> bool:
                projection_task: asyncio.Task[Any] | None = None
                try:
                    if not wait_for_initialize:
                        # Skill discovery, hooks, capability projection and
                        # workspace indexing all belong to the same retired
                        # generation, but none may delay conversation deletion
                        # or a normal conversation switch.
                        # Start the projection and the potentially slow index
                        # concurrently so a heavy skill catalog cannot delay
                        # the workspace context becoming usable.
                        projection_task = asyncio.create_task(
                            prepare_workspace_projection_background()
                        )
                    metadata = await ctx.initialize()
                    if projection_task is not None:
                        await projection_task
                    if not activation_is_current():
                        return False
                    record_recent_workspace_project(project_path, metadata)
                    if announce:
                        await self._session.send_payload(
                            workspace_imported_payload(
                                ctx,
                                metadata,
                                conversation_id=owner_conversation_id,
                                workspace_root=project_path,
                                request_id=request_id,
                            ),
                            log_context="workspace.imported",
                        )
                    return True
                except asyncio.CancelledError:
                    if projection_task is not None and not projection_task.done():
                        projection_task.cancel()
                        await asyncio.gather(projection_task, return_exceptions=True)
                    raise
                except Exception as exc:
                    if not activation_is_current():
                        if projection_task is not None and not projection_task.done():
                            projection_task.cancel()
                            await asyncio.gather(projection_task, return_exceptions=True)
                        return False
                    # Only roll back if this activation is still current. A
                    # newer switch may have replaced it while initialization
                    # was running and must not be overwritten by this failure.
                    # Background conversation activation has already committed
                    # the new owner/workspace projection. Rolling it back to a
                    # deleted or previously selected conversation would create
                    # a cross-conversation workspace mismatch, so rollback is
                    # reserved for the explicit, blocking activation contract.
                    if wait_for_initialize:
                        if projection_task is not None and not projection_task.done():
                            projection_task.cancel()
                            await asyncio.gather(projection_task, return_exceptions=True)
                        await rollback_workspace()
                    elif projection_task is not None:
                        # The non-blocking activation already committed the
                        # new workspace owner. Index failure must not suppress
                        # its independent skill/hook/watcher/capability
                        # projection or leave the session half-switched.
                        await projection_task
                    message = f"Failed to switch session workspace: {exc}"
                    if error_command:
                        from backend.ws.command_results import emit_command_error
                        await emit_command_error(self._session, error_command, message)
                    else:
                        await self._session.send_event(AgentEvent.error(message, recoverable=True))
                    return False

            if wait_for_initialize:
                await prepare_workspace_projection()
                return await initialize_workspace()
            else:
                context_task = asyncio.create_task(initialize_workspace())
                self._workspace_context_task = context_task

                def clear_context_task(completed: asyncio.Task[Any]) -> None:
                    if self._workspace_context_task is completed:
                        self._workspace_context_task = None

                context_task.add_done_callback(clear_context_task)
            return True
        except Exception as exc:
            await rollback_workspace()
            message = f"Failed to switch session workspace: {exc}"
            if error_command:
                from backend.ws.command_results import emit_command_error
                await emit_command_error(self._session, error_command, message)
            else:
                await self._session.send_event(AgentEvent.error(message, recoverable=True))
            return False

    async def send_runtime_capabilities(self, *, source: str = "session") -> None:
        try:
            workspace = self.workspace_root_for_conversation()
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
                            self._session.permission_context,
                        )
                    )
                    self._sandbox_capability_task = probe_task

                    def publish_probe_result(task: asyncio.Task[Any]) -> None:
                        if task.cancelled():
                            return
                        if (
                            probe_generation != self._sandbox_capability_generation
                            or probe_workspace
                            != canonical_file_path_key(
                                self.workspace_root_for_conversation()
                            )
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
                        if self._session.is_connected:
                            asyncio.create_task(
                                self.send_runtime_capabilities(source="sandbox.probe")
                            )

                    probe_task.add_done_callback(publish_probe_result)

                # Workspace activation is latency-sensitive. Expose a pending
                # snapshot immediately and publish the probe result later.
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
        await self._session.send_payload(
            self._session.runtime_capabilities_payload(source=source),
            log_context="runtime.capabilities",
        )

    def schedule_task_runtime_update(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        existing = self._task_runtime_update_task
        if existing is not None and not existing.done():
            return
        coroutine = self.send_task_runtime_update()
        try:
            task = loop.create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            return
        self._task_runtime_update_task = task

        def _clear_runtime_update(finished: asyncio.Task[None]) -> None:
            if self._task_runtime_update_task is finished:
                self._task_runtime_update_task = None

        task.add_done_callback(_clear_runtime_update)

    async def send_task_runtime_update(self) -> None:
        try:
            await self._session.send_event(
                AgentEvent(
                    type="task.update",
                    data={"session": self._session.runtime_snapshot()},
                )
            )
        except Exception:
            logger.debug(
                "session %s task runtime update failed",
                self._session.session_id,
                exc_info=True,
            )

    def current_workspace_root(self) -> Path | None:
        """Return the workspace currently mounted by this session, if any."""
        # Prefer workspace_context if available (for active conversation)
        workspace_context = self._workspace_context
        if workspace_context is not None:
            resolved = workspace_context.root_path.resolve()
            self._workspace_root = resolved
            return resolved

        if self._workspace_root is not None:
            return self._workspace_root

        return None

    def workspace_path_for_conversation(self, conversation: Any | None = None) -> str:
        target = conversation if conversation is not None else self._session.active_conversation
        if target is None:
            return ""
        return str(target.worktree_path or target.workspace_root or "").strip()

    def workspace_root_for_conversation(self, conversation: Any | None = None) -> Path | None:
        workspace_path = self.workspace_path_for_conversation(conversation)
        if not workspace_path:
            return None
        return Path(workspace_path).expanduser().resolve()

    def workspace_context_for_conversation(self, conversation: Any | None = None) -> Any | None:
        workspace_root = self.workspace_root_for_conversation(conversation)
        if workspace_root is None:
            return None
        workspace_context = self._workspace_context
        if workspace_context is not None and workspace_context.root_path.resolve() == workspace_root:
            return workspace_context
        return None

    async def on_file_changed(
        self,
        path: Path,
        event_type: str,
        *,
        workspace_root: Path | None = None,
        conversation_id: str = "",
    ):
        try:
            owner_workspace_root = workspace_root or self.current_workspace_root()
            if owner_workspace_root is None:
                raise RuntimeError("A file watcher event has no workspace owner")
            owner_workspace_root = owner_workspace_root.resolve()
            owner_conversation_id = str(conversation_id or self._session.active_conversation_id or "").strip()
            if not owner_conversation_id:
                return
            try:
                relative_path = str(path.relative_to(owner_workspace_root))
            except ValueError:
                relative_path = path.name
            await self._session.send_payload(
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
                session_id=self._session.session_id,
                conversation_id=owner_conversation_id,
                workspace_root=owner_workspace_root,
            )
            if active_previews:
                await self._session.send_payload(
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
                await self._session.send_payload(
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

    async def handle(self, *, connection_generation: int | None = None) -> None:
        active_generation = connection_generation or self._session.connection_generation
        if active_generation != self._session.connection_generation:
            return

        try:
            self.ensure_workspace_context_task()
            await self.recover_orphaned_background_commands()
            from backend.api.routes_health import get_mcp_status

            mcp_status = get_mcp_status() or []
            await self._session.send_event(AgentEvent(type="mcp_status", data={"servers": mcp_status}))
            await self._session.send_llm_state(force=True)
            await self._session.command_dispatcher._replay_pending_client_commands(
                active_generation
            )

            await self._session.command_dispatcher.run(active_generation)

        except WebSocketDisconnect:
            logger.info("session %s disconnected", self._session.session_id)
        except Exception as exc:
            logger.error("session %s failed: %s", self._session.session_id, exc, exc_info=True)
        finally:
            if active_generation == self._session.connection_generation:
                await self._session.artifact_store.flush()
                self._session.artifact_store.clear()

    async def shutdown(self, *, reason: str = "session_shutdown") -> None:
        """Cancel and drain every resource owned by this websocket session."""
        self._session.mark_disconnected()
        self._session.run_manager.stop_notification_wake_intake()
        self._session.run_manager.clear_all_user_message_queues()
        self._workspace_generation += 1
        self._sandbox_capability_generation += 1
        try:
            await self._session.cancel_agent_runs(reason=reason)
        except Exception:
            logger.debug("Failed to cancel agent runs during session shutdown", exc_info=True)
        # Run cancellation emits owned terminal notifications first.  The
        # state container is then the final cleanup authority for any waiter
        # whose owning task disappeared before it could run its own finally.
        self._session.turn_wait_state.clear_pending_waiters()
        self._session.approval_diff_cache.clear()
        await self._session.run_manager.shutdown_notification_wakes()
        # Notification hooks are observational and session-owned. Drain them
        # before extension/session teardown so no hook runs against a retired
        # lifecycle generation.
        current = asyncio.current_task()
        notification_tasks = [
            task
            for task in list(self._session._notification_hook_tasks)
            if task is not current and not task.done()
        ]
        if notification_tasks:
            await cancel_and_drain_receipt(
                notification_tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket notification hooks",
                owner=self._session._notification_hook_tasks,
            )
        retained_notification_tasks = {
            task for task in self._session._notification_hook_tasks
            if task is not current and not task.done()
        }
        self._session._notification_hook_tasks.intersection_update(retained_notification_tasks)
        background_tasks: set[asyncio.Task[Any]] = set(self._retired_workspace_tasks)
        for task in (
            self._workspace_context_task,
            self._workspace_mcp_task,
            self._sandbox_capability_task,
            self._session._extension_requested_shutdown_task,
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
        self._session._extension_requested_shutdown_task = None
        try:
            await self._session.conversation_runtime.shutdown()
        except Exception:
            logger.debug("Failed to drain conversation hydration", exc_info=True)
        # MiniCode extension generations are session-owned. Shut them down only
        # after active turns have been cancelled so in-flight tool adapters are
        # never invalidated underneath a running turn.
        try:
            await await_with_deadline(
                self._session._shutdown_lifecycle_runtimes(reason),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="MiniCode extension shutdown",
                owner=self._session.cleanup_tasks,
            )
        except Exception:
            logger.debug("Failed to shut down MiniCode extensions during session shutdown", exc_info=True)
        try:
            from backend.hooks.runtime import run_session_end_hook

            await await_with_deadline(
                run_session_end_hook(session_id=self._session.session_id, reason=reason),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="session end hook shutdown",
                owner=self._session.cleanup_tasks,
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
                await runtime.stop_subagent_tasks_for_session(self._session.session_id, reason=reason)
        except Exception:
            logger.exception("Failed to stop subagents during session %s shutdown", self._session.session_id)

        current = asyncio.current_task()
        command_dispatcher = self._session.command_dispatcher
        command_tasks = [
            task
            for task in list(command_dispatcher.command_tasks)
            if task is not current and not task.done()
        ]
        if command_tasks:
            await cancel_and_drain_receipt(
                command_tasks,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket command tasks",
                owner=command_dispatcher.command_tasks,
            )
        command_dispatcher.prune_command_tasks()

        try:
            await await_with_deadline(
                self._session.task_manager.cancel_all_and_wait(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="managed task shutdown",
                owner=self._session.cleanup_tasks,
            )
        except Exception:
            logger.debug("Failed to drain managed tasks during session shutdown", exc_info=True)
        runtime_update_task = self._task_runtime_update_task
        if runtime_update_task is not None and runtime_update_task is not current:
            await cancel_and_drain_receipt(
                [runtime_update_task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="runtime update task",
                owner=self._session.cleanup_tasks,
            )
        self._task_runtime_update_task = None
        if self.file_watcher and self.file_watcher.is_running():
            self.file_watcher.stop()
        try:
            await await_with_deadline(
                self._session.terminal_manager.destroy_all(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="terminal shutdown",
                owner=self._session.cleanup_tasks,
            )
        except Exception as exc:
            # destroy_all/destroy_sessions_for_conversation raise naming the
            # shells whose exit was never proven. Terminals keep no durable
            # record, so a debug-level log was the only trace of a leaked
            # process tree.
            logger.error(
                "Terminals could not be proven stopped during session %s shutdown: %s",
                self._session.session_id,
                exc,
                exc_info=True,
            )
        try:
            from backend.preview import stop_preview_launches_for_session

            await await_with_deadline(
                stop_preview_launches_for_session(self._session.session_id),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="preview shutdown",
                owner=self._session.cleanup_tasks,
            )
        except Exception as exc:
            # _stop_preview_processes raises with the surviving preview ids; a
            # dev server that outlives the session keeps writing to the
            # workspace.
            logger.error(
                "Previews could not be proven stopped during session %s shutdown: %s",
                self._session.session_id,
                exc,
                exc_info=True,
            )
        try:
            await await_with_deadline(
                self._session.background_manager.shutdown(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="background command shutdown",
                owner=self._session.cleanup_tasks,
            )
        except Exception as exc:
            logger.error(
                "Background commands could not be proven stopped during session %s shutdown: %s",
                self._session.session_id,
                exc,
                exc_info=True,
            )
        try:
            from backend.ws.agent_runner import (
                _clear_session_llm_cache,
                _schedule_session_llm_close,
            )

            _clear_session_llm_cache(self._session)
            for adapter in list(self._session._retired_llm_adapters.values()):
                _schedule_session_llm_close(self._session, adapter)
            self._session._retired_llm_adapters.clear()
            self._session._llm_adapter_leases.clear()
            if self._session._llm_close_tasks:
                await await_with_deadline(
                    asyncio.gather(
                        *tuple(self._session._llm_close_tasks),
                        return_exceptions=True,
                    ),
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label="LLM adapter close tasks",
                    owner=self._session.cleanup_tasks,
                )
                self._session._llm_close_tasks = {
                    task for task in self._session._llm_close_tasks if not task.done()
                }
        except Exception:
            logger.debug("Failed to close session LLM adapters", exc_info=True)
        try:
            await self._session.event_outbox.drain_persistence()
            await self._session.artifact_store.flush()
            self._session.artifact_store.shutdown()
            self._session.artifact_store.clear()
        finally:
            self._session.run_manager.close_durable_queue()
