from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from backend.config import (
    SETTINGS_FILE,
    get_anthropic_settings,
    get_custom_settings,
    get_openai_settings,
    normalize_custom_wire_api,
    resolve_provider_api_key_for_base_url,
)
from backend.hooks.runtime import run_config_change_hook

from backend.services.llm_provider_helpers import (
    _LATEST_PROVIDER_MODELS,
    _check_openai_compatible_generation,
    _fetch_anthropic_models,
    _fetch_openai_compatible_models,
    _http_error_message,
    _http_error_status,
    _is_anthropic_model_id,
    _manual_models_from_payload,
    _merge_model_sources,
    _merge_models,
    _normalize_provider_value,
    _deepseek_model_candidates_only,
    _persist_refreshed_models,
    _resolve_openai_provider_id,
    _select_refreshed_model,
    _status_hint_for_provider,
)

logger = logging.getLogger(__name__)

FetchModels = Callable[[str, str], Awaitable[list[str]]]
CheckGeneration = Callable[[str, str, str, str], Awaitable[None]]
ConfigChangeHook = Callable[..., Awaitable[Any]]


async def refresh_llm_models(
    request: Any,
    *,
    fetch_anthropic_models: FetchModels = _fetch_anthropic_models,
    fetch_openai_models: FetchModels = _fetch_openai_compatible_models,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> dict[str, Any]:
    provider = _normalize_provider_value(request.provider)

    if provider == "anthropic":
        current = get_anthropic_settings()
        api_key = request.anthropic.api_key.strip() or current["api_key"]
        base_url = request.anthropic.base_url.strip() or current["base_url"]
        current_model = request.anthropic.model.strip() or current["model"]
        provider_id = "anthropic_off"
        models: list[str] = []
        source = "preset"
        source_message = ""
        try:
            models = await fetch_anthropic_models(base_url, api_key)
        except Exception as exc:
            logger.warning("Failed to refresh Anthropic model list: %s", exc)
        if models:
            source = "live"
            source_message = "Fetched available models from Anthropic /models."
        else:
            models = list(_LATEST_PROVIDER_MODELS[provider_id])
            source_message = (
                "No API key configured. Showing the built-in fallback models."
                if not api_key.strip()
                else "Live fetch failed or returned no models. Showing the built-in fallback models."
            )
        selected_model = _select_refreshed_model(provider_id, models, current_model)
        final_models = _merge_models(models, selected_model)
        return {
            "provider": provider,
            "provider_id": provider_id,
            "models": final_models,
            "selected_model": selected_model,
            "source": source,
            "source_message": source_message,
        }

    provider_key = "custom" if provider == "custom" else "openai"
    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
    api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
    current_model = incoming.model.strip() or str(current.get("model", "")).strip()
    raw_wire_api = str(getattr(incoming, "wire_api", "") or current.get("wire_api", "") or "").strip().lower()
    wire_api = normalize_custom_wire_api(base_url, raw_wire_api, str(current.get("wire_api", "chat")))
    provider_id = "custom_anthropic" if provider == "custom" and wire_api == "anthropic" else _resolve_openai_provider_id(base_url)
    models: list[str] = []
    discovered_reasoning_efforts: dict[str, list[str]] = {}
    source = "preset"
    source_message = ""
    manual_models = _manual_models_from_payload(incoming)
    if provider_id == "custom_anthropic":
        manual_models = [model for model in manual_models if _is_anthropic_model_id(model)]
    preset_models = list(_LATEST_PROVIDER_MODELS.get(provider_id, _LATEST_PROVIDER_MODELS.get("anthropic_off", []) if provider_id == "custom_anthropic" else []))
    try:
        if provider_id == "custom_anthropic":
            models = await fetch_anthropic_models(base_url, api_key)
        else:
            models = await fetch_openai_models(base_url, api_key)
            discovered_reasoning_efforts = dict(getattr(models, "reasoning_efforts", {}))
    except Exception as exc:
        logger.warning("Failed to refresh %s model list: %s", provider_id, exc)
    if provider_id == "deepseek":
        models = _deepseek_model_candidates_only(models)
        manual_models = _deepseek_model_candidates_only(manual_models)
        preset_models = _deepseek_model_candidates_only(preset_models)
    if models:
        source = "live"
        models = _merge_model_sources(models, manual_models)
        source_message = "Fetched models from the current provider /models endpoint and prioritized the latest releases."
    else:
        models = _merge_model_sources(preset_models, manual_models)
        if not api_key.strip():
            source_message = (
                "No API key is configured, keeping manual model list."
                if not preset_models
                else "No API key is configured, showing built-in model list."
            )
        elif preset_models:
            source_message = "Live model refresh failed or returned no models, showing built-in model list."
        else:
            source_message = "Live model refresh failed or returned no models, keeping manual model list."
    selected_model = _select_refreshed_model(provider_id, models, current_model)
    final_models = _merge_models(models, selected_model)
    discovered_effort_levels = discovered_reasoning_efforts.get(selected_model, [])
    config = None
    if source == "live":
        config = _persist_refreshed_models(provider, final_models, selected_model, discovered_effort_levels)
        await config_change_hook(source="llm", file_path=str(SETTINGS_FILE))
    payload = {
        "provider": provider,
        "provider_id": provider_id,
        "models": final_models,
        "selected_model": selected_model,
        "source": source,
        "source_message": source_message,
    }
    if config is not None:
        payload["_config"] = config
    return payload


async def check_llm_connection(
    request: Any,
    *,
    fetch_anthropic_models: FetchModels = _fetch_anthropic_models,
    fetch_openai_models: FetchModels = _fetch_openai_compatible_models,
    check_openai_generation: CheckGeneration = _check_openai_compatible_generation,
) -> dict[str, Any]:
    provider = _normalize_provider_value(request.provider)

    if provider == "anthropic":
        current = get_anthropic_settings()
        api_key = request.anthropic.api_key.strip() or current["api_key"]
        base_url = request.anthropic.base_url.strip() or current["base_url"]
        model = request.anthropic.model.strip() or current["model"]
        if not api_key.strip():
            return {
                "ok": False,
                "provider": provider,
                "provider_id": "anthropic_off",
                "base_url": base_url,
                "model": model,
                "wire_api": "anthropic",
                "has_api_key": False,
                "message": "Missing Anthropic API key.",
                "hint": _status_hint_for_provider("anthropic_off", None, False),
            }
        try:
            models = await fetch_anthropic_models(base_url, api_key)
            return {
                "ok": True,
                "provider": provider,
                "provider_id": "anthropic_off",
                "base_url": base_url,
                "model": model,
                "wire_api": "anthropic",
                "has_api_key": True,
                "message": "Anthropic authentication succeeded.",
                "models": _merge_models(models, model),
            }
        except Exception as exc:
            status_code = _http_error_status(exc)
            return {
                "ok": False,
                "provider": provider,
                "provider_id": "anthropic_off",
                "base_url": base_url,
                "model": model,
                "wire_api": "anthropic",
                "has_api_key": True,
                "status_code": status_code,
                "message": _http_error_message(exc),
                "hint": _status_hint_for_provider("anthropic_off", status_code, True),
            }

    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
    api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
    model = incoming.model.strip() or str(current.get("model", "")).strip()
    raw_wire_api = str(getattr(incoming, "wire_api", "") or current.get("wire_api", "") or "").strip().lower()
    wire_api = normalize_custom_wire_api(base_url, raw_wire_api, str(current.get("wire_api", "chat")))
    provider_id = "custom_anthropic" if provider == "custom" and wire_api == "anthropic" else _resolve_openai_provider_id(base_url)

    if not api_key.strip():
        return {
            "ok": False,
            "provider": provider,
            "provider_id": provider_id,
            "base_url": base_url,
            "model": model,
            "wire_api": wire_api or "chat",
            "has_api_key": False,
            "message": "Missing API key for current provider.",
            "hint": _status_hint_for_provider(provider_id, None, False),
        }

    try:
        if provider_id == "custom_anthropic":
            models = await fetch_anthropic_models(base_url, api_key)
        else:
            models = await fetch_openai_models(base_url, api_key)
            await check_openai_generation(base_url, api_key, model, wire_api or "chat")
        return {
            "ok": True,
            "provider": provider,
            "provider_id": provider_id,
            "base_url": base_url,
            "model": model,
            "wire_api": wire_api or "chat",
            "has_api_key": True,
            "message": "Provider authentication and a small generation check succeeded.",
            "models": _merge_models(models, model),
        }
    except Exception as exc:
        status_code = _http_error_status(exc)
        return {
            "ok": False,
            "provider": provider,
            "provider_id": provider_id,
            "base_url": base_url,
            "model": model,
            "wire_api": wire_api or "chat",
            "has_api_key": True,
            "status_code": status_code,
            "message": _http_error_message(exc),
            "hint": _status_hint_for_provider(provider_id, status_code, True),
        }
