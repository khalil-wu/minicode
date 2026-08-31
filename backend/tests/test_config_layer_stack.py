from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.config_layers import ConfigLayerError, load_config_layers_state


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def test_config_layers_follow_minicode_precedence_and_track_leaf_origins(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    user_file = state_root / "settings.json"
    system_file = tmp_path / "system" / "config.toml"
    requirements_file = tmp_path / "system" / "requirements.toml"
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    _write(system_file, "[agent]\nstream_timeout_seconds = 10\n")
    _write(requirements_file, "allowed_sandbox_modes = ['read-only', 'workspace-write']\n")
    _write(
        user_file,
        json.dumps({"agent": {"stream_timeout_seconds": 20}, "feature_flags": {"global_search": True}}),
    )
    _write(workspace / ".minicode" / "config.toml", "[agent]\nstream_timeout_seconds = 30\n")

    stack = load_config_layers_state(
        state_root=state_root,
        user_config_file=user_file,
        cwd=workspace,
        system_config_path=system_file,
        requirements_path=requirements_file,
        session_flags={"agent": {"stream_timeout_seconds": 40}},
        trust_resolver=lambda _path: True,
    )

    assert stack.effective_config()["agent"]["stream_timeout_seconds"] == 40
    origin = stack.origins()["agent.stream_timeout_seconds"]
    assert origin.source.kind == "session_flags"
    assert [layer.source.kind for layer in stack.get_layers()] == [
        "system",
        "user",
        "project",
        "session_flags",
    ]


def test_untrusted_project_layer_is_visible_but_disabled(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    user_file = state_root / "settings.json"
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    _write(user_file, json.dumps({"agent": {"agent_mode": "react"}}))
    _write(workspace / ".minicode" / "config.toml", "[agent]\nagent_mode = 'auto'\n")

    stack = load_config_layers_state(
        state_root=state_root,
        user_config_file=user_file,
        cwd=workspace,
        system_config_path=tmp_path / "missing-system.toml",
        requirements_path=tmp_path / "missing-requirements.toml",
        trust_resolver=lambda _path: False,
    )

    project = next(layer for layer in stack.layers if layer.source.kind == "project")
    assert project.is_disabled is True
    assert "not trusted" in str(project.disabled_reason)
    assert project.config["agent"]["agent_mode"] == "auto"
    assert stack.effective_config()["agent"]["agent_mode"] == "react"
    assert stack.to_payload()["layers"][2]["disabled_reason"]


def test_trusted_project_cannot_override_provider_credential_destination(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    user_file = state_root / "settings.json"
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    _write(user_file, json.dumps({"llm": {"provider": "openai"}}))
    _write(
        workspace / ".minicode" / "config.toml",
        "[llm]\nprovider = 'custom'\n[agent]\nagent_mode = 'auto'\n",
    )

    stack = load_config_layers_state(
        state_root=state_root,
        user_config_file=user_file,
        cwd=workspace,
        system_config_path=tmp_path / "missing-system.toml",
        requirements_path=tmp_path / "missing-requirements.toml",
        trust_resolver=lambda _path: True,
    )

    effective = stack.effective_config()
    assert effective["llm"]["provider"] == "openai"
    assert effective["agent"]["agent_mode"] == "auto"
    assert any("llm" in warning for warning in stack.startup_warnings)


def test_profile_v2_overlays_user_settings_and_rejects_legacy_collision(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    user_file = state_root / "settings.json"
    _write(user_file, json.dumps({"agent": {"agent_mode": "react"}}))
    _write(state_root / "work.config.toml", "[agent]\nagent_mode = 'auto'\n")

    stack = load_config_layers_state(
        state_root=state_root,
        user_config_file=user_file,
        profile="work",
        system_config_path=tmp_path / "missing-system.toml",
        requirements_path=tmp_path / "missing-requirements.toml",
    )
    assert stack.effective_config()["agent"]["agent_mode"] == "auto"
    assert stack.effective_user_config()["agent"]["agent_mode"] == "auto"

    _write(user_file, json.dumps({"profile": "work", "profiles": {"work": {}}}))
    with pytest.raises(ConfigLayerError, match="legacy"):
        load_config_layers_state(
            state_root=state_root,
            user_config_file=user_file,
            profile="work",
            system_config_path=tmp_path / "missing-system.toml",
            requirements_path=tmp_path / "missing-requirements.toml",
        )


def test_layer_diagnostics_do_not_return_config_values(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    user_file = state_root / "settings.json"
    secret = "sk-test-secret-value"
    _write(user_file, json.dumps({"llm": {"openai": {"api_key": secret}}}))
    stack = load_config_layers_state(
        state_root=state_root,
        user_config_file=user_file,
        system_config_path=tmp_path / "missing-system.toml",
        requirements_path=tmp_path / "missing-requirements.toml",
    )

    assert secret not in json.dumps(stack.to_payload())

