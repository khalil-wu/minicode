"""Read the model selection for an unsubmitted draft without selecting a thread."""
from __future__ import annotations

from pathlib import Path

from backend.config import load_config
from backend.commands.catalog import get_enabled_composer_command_catalog
from backend.feature_flags import feature_flags_payload
from backend.llm.capabilities import capabilities_from_settings
from backend.llm.model_runtime import ModelRuntime
from backend.llm.model_selection import default_model_thinking_level, model_thinking_levels
from backend.permissions.approval_policy import policy_for_mode
from backend.services.llm_config_service import llm_model_updated_payload


def draft_model_selection(workspace_root: str, model: str = "", reasoning_effort: str = "", config_cwd: str = "") -> dict:
    scope = config_cwd or workspace_root
    config = load_config(cwd=Path(scope) if scope else None, resolve_credentials=False)
    stack = config.config_layer_stack
    settings = stack.effective_config()
    # A request owns this catalog. Refreshing a live thread's ModelRuntime here
    # would change its next step while the user was only editing another draft.
    runtime = ModelRuntime(settings_snapshot=settings)
    provider = config.llm.provider
    definitions = runtime.get_models(provider)
    if error := runtime.get_error():
        raise ValueError(error)
    selected = model or config.llm.model
    definition = runtime.get_model(provider, selected) if selected else None
    if model and definition is None:
        raise ValueError(f"Model '{model}' is unavailable for this project")
    options = []
    for item in definitions:
        levels = model_thinking_levels(item, configured_reasoning_effort=config.llm.reasoning_effort)
        options.append({"model": item.id, "reasoning_effort_levels": list(levels),
            "default_reasoning_effort": default_model_thinking_level(item, levels) or "off"})
    levels = (model_thinking_levels(definition, configured_reasoning_effort=config.llm.reasoning_effort)
        if definition is not None else capabilities_from_settings(config.llm, provider=provider).reasoning_effort_levels or ("off",))
    effort = reasoning_effort or config.llm.reasoning_effort
    if reasoning_effort and reasoning_effort not in levels:
        raise ValueError(f"Reasoning effort '{reasoning_effort}' is unavailable for '{selected}'")
    if effort not in levels:
        effort = default_model_thinking_level(definition or config.llm, levels) or "off"
    default_definition = runtime.get_model(provider, config.llm.model) if config.llm.model else None
    defaults = None
    if default_definition is not None:
        default_levels = model_thinking_levels(default_definition, configured_reasoning_effort=config.llm.reasoning_effort)
        default_effort = config.llm.reasoning_effort
        if default_effort not in default_levels:
            default_effort = default_model_thinking_level(default_definition, default_levels) or "off"
        defaults = {"provider": provider, "model": config.llm.model, "reasoning_effort": default_effort,
            "conversation_id": "", "working_directory": workspace_root}
    payload = llm_model_updated_payload(provider=provider, selected_model=selected if definition is not None else "",
        available_models=[item.id for item in definitions], workspace_root=workspace_root,
        models_source="extension" if runtime.get_registered_provider_config(provider) is not None else "configured",
        provider_metadata=runtime.provider_payload(provider, selected), settings_data=settings,
        configured_reasoning_effort=effort)
    return {**payload, "model_selection_mode": "model" if model else "default",
        "default_model_selection": defaults, "available_model_options": options,
        "config_version": stack.fingerprint, "config_cwd": scope,
        "feature_flags": feature_flags_payload(settings, managed_requirements=stack.requirements.feature_requirements),
        "composer_commands": get_enabled_composer_command_catalog(scope or None, resolve_active_workspace=False),
        "available_permission_modes": [mode for mode in ("confirm", "auto", "plan", "bypass")
            if stack.requirements.resolve_permission_mode(mode,
                approval_policy=policy_for_mode(mode, config.permissions.approval_policy))[1] is None]}
