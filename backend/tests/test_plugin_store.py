from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plugins.store import (
    ACTIVE_SELECTOR_SCHEMA_VERSION,
    STORE_METADATA_SCHEMA_VERSION,
    PluginStore,
    PluginStoreRecordError,
)


def _plugin_source(tmp_path: Path, name: str = "demo") -> Path:
    source = tmp_path / "source"
    manifest_dir = source / ".minicode-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": name}),
        encoding="utf-8",
    )
    return source


def _source_descriptor(source: Path) -> dict[str, str]:
    return {"source": "directory", "path": str(source.resolve())}


def _metadata(path: Path) -> dict[str, object]:
    return json.loads((path / ".plugin-store.json").read_text(encoding="utf-8"))


def test_materialize_writes_path_derived_schema_and_selector(tmp_path: Path) -> None:
    source = _plugin_source(tmp_path)
    store = PluginStore(tmp_path / "store", create=True)

    record = store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="1.2.3",
        source=_source_descriptor(source),
    )

    metadata = _metadata(record.path)
    selector = json.loads(
        (record.path.parent / "active.json").read_text(encoding="utf-8")
    )
    assert record.plugin_id == "demo@trusted"
    assert record.version == "1.2.3"
    assert metadata["schema_version"] == STORE_METADATA_SCHEMA_VERSION
    assert metadata["id"] == "demo@trusted"
    assert metadata["name"] == "demo"
    assert metadata["marketplace"] == "trusted"
    assert metadata["version"] == "1.2.3"
    assert Path(str(metadata["path"])).absolute() == record.path.absolute()
    assert selector == {
        "schema_version": ACTIVE_SELECTOR_SCHEMA_VERSION,
        "id": "demo@trusted",
        "version": "1.2.3",
        "path": str(record.path.absolute()),
    }
    assert store.active("demo@trusted") == record
    assert store.reconcile() == {
        "ok": True,
        "plugins": 1,
        "errors": [],
        "repaired": [],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "forged@trusted"),
        ("name", "forged"),
        ("marketplace", "forged"),
        ("version", "9.9.9"),
        ("path", "/outside/store/demo/1.0.0"),
    ],
)
def test_metadata_mismatch_is_not_runtime_loadable(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    source = _plugin_source(tmp_path)
    store = PluginStore(tmp_path / "store", create=True)
    record = store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="1.0.0",
        source=_source_descriptor(source),
    )
    metadata_path = record.path / ".plugin-store.json"
    metadata = _metadata(record.path)
    metadata[field] = value
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert store.list() == []
    assert store.active("demo@trusted") is None
    report = store.reconcile()
    assert report["ok"] is False
    assert any(field in str(error) or "physical path" in str(error) for error in report["errors"])
    with pytest.raises(PluginStoreRecordError):
        store.materialize(
            source,
            name="demo",
            marketplace="trusted",
            version="1.0.0",
            source=_source_descriptor(source),
        )


@pytest.mark.parametrize(
    "descriptor",
    [
        {},
        {"source": "unknown", "path": "/tmp/source"},
        {"source": "directory", "path": "relative/source"},
        {"source": "npm", "package": 42},
        {"source": "github", "repo": "owner"},
        {"source": "directory", "path": "/tmp/source", "marketplace": "other"},
    ],
)
def test_source_descriptor_schema_is_validated(
    tmp_path: Path,
    descriptor: dict[str, object],
) -> None:
    source = _plugin_source(tmp_path)
    store = PluginStore(tmp_path / "store")

    with pytest.raises(PluginStoreRecordError):
        store.materialize(
            source,
            name="demo",
            marketplace="trusted",
            version="local",
            source=descriptor,
        )


@pytest.mark.parametrize(
    "segment",
    [
        "../escape",
        r"..\escape",
        ".",
        "",
        "CON",
        "foo:bar",
        "foo/bar",
    ],
)
def test_store_path_segments_reject_traversal_and_windows_aliases(
    tmp_path: Path,
    segment: str,
) -> None:
    store = PluginStore(tmp_path / "store")
    with pytest.raises(ValueError):
        store.version_path(segment, "demo", "local")
    with pytest.raises(ValueError):
        store.version_path("trusted", segment, "local")
    if segment:
        with pytest.raises(ValueError):
            store.version_path("trusted", "demo", segment)


def test_stale_selector_recovers_to_highest_valid_semver(tmp_path: Path) -> None:
    source = _plugin_source(tmp_path)
    store = PluginStore(tmp_path / "store", create=True)
    descriptor = _source_descriptor(source)
    store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="1.9.9",
        source=descriptor,
    )
    store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="2.0.0-beta.1",
        source=descriptor,
        activate=False,
    )

    selector_path = tmp_path / "store" / "trusted" / "demo" / "active.json"
    selector_path.write_text(
        json.dumps(
            {
                "schema_version": ACTIVE_SELECTOR_SCHEMA_VERSION,
                "id": "demo@trusted",
                "version": "99.0.0",
                "path": str(selector_path.parent / "99.0.0"),
            }
        ),
        encoding="utf-8",
    )

    active = store.active("demo@trusted")
    assert active is not None
    assert active.version == "2.0.0-beta.1"
    repaired = json.loads(selector_path.read_text(encoding="utf-8"))
    assert repaired["version"] == "2.0.0-beta.1"
    assert repaired["id"] == "demo@trusted"
    assert store.reconcile()["ok"] is True


def test_stale_selector_prefers_local_version(tmp_path: Path) -> None:
    source = _plugin_source(tmp_path)
    store = PluginStore(tmp_path / "store", create=True)
    descriptor = _source_descriptor(source)
    store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="9.9.9",
        source=descriptor,
    )
    store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="local",
        source=descriptor,
        activate=False,
    )
    selector_path = tmp_path / "store" / "trusted" / "demo" / "active.json"
    selector_path.write_text("{}", encoding="utf-8")

    active = store.active("demo@trusted")
    assert active is not None
    assert active.version == "local"


def test_stale_selector_without_valid_versions_is_reported(tmp_path: Path) -> None:
    source = _plugin_source(tmp_path)
    store = PluginStore(tmp_path / "store", create=True)
    record = store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="1.0.0",
        source=_source_descriptor(source),
    )
    (record.path / ".plugin-store.json").unlink()

    assert store.active("demo@trusted") is None
    report = store.reconcile()
    assert report["ok"] is False
    assert any("missing" in str(error) or "stale" in str(error) for error in report["errors"])


def test_stage_remove_rejects_constraints_and_supports_rollback(tmp_path: Path) -> None:
    source = _plugin_source(tmp_path)
    store = PluginStore(tmp_path / "store", create=True)
    store.materialize(
        source,
        name="demo",
        marketplace="trusted",
        version="local",
        source=_source_descriptor(source),
    )
    with pytest.raises(ValueError):
        store.stage_remove("demo@trusted@^1")

    removal = store.stage_remove("demo@trusted")
    assert removal is not None
    assert not removal.original.exists()
    assert removal.staged.exists()
    store.rollback_remove(removal)
    assert removal.original.exists()
    store.commit_remove(None)


def test_symlinked_store_version_is_fail_closed(tmp_path: Path) -> None:
    source = _plugin_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".minicode-plugin").mkdir()
    (outside / ".minicode-plugin" / "plugin.json").write_text(
        '{"name":"demo"}',
        encoding="utf-8",
    )
    store = PluginStore(tmp_path / "store", create=True)
    base = tmp_path / "store" / "trusted" / "demo"
    base.mkdir(parents=True)
    version = base / "local"
    try:
        version.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    assert store.list() == []
    report = store.reconcile()
    assert report["ok"] is False
    assert any("symlink" in str(error) for error in report["errors"])
