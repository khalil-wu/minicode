"""Small feature-flag layer for experimental MiniCode capabilities.

Precedence is:
default < settings.json `feature_flags` < `MINICODE_FEATURE_<FLAG>` env.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    # Existing behaviour defaults remain unchanged.
    "reactive_compact": True,
    "plugin_lifecycle_api": True,
    "plugin_skills": True,
    "sdk_query": True,
    "mcp_roots": True,
    # Host-side LLM sampling lets an MCP server ask MiniCode to call the model.
    # Keep it opt-in: every enabled request still needs a turn/session owner and
    # policy checks before it can run.
    "mcp_sampling": False,
    "mcp_elicitation": True,
    "mcp_websocket_transport": True,
    "mcp_streamable_http_transport": True,
    "global_search": True,
    "agent_editor": True,
    "agent_trace_export_v1": False,
}

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_ENV_PREFIX = "MINICODE_FEATURE_"


def normalize_feature_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", str(name or "").strip()).strip("_")
    return normalized.lower()


def feature_env_name(name: str) -> str:
    return f"{_ENV_PREFIX}{normalize_feature_name(name).upper()}"


def coerce_feature_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


@dataclass(frozen=True)
class FeatureFlags:
    flags: Mapping[str, bool] = field(default_factory=lambda: dict(DEFAULT_FEATURE_FLAGS))

    def enabled(self, name: str, default: bool | None = None) -> bool:
        key = normalize_feature_name(name)
        fallback = DEFAULT_FEATURE_FLAGS.get(key, False) if default is None else default
        return bool(self.flags.get(key, fallback))


def _settings_flags(settings_data: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(settings_data, Mapping):
        return {}
    raw = settings_data.get("feature_flags")
    if not isinstance(raw, Mapping):
        raw = settings_data.get("features")
    return raw if isinstance(raw, Mapping) else {}


def load_feature_flags(settings_data: Mapping[str, Any] | None = None) -> FeatureFlags:
    if settings_data is None:
        try:
            from backend.config import _load_settings_json

            settings_data = _load_settings_json()
        except Exception:
            settings_data = {}

    flags: dict[str, bool] = dict(DEFAULT_FEATURE_FLAGS)

    for raw_name, raw_value in _settings_flags(settings_data).items():
        name = normalize_feature_name(str(raw_name))
        if not name:
            continue
        flags[name] = coerce_feature_bool(raw_value, flags.get(name, False))

    for env_name, raw_value in os.environ.items():
        if not env_name.startswith(_ENV_PREFIX):
            continue
        name = normalize_feature_name(env_name[len(_ENV_PREFIX):])
        if not name:
            continue
        flags[name] = coerce_feature_bool(raw_value, flags.get(name, False))

    return FeatureFlags(flags=flags)


def feature_enabled(name: str, default: bool | None = None) -> bool:
    return load_feature_flags().enabled(name, default)


def feature_flags_payload(settings_data: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    feature_flags = load_feature_flags(settings_data)
    names = sorted(feature_flags.flags.keys())
    return {
        name: {
            "enabled": feature_flags.enabled(name),
        }
        for name in names
    }
