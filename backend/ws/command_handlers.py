"""Session utility mixin for WebSocketSession."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent

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
        if callable(models_source_resolver):
            self.models_source = models_source_resolver(self.provider)

    async def _run_cwd_changed_hook(self, *, old_cwd: str, new_cwd: str) -> None:
        from backend.hooks.runtime import run_cwd_changed_hook

        await run_cwd_changed_hook(old_cwd=old_cwd, new_cwd=new_cwd)

    # ── Skill toggle ─────────────────────────────────────

    async def _toggle_skill(self, skill_name: str, *, activate: bool) -> None:
        from backend.services.skills_service import toggle_skill_events
        from backend.ws.command_results import emit_command_error

        for event in toggle_skill_events(self.skill_manager, skill_name, activate=activate):
            if event.type == "error":
                await emit_command_error(self, "skills.toggle", event)
            else:
                await self._send_event(event)

    # ── LLM model selection ──────────────────────────────

    async def _set_selected_model(self, model: str, *, manual_override: bool) -> None:
        normalized = model.strip()
        if not normalized:
            return
        self._refresh_llm_selection()
        if self.available_models and normalized not in self.available_models:
            from backend.services.llm_config_service import model_unavailable_event
            from backend.ws.command_results import emit_command_error
            await emit_command_error(self, "model.set", model_unavailable_event(normalized, self.available_models))
            self._refresh_llm_selection(prefer_config=True)
            return
        self.selected_model = normalized
        self._model_override_active = manual_override
        from backend.ws.agent_runner import _clear_session_llm_cache, _get_or_create_session_llm

        _clear_session_llm_cache(self)
        self.llm = _get_or_create_session_llm(
            self,
            config=self.config,
            provider=str(getattr(self, "provider", "") or ""),
            model=self.selected_model,
        )
        self.context_builder._llm = self.llm

    async def _send_llm_state(self) -> None:
        from backend.services.llm_config_service import llm_model_updated_payload

        self._refresh_llm_selection()
        workspace_root = self._workspace_root_for_conversation()
        await self._send_ws_payload(
            llm_model_updated_payload(
                provider=self.provider,
                selected_model=self.selected_model,
                available_models=self.available_models,
                workspace_root=workspace_root,
                models_source=getattr(self, "models_source", ""),
            ),
            log_context="llm.model.updated",
        )

    # ── Workspace utilities ──────────────────────────────

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
            return conversation

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
            await self._send_event(result.notice_event)
        return updated or conversation

    async def _switch_workspace_for_conversation(
        self,
        conversation: Any,
        *,
        announce: bool,
        wait_for_initialize: bool = False,
    ) -> None:
        from backend.services.workspace_service import conversation_workspace_path, workspace_matches_context

        workspace_path = conversation_workspace_path(conversation)
        if not workspace_path:
            clear_runtime = getattr(self, "_clear_workspace_runtime", None)
            if callable(clear_runtime):
                clear_runtime()
            return

        if workspace_matches_context(workspace_path, self._workspace_context):
            return

        await self._activate_workspace_path(
            workspace_path,
            announce=announce,
            wait_for_initialize=wait_for_initialize,
        )

    async def _activate_workspace_path(
        self,
        path_str: str,
        *,
        announce: bool = False,
        wait_for_initialize: bool = False,
        error_command: str | None = "workspace.activate",
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

        try:
            ctx = create_workspace_context(project_path)
            old_workspace_root = workspace_context_root(getattr(self, "_workspace_context", None))
            # 先切换全局工作区指针与会话上下文，使 git/file API 立即指向新目录，
            # 再做耗时的文件索引扫描。
            self._workspace_context = ctx
            set_active_workspace(project_path)
            await self._run_cwd_changed_hook(old_cwd=old_workspace_root, new_cwd=str(project_path))
            restart_file_watcher = getattr(self, "_restart_file_watcher", None)
            if callable(restart_file_watcher):
                restart_file_watcher(project_path)
            async def initialize_workspace() -> bool:
                try:
                    metadata = await ctx.initialize()
                    record_recent_workspace_project(project_path, metadata)
                    if announce:
                        await self._send_ws_payload(
                            workspace_imported_payload(ctx, metadata),
                            log_context="workspace.imported",
                        )
                    return True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = f"Failed to switch session workspace: {exc}"
                    if error_command:
                        from backend.ws.command_results import emit_command_error
                        await emit_command_error(self, error_command, message)
                    else:
                        await self._send_event(AgentEvent.error(message, recoverable=True))
                    return False

            if wait_for_initialize:
                return await initialize_workspace()
            else:
                task = getattr(self, "_workspace_context_task", None)
                if task is not None and not task.done():
                    task.cancel()
                self._workspace_context_task = asyncio.create_task(initialize_workspace())
            return True
        except Exception as exc:
            message = f"Failed to switch session workspace: {exc}"
            if error_command:
                from backend.ws.command_results import emit_command_error
                await emit_command_error(self, error_command, message)
            else:
                await self._send_event(AgentEvent.error(message, recoverable=True))
            return False

    def _git_branch_for(self, path: Path) -> str:
        from backend.services.workspace_service import git_branch_for

        return git_branch_for(path, getattr(self, "_workspace_state", None))

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
