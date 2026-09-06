from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.services.workspace_api_service import search_workspace_directories
from backend.tools.fuzzy_search_tool import FuzzySearchTool
from backend.workspace.api import create_workspace_router
from backend.workspace.file_watcher import WorkspaceFileWatcher
from backend.workspace.fuzzy_search import FuzzySearchEngine, get_global_fuzzy_search
from backend.workspace.service import WorkspaceService


def test_concurrent_refreshes_publish_complete_independent_results(tmp_path, monkeypatch):
    for name in ("alpha.txt", "beta.txt"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    engine = FuzzySearchEngine(tmp_path)
    scandir = os.scandir
    barrier = threading.Barrier(2)

    def synchronized(path):
        if Path(path) == tmp_path:
            barrier.wait(timeout=5)
        return scandir(path)

    with monkeypatch.context() as context:
        context.setattr(os, "scandir", synchronized)
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(lambda _: engine.search(".txt"), range(2)))

    for matches in [*batches, engine.search(".txt")]:
        assert sorted(match.path.name for match in matches) == ["alpha.txt", "beta.txt"]


def test_invalidation_during_scan_is_not_lost(tmp_path, monkeypatch):
    (tmp_path / "existing.txt").write_text("fixture", encoding="utf-8")
    engine = FuzzySearchEngine(tmp_path)
    scandir = os.scandir
    captured = False

    class ScanSnapshot:
        def __init__(self, entries):
            self.entries = iter(entries)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.entries)

    def invalidating_scan(path):
        nonlocal captured
        with scandir(path) as entries:
            snapshot = list(entries)
        if Path(path) == tmp_path and not captured:
            captured = True
            engine.invalidate_cache()
            (tmp_path / "created.txt").write_text("created during scan", encoding="utf-8")
        return ScanSnapshot(snapshot)

    with monkeypatch.context() as context:
        context.setattr(os, "scandir", invalidating_scan)
        assert engine.search("existing")
    assert [match.path.name for match in engine.search("created")] == ["created.txt"]


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_search_prunes_ignored_trees_and_applies_nested_ignore_rules(tmp_path, monkeypatch, kind):
    for name in ("node_modules/package", "generated/deep", "src/private", "src/public"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / ".gitignore").write_text("generated/\n*.tmp\n", encoding="utf-8")
    (tmp_path / "src/.gitignore").write_text("private/\n!keep.tmp\n", encoding="utf-8")
    for name in ("src/keep.tmp", "src/drop.tmp", "src/public/code.py", "src/private/hidden.py"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    visited = []
    scandir = os.scandir

    def counted(path):
        visited.append(Path(path).relative_to(tmp_path).as_posix())
        return scandir(path)

    with monkeypatch.context() as context:
        context.setattr(os, "scandir", counted)
        if kind == "file":
            paths = {match.path.relative_to(tmp_path).as_posix() for match in FuzzySearchEngine(tmp_path).search("src")}
            assert paths == {"src/keep.tmp", "src/public/code.py"}
        else:
            assert {item["path"] for item in search_workspace_directories(tmp_path, "src", 20)} == {"src", "src/public"}
    assert set(visited) == {".", "src", "src/public"}


def test_directory_queries_match_characters_in_order_and_with_multiplicity(tmp_path):
    for name in ("cab", "a_b_c", "ab"):
        (tmp_path / name).mkdir()
    assert [item["path"] for item in search_workspace_directories(tmp_path, "abc", 20)] == ["a_b_c"]
    assert search_workspace_directories(tmp_path, "aa", 20) == []


@pytest.mark.parametrize("directory", [".", "nested"])
def test_directory_read_failures_are_not_reported_as_no_matches(tmp_path, monkeypatch, directory):
    (tmp_path / "nested").mkdir()
    scandir = os.scandir

    def denied(path):
        if Path(path) == tmp_path / directory:
            raise PermissionError("fixture directory read denied")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(PermissionError, match="fixture directory read denied"):
        FuzzySearchEngine(tmp_path).search("example")


@pytest.mark.parametrize("kind", ["file", "folder", "all"])
def test_search_api_returns_read_error_details(tmp_path, monkeypatch, kind):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda root: True)
    scandir = os.scandir

    def denied(path):
        if Path(path) == tmp_path:
            raise PermissionError("fixture directory read denied")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    app = FastAPI()
    app.include_router(create_workspace_router())
    with TestClient(app) as client:
        response = client.get("/api/workspace/search", params={"workspace_root": str(tmp_path), "query": "file", "kind": kind})
    assert response.status_code == 500
    assert "fixture directory read denied" in response.json()["detail"]


def test_search_does_not_cross_directory_links(tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "external.txt").write_text("owned fixture", encoding="utf-8")
    link = root / "external-link"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    assert FuzzySearchEngine(root).search("external") == []
    assert search_workspace_directories(root, "external", 20) == []


@pytest.mark.parametrize("name,query,indices", [("İx", "x", [1]), ("İx", "İx", [0, 1]), ("ΟΣ", "ΟΣ", [0, 1])])
def test_unicode_case_matching_preserves_original_character_indices(tmp_path, name, query, indices):
    (tmp_path / name).write_text("fixture", encoding="utf-8")
    matches = FuzzySearchEngine(tmp_path).search(query)
    assert len(matches) == 1
    assert matches[0].matched_indices == indices


def test_test_file_filter_includes_top_level_test_directories(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/example.py").write_text("fixture", encoding="utf-8")
    (tmp_path / "example.py").write_text("fixture", encoding="utf-8")
    engine = FuzzySearchEngine(tmp_path)
    assert [match.path for match in engine.search("example", include_tests=False)] == [tmp_path / "example.py"]
    assert [match.path for match in engine.search("example", include_tests=True)] == [tmp_path / "example.py", tmp_path / "tests/example.py"]


def test_agent_file_search_does_not_scan_on_the_event_loop(tmp_path, monkeypatch):
    (tmp_path / "example.py").write_text("fixture", encoding="utf-8")
    loop_thread = threading.get_ident()
    scan_threads = []
    scandir = os.scandir

    def recorded(path):
        scan_threads.append(threading.get_ident())
        return scandir(path)

    monkeypatch.setattr(os, "scandir", recorded)
    result = asyncio.run(FuzzySearchTool(tmp_path).execute({"query": "example"}))
    assert not result.is_error
    assert "example.py" in result.content
    assert scan_threads and loop_thread not in scan_threads


def test_watcher_invalidates_ignore_rules_before_notifying_consumers(tmp_path):
    (tmp_path / "example.py").write_text("fixture", encoding="utf-8")
    ignore = tmp_path / ".gitignore"
    ignore.write_text("", encoding="utf-8")
    engine = get_global_fuzzy_search(tmp_path)
    assert engine.search("example")
    ignore.write_text("example.py\n", encoding="utf-8")
    observed = []

    async def exercise():
        watcher = WorkspaceFileWatcher(tmp_path, lambda *_: observed.append(engine.search("example")), stability_threshold=0)
        await watcher._debounced_change(ignore, "modified")
        await asyncio.gather(*watcher._debounce_tasks.values())

    asyncio.run(exercise())
    assert observed == [[]]


def test_editor_ignore_rule_save_invalidates_search_immediately(tmp_path):
    (tmp_path / "example.py").write_text("fixture", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    service = WorkspaceService(get_workspace_root=lambda: tmp_path)
    engine = get_global_fuzzy_search(tmp_path)
    assert engine.search("example")
    service.compare_and_write_file(".gitignore", service.content_hash(""), "example.py\n")
    assert engine.search("example") == []
