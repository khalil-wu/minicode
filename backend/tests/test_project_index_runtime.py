from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI

from backend.workspace.api import create_workspace_router
from backend.workspace.project_index_runtime import ProjectIndexRuntime
from backend.workspace.service import WorkspaceService


async def _until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


def _project(root: Path, count: int = 1) -> WorkspaceService:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"source-{index}.ts").write_text(f"export const value{index} = {index};", encoding="utf-8")
    return WorkspaceService(lambda: root)


@pytest.mark.asyncio
async def test_same_workspace_consumers_share_real_scan_and_cancel_independently(tmp_path, monkeypatch):
    service = _project(tmp_path)
    dependency = tmp_path / "node_modules/types/index.d.ts"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("export declare const dependency: string;", encoding="utf-8")
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(tmp_path)
    started = threading.Event()
    release = threading.Event()
    reads = []
    read_snapshot = service._read_indexed_file_snapshot

    def blocked_read(path, **kwargs):
        reads.append(path)
        started.set()
        assert release.wait(5)
        return read_snapshot(path, **kwargs)

    monkeypatch.setattr(service, "_read_indexed_file_snapshot", blocked_read)
    full = asyncio.create_task(runtime.read(service, include_dependencies=True))
    try:
        await _until(started.is_set)
        source_only = asyncio.create_task(runtime.read(service, include_dependencies=False))
        await _until(lambda: len(next(iter(runtime._workspaces.values())).scan.readers) == 2)
        full.cancel()
        with pytest.raises(asyncio.CancelledError):
            await full
        assert not source_only.done()
        release.set()
        snapshot = await source_only
        assert [file.path for file in snapshot.files] == ["source-0.ts"]
        assert len(reads) == 2
        cached = await runtime.read(service, include_dependencies=True)
        assert {file.path for file in cached.files} == {"source-0.ts", "node_modules/types/index.d.ts"}
        assert len(reads) == 2
    finally:
        release.set()
        runtime.release_workspace(tmp_path)
        await runtime.shutdown()
    assert runtime._workspaces == {}


@pytest.mark.asyncio
async def test_last_consumer_cancel_drains_bounded_real_workers_and_new_reader_restarts(tmp_path, monkeypatch):
    service = _project(tmp_path, count=100)
    runtime = ProjectIndexRuntime()
    started = threading.Event()
    release = threading.Event()
    active = set()
    reads = []
    lock = threading.Lock()
    read_snapshot = service._read_indexed_file_snapshot

    def blocked_read(path, **kwargs):
        with lock:
            active.add(threading.get_ident())
            reads.append(path)
        started.set()
        try:
            assert release.wait(5)
            return read_snapshot(path, **kwargs)
        finally:
            with lock:
                active.remove(threading.get_ident())

    monkeypatch.setattr(service, "_read_indexed_file_snapshot", blocked_read)
    cancelled = asyncio.create_task(runtime.read(service))
    try:
        await _until(started.is_set)
        cancelled.cancel()
        await _until(lambda: next(iter(runtime._workspaces.values())).scan.stop.is_set())
        assert not cancelled.done()
        assert len(reads) <= 8
        replacement = asyncio.create_task(runtime.read(service))
        await _until(lambda: next(iter(runtime._workspaces.values())).readers == 2)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        snapshot = await replacement
        assert len(snapshot.files) == 100 and snapshot.complete
        assert active == set()
        assert runtime._workspaces == {}
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_workspace_cache_revalidates_content_names_dependencies_and_explicit_mutations(tmp_path, monkeypatch):
    service = _project(tmp_path)
    dependency = tmp_path / "node_modules/types/index.d.ts"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("export declare const dependency: string;", encoding="utf-8")
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(tmp_path)
    reads = []
    read_snapshot = service._read_indexed_file_snapshot

    def observed_read(path, **kwargs):
        reads.append(path)
        return read_snapshot(path, **kwargs)

    monkeypatch.setattr(service, "_read_indexed_file_snapshot", observed_read)
    try:
        await runtime.read(service)
        await runtime.read(service)
        assert len(reads) == 2
        old = (tmp_path / "source-0.ts").stat()
        (tmp_path / "source-0.ts").write_text("export const value0 = 9;", encoding="utf-8")
        os.utime(tmp_path / "source-0.ts", ns=(old.st_atime_ns, old.st_mtime_ns))
        runtime.invalidate_path(tmp_path, tmp_path / "source-0.ts")
        changed = await runtime.read(service, include_dependencies=False)
        assert changed.files[0].content == "export const value0 = 9;"
        assert len(reads) == 3
        (tmp_path / "source-0.ts").rename(tmp_path / "renamed.ts")
        dependency.unlink()
        replaced = dependency.with_name("replacement.d.ts")
        replaced.write_text("export declare const dependency: number;", encoding="utf-8")
        renamed = await runtime.read(service)
        assert {file.path for file in renamed.files} == {"renamed.ts", "node_modules/types/replacement.d.ts"}
        runtime.invalidate_all()
        await runtime.read(service)
        assert len(reads) == 7
        limited = await runtime.read(WorkspaceService(lambda: tmp_path, max_file_bytes=8))
        assert not limited.complete and limited.files == []
        assert {issue.status_code for issue in limited.issues} == {413}
    finally:
        runtime.release_workspace(tmp_path)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_late_consumer_waits_for_the_revision_present_when_its_read_started(tmp_path, monkeypatch):
    service = _project(tmp_path, count=2)
    runtime = ProjectIndexRuntime()
    runtime.retain_workspace(tmp_path)
    old_content_read = threading.Event()
    release_old_scan = threading.Event()
    read_snapshot = service._read_indexed_file_snapshot
    reads = []

    def blocked_after_first_snapshot(path, **kwargs):
        if path.name == "source-1.ts" and not release_old_scan.is_set():
            assert old_content_read.wait(5)
            assert release_old_scan.wait(5)
        snapshot = read_snapshot(path, **kwargs)
        reads.append((path.name, snapshot[0].content))
        if path.name == "source-0.ts":
            old_content_read.set()
        return snapshot

    monkeypatch.setattr(service, "_read_indexed_file_snapshot", blocked_after_first_snapshot)
    early = asyncio.create_task(runtime.read(service, include_dependencies=False))
    try:
        await _until(old_content_read.is_set)
        changed = tmp_path / "source-0.ts"
        changed.write_text("export const value0 = 2;", encoding="utf-8")
        runtime.invalidate_path(tmp_path, changed)
        late = asyncio.create_task(runtime.read(service, include_dependencies=False))
        await _until(lambda: len(next(iter(runtime._workspaces.values())).scan.readers) == 2)
        assert not late.done()
        release_old_scan.set()
        old_snapshot, current_snapshot = await asyncio.gather(early, late)
        assert next(file for file in old_snapshot.files if file.path == changed.name).content == "export const value0 = 0;"
        assert next(file for file in current_snapshot.files if file.path == changed.name).content == "export const value0 = 2;"
        assert len(reads) == 4
        await runtime.read(service, include_dependencies=False)
        assert len(reads) == 4
    finally:
        release_old_scan.set()
        runtime.release_workspace(tmp_path)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_http_disconnect_stops_actual_scan_before_endpoint_finishes(tmp_path, monkeypatch):
    _project(tmp_path, count=100)
    runtime = ProjectIndexRuntime()
    monkeypatch.setattr("backend.workspace.project_index_runtime.project_index_runtime", runtime)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    started = threading.Event()
    release = threading.Event()
    reads = []
    original = WorkspaceService._read_indexed_file_snapshot

    def blocked_read(self, path, **kwargs):
        reads.append(path)
        started.set()
        assert release.wait(5)
        return original(self, path, **kwargs)

    monkeypatch.setattr(WorkspaceService, "_read_indexed_file_snapshot", blocked_read)
    app = FastAPI()
    app.include_router(create_workspace_router())
    incoming = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    outgoing = []

    async def send(message):
        outgoing.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/api/workspace/project-index", "root_path": "",
        "query_string": urlencode({"workspace_root": str(tmp_path)}).encode(),
        "headers": [], "client": ("127.0.0.1", 1), "server": ("test", 80)}
    endpoint = asyncio.create_task(app(scope, incoming.get, send))
    try:
        await _until(started.is_set)
        await incoming.put({"type": "http.disconnect"})
        await _until(lambda: next(iter(runtime._workspaces.values())).scan.stop.is_set())
        assert not endpoint.done() and len(reads) <= 8
        release.set()
        await endpoint
        start = next(message for message in outgoing if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in outgoing if message["type"] == "http.response.body")
        assert start["status"] == 499
        assert "client disconnected" in json.loads(body)["detail"]
        assert runtime._workspaces == {}
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_consumers_and_same_named_files_stay_workspace_owned(tmp_path, monkeypatch):
    first = _project(tmp_path / "first")
    second = _project(tmp_path / "second")
    (tmp_path / "second/source-0.ts").write_text("export const other = true;", encoding="utf-8")
    runtime = ProjectIndexRuntime()
    results = await asyncio.gather(runtime.read(first), runtime.read(second))
    assert results[0].files[0].content == "export const value0 = 0;"
    assert results[1].files[0].content == "export const other = true;"
    started = threading.Event()
    release = threading.Event()
    original = first._read_indexed_file_snapshot

    def blocked_read(path, **kwargs):
        started.set()
        assert release.wait(5)
        return original(path, **kwargs)

    monkeypatch.setattr(first, "_read_indexed_file_snapshot", blocked_read)
    consumer = asyncio.create_task(runtime.read(first))
    await _until(started.is_set)
    closing = asyncio.create_task(runtime.shutdown())
    await _until(lambda: next(iter(runtime._workspaces.values())).scan.stop.is_set())
    assert not closing.done()
    release.set()
    await closing
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert runtime._workspaces == {}
