from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from backend.config import (
    SETTINGS_FILE,
    get_anthropic_settings,
    get_custom_settings,
    get_openai_settings,
    get_provider_model_metadata,
    normalize_custom_wire_api,
    resolve_provider_api_key_for_base_url,
    resolve_provider_image_api_key_for_base_url,
)
from backend.hooks.runtime import raise_if_config_change_blocked, run_config_change_hook
from backend.llm.capabilities import is_gpt_image_model
from backend.llm.reasoning_effort import normalize_reasoning_effort, reasoning_effort_levels
from backend.llm.proxy_policy import normalize_provider_proxy_mode

from backend.services.llm_provider_helpers import (
    _check_anthropic_generation,
    _check_openai_compatible_image_generation,
    _check_openai_compatible_generation,
    _fetch_anthropic_models,
    _fetch_openai_compatible_models,
    _http_error_message,
    _http_error_status,
    _is_network_error,
    _manual_models_from_payload,
    _merge_models,
    _normalize_provider_value,
    _persist_refreshed_models,
    _select_refreshed_model,
    _status_hint_for_provider,
)

logger = logging.getLogger(__name__)

FetchModels = Callable[..., Awaitable[list[str]]]
CheckGeneration = Callable[..., Awaitable[None]]
CheckAnthropicGeneration = Callable[..., Awaitable[None]]
CheckImageGeneration = Callable[..., Awaitable[None]]
ConfigChangeHook = Callable[..., Awaitable[Any]]


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


def _connection_failure_kind(
    status_code: int | None,
    exc: Exception | None = None,
) -> str:
    if status_code in {401, 403}:
        return "authentication_failed"
    if status_code == 404:
        return "model_or_endpoint_not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code is not None and status_code >= 500:
        return "provider_unavailable"
    if status_code is not None:
        return "generation_failed"
    return "network_error" if exc is not None and _is_network_error(exc) else "generation_failed"


def _connection_failure_retryable(failure_kind: str) -> bool:
    return failure_kind in {"rate_limited", "provider_unavailable", "network_error"}


def _image_connection_failure_kind(
    status_code: int | None,
    exc: Exception | None = None,
) -> str:
    if status_code in {401, 403}:
        return "authentication_failed"
    if status_code == 404:
        return "image_model_or_endpoint_not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code is not None and status_code >= 500:
        return "provider_unavailable"
    if status_code is not None:
        return "image_generation_failed"
    return (
        "network_error"
        if exc is not None and _is_network_error(exc)
        else "image_generation_failed"
    )


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


def _resolve_image_check_configuration(
    *,
    provider: str,
    incoming: Any,
    current: dict[str, Any],
    text_base_url: str,
    text_api_key: str,
    text_model: str,
    wire_api: str,
) -> dict[str, Any]:
    raw_mode = _request_text_value(
        incoming,
        current,
        "image_mode",
        default="inherit",
    ).lower()
    mode = raw_mode if raw_mode in {"disabled", "inherit", "custom"} else "inherit"
    model = _request_text_value(
        incoming,
        current,
        "image_model",
        blank_is_value=True,
    )
    if not model and is_gpt_image_model(text_model):
        model = text_model
    size = _request_text_value(
        incoming,
        current,
        "image_size",
        default="1024x1024",
    )
    if size not in {"auto", "1024x1024", "1536x1024", "1024x1536"}:
        size = "1024x1024"
    quality = _request_text_value(
        incoming,
        current,
        "image_quality",
        blank_is_value=True,
    ).lower()
    if quality not in {"", "auto", "low", "medium", "high", "standard", "hd"}:
        quality = ""

    if mode == "disabled":
        return {
            "mode": mode,
            "base_url": "",
            "api_key": "",
            "model": model,
            "size": size,
            "quality": quality,
            "reason": "Image generation is disabled for this provider profile.",
        }

    if mode == "custom":
        base_url = _request_text_value(
            incoming,
            current,
            "image_base_url",
            blank_is_value=True,
        )
        submitted_key = (
            str(getattr(incoming, "image_api_key", "") or "").strip()
            if _request_field_was_set(incoming, "image_api_key")
            else ""
        )
        api_key = submitted_key or resolve_provider_image_api_key_for_base_url(
            provider,
            base_url,
        )
        if not base_url:
            reason = "Independent image generation requires an image base URL."
        elif not model:
            reason = "Independent image generation requires an image model."
        else:
            reason = ""
    else:
        base_url = text_base_url
        api_key = text_api_key
        if wire_api not in {"chat", "responses"}:
            reason = (
                "The current provider uses Anthropic Messages; configure an "
                "independent OpenAI-compatible image channel."
            )
        elif not base_url:
            reason = "Inherited image generation requires the provider base URL."
        elif not model:
            reason = "Set an image model before using image generation."
        else:
            reason = ""

    return {
        "mode": mode,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "size": size,
        "quality": quality,
        "reason": reason,
    }


def _image_check_failure_payload(
    provider_id: str,
    *,
    model: str,
    exc: Exception | None = None,
    configuration_message: str = "",
) -> dict[str, Any]:
    if configuration_message:
        return {
            "image_generation_ok": False,
            "image_status_code": None,
            "image_failure_kind": "configuration_error",
            "image_retryable": False,
            "image_message": configuration_message,
            "image_hint": "Complete the image channel configuration and run the check again.",
            "image_model": model,
        }
    status_code = _http_error_status(exc) if exc is not None else None
    failure_kind = _image_connection_failure_kind(status_code, exc)
    hint = _status_hint_for_provider(provider_id, status_code, True)
    if failure_kind == "image_model_or_endpoint_not_found":
        hint = (
            "The image model or /images/generations endpoint was not found. "
            "Check the image Base URL and model name."
        )
    elif failure_kind == "network_error":
        hint = "The image provider could not be reached. Check its URL, proxy, DNS, and network."
    return {
        "image_generation_ok": False,
        "image_status_code": status_code,
        "image_failure_kind": failure_kind,
        "image_retryable": _connection_failure_retryable(failure_kind),
        "image_message": _http_error_message(exc) if exc is not None else "Image generation check failed.",
        "image_hint": hint,
        "image_model": model,
    }


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
        proxy_mode = _request_proxy_mode(request.anthropic, current)
        headers = _request_headers(request.anthropic, current)
        auth_header = _request_auth_header(request.anthropic, current)
        base_url = request.anthropic.base_url.strip() or current["base_url"]
        api_key = (
            request.anthropic.api_key.strip()
            or resolve_provider_api_key_for_base_url("anthropic", base_url)
        )
        current_model = request.anthropic.model.strip() or current["model"]
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
            else dict(
                getattr(request.anthropic, "model_metadata", {})
                or current.get("model_metadata", {})
                or {}
            )
        )
        config = None
        if source == "live":
            hook_result = await config_change_hook(
                source="llm",
                file_path=str(SETTINGS_FILE),
            )
            raise_if_config_change_blocked(
                hook_result,
                source="llm",
                file_path=str(SETTINGS_FILE),
            )
            config = _persist_refreshed_models(
                provider,
                final_models,
                selected_model,
                model_metadata,
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
        if config is not None:
            payload["_config"] = config
        return payload

    current = get_custom_settings() if provider == "custom" else get_openai_settings()
    incoming = request.custom if provider == "custom" else request.openai
    proxy_mode = _request_proxy_mode(incoming, current)
    headers = _request_headers(incoming, current)
    auth_header = _request_auth_header(incoming, current)
    base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
    api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
    current_model = incoming.model.strip() or str(current.get("model", "")).strip()
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
        else dict(
            getattr(incoming, "model_metadata", {})
            or current.get("model_metadata", {})
            or {}
        )
    )
    config = None
    if source == "live":
        hook_result = await config_change_hook(
            source="llm",
            file_path=str(SETTINGS_FILE),
        )
        raise_if_config_change_blocked(
            hook_result,
            source="llm",
            file_path=str(SETTINGS_FILE),
        )
        config = _persist_refreshed_models(
            provider,
            final_models,
            selected_model,
            model_metadata,
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
            wire_api="anthropic" if custom_anthropic else wire_api,
            live_refresh=source == "live",
        ),
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
    check_image_generation: CheckImageGeneration = _check_openai_compatible_image_generation,
) -> dict[str, Any]:
    provider = _normalize_provider_value(request.provider)

    if provider == "anthropic":
        current = get_anthropic_settings()
        incoming = request.anthropic
        proxy_mode = _request_proxy_mode(incoming, current)
        headers = _request_headers(incoming, current)
        auth_header = _request_auth_header(incoming, current)
        base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
        api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url("anthropic", base_url)
        model = incoming.model.strip() or str(current.get("model", "")).strip()
        wire_api = "anthropic"
        provider_id = "anthropic"
        fetch_models = fetch_anthropic_models
    else:
        current = get_custom_settings() if provider == "custom" else get_openai_settings()
        incoming = request.custom if provider == "custom" else request.openai
        proxy_mode = _request_proxy_mode(incoming, current)
        headers = _request_headers(incoming, current)
        auth_header = _request_auth_header(incoming, current)
        base_url = incoming.base_url.strip() or str(current.get("base_url", "")).strip()
        api_key = incoming.api_key.strip() or resolve_provider_api_key_for_base_url(provider, base_url)
        model = incoming.model.strip() or str(current.get("model", "")).strip()
        raw_wire_api = str(
            getattr(incoming, "wire_api", "")
            or current.get("wire_api", "")
            or ""
        ).strip().lower()
        wire_api = normalize_custom_wire_api(
            base_url,
            raw_wire_api,
            str(current.get("wire_api", "chat")),
        ) or "chat"
        custom_anthropic = provider == "custom" and wire_api == "anthropic"
        provider_id = "custom_anthropic" if custom_anthropic else provider
        fetch_models = fetch_anthropic_models if custom_anthropic else fetch_openai_models

    image_config = _resolve_image_check_configuration(
        provider=provider,
        incoming=incoming,
        current=current,
        text_base_url=base_url,
        text_api_key=api_key,
        text_model=model,
        wire_api=wire_api,
    )
    dedicated_image_model = provider != "anthropic" and is_gpt_image_model(model)
    if dedicated_image_model:
        # A custom profile may keep ``wire_api=anthropic`` for text models but
        # explicitly route a dedicated image model through an independent
        # OpenAI-compatible Images endpoint. Discovery must follow the actual
        # image endpoint instead of probing the unrelated Messages transport.
        fetch_models = fetch_openai_models
    generation_base_url = (
        str(image_config["base_url"])
        if dedicated_image_model
        else base_url
    )
    generation_api_key = (
        str(image_config["api_key"])
        if dedicated_image_model
        else api_key
    )
    generation_model = (
        str(image_config["model"])
        if dedicated_image_model
        else model
    )

    base_payload = {
        "provider": provider,
        "provider_id": provider_id,
        "base_url": generation_base_url if dedicated_image_model else base_url,
        # Preserve the user-selected main model.  The independent Images API
        # wire model is reported separately in ``image_model`` and must never
        # replace the capability-routing identity (for example gpt-image-*).
        "model": model,
        "wire_api": "images" if dedicated_image_model else wire_api,
        "proxy_mode": proxy_mode,
        "generation_kind": "image" if dedicated_image_model else "text",
        "has_api_key": bool(generation_api_key.strip()),
    }

    def response_models(discovered: list[str]) -> list[str]:
        if dedicated_image_model:
            # Image endpoint model ids may be relay-specific aliases such as
            # flux-*.  They belong in image_model, not in the main chat model
            # picker where selecting one would disable Images API routing.
            return _merge_models([], model)
        return _merge_models(discovered, model)

    if dedicated_image_model and str(image_config.get("reason") or ""):
        missing_fields = [str(image_config["reason"])]
    else:
        requires_base_url = provider != "anthropic"
        missing_fields = [
            field
            for field, value, required in (
                ("base URL", generation_base_url, requires_base_url),
                (
                    "API key required by auth_header=true",
                    generation_api_key,
                    auth_header,
                ),
                ("model", generation_model, True),
            )
            if required and not str(value or "").strip()
        ]
    if missing_fields:
        has_api_key = bool(generation_api_key.strip())
        configuration_message = (
            missing_fields[0]
            if dedicated_image_model
            else f"Missing provider configuration: {', '.join(missing_fields)}."
        )
        image_payload = (
            _image_check_failure_payload(
                provider_id,
                model=generation_model,
                configuration_message=configuration_message,
            )
            if dedicated_image_model
            else {
                "image_generation_ok": None,
                "image_status_code": None,
                "image_failure_kind": "",
                "image_retryable": False,
                "image_message": "",
                "image_hint": "",
                "image_model": str(image_config.get("model") or ""),
            }
        )
        return {
            **base_payload,
            "ok": False,
            "model_discovery_ok": None,
            "generation_ok": None,
            "failure_kind": "configuration_error",
            "retryable": False,
            "message": configuration_message,
            "hint": (
                _status_hint_for_provider(provider_id, None, False)
                if not has_api_key
                else "Complete the Base URL and model configuration before checking the connection."
            ),
            **image_payload,
            "models": response_models([]),
        }

    models: list[str] = []
    model_discovery_ok = False
    discovery_failure: dict[str, Any] | None = None
    try:
        models = await _call_provider_helper(
            fetch_models,
            generation_base_url,
            generation_api_key,
            proxy_mode=proxy_mode,
            headers=headers,
            auth_header=auth_header,
        )
        model_discovery_ok = bool(models)
        if not models:
            discovery_failure = _model_discovery_failure(
                provider_id,
                empty=True,
            )
            logger.info("%s model discovery returned an empty list", provider_id)
    except Exception as discovery_exc:
        discovery_failure = _model_discovery_failure(provider_id, exc=discovery_exc)
        logger.info(
            "%s model discovery unavailable kind=%s status=%s",
            provider_id,
            discovery_failure["failure_kind"],
            discovery_failure["status_code"],
        )

    discovery_evidence = {
        "model_discovery_status_code": (
            discovery_failure["status_code"] if discovery_failure else None
        ),
        "model_discovery_failure_kind": (
            discovery_failure["failure_kind"] if discovery_failure else ""
        ),
        "model_discovery_retryable": (
            discovery_failure["retryable"] if discovery_failure else False
        ),
        "model_discovery_message": (
            discovery_failure["message"] if discovery_failure else ""
        ),
        "model_discovery_hint": (
            discovery_failure["hint"] if discovery_failure else ""
        ),
    }

    generation_exc: Exception | None = None
    try:
        if dedicated_image_model:
            await _call_provider_helper(
                check_image_generation,
                generation_base_url,
                generation_api_key,
                generation_model,
                str(image_config.get("size") or "1024x1024"),
                str(image_config.get("quality") or ""),
                proxy_mode=proxy_mode,
                headers=headers,
                auth_header=auth_header,
            )
        elif provider == "anthropic" or (provider == "custom" and wire_api == "anthropic"):
            await _call_provider_helper(
                check_anthropic_generation,
                base_url,
                api_key,
                model,
                proxy_mode=proxy_mode,
                headers=headers,
                auth_header=auth_header,
            )
        else:
            await _call_provider_helper(
                check_openai_generation,
                base_url,
                api_key,
                model,
                wire_api,
                proxy_mode=proxy_mode,
                headers=headers,
                auth_header=auth_header,
            )
    except Exception as exc:
        generation_exc = exc

    if dedicated_image_model:
        if generation_exc is None:
            image_payload = {
                "image_generation_ok": True,
                "image_status_code": None,
                "image_failure_kind": "",
                "image_retryable": False,
                "image_message": "Image generation check succeeded.",
                "image_hint": "",
                "image_model": generation_model,
            }
        else:
            image_payload = _image_check_failure_payload(
                provider_id,
                model=generation_model,
                exc=generation_exc,
            )
    elif image_config["mode"] == "disabled" or not str(image_config.get("model") or ""):
        image_payload = {
            "image_generation_ok": None,
            "image_status_code": None,
            "image_failure_kind": "",
            "image_retryable": False,
            "image_message": "",
            "image_hint": "",
            "image_model": str(image_config.get("model") or ""),
        }
    elif str(image_config.get("reason") or ""):
        image_payload = _image_check_failure_payload(
            provider_id,
            model=str(image_config.get("model") or ""),
            configuration_message=str(image_config["reason"]),
        )
    else:
        try:
            await _call_provider_helper(
                check_image_generation,
                str(image_config["base_url"]),
                str(image_config["api_key"]),
                str(image_config["model"]),
                str(image_config.get("size") or "1024x1024"),
                str(image_config.get("quality") or ""),
                proxy_mode=proxy_mode,
                headers=headers,
                auth_header=auth_header,
            )
        except Exception as image_exc:
            image_payload = _image_check_failure_payload(
                provider_id,
                model=str(image_config["model"]),
                exc=image_exc,
            )
        else:
            image_payload = {
                "image_generation_ok": True,
                "image_status_code": None,
                "image_failure_kind": "",
                "image_retryable": False,
                "image_message": "Image generation check succeeded.",
                "image_hint": "",
                "image_model": str(image_config["model"]),
            }

    if generation_exc is not None:
        status_code = _http_error_status(generation_exc)
        failure_kind = _connection_failure_kind(status_code, generation_exc)
        retryable = _connection_failure_retryable(failure_kind)
        hint = _status_hint_for_provider(provider_id, status_code, True)
        if model_discovery_ok and failure_kind == "provider_unavailable":
            hint = (
                "The model list endpoint accepted the current credentials, but generation is "
                "temporarily unavailable. This does not mean the API key was rejected."
            )
        return {
            **base_payload,
            "ok": False,
            "status_code": status_code,
            "model_discovery_ok": model_discovery_ok,
            "generation_ok": False,
            **discovery_evidence,
            "failure_kind": failure_kind,
            "retryable": retryable,
            "message": _http_error_message(generation_exc),
            "hint": hint,
            **image_payload,
            "models": response_models(models),
        }

    image_failed = image_payload["image_generation_ok"] is False
    if dedicated_image_model:
        success_message = "Provider image generation check succeeded."
    elif image_payload["image_generation_ok"] is True:
        success_message = "Provider text generation and image channel checks succeeded."
    elif image_failed:
        success_message = "Provider text generation succeeded, but the image channel check failed."
    elif model_discovery_ok:
        success_message = "Provider connection and a small generation check succeeded."
    else:
        success_message = "Generation succeeded; the optional model list endpoint was unavailable."

    return {
        **base_payload,
        "ok": True,
        "model_discovery_ok": model_discovery_ok,
        "generation_ok": True,
        **discovery_evidence,
        "failure_kind": "",
        "retryable": False,
        "message": success_message,
        "hint": (
            ""
            if model_discovery_ok
            else "The selected model is usable even though live model discovery did not complete."
        ),
        **image_payload,
        "models": response_models(models),
    }
