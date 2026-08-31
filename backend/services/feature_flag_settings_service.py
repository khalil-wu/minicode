from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from backend.config import (
    _load_settings_json,
    _update_settings_json,
    get_config_requirements,
    load_config,
)
from backend.config_requirements import RequirementViolation
from backend.feature_flags import (
    DEFAULT_FEATURE_FLAGS,
    coerce_feature_bool,
    feature_env_name,
    feature_flags_payload,
    normalize_feature_name,
)
from backend.hooks.runtime import raise_if_config_change_blocked

ConfigChangeHook = Callable[..., Awaitable[Any]]


class FeatureFlagSettingsError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_feature_flag_settings() -> dict[str, Any]:
    settings_data = _load_settings_json()
    requirements = get_config_requirements()
    managed = requirements.feature_requirements
    overrides = _settings_overrides(settings_data)
    effective = feature_flags_payload(settings_data, managed_requirements=managed)
    flags: list[dict[str, Any]] = []
    for name in sorted(DEFAULT_FEATURE_FLAGS):
        env_name = feature_env_name(name)
        effective_item = effective.get(name, {})
        source = (
            "managed"
            if name in managed
            else "env"
            if env_name in os.environ
            else "settings"
            if name in overrides
            else "default"
        )
        flags.append({
            "name": name,
            "default": DEFAULT_FEATURE_FLAGS[name],
            "enabled": bool(effective_item.get("enabled", DEFAULT_FEATURE_FLAGS[name])),
            "source": source,
            "override": overrides.get(name),
            "env_var": env_name,
            "env_override": coerce_feature_bool(os.environ[env_name], DEFAULT_FEATURE_FLAGS[name])
            if env_name in os.environ
            else None,
            "managed_value": bool(managed[name]) if name in managed else None,
            "managed_source": (
                requirements.source_for("features").to_payload()
                if name in managed and requirements.source_for("features") is not None
                else None
            ),
        })

    return {
        "flags": flags,
        "overrides": overrides,
        "effective": effective,
    }


async def update_feature_flag_settings(
    updates: Mapping[str, bool | None],
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    requirements = get_config_requirements()
    normalized_updates: dict[str, bool | None] = {}
    for raw_name, value in updates.items():
        name = normalize_feature_name(str(raw_name))
        if name not in DEFAULT_FEATURE_FLAGS:
            raise FeatureFlagSettingsError(f"Unknown feature flag: {raw_name}", status_code=400)
        if value is not None and not isinstance(value, bool):
            raise FeatureFlagSettingsError(f"Feature flag '{name}' must be true, false, or null", status_code=400)
        if value is not None:
            try:
                requirements.ensure_feature_value(name, value)
            except RequirementViolation as exc:
                raise FeatureFlagSettingsError(str(exc), status_code=409) from exc
        normalized_updates[name] = value

    # Gate the live settings mutation before writing the user layer.  Managed
    # policy paths are normalized by the hook bridge to ``policy_settings``
    # and therefore remain audit-only.
    hook_result = await config_change_hook(
        source="feature_flags",
        file_path=str(settings_file),
    )
    try:
        raise_if_config_change_blocked(
            hook_result,
            source="feature_flags",
            file_path=str(settings_file),
        )
    except Exception as exc:
        if isinstance(exc, FeatureFlagSettingsError):
            raise
        raise FeatureFlagSettingsError(str(exc), status_code=409) from exc

    def apply_updates(settings_data: dict[str, Any]) -> None:
        raw_flags = settings_data.get("feature_flags")
        feature_flags = dict(raw_flags) if isinstance(raw_flags, dict) else {}
        for name, value in normalized_updates.items():
            if value is None:
                feature_flags.pop(name, None)
            else:
                feature_flags[name] = value
        if feature_flags:
            settings_data["feature_flags"] = dict(sorted(feature_flags.items()))
        else:
            settings_data.pop("feature_flags", None)

    _update_settings_json(apply_updates)
    return {
        **get_feature_flag_settings(),
        "_config": load_config(),
    }


def _settings_overrides(settings_data: Mapping[str, Any]) -> dict[str, bool]:
    raw = settings_data.get("feature_flags")
    if not isinstance(raw, Mapping):
        return {}
    overrides: dict[str, bool] = {}
    for raw_name, raw_value in raw.items():
        name = normalize_feature_name(str(raw_name))
        if name in DEFAULT_FEATURE_FLAGS:
            overrides[name] = coerce_feature_bool(raw_value, DEFAULT_FEATURE_FLAGS[name])
    return dict(sorted(overrides.items()))
