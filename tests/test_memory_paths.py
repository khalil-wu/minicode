from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.memory import paths
from backend.memory.file_memory import FileMemory
from backend.memory.generation import MemoryGenerationCoordinator
from backend.memory.job_store import MEMORY_DB_NAME, MemoryJobStore
from backend.memory.local_backend import LocalMemoryBackend
from backend.memory.paths import MemoryBackendError, is_link, resolve_memory_path


@pytest.fixture(params=[
    "symlink",
    pytest.param("junction", marks=pytest.mark.skipif(os.name != "nt", reason="Windows junction")),
])
def directory_link(request):
    created: list[Path] = []

    def create(link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if request.param == "junction":
            subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
            )
        else:
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                if os.name == "nt" and error.winerror == 1314:
                    pytest.skip("Windows symlink privilege is unavailable")
                raise
        created.append(link)

    yield create
    for link in reversed(created):
        if is_link(link):
            if request.param == "junction":
                link.rmdir()
            else:
                link.unlink()


@pytest.fixture(params=["root", "ancestor"])
def linked_root(tmp_path: Path, directory_link, request):
    outside = tmp_path / "outside"
    outside.mkdir()
    if request.param == "root":
        root = tmp_path / "memory"
        target = outside
        directory_link(root, outside)
    else:
        parent = tmp_path / "parent"
        root = parent / "memory"
        target = outside / "memory"
        target.mkdir()
        directory_link(parent, outside)
    return root, target


@pytest.mark.parametrize("reparse_tag, expected", [
    (0xA0000003, True),
    (0, False),
])
def test_windows_junction_metadata_is_not_a_symlink(monkeypatch, reparse_tag, expected):
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=reparse_tag)
    path = SimpleNamespace(lstat=lambda: metadata)
    monkeypatch.setattr(paths, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(paths, "stat", SimpleNamespace(
        S_ISLNK=stat.S_ISLNK, IO_REPARSE_TAG_MOUNT_POINT=0xA0000003,
    ))

    assert is_link(path) is expected


def test_missing_memory_paths_can_be_created(tmp_path: Path) -> None:
    root = tmp_path / "new" / "parent" / "memory"
    filename = "2026-09-07T00-00-00-note.md"
    target = root / "extensions" / "ad_hoc" / "notes" / filename

    assert resolve_memory_path(root, target.relative_to(root)) == target
    assert not root.exists()
    LocalMemoryBackend(root).add_ad_hoc_note(filename=filename, note="new note")
    assert target.read_text(encoding="utf-8") == "new note"
    assert FileMemory(root).read_file("MEMORY.md") == ""


def test_memory_path_rejects_escape_and_non_directory_components(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    root.mkdir()
    (root / "file.md").write_text("memory", encoding="utf-8")

    for relative in ("../outside", tmp_path / "outside"):
        with pytest.raises(MemoryBackendError, match="must stay within"):
            resolve_memory_path(root, relative)
    with pytest.raises(MemoryBackendError, match="non-directory"):
        resolve_memory_path(root, "file.md/child.md")


def test_initialization_rejects_linked_root_before_creating_files_or_locks(linked_root) -> None:
    root, target = linked_root

    with pytest.raises(MemoryBackendError, match="symlink or junction"):
        FileMemory(root)

    assert list(target.iterdir()) == []
    assert not (target.parent / ".memory.reset.lock").exists()


@pytest.mark.parametrize("operation", ["list", "read", "search", "note"])
def test_local_memory_rejects_linked_root(linked_root, operation: str) -> None:
    root, target = linked_root
    marker = target / "marker.md"
    marker.write_text("OUTSIDE_MEMORY_MARKER", encoding="utf-8")
    backend = LocalMemoryBackend(root)
    operations = {
        "list": backend.list,
        "read": lambda: backend.read(path="marker.md"),
        "search": lambda: backend.search(queries=["OUTSIDE_MEMORY_MARKER"]),
        "note": lambda: backend.add_ad_hoc_note(
            filename="2026-09-07T00-00-00-note.md", note="do not write outside"
        ),
    }

    with pytest.raises(MemoryBackendError, match="symlink or junction"):
        operations[operation]()

    assert list(target.iterdir()) == [marker]
    assert marker.read_text(encoding="utf-8") == "OUTSIDE_MEMORY_MARKER"


def test_database_rejects_linked_root_before_creating_files(linked_root) -> None:
    root, target = linked_root
    store = MemoryJobStore(root / MEMORY_DB_NAME)

    with pytest.raises(MemoryBackendError, match="symlink or junction"):
        store.list_stage1_outputs(limit=256, max_unused_days=30)

    assert list(target.iterdir()) == []


def test_phase2_rejects_linked_root_before_summary_cleanup(linked_root) -> None:
    root, target = linked_root
    summaries = target / "rollout_summaries"
    summaries.mkdir()
    marker = summaries / "keep.md"
    marker.write_text("outside summary", encoding="utf-8")
    coordinator = object.__new__(MemoryGenerationCoordinator)
    coordinator.memory_root = root

    with pytest.raises(MemoryBackendError, match="symlink or junction"):
        coordinator._sync_phase2_inputs([])

    assert marker.read_text(encoding="utf-8") == "outside summary"
    assert not (target / "raw_memories.md").exists()


def test_listing_and_search_prune_linked_directories(tmp_path: Path, directory_link) -> None:
    root = tmp_path / "memory"
    root.mkdir()
    (root / "inside.md").write_text("inside marker", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.md").write_text("outside marker", encoding="utf-8")
    directory_link(root / "linked", outside)
    backend = LocalMemoryBackend(root)

    assert backend.list()["entries"] == [{"path": "inside.md", "entry_type": "file"}]
    assert [item["path"] for item in backend.search(queries=["marker"])["matches"]] == ["inside.md"]
    with pytest.raises(MemoryBackendError, match="symlink or junction"):
        backend.read(path="linked/outside.md")


@pytest.mark.parametrize("operation", ["initialize", "note", "sync", "git_prepare", "git_commit"])
def test_memory_writers_reject_linked_children(tmp_path: Path, directory_link, operation: str) -> None:
    root = tmp_path / "memory"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.md"
    marker.write_text("outside", encoding="utf-8")
    coordinator = object.__new__(MemoryGenerationCoordinator)
    coordinator.memory_root = root
    relative, execute = {
        "initialize": ("extensions", lambda: FileMemory(root)),
        "note": ("extensions/ad_hoc/notes", lambda: LocalMemoryBackend(root).add_ad_hoc_note(
            filename="2026-09-07T00-00-00-note.md", note="note"
        )),
        "sync": ("rollout_summaries", lambda: coordinator._sync_phase2_inputs([])),
        "git_prepare": (".git", coordinator._ensure_git_workspace),
        "git_commit": (".git", coordinator._commit_git_baseline),
    }[operation]
    directory_link(root / relative, outside)

    with pytest.raises(MemoryBackendError, match="symlink or junction"):
        execute()

    assert list(outside.iterdir()) == [marker]
    assert marker.read_text(encoding="utf-8") == "outside"
    assert not (root / "MEMORY.md").exists()


@pytest.mark.parametrize("operation", ["reset", "lock"])
def test_reset_and_shared_lock_recheck_replaced_ancestors(tmp_path: Path, directory_link, operation: str) -> None:
    parent = tmp_path / "parent"
    root = parent / "memory"
    memory = FileMemory(root)
    parent.rename(tmp_path / "original")
    outside = tmp_path / "outside"
    target = outside / "memory"
    target.mkdir(parents=True)
    marker = target / "keep.md"
    marker.write_text("outside", encoding="utf-8")
    directory_link(parent, outside)

    with pytest.raises(MemoryBackendError, match="symlink or junction"):
        if operation == "reset":
            memory.reset()
        else:
            with memory.reset_lock.acquire(timeout=1):
                pass

    assert marker.read_text(encoding="utf-8") == "outside"
    assert not (outside / ".memory.reset.lock").exists()


def test_reset_counts_links_without_following_them(tmp_path: Path, directory_link) -> None:
    root = tmp_path / "memory"
    memory = FileMemory(root)
    outside = tmp_path / "outside"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    marker = nested / "keep.md"
    marker.write_text("outside", encoding="utf-8")
    directory_link(root / "linked", outside)

    result = memory.reset()

    assert result.files_removed == 2
    assert result.directories_removed == 3
    assert result.cleanup_pending is False
    assert marker.read_text(encoding="utf-8") == "outside"
    assert memory.read_file("MEMORY.md") == ""
