"""Session utility mixin for WebSocketSession."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionCommandHandlersMixin:
    """Shared session utilities used by flat websocket handlers."""

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
            model_runtime.refresh()
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

    def reset_model_selection_overrides(self) -> None:
        self._model_override_active = False
        self._provider_override_active = False

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
        from backend.config import load_config
        from backend.ws.agent_runner import _resolver_accepts_positional_arguments

        normalized_provider = str(provider or "").strip()
        normalized_model = str(model or "").strip()
        if not normalized_provider or not normalized_model:
            return False
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
        if model_runtime is None:
            model_runtime = self._model_runtime_for_conversation(self.active_conversation_id)
        selected_runtime_model = None
        if model_runtime is not None:
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
        from backend.ws.agent_runner import _config_with_runtime_model_budget

        self.config = _config_with_runtime_model_budget(
            scoped_config,
            model_runtime=model_runtime,
            provider=normalized_provider,
            model=normalized_model,
        )
        configured_provider = str(
            resolve_provider()
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
            else resolve_models_source(normalized_provider)
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

    async def set_selected_model(self, model: str, *, manual_override: bool) -> None:
        self.refresh_llm_selection()
        await self._set_selected_provider_model(
            str(self.provider or ""),
            model,
            manual_override=manual_override,
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
        )
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
    ) -> bool:
        return await self.session_lifecycle.switch_workspace_for_conversation(
            conversation,
            announce=announce,
            wait_for_initialize=wait_for_initialize,
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
