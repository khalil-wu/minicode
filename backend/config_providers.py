"""Provider settings writers and model-list helpers.

Extracted from ``backend/config.py``; depends on :mod:`backend.config_helpers`.
"""

from __future__ import annotations

from typing import (
    Any,
    Mapping,
)
from urllib.parse import urlsplit
import json
import logging
import os
import time

from backend.config_helpers import (
    MINICODE_CAPPED_DEFAULT_MAX_TOKENS,
    SettingsError,
    _RUNTIME_API_KEY_SCOPES,
    _RUNTIME_IMAGE_API_KEY_SCOPES,
    _coerce_int,
    _coerce_model_labels,
    _coerce_model_list,
    _coerce_model_metadata,
    _history_identity,
    _history_profile_identity,
    _image_api_key_for_base_url,
    _image_scoped_vault_names,
    _is_api_key_replacement,
    _llm_history,
    _load_settings_json,
    _normalize_image_mode,
    _normalize_image_quality,
    _normalize_image_size,
    _normalize_openai_base_url,
    _normalize_prompt_cache_retention,
    _normalize_provider,
    _normalize_proxy_mode,
    _provider_api_key_for_base_url,
    _provider_display_name,
    _provider_id_for_history,
    _provider_key_scope,
    _responses_prompt_cache_retention_default,
    _scoped_vault_names,
    _select_custom_model,
    _serialized_settings_update,
    _write_settings_json,
    get_anthropic_settings,
    get_custom_settings,
    get_llm_provider,
    get_llm_settings_payload,
    get_openai_settings,
    get_provider_model_metadata,
    normalize_custom_wire_api,
)


logger = logging.getLogger(__name__)


def _set_runtime_image_api_key(provider: str, api_key: str, base_url: str) -> None:
    """Persist an independent Images API key without replacing the text key."""

    if not api_key:
        return
    scoped_names = _image_scoped_vault_names(provider, base_url)
    if not scoped_names:
        raise SettingsError("An independent image API key requires an image base URL.")
    runtime_key = _normalize_provider(provider)
    _RUNTIME_IMAGE_API_KEY_SCOPES[runtime_key] = _provider_key_scope(base_url)
    for name in scoped_names:
        os.environ[name] = api_key
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        host = urlsplit(base_url).netloc or base_url
        for name in scoped_names:
            vault.set(
                name,
                api_key,
                description=f"{runtime_key} image provider API key for {host}",
                scope="global",
            )
    except Exception as exc:
        logger.debug("vault image API key write failed for %s: %s", runtime_key, exc)



def _set_runtime_api_key(provider: str, api_key: str, base_url: str = "") -> None:
    if not api_key:
        _clear_runtime_api_key(provider, base_url)
        return
    if provider == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = api_key
        vault_name = "ANTHROPIC_API_KEY"
    elif provider == "custom":
        os.environ["CUSTOM_API_KEY"] = api_key
        vault_name = "CUSTOM_API_KEY"
    else:
        os.environ["OPENAI_API_KEY"] = api_key
        vault_name = "OPENAI_API_KEY"
    try:
        from backend.vault import EnvVault

        EnvVault().set(
            vault_name,
            api_key,
            description=f"{provider} provider API key",
            scope="global",
        )
    except Exception as exc:
        logger.debug("vault API key write failed for %s: %s", vault_name, exc)

    scoped_names = _scoped_vault_names(provider, base_url)
    if not scoped_names:
        return
    _RUNTIME_API_KEY_SCOPES[provider] = _provider_key_scope(base_url)
    for scoped_name in scoped_names:
        os.environ[scoped_name] = api_key
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        for scoped_name in scoped_names:
            vault.set(
                scoped_name,
                api_key,
                description=f"{provider} provider API key for {urlsplit(base_url).netloc or base_url}",
                scope="global",
            )
    except Exception as exc:
        logger.debug("vault scoped API key write failed for %s: %s", provider, exc)


def _clear_runtime_api_key(provider: str, base_url: str = "") -> None:
    if provider == "anthropic":
        vault_name = "ANTHROPIC_API_KEY"
    elif provider == "custom":
        vault_name = "CUSTOM_API_KEY"
    else:
        vault_name = "OPENAI_API_KEY"
    os.environ.pop(vault_name, None)
    names = [vault_name]
    scoped_names = _scoped_vault_names(provider, base_url)
    names.extend(name for name in scoped_names if name not in names)
    for scoped_name in scoped_names:
        os.environ.pop(scoped_name, None)
    if not base_url or _RUNTIME_API_KEY_SCOPES.get(provider) == _provider_key_scope(base_url):
        _RUNTIME_API_KEY_SCOPES.pop(provider, None)
    try:
        from backend.vault import EnvVault

        vault = EnvVault()
        for name in names:
            vault.delete(name)
    except Exception as exc:
        logger.debug("vault API key clear failed for %s: %s", vault_name, exc)



def _next_model_metadata(
    updates: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    capabilities_match: bool,
) -> dict[str, dict[str, Any]]:
    """Persist submitted metadata, otherwise retain it only for the same target.

    Provider metadata belongs to one endpoint/model selection.  A model or
    endpoint change without a fresh catalog must not inherit the previous
    model's capabilities.
    """

    if "model_metadata" in updates:
        return _coerce_model_metadata(updates.get("model_metadata"))
    if capabilities_match:
        return _coerce_model_metadata(current.get("model_metadata"))
    return {}


def _next_image_settings(
    provider: str,
    updates: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    mode = _normalize_image_mode(
        updates.get("image_mode", current.get("image_mode", "inherit")),
    )
    base_url = str(
        updates.get("image_base_url", current.get("image_base_url", "")) or ""
    ).strip()
    normalized_base_url = _normalize_openai_base_url(base_url) if base_url else ""
    image_key_provided = "image_api_key" in updates
    image_key = str(updates.get("image_api_key") or "").strip()
    if image_key_provided and _is_api_key_replacement(image_key):
        _set_runtime_image_api_key(provider, image_key, normalized_base_url)
    return {
        "image_mode": mode,
        # Secrets live only in the endpoint-scoped vault.
        "image_api_key": "",
        "image_base_url": normalized_base_url,
        "image_model": str(
            updates.get("image_model", current.get("image_model", "")) or ""
        ).strip(),
        "image_size": _normalize_image_size(
            updates.get("image_size", current.get("image_size", "1024x1024")),
        ),
        "image_quality": _normalize_image_quality(
            updates.get("image_quality", current.get("image_quality", "")),
        ),
    }



def _upsert_llm_history(
    settings_data: dict[str, Any],
    provider: str,
    section: dict[str, Any],
) -> list[dict[str, Any]]:
    llm_data = settings_data.setdefault("llm", {})
    base_url = str(section.get("base_url") or "").strip()
    model = str(section.get("model") or "").strip()
    default_wire_api = "anthropic" if provider == "anthropic" else "responses" if provider == "openai" else "chat"
    wire_api = _history_identity(provider, base_url, str(section.get("wire_api") or default_wire_api))[2]
    if not (base_url or model):
        return _llm_history(settings_data)

    provider_id = _provider_id_for_history(provider, base_url, wire_api)
    key = _history_profile_identity(provider, base_url, wire_api)
    responses_defaults_enabled = wire_api == "responses"
    prompt_cache_retention_default = _responses_prompt_cache_retention_default(
        responses_defaults_enabled,
        provider,
    )
    next_entry = {
        "provider": provider,
        "provider_id": provider_id,
        "display_name": _provider_display_name(section),
        "base_url": base_url,
        "model": model,
        "small_fast_model": str(section.get("small_fast_model") or "").strip(),
        "available_models": _coerce_model_list(section.get("available_models")),
        "models_source": str(section.get("models_source") or "").strip(),
        "model_metadata": _coerce_model_metadata(section.get("model_metadata")),
        "wire_api": wire_api,
        "proxy_mode": _normalize_proxy_mode(section.get("proxy_mode")),
        "reasoning_effort": str(section.get("reasoning_effort") or "").strip().lower(),
        "responses_reasoning_summary": str(section.get("responses_reasoning_summary") or "off").strip(),
        "max_tokens": max(0, _coerce_int(section.get("max_tokens", 0), 0)),
        "prompt_cache_retention": _normalize_prompt_cache_retention(
            section.get("prompt_cache_retention", prompt_cache_retention_default),
            prompt_cache_retention_default,
        ),
        "reasoning_effort_levels": _coerce_model_list(section.get("reasoning_effort_levels")),
        "thinking_budget": _coerce_int(section.get("thinking_budget", 0), 0),
        "image_mode": _normalize_image_mode(section.get("image_mode")),
        "has_image_api_key": bool(
            _image_api_key_for_base_url(
                provider,
                str(section.get("image_base_url") or "").strip(),
            )
        ),
        "image_api_key": "",
        "image_base_url": str(section.get("image_base_url") or "").strip(),
        "image_model": str(section.get("image_model") or "").strip(),
        "image_size": _normalize_image_size(section.get("image_size")),
        "image_quality": _normalize_image_quality(section.get("image_quality")),
        "has_api_key": bool(_provider_api_key_for_base_url(provider, base_url)),
        "updated_at": time.time(),
    }

    merged: list[dict[str, Any]] = [next_entry]
    for entry in _llm_history(settings_data):
        entry_key = _history_profile_identity(
            str(entry.get("provider") or ""),
            str(entry.get("base_url") or ""),
            str(entry.get("wire_api") or ""),
        )
        if entry_key == key:
            continue
        merged.append(entry)
    llm_data["provider_history"] = merged[:16]
    return llm_data["provider_history"]



def get_available_models(
    provider: str | None = None,
    settings_data: dict[str, Any] | None = None,
) -> list[str]:
    active_provider = _normalize_provider(provider or get_llm_provider(settings_data))
    if active_provider == "anthropic":
        return get_anthropic_settings(settings_data)["available_models"]
    if active_provider == "custom":
        return get_custom_settings(settings_data)["available_models"]
    return get_openai_settings(settings_data)["available_models"]


def get_models_source(
    provider: str | None = None,
    settings_data: dict[str, Any] | None = None,
) -> str:
    """Return the persisted model list source ('live' or '') for the given provider."""
    active_provider = _normalize_provider(provider or get_llm_provider(settings_data))
    if active_provider == "anthropic":
        return get_anthropic_settings(settings_data).get("models_source", "")
    if active_provider == "custom":
        return get_custom_settings(settings_data).get("models_source", "")
    return get_openai_settings(settings_data).get("models_source", "")



@_serialized_settings_update
def save_llm_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings_data = _load_settings_json()
    settings_data.pop("prompt_persona", None)
    raw_llm = settings_data.get("llm")
    stored_llm = dict(raw_llm) if isinstance(raw_llm, dict) else {}
    current_openai = get_openai_settings(settings_data)
    current_anthropic = get_anthropic_settings(settings_data)
    current_custom = get_custom_settings(settings_data)

    raw_provider = payload.get("provider")
    provider = _normalize_provider(str(raw_provider or get_llm_provider(settings_data)))

    raw_openai = payload.get("openai", {})
    openai_updates = raw_openai if isinstance(raw_openai, dict) else {}
    openai_api_key_provided = "api_key" in openai_updates
    openai_api_key = str(openai_updates.get("api_key", "")).strip()
    openai_base_url = str(openai_updates.get("base_url", current_openai["base_url"])).strip()
    if openai_api_key_provided and _is_api_key_replacement(openai_api_key):
        _set_runtime_api_key("openai", openai_api_key, openai_base_url)
    openai_model = str(openai_updates.get("model", current_openai["model"])).strip()
    openai_capabilities_match = (
        (openai_model or current_openai["model"]) == current_openai["model"]
        and _normalize_openai_base_url(openai_base_url) == current_openai["base_url"]
    )
    openai_reasoning_effort = str(
        openai_updates.get("reasoning_effort", current_openai["reasoning_effort"])
    ).strip()
    openai_responses_reasoning_summary = str(
        openai_updates.get(
            "responses_reasoning_summary",
            current_openai["responses_reasoning_summary"],
        )
    ).strip()
    openai_wire_api = str(openai_updates.get("wire_api", current_openai["wire_api"])).strip()
    current_openai_wire_api = normalize_custom_wire_api(
        str(current_openai.get("base_url") or openai_base_url),
        str(current_openai["wire_api"]),
        "responses",
    )
    next_openai_wire_api = normalize_custom_wire_api(
        openai_base_url,
        openai_wire_api or current_openai["wire_api"],
        current_openai["wire_api"],
    )
    openai_switched_to_responses = current_openai_wire_api != "responses" and next_openai_wire_api == "responses"
    openai_prompt_cache_retention_default = (
        _responses_prompt_cache_retention_default(True)
        if openai_switched_to_responses
        else current_openai["prompt_cache_retention"] if next_openai_wire_api == "responses" else ""
    )
    openai_prompt_cache_retention = (
        _normalize_prompt_cache_retention(
            openai_updates.get("prompt_cache_retention", openai_prompt_cache_retention_default),
            openai_prompt_cache_retention_default,
        )
        if next_openai_wire_api == "responses"
        else ""
    )
    next_openai_model = openai_model or current_openai["model"]
    next_openai_metadata = _next_model_metadata(
        openai_updates,
        current_openai,
        capabilities_match=openai_capabilities_match,
    )
    next_openai_resolved_metadata = get_provider_model_metadata(
        {
            "model": next_openai_model,
            "model_metadata": next_openai_metadata,
            "reasoning_effort_levels": openai_updates.get(
                "reasoning_effort_levels",
                current_openai["reasoning_effort_levels"]
                if openai_capabilities_match
                else [],
            ),
        },
        next_openai_model,
    )
    next_openai_image = _next_image_settings(
        "openai",
        openai_updates,
        current_openai,
    )
    next_openai = {
        "display_name": str(openai_updates.get("display_name", current_openai["display_name"])).strip(),
        "api_key": "",
        "base_url": _normalize_openai_base_url(openai_base_url),
        "model": next_openai_model,
        "small_fast_model": str(
            openai_updates.get("small_fast_model", current_openai["small_fast_model"])
        ).strip(),
        "available_models": _coerce_model_list(
            openai_updates.get("available_models", current_openai["available_models"])
        ),
        "models_source": str(openai_updates.get("models_source", current_openai.get("models_source", ""))).strip(),
        "model_metadata": next_openai_metadata,
        "model_labels": _coerce_model_labels(
            openai_updates.get("model_labels", current_openai.get("model_labels", {}))
        ),
        "reasoning_effort": openai_reasoning_effort or current_openai["reasoning_effort"],
        "responses_reasoning_summary": (
            openai_responses_reasoning_summary
            or current_openai["responses_reasoning_summary"]
        ),
        "max_tokens": max(
            0,
            _coerce_int(
                openai_updates.get("max_tokens", current_openai["max_tokens"]),
                current_openai["max_tokens"],
            ),
        ),
        "wire_api": next_openai_wire_api,
        "proxy_mode": _normalize_proxy_mode(
            openai_updates.get("proxy_mode", current_openai["proxy_mode"]),
        ),
        "headers": dict(
            openai_updates.get("headers", current_openai["default_headers"])
        ),
        "auth_header": bool(
            openai_updates.get("auth_header", current_openai["auth_header"])
        ),
        "prompt_cache_retention": openai_prompt_cache_retention,
        "reasoning_effort_levels": next_openai_resolved_metadata[
            "reasoning_effort_levels"
        ],
        **next_openai_image,
    }
    if next_openai["model"] and next_openai["model"] not in next_openai["available_models"]:
        next_openai["available_models"].insert(0, next_openai["model"])

    raw_anthropic = payload.get("anthropic", {})
    anthropic_updates = raw_anthropic if isinstance(raw_anthropic, dict) else {}
    anthropic_api_key_provided = "api_key" in anthropic_updates
    anthropic_api_key = str(anthropic_updates.get("api_key", "")).strip()
    anthropic_base_url = str(anthropic_updates.get("base_url", current_anthropic["base_url"])).strip()
    if anthropic_api_key_provided and _is_api_key_replacement(anthropic_api_key):
        _set_runtime_api_key("anthropic", anthropic_api_key, anthropic_base_url)
    anthropic_model = str(anthropic_updates.get("model", current_anthropic["model"])).strip()
    next_anthropic_model = anthropic_model or current_anthropic["model"]
    anthropic_capabilities_match = (
        next_anthropic_model == current_anthropic["model"]
        and anthropic_base_url.rstrip("/")
        == str(current_anthropic["base_url"]).rstrip("/")
    )
    next_anthropic_metadata = _next_model_metadata(
        anthropic_updates,
        current_anthropic,
        capabilities_match=anthropic_capabilities_match,
    )
    next_anthropic_image = _next_image_settings(
        "anthropic",
        anthropic_updates,
        current_anthropic,
    )
    next_anthropic = {
        "display_name": str(anthropic_updates.get("display_name", current_anthropic["display_name"])).strip(),
        "api_key": "",
        "base_url": anthropic_base_url,
        "model": next_anthropic_model,
        "small_fast_model": str(
            anthropic_updates.get(
                "small_fast_model", current_anthropic["small_fast_model"]
            )
        ).strip(),
        "available_models": _coerce_model_list(
            anthropic_updates.get("available_models", current_anthropic["available_models"])
        ),
        "models_source": str(anthropic_updates.get("models_source", current_anthropic.get("models_source", ""))).strip(),
        "model_metadata": next_anthropic_metadata,
        "model_labels": _coerce_model_labels(
            anthropic_updates.get("model_labels", current_anthropic.get("model_labels", {}))
        ),
        "max_tokens": _coerce_int(
            anthropic_updates.get("max_tokens", current_anthropic["max_tokens"]),
            current_anthropic["max_tokens"],
        ),
        "thinking_budget": _coerce_int(
            anthropic_updates.get("thinking_budget", current_anthropic["thinking_budget"]),
            current_anthropic["thinking_budget"],
        ),
        "proxy_mode": _normalize_proxy_mode(
            anthropic_updates.get("proxy_mode", current_anthropic["proxy_mode"]),
        ),
        "headers": dict(
            anthropic_updates.get("headers", current_anthropic["default_headers"])
        ),
        "auth_header": bool(
            anthropic_updates.get("auth_header", current_anthropic["auth_header"])
        ),
        **next_anthropic_image,
    }
    if next_anthropic["max_tokens"] <= 0:
        next_anthropic["max_tokens"] = MINICODE_CAPPED_DEFAULT_MAX_TOKENS
    if next_anthropic["model"] and next_anthropic["model"] not in next_anthropic["available_models"]:
        next_anthropic["available_models"].insert(0, next_anthropic["model"])

    raw_custom = payload.get("custom", {})
    custom_updates = raw_custom if isinstance(raw_custom, dict) else {}
    custom_api_key_provided = "api_key" in custom_updates
    custom_api_key = str(custom_updates.get("api_key", "")).strip()
    custom_base_url = str(custom_updates.get("base_url", current_custom["base_url"])).strip()
    if custom_api_key_provided and _is_api_key_replacement(custom_api_key):
        _set_runtime_api_key("custom", custom_api_key, custom_base_url)
    custom_model = str(custom_updates.get("model", current_custom["model"])).strip()
    custom_wire_api = normalize_custom_wire_api(
        custom_base_url,
        str(custom_updates.get("wire_api", current_custom["wire_api"])),
        current_custom["wire_api"],
    )
    current_custom_wire_api = normalize_custom_wire_api(
        str(current_custom.get("base_url") or custom_base_url),
        str(current_custom["wire_api"]),
        "chat",
    )
    custom_switched_to_responses = current_custom_wire_api != "responses" and custom_wire_api == "responses"
    custom_prompt_cache_retention_default = (
        _responses_prompt_cache_retention_default(True, "custom")
        if custom_switched_to_responses
        else current_custom["prompt_cache_retention"] if custom_wire_api == "responses" else ""
    )
    next_custom_available = _coerce_model_list(
        custom_updates.get("available_models", current_custom["available_models"])
    )
    next_custom_model = _select_custom_model(
        custom_model or current_custom["model"],
        next_custom_available,
    )
    custom_capabilities_match = (
        next_custom_model == current_custom["model"]
        and custom_base_url.rstrip("/") == str(current_custom["base_url"]).rstrip("/")
    )
    next_custom_metadata = _next_model_metadata(
        custom_updates,
        current_custom,
        capabilities_match=custom_capabilities_match,
    )
    next_custom_resolved_metadata = get_provider_model_metadata(
        {
            "model": next_custom_model,
            "model_metadata": next_custom_metadata,
            "reasoning_effort_levels": custom_updates.get(
                "reasoning_effort_levels",
                current_custom["reasoning_effort_levels"]
                if custom_capabilities_match
                else [],
            ),
        },
        next_custom_model,
    )
    next_custom_image = _next_image_settings(
        "custom",
        custom_updates,
        current_custom,
    )
    custom_max_tokens_default = (
        MINICODE_CAPPED_DEFAULT_MAX_TOKENS if custom_wire_api == "anthropic" else 0
    )
    if "max_tokens" in custom_updates:
        custom_max_tokens = _coerce_int(
            custom_updates.get("max_tokens"),
            custom_max_tokens_default,
        )
    elif custom_wire_api == current_custom_wire_api:
        custom_max_tokens = _coerce_int(
            current_custom.get("max_tokens"),
            custom_max_tokens_default,
        )
    else:
        custom_max_tokens = custom_max_tokens_default
    if custom_wire_api == "anthropic" and custom_max_tokens <= 0:
        custom_max_tokens = MINICODE_CAPPED_DEFAULT_MAX_TOKENS

    next_custom = {
        "display_name": str(custom_updates.get("display_name", current_custom["display_name"])).strip(),
        "api_key": "",
        "base_url": (
            _normalize_openai_base_url(custom_base_url)
            if custom_base_url and custom_wire_api != "anthropic"
            else custom_base_url
        ),
        "model": next_custom_model,
        "small_fast_model": str(
            custom_updates.get("small_fast_model", current_custom["small_fast_model"])
        ).strip(),
        "available_models": next_custom_available,
        "models_source": str(custom_updates.get("models_source", current_custom.get("models_source", ""))).strip(),
        "model_metadata": next_custom_metadata,
        "model_labels": _coerce_model_labels(
            custom_updates.get("model_labels", current_custom.get("model_labels", {}))
        ),
        "reasoning_effort": str(custom_updates.get("reasoning_effort", current_custom["reasoning_effort"])).strip(),
        "responses_reasoning_summary": str(
            custom_updates.get(
                "responses_reasoning_summary",
                current_custom["responses_reasoning_summary"],
            )
        ).strip(),
        "max_tokens": max(0, custom_max_tokens),
        "thinking_budget": _coerce_int(
            custom_updates.get("thinking_budget", current_custom["thinking_budget"]),
            current_custom["thinking_budget"],
        ),
        "wire_api": custom_wire_api or current_custom["wire_api"],
        "proxy_mode": _normalize_proxy_mode(
            custom_updates.get("proxy_mode", current_custom["proxy_mode"]),
        ),
        "headers": dict(
            custom_updates.get("headers", current_custom["default_headers"])
        ),
        "auth_header": bool(
            custom_updates.get("auth_header", current_custom["auth_header"])
        ),
        "prompt_cache_retention": (
            _normalize_prompt_cache_retention(
                custom_updates.get("prompt_cache_retention", custom_prompt_cache_retention_default),
                custom_prompt_cache_retention_default,
            )
            if custom_wire_api == "responses"
            else ""
        ),
        "reasoning_effort_levels": next_custom_resolved_metadata[
            "reasoning_effort_levels"
        ],
        **next_custom_image,
    }
    if next_custom["model"] and next_custom["model"] not in next_custom["available_models"]:
        next_custom["available_models"].insert(0, next_custom["model"])

    # Preserve untouched provider sections exactly as writable user data
    # instead of materializing environment/config-layer fallbacks into
    # settings.json. This distinction is important for desktop launches from
    # development agents, which may expose temporary localhost model proxies.
    # Secrets are still scrubbed from any legacy plaintext provider section.
    next_llm = dict(stored_llm)
    next_llm["provider"] = provider
    next_llm["provider_history"] = _llm_history(settings_data)
    for section_provider, updates, section in (
        ("openai", openai_updates, next_openai),
        ("anthropic", anthropic_updates, next_anthropic),
        ("custom", custom_updates, next_custom),
    ):
        if updates:
            next_llm[section_provider] = section
            continue
        stored_section = next_llm.get(section_provider)
        if isinstance(stored_section, dict):
            preserved_section = dict(stored_section)
            preserved_section["api_key"] = ""
            preserved_section["image_api_key"] = ""
            next_llm[section_provider] = preserved_section
        else:
            next_llm.pop(section_provider, None)
    settings_data["llm"] = next_llm
    upserted_providers: set[str] = set()
    for section_provider, updates, section in (
        ("openai", openai_updates, next_openai),
        ("anthropic", anthropic_updates, next_anthropic),
        ("custom", custom_updates, next_custom),
    ):
        if updates:
            _upsert_llm_history(settings_data, section_provider, section)
            upserted_providers.add(section_provider)
    active_section = {
        "openai": next_openai,
        "anthropic": next_anthropic,
        "custom": next_custom,
    }.get(provider)
    # Only create a history card when the caller explicitly selected a
    # provider. Persona-only and other unrelated settings saves must not turn
    # built-in/default provider sections into user-configured profiles.
    if active_section is not None and not upserted_providers and raw_provider is not None:
        _upsert_llm_history(settings_data, provider, active_section)
    _write_settings_json(settings_data)
    return get_llm_settings_payload(settings_data, include_api_keys=True)
