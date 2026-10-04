from pathlib import Path

import pytest

from backend.config_layers import ConfigLayer, ConfigLayerError, ConfigLayerSource, load_config_layers_state


@pytest.mark.parametrize("kind", ["policy", "session"])
def test_nonproject_marker_override_selects_config_and_instruction_scope_together(tmp_path, kind):
    workspace = tmp_path / "repo"
    cwd = workspace / "package/src"
    cwd.mkdir(parents=True)
    (workspace / ".git").mkdir()
    (workspace / ".minicode").mkdir()
    (workspace / ".minicode/config.toml").write_text('outer_scope_flag = "outside"', encoding="utf-8")
    (workspace / "package/pyproject.toml").write_text('[project]\nname="package"', encoding="utf-8")
    markers = {"project_root_markers": [" pyproject.toml "]}
    arguments = {"policy_config_layers": [ConfigLayer(ConfigLayerSource("policy"), markers)]} if kind == "policy" else {"session_flags": markers}
    stack = load_config_layers_state(state_root=tmp_path / "state", user_config_file=tmp_path / "missing-user.json",
        system_config_path=tmp_path / "missing-system.toml", requirements_path=tmp_path / "missing-requirements.toml",
        cwd=cwd, trust_resolver=lambda root: True, **arguments)
    assert stack.project_instruction_config()["project_root_markers"] == ["pyproject.toml"]
    assert "outer_scope_flag" not in stack.effective_config()
    assert not any(Path(layer.source.project_config_folder) == workspace / ".minicode" for layer in stack.layers if layer.source.kind == "project")


@pytest.mark.parametrize("field", ["project_root_markers", "project_doc_fallback_filenames"])
@pytest.mark.parametrize("name", ["../marker", "/absolute/marker", "name\x00marker"])
def test_config_filename_tokens_reject_paths_before_discovery(field, name):
    with pytest.raises(ConfigLayerError, match="filenames, not paths"):
        ConfigLayer(ConfigLayerSource("user"), {field: [name]})


def test_disabled_project_document_configuration_does_not_block_trusted_layers():
    disabled = ConfigLayer(ConfigLayerSource("project", project_config_folder=".minicode"),
        {"project_root_markers": 42, "project_doc_max_bytes": "invalid"}, disabled_reason="not trusted")
    assert disabled.is_disabled
