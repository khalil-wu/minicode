from __future__ import annotations

import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event

import pytest

from backend.plugins.manager import MarketplaceRegistry
from backend.plugins.materializer import MaterializationError


def _plugin(root: Path, name: str = "demo-plugin") -> Path:
    plugin = root / "plugins" / name
    (plugin / ".minicode-plugin").mkdir(parents=True)
    (plugin / ".minicode-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    return plugin


def _write_catalog(
    root: Path,
    *,
    layout: str = ".minicode-plugin/marketplace.json",
    name: str = "demo-market",
    source: str = "./plugins/demo-plugin",
    policy: dict[str, object] | None = None,
) -> Path:
    manifest = root / layout
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "plugins": [
            {
                "name": "demo-plugin",
                "source": {"source": "local", "path": source},
                "policy": policy or {},
            }
        ],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def _write_catalog_archive(
    archive: Path,
    *,
    name: str = "demo-market",
    marker: str,
) -> Path:
    payload = {
        "name": name,
        "plugins": [
            {
                "name": "demo-plugin",
                "source": {"source": "local", "path": "./plugins/demo-plugin"},
            }
        ],
    }
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "bundle/.minicode-plugin/marketplace.json",
            json.dumps(payload),
        )
        handle.writestr(
            "bundle/plugins/demo-plugin/.minicode-plugin/plugin.json",
            json.dumps({"name": "demo-plugin"}),
        )
        handle.writestr("bundle/plugins/demo-plugin/marker.txt", marker)
    return archive


def test_minicode_marketplace_resolves_root_relative_plugins(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _write_catalog(
        tmp_path,
        policy={
            "installation": "AVAILABLE",
            "authentication": "ON_USE",
            "products": ["MINICODE"],
        },
    )
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "directory", "path": str(tmp_path)})

    refreshed = registry.refresh("demo-market")
    plugin = refreshed["plugins"][0]
    assert Path(plugin["path"]) == (tmp_path / "plugins" / "demo-plugin").resolve()
    assert plugin["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_USE",
        "products": ["MINICODE"],
    }

    _plugin_record, installed_path = registry.materialize_plugin(
        "demo-market", "demo-plugin"
    )
    assert installed_path == (tmp_path / "plugins" / "demo-plugin").resolve()


def test_marketplace_registration_records_minicode_native_provenance(tmp_path: Path) -> None:
    registry = MarketplaceRegistry(tmp_path / "registry.json")

    record = registry.add(
        "native-market",
        {"source": "directory", "path": str(tmp_path)},
    )

    assert record["provenance"]["parser"] == "minicode-native-source-parser"


def test_marketplace_manifest_name_must_match_registered_name(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _write_catalog(tmp_path, name="declared-name")
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("registered-name", {"source": "directory", "path": str(tmp_path)})

    with pytest.raises(MaterializationError, match="does not match registered"):
        registry.refresh("registered-name")

    failed = registry.get("registered-name")
    assert failed is not None
    assert failed["status"] == "registered"
    assert "error" not in failed


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        ({"installation": "NOT_AVAILABLE"}, "not available for install"),
        ({"products": []}, "not available for product"),
    ],
)
def test_marketplace_plugin_policy_is_enforced_at_install(
    tmp_path: Path, policy: dict[str, object], message: str
) -> None:
    _plugin(tmp_path)
    _write_catalog(tmp_path, policy=policy)
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "directory", "path": str(tmp_path)})
    registry.refresh("demo-market")

    with pytest.raises(MaterializationError, match=message):
        registry.materialize_plugin("demo-market", "demo-plugin")


def test_marketplace_lists_codex_product_but_rejects_install_for_minicode(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _write_catalog(tmp_path, policy={"products": ["CODEX"]})
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "directory", "path": str(tmp_path)})

    plugin = registry.refresh("demo-market")["plugins"][0]
    assert plugin["installation"]["available"] is False
    assert "not available for product MINICODE" in plugin["installation"]["reason"]
    with pytest.raises(MaterializationError, match="not available for product MINICODE"):
        registry.materialize_plugin("demo-market", "demo-plugin")


def test_marketplace_local_plugin_path_cannot_escape_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _plugin(outside)
    root = tmp_path / "marketplace"
    root.mkdir()
    _write_catalog(root, source="../outside/plugins/demo-plugin")
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "directory", "path": str(root)})

    registry.refresh("demo-market")
    with pytest.raises(MaterializationError, match="stay within"):
        registry.materialize_plugin("demo-market", "demo-plugin")


def test_marketplace_absolute_plugin_path_outside_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _plugin(outside)
    root = tmp_path / "marketplace"
    root.mkdir()
    _write_catalog(root, source=str((outside / "plugins" / "demo-plugin").resolve()))
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "directory", "path": str(root)})
    registry.refresh("demo-market")

    with pytest.raises(MaterializationError, match="stay within"):
        registry.materialize_plugin("demo-market", "demo-plugin")


def test_invalid_refresh_preserves_registry_bytes_and_active_snapshot(tmp_path: Path) -> None:
    archive = _write_catalog_archive(
        tmp_path / "marketplace.zip",
        marker="known-good",
    )
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "file", "path": str(archive)})
    ready = registry.refresh("demo-market")
    active_root = Path(ready["materialized_path"])
    registry_before = registry.path.read_bytes()

    _write_catalog_archive(
        archive,
        name="wrong-marketplace",
        marker="unvalidated-new",
    )
    with pytest.raises(MaterializationError, match="does not match registered"):
        registry.refresh("demo-market")

    assert registry.path.read_bytes() == registry_before
    assert (
        active_root / "plugins" / "demo-plugin" / "marker.txt"
    ).read_text(encoding="utf-8") == "known-good"
    _plugin_record, installed_path = registry.materialize_plugin(
        "demo-market", "demo-plugin"
    )
    assert (installed_path / "marker.txt").read_text(encoding="utf-8") == "known-good"
    assert not list(active_root.parent.glob(".marketplace-stage-*"))
    assert not list(active_root.parent.glob(".demo-market.*.activate"))
    assert not list(active_root.parent.glob(".demo-market.*.backup"))


def test_successful_refresh_rebases_plugins_to_activated_root(tmp_path: Path) -> None:
    archive = _write_catalog_archive(
        tmp_path / "marketplace.zip",
        marker="version-one",
    )
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "file", "path": str(archive)})
    first = registry.refresh("demo-market")
    active_root = Path(first["materialized_path"])

    _write_catalog_archive(archive, marker="version-two")
    refreshed = registry.refresh("demo-market")

    plugin = refreshed["plugins"][0]
    assert Path(plugin["path"]) == (
        active_root / "plugins" / "demo-plugin"
    ).resolve()
    assert ".marketplace-stage-" not in plugin["path"]
    assert (Path(plugin["path"]) / "marker.txt").read_text(
        encoding="utf-8"
    ) == "version-two"
    assert refreshed["status"] == "ready"
    assert refreshed["provenance"]["materialized"]["path"] == str(active_root)


def test_registry_commit_failure_rolls_back_activated_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_catalog_archive(
        tmp_path / "marketplace.zip",
        marker="known-good",
    )
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "file", "path": str(archive)})
    ready = registry.refresh("demo-market")
    active_root = Path(ready["materialized_path"])
    registry_before = registry.path.read_bytes()
    _write_catalog_archive(archive, marker="must-roll-back")

    def fail_registry_write(_records: list[dict[str, object]]) -> None:
        raise OSError("simulated registry write failure")

    monkeypatch.setattr(registry, "_write", fail_registry_write)
    with pytest.raises(OSError, match="simulated registry write failure"):
        registry.refresh("demo-market")

    assert registry.path.read_bytes() == registry_before
    assert (
        active_root / "plugins" / "demo-plugin" / "marker.txt"
    ).read_text(encoding="utf-8") == "known-good"
    assert not list(active_root.parent.glob(".demo-market.*.activate"))
    assert not list(active_root.parent.glob(".demo-market.*.backup"))


def test_concurrent_registry_mutations_do_not_lose_records(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    alpha_root.mkdir()
    beta_root.mkdir()
    barrier = Barrier(2)

    def add(name: str, root: Path) -> None:
        barrier.wait()
        MarketplaceRegistry(registry_path).add(
            name,
            {"source": "directory", "path": str(root)},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(add, "alpha", alpha_root),
            pool.submit(add, "beta", beta_root),
        ]
        for future in futures:
            future.result()

    assert {item["name"] for item in MarketplaceRegistry(registry_path).list()} == {
        "alpha",
        "beta",
    }


@pytest.mark.parametrize(
    "foreign_layout",
    [
        ".codex-plugin/marketplace.json",
        ".other-plugin/marketplace.json",
    ],
)
def test_marketplace_ignores_foreign_manifest_layouts(
    tmp_path: Path, foreign_layout: str
) -> None:
    _plugin(tmp_path)
    _write_catalog(
        tmp_path,
        layout=foreign_layout,
    )
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "directory", "path": str(tmp_path)})

    with pytest.raises(MaterializationError, match="marketplace manifest not found"):
        registry.refresh("demo-market")


@pytest.mark.parametrize("layout", [
    ".minicode-plugin/marketplace.json", ".agents/plugins/marketplace.json",
    ".agents/plugins/api_marketplace.json", ".claude-plugin/marketplace.json",
    ".cursor-plugin/marketplace.json",
])
def test_desktop_add_reads_marketplace_identity_and_catalog(tmp_path: Path, layout: str) -> None:
    source_root = tmp_path / "catalog"
    _plugin(source_root)
    _write_catalog(source_root, layout=layout)
    registry = MarketplaceRegistry(tmp_path / "state" / "registry.json")

    record, already_added = registry.add_source(str(source_root))

    assert not already_added
    assert record["name"] == "demo-market"
    assert record["status"] == "ready"
    assert record["plugins"][0]["id"] == "demo-plugin@demo-market"
    assert Path(record["plugins"][0]["path"]) == source_root / "plugins" / "demo-plugin"
    duplicate, already_added = registry.add_source(str(source_root))
    assert already_added
    assert duplicate["name"] == "demo-market"
    assert len(registry.list()) == 1


def test_desktop_add_rejects_collision_without_registering_another_source(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_catalog(first)
    _write_catalog(second)
    registry = MarketplaceRegistry(tmp_path / "state" / "registry.json")
    registry.add_source(str(first))

    with pytest.raises(MaterializationError, match="already registered with a different source"):
        registry.add_source(str(second))

    assert len(registry.list()) == 1
    assert registry.list()[0]["source"]["path"] == str(first)


def test_slow_source_materialization_does_not_lock_unrelated_registry_changes(tmp_path: Path, monkeypatch) -> None:
    from backend.plugins import manager

    source = tmp_path / "catalog"
    _plugin(source)
    _write_catalog(source)
    registry = MarketplaceRegistry(tmp_path / "state" / "registry.json")
    other = MarketplaceRegistry(registry.path)
    entered, release = Event(), Event()
    materialize = manager.materialize_source

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return materialize(*args, **kwargs)

    monkeypatch.setattr(manager, "materialize_source", delayed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        installing = executor.submit(registry.add_source, str(source))
        try:
            assert entered.wait(3)
            independent = executor.submit(other.add, "independent", {"source": "directory", "path": str(tmp_path)})
            assert independent.result(timeout=2)["name"] == "independent"
        finally:
            release.set()
        assert installing.result(timeout=5)[0]["name"] == "demo-market"
    assert {item["name"] for item in registry.list()} == {"independent", "demo-market"}


def test_real_http_download_does_not_hold_registry_lock(tmp_path: Path, monkeypatch) -> None:
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from backend.plugins import manager

    archive = _write_catalog_archive(tmp_path / "marketplace.zip", marker="downloaded")
    payload = archive.read_bytes()
    entered, release = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            assert release.wait(5)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    materialize = manager.materialize_source

    def local_download(source, destination, **kwargs):
        kwargs["urlopen"] = lambda _url, timeout: opener.open(f"http://127.0.0.1:{server.server_port}/marketplace.zip", timeout=timeout)
        return materialize(source, destination, **kwargs)

    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "url", "url": "https://example.invalid/marketplace.zip"})
    monkeypatch.setattr(manager, "materialize_source", local_download)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            refreshing = executor.submit(registry.refresh, "demo-market")
            try:
                assert entered.wait(3)
                other = executor.submit(registry.add, "independent", {"source": "directory", "path": str(tmp_path)})
                assert other.result(timeout=2)["name"] == "independent"
            finally:
                release.set()
            ready = refreshing.result(timeout=5)
        assert (Path(ready["materialized_path"]) / "plugins" / "demo-plugin" / "marker.txt").read_text(encoding="utf-8") == "downloaded"
        assert {item["name"] for item in registry.list()} == {"independent", "demo-market"}
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_refresh_does_not_replace_marketplace_changed_during_materialization(tmp_path: Path, monkeypatch) -> None:
    from backend.plugins import manager

    archive = _write_catalog_archive(tmp_path / "marketplace.zip", marker="old")
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "file", "path": str(archive)})
    old_root = Path(registry.refresh("demo-market")["materialized_path"])
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    entered, release = Event(), Event()
    materialize = manager.materialize_source

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return materialize(*args, **kwargs)

    monkeypatch.setattr(manager, "materialize_source", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        refreshing = executor.submit(registry.refresh, "demo-market")
        try:
            assert entered.wait(3)
            registry.remove("demo-market")
            registry.add("demo-market", {"source": "directory", "path": str(replacement)})
        finally:
            release.set()
        with pytest.raises(MaterializationError, match="changed while refresh"):
            refreshing.result(timeout=5)
    assert registry.get("demo-market")["source"]["path"] == str(replacement)
    assert (old_root / "plugins" / "demo-plugin" / "marker.txt").read_text(encoding="utf-8") == "old"


def test_refresh_does_not_retarget_while_waiting_for_its_destination(tmp_path: Path, monkeypatch) -> None:
    archive = _write_catalog_archive(tmp_path / "marketplace.zip", marker="old")
    registry = MarketplaceRegistry(tmp_path / "registry.json")
    registry.add("demo-market", {"source": "file", "path": str(archive)})
    destination = registry._materialized_root("demo-market")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    attempted = Event()
    original_scope = registry._materialization_scope

    @contextmanager
    def observed_scope(path):
        attempted.set()
        with original_scope(path):
            yield

    with ThreadPoolExecutor(max_workers=1) as executor:
        with original_scope(destination):
            monkeypatch.setattr(registry, "_materialization_scope", observed_scope)
            refreshing = executor.submit(registry.refresh, "demo-market")
            assert attempted.wait(3)
            registry.remove("demo-market")
            registry.add("demo-market", {"source": "directory", "path": str(replacement)})
        with pytest.raises(MaterializationError, match="changed while refresh"):
            refreshing.result(timeout=5)


def test_remote_plugin_materialization_rechecks_marketplace_after_unrelated_change(tmp_path: Path, monkeypatch) -> None:
    from backend.plugins import manager

    root = tmp_path / "catalog"
    manifest = root / ".minicode-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "demo-market", "plugins": [{"name": "remote-plugin",
        "source": {"source": "git", "url": "https://example.invalid/plugin.git"}}]}), encoding="utf-8")
    staged_plugin = tmp_path / "staged-plugin"
    staged_plugin.mkdir()
    (staged_plugin / "fixture.txt").write_text("plugin", encoding="utf-8")
    registry = MarketplaceRegistry(tmp_path / "state" / "registry.json")
    registry.add("demo-market", {"source": "directory", "path": str(root)})
    registry.refresh("demo-market")
    entered, release = Event(), Event()
    materialize = manager.materialize_source

    def delayed(_source, destination, **kwargs):
        entered.set()
        assert release.wait(5)
        return materialize({"source": "directory", "path": str(staged_plugin)}, destination,
                           copy_local_directory=True, **kwargs)

    monkeypatch.setattr(manager, "materialize_source", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        installing = executor.submit(registry.materialize_plugin, "demo-market", "remote-plugin")
        try:
            assert entered.wait(3)
            registry.add("independent", {"source": "directory", "path": str(tmp_path)})
            registry.remove("demo-market")
        finally:
            release.set()
        with pytest.raises(MaterializationError, match="changed while materialization"):
            installing.result(timeout=5)
    assert registry.get("demo-market") is None
    assert registry.get("independent") is not None
    assert not registry._plugin_materialized_root("demo-market", "remote-plugin").exists()


def test_registry_lock_timeout_is_reported_as_a_marketplace_error(tmp_path: Path) -> None:
    first = MarketplaceRegistry(tmp_path / "registry.json")
    second = MarketplaceRegistry(first.path)
    second._mutation_lock.timeout = 0.1
    with first._mutation_lock:
        with pytest.raises(MaterializationError, match="Timed out waiting for the marketplace registry"):
            second.add("other", {"source": "directory", "path": str(tmp_path)})


def test_concurrent_adds_of_the_same_source_share_the_registered_result(tmp_path: Path) -> None:
    source = tmp_path / "catalog"
    _plugin(source)
    _write_catalog(source)
    registry_path = tmp_path / "state" / "registry.json"
    barrier = Barrier(2)

    def add():
        registry = MarketplaceRegistry(registry_path)
        barrier.wait()
        return registry.add_source(str(source))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: add(), range(2)))
    assert sorted(already for _, already in outcomes) == [False, True]
    assert len(MarketplaceRegistry(registry_path).list()) == 1


@pytest.mark.parametrize("options", [{"ref_name": "main"}, {"sparse_paths": ["plugins/review"]}])
def test_desktop_add_reports_git_only_options_on_local_sources(tmp_path: Path, options: dict) -> None:
    registry = MarketplaceRegistry(tmp_path / "state" / "registry.json")
    with pytest.raises(ValueError, match="only supported for git"):
        registry.add_source(str(tmp_path), **options)
    assert not registry.path.exists()


def test_desktop_add_api_preserves_source_contract_and_reports_validation_errors(tmp_path: Path, monkeypatch) -> None:
    from fastapi import HTTPException, Response
    from backend.api import routes_skills
    from backend.api.models import PluginMarketplaceRequest
    from backend.plugins.policy import ManagedPluginPolicy

    source_root = tmp_path / "catalog"
    _write_catalog(source_root, layout=".agents/plugins/marketplace.json")
    registry = MarketplaceRegistry(tmp_path / "state" / "registry.json")
    policy = ManagedPluginPolicy({}, None, (), {})
    monkeypatch.setattr(routes_skills, "MarketplaceRegistry", lambda: registry)
    monkeypatch.setattr(routes_skills, "load_config_layer_stack", lambda **_kwargs: None)
    monkeypatch.setattr("backend.plugins.policy._plugin_policy_from_stack", lambda _stack: policy)

    request = PluginMarketplaceRequest(source=str(source_root), ref_name=None, sparse_paths=None)
    result = routes_skills.add_plugin_marketplace_api(request, Response())
    assert result["marketplace"]["name"] == "demo-market"
    assert result["marketplace"]["status"] == "ready"
    assert result["already_added"] is False

    with pytest.raises(HTTPException) as captured:
        routes_skills.add_plugin_marketplace_api(PluginMarketplaceRequest(source=str(source_root), ref_name="main"), Response())
    assert captured.value.status_code == 400
    assert "only supported for git" in captured.value.detail


def test_remote_plugin_cache_identity_cannot_collide_on_hyphens(tmp_path: Path) -> None:
    registry = MarketplaceRegistry(tmp_path / "registry.json")

    first = registry._plugin_materialized_root("a-b", "c")
    second = registry._plugin_materialized_root("a", "b-c")

    assert first != second
    assert first.parts[-2:] == ("a-b", "c")
    assert second.parts[-2:] == ("a", "b-c")
