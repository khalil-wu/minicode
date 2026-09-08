import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.preview import launcher
from backend.ws.session_lifecycle import SessionLifecycle


@pytest.fixture
def register_preview(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(launcher, "_RUNNING", {})

    def register(name: str, port: int) -> launcher.PreviewLaunchProcess:
        preview = launcher.PreviewLaunchProcess(
            id=name,
            config=launcher.PreviewLaunchConfig(
                name=name,
                command="fixture",
                cwd=str(tmp_path),
                port=port,
                url=f"http://127.0.0.1:{port}",
            ),
            process=SimpleNamespace(returncode=None, pid=port),
            status="ready",
            session_id="session",
            conversation_id="conversation",
            workspace_root=str(tmp_path.resolve()),
        )
        launcher._RUNNING[name] = preview
        return preview

    return register


def test_file_changes_refresh_every_active_preview_for_the_exact_owner(register_preview, tmp_path: Path):
    stopping = register_preview("stopping", 43140)
    stopping.status = "stopping"
    stopping.cleanup_pending = True
    web = register_preview("web", 43141)
    docs = register_preview("docs", 43142)
    other_conversation = register_preview("other-conversation", 43143)
    other_conversation.conversation_id = "other"
    other_session = register_preview("other-session", 43144)
    other_session.session_id = "other"
    other_workspace = register_preview("other-workspace", 43145)
    other_workspace.workspace_root = str(tmp_path / "other")
    exited = register_preview("exited", 43146)
    exited.process.returncode = 0
    cleanup_pending = register_preview("cleanup-pending", 43147)
    cleanup_pending.cleanup_pending = True
    starting = register_preview("starting", 43148)
    starting.status = "starting"
    starting.config = replace(starting.config, url="")
    events = []

    async def capture(payload, **kwargs):
        events.append(payload)

    lifecycle = SessionLifecycle(SimpleNamespace(
        session_id="session", active_conversation_id="other", send_payload=capture,
    ))
    asyncio.run(lifecycle.on_file_changed(
        tmp_path / "index.html", "modified", workspace_root=tmp_path, conversation_id="conversation",
    ))

    scope = {"conversation_id": "conversation", "workspace_root": str(tmp_path.resolve())}
    assert events == [
        {"type": "file.changed", "path": "index.html", "event": "modified", **scope},
        {"type": "preview.refreshed", "path": "index.html", "url": web.effective_url, **scope},
        {"type": "preview.refreshed", "path": "index.html", "url": docs.effective_url, **scope},
    ]


def test_preview_stopping_during_another_refresh_is_not_reloaded(register_preview, tmp_path: Path):
    web = register_preview("web", 43141)
    docs = register_preview("docs", 43142)
    events = []

    async def capture(payload, **kwargs):
        events.append(payload)
        if payload["type"] == "preview.refreshed":
            docs.status = "stopping"
            docs.cleanup_pending = True

    lifecycle = SessionLifecycle(SimpleNamespace(
        session_id="session", active_conversation_id="conversation", send_payload=capture,
    ))
    asyncio.run(lifecycle.on_file_changed(
        tmp_path / "index.html", "modified", workspace_root=tmp_path, conversation_id="conversation",
    ))

    assert [event["url"] for event in events if event["type"] == "preview.refreshed"] == [web.effective_url]


def test_file_change_without_an_active_preview_still_reaches_the_editor(register_preview, tmp_path: Path):
    preview = register_preview("stopping", 43141)
    preview.status = "stopping"
    events = []

    async def capture(payload, **kwargs):
        events.append(payload)

    lifecycle = SessionLifecycle(SimpleNamespace(
        session_id="session", active_conversation_id="conversation", send_payload=capture,
    ))
    asyncio.run(lifecycle.on_file_changed(tmp_path / "index.html", "deleted", workspace_root=tmp_path))

    assert events == [{
        "type": "file.changed", "path": "index.html", "event": "deleted",
        "conversation_id": "conversation", "workspace_root": str(tmp_path.resolve()),
    }]
