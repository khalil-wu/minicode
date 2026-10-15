from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import __main__ as entrypoint
from backend.workspace.project_index import build_project_index
from backend.workspace.project_index_runtime import ProjectIndexRuntime
from backend.workspace.service import WorkspaceService


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("development,owned", [(False, False), (True, False), (True, True)])
def test_reload_is_explicit_development_only(monkeypatch, platform, development, owned):
    monkeypatch.setattr(entrypoint.sys, "platform", platform)
    monkeypatch.setenv("MINICODE_BACKEND_RELOAD", "1" if development else "0")
    monkeypatch.setenv("MINICODE_DESKTOP_OWNED", "1" if owned else "0")
    assert entrypoint._should_enable_reload() is (platform != "win32" and development and not owned)


def test_dependency_inventory_does_not_read_or_validate_unused_dependency_bodies(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.ts").write_text('export const value = 1;', encoding="utf-8")
    dependency = tmp_path / "node_modules/library/index.d.ts"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"\xff\xfe")
    service = WorkspaceService(lambda: tmp_path)
    original = service._read_indexed_file_snapshot
    reads = []

    def observed(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(service, "_read_indexed_file_snapshot", observed)
    snapshot = build_project_index(service, include_dependency_paths=True)
    assert snapshot.complete and not snapshot.issues
    assert [file.path for file in snapshot.files] == ["src/main.ts"]
    assert snapshot.dependency_paths == ["node_modules/library/index.d.ts"]
    assert reads == [tmp_path / "src/main.ts"]
    complete = build_project_index(service, include_dependencies=True)
    assert not complete.complete and complete.issues[0].status_code == 400


@pytest.mark.asyncio
async def test_known_file_edit_reads_only_that_file_without_tree_scan_and_delivers_owned_delta(tmp_path, monkeypatch):
    for name in ["first.ts", "second.ts"]:
        (tmp_path / name).write_text("export const value = 1;", encoding="utf-8")
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(tmp_path)
    service = WorkspaceService(lambda: tmp_path)
    original = service._read_indexed_file_snapshot
    reads = []

    def observed(path, **kwargs):
        reads.append(path.name)
        return original(path, **kwargs)

    monkeypatch.setattr(service, "_read_indexed_file_snapshot", observed)
    try:
        initial = await runtime.read(service)
        assert initial.revision is not None and len(initial.files) == 2
        reads.clear()
        changed = tmp_path / "first.ts"
        changed.write_text("export const value = 2;", encoding="utf-8")
        runtime.invalidate_paths((changed,))

        def no_scan(*_args, **_kwargs):
            raise AssertionError("An existing content edit must not enumerate the workspace tree.")

        with monkeypatch.context() as local:
            local.setattr("backend.workspace.project_index._project_files", no_scan)
            delta = await runtime.read(service, previous_revision=initial.revision, previous_instance_id=initial.index_instance_id)
        assert reads == ["first.ts"]
        assert delta.base_revision == initial.revision
        assert [file.path for file in delta.files] == ["first.ts"]
        assert delta.files[0].content == "export const value = 2;"
        assert delta.removed_paths == []
        stale = await runtime.read(service, previous_revision=initial.revision, previous_instance_id=initial.index_instance_id)
        assert stale.base_revision is None and len(stale.files) == 2
    finally:
        runtime.release_workspace(tmp_path)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_delete_delta_and_shell_invalidation_are_workspace_owned(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(); second.mkdir()
    for root in [first, second]:
        (root / "value.ts").write_text("export const value = 1;", encoding="utf-8")
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(first); runtime.retain_workspace(second)
    first_service = WorkspaceService(lambda: first)
    second_service = WorkspaceService(lambda: second)
    try:
        initial, _ = await asyncio.gather(runtime.read(first_service), runtime.read(second_service))
        second_entry = next(entry for entry in runtime._workspaces.values() if entry.root == second)
        second_revision = second_entry.revision
        (first / "value.ts").unlink()
        runtime.invalidate_paths((first / "value.ts",))
        removed = await runtime.read(first_service, previous_revision=initial.revision, previous_instance_id=initial.index_instance_id)
        assert removed.complete and removed.files == []
        assert removed.removed_paths == ["value.ts"]
        runtime.invalidate_all(first)
        assert second_entry.revision == second_revision
        assert second_entry.complete
    finally:
        runtime.release_workspace(first); runtime.release_workspace(second)
        await runtime.shutdown()
