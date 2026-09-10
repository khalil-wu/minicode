from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import backend.plugins.package as package
from backend.main import app
from backend.plugins.policy import ManagedPluginPolicy, PluginSettingsError
from backend.services.plugin_settings_service import import_plugin_from_path


def _linked_manifest(tmp_path: Path, kind: str) -> tuple[Path, Path]:
    source = tmp_path / "plugin"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    target = outside / "plugin.json"
    target.write_text(json.dumps({
        "name": "outside-fixture", "description": "OUTSIDE_METADATA_MARKER", "version": "1.0.0",
    }), encoding="utf-8")
    try:
        if kind == "directory":
            (source / ".minicode-plugin").symlink_to(outside, target_is_directory=True)
        else:
            (source / ".minicode-plugin").mkdir()
            (source / ".minicode-plugin" / "plugin.json").symlink_to(target)
    except OSError as exc:
        if os.name == "nt":
            pytest.skip(f"Windows symbolic links unavailable: {exc}")
        raise
    return source, outside


def _record_outside_reads(monkeypatch, outside: Path) -> list[Path]:
    reads = []
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.resolve().is_relative_to(outside):
            reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return reads


def _assert_rejected(operation: str, source: Path, tmp_path: Path) -> None:
    if operation == "validate":
        result = package.validate_plugin_directory(source)
        assert result["ok"] is False
        assert "symbolic links or junctions" in result["errors"][0]
        assert "plugin" not in result
        assert result["manifests"] == []
    elif operation == "package":
        with pytest.raises(PluginSettingsError, match="symbolic links or junctions"):
            package.package_plugin_directory(source, tmp_path / "packages")
        assert not (tmp_path / "packages").exists()
    else:
        hook = AsyncMock()
        policy = ManagedPluginPolicy(
            enabled_plugins={}, strict_known_marketplaces=None, blocked_marketplaces=(), marketplace_requirements={},
        )
        with pytest.raises(PluginSettingsError, match="symbolic links or junctions"):
            asyncio.run(import_plugin_from_path(
                source,
                settings_file=tmp_path / "settings.json",
                config_change_hook=hook,
                _policy=policy,
            ))
        hook.assert_not_awaited()
        assert not (tmp_path / "settings.json").exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("operation", ["validate", "package", "import"])
def test_plugin_entry_points_reject_links_before_reading_metadata(
    monkeypatch, tmp_path: Path, kind: str, operation: str,
) -> None:
    source, outside = _linked_manifest(tmp_path, kind)
    reads = _record_outside_reads(monkeypatch, outside)

    _assert_rejected(operation, source, tmp_path)

    assert reads == []


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("operation", ["validate", "package"])
def test_plugin_http_validation_does_not_return_link_target_metadata(
    monkeypatch, tmp_path: Path, kind: str, operation: str,
) -> None:
    source, outside = _linked_manifest(tmp_path, kind)
    reads = _record_outside_reads(monkeypatch, outside)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(f"/api/plugins/{operation}", json={
                "source_path": str(source), "output_dir": str(tmp_path / "packages"),
            })

    response = asyncio.run(scenario())

    assert response.status_code == (200 if operation == "validate" else 400)
    assert "OUTSIDE_METADATA_MARKER" not in response.text
    assert "outside-fixture" not in response.text
    assert "symbolic links or junctions" in response.text
    assert reads == []


def test_plugin_link_diagnostic_uses_the_lexical_link_path(tmp_path: Path) -> None:
    source, outside = _linked_manifest(tmp_path, "file")
    (source / ".minicode-plugin" / "plugin.json").unlink()
    target = source / "declaration.json"
    target.write_text((outside / "plugin.json").read_text(), encoding="utf-8")
    (source / ".minicode-plugin" / "plugin.json").symlink_to(target)

    result = package.validate_plugin_directory(source)

    assert result["ok"] is False
    assert result["errors"][0].endswith(".minicode-plugin/plugin.json")


def test_plugin_broken_manifest_link_is_rejected_before_manifest_discovery(tmp_path: Path) -> None:
    source, outside = _linked_manifest(tmp_path, "file")
    (outside / "plugin.json").unlink()

    result = package.validate_plugin_directory(source)

    assert result["ok"] is False
    assert "symbolic links or junctions" in result["errors"][0]
    assert result["manifests"] == []


def test_plugin_scanner_prunes_junction_metadata_without_descending(monkeypatch, tmp_path: Path) -> None:
    junction = tmp_path / "junction"
    junction.mkdir()
    (junction / "not-visited.txt").write_text("fixture", encoding="utf-8")
    (tmp_path / "ordinary.txt").write_text("ordinary", encoding="utf-8")
    original_lstat = Path.lstat
    inspected = []
    mount_point_tag = 0xA0000003

    def lstat(path, *args, **kwargs):
        inspected.append(path)
        metadata = original_lstat(path, *args, **kwargs)
        return SimpleNamespace(st_mode=metadata.st_mode, st_reparse_tag=mount_point_tag if path == junction else 0)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(package, "os", SimpleNamespace(name="nt", walk=os.walk))
    monkeypatch.setattr(package, "stat", SimpleNamespace(S_ISLNK=stat.S_ISLNK, IO_REPARSE_TAG_MOUNT_POINT=mount_point_tag))

    assert package._plugin_symlink_paths(tmp_path) == ["junction"]
    assert junction / "not-visited.txt" not in inspected
    assert tmp_path / "ordinary.txt" in inspected


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows junctions")
@pytest.mark.parametrize("operation", ["validate", "package", "import"])
def test_plugin_entry_points_reject_real_windows_junctions(monkeypatch, tmp_path: Path, operation: str) -> None:
    source = tmp_path / "plugin"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "plugin.json").write_text(json.dumps({"name": "outside-fixture"}), encoding="utf-8")
    junction = source / ".minicode-plugin"
    subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    try:
        reads = _record_outside_reads(monkeypatch, outside)
        _assert_rejected(operation, source, tmp_path)
        assert reads == []
        assert (outside / "plugin.json").is_file()
    finally:
        os.rmdir(junction)


def test_regular_plugin_validation_and_packaging_keep_their_metadata(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    manifest = source / ".minicode-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "regular-fixture", "version": "1.0.0"}), encoding="utf-8")
    skill = source / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example\ndescription: fixture\n---\n# Example\n", encoding="utf-8")
    dependency = source / "node_modules" / "runtime.txt"
    dependency.parent.mkdir()
    dependency.write_text("runtime dependency", encoding="utf-8")

    result = package.package_plugin_directory(source, tmp_path / "packages")

    assert result["ok"] is True
    assert result["validation"]["plugin"]["name"] == "regular-fixture"
    assert result["validation"]["plugin"]["skill_count"] == 1
    assert result["validation"]["plugin"]["file_count"] == 3
    with zipfile.ZipFile(result["package"]["path"]) as archive:
        assert archive.namelist() == [".minicode-plugin/plugin.json", "node_modules/runtime.txt", "skills/example/SKILL.md"]
        assert archive.read("node_modules/runtime.txt") == dependency.read_bytes()
        assert archive.read("skills/example/SKILL.md") == skill.read_bytes()
