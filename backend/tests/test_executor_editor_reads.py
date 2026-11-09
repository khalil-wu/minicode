"""Bounded editor scans retain the captured executor and drain their file handles."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_editor import ExecutorEditorService
from backend.workspace.text_search import SearchBuffer, WorkspaceTextSearchRequest


def editor_context(host):
    attachment = host.environments.primary()
    return replace(host.context, turn_environment=attachment,
        permission=replace(host.context.permission, environment_scope=attachment.permission_scope),
        environment_id=attachment.permission_scope.environment_id)


@pytest.mark.asyncio
async def test_search_preserves_order_unicode_buffers_and_issues_when_reads_finish_out_of_order(executor_host, monkeypatch):
    host = executor_host
    for name, content in {
        "00.ts": "😀 NEEDLE\r\n", "01.ts": "disk unchanged", "02.ts": "NEEDLE\n",
        "03.ts": "no match\n" * 100, "04.ts": "NEEDLE excluded",
    }.items():
        (host.workspace / name).write_text(content, encoding="utf-8")
    (host.workspace / "05.ts").write_bytes(b"\xff")
    async with ExecutorEditorService(editor_context(host)) as service:
        original = service.read_file
        later_finished = asyncio.Event()
        read_paths = []

        async def read(path):
            name = service.resolve(path).name
            read_paths.append(name)
            if name == "00.ts":
                await later_finished.wait()
            result = await original(path)
            if name == "02.ts":
                later_finished.set()
            return result

        monkeypatch.setattr(service, "read_file", read)
        from backend.workspace import text_search
        index = text_search._text_index
        indexed = []

        def text_index(content):
            indexed.append(content)
            return index(content)

        monkeypatch.setattr(text_search, "_text_index", text_index)
        async with asyncio.timeout(10):
            result = await service.search_content(WorkspaceTextSearchRequest(query="NEEDLE", include=["*.ts"], exclude=["04.ts"],
                buffers=[SearchBuffer(path="01.ts", content="NEEDLE unsaved", original="disk unchanged", content_hash="buffer-hash")]))
        assert [file["path"] for file in result["files"]] == ["00.ts", "01.ts", "02.ts"]
        assert result["match_count"] == 3 and not result["truncated"]
        assert result["files"][0]["matches"][0]["offset"] == 3
        assert result["files"][0]["matches"][0]["column"] == 4
        assert result["files"][1]["original"] == "disk unchanged"
        assert result["files"][1]["content_hash"] == "buffer-hash" and result["files"][1]["from_buffer"]
        assert [issue["path"] for issue in result["issues"]] == ["05.ts"]
        assert "01.ts" not in read_paths and "04.ts" not in read_paths
        assert len(indexed) == 3
        assert (host.workspace / "01.ts").read_text() == "disk unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
@pytest.mark.parametrize(("operation", "finish"), [("search", "cancel"), ("index", "cancel"), ("search", "limit"), ("index", "error")])
async def test_scan_bounds_real_open_files_and_drains_on_exit(executor_host, monkeypatch, operation, finish):
    host = executor_host
    for number in range(40):
        (host.workspace / f"{number:02}.ts").write_text("NEEDLE NEEDLE\n", encoding="utf-8")
    async with ExecutorEditorService(editor_context(host)) as service:
        filesystem = service.filesystem
        original_open = filesystem.open
        handles = []
        batch_open = asyncio.Event()
        blocked = asyncio.Event()

        async def open_file(path, **kwargs):
            handle = await original_open(path, **kwargs)
            handles.append(handle)
            if len(handles) == 8:
                batch_open.set()
            original_read = handle.read_block

            async def read_block(offset, length):
                await batch_open.wait()
                if service.resolve(path).name == "00.ts":
                    if finish == "limit":
                        return await original_read(offset, length)
                    if finish == "error":
                        raise OSError("actual read failure")
                await blocked.wait()
                return await original_read(offset, length)

            monkeypatch.setattr(handle, "read_block", read_block)
            return handle

        monkeypatch.setattr(filesystem, "open", open_file)
        task = asyncio.create_task(service.search_content(WorkspaceTextSearchRequest(query="NEEDLE", limit=1))
            if operation == "search" else service.project_index())
        try:
            async with asyncio.timeout(10):
                await batch_open.wait()
                if finish == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                elif finish == "error":
                    with pytest.raises(OSError, match="actual read failure"):
                        await task
                else:
                    result = await task
                    assert result["match_count"] == 1 and result["truncated"]
                    assert [file["path"] for file in result["files"]] == ["00.ts"]
            assert len(handles) == 8
            assert all(handle.closed for handle in handles)
            assert not filesystem.files and not filesystem.operations
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
