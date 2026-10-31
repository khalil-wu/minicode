from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from backend.config import (
    get_anthropic_settings,
    get_custom_settings,
    get_openai_settings,
    get_provider_model_metadata,
    normalize_custom_wire_api,
    resolve_provider_api_key_for_base_url,
)
from backend.llm.reasoning_effort import normalize_reasoning_effort, reasoning_effort_levels
from backend.llm.proxy_policy import normalize_provider_proxy_mode
from backend.application_network import ApplicationNetworkDenied

from backend.services.llm_provider_helpers import (
    _fetch_anthropic_models,
    _fetch_openai_compatible_models,
    _http_error_message,
    _http_error_status,
    _is_network_error,
    _manual_models_from_payload,
    _merge_models,
    _normalize_provider_value,
    _select_refreshed_model,
    _status_hint_for_provider,
)

logger = logging.getLogger(__name__)

FetchModels = Callable[..., Awaitable[list[str]]]


async def _call_provider_helper(
    callback: Callable[..., Awaitable[Any]],
    *args: Any,
    proxy_mode: str,
    headers: Mapping[str, str],
    auth_header: bool,
) -> Any:
    return await callback(
        *args,
        proxy_mode=proxy_mode,
        headers=headers,
        auth_header=auth_header,
    )


def _request_proxy_mode(incoming: Any, current: dict[str, Any]) -> str:
    """Resolve an explicitly submitted mode without erasing saved profiles.

    Pydantic fills omitted nested fields with their schema defaults. Older
    clients therefore appear to submit ``inherit`` even when the saved profile
    is ``direct`` unless we inspect the field-set metadata first. Plain test
    doubles and embedders without Pydantic metadata retain the established
    attribute-or-current behavior.
    """

    fields_set = getattr(incoming, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(incoming, "__fields_set__", None)
    if fields_set is not None:
        raw_mode = (
            getattr(incoming, "proxy_mode", None)
            if "proxy_mode" in fields_set
            else current.get("proxy_mode")
        )
    else:
        raw_mode = getattr(incoming, "proxy_mode", None) or current.get("proxy_mode")
    return normalize_provider_proxy_mode(raw_mode)




def _connection_failure_retryable(failure_kind: str) -> bool:
    return failure_kind in {"rate_limited", "provider_unavailable", "network_error"}




def _request_field_was_set(incoming: Any, name: str) -> bool:
    """Return whether a request explicitly supplied one field.

    Pydantic payload objects expose every default as an attribute, so checking
    ``hasattr`` alone would make an older client silently reset saved image
    settings to the schema defaults.  SimpleNamespace-based tests do not have
    a fields-set marker and naturally fall back to attribute presence.
    """

    fields_set = getattr(incoming, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(incoming, "__fields_set__", None)
    if fields_set is not None:
        return name in fields_set
    return hasattr(incoming, name)


def _request_text_value(
    incoming: Any,
    current: dict[str, Any],
    name: str,
    *,
    default: str = "",
    blank_is_value: bool = False,
) -> str:
    if _request_field_was_set(incoming, name):
        value = str(getattr(incoming, name, "") or "").strip()
        if value or blank_is_value:
            return value
    return str(current.get(name, default) or default).strip()


def _request_headers(incoming: Any, current: dict[str, Any]) -> dict[str, str]:
    request_supplied = _request_field_was_set(incoming, "headers")
    raw = getattr(incoming, "headers") if request_supplied else current.get("default_headers", {})
    if raw is None:
        return {}
    if request_supplied and not isinstance(raw, Mapping):
        raise ValueError("Provider headers must be an object")
    if not request_supplied:
        if isinstance(raw, Mapping):
            return {str(name): str(value) for name, value in raw.items()}
        if isinstance(raw, tuple):
            return {str(name): str(value) for name, value in raw}
        raise ValueError("Saved provider headers must be an object or header pairs")
    return {str(name): str(value) for name, value in raw.items()}


def _request_auth_header(incoming: Any, current: dict[str, Any]) -> bool:
    return bool(
        getattr(incoming, "auth_header")
        if _request_field_was_set(incoming, "auth_header")
        else current.get("auth_header", False)
    )






def _model_discovery_failure(
    provider_id: str,
    *,
    exc: Exception | None = None,
    missing_fields: list[str] | None = None,
    empty: bool = False,
) -> dict[str, Any]:
    """Build one redacted, retry-aware model-discovery result contract."""

    missing = [field for field in (missing_fields or []) if field]
    if missing:
        return {
            "status_code": None,
            "failure_kind": "configuration_error",
            "retryable": False,
            "message": f"Missing provider configuration: {', '.join(missing)}.",
            "hint": "Complete the listed provider configuration before discovering models.",
        }
    if empty:
        return {
            "status_code": None,
            "failure_kind": "model_list_empty",
            "retryable": True,
            "message": "The provider model-list endpoint returned no models.",
            "hint": "Keep the manually configured model or retry model discovery later.",
        }

    status_code = _http_error_status(exc) if exc is not None else None
    if isinstance(exc, ApplicationNetworkDenied):
        return {"status_code": None, "failure_kind": "application_network_policy", "retryable": False,
            "message": _http_error_message(exc), "hint": "The managed application network policy must permit this operation."}
    if status_code in {401, 403}:
        failure_kind = "authentication_failed"
    elif status_code == 404:
        failure_kind = "models_endpoint_not_found"
    elif status_code == 429:
        failure_kind = "rate_limited"
    elif status_code is not None and status_code >= 500:
        failure_kind = "provider_unavailable"
    elif status_code is None and exc is not None and _is_network_error(exc):
        failure_kind = "network_error"
    else:
        failure_kind = "model_discovery_failed"
    hint = _status_hint_for_provider(provider_id, status_code, True)
    if failure_kind == "models_endpoint_not_found":
        hint = "The provider does not expose a compatible /models endpoint. Keep the manual model or correct the Base URL."
    elif failure_kind == "network_error":
        hint = "The provider could not be reached. Check the Base URL, proxy, DNS, and network, then retry."
    return {
        "status_code": status_code,
        "failure_kind": failure_kind,
        "retryable": _connection_failure_retryable(failure_kind),
        "message": _http_error_message(exc) if exc is not None else "Model discovery failed.",
        "hint": hint,
    }


def _selected_model_capability_payload(
    section: dict[str, Any],
    *,
    selected_model: str,
    model_metadata: dict[str, dict[str, Any]],
    wire_api: str,
    live_refresh: bool,
) -> dict[str, Any]:
    projected_section = dict(section)
    projected_section["model"] = selected_model
    projected_section["model_metadata"] = model_metadata
    if live_refresh:
        # The live catalog is authoritative even when it declares no
        # reasoning levels. Do not fall back to a stale compatibility field.
        projected_section["reasoning_effort_levels"] = []
    resolved = get_provider_model_metadata(projected_section, selected_model)
    levels = list(
        reasoning_effort_levels(
            selected_model,
            wire_api,
            resolved["reasoning_effort_levels"],
        )
    )
    configured = str(section.get("reasoning_effort") or "").strip().lower()
    effort_supported = wire_api != "anthropic" and bool(levels)
    effective = (
        normalize_reasoning_effort(
            selected_model,
            wire_api,
            configured,
            levels,
            resolved["default_reasoning_effort"],
        )
        if effort_supported
        else ""
    )
    return {
        "model_metadata": model_metadata,
        "reasoning_effort_levels": levels if wire_api != "anthropic" else [],
        "configured_reasoning_effort": configured if wire_api != "anthropic" else "",
        "effective_reasoning_effort": effective,
        "reasoning_effort_supported": effort_supported,
        "context_window": resolved["context_window"],
        "context_window_source": resolved["context_window_source"],
        "context_window_verified": resolved["context_window_verified"],
        "max_context_window": resolved["max_context_window"],
        "max_context_window_source": resolved["max_context_window_source"],
        "max_context_window_verified": resolved["max_context_window_verified"],
        "max_output_tokens": resolved["max_output_tokens"],
        "max_output_tokens_source": resolved["max_output_tokens_source"],
        "max_output_tokens_verified": resolved["max_output_tokens_verified"],
        "default_reasoning_effort": resolved["default_reasoning_effort"],
        "default_reasoning_summary": resolved["default_reasoning_summary"],
    }


def _retain_model_behavior(
    discovered: dict[str, dict[str, Any]], *, current: dict[str, Any], incoming: Any,
    base_url: str, wire_api: str, models: list[str],
) -> dict[str, dict[str, Any]]:
    """Keep explicit model behavior when refreshing the same provider draft."""
    if _request_field_was_set(incoming, "model_metadata"):
        configured = incoming.model_metadata
    elif (base_url.rstrip("/") == str(current.get("base_url") or "").rstrip("/")
          and wire_api == str(current.get("wire_api") or wire_api)):
        configured = current.get("model_metadata", {})
    else:
        configured = {}
    merged = {name: dict(value) for name, value in discovered.items()}
    for name in models:
        behavior = {key: value for key, value in configured.get(name, {}).items()
                    if key in {"supports_custom_tools", "supports_hosted_web_search", "model_instructions", "responses_websocket", "native_compaction"}}
        if behavior:
            merged[name] = {**merged.get(name, {}), **behavior}
    return merged


async def refresh_llm_models(
    request: Any,
    *,
    fetch_anthropic_models: FetchModels = _fetch_anthropic_models,
    fetch_openai_models: FetchModels = _fetch_openai_compatible_models,
) -> dict[str, Any]:
    """Discover candidates for a provider draft without changing saved settings."""
    provider = _normalize_provider_value(request.provider)

    if provider == "anthropic":
        current = get_anthropic_settings()
        proxy_mode = _request_proxy_mode(request.anthropic, current)
        headers = _request_headers(request.anthropic, current)
        auth_header = _request_auth_header(request.anthropic, current)
        base_url = _request_text_value(request.anthropic, current, "base_url", blank_is_value=True)
        api_key = (
            request.anthropic.api_key.strip()
            or resolve_provider_api_key_for_base_url("anthropic", base_url)
        )
        current_model = _request_text_value(request.anthropic, current, "model", blank_is_value=True)
        provider_id = "anthropic"
        models: list[str] = []
        discovered_model_metadata: dict[str, dict[str, Any]] = {}
        source = "manual"
        source_message = ""
        manual_models = _manual_models_from_payload(request.anthropic)
        discovery_failure: dict[str, Any] = {}
        if auth_header and not api_key.strip():
            discovery_failure = _model_discovery_failure(
                provider_id,
                missing_fields=["API key required by auth_header=true"],
            )
        else:
            try:
                models = await _call_provider_helper(
                    fetch_anthropic_models,
                    base_url,
                    api_key,
                    proxy_mode=proxy_mode,
                    headers=headers,
                    auth_header=auth_header,
                )
                discovered_model_metadata = dict(
                    getattr(models, "model_metadata", {}) or {}
                )
                if not models:
                    discovery_failure = _model_discovery_failure(
                        provider_id,
                        empty=True,
                    )
            except Exception as exc:
                discovery_failure = _model_discovery_failure(provider_id, exc=exc)
                logger.info(
                    "Anthropic model discovery failed kind=%s status=%s",
                    discovery_failure["failure_kind"],
                    discovery_failure["status_code"],
                )
        if models:
            source = "live"
            source_message = "Fetched available models from Anthropic /models."
        else:
            models = manual_models
            source_message = discovery_failure.get(
                "message",
                "Live model discovery unavailable; keeping the configured model list.",
            )
        selected_model = _select_refreshed_model(provider_id, models, current_model)
        final_models = _merge_models(models, selected_model)
        model_metadata = (
            discovered_model_metadata
            if source == "live"
            else dict(request.anthropic.model_metadata
                      if _request_field_was_set(request.anthropic, "model_metadata")
                      else current.get("model_metadata", {}))
        )
        if source == "live":
            model_metadata = _retain_model_behavior(
                model_metadata, current=current, incoming=request.anthropic,
                base_url=base_url, wire_api="anthropic", models=final_models,
            )
        payload = {
            "provider": provider,
            "provider_id": provider_id,
            "models": final_models,
            "selected_model": selected_model,
            "proxy_mode": proxy_mode,
            "source": source,
            "source_message": source_message,
            **discovery_failure,
            **_selected_model_capability_payload(
                current,
                selected_model=selected_model,
                model_metadata=model_metadata,
                wire_api="anthropic",
                live_refresh=source == "live",
            ),
        }
        return payload

    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    proxy_mode = _request_proxy_mode(incoming, current)
    headers = _request_headers(incoming, current)
    auth_header = _request_auth_header(incoming, current)
    base_url = _request_text_value(incoming, current, "base_url", blank_is_value=True)
    api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
    current_model = _request_text_value(incoming, current, "model", blank_is_value=True)
    raw_wire_api = str(getattr(incoming, "wire_api", "") or current.get("wire_api", "") or "").strip().lower()
    wire_api = normalize_custom_wire_api(base_url, raw_wire_api, str(current.get("wire_api", "chat")))
    custom_anthropic = provider == "custom" and wire_api == "anthropic"
    provider_id = "custom_anthropic" if custom_anthropic else provider
    models: list[str] = []
    discovered_model_metadata: dict[str, dict[str, Any]] = {}
    source = "manual"
    source_message = ""
    manual_models = _manual_models_from_payload(incoming)
    discovery_failure: dict[str, Any] = {}
    # A wire protocol is not a model-vendor assertion. Custom Messages
    # gateways may expose arbitrary model ids, so never inject/filter Claude
    # models merely because wire_api=anthropic.
    missing_fields = [
        field
        for field, missing in (
            ("base URL", not base_url),
            ("API key required by auth_header=true", auth_header and not api_key),
        )
        if missing
    ]
    if missing_fields:
        discovery_failure = _model_discovery_failure(
            provider_id,
            missing_fields=missing_fields,
        )
    else:
        try:
            if custom_anthropic:
                models = await _call_provider_helper(
                    fetch_anthropic_models,
                    base_url,
                    api_key,
                    proxy_mode=proxy_mode,
                    headers=headers,
                    auth_header=auth_header,
                )
            else:
                models = await _call_provider_helper(
                    fetch_openai_models,
                    base_url,
                    api_key,
                    proxy_mode=proxy_mode,
                    headers=headers,
                    auth_header=auth_header,
                )
            discovered_model_metadata = dict(
                getattr(models, "model_metadata", {}) or {}
            )
            if not models:
                discovery_failure = _model_discovery_failure(
                    provider_id,
                    empty=True,
                )
        except Exception as exc:
            discovery_failure = _model_discovery_failure(provider_id, exc=exc)
            logger.info(
                "%s model discovery failed kind=%s status=%s",
                provider_id,
                discovery_failure["failure_kind"],
                discovery_failure["status_code"],
            )
    if models:
        source = "live"
        source_message = "Fetched models from the current provider /models endpoint."
    else:
        models = manual_models
        source_message = discovery_failure.get(
            "message",
            "Live model discovery unavailable; keeping the configured model list.",
        )
    selected_model = _select_refreshed_model(provider_id, models, current_model)
    final_models = _merge_models(models, selected_model)
    model_metadata = (
        discovered_model_metadata
        if source == "live"
        else dict(incoming.model_metadata
                  if _request_field_was_set(incoming, "model_metadata")
                  else current.get("model_metadata", {}))
    )
    if source == "live":
        model_metadata = _retain_model_behavior(
            model_metadata, current=current, incoming=incoming,
            base_url=base_url, wire_api=wire_api, models=final_models,
        )
    payload = {
        "provider": provider,
        "provider_id": provider_id,
        "models": final_models,
        "selected_model": selected_model,
        "proxy_mode": proxy_mode,
        "source": source,
        "source_message": source_message,
        **discovery_failure,
        **_selected_model_capability_payload(
            {**current, "reasoning_effort": _request_text_value(
                incoming, current, "reasoning_effort", blank_is_value=True,
            )},
            selected_model=selected_model,
            model_metadata=model_metadata,
            wire_api="anthropic" if custom_anthropic else wire_api,
            live_refresh=source == "live",
        ),
    }
    return payload
