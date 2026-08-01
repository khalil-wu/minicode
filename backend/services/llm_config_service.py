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
    load_config,
)
from backend.hooks.runtime import run_config_change_hook

ConfigChangeHook = Callable[..., Awaitable[Any]]
ProviderResolver = Callable[[], str]
ModelsResolver = Callable[[str], Any]
ConfigLoader = Callable[[], AppConfig]

VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


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
    reasoning_effort_requested: bool
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

    if available_models and selected and selected not in available_models:
        selected = ""
        override_active = False
    if not selected and available_models:
        selected = available_models[0]

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
) -> dict[str, Any]:
    normalized_provider = str(provider or "").strip().lower() or "openai"
    payload_section: dict[str, Any]
    if normalized_provider == "anthropic":
        payload_section = get_anthropic_settings()
        provider_id = "anthropic"
    elif normalized_provider == "custom":
        payload_section = get_custom_settings()
        wire_api = str(payload_section.get("wire_api") or "chat").strip()
        provider_id = "custom_anthropic" if wire_api == "anthropic" else "custom"
    else:
        payload_section = get_openai_settings()
        provider_id = "openai"
    wire_api = str(
        payload_section.get("wire_api")
        or ("anthropic" if normalized_provider == "anthropic" else "chat")
    ).strip()
    base_url = str(payload_section.get("base_url") or "").strip()
    resolved_models_source = str(models_source or payload_section.get("models_source") or "").strip()
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
    reasoning_effort_requested = bool(reasoning_effort)
    if reasoning_effort and reasoning_effort not in VALID_REASONING_EFFORTS:
        raise ValueError("reasoning_effort must be none, minimal, low, medium, high, xhigh, or max")

    provider = config_mod._normalize_provider(raw_provider) if raw_provider else "openai"
    config = config_mod.load_config()
    saved_payload = config_mod.get_llm_settings_payload()
    notice: CommandResultNotice | None = None

    if reasoning_effort:
        target_provider = str(saved_payload.get("provider") or provider)
        if target_provider in {"openai", "custom"}:
            section = saved_payload.get(target_provider)
            if isinstance(section, dict):
                if not config_mod.active_provider_supports_reasoning_effort(saved_payload):
                    if not from_slash_command:
                        notice = CommandResultNotice(
                            command="effort",
                            message=(
                                "Reasoning effort was not applied because the active provider "
                                "did not declare supported reasoning-effort levels for this model."
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
                    config_mod.save_llm_settings(saved_payload)
                    await config_change_hook(source="llm", file_path=str(config_mod.SETTINGS_FILE))
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
        reasoning_effort_requested=reasoning_effort_requested,
        notice=notice,
    )
