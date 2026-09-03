from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.ws.session_lifecycle import SessionLifecycle


def test_file_watcher_callback_uses_the_current_same_workspace_conversation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    callbacks = []

    class _Watcher:
        def __init__(self, *, on_change, **_kwargs) -> None:
            callbacks.append(on_change)

        def start(self) -> None:
            return None

        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            return None

    monkeypatch.setattr(
        "backend.ws.session_lifecycle.WorkspaceFileWatcher",
        _Watcher,
    )
    root = tmp_path.resolve()
    captured: list[dict] = []

    async def send_payload(payload, **_kwargs):
        captured.append(dict(payload))
        return True

    session = SimpleNamespace(
        session_id="session-watcher-owner",
        active_conversation_id="conversation-a",
        active_conversation=SimpleNamespace(
            workspace_root=str(root),
            worktree_path="",
        ),
        send_payload=send_payload,
    )
    lifecycle = SessionLifecycle(session)
    lifecycle.start_file_watcher()
    assert len(callbacks) == 1

    session.active_conversation_id = "conversation-b"
    session.active_conversation = SimpleNamespace(
        workspace_root=str(root),
        worktree_path="",
    )
    asyncio.run(callbacks[0](root / "src" / "app.py", "modified"))

    assert captured[0]["conversation_id"] == "conversation-b"


def test_file_watcher_callback_drops_events_after_workspace_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    callbacks = []

    class _Watcher:
        def __init__(self, *, on_change, **_kwargs) -> None:
            callbacks.append(on_change)

        def start(self) -> None:
            return None

        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            return None

    monkeypatch.setattr(
        "backend.ws.session_lifecycle.WorkspaceFileWatcher",
        _Watcher,
    )
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    captured: list[dict] = []

    async def send_payload(payload, **_kwargs):
        captured.append(dict(payload))
        return True

    session = SimpleNamespace(
        session_id="session-watcher-generation",
        active_conversation_id="conversation-a",
        active_conversation=SimpleNamespace(
            workspace_root=str(first_root),
            worktree_path="",
        ),
        send_payload=send_payload,
    )
    lifecycle = SessionLifecycle(session)
    lifecycle.start_file_watcher()
    session.active_conversation_id = "conversation-b"
    session.active_conversation = SimpleNamespace(
        workspace_root=str(second_root),
        worktree_path="",
    )

    asyncio.run(callbacks[0](first_root / "app.py", "modified"))
    assert captured == []
