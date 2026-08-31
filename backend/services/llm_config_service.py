from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from backend.agent.message import AgentEvent
from backend.config import (
    AppConfig,
    get_available_models,
    get_custom_settings,
    get_llm_provider,
    get_openai_settings,
    get_anthropic_settings,
    get_provider_model_metadata,
    load_config,
)
from backend.hooks.runtime import run_config_change_hook
from backend.hooks.runtime import raise_if_config_change_blocked
from backend.llm.reasoning_effort import normalize_reasoning_effort, reasoning_effort_levels

ConfigChangeHook = Callable[..., Awaitable[Any]]
ProviderResolver = Callable[[], str]
ModelsResolver = Callable[[str], Any]
ConfigLoader = Callable[[], AppConfig]

@dataclass
class CommandResultNotice:
    command: str
    message: str
    level: str = "info"
    data: dict[str, Any] | None = None


@dataclass
class LLMConfigUpdateResult:
    config: AppConfig
    saved_payload: dict[str, Any]
    provider: str
    reasoning_effort: str
    notice: CommandResultNotice | None = None


@dataclass(frozen=True)
class LLMSelectionState:
    config: AppConfig
    provider: str
    available_models: list[str]
    selected_model: str
    model_override_active: bool


def refresh_llm_selection_state(
    *,
    previous_provider: str,
    selected_model: str,
    model_override_active: bool,
    prefer_config: bool = False,
    provider_resolver: ProviderResolver | None = None,
    models_resolver: ModelsResolver | None = None,
    config_loader: ConfigLoader = load_config,
) -> LLMSelectionState:
    config = config_loader()
    resolve_provider = provider_resolver or get_llm_provider
    resolve_models = models_resolver or get_available_models
    provider = resolve_provider()
    available_models = list(resolve_models(provider))
    config_model = str(getattr(config.llm, "model", "") or "").strip()
    selected = str(selected_model or "").strip()
    override_active = bool(model_override_active)

    if provider != str(previous_provider or ""):
        override_active = False
        selected = config_model
    elif prefer_config or not override_active:
        selected = config_model

    return LLMSelectionState(
        config=config,
        provider=provider,
        available_models=available_models,
        selected_model=selected,
        model_override_active=override_active,
    )


def model_unavailable_event(model: str, available_models: list[str]) -> AgentEvent:
    return AgentEvent.error(
        (
            f"Model '{model}' is not in the configured model list. "
            f"Open Settings to add it, or choose one of: {', '.join(available_models)}."
        ),
        recoverable=True,
        error_type="llm",
        provider_error_type="model",
    )


def llm_model_updated_payload(
    *,
    provider: str,
    selected_model: str,
    available_models: list[str],
    workspace_root: Any,
    models_source: str = "",
    provider_metadata: dict[str, Any] | None = None,
    settings_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_provider = str(provider or "").strip() or "openai"
    extension_metadata = dict(provider_metadata or {})
    extension_defined = (
        str(extension_metadata.get("models_source") or "").strip().lower()
        == "extension"
    )
    normalized_provider = (
        requested_provider
        if extension_defined
        else requested_provider.lower()
    )
    payload_section: dict[str, Any]
    if not extension_defined and normalized_provider == "anthropic":
        payload_section = (
            get_anthropic_settings(settings_data)
            if settings_data is not None
            else get_anthropic_settings()
        )
        provider_id = "anthropic"
    elif not extension_defined and normalized_provider == "custom":
        payload_section = (
            get_custom_settings(settings_data)
            if settings_data is not None
            else get_custom_settings()
        )
        wire_api = str(payload_section.get("wire_api") or "chat").strip()
        provider_id = "custom_anthropic" if wire_api == "anthropic" else "custom"
    elif not extension_defined and normalized_provider == "openai":
        payload_section = (
            get_openai_settings(settings_data)
            if settings_data is not None
            else get_openai_settings()
        )
        provider_id = "openai"
    else:
        payload_section = extension_metadata
        provider_id = str(
            payload_section.get("provider_id") or normalized_provider
        ).strip()
    raw_wire_api = str(
        payload_section.get("wire_api")
        or ("anthropic" if normalized_provider == "anthropic" else "chat")
    ).strip()
    wire_api = {
        "anthropic-messages": "anthropic",
        "openai-responses": "responses",
        "openai-completions": "chat",
    }.get(raw_wire_api, raw_wire_api)
    base_url = str(payload_section.get("base_url") or "").strip()
    resolved_models_source = str(models_source or payload_section.get("models_source") or "").strip()
    if not extension_defined and normalized_provider in {
        "openai",
        "anthropic",
        "custom",
    }:
        resolved_metadata = get_provider_model_metadata(
            payload_section,
            selected_model,
        )
        reasoning_levels = list(
            reasoning_effort_levels(
                selected_model,
                wire_api,
                resolved_metadata["reasoning_effort_levels"],
            )
        )
        context_window = int(resolved_metadata["context_window"])
        context_window_source = str(resolved_metadata["context_window_source"])
        context_window_verified = bool(resolved_metadata["context_window_verified"])
        max_context_window = int(resolved_metadata["max_context_window"])
        max_context_window_source = str(
            resolved_metadata["max_context_window_source"]
        )
        max_context_window_verified = bool(
            resolved_metadata["max_context_window_verified"]
        )
        max_output_tokens = int(resolved_metadata["max_output_tokens"])
        max_output_tokens_source = str(
            resolved_metadata["max_output_tokens_source"]
        )
        max_output_tokens_verified = bool(
            resolved_metadata["max_output_tokens_verified"]
        )
        default_reasoning_effort = str(
            resolved_metadata["default_reasoning_effort"]
        )
        default_reasoning_summary = str(
            resolved_metadata["default_reasoning_summary"]
        )
    else:
        reasoning_levels = list(
            reasoning_effort_levels(
                selected_model,
                wire_api,
                payload_section.get("reasoning_effort_levels", []),
            )
        )
        try:
            context_window = max(
                0,
                int(payload_section.get("context_window") or 0),
            )
        except (TypeError, ValueError):
            context_window = 0
        context_window_source = str(
            payload_section.get("context_window_source") or ""
        )
        context_window_verified = bool(
            payload_section.get("context_window_verified", False)
        )
        try:
            max_context_window = max(
                context_window,
                int(payload_section.get("max_context_window") or 0),
            )
        except (TypeError, ValueError):
            max_context_window = context_window
        max_context_window_source = str(
            payload_section.get("max_context_window_source") or ""
        )
        max_context_window_verified = bool(
            payload_section.get("max_context_window_verified", False)
        )
        try:
            max_output_tokens = max(
                0,
                int(payload_section.get("max_output_tokens") or 0),
            )
        except (TypeError, ValueError):
            max_output_tokens = 0
        max_output_tokens_source = str(
            payload_section.get("max_output_tokens_source") or ""
        )
        max_output_tokens_verified = bool(
            payload_section.get("max_output_tokens_verified", False)
        )
        default_reasoning_effort = str(
            payload_section.get("default_reasoning_effort") or ""
        )
        default_reasoning_summary = str(
            payload_section.get("default_reasoning_summary") or ""
        )
    configured_reasoning_effort = str(
        payload_section.get("configured_reasoning_effort")
        or payload_section.get("reasoning_effort")
        or ""
    ).strip().lower()
    reasoning_effort_supported = wire_api != "anthropic" and bool(reasoning_levels)
    effective_reasoning_effort = (
        normalize_reasoning_effort(
            selected_model,
            wire_api,
            configured_reasoning_effort,
            reasoning_levels,
            default_reasoning_effort,
        )
        if reasoning_effort_supported
        else ""
    )
    return {
        "type": "llm.model.updated",
        "provider": normalized_provider,
        "provider_id": provider_id,
        "base_url": base_url,
        "wire_api": wire_api,
        "model": selected_model,
        "current_model": selected_model,
        "available_models": available_models,
        "models_source": resolved_models_source,
        # The legacy field now reports what is actually sent on the wire.
        "reasoning_effort": effective_reasoning_effort,
        "configured_reasoning_effort": configured_reasoning_effort,
        "effective_reasoning_effort": effective_reasoning_effort,
        "reasoning_effort_supported": reasoning_effort_supported,
        "reasoning_effort_levels": reasoning_levels if wire_api != "anthropic" else [],
        "context_window": context_window,
        "context_window_source": context_window_source,
        "context_window_verified": context_window_verified,
        "max_context_window": max_context_window,
        "max_context_window_source": max_context_window_source,
        "max_context_window_verified": max_context_window_verified,
        "max_output_tokens": max_output_tokens,
        "max_output_tokens_source": max_output_tokens_source,
        "max_output_tokens_verified": max_output_tokens_verified,
        "default_reasoning_effort": default_reasoning_effort,
        "default_reasoning_summary": default_reasoning_summary,
        "working_directory": str(workspace_root) if workspace_root is not None else "",
    }


async def apply_llm_config_update(
    data: dict[str, Any],
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> LLMConfigUpdateResult:
    import backend.config as config_mod

    raw_provider = str(data.get("provider", "")).strip()
    source = str(data.get("source") or "").strip()
    from_slash_command = source.startswith("slash:")
    reasoning_effort = str(data.get("reasoning_effort") or "").strip().lower()

    provider = config_mod._normalize_provider(raw_provider) if raw_provider else "openai"
    config = config_mod.load_config()
    saved_payload = config_mod.get_llm_settings_payload()
    notice: CommandResultNotice | None = None

    if reasoning_effort:
        target_provider = str(saved_payload.get("provider") or provider)
        if target_provider in {"openai", "custom"}:
            section = saved_payload.get(target_provider)
            if isinstance(section, dict):
                declared_levels = config_mod.active_provider_reasoning_effort_levels(
                    saved_payload
                )
                if reasoning_effort not in declared_levels:
                    if not from_slash_command:
                        notice = CommandResultNotice(
                            command="effort",
                            message=(
                                "Reasoning effort was not applied because the active model "
                                f"did not declare the '{reasoning_effort}' level."
                            ),
                            level="warning",
                            data={
                                "reasoning_effort": reasoning_effort,
                                "applied": False,
                            },
                        )
                    reasoning_effort = ""
                else:
                    section["reasoning_effort"] = reasoning_effort
                    hook_result = await config_change_hook(
                        source="llm",
                        file_path=str(config_mod.SETTINGS_FILE),
                    )
                    raise_if_config_change_blocked(
                        hook_result,
                        source="llm",
                        file_path=str(config_mod.SETTINGS_FILE),
                    )
                    # Persist only the active provider section. Passing the
                    # complete effective payload would refresh unrelated
                    # provider-history entries, including environment-only
                    # local proxy endpoints.
                    config_mod.save_llm_settings({target_provider: dict(section)})
                    config = config_mod.load_config()
                    saved_payload = config_mod.get_llm_settings_payload()
        else:
            if not from_slash_command:
                notice = CommandResultNotice(
                    command="effort",
                    message="Reasoning effort applies to OpenAI-compatible providers.",
                    level="warning",
                    data={
                        "reasoning_effort": reasoning_effort,
                        "applied": False,
                    },
                )
            reasoning_effort = ""

    return LLMConfigUpdateResult(
        config=config,
        saved_payload=saved_payload,
        provider=str(saved_payload.get("provider") or provider),
        reasoning_effort=reasoning_effort,
        notice=notice,
    )
