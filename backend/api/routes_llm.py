"""LLM settings and MCP configuration routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from backend.config import (
    get_anthropic_settings,
    get_custom_settings,
    get_llm_settings_payload,
    get_openai_settings,
    load_config,
    normalize_custom_wire_api,
    resolve_provider_api_key_for_base_url,
    save_llm_settings,
)
from backend.mcp.config_file import MCP_CONFIG_FILE, read_mcp_config, write_mcp_config

from . import _state
from .llm_helpers import (
    _CUSTOM_GATEWAY_FALLBACK_MODELS,
    _LATEST_PROVIDER_MODELS,
    _fetch_anthropic_models,
    _check_openai_compatible_generation,
    _fetch_openai_compatible_models,
    _http_error_message,
    _http_error_status,
    _merge_models,
    _merge_model_sources,
    _normalize_provider_value,
    _persist_refreshed_models,
    _resolve_openai_provider_id,
    _select_refreshed_model,
    _status_hint_for_provider,
    _manual_models_from_payload,
    _is_anthropic_model_id,
)
from .models import (
    LLMCheckResponse,
    LLMModelsRefreshResponse,
    LLMSettingsUpdateRequest,
    MCPConfigUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── LLM settings ──


@router.get("/api/llm/settings")
async def get_llm_settings_api(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=30"
    return get_llm_settings_payload()


@router.put("/api/llm/settings")
async def update_llm_settings_api(request: LLMSettingsUpdateRequest) -> dict[str, Any]:
    if not request.confirm_sensitive_change:
        raise HTTPException(
            status_code=409,
            detail="LLM settings changes require explicit confirmation.",
        )
    saved = save_llm_settings(request.model_dump())
    if _state.bootstrap is not None:
        _state.bootstrap.config = load_config()
    return saved


# ── LLM model refresh / check ──


@router.post("/api/llm/models/refresh", response_model=LLMModelsRefreshResponse)
async def refresh_llm_models_api(request: LLMSettingsUpdateRequest) -> LLMModelsRefreshResponse:
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
            models = await _fetch_anthropic_models(base_url, api_key)
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
        return LLMModelsRefreshResponse(
            provider=provider,
            provider_id=provider_id,
            models=final_models,
            selected_model=selected_model,
            source=source,
            source_message=source_message,
        )

    provider_key = "custom" if provider == "custom" else "openai"
    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
    api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
    current_model = incoming.model.strip() or str(current.get("model", "")).strip()
    raw_wire_api = str(getattr(incoming, "wire_api", "") or current.get("wire_api", "") or "").strip().lower()
    wire_api = normalize_custom_wire_api(base_url, raw_wire_api, str(current.get("wire_api", "chat")))
    provider_id = "custom_anthropic" if provider == "custom" and wire_api == "anthropic" else _resolve_openai_provider_id(base_url)
    models = []
    source = "preset"
    source_message = ""
    manual_models = _manual_models_from_payload(incoming)
    if provider_id == "custom_anthropic":
        manual_models = [model for model in manual_models if _is_anthropic_model_id(model)]
    preset_models = list(_LATEST_PROVIDER_MODELS.get(provider_id, _LATEST_PROVIDER_MODELS.get("anthropic_off", []) if provider_id == "custom_anthropic" else []))
    custom_fallback_models = (
        list(_CUSTOM_GATEWAY_FALLBACK_MODELS)
        if provider == "custom" and provider_id == "custom_openai"
        else []
    )
    try:
        if provider_id == "custom_anthropic":
            models = await _fetch_anthropic_models(base_url, api_key)
        else:
            models = await _fetch_openai_compatible_models(base_url, api_key)
    except Exception as exc:
        logger.warning("Failed to refresh %s model list: %s", provider_id, exc)
    if models:
        source = "live"
        models = _merge_model_sources(models, manual_models)
        source_message = "Fetched models from the current provider /models endpoint and prioritized the latest releases."
    else:
        models = _merge_model_sources(preset_models, manual_models, custom_fallback_models)
        if not api_key.strip():
            source_message = "No API key is configured, showing built-in model list."
        elif preset_models:
            source_message = "Live model refresh failed or returned no models, showing built-in model list."
        else:
            source_message = "Live model refresh failed or returned no models, keeping manual model list."
    selected_model = _select_refreshed_model(provider_id, models, current_model)
    final_models = _merge_models(models, selected_model)
    if source == "live":
        _persist_refreshed_models(provider, final_models, selected_model)
    return LLMModelsRefreshResponse(
        provider=provider,
        provider_id=provider_id,
        models=final_models,
        selected_model=selected_model,
        source=source,
        source_message=source_message,
    )


@router.post("/api/llm/check", response_model=LLMCheckResponse)
async def check_llm_connection_api(request: LLMSettingsUpdateRequest) -> LLMCheckResponse:
    provider = _normalize_provider_value(request.provider)

    if provider == "anthropic":
        current = get_anthropic_settings()
        api_key = request.anthropic.api_key.strip() or current["api_key"]
        base_url = request.anthropic.base_url.strip() or current["base_url"]
        model = request.anthropic.model.strip() or current["model"]
        if not api_key.strip():
            return LLMCheckResponse(
                ok=False,
                provider=provider,
                provider_id="anthropic_off",
                base_url=base_url,
                model=model,
                wire_api="anthropic",
                has_api_key=False,
                message="Missing Anthropic API key.",
                hint=_status_hint_for_provider("anthropic_off", None, False),
            )
        try:
            models = await _fetch_anthropic_models(base_url, api_key)
            return LLMCheckResponse(
                ok=True,
                provider=provider,
                provider_id="anthropic_off",
                base_url=base_url,
                model=model,
                wire_api="anthropic",
                has_api_key=True,
                message="Anthropic authentication succeeded.",
                models=_merge_models(models, model),
            )
        except Exception as exc:
            status_code = _http_error_status(exc)
            return LLMCheckResponse(
                ok=False,
                provider=provider,
                provider_id="anthropic_off",
                base_url=base_url,
                model=model,
                wire_api="anthropic",
                has_api_key=True,
                status_code=status_code,
                message=_http_error_message(exc),
                hint=_status_hint_for_provider("anthropic_off", status_code, True),
            )

    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
    api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
    model = incoming.model.strip() or str(current.get("model", "")).strip()
    raw_wire_api = str(getattr(incoming, "wire_api", "") or current.get("wire_api", "") or "").strip().lower()
    wire_api = normalize_custom_wire_api(base_url, raw_wire_api, str(current.get("wire_api", "chat")))
    provider_id = "custom_anthropic" if provider == "custom" and wire_api == "anthropic" else _resolve_openai_provider_id(base_url)

    if not api_key.strip():
        return LLMCheckResponse(
            ok=False,
            provider=provider,
            provider_id=provider_id,
            base_url=base_url,
            model=model,
            wire_api=wire_api or "chat",
            has_api_key=False,
            message="Missing API key for current provider.",
            hint=_status_hint_for_provider(provider_id, None, False),
        )

    try:
        if provider_id == "custom_anthropic":
            models = await _fetch_anthropic_models(base_url, api_key)
        else:
            models = await _fetch_openai_compatible_models(base_url, api_key)
            await _check_openai_compatible_generation(base_url, api_key, model, wire_api or "chat")
        return LLMCheckResponse(
            ok=True,
            provider=provider,
            provider_id=provider_id,
            base_url=base_url,
            model=model,
            wire_api=wire_api or "chat",
            has_api_key=True,
            message="Provider authentication and generation succeeded.",
            models=_merge_models(models, model),
        )
    except Exception as exc:
        status_code = _http_error_status(exc)
        return LLMCheckResponse(
            ok=False,
            provider=provider,
            provider_id=provider_id,
            base_url=base_url,
            model=model,
            wire_api=wire_api or "chat",
            has_api_key=True,
            status_code=status_code,
            message=_http_error_message(exc),
            hint=_status_hint_for_provider(provider_id, status_code, True),
        )


# ── MCP configuration ──


@router.get("/api/mcp/config")
async def get_mcp_config_api(response: Response) -> dict[str, Any]:
    """Read the local .mcp.json file for the Settings center."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return read_mcp_config(MCP_CONFIG_FILE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read MCP config: {exc}") from exc


@router.put("/api/mcp/config")
async def update_mcp_config_api(request: MCPConfigUpdateRequest, response: Response) -> dict[str, Any]:
    """Validate, backup, save, and optionally reload the local .mcp.json file."""
    response.headers["Cache-Control"] = "no-store"
    if not request.confirm_sensitive_change:
        raise HTTPException(
            status_code=409,
            detail="MCP config changes require explicit confirmation.",
        )

    try:
        result = write_mcp_config(request.content, MCP_CONFIG_FILE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save MCP config: {exc}") from exc

    _state.invalidate_status_cache()

    if request.reload and _state.bootstrap is not None and _state.bootstrap.mcp_manager is not None:
        await _state.bootstrap.mcp_manager.stop_all()
        await _state.bootstrap.mcp_manager.start_all()

    return {
        **result,
        "mcp": get_mcp_status(),
    }


# Local helper to avoid circular import with routes_health
def get_mcp_status() -> list[dict[str, Any]]:
    """Return MCP server status (local copy for MCP config response)."""
    if _state.bootstrap is not None:
        return _state.bootstrap.get_mcp_status()
    return []
