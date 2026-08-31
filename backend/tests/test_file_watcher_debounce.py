from __future__ import annotations

import asyncio
from pathlib import Path

from backend.workspace.file_watcher import WorkspaceFileWatcher


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
