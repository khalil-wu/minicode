"""Provider settings writers and model-list helpers.

Extracted from ``backend/config.py``; depends on :mod:`backend.config_helpers`.
"""

from __future__ import annotations

from typing import (
    Any,
    Mapping,
    Callable,
)
from urllib.parse import urlsplit
import json
import logging
import os
import time

from backend.atomic_io import atomic_write_text

from backend.config_helpers import (
    MINICODE_CAPPED_DEFAULT_MAX_TOKENS,
    SettingsError,
    SETTINGS_FILE,
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
    _vault_api_key,
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


def _credential_changes(provider: str, api_key: str, base_url: str, *, image: bool = False):
    provider = _normalize_provider(provider)
    names = _image_scoped_vault_names(provider, base_url) if image else _scoped_vault_names(provider, base_url)
    if image and not names:
        raise SettingsError("An independent image API key requires an image base URL.")
    global_name = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "custom": "CUSTOM_API_KEY"}[provider]
    description = f"{provider}{' image' if image else ''} provider API key for {urlsplit(base_url).netloc or base_url}"
    changes = {name: (api_key, description, "global") for name in names}
    if not image:
        changes[global_name] = (api_key, f"{provider} provider API key", "global")
    scope_map = _RUNTIME_IMAGE_API_KEY_SCOPES if image else _RUNTIME_API_KEY_SCOPES
    scope_change = (scope_map, provider, _provider_key_scope(base_url)) if names else None
    return changes, scope_change


def _credential_deletions(
    provider: str, base_url: str, *, image: bool = False, scoped_only: bool = True,
):
    """Remove an endpoint family while preserving another endpoint's alias."""
    provider = _normalize_provider(provider)
    names = list(
        _image_scoped_vault_names(provider, base_url)
        if image else _scoped_vault_names(provider, base_url)
    )
    scope_map = _RUNTIME_IMAGE_API_KEY_SCOPES if image else _RUNTIME_API_KEY_SCOPES
    scope_matches = scope_map.get(provider) == _provider_key_scope(base_url)
    if not image:
        global_name = {
            "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "custom": "CUSTOM_API_KEY",
        }[provider]
        alias_scope = scope_map.get(provider) or _provider_key_scope(
            os.getenv({"openai": "OPENAI_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL", "custom": "CUSTOM_BASE_URL"}[provider], "")
        )
        clear_global = not scoped_only or bool(alias_scope) and alias_scope == _provider_key_scope(base_url)
        if scoped_only and names and not alias_scope:
            scoped_values = {
                value for name in names
                if _is_api_key_replacement(
                    value := (os.getenv(name, "").strip() or _vault_api_key(name).strip())
                )
            }
            global_values = {
                value for value in (os.getenv(global_name, "").strip(), _vault_api_key(global_name).strip())
                if _is_api_key_replacement(value)
            }
            clear_global = bool(scoped_values & global_values)
        if clear_global:
            names.append(global_name)
    changes = {name: (None, "", "") for name in names}
    scope_changes = [(scope_map, provider, None)] if scope_matches or not scoped_only else []
    return changes, scope_changes


def _commit_credential_changes(changes, scope_changes, settings_data=None, after_publish=None):
    """Publish one update or restore the exact previous profile on failure."""
    env_before = {name: os.environ.get(name) for name in changes}
    scopes_before = [(mapping, provider, mapping.get(provider)) for mapping, provider, _scope in scope_changes]
    settings_before = (
        SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
    ) if settings_data is not None else None

    def publish():
        try:
            if settings_data is not None:
                _write_settings_json(settings_data)
            for name, (value, _description, _scope) in changes.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            for mapping, provider, scope in scope_changes:
                if scope is None:
                    mapping.pop(provider, None)
                else:
                    mapping[provider] = scope
            if after_publish is not None:
                after_publish()
            return get_llm_settings_payload(settings_data, include_api_keys=True) if settings_data is not None else None
        except Exception as failure:
            for name, value in env_before.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            for mapping, provider, scope in scopes_before:
                if scope is None:
                    mapping.pop(provider, None)
                else:
                    mapping[provider] = scope
            if settings_data is not None:
                try:
                    current = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
                    if current != settings_before:
                        if settings_before is None:
                            SETTINGS_FILE.unlink()
                        else:
                            atomic_write_text(SETTINGS_FILE, settings_before.decode("utf-8"))
                except OSError as rollback_failure:
                    raise ExceptionGroup("Provider settings publication and rollback failed", [failure, rollback_failure]) from failure
            raise

    if changes:
        from backend.vault import EnvVault
        return EnvVault().set_many(changes, publish=publish)
    return publish()


def _set_runtime_image_api_key(provider: str, api_key: str, base_url: str) -> None:
    """Persist an independent Images API key without replacing the text key."""

    if not api_key:
        return
    changes, scope_change = _credential_changes(provider, api_key, base_url, image=True)
    _commit_credential_changes(changes, [scope_change])



def _set_runtime_api_key(provider: str, api_key: str, base_url: str = "") -> None:
    if not api_key:
        _clear_runtime_api_key(provider, base_url)
        return
    changes, scope_change = _credential_changes(provider, api_key, base_url)
    _commit_credential_changes(changes, [scope_change] if scope_change is not None else [])


def _clear_runtime_api_key(provider: str, base_url: str = "") -> None:
    changes, scope_changes = _credential_deletions(provider, base_url, scoped_only=False)
    _commit_credential_changes(changes, scope_changes)


def _clear_scoped_runtime_api_key(provider: str, base_url: str = "") -> None:
    changes, scope_changes = _credential_deletions(provider, base_url)
    _commit_credential_changes(changes, scope_changes)


def _clear_scoped_runtime_image_api_key(provider: str, base_url: str) -> None:
    changes, scope_changes = _credential_deletions(provider, base_url, image=True)
    _commit_credential_changes(changes, scope_changes)



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
    if image_key_provided and _is_api_key_replacement(image_key) and not normalized_base_url:
        raise SettingsError("An independent image API key requires an image base URL.")
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
        "headers": dict(section.get("default_headers", section.get("headers", {}))),
        "auth_header": bool(section.get("auth_header", False)),
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
def save_llm_settings(payload: dict[str, Any], *, after_publish: Callable[[], None] | None = None) -> dict[str, Any]:
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
    openai_base_url = str(openai_updates.get("base_url", current_openai["base_url"])).strip()
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
        "reasoning_effort": openai_reasoning_effort,
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
    anthropic_base_url = str(anthropic_updates.get("base_url", current_anthropic["base_url"])).strip()
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
    custom_base_url = str(custom_updates.get("base_url", current_custom["base_url"])).strip()
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
    # Parse the whole candidate before publishing credentials or settings.
    # In particular, an invalid header must never poison the next startup.
    get_llm_settings_payload(settings_data)
    upserted_providers: set[str] = set()
    credential_changes = {}
    scope_changes = []
    for section_provider, updates, section in (
        ("openai", openai_updates, next_openai),
        ("anthropic", anthropic_updates, next_anthropic),
        ("custom", custom_updates, next_custom),
    ):
        if updates:
            api_key = str(updates.get("api_key") or "").strip()
            if _is_api_key_replacement(api_key):
                changes, scope_change = _credential_changes(section_provider, api_key, str(section["base_url"]))
                credential_changes.update(changes)
                if scope_change is not None:
                    scope_changes.append(scope_change)
            image_key = str(updates.get("image_api_key") or "").strip()
            if _is_api_key_replacement(image_key):
                changes, scope_change = _credential_changes(section_provider, image_key, str(section["image_base_url"]), image=True)
                credential_changes.update(changes)
                scope_changes.append(scope_change)
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
    return _commit_credential_changes(credential_changes, scope_changes, settings_data, after_publish)
