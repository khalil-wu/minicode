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
    _check_anthropic_generation,
    _check_openai_compatible_generation,
    _fetch_anthropic_models,
    _fetch_openai_compatible_models,
    _http_error_message,
    _http_error_status,
    _manual_models_from_payload,
    _merge_models,
    _normalize_provider_value,
    _persist_refreshed_models,
    _select_refreshed_model,
    _status_hint_for_provider,
)

logger = logging.getLogger(__name__)

FetchModels = Callable[[str, str], Awaitable[list[str]]]
CheckGeneration = Callable[[str, str, str, str], Awaitable[None]]
CheckAnthropicGeneration = Callable[[str, str, str], Awaitable[None]]
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
        provider_id = "anthropic"
        models: list[str] = []
        source = "manual"
        source_message = ""
        manual_models = _manual_models_from_payload(request.anthropic)
        try:
            models = await fetch_anthropic_models(base_url, api_key)
        except Exception as exc:
            logger.warning("Failed to refresh Anthropic model list: %s", exc)
        if models:
            source = "live"
            source_message = "Fetched available models from Anthropic /models."
        else:
            models = manual_models
            source_message = "Live model discovery unavailable; keeping the configured model list."
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
    custom_anthropic = provider == "custom" and wire_api == "anthropic"
    provider_id = "custom_anthropic" if custom_anthropic else provider
    models: list[str] = []
    discovered_reasoning_efforts: dict[str, list[str]] = {}
    source = "manual"
    source_message = ""
    manual_models = _manual_models_from_payload(incoming)
    # A wire protocol is not a model-vendor assertion. Custom Messages
    # gateways may expose arbitrary model ids, so never inject/filter Claude
    # models merely because wire_api=anthropic.
    try:
        if custom_anthropic:
            models = await fetch_anthropic_models(base_url, api_key)
        else:
            models = await fetch_openai_models(base_url, api_key)
            discovered_reasoning_efforts = dict(getattr(models, "reasoning_efforts", {}))
    except Exception as exc:
        logger.warning("Failed to refresh %s model list: %s", provider_id, exc)
    if models:
        source = "live"
        source_message = "Fetched models from the current provider /models endpoint."
    else:
        models = manual_models
        source_message = "Live model discovery unavailable; keeping the configured model list."
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
    check_anthropic_generation: CheckAnthropicGeneration = _check_anthropic_generation,
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
                "provider_id": "anthropic",
                "base_url": base_url,
                "model": model,
                "wire_api": "anthropic",
                "has_api_key": False,
                "message": "Missing Anthropic API key.",
                "hint": _status_hint_for_provider("anthropic", None, False),
            }
        try:
            try:
                models = await fetch_anthropic_models(base_url, api_key)
            except Exception as discovery_exc:
                logger.info("Anthropic model discovery unavailable: %s", discovery_exc)
                models = []
            await check_anthropic_generation(base_url, api_key, model)
            return {
                "ok": True,
                "provider": provider,
                "provider_id": "anthropic",
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
                "provider_id": "anthropic",
                "base_url": base_url,
                "model": model,
                "wire_api": "anthropic",
                "has_api_key": True,
                "status_code": status_code,
                "message": _http_error_message(exc),
                "hint": _status_hint_for_provider("anthropic", status_code, True),
            }

    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
    api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
    model = incoming.model.strip() or str(current.get("model", "")).strip()
    raw_wire_api = str(getattr(incoming, "wire_api", "") or current.get("wire_api", "") or "").strip().lower()
    wire_api = normalize_custom_wire_api(base_url, raw_wire_api, str(current.get("wire_api", "chat")))
    custom_anthropic = provider == "custom" and wire_api == "anthropic"
    provider_id = "custom_anthropic" if custom_anthropic else provider

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
        if custom_anthropic:
            # Model discovery is optional for third-party Messages gateways;
            # generation is the authoritative connectivity check.
            try:
                models = await fetch_anthropic_models(base_url, api_key)
            except Exception as discovery_exc:
                logger.info("Custom Anthropic model discovery unavailable: %s", discovery_exc)
                models = []
            await check_anthropic_generation(base_url, api_key, model)
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
