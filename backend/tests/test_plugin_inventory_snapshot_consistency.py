from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config_requirements import ConfigRequirements
from backend.extensions import capability_source
from backend.services import plugin_settings_service as plugins


@pytest.mark.parametrize("relative_root", [False, True])
def test_one_manifest_revision_drives_inventory_and_extension_load(tmp_path, monkeypatch, relative_root):
    from backend.commands import plugins as commands

    root = tmp_path / "installed" / "sample"
    manifest = root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    (root / "extensions").mkdir()
    (root / "extensions" / "first.py").write_text(
        "def extension(api):\n    api.register_flag('revision', {'type': 'string', 'default': 'first'})\n", encoding="utf-8")
    (root / "extensions" / "other.py").write_text(
        "def extension(api):\n    api.register_flag('revision', {'type': 'string', 'default': 'other'})\n", encoding="utf-8")
    first = {"name": "sample", "version": "1.0.0", "description": "first revision", "extensions": "./extensions/first.py",
             "mcp_servers": {"first-server": {"command": "echo"}}}
    second = {"name": "sample", "version": "1.0.0", "description": "other revision", "extensions": "./extensions/other.py",
              "mcp_servers": {"other-server": {"command": "echo"}}}
    manifest.write_text(json.dumps(first), encoding="utf-8")
    original_stat = manifest.stat()
    assert len(json.dumps(first)) == len(json.dumps(second))
    config = {"plugins": {"sample@local": {"enabled": True}}, "feature_flags": {"plugins": True}}
    stack = SimpleNamespace(requirements=ConfigRequirements(), fingerprint="fixture", managed_policy_errors=(),
                            effective_config=lambda: config, get_layers=lambda: [])
    monkeypatch.setattr(plugins, "_load_settings_json", lambda: config)
    if relative_root:
        monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "default_plugin_roots", lambda: [Path("installed") if relative_root else root.parent])
    monkeypatch.setattr(capability_source, "load_config_layer_stack", lambda **_kwargs: stack)
    original_read = Path.read_text
    reads = []

    def change_after_read(path: Path, *args, **kwargs):
        value = original_read(path, *args, **kwargs)
        if path.absolute() == manifest:
            reads.append(value)
            if len(reads) == 1:
                manifest.write_text(json.dumps(second), encoding="utf-8")
                os.utime(manifest, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return value

    monkeypatch.setattr(Path, "read_text", change_after_read)
    source = capability_source.ExtensionCapabilitySource(session_owner="fixture", owner_id="conversation",
        workspace_root=tmp_path, project_trusted=True, on_model_change=lambda *_args: None)
    first_fingerprint = source.fingerprint()
    first_snapshot = source._plugin_snapshot
    assert len(reads) == 1
    assert first_snapshot["plugins"][0]["description"] == "first revision"
    assert first_snapshot["plugins"][0]["mcp_server_names"] == ["first-server"]
    assert "runtime_manifests" not in json.dumps(first_snapshot)
    first_candidate = asyncio.run(source.load())
    try:
        assert first_candidate.result.runtime.flag_values["revision"] == "first"
        assert len(reads) == 1
        second_fingerprint = source.fingerprint()
        assert second_fingerprint != first_fingerprint and len(reads) == 2
        second_candidate = asyncio.run(source.load())
        try:
            assert second_candidate.result.runtime.flag_values["revision"] == "other"
            assert source._plugin_snapshot["plugins"][0]["mcp_server_names"] == ["other-server"]
        finally:
            second_candidate.discard("test complete")
    finally:
        first_candidate.discard("test complete")
