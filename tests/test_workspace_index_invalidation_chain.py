from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.sandbox import SandboxPolicy
from backend.workspace.project_index_runtime import ProjectIndexRuntime
from backend.workspace.service import WorkspaceService
from backend.ws.session_lifecycle import SessionLifecycle


def project(root: Path, name: str = "value.ts") -> WorkspaceService:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("export const value = 1;", encoding="utf-8")
    return WorkspaceService(lambda: root)


@pytest.mark.asyncio
async def test_unknown_write_authority_invalidates_parent_child_and_additional_mounts_only(tmp_path):
    parent = tmp_path / "project"
    child = parent / "nested"
    additional = tmp_path / "additional"
    unrelated = tmp_path / "unrelated"
    services = [project(root) for root in [parent, child, additional, unrelated]]
    runtime = ProjectIndexRuntime()
    for service in services:
        runtime.retain_workspace(service.workspace_root_path())
    try:
        await asyncio.gather(*(runtime.read(service) for service in services))
        authority = SandboxPolicy(workspace_root=child, writable_roots=(child, additional), readable_roots=(tmp_path,))
        runtime.invalidate_all(mutation_policy=authority)
        entries = {entry.root: entry for entry in runtime._workspaces.values()}
        assert [entries[root].revision for root in [parent, child, additional, unrelated]] == [1, 1, 1, 0]
        assert entries[parent].dirty == {"nested/value.ts"}
        assert entries[unrelated].complete
        runtime.invalidate_all(mutation_policy=SandboxPolicy.bypass())
        assert entries[unrelated].revision == 1
        assert not entries[unrelated].complete
    finally:
        for service in services:
            runtime.release_workspace(service.workspace_root_path())
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_numeric_snapshot_revision_cannot_be_reused_after_runtime_instance_recreation(tmp_path):
    service = project(tmp_path)
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(tmp_path)
    original = await runtime.read(service)
    runtime.release_workspace(tmp_path)
    (tmp_path / "value.ts").write_text("export const value = 2;", encoding="utf-8")
    runtime.retain_workspace(tmp_path)
    try:
        recreated = await runtime.read(service)
        assert recreated.revision == original.revision
        assert recreated.index_instance_id != original.index_instance_id
        replayed_cursor = await runtime.read(service, previous_revision=original.revision,
            previous_instance_id=original.index_instance_id)
        assert replayed_cursor.base_revision is None
        assert replayed_cursor.files[0].content == "export const value = 2;"
    finally:
        runtime.release_workspace(tmp_path)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_real_dependency_write_notifies_mounted_session_without_fabricating_file_changed(tmp_path, monkeypatch):
    service = project(tmp_path)
    declaration = tmp_path / "node_modules/library/index.d.ts"
    declaration.parent.mkdir(parents=True)
    declaration.write_text("export declare const value: string;", encoding="utf-8")
    runtime = ProjectIndexRuntime()
    monkeypatch.setattr("backend.workspace.project_index_runtime.project_index_runtime", runtime)
    payloads = []
    delivered = asyncio.Event()

    async def send_payload(payload, **_kwargs):
        payloads.append(payload)
        delivered.set()
        return True

    session = SimpleNamespace(session_id="owned-index-session", active_conversation_id="owned-conversation", send_payload=send_payload)
    lifecycle = SessionLifecycle(session)
    current_root = [tmp_path]
    lifecycle.workspace_root_for_conversation = lambda: current_root[0]
    lifecycle.start_file_watcher()
    watcher = lifecycle.file_watcher
    assert watcher is not None and watcher.is_running()
    mounted = next(iter(runtime._workspaces.values()))
    try:
        initial = await runtime.read(service, include_dependency_paths=True)
        declaration.write_text("export declare const value: number;", encoding="utf-8")
        await asyncio.wait_for(delivered.wait(), timeout=5)
        event = next(payload for payload in payloads if payload["type"] == "workspace.project_index.invalidated")
        assert event["conversation_id"] == "owned-conversation"
        assert event["workspace_root"] == str(tmp_path)
        assert event["index_instance_id"] == initial.index_instance_id
        assert event["revision"] >= 1
        assert not any(payload["type"] == "file.changed" for payload in payloads)
        current_root[0] = tmp_path / "another-workspace"
        count = len(payloads)
        await lifecycle.on_project_index_invalidated(tmp_path, initial.index_instance_id, 100)
        assert len(payloads) == count
    finally:
        watcher.stop()
        released_before_shutdown = not mounted.listeners and mounted.owners == 0 and not runtime._workspaces
        await runtime.shutdown()
    assert released_before_shutdown
