"""Incremental editor indexes retain the admitted executor and scan ownership."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os

import pytest

from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_editor_reads import editor_context
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_editor import ExecutorEditorService
from backend.workspace.project_index_runtime import ProjectIndexRuntime


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.001)


def project(root, count=4):
    for number in range(count):
        (root / f"source-{number}.ts").write_bytes(f"export const value = {number};\n".encode())


@pytest.mark.asyncio
async def test_executor_index_refresh_preserves_bytes_and_removes_deleted_files(executor_host, monkeypatch):
    host = executor_host
    project(host.workspace)
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(host.workspace)
    reads = []
    original = ExecutorEditorService.read_file

    async def read(self, path):
        reads.append(self.resolve(path).name)
        return await original(self, path)

    monkeypatch.setattr(ExecutorEditorService, "read_file", read)
    try:
        async with ExecutorEditorService(editor_context(host)) as service:
            first = await runtime.read(service)
            assert first.complete and len(first.files) == len(reads) == 4
            changed = host.workspace / "source-0.ts"
            metadata = changed.stat()
            changed.write_bytes(b"export const value = 9;\n")
            os.utime(changed, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            runtime.invalidate_path(host.workspace, changed)
            reads.clear()
            current = await runtime.read(service, previous_revision=first.revision, previous_instance_id=first.index_instance_id)
            assert current.complete
            assert next(file for file in current.files if file.path == changed.name).content == "export const value = 9;\n"
            if host.kind == "local":
                assert reads == [changed.name]
                assert current.base_revision == first.revision and len(current.files) == 1
                assert current.revision > first.revision
            else:
                assert len(reads) == 4 and current.revision is None
            changed.rename(host.workspace / "renamed.ts")
            runtime.invalidate_paths((changed, host.workspace / "renamed.ts"))
            renamed = await runtime.read(service, previous_revision=current.revision, previous_instance_id=current.index_instance_id)
            assert any(file.path == "renamed.ts" for file in renamed.files)
            assert all(file.path != changed.name for file in renamed.files)
            if host.kind == "local":
                assert renamed.removed_paths == [changed.name]
            (host.workspace / "source-1.ts").unlink()
            runtime.invalidate_path(host.workspace, host.workspace / "source-1.ts")
            removed = await runtime.read(service)
            assert removed.complete and len(removed.files) == 3
            assert {file.path for file in removed.files} == {"source-2.ts", "source-3.ts", "renamed.ts"}
    finally:
        runtime.release_workspace(host.workspace)
        await runtime.shutdown()
    assert runtime._workspaces == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_changed_permissions_never_reuse_a_wider_index(executor_host):
    from backend.sandbox.policy import (
        FileSystemAccessMode as Access, FileSystemPath, FileSystemSandboxEntry as Entry,
        FileSystemSandboxPolicy, FileSystemSpecialPath as Special, PermissionProfile,
    )

    host = executor_host
    project(host.workspace, 2)
    context = editor_context(host)
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(host.workspace)
    try:
        async with ExecutorEditorService(context) as service:
            first = await runtime.read(service)
        private = host.workspace / "source-0.ts"
        profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
            Entry(FileSystemPath.special(Special.ROOT), Access.READ),
            Entry(FileSystemPath.special(Special.PROJECT_ROOTS), Access.WRITE),
            Entry(FileSystemPath("path", private.as_uri()), Access.DENY),
        ]))
        scope = replace(context.permission.environment_scope, permission_profile=profile, windows_sandbox_level="elevated")
        narrowed = replace(context, permission=replace(context.permission, environment_scope=scope))
        runtime.invalidate_path(host.workspace, host.workspace / "source-1.ts")
        async with ExecutorEditorService(narrowed) as service:
            current = await runtime.read(service, previous_revision=first.revision, previous_instance_id=first.index_instance_id)
        assert current.index_instance_id != first.index_instance_id
        assert current.base_revision is None
        assert [file.path for file in current.files] == ["source-1.ts"]
        assert [issue.path for issue in current.issues] == ["source-0.ts"]
        assert not current.complete
        async with ExecutorEditorService(context) as service:
            widened = await runtime.read(service, previous_revision=current.revision, previous_instance_id=current.index_instance_id)
        assert widened.complete and len(widened.files) == 2
        assert widened.index_instance_id != current.index_instance_id
    finally:
        runtime.release_workspace(host.workspace)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_shared_index_survives_first_reader_and_rebinds_receipt_owner(executor_host, monkeypatch):
    host = executor_host
    project(host.workspace)
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(host.workspace)
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []
    original = ExecutorEditorService.read_file

    async def blocked(self, path):
        reads.append(path)
        entered.set()
        await release.wait()
        return await original(self, path)

    monkeypatch.setattr(ExecutorEditorService, "read_file", blocked)
    first_service = ExecutorEditorService(editor_context(host))
    second_service = ExecutorEditorService(replace(editor_context(host), session_id="second-session", conversation_id="second-owner"))
    first = asyncio.create_task(runtime.read(first_service))
    second = None
    try:
        await entered.wait()
        second = asyncio.create_task(runtime.read(second_service))
        await until(lambda: len(next(iter(next(iter(runtime._workspaces.values())).scopes.values())).scan.readers) == 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await first_service.aclose()
        assert not second.done()
        release.set()
        result = await second
        assert result.complete and len(reads) == 4
        assert result.session_id == "second-session" and result.conversation_id == "second-owner"
        assert all(file.session_id == "second-session" and file.conversation_id == "second-owner" for file in result.files)
    finally:
        release.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        if not first_service.lease.released:
            await first_service.aclose()
        await second_service.aclose()
        runtime.release_workspace(host.workspace)
        await runtime.shutdown()
    assert runtime._workspaces == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_last_index_reader_waits_for_worker_cleanup_and_replacement_gets_fresh_scan(executor_host, monkeypatch):
    host = executor_host
    project(host.workspace, 20)
    runtime = ProjectIndexRuntime()
    entered, cancelled, drain = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ExecutorEditorService.read_file
    held = []

    async def blocked(self, path):
        if not drain.is_set():
            held.append(path)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await drain.wait()
        return await original(self, path)

    monkeypatch.setattr(ExecutorEditorService, "read_file", blocked)
    async with ExecutorEditorService(editor_context(host)) as service:
        first = asyncio.create_task(runtime.read(service))
        replacement = None
        try:
            await entered.wait()
            first.cancel()
            await cancelled.wait()
            assert not first.done() and len(held) <= 8
            replacement = asyncio.create_task(runtime.read(service))
            await until(lambda: next(iter(next(iter(runtime._workspaces.values())).scopes.values())).readers == 2)
            drain.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            result = await replacement
            assert result.complete and len(result.files) == 20
        finally:
            drain.set()
            for task in (first, replacement):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (first, replacement) if task is not None), return_exceptions=True)
            await runtime.shutdown()
    assert runtime._workspaces == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_http_index_returns_one_changed_file_after_watcher_invalidation(editor_server, monkeypatch):
    server = editor_server
    project(server.host.workspace)
    runtime = ProjectIndexRuntime()
    monkeypatch.setattr("backend.workspace.project_index_runtime.project_index_runtime", runtime)
    runtime.retain_workspace(server.host.workspace)
    try:
        first = await server.client.get("/api/workspace/project-index", params=server.params)
        assert first.status_code == 200, first.text
        snapshot = first.json()
        changed = server.host.workspace / "source-2.ts"
        changed.write_bytes(b"export const current = true;\n")
        runtime.invalidate_path(server.host.workspace, changed)
        second = await server.client.get("/api/workspace/project-index", params={**server.params,
            "previous_revision": snapshot["revision"], "previous_instance_id": snapshot["index_instance_id"]})
        assert second.status_code == 200, second.text
        current = second.json()
        assert [file["path"] for file in current["files"]] == ["source-2.ts"]
        assert current["base_revision"] == snapshot["revision"]
        assert current["files"][0]["content"] == "export const current = true;\n"
        assert current["files"][0]["conversation_id"] == server.params["conversation_id"]
        assert (server.view / "same.txt").read_text() == "controller must remain untouched\n"
    finally:
        runtime.release_workspace(server.host.workspace)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_late_reader_observes_mutation_during_an_existing_scan(executor_host, monkeypatch):
    host = executor_host
    project(host.workspace, 2)
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(host.workspace)
    first_read, release = asyncio.Event(), asyncio.Event()
    original = ExecutorEditorService.read_file

    async def blocked(self, path):
        name = self.resolve(path).name
        if name == "source-1.ts" and not release.is_set():
            await release.wait()
        file = await original(self, path)
        if name == "source-0.ts":
            first_read.set()
        return file

    monkeypatch.setattr(ExecutorEditorService, "read_file", blocked)
    async with ExecutorEditorService(editor_context(host)) as service:
        early = asyncio.create_task(runtime.read(service))
        late = None
        try:
            await first_read.wait()
            changed = host.workspace / "source-0.ts"
            changed.write_bytes(b"export const value = 9;\n")
            runtime.invalidate_path(host.workspace, changed)
            late = asyncio.create_task(runtime.read(service))
            await until(lambda: next(iter(next(iter(runtime._workspaces.values())).scopes.values())).readers == 2)
            release.set()
            old, current = await asyncio.gather(early, late)
            assert next(file for file in old.files if file.path == changed.name).content == "export const value = 0;\n"
            assert old.revision is None
            assert next(file for file in current.files if file.path == changed.name).content == "export const value = 9;\n"
            assert current.revision is not None
        finally:
            release.set()
            for task in (early, late):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (early, late) if task is not None), return_exceptions=True)
            runtime.release_workspace(host.workspace)
            await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_replaced_registration_and_dependency_discovery_keep_distinct_snapshots(executor_host, monkeypatch):
    from backend.environments.manager import LocalEnvironment
    from backend.workspace.executor_local import LocalExecutionEnvironmentBackend

    host = executor_host
    project(host.workspace, 2)
    dependency = host.workspace / "node_modules/types/index.d.ts"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"export declare const dependency: string;\n")
    context = editor_context(host)
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(host.workspace)
    replacement = LocalEnvironment(LocalExecutionEnvironmentBackend())
    lease = replacement.borrow()
    reads = []
    original = ExecutorEditorService.read_file

    async def read(self, path):
        reads.append((self.environment, self.resolve(path).name))
        return await original(self, path)

    monkeypatch.setattr(ExecutorEditorService, "read_file", read)
    try:
        async with ExecutorEditorService(context) as service:
            first = await runtime.read(service, include_dependency_paths=True)
            assert first.dependency_paths == ["node_modules/types/index.d.ts"]
            assert len(first.files) == 2
        (host.workspace / "source-0.ts").write_bytes(b"export const changed = true;\n")
        runtime.invalidate_path(host.workspace, host.workspace / "source-1.ts")
        current_context = replace(context, turn_environment=replace(context.turn_environment, lease=lease))
        reads.clear()
        async with ExecutorEditorService(current_context) as service:
            current = await runtime.read(service, previous_revision=first.revision, previous_instance_id=first.index_instance_id)
            assert current.index_instance_id != first.index_instance_id and current.base_revision is None
            assert next(file for file in current.files if file.path == "source-0.ts").content == "export const changed = true;\n"
            assert len(reads) == 2 and all(environment is replacement for environment, _ in reads)
            dependency.unlink()
            new_dependency = dependency.with_name("replaced.d.ts")
            new_dependency.write_bytes(b"export declare const replacement: number;\n")
            runtime.invalidate_paths((dependency, new_dependency))
            updated = await runtime.read(service, include_dependencies=True)
            assert updated.complete and updated.dependency_paths == ["node_modules/types/replaced.d.ts"]
            assert {file.path for file in updated.files} == {"source-0.ts", "source-1.ts", "node_modules/types/replaced.d.ts"}
    finally:
        runtime.release_workspace(host.workspace)
        await runtime.shutdown()
        await lease.aclose()
        await replacement.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_shutdown_releases_an_admitted_scan_before_its_coroutine_starts(executor_host):
    runtime = ProjectIndexRuntime()
    async with ExecutorEditorService(editor_context(executor_host)) as service:
        reader = asyncio.create_task(runtime.read(service))
        await asyncio.sleep(0)
        assert runtime._workspaces
        await runtime.shutdown()
        with pytest.raises(asyncio.CancelledError):
            await reader
    assert runtime._workspaces == {}
