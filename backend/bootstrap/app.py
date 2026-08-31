from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from inspect import Parameter, signature
from pathlib import Path
from typing import Any

from backend.config import DATA_ROOT, AppConfig, load_config
from backend.memory.file_memory import FileMemory
from backend.memory.manager import MemoryManager
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_STARTUP_STEP_TIMEOUT_SECONDS = 8.0
_STARTUP_TIMED_OUT = object()
_WORKSPACE_ROOT_UNSET = object()


async def _with_startup_timeout(label: str, awaitable: Awaitable[Any], timeout: float = _STARTUP_STEP_TIMEOUT_SECONDS) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError:
        logger.warning("%s init timed out after %.1fs; continuing without it", label, timeout)
        return _STARTUP_TIMED_OUT


async def _to_thread_with_timeout(label: str, func: Callable[[], Any], timeout: float = _STARTUP_STEP_TIMEOUT_SECONDS) -> Any:
    return await _with_startup_timeout(label, asyncio.to_thread(func), timeout=timeout)


class AppBootstrap:
    """Central composition root for shared backend services."""

    def __init__(
        self,
        *,
        build_tool_registry: Callable[..., ToolRegistry],
        build_status_payload: Callable[[], dict[str, Any]] | None = None,
        create_session_llm: Callable[..., Any],
        ws_manager: Any,
        on_mcp_status_change: Callable[[str, Any], Awaitable[None]],
        status_cache_ttl_seconds: float = 5.0,
    ) -> None:
        self._build_tool_registry = build_tool_registry
        self._build_status_payload = build_status_payload
        self._create_session_llm = create_session_llm
        self.ws_manager = ws_manager
        self._on_mcp_status_change = on_mcp_status_change
        self._status_cache_ttl_seconds = status_cache_ttl_seconds

        self.config: AppConfig | None = None
        self.mcp_manager: Any | None = None
        self._mcp_managers: dict[str, Any] = {}
        self._mcp_start_tasks: dict[str, asyncio.Task[Any]] = {}
        self._mcp_manager_lock = asyncio.Lock()
        self.skill_manager: Any | None = None
        self.skill_executor: Any | None = None
        self.file_memory: Any | None = None
        self.memory_manager: Any | None = None
        self._status_cache_payload: dict[str, Any] | None = None
        self._status_cache_expires_at = 0.0
        self.task_scheduler: Any | None = None
        self._pr_monitor_task: asyncio.Task[None] | None = None
        self._sandbox_probe_task: asyncio.Task[bool] | None = None

    async def startup(self) -> None:
        from backend.permissions.profiles import refresh_native_os_sandbox

        # Capability discovery may invoke a container CLI. Warm its cache in
        # parallel with normal startup so session snapshots remain nonblocking.
        self._sandbox_probe_task = asyncio.create_task(
            asyncio.to_thread(refresh_native_os_sandbox)
        )
        self.config = load_config()

        try:
            from backend.ws.client_command_log import cleanup_stale_client_command_logs
            from backend.ws.event_log import cleanup_stale_replay_logs

            cleanup_result = await _to_thread_with_timeout(
                "Websocket session log cleanup",
                lambda: (
                    cleanup_stale_replay_logs(DATA_ROOT / "ws-event-log"),
                    cleanup_stale_client_command_logs(DATA_ROOT / "client-command-log"),
                ),
            )
            if cleanup_result is not _STARTUP_TIMED_OUT:
                replay_logs, command_logs = cleanup_result
                if replay_logs or command_logs:
                    logger.info(
                        "Cleaned stale websocket session logs: replay=%d command=%d",
                        replay_logs,
                        command_logs,
                    )
        except Exception as exc:
            logger.warning("Websocket session log cleanup failed: %s", exc)

        try:
            self.file_memory = FileMemory()
            self.memory_manager = MemoryManager(self.file_memory)
            logger.info("File memory is ready")
        except Exception as exc:
            logger.warning("File memory init failed: %s", exc)

        try:
            from backend.skills.executor import SkillExecutor
            from backend.skills.loader import SkillLoader
            from backend.skills.manager import SkillManager

            loader = SkillLoader()
            self.skill_manager = SkillManager(
                loader=loader,
            )
            self.skill_manager.discover()
            self.skill_executor = SkillExecutor(self.skill_manager)
            logger.info("Skills are ready")
        except Exception as exc:
            logger.warning("Skills init failed: %s", exc)

        try:
            from backend.workspace.state import get_explicit_active_workspace_root

            await self.ensure_mcp_manager(
                get_explicit_active_workspace_root(),
                activate=True,
            )
            logger.info("MCP manager initialized")
        except Exception as exc:
            logger.warning("MCP init failed: %s", exc)

        try:
            from backend.hooks.manager import HookManager, set_hook_manager
            from backend.config import SETTINGS_FILE
            from pathlib import Path
            import json

            settings_data: dict[str, Any] = {}
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    settings_data = json.load(f)
            project_root = Path(SETTINGS_FILE).resolve().parent if SETTINGS_FILE.exists() else None
            hook_mgr = HookManager.from_settings(settings_data, workspace_root=project_root)
            set_hook_manager(hook_mgr)
            hook_count = len(hook_mgr.pre_tool) + len(hook_mgr.post_tool)
            if hook_count:
                logger.info("Hooks loaded: %d pre_tool, %d post_tool", len(hook_mgr.pre_tool), len(hook_mgr.post_tool))
        except Exception as exc:
            logger.warning("Hooks init failed: %s", exc)

        try:
            from backend.tasks.scheduler import get_global_scheduler
            self.task_scheduler = get_global_scheduler(on_fire=self._run_scheduled_task)
            self.task_scheduler.set_on_change(self._broadcast_scheduled_task_state)
            await self.task_scheduler.start()
            logger.info("Task scheduler is ready")
        except Exception as exc:
            logger.warning("Task scheduler init failed: %s", exc)

        self._pr_monitor_task = asyncio.create_task(self._run_pr_automation_monitor())

    async def _run_scheduled_task(self, task: Any, run: Any) -> dict[str, Any]:
        from backend.services.scheduled_task_runner import run_scheduled_task

        return await run_scheduled_task(task, run, bootstrap=self)

    async def _broadcast_scheduled_task_state(self) -> None:
        if self.task_scheduler is None:
            return
        sessions = list(getattr(self.ws_manager, "_sessions", {}).values())
        for session in sessions:
            if not session.is_connected:
                continue
            try:
                workspace_root = str(session.session_lifecycle.current_workspace_root() or "")
                payload = {
                    "type": "scheduler.list",
                    "tasks": self.task_scheduler.list_tasks(workspace_root=workspace_root),
                    "runs": self.task_scheduler.list_runs(workspace_root=workspace_root),
                    "conversation_id": str(getattr(session, "active_conversation_id", "") or ""),
                    "workspace_root": workspace_root,
                }
                await session.send_payload(payload, log_context="scheduler.list")
            except Exception:
                logger.debug("Failed to broadcast scheduler update", exc_info=True)

    async def _run_pr_automation_monitor(self) -> None:
        """Poll enabled workspaces so Auto-fix does not depend on an open panel."""
        try:
            while True:
                await asyncio.sleep(60)
                await self._poll_pr_automation_once()
        except asyncio.CancelledError:
            return

    async def _poll_pr_automation_once(self) -> None:
        """Poll each connected workspace once, even when several windows use it."""

        from pathlib import Path

        from backend.services.workspace_service import read_pr_automation
        from backend.ws.handlers.workspace import handle_git_pr_status

        sessions_by_workspace: dict[str, Any] = {}
        for session in list(getattr(self.ws_manager, "_sessions", {}).values()):
            if not session.is_connected:
                continue
            try:
                raw_root = session.session_lifecycle.current_workspace_root()
                if raw_root is None:
                    continue
                workspace_key = os.path.normcase(
                    str(Path(raw_root).expanduser().resolve(strict=False))
                )
                sessions_by_workspace.setdefault(workspace_key, session)
            except (OSError, ValueError):
                logger.debug("PR automation ignored an invalid workspace", exc_info=True)

        for session in sessions_by_workspace.values():
            try:
                workspace_root = session.session_lifecycle.current_workspace_root()
                if workspace_root is None:
                    continue
                if not read_pr_automation(workspace_root).get("auto_fix"):
                    continue
                await handle_git_pr_status(session, {})
            except Exception:
                logger.debug("PR automation poll failed", exc_info=True)

    async def shutdown(self) -> None:
        try:
            if self._sandbox_probe_task is not None:
                if not self._sandbox_probe_task.done():
                    self._sandbox_probe_task.cancel()
                try:
                    await self._sandbox_probe_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.debug("Sandbox capability probe error (harmless): %s", exc)
                self._sandbox_probe_task = None
            if self._pr_monitor_task is not None:
                self._pr_monitor_task.cancel()
                try:
                    await self._pr_monitor_task
                except asyncio.CancelledError:
                    pass
                self._pr_monitor_task = None
            try:
                from backend.memory.generation import drain_memory_background_tasks

                pending_memory = await drain_memory_background_tasks(timeout=5.0)
                if pending_memory:
                    logger.warning(
                        "Memory shutdown drain left %d worker(s) pending",
                        len(pending_memory),
                    )
            except Exception as exc:
                logger.debug("Memory worker shutdown error (continuing): %s", exc)
            if self.task_scheduler:
                try:
                    await self.task_scheduler.stop()
                except Exception as exc:
                    logger.debug("Task scheduler stop error (harmless): %s", exc)
            pending_starts = list(self._mcp_start_tasks.values())
            for task in pending_starts:
                if not task.done():
                    task.cancel()
            if pending_starts:
                await asyncio.gather(*pending_starts, return_exceptions=True)
            self._mcp_start_tasks.clear()
            managers = list(dict.fromkeys(self._mcp_managers.values()))
            if managers:
                await asyncio.gather(
                    *(manager.stop_all() for manager in managers),
                    return_exceptions=True,
                )
                self._mcp_managers.clear()
                self.mcp_manager = None
                logger.info("MCP manager stopped")
            try:
                from backend.lsp.client import get_lsp_manager

                await get_lsp_manager().shutdown_all()
                logger.info("LSP manager stopped")
            except Exception as exc:
                logger.debug("LSP manager stop error (harmless): %s", exc)
            try:
                from backend.preview.launcher import stop_all_preview_launches

                await stop_all_preview_launches()
                logger.info("Preview processes stopped")
            except Exception as exc:
                logger.debug("Preview shutdown error (harmless): %s", exc)
        finally:
            # The hook manager is process-global. Leaving a test/application
            # instance installed leaks hooks into the next bootstrap and can
            # execute stale or incomplete manager implementations.
            from backend.hooks.manager import set_hook_manager

            set_hook_manager(None)

    def refresh_config(self) -> AppConfig:
        self.config = load_config()
        return self.config

    def create_tool_registry(
        self,
        artifact_store: Any,
        *,
        workspace_root: str | Path | None | object = _WORKSPACE_ROOT_UNSET,
        config: AppConfig | None = None,
        mcp_manager: Any | None = None,
    ) -> ToolRegistry:
        from backend.workspace.state import get_active_workspace_root

        if workspace_root is _WORKSPACE_ROOT_UNSET:
            resolved_workspace_root = get_active_workspace_root()
        elif workspace_root is None:
            resolved_workspace_root = None
        else:
            resolved_workspace_root = Path(workspace_root).expanduser().resolve()
        effective_config = config or load_config(cwd=resolved_workspace_root)
        return self._build_tool_registry(
            artifact_store,
            workspace_root=resolved_workspace_root,
            config=effective_config,
            llm_provider=lambda: self.create_llm(config=effective_config),
            mcp_manager=(
                mcp_manager if mcp_manager is not None else self.mcp_manager
            ),
        )

    @staticmethod
    def _mcp_workspace_key(workspace_root: str | Path | None) -> str:
        from backend.owner_scope import canonical_workspace_root

        return canonical_workspace_root(workspace_root) or "<projectless>"

    async def _handle_scoped_mcp_status_change(
        self,
        manager: Any,
        server_name: str,
        status: Any,
    ) -> None:
        callback = self._on_mcp_status_change
        try:
            params = signature(callback).parameters
        except (TypeError, ValueError):
            params = {}
        positional = [
            parameter
            for parameter in params.values()
            if parameter.kind
            in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_varargs = any(
            parameter.kind == Parameter.VAR_POSITIONAL
            for parameter in params.values()
        )
        if accepts_varargs or len(positional) >= 3:
            await callback(server_name, status, manager)
        elif manager is self.mcp_manager:
            await callback(server_name, status)

    def _new_mcp_manager(self, workspace_root: Path | None) -> Any:
        from backend.mcp.manager import MCPServerManager

        owner: dict[str, Any] = {}

        async def on_status_change(server_name: str, status: Any) -> None:
            manager = owner.get("manager")
            if manager is not None:
                await self._handle_scoped_mcp_status_change(
                    manager,
                    server_name,
                    status,
                )

        manager = MCPServerManager(
            on_status_change=on_status_change,
            elicitation_handler=self._handle_mcp_elicitation,
            workspace_root=workspace_root,
        )
        owner["manager"] = manager
        return manager

    async def ensure_mcp_manager(
        self,
        workspace_root: str | Path | None,
        *,
        activate: bool = False,
        reload: bool = False,
    ) -> Any:
        """Return the session MCP owner bound to one explicit workspace."""

        manager, key, start_task = await self._prepare_mcp_manager(
            workspace_root,
            activate=activate,
        )
        return await self._await_mcp_manager_ready(
            manager,
            key,
            start_task,
            reload=reload,
        )

    async def _prepare_mcp_manager(
        self,
        workspace_root: str | Path | None,
        *,
        activate: bool,
    ) -> tuple[Any, str, asyncio.Task[Any] | None]:
        """Bind the workspace owner without waiting for external servers."""

        resolved_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None and str(workspace_root).strip()
            else None
        )
        key = self._mcp_workspace_key(resolved_root)
        async with self._mcp_manager_lock:
            manager = self._mcp_managers.get(key)
            if manager is None:
                manager = self._new_mcp_manager(resolved_root)
                self._mcp_managers[key] = manager
                self._mcp_start_tasks[key] = asyncio.create_task(manager.start_all())
            start_task = self._mcp_start_tasks.get(key)
            if activate:
                self.mcp_manager = manager

        return manager, key, start_task

    async def _await_mcp_manager_ready(
        self,
        manager: Any,
        key: str,
        start_task: asyncio.Task[Any] | None,
        *,
        reload: bool,
    ) -> Any:
        """Wait for one prepared manager's connection lifecycle."""

        startup_result: Any = None
        if start_task is not None and not start_task.done():
            try:
                workspace_label = getattr(manager, "workspace_root", None) or "projectless"
                startup_result = await _with_startup_timeout(
                    f"MCP manager ({workspace_label})",
                    asyncio.shield(start_task),
                    timeout=30.0,
                )
            except Exception:
                if start_task.done():
                    async with self._mcp_manager_lock:
                        if self._mcp_start_tasks.get(key) is start_task:
                            self._mcp_start_tasks.pop(key, None)
                raise
            if startup_result is _STARTUP_TIMED_OUT:
                return manager
        if start_task is not None:
            try:
                await start_task
            finally:
                if start_task.done():
                    async with self._mcp_manager_lock:
                        if self._mcp_start_tasks.get(key) is start_task:
                            self._mcp_start_tasks.pop(key, None)

        if reload:
            await manager.reload_config()
        return manager

    async def begin_mcp_workspace_activation(
        self,
        workspace_root: str | Path | None,
    ) -> tuple[Any, asyncio.Task[Any]]:
        """Bind a workspace now and finish MCP startup in the background."""

        manager, key, start_task = await self._prepare_mcp_manager(
            workspace_root,
            activate=True,
        )
        ready_task = asyncio.create_task(
            self._await_mcp_manager_ready(
                manager,
                key,
                start_task,
                reload=True,
            )
        )
        return manager, ready_task

    async def activate_mcp_workspace(
        self,
        workspace_root: str | Path | None,
    ) -> Any:
        return await self.ensure_mcp_manager(
            workspace_root,
            activate=True,
            reload=True,
        )

    def get_mcp_manager_for_workspace(
        self,
        workspace_root: str | Path | None,
    ) -> Any | None:
        return self._mcp_managers.get(self._mcp_workspace_key(workspace_root))

    async def reload_mcp_managers(self, *, exclude: Any | None = None) -> None:
        managers = [
            manager
            for manager in dict.fromkeys(self._mcp_managers.values())
            if manager is not exclude
        ]
        if managers:
            await asyncio.gather(
                *(manager.reload_config() for manager in managers),
                return_exceptions=False,
            )

    def create_permission_checker(
        self,
        *,
        config: AppConfig | None = None,
        workspace_root: str | Path | None | object = _WORKSPACE_ROOT_UNSET,
    ) -> PermissionChecker:
        from backend.workspace.state import get_active_workspace_root

        if workspace_root is _WORKSPACE_ROOT_UNSET:
            resolved_workspace_root = get_active_workspace_root()
        elif workspace_root is None:
            resolved_workspace_root = None
        else:
            resolved_workspace_root = Path(workspace_root).expanduser().resolve()
        effective_config = config or load_config(cwd=resolved_workspace_root)
        return PermissionChecker(
            effective_config.permissions,
            resolved_workspace_root,
        )

    def create_llm(
        self,
        *,
        model_override: str | None = None,
        config: AppConfig | None = None,
    ) -> Any:
        effective_config = config or self.config or self.refresh_config()
        return self._create_session_llm(
            effective_config,
            model_override=model_override,
            provider_override=(
                str(getattr(effective_config.llm, "provider", "") or "") or None
            ),
        )

    def build_status_payload(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._status_cache_payload is not None and now < self._status_cache_expires_at:
            return self._status_cache_payload

        if self._build_status_payload is not None:
            self._status_cache_payload = self._build_status_payload()
        else:
            self._status_cache_payload = {
                "mcp": self.mcp_manager.get_all_status() if self.mcp_manager else [],
                "skills": self.skill_manager.list_all() if self.skill_manager else [],
                "memory": {
                    "available": self.file_memory is not None,
                    "files": self.file_memory.list_files() if self.file_memory else [],
                },
                "capabilities": self.build_capability_snapshot(),
            }
        self._status_cache_expires_at = now + self._status_cache_ttl_seconds
        return self._status_cache_payload

    def get_mcp_status(self) -> list[dict[str, Any]]:
        if self.mcp_manager:
            return self.mcp_manager.get_all_status()
        return []

    def build_capability_snapshot(self) -> dict[str, Any]:
        from backend.artifact.store import ArtifactStore

        registry = self.create_tool_registry(ArtifactStore())
        return registry.build_snapshot()

    def _resolve_mcp_request_session(
        self,
        params: dict[str, Any],
    ) -> tuple[Any | None, dict[str, Any]]:
        owner = params.get("_minicode_owner")
        if not isinstance(owner, dict) or self.ws_manager is None:
            return None, {}
        session_id = str(owner.get("session_id") or "").strip()
        conversation_id = str(owner.get("conversation_id") or "").strip()
        owner_manager = owner.get("mcp_manager")
        try:
            owner_generation = int(owner.get("conversation_run_generation"))
        except (TypeError, ValueError):
            owner_generation = 0
        if (
            not session_id
            or not conversation_id
            or owner_manager is None
            or owner_generation <= 0
        ):
            return None, owner
        iter_sessions = getattr(self.ws_manager, "iter_sessions", None)
        sessions = (
            list(iter_sessions())
            if callable(iter_sessions)
            else list(getattr(self.ws_manager, "_sessions", {}).values())
        )
        for session in sessions:
            if (
                str(getattr(session, "session_id", "") or "") == session_id
                and session.is_connected
            ):
                repository = getattr(session, "conversation_repo", None)
                get_conversation = getattr(repository, "get_conversation", None)
                conversation = (
                    get_conversation(conversation_id)
                    if callable(get_conversation)
                    else None
                )
                if conversation is None:
                    return None, owner
                workspace_root = session.session_lifecycle.workspace_root_for_conversation(
                    conversation
                )
                if self.get_mcp_manager_for_workspace(workspace_root) is not owner_manager:
                    return None, owner

                from backend.agent.conversation_query_guard import (
                    conversation_query_guards,
                )

                active_claim = conversation_query_guards().active_claim(
                    conversation_id
                )
                if (
                    active_claim is None
                    or active_claim.generation != owner_generation
                ):
                    return None, owner
                return session, owner
        return None, owner

    async def _await_mcp_owner_operation(
        self,
        operation: Awaitable[Any],
        owner: dict[str, Any],
        *,
        label: str,
        maximum_seconds: float,
    ) -> Any:
        """Await one server-initiated operation inside its owner turn fence."""

        operation_task = asyncio.ensure_future(operation)
        cancel_event = owner.get("cancel_event")
        cancel_wait: asyncio.Task[bool] | None = None
        if isinstance(cancel_event, asyncio.Event):
            cancel_wait = asyncio.create_task(cancel_event.wait())

        timeout = max(0.05, float(maximum_seconds))
        deadline = owner.get("deadline_monotonic")
        if isinstance(deadline, (int, float)):
            timeout = min(timeout, max(0.0, float(deadline) - time.monotonic()))

        try:
            waiters: set[asyncio.Future[Any]] = {operation_task}
            if cancel_wait is not None:
                waiters.add(cancel_wait)
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return operation_task.result()
            if cancel_wait is not None and cancel_wait in done:
                raise PermissionError(f"MCP {label} cancelled with its owning turn")
            raise TimeoutError(f"MCP {label} exceeded its owner deadline")
        finally:
            if not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
            if cancel_wait is not None and not cancel_wait.done():
                cancel_wait.cancel()
            if cancel_wait is not None:
                await asyncio.gather(cancel_wait, return_exceptions=True)

    async def _handle_mcp_elicitation(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle elicitation/create request from MCP server."""
        import uuid

        from backend.hooks.manager import HookEvent

        session, owner = self._resolve_mcp_request_session(params)
        if not session:
            return {"action": "cancel", "error": "MCP elicitation has no active owning session"}

        request_id = f"elicit_{uuid.uuid4().hex}"
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            prompt = "MCP server requests additional input."
        raw_schema = params.get("schema")
        schema = dict(raw_schema) if isinstance(raw_schema, dict) else {}
        conversation_id = str(owner.get("conversation_id") or "").strip()

        # cc runs Elicitation hooks and validates the requested schema
        # before prompting (elicitationHandler.ts); a blocked hook cancels.
        hook_mgr = owner.get("hook_manager")
        if hook_mgr is not None and hook_mgr.has_hooks(HookEvent.ELICITATION):
            hook_result = await hook_mgr.run_elicitation(
                prompt,
                elicitation_id=request_id,
                mcp_server_name=str(params.get("_mcp_server_name") or ""),
                mode=str(params.get("mode") or ""),
                url=str(params.get("url") or ""),
                requested_schema=schema or None,
            )
            if getattr(hook_result, "blocked", False):
                return {
                    "action": "cancel",
                    "error": str(getattr(hook_result, "message", "") or "Elicitation blocked by hook"),
                }

        # Build the payload
        payload = {
            "type": "control_request",
            "request_id": request_id,
            "conversation_id": conversation_id,
            "request": {
                "subtype": "elicitation",
                "tool_use_id": request_id,
                "prompt": prompt,
                "question": prompt,
                "schema": schema,
            }
        }

        # Register the future in the owning session's typed wait lane.  The
        # shared approval payload index remains the replay/ownership source so
        # existing clients see MCP prompts exactly like other control requests.
        future = asyncio.get_running_loop().create_future()
        from backend.ws.turn_wait_state import TurnWaitState

        wait_state = TurnWaitState.for_session(session)
        wait_state.register_waiter(request_id, future, kind="elicitation")
        wait_state.pending_approval_payloads[request_id] = payload

        try:
            # Send event to client. A disconnected owner must fail immediately
            # instead of leaving an unreachable prompt registered for five
            # minutes, and every exit path below shares the same cleanup.
            sent = await session.send_payload(payload, log_context="mcp:elicitation")
            if not sent:
                await session.emit_approval_cancelled_once(
                    [request_id],
                    reason="mcp_elicitation_delivery_failed",
                    conversation_id=conversation_id,
                )
                return {"action": "cancel", "error": "MCP elicitation could not be delivered"}
            # Wait for user input from the desktop app (up to 5 minutes)
            result = await self._await_mcp_owner_operation(
                future,
                owner,
                label="elicitation",
                maximum_seconds=300.0,
            )

            # The client responds via "answer" or "approval" command, which resolves
            # the future with the payload. Let's inspect result structure:
            if str(result.get("action") or "").strip().lower() in {
                "cancel",
                "deny",
                "reject",
            }:
                await session.emit_approval_cancelled_once(
                    [request_id],
                    reason="mcp_elicitation_rejected",
                    conversation_id=conversation_id,
                )
                return {"action": "cancel", "error": "User cancelled the elicitation"}
            answer = result.get("answer") or result.get("content") or ""
            await session.emit_approval_cancelled_once(
                [request_id],
                reason="mcp_elicitation_resolved",
                conversation_id=conversation_id,
            )
            return {
                "action": "submit",
                "response": {
                    "answer": answer
                }
            }
        except TimeoutError:
            await session.emit_approval_cancelled_once(
                [request_id],
                reason="mcp_elicitation_timeout",
                conversation_id=conversation_id,
            )
            return {"action": "cancel", "error": "User response timed out"}
        except PermissionError as exc:
            await session.emit_approval_cancelled_once(
                [request_id],
                reason="mcp_elicitation_owner_cancelled",
                conversation_id=conversation_id,
            )
            return {"action": "cancel", "error": str(exc)}
        except asyncio.CancelledError:
            await session.emit_approval_cancelled_once(
                [request_id],
                reason="mcp_elicitation_cancelled",
                conversation_id=conversation_id,
            )
            return {"action": "cancel", "error": "Interaction cancelled"}
        except Exception as exc:
            await session.emit_approval_cancelled_once(
                [request_id],
                reason="mcp_elicitation_failed",
                conversation_id=conversation_id,
            )
            return {"action": "cancel", "error": str(exc) or "MCP elicitation failed"}
        finally:
            wait_state.remove_waiter(request_id)
            wait_state.pending_approval_payloads.pop(request_id, None)
