"""Source session/token_budget.rs startup and per-model preference resolution."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from backend.agent.context_window import ContextWindowConfig
from backend.feature_flags import FeatureFlags

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenBudgetStartupConfig:
    enabled: bool
    token_budget: ContextWindowConfig | None


def _settings(config: Any) -> dict:
    return config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}


def _configured_token_budget(settings: dict) -> Any:
    return settings.get("features", {}).get("token_budget")


def _enable(config: Any, enabled: bool) -> Any:
    requirements = config.config_layer_stack.requirements.feature_requirements if config.config_layer_stack is not None else {}
    selected = requirements.get("token_budget", enabled)
    return replace(config, feature_flags=FeatureFlags({**config.feature_flags.flags, "token_budget": selected}))


def model_token_budget(config: Any, model_info: Any = None) -> dict | None:
    messages = model_info.model_messages if model_info is not None else config.llm.model_messages
    return messages.get("token_budget") if messages is not None else None


def has_explicit_token_budget_settings(config: Any) -> bool:
    raw = _configured_token_budget(_settings(config))
    if isinstance(raw, dict) and any(key not in {"enabled", "use_history_notes_extension"} for key in raw):
        return True
    configured = config.context_window_config
    if configured is None:
        return False
    default = ContextWindowConfig()
    return any(getattr(configured, name) != getattr(default, name) for name in (
        "reminder_threshold_tokens", "reminder_message_template", "guidance_message",
        "auto_compact_fallback_prompt", "auto_compact_fallback_buffer_tokens",
    ))


def experimental_context_is_eligible(config: Any, model_info: Any, auth: Any) -> bool:
    supports = model_info.supports_experimental_context if model_info is not None else config.llm.supports_experimental_context
    return bool(config.feature_flags.enabled("context_management") and supports and auth is not None
        and auth.auth_mode == "chatgpt" and auth.account_plan_type in {"plus", "pro", "prolite", "promax"}
        and auth.supports_codex_backend_routes and auth.requires_openai_auth
        and not (auth.has_env_key or auth.has_experimental_bearer_token or auth.has_auth or auth.has_aws))


def prepare_token_budget_for_startup(config: Any, model_info: Any = None, auth: Any = None,
                                     *, inherit_activation: bool = False) -> Any:
    snapshot = config.token_budget_startup_config
    if inherit_activation and snapshot is not None:
        return config
    if snapshot is not None:
        config = replace(_enable(config, snapshot.enabled), context_window_config=snapshot.token_budget)
    else:
        seed = config.context_window_config
        if seed is None and config.feature_flags.enabled("token_budget"):
            seed = ContextWindowConfig.from_settings(_settings(config))
        snapshot = TokenBudgetStartupConfig(config.feature_flags.enabled("token_budget"), seed)
        config = replace(config, context_window_config=seed)
    config = replace(config, token_budget_startup_config=snapshot)
    if experimental_context_is_eligible(config, model_info, auth):
        config = _enable(config, True)
        if config.feature_flags.enabled("token_budget"):
            seed = config.context_window_config or ContextWindowConfig.from_settings(_settings(config))
            config = replace(config, context_window_config=replace(seed, use_history_notes_extension=True))
    defaults = model_token_budget(config, model_info)
    explicit = config.context_window_config is not None or _configured_token_budget(_settings(config)) is not None
    if defaults is not None and defaults.get("enabled", False) is True and not explicit:
        config = _enable(config, True)
        if config.feature_flags.enabled("token_budget"):
            seed = ContextWindowConfig.from_settings(_settings(config))
            config = replace(config, context_window_config=replace(seed,
                use_history_notes_extension=defaults.get("use_history_notes_extension", False)))
    return config


def resolve_context_window_config(config: Any, model_info: Any = None) -> ContextWindowConfig | None:
    if not config.feature_flags.enabled("token_budget"):
        return None
    seed = config.context_window_config or ContextWindowConfig.from_settings(_settings(config))
    defaults = model_token_budget(config, model_info)
    if defaults is None or has_explicit_token_budget_settings(config):
        return seed
    try:
        resolved = replace(seed,
            reminder_threshold_tokens=defaults["reminder_threshold_tokens"],
            reminder_message_template=defaults["reminder_message_template"],
            guidance_message=defaults["guidance_message"],
            auto_compact_fallback_prompt=defaults["auto_compact_fallback_prompt"],
            auto_compact_fallback_buffer_tokens=defaults["auto_compact_fallback_buffer_tokens"])
        resolved.validate()
    except (KeyError, TypeError, ValueError) as error:
        # Source explicitly ignores invalid model-owned defaults, retaining
        # configured preferences. User config errors are never caught here.
        logger.warning("ignoring invalid model-owned token-budget defaults: %s", error)
        return seed
    return resolved


def token_budget_auth_for_snapshot(snapshot: Any) -> Any:
    supports = snapshot.model_info.supports_experimental_context if snapshot.model_info is not None else snapshot.config.llm.supports_experimental_context
    if not snapshot.config.feature_flags.enabled("context_management") or not supports:
        return None
    if snapshot.model_runtime is None:
        return None
    return snapshot.model_runtime.token_budget_auth_context(snapshot.model_info or snapshot.provider)


def prepare_snapshot_token_budget(snapshot: Any, *, inherit_activation: bool = False) -> Any:
    config = prepare_token_budget_for_startup(snapshot.config, snapshot.model_info,
        token_budget_auth_for_snapshot(snapshot), inherit_activation=inherit_activation)
    return replace(snapshot, config=config)


def config_for_model_switch(previous: Any, selected: Any) -> Any:
    # A model switch changes per-step defaults, not the session's configured
    # preferences or retained startup activation (Source step_activation_tests).
    config = replace(selected, token_budget_startup_config=previous.token_budget_startup_config,
                     context_window_config=previous.context_window_config)
    return _enable(config, previous.feature_flags.enabled("token_budget"))


def register_context_window_tools(registry: Any, config: ContextWindowConfig | None) -> None:
    from backend.tools.context_window_tools import (
        GetContextRemainingTool,
        NewContextTool,
    )

    if config is None:
        registry.unregister("new_context")
        registry.unregister("get_context_remaining")
        return
    existing = registry.get_tool("new_context")
    if existing is None or existing.config != config:
        registry.register(NewContextTool(config), replace=existing is not None, owner="codex-token-budget")
    elif registry.get_tool_owner("new_context") != "codex-token-budget":
        registry.register(existing, replace=True, owner="codex-token-budget")
    if registry.get_tool("get_context_remaining") is None:
        registry.register(GetContextRemainingTool(), owner="codex-token-budget")
