"""Session utility mixin for WebSocketSession."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.async_cleanup import (
    cancel_and_retire,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


class SessionCommandHandlersMixin:
    """Shared session utilities used by flat websocket handlers."""

    def _register_command_handlers(self) -> None:
        from backend.commands.slash_commands import register_all_slash_commands
        from backend.ws.handlers import register_domain_handlers

        register_all_slash_commands(self.command_registry)
        register_domain_handlers(self)

    def _refresh_llm_selection(self, *, prefer_config: bool = False) -> None:
        from backend.services.llm_config_service import refresh_llm_selection_state

        model_runtime_resolver = getattr(self, "_model_runtime_for_conversation", None)
        model_runtime = (
            model_runtime_resolver(getattr(self, "active_conversation_id", None))
            if callable(model_runtime_resolver)
            else None
        )
        if model_runtime is not None:
            model_runtime.refresh()
        current_provider = str(getattr(self, "provider", "") or "").strip()
        extension_provider_active = bool(
            model_runtime is not None
            and model_runtime.get_registered_provider_config(current_provider)
            is not None
        )
        if (
            not prefer_config
            and model_runtime is not None
            and (
                bool(getattr(self, "_provider_override_active", False))
                or extension_provider_active
            )
        ):
            provider = current_provider
            models = list(model_runtime.get_models(provider))
            available_models = [model.id for model in models]
            selected_model = str(getattr(self, "selected_model", "") or "").strip()
            if model_runtime.get_provider(provider) is not None and (
                not selected_model or selected_model in available_models
            ):
                from backend.config import load_config

                self.config = load_config()
                self.available_models = available_models
                if not selected_model:
                    self.selected_model = ""
                self.models_source = (
                    "extension"
                    if model_runtime.get_registered_provider_config(provider)
                    is not None
                    else str(
                        getattr(self, "_resolve_models_source", lambda _provider: "")(
                            provider
                        )
                    )
                )
                return

        provider_resolver = getattr(self, "_resolve_llm_provider", None)
        models_resolver = getattr(self, "_resolve_available_models", None)
        models_source_resolver = getattr(self, "_resolve_models_source", None)
        selection = refresh_llm_selection_state(
            previous_provider=str(getattr(self, "provider", "") or ""),
            selected_model=str(getattr(self, "selected_model", "") or ""),
            model_override_active=bool(getattr(self, "_model_override_active", False)),
            prefer_config=prefer_config,
            provider_resolver=provider_resolver if callable(provider_resolver) else None,
            models_resolver=models_resolver if callable(models_resolver) else None,
        )
        self.config = selection.config
        self.provider = selection.provider
        self.available_models = selection.available_models
        self.selected_model = selection.selected_model
        self._model_override_active = selection.model_override_active
        self._provider_override_active = False
        if callable(models_source_resolver):
            self.models_source = models_source_resolver(self.provider)

    async def _run_cwd_changed_hook(self, *, old_cwd: str, new_cwd: str) -> None:
        from backend.hooks.runtime import run_cwd_changed_hook

        await run_cwd_changed_hook(old_cwd=old_cwd, new_cwd=new_cwd)

    # ── LLM model selection ──────────────────────────────

    async def _set_selected_provider_model(
        self,
        provider: str,
        model: str,
        *,
        manual_override: bool,
        model_runtime: Any | None = None,
        emit_unavailable: bool = True,
    ) -> bool:
        normalized_provider = str(provider or "").strip()
        normalized_model = str(model or "").strip()
        if not normalized_provider or not normalized_model:
            return False
        if model_runtime is None:
            resolver = getattr(self, "_model_runtime_for_conversation", None)
            if callable(resolver):
                model_runtime = resolver(getattr(self, "active_conversation_id", None))
        selected_runtime_model = None
        if model_runtime is not None:
            refresh_oauth = getattr(model_runtime, "refresh_oauth_credentials", None)
            if callable(refresh_oauth):
                await refresh_oauth(normalized_provider)
            refresh_provider_auth = getattr(model_runtime, "refresh_provider_auth", None)
            if callable(refresh_provider_auth):
                await refresh_provider_auth(normalized_provider)
            runtime_models = model_runtime.get_models(normalized_provider)
            available_models = [item.id for item in runtime_models]
            selected_runtime_model = model_runtime.get_model(
                normalized_provider,
                normalized_model,
            )
        else:
            models_resolver = getattr(self, "_resolve_available_models", None)
            available_models = list(
                models_resolver(normalized_provider)
                if callable(models_resolver)
                else ()
            )
        if (
            model_runtime is not None
            and selected_runtime_model is None
        ) or (
            model_runtime is None
            and available_models
            and normalized_model not in available_models
        ):
            if not emit_unavailable:
                return False
            from backend.services.llm_config_service import model_unavailable_event
            from backend.ws.command_results import emit_command_error

            await emit_command_error(
                self,
                "model.set",
                model_unavailable_event(normalized_model, available_models),
            )
            self._refresh_llm_selection(prefer_config=True)
            return False
        from backend.config import load_config

        from backend.ws.agent_runner import _config_with_runtime_model_budget

        self.config = _config_with_runtime_model_budget(
            load_config(),
            model_runtime=model_runtime,
            provider=normalized_provider,
            model=normalized_model,
        )
        configured_provider = str(
            getattr(self, "_resolve_llm_provider", lambda: normalized_provider)()
            or normalized_provider
        ).strip().lower()
        self.provider = normalized_provider
        self.available_models = available_models
        self.selected_model = normalized_model
        self._model_override_active = manual_override
        self._provider_override_active = bool(
            manual_override and normalized_provider != configured_provider
        )
        self.models_source = (
            "extension"
            if model_runtime is not None
            and model_runtime.get_registered_provider_config(normalized_provider)
            is not None
            else str(
                getattr(self, "_resolve_models_source", lambda _provider: "")(
                    normalized_provider
                )
            )
        )
        from backend.ws.agent_runner import _clear_session_llm_cache, _get_or_create_session_llm

        _clear_session_llm_cache(self)
        self.llm = _get_or_create_session_llm(
            self,
            config=self.config,
            provider=normalized_provider,
            model=self.selected_model,
            model_runtime=model_runtime,
        )
        self.context_builder._llm = self.llm
        self.context_builder._budget = self.config.token_budget
        return True

    async def _set_selected_model(self, model: str, *, manual_override: bool) -> None:
        self._refresh_llm_selection()
        await self._set_selected_provider_model(
            str(getattr(self, "provider", "") or ""),
            model,
            manual_override=manual_override,
        )

    async def _send_llm_state(self, *, force: bool = False) -> None:
        """Publish the effective model projection once for each state change.

        A freshly published extension generation emits a ModelRuntime change as
        part of its normal startup.  That projection can race the first user
        turn even when provider, model and capabilities are unchanged.  Keep
        the websocket state stream edge-triggered, while ``force`` preserves
        the mandatory initial snapshot for every newly attached connection.
        """
        from backend.services.llm_config_service import llm_model_updated_payload

        self._refresh_llm_selection()
        workspace_root = self._workspace_root_for_conversation()
        model_runtime_resolver = getattr(self, "_model_runtime_for_conversation", None)
        model_runtime = (
            model_runtime_resolver(getattr(self, "active_conversation_id", None))
            if callable(model_runtime_resolver)
            else None
        )
        provider_metadata = (
            model_runtime.provider_payload(self.provider, self.selected_model)
            if model_runtime is not None
            else None
        )
        payload = llm_model_updated_payload(
            provider=self.provider,
            selected_model=self.selected_model,
            available_models=self.available_models,
            workspace_root=workspace_root,
            models_source=getattr(self, "models_source", ""),
            provider_metadata=provider_metadata,
        )
        previous_payload = getattr(self, "_last_llm_state_payload", None)
        if not force and previous_payload == payload:
            return

        # Record before the awaited websocket write. Concurrent runtime
        # notifications then observe the in-flight projection and cannot append
        # an identical state event behind a user turn. Roll it back if sending
        # fails so the next genuine state synchronization can retry.
        self._last_llm_state_payload = dict(payload)
        sent = await self._send_ws_payload(payload, log_context="llm.model.updated")
        if not sent and getattr(self, "_last_llm_state_payload", None) == payload:
            self._last_llm_state_payload = previous_payload
        if sent:
            await self._report_model_catalog_error(model_runtime)

    async def _report_model_catalog_error(self, model_runtime: Any) -> None:
        """Surface the catalog's own load/parse/refresh failures to the user.

        The runtime already accumulates structured reasons (models.json parse
        errors, per-provider composition failures, availability refresh
        failures). Without this the catalog just arrives empty and the user is
        given a model list with no explanation.
        """
        get_error = getattr(model_runtime, "get_error", None)
        catalog_error = str(get_error() or "").strip() if callable(get_error) else ""
        if catalog_error == str(getattr(self, "_last_model_catalog_error", "") or ""):
            return
        self._last_model_catalog_error = catalog_error
        if not catalog_error:
            return
        from backend.ws.command_results import emit_command_error

        await emit_command_error(
            self,
            "llm.models",
            catalog_error,
            data={"provider": self.provider, "models_source": getattr(self, "models_source", "")},
        )

    # ── Workspace utilities ──────────────────────────────

    def _retire_workspace_task(self, attribute: str) -> None:
        """Cancel a superseded workspace task without delaying the switch."""

        task = getattr(self, attribute, None)
        if task is None:
            return
        retired = getattr(self, "_retired_workspace_tasks", None)
        if not isinstance(retired, set):
            retired = set()
            self._retired_workspace_tasks = retired
        cancel_and_retire(task, owner=retired)
        if getattr(self, attribute, None) is task:
            setattr(self, attribute, None)

    async def _create_isolated_conversation_worktree(self, conversation: Any) -> Any | None:
        from backend.services.conversation_payload_service import create_isolated_worktree_binding

        result = create_isolated_worktree_binding(
            conversation,
            current_workspace_root=self._current_workspace_root(),
            main_worktree_root=self._main_worktree_root,
        )
        if result.error_event is not None:
            from backend.ws.command_results import emit_command_error
            await emit_command_error(self, "conversation.create", result.error_event)
            # The record was persisted with git_isolated=True before the attempt.
            # Leaving it that way describes isolation that does not exist, and
            # cleanup_isolated_worktree then refuses to remove a worktree that
            # was never created, so the conversation could never be deleted.
            return self.conversation_repo.update_workspace_binding(
                conversation.id,
                workspace_root=str(self._current_workspace_root() or ""),
                git_branch=result.git_branch,
                worktree_path="",
                git_isolated=False,
            ) or conversation

        if not result.created:
            return conversation

        updated = self.conversation_repo.update_workspace_binding(
            conversation.id,
            workspace_root=result.workspace_root,
            git_branch=result.git_branch,
            worktree_path=result.worktree_path,
            git_isolated=True,
        )
        if result.notice_event is not None:
            result.notice_event.data.setdefault(
                "conversation_id",
                str(result.conversation_id or getattr(conversation, "id", "")).strip(),
            )
            await self._send_event(result.notice_event)
        return updated or conversation

    async def _switch_workspace_for_conversation(
        self,
        conversation: Any,
        *,
        announce: bool,
        wait_for_initialize: bool = False,
    ) -> bool:
        from backend.services.workspace_service import conversation_workspace_path, workspace_matches_context

        workspace_path = conversation_workspace_path(conversation)
        if not workspace_path:
            clear_runtime = getattr(self, "_clear_workspace_runtime", None)
            if callable(clear_runtime):
                clear_runtime()
            return True

        if workspace_matches_context(workspace_path, self._workspace_context):
            return True

        return await self._activate_workspace_path(
            workspace_path,
            announce=announce,
            wait_for_initialize=wait_for_initialize,
            conversation_id=str(getattr(conversation, "id", "") or "").strip(),
        )

    async def _activate_workspace_path(
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
            set_active_workspace,
            workspace_context_root,
            workspace_imported_payload,
        )

        request = parse_workspace_activation_request(path_str)
        if request.error_event is not None:
            await self._send_event(request.error_event)
            return False
        project_path = request.project_path
        if project_path is None:
            return False

        owner_conversation_id = str(
            conversation_id or getattr(self, "active_conversation_id", None) or ""
        ).strip()
        # Explicit activation is user-visible conversation state. On a
        # first-run session create the ordinary active conversation only after
        # path validation, so the success event is never unowned.
        if announce and not owner_conversation_id:
            self._ensure_active_conversation()
            owner_conversation_id = str(self.active_conversation_id or "").strip()
        command_context = getattr(self, "_client_command_context", None)
        request_id = str(command_context.get() if command_context is not None else "").strip()

        old_workspace_context = getattr(self, "_workspace_context", None)
        old_workspace_root = workspace_context_root(old_workspace_context)
        self._workspace_generation = int(getattr(self, "_workspace_generation", 0)) + 1
        activation_generation = self._workspace_generation
        ctx: Any | None = None
        restart_file_watcher = getattr(self, "_restart_file_watcher", None)
        from backend.commands.slash_commands import refresh_slash_commands

        def refresh_commands() -> None:
            registry = getattr(self, "command_registry", None)
            if registry is not None:
                refresh_slash_commands(registry)

        def activation_is_current() -> bool:
            return (
                getattr(self, "_workspace_generation", 0) == activation_generation
                and getattr(self, "_workspace_context", None) is ctx
            )

        async def reload_workspace_mcp(
            workspace_root: Path | None,
            *,
            enforce_generation: bool = True,
        ) -> None:
            from backend.api import _state

            bootstrap = getattr(_state, "bootstrap", None)
            activate_manager = getattr(bootstrap, "activate_mcp_workspace", None)
            if callable(activate_manager):
                manager = await activate_manager(workspace_root)
            else:
                from backend.api.routes_health import get_mcp_manager

                manager = get_mcp_manager()
                if manager is not None:
                    await manager.reload_config()
            if manager is None:
                return
            if enforce_generation and ctx is not None and not activation_is_current():
                return
            self.mcp_manager = manager
            refresh_registry = getattr(self, "refresh_tool_registry_if_mcp_changed", None)
            if callable(refresh_registry):
                refresh_registry(allow_when_busy=False)

        refresh_registry = getattr(self, "refresh_tool_registry_if_mcp_changed", None)

        def publish_mcp_manager(manager: Any | None) -> None:
            self.mcp_manager = manager
            if callable(refresh_registry):
                refresh_registry(allow_when_busy=False)

        async def begin_workspace_mcp(workspace_root: Path) -> None:
            from backend.api import _state

            bootstrap = getattr(_state, "bootstrap", None)
            begin_activation = getattr(bootstrap, "begin_mcp_workspace_activation", None)
            ready_task: asyncio.Task[Any] | None = None
            try:
                if not callable(begin_activation):
                    await reload_workspace_mcp(workspace_root)
                    return

                manager, ready_task = await begin_activation(workspace_root)
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
            from backend.workspace.state import clear_active_workspace_root

            self._workspace_context = old_workspace_context
            restored_root = Path(old_workspace_root) if old_workspace_root else None
            if restored_root is not None:
                set_active_workspace(restored_root)
                if callable(restart_file_watcher):
                    restart_file_watcher(restored_root)
            else:
                clear_active_workspace_root()
                watcher = getattr(self, "file_watcher", None)
                if watcher is not None:
                    watcher.stop()
                    self.file_watcher = None

            await reload_workspace_mcp(restored_root, enforce_generation=False)

            skill_manager = getattr(self, "skill_manager", None)
            if skill_manager is not None and hasattr(skill_manager, "set_project_root"):
                skill_manager.set_project_root(restored_root)
            refresh_commands()
            try:
                await self._run_cwd_changed_hook(
                    old_cwd=str(project_path),
                    new_cwd=str(restored_root) if restored_root is not None else "",
                )
            except Exception:
                logger.debug("Workspace rollback cwd hook failed", exc_info=True)
            send_capabilities = getattr(self, "_send_runtime_capabilities", None)
            if callable(send_capabilities):
                await send_capabilities(source="workspace.activate.rollback")

        try:
            ctx = create_workspace_context(project_path)
            # Superseded initialization remains session-owned until it settles,
            # but must never delay this user-visible switch or publish into the
            # new generation.
            self._retire_workspace_task("_workspace_context_task")
            self._retire_workspace_task("_workspace_mcp_task")
            # 先切换全局工作区指针与会话上下文，使 git/file API 立即指向新目录，
            # 再做耗时的文件索引扫描。
            self._workspace_context = ctx
            set_active_workspace(project_path)
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
                    if getattr(self, "_workspace_mcp_task", None) is completed:
                        self._workspace_mcp_task = None

                mcp_task.add_done_callback(clear_mcp_task)

            async def prepare_workspace_projection() -> None:
                """Finish non-index workspace projections for this generation."""

                refresh_commands()
                if not activation_is_current():
                    return
                skill_manager = getattr(self, "skill_manager", None)
                if skill_manager is not None and hasattr(skill_manager, "set_project_root"):
                    skill_manager.set_project_root(project_path)
                if not activation_is_current():
                    return
                await self._run_cwd_changed_hook(
                    old_cwd=old_workspace_root,
                    new_cwd=str(project_path),
                )
                if not activation_is_current():
                    return
                if callable(restart_file_watcher):
                    restart_file_watcher(project_path)
                send_capabilities = getattr(self, "_send_runtime_capabilities", None)
                if callable(send_capabilities):
                    await send_capabilities(source="workspace.activate")

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
                        await self._send_ws_payload(
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
                        await emit_command_error(self, error_command, message)
                    else:
                        await self._send_event(AgentEvent.error(message, recoverable=True))
                    return False

            if wait_for_initialize:
                await prepare_workspace_projection()
                return await initialize_workspace()
            else:
                context_task = asyncio.create_task(initialize_workspace())
                self._workspace_context_task = context_task

                def clear_context_task(completed: asyncio.Task[Any]) -> None:
                    if getattr(self, "_workspace_context_task", None) is completed:
                        self._workspace_context_task = None

                context_task.add_done_callback(clear_context_task)
            return True
        except Exception as exc:
            await rollback_workspace()
            message = f"Failed to switch session workspace: {exc}"
            if error_command:
                from backend.ws.command_results import emit_command_error
                await emit_command_error(self, error_command, message)
            else:
                await self._send_event(AgentEvent.error(message, recoverable=True))
            return False

    def _git_branch_for(self, path: Path) -> str:
        from backend.services.workspace_service import git_branch_for

        return git_branch_for(path)

    def _main_worktree_root(self, path: Path) -> Path:
        from backend.services.workspace_service import main_worktree_root

        return main_worktree_root(path)

    def _is_path_within(self, path: Path, parent: Path) -> bool:
        from backend.services.workspace_service import is_path_within

        return is_path_within(path, parent)

    def _resolve_workspace_cwd(self, cwd: str | None = None) -> Path:
        from backend.services.workspace_service import resolve_workspace_cwd

        return resolve_workspace_cwd(self._current_workspace_root(), cwd)

    def _resolve_requested_workspace(self, requested_workspace: str | None = None) -> Path:
        from backend.services.workspace_service import resolve_requested_workspace

        return resolve_requested_workspace(self._current_workspace_root(), requested_workspace)

    def _validate_git_relative_path(self, path: str) -> str:
        from backend.services.workspace_service import validate_git_relative_path

        return validate_git_relative_path(path)

    def _worktree_has_local_changes(self, path: Path) -> bool:
        from backend.services.workspace_service import worktree_has_local_changes

        return worktree_has_local_changes(path)
