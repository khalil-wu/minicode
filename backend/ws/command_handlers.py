"""Session utility mixin for WebSocketSession."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from backend.agent.model_execution import ModelExecutionSnapshot


class SessionCommandHandlersMixin:
    """Shared session utilities used by flat websocket handlers."""

    def publish_live_model_execution(self, conversation_id: str, snapshot: ModelExecutionSnapshot) -> None:
        from backend.ws.agent_runner import _lease_session_llm_for_task

        task = self.run_manager.publish_model_execution(conversation_id, snapshot)
        if task is not None:
            _lease_session_llm_for_task(self, snapshot.llm, task)

    def _register_command_handlers(self) -> None:
        from backend.commands.slash_commands import register_all_slash_commands
        from backend.ws.handlers import register_domain_handlers

        register_all_slash_commands(self.command_registry)
        register_domain_handlers(self)

    def refresh_llm_selection(self, *, prefer_config: bool = False) -> None:
        from backend.services.llm_config_service import refresh_llm_selection_state
        from backend.config import load_config
        from backend.ws.agent_runner import _resolver_accepts_positional_arguments

        workspace_root = self.session_lifecycle.workspace_root_for_conversation()
        scoped_config = load_config(cwd=workspace_root)
        scoped_settings = (
            scoped_config.config_layer_stack.effective_config()
            if scoped_config.config_layer_stack is not None
            else None
        )
        provider_resolver = self._resolve_llm_provider
        models_resolver = self._resolve_available_models
        models_source_resolver = self._resolve_models_source

        def resolve_provider() -> str:
            if _resolver_accepts_positional_arguments(provider_resolver, scoped_settings):
                return provider_resolver(scoped_settings)
            return provider_resolver()

        def resolve_models(provider: str) -> Any:
            if _resolver_accepts_positional_arguments(
                models_resolver,
                provider,
                scoped_settings,
            ):
                return models_resolver(provider, scoped_settings)
            return models_resolver(provider)

        def resolve_models_source(provider: str) -> str:
            if _resolver_accepts_positional_arguments(
                models_source_resolver,
                provider,
                scoped_settings,
            ):
                return str(models_source_resolver(provider, scoped_settings))
            return str(models_source_resolver(provider))

        model_runtime = self._model_runtime_for_conversation(self.active_conversation_id)
        if model_runtime is not None:
            model_runtime.refresh(settings_snapshot=scoped_settings)
        conversation = self.conversation_repo.get_conversation_summary(self.active_conversation_id) if self.active_conversation_id else None
        task_selection = conversation.model_selection if conversation is not None else {}
        if task_selection and not prefer_config:
            provider = task_selection["provider"]
            self.provider = provider
            self.selected_model = task_selection["model"]
            self._model_override_active = True
            self._provider_override_active = provider != resolve_provider()
            self.available_models = (
                [model.id for model in model_runtime.get_models(provider)]
                if model_runtime is not None else list(resolve_models(provider))
            )
            self.models_source = (
                "extension"
                if model_runtime is not None and model_runtime.get_registered_provider_config(provider) is not None
                else resolve_models_source(provider)
            )
            self.config = replace(scoped_config, llm=replace(
                scoped_config.llm,
                model=self.selected_model,
                reasoning_effort=task_selection.get("reasoning_effort", scoped_config.llm.reasoning_effort),
            ))
            if provider in {"openai", "anthropic", "custom"} or (
                model_runtime is not None and model_runtime.get_provider(provider) is not None
            ):
                self._bind_selected_llm(model_runtime)
            return
        if conversation is not None:
            # Older tasks without a saved choice follow project defaults;
            # another visible task's session override does not belong to them.
            prefer_config = True
        current_provider = str(self.provider or "").strip()
        extension_provider_active = bool(
            model_runtime is not None
            and model_runtime.get_registered_provider_config(current_provider)
            is not None
        )
        if (
            not prefer_config
            and model_runtime is not None
            and (
                bool(self._provider_override_active)
                or extension_provider_active
            )
        ):
            provider = current_provider
            models = list(model_runtime.get_models(provider))
            available_models = [model.id for model in models]
            selected_model = str(self.selected_model or "").strip()
            if model_runtime.get_provider(provider) is not None and (
                not selected_model or selected_model in available_models
            ):
                self.config = scoped_config
                self.available_models = available_models
                if not selected_model:
                    self.selected_model = ""
                self.models_source = (
                    "extension"
                    if model_runtime.get_registered_provider_config(provider)
                    is not None
                    else resolve_models_source(provider)
                )
                self._bind_selected_llm(model_runtime)
                return

        selection = refresh_llm_selection_state(
            previous_provider=str(self.provider or ""),
            selected_model=str(self.selected_model or ""),
            model_override_active=bool(self._model_override_active),
            prefer_config=prefer_config,
            provider_resolver=resolve_provider,
            models_resolver=resolve_models,
            config_loader=lambda: scoped_config,
        )
        self.config = selection.config
        self.provider = selection.provider
        self.available_models = selection.available_models
        self.selected_model = selection.selected_model
        self._model_override_active = selection.model_override_active
        self._provider_override_active = False
        self.models_source = resolve_models_source(self.provider)
        if self.selected_model:
            self._bind_selected_llm(model_runtime)

    def reset_model_selection_overrides(self) -> None:
        self._model_override_active = False
        self._provider_override_active = False

    def _bind_selected_llm(self, model_runtime: Any | None) -> None:
        from backend.ws.agent_runner import _config_with_runtime_model_budget, _get_or_create_session_llm

        self.config = _config_with_runtime_model_budget(
            self.config, model_runtime=model_runtime,
            provider=self.provider, model=self.selected_model,
        )
        self.llm = _get_or_create_session_llm(
            self, config=self.config, provider=self.provider,
            model=self.selected_model, model_runtime=model_runtime,
        )
        self.context_builder.bind_llm(self.llm)
        self.context_builder.bind_budget(self.config.token_budget)

    async def _run_cwd_changed_hook(self, *, old_cwd: str, new_cwd: str) -> None:
        from backend.hooks.runtime import run_cwd_changed_hook
        from backend.hooks.manager import load_hook_manager_for_workspace, register_hook_manager_for_session

        scope_id = self.active_conversation_id or self.session_id
        manager = await asyncio.to_thread(
            load_hook_manager_for_workspace,
            Path(new_cwd) if new_cwd else None,
            session_id=scope_id,
        )
        register_hook_manager_for_session(scope_id, manager, owner_session_id=self.session_id)
        await run_cwd_changed_hook(old_cwd=old_cwd, new_cwd=new_cwd, hook_manager=manager)

    # ── LLM model selection ──────────────────────────────

    async def _set_selected_provider_model(
        self,
        provider: str,
        model: str,
        *,
        manual_override: bool,
        model_runtime: Any | None = None,
        emit_unavailable: bool = True,
        conversation_id: str | None = None,
        reasoning_effort: str | None = None,
        config_override: Any | None = None,
    ) -> bool:
        from backend.config import load_config
        from backend.ws.agent_runner import _resolver_accepts_positional_arguments

        normalized_provider = str(provider or "").strip()
        normalized_model = str(model or "").strip()
        if not normalized_provider or not normalized_model:
            return False
        owner_id = conversation_id or self.active_conversation_id
        conversation = self.conversation_repo.get_conversation_summary(owner_id) if owner_id else None
        workspace_root = self.session_lifecycle.workspace_root_for_conversation(conversation)
        if owner_id and conversation is None:
            raise ValueError("The conversation selected for this model change no longer exists")
        scoped_config = config_override or load_config(cwd=workspace_root)
        scoped_settings = (
            scoped_config.config_layer_stack.effective_config()
            if scoped_config.config_layer_stack is not None
            else None
        )
        provider_resolver = self._resolve_llm_provider
        models_resolver = self._resolve_available_models
        models_source_resolver = self._resolve_models_source

        def resolve_provider() -> str:
            if _resolver_accepts_positional_arguments(provider_resolver, scoped_settings):
                return provider_resolver(scoped_settings)
            return provider_resolver()

        def resolve_models(provider: str) -> Any:
            if _resolver_accepts_positional_arguments(
                models_resolver,
                provider,
                scoped_settings,
            ):
                return models_resolver(provider, scoped_settings)
            return models_resolver(provider)

        def resolve_models_source(provider: str) -> str:
            if _resolver_accepts_positional_arguments(
                models_source_resolver,
                provider,
                scoped_settings,
            ):
                return str(models_source_resolver(provider, scoped_settings))
            return str(models_source_resolver(provider))
        if model_runtime is None:
            model_runtime = self._model_runtime_for_conversation(owner_id)
        selected_runtime_model = None
        if model_runtime is not None:
            if config_override is not None:
                model_runtime.refresh(settings_snapshot=scoped_settings)
            await model_runtime.refresh_oauth_credentials(normalized_provider)
            await model_runtime.refresh_provider_auth(normalized_provider)
            runtime_models = model_runtime.get_models(normalized_provider)
            available_models = [item.id for item in runtime_models]
            selected_runtime_model = model_runtime.get_model(
                normalized_provider,
                normalized_model,
            )
        else:
            available_models = list(resolve_models(normalized_provider))
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
            self.refresh_llm_selection(prefer_config=True)
            return False
        from backend.agent.model_execution import ModelExecutionSnapshot
        from backend.llm.model_selection import model_thinking_levels
        from backend.ws.agent_runner import (
            _apply_thinking_level, _config_with_runtime_model_budget,
            _get_or_create_session_llm,
        )

        effort = reasoning_effort
        if effort is None:
            effort = (conversation.model_selection.get("reasoning_effort", scoped_config.llm.reasoning_effort)
                      if conversation is not None else scoped_config.llm.reasoning_effort)
        config = _config_with_runtime_model_budget(
            replace(scoped_config, llm=replace(scoped_config.llm, provider=normalized_provider,
                model=normalized_model, reasoning_effort=effort)),
            model_runtime=model_runtime, provider=normalized_provider, model=normalized_model,
        )
        adapter = _get_or_create_session_llm(self, config=config,
            provider=normalized_provider, model=normalized_model, model_runtime=model_runtime)
        supported = model_thinking_levels(selected_runtime_model, adapter)
        if reasoning_effort is not None and reasoning_effort not in supported:
            raise ValueError(f"Reasoning effort '{reasoning_effort}' is not supported for '{normalized_provider}/{normalized_model}'. Supported: {', '.join(supported) or 'none'}.")
        effective = _apply_thinking_level(adapter, selected_runtime_model, effort)
        config = replace(config, llm=replace(config.llm, reasoning_effort=effective))
        models_source = (
            "extension" if model_runtime is not None
            and model_runtime.get_registered_provider_config(normalized_provider) is not None
            else resolve_models_source(normalized_provider)
        )
        snapshot = ModelExecutionSnapshot(config=config, llm=adapter,
            provider=normalized_provider, model=normalized_model, thinking_level=effective,
            model_runtime=model_runtime, available_models=tuple(available_models),
            models_source=models_source, model_info=selected_runtime_model)
        if conversation is not None and manual_override:
            self.conversation_repo.update_model_selection(conversation.id,
                provider=normalized_provider, model=normalized_model, reasoning_effort=effective)
        self.publish_live_model_execution(owner_id or "", snapshot)
        if owner_id == self.active_conversation_id:
            self.config = config
            self.provider = normalized_provider
            self.available_models = available_models
            self.selected_model = normalized_model
            self.models_source = models_source
            self._model_override_active = manual_override
            self._provider_override_active = bool(manual_override and normalized_provider != resolve_provider())
            self.llm = adapter
            self.context_builder.bind_llm(adapter)
            self.context_builder.bind_budget(config.token_budget)
        return True

    async def set_selected_model(self, model: str, *, manual_override: bool,
                                 conversation_id: str | None = None, reasoning_effort: str | None = None) -> bool:
        from backend.config import load_config

        owner_id = conversation_id or self.active_conversation_id
        if owner_id == self.active_conversation_id:
            self.refresh_llm_selection()
            provider, current_model = self.provider, self.selected_model
        else:
            conversation = self.conversation_repo.get_conversation_summary(owner_id)
            if conversation is None:
                raise ValueError("The conversation selected for this model change no longer exists")
            config = load_config(cwd=self.session_lifecycle.workspace_root_for_conversation(conversation))
            provider = conversation.model_selection.get("provider", config.llm.provider)
            current_model = conversation.model_selection.get("model", config.llm.model)
        return await self._set_selected_provider_model(
            provider, model or current_model, manual_override=manual_override,
            conversation_id=owner_id, reasoning_effort=reasoning_effort,
        )

    async def send_llm_state(self, *, force: bool = False) -> None:
        """Publish the effective model projection once for each state change.

        A freshly published extension generation emits a ModelRuntime change as
        part of its normal startup.  That projection can race the first user
        turn even when provider, model and capabilities are unchanged.  Keep
        the websocket state stream edge-triggered, while ``force`` preserves
        the mandatory initial snapshot for every newly attached connection.
        """
        from backend.services.llm_config_service import llm_model_updated_payload

        self.refresh_llm_selection()
        workspace_root = self.session_lifecycle.workspace_root_for_conversation()
        model_runtime = self._model_runtime_for_conversation(self.active_conversation_id)
        provider_metadata = (
            model_runtime.provider_payload(self.provider, self.selected_model)
            if model_runtime is not None
            else None
        )
        settings_data = (
            self.config.config_layer_stack.effective_config()
            if self.config.config_layer_stack is not None
            else None
        )
        payload = llm_model_updated_payload(
            provider=self.provider,
            selected_model=self.selected_model,
            available_models=self.available_models,
            workspace_root=workspace_root,
            models_source=self.models_source,
            provider_metadata=provider_metadata,
            settings_data=settings_data,
            configured_reasoning_effort=self.config.llm.reasoning_effort,
        )
        payload["conversation_id"] = self.active_conversation_id
        previous_payload = self._last_llm_state_payload
        if not force and previous_payload == payload:
            return

        # Record before the awaited websocket write. Concurrent runtime
        # notifications then observe the in-flight projection and cannot append
        # an identical state event behind a user turn. Roll it back if sending
        # fails so the next genuine state synchronization can retry.
        self._last_llm_state_payload = dict(payload)
        sent = await self.send_payload(payload, log_context="llm.model.updated")
        if not sent and self._last_llm_state_payload == payload:
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
        if model_runtime is None:
            return
        catalog_error = str(model_runtime.get_error() or "").strip()
        if catalog_error == self._last_model_catalog_error:
            return
        self._last_model_catalog_error = catalog_error
        if not catalog_error:
            return
        from backend.ws.command_results import emit_command_error

        await emit_command_error(
            self,
            "llm.models",
            catalog_error,
            data={"provider": self.provider, "models_source": self.models_source},
        )

    # ── Workspace utilities ──────────────────────────────

    async def create_isolated_conversation_worktree(self, conversation: Any) -> Any | None:
        return await self.session_lifecycle.create_isolated_conversation_worktree(conversation)

    async def switch_workspace_for_conversation(
        self,
        conversation: Any,
        *,
        announce: bool,
        wait_for_initialize: bool = False,
        error_command: str | None = "workspace.activate",
    ) -> bool:
        return await self.session_lifecycle.switch_workspace_for_conversation(
            conversation,
            announce=announce,
            wait_for_initialize=wait_for_initialize,
            error_command=error_command,
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
        return await self.session_lifecycle.activate_workspace_path(
            path_str,
            announce=announce,
            wait_for_initialize=wait_for_initialize,
            error_command=error_command,
            conversation_id=conversation_id,
        )



    def git_branch_for(self, path: Path) -> str:
        from backend.services.workspace_service import git_branch_for

        return git_branch_for(path)

    def main_worktree_root(self, path: Path) -> Path:
        from backend.services.workspace_service import main_worktree_root

        return main_worktree_root(path)

    def is_path_within(self, path: Path, parent: Path) -> bool:
        from backend.services.workspace_service import is_path_within

        return is_path_within(path, parent)

    def resolve_workspace_cwd(self, cwd: str | None = None) -> Path:
        from backend.services.workspace_service import resolve_workspace_cwd

        return resolve_workspace_cwd(self.session_lifecycle.current_workspace_root(), cwd)

    def resolve_requested_workspace(self, requested_workspace: str | None = None) -> Path:
        from backend.services.workspace_service import resolve_requested_workspace

        return resolve_requested_workspace(
            self.session_lifecycle.current_workspace_root(), requested_workspace
        )

    def validate_git_relative_path(self, path: str) -> str:
        from backend.services.workspace_service import validate_git_relative_path

        return validate_git_relative_path(path)

    def worktree_has_local_changes(self, path: Path) -> bool:
        from backend.services.workspace_service import worktree_has_local_changes

        return worktree_has_local_changes(path)
