from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.conversations.repository import ConversationRepository
from backend.ws.session_lifecycle import SessionLifecycle


def _owner_session(repository: ConversationRepository, active_id: str, **extra):
    """A session stub carrying exactly the collaborators SessionLifecycle reads.

    ``workspace_path_for_conversation`` resolves the workspace from the
    repository, so the stub owns the active conversation id and the repository
    and nothing else. Omitting ``active_conversation`` is deliberate: if the
    resolution ever goes back to reading that cached attribute, this stub fails
    loudly instead of quietly taking a different path.
    """

    return SimpleNamespace(
        active_conversation_id=active_id,
        conversation_repo=repository,
        **extra,
    )


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

    repository = ConversationRepository(tmp_path / "conversations")
    for conversation_id in ("conv-watcher-a", "conv-watcher-b"):
        repository.create_conversation(
            conversation_id=conversation_id,
            title=conversation_id,
            workspace_root=str(root),
        )
    session = _owner_session(
        repository,
        "conv-watcher-a",
        session_id="session-watcher-owner",
        send_payload=send_payload,
    )
    lifecycle = SessionLifecycle(session)
    lifecycle.start_file_watcher()
    assert len(callbacks) == 1

    session.active_conversation_id = "conv-watcher-b"
    asyncio.run(callbacks[0](root / "src" / "app.py", "modified"))

    assert captured[0]["conversation_id"] == "conv-watcher-b"


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

    repository = ConversationRepository(tmp_path / "conversations")
    repository.create_conversation(
        conversation_id="conv-watcher-a",
        title="conv-watcher-a",
        workspace_root=str(first_root),
    )
    repository.create_conversation(
        conversation_id="conv-watcher-b",
        title="conv-watcher-b",
        workspace_root=str(second_root),
    )
    session = _owner_session(
        repository,
        "conv-watcher-a",
        session_id="session-watcher-generation",
        send_payload=send_payload,
    )
    lifecycle = SessionLifecycle(session)
    lifecycle.start_file_watcher()
    session.active_conversation_id = "conv-watcher-b"

    asyncio.run(callbacks[0](first_root / "app.py", "modified"))
    assert captured == []
