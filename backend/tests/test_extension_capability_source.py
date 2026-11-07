from pathlib import Path

from backend.extensions.capability_source import ExtensionCapabilitySource


def test_capability_source_fingerprint_is_provider_neutral(tmp_path: Path) -> None:
    source = ExtensionCapabilitySource(
        session_owner="session",
        owner_id="conversation",
        workspace_root=tmp_path,
        project_trusted=True,
        on_model_change=lambda *_args: None,
    )

    fingerprint = source.fingerprint()

    assert str(tmp_path.resolve()) in fingerprint
    assert "trusted" in fingerprint


def test_fingerprint_caches_config_snapshot_for_candidate_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def load_stack(*, cwd: Path):
        nonlocal calls
        calls += 1
        return {"cwd": str(cwd)}

    monkeypatch.setattr(
        "backend.extensions.capability_source.load_config_layer_stack",
        load_stack,
    )
    monkeypatch.setattr(
        "backend.extensions.capability_source.get_plugin_snapshot",
        lambda *, config_stack: {"fingerprint": "snapshot"},
    )
    source = ExtensionCapabilitySource(
        session_owner="session",
        owner_id="conversation",
        workspace_root=tmp_path,
        project_trusted=True,
        on_model_change=lambda *_args: None,
    )

    assert source.fingerprint().endswith("|snapshot")
    assert calls == 1
    assert source._config_stack == {"cwd": str(tmp_path)}


def test_admitted_stack_owns_plugin_fingerprint_without_reloading_workspace_configuration(tmp_path, monkeypatch):
    from backend.config_layers import ConfigLayerStack

    stack = ConfigLayerStack(())
    captured = []

    def forbidden(**kwargs):
        raise AssertionError("An admitted generation must use its captured configuration")

    def snapshot(*, config_stack):
        captured.append(config_stack)
        return {"fingerprint": "owned-plugin-content"}

    monkeypatch.setattr("backend.extensions.capability_source.load_config_layer_stack", forbidden)
    monkeypatch.setattr("backend.extensions.capability_source.get_plugin_snapshot", snapshot)
    source = ExtensionCapabilitySource(session_owner="window", owner_id="thread", workspace_root=tmp_path,
        project_trusted=True, on_model_change=lambda *_args: None)
    assert source.fingerprint(config_stack=stack).endswith("|owned-plugin-content")
    assert source._config_stack is stack
    assert captured == [stack]
