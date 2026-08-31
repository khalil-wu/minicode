from __future__ import annotations

import json
import asyncio

import pytest

from backend.config import load_config
from backend.feature_flags import (
    coerce_feature_bool,
    feature_env_name,
    feature_flags_payload,
    load_feature_flags,
    normalize_feature_name,
)
from backend.services.feature_flag_settings_service import (
    FeatureFlagSettingsError,
    get_feature_flag_settings,
    update_feature_flag_settings,
)


def test_feature_flag_name_normalization_and_boolean_coercion() -> None:
    assert normalize_feature_name("MCP-Streamable HTTP") == "mcp_streamable_http"
    assert feature_env_name("global-search") == "MINICODE_FEATURE_GLOBAL_SEARCH"
    assert coerce_feature_bool("enabled") is True
    assert coerce_feature_bool("off", True) is False
    assert coerce_feature_bool("not-a-bool", True) is True


def test_feature_flags_default_to_existing_runtime_behaviour(monkeypatch) -> None:
    flags = load_feature_flags({})

    assert flags.enabled("reactive_compact") is True
    assert flags.enabled("plugin_lifecycle_api") is True
    assert flags.enabled("plugin_skills") is True
    assert flags.enabled("sdk_query") is True
    assert flags.enabled("mcp_roots") is True
    assert flags.enabled("mcp_elicitation") is True
    assert flags.enabled("mcp_websocket_transport") is True
    assert flags.enabled("mcp_streamable_http_transport") is True
    assert flags.enabled("global_search") is True
    assert flags.enabled("agent_editor") is True
    assert flags.enabled("agent_trace_export_v1") is False


def test_feature_flags_payload_includes_enabled_state_and_source() -> None:
    payload = feature_flags_payload({"feature_flags": {"global_search": False}})

    assert payload["global_search"] == {"enabled": False}
    assert payload["agent_editor"] == {"enabled": True}


def test_feature_flags_settings_then_env_precedence(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_FEATURE_GLOBAL_SEARCH", "false")

    flags = load_feature_flags({
        "feature_flags": {
            "global_search": True,
            "custom.future.flag": "yes",
        },
    })

    assert flags.enabled("global_search") is False
    assert flags.enabled("custom_future_flag") is True


def test_load_config_reads_agent_live_text_streaming(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "agent": {"live_text_streaming": False},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)

    config = load_config()

    assert config.agent.live_text_streaming is False


def test_load_config_reads_and_bounds_time_based_microcompact_settings(
    monkeypatch, tmp_path
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "agent": {
                    "time_based_microcompact_enabled": "enabled",
                    "time_based_microcompact_gap_threshold_minutes": 90,
                    "time_based_microcompact_keep_recent": 7,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)

    config = load_config()

    assert config.agent.time_based_microcompact_enabled is True
    assert config.agent.time_based_microcompact_gap_threshold_minutes == 90
    assert config.agent.time_based_microcompact_keep_recent == 7

    settings_file.write_text(
        json.dumps(
            {
                "agent": {
                    "time_based_microcompact_enabled": "off",
                    "time_based_microcompact_gap_threshold_minutes": 0,
                    "time_based_microcompact_keep_recent": -4,
                }
            }
        ),
        encoding="utf-8",
    )

    bounded = load_config()

    assert bounded.agent.time_based_microcompact_enabled is False
    assert bounded.agent.time_based_microcompact_gap_threshold_minutes == 1
    assert bounded.agent.time_based_microcompact_keep_recent == 1


def test_feature_flag_settings_service_updates_known_overrides(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"feature_flags": {"global_search": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)

    calls: list[dict[str, str]] = []

    async def _hook(**kwargs: str) -> None:
        calls.append(dict(kwargs))

    result = asyncio.run(
        update_feature_flag_settings(
            {"global_search": False, "agent_editor": None},
            settings_file=settings_file,
            config_change_hook=_hook,
        )
    )

    payload = json.loads(settings_file.read_text(encoding="utf-8"))
    assert payload["feature_flags"] == {"global_search": False}
    assert result["overrides"] == {"global_search": False}
    assert result["effective"]["global_search"] == {"enabled": False}
    assert calls == [{"source": "feature_flags", "file_path": str(settings_file)}]


def test_feature_flag_settings_service_reports_env_overrides(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"feature_flags": {"global_search": False}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)
    monkeypatch.setenv("MINICODE_FEATURE_GLOBAL_SEARCH", "1")

    payload = get_feature_flag_settings()
    global_search = next(flag for flag in payload["flags"] if flag["name"] == "global_search")

    assert global_search["enabled"] is True
    assert global_search["source"] == "env"
    assert global_search["override"] is False
    assert global_search["env_override"] is True


def test_feature_flag_settings_service_rejects_unknown_flags(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)

    async def _hook(**kwargs: str) -> None:
        raise AssertionError(kwargs)

    with pytest.raises(FeatureFlagSettingsError):
        asyncio.run(
            update_feature_flag_settings(
                {"not_real": True},
                settings_file=settings_file,
                config_change_hook=_hook,
            )
        )
