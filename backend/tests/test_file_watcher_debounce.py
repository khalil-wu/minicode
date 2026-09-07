from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from watchdog.events import FileClosedEvent, FileClosedNoWriteEvent, FileModifiedEvent, FileOpenedEvent

from backend.workspace.file_watcher import WorkspaceFileWatcher
from backend.workspace.service import WorkspaceService


def test_debounce_keeps_only_latest_task(tmp_path: Path) -> None:
    calls: list[tuple[Path, str]] = []

    async def exercise() -> None:
        watcher = WorkspaceFileWatcher(
            workspace_root=tmp_path,
            on_change=lambda path, event_type: calls.append((path, event_type)),
            stability_threshold=0.03,
        )
        target = tmp_path / "app.py"
        await watcher._debounced_change(target, "modified")
        await asyncio.sleep(0)
        await watcher._debounced_change(target, "modified")
        await asyncio.sleep(0)
        await watcher._debounced_change(target, "modified")
        await asyncio.sleep(0.08)

    asyncio.run(exercise())

    assert calls == [(tmp_path / "app.py", "modified")]


@pytest.mark.parametrize("event_class", [FileOpenedEvent, FileClosedEvent, FileClosedNoWriteEvent])
def test_access_events_do_not_start_change_debounces(tmp_path, event_class):
    async def exercise():
        on_change = Mock()
        watcher = WorkspaceFileWatcher(tmp_path, on_change, stability_threshold=0)
        watcher._create_handler().on_any_event(event_class(str(tmp_path / "sample.txt")))
        await asyncio.sleep(0)
        assert watcher._debounce_tasks == {}
        on_change.assert_not_called()

    asyncio.run(exercise())


def test_close_and_read_events_do_not_replace_a_pending_modification(tmp_path, monkeypatch):
    cache = Mock()
    invalidate_search = Mock()
    monkeypatch.setattr("backend.workspace.file_watcher.get_global_file_cache", lambda: cache)
    monkeypatch.setattr("backend.workspace.file_watcher.invalidate_global_fuzzy_search", invalidate_search)
    target = tmp_path / ".gitignore"

    async def exercise():
        on_change = Mock()
        watcher = WorkspaceFileWatcher(tmp_path, on_change, stability_threshold=0.01)
        handler = watcher._create_handler()
        for event_class in (FileModifiedEvent, FileClosedEvent, FileOpenedEvent, FileClosedNoWriteEvent):
            handler.on_any_event(event_class(str(target)))
        await asyncio.sleep(0.06)
        on_change.assert_called_once_with(target, "modified")
        cache.invalidate.assert_called_once_with(target)
        invalidate_search.assert_called_once_with()
        assert watcher._debounce_tasks == {}

    asyncio.run(exercise())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Exercises real Linux inotify access events")
def test_real_editor_reads_do_not_feed_back_into_file_change_notifications(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("original\n", encoding="utf-8")
    service = WorkspaceService(lambda: tmp_path)

    async def exercise():
        changes = []
        reloads = []
        changed = asyncio.Event()

        def on_change(path, event_type):
            if path != target:
                return
            changes.append(event_type)
            if len(reloads) < 4:
                reloads.append(service.read_file(target.name).content)
            changed.set()

        watcher = WorkspaceFileWatcher(tmp_path, on_change, stability_threshold=0.02)
        watcher.start()
        try:
            await asyncio.to_thread(service.read_file, target.name)
            await asyncio.sleep(0.15)
            assert changes == []
            assert reloads == []

            await asyncio.to_thread(target.write_text, "updated\n", encoding="utf-8")
            await asyncio.wait_for(changed.wait(), timeout=3)
            await asyncio.sleep(0.15)
            assert changes == ["modified"]
            assert reloads == ["updated\n"]
        finally:
            watcher.stop()

    asyncio.run(exercise())
