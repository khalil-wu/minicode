from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.workspace.recent_projects import RecentProjectStore


def test_listing_reads_one_fresh_snapshot_and_does_not_lock_for_mutation(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    writer = RecentProjectStore(path)
    writer.add(str(tmp_path / "first"), "First")
    reads = []
    read = Path.read_text

    def observe(target, *args, **kwargs):
        if target == path:
            reads.append(True)
        return read(target, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("A project snapshot read must not claim the writer lock")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", observe)
        patch.setattr("backend.workspace.recent_projects.file_mutation_locks", forbidden)
        reader = RecentProjectStore(path)
        assert reads == []
        assert [project.name for project in reader.list()] == ["First"]
        assert reads == [True]
    writer.add(str(tmp_path / "second"), "Second")
    assert [project.name for project in reader.list()] == ["First", "Second"]


def test_read_during_atomic_project_publication_observes_whole_old_then_whole_new_snapshot(tmp_path, monkeypatch):
    import backend.workspace.recent_projects as module

    path = tmp_path / "projects.json"
    writer = RecentProjectStore(path)
    writer.add(str(tmp_path / "first"), "First")
    entered, release = threading.Event(), threading.Event()
    write = module.atomic_write_text

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        write(*args, **kwargs)

    monkeypatch.setattr(module, "atomic_write_text", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(writer.add, str(tmp_path / "second"), "Second")
        try:
            assert entered.wait(3)
            rows = pool.submit(lambda: RecentProjectStore(path).list()).result(timeout=2)
            assert [row.name for row in rows] == ["First"]
        finally:
            release.set()
        pending.result(timeout=4)
    assert [row.name for row in RecentProjectStore(path).list()] == ["First", "Second"]
