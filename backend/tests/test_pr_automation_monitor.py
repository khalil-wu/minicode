from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.bootstrap.app import AppBootstrap


def test_pr_automation_poll_deduplicates_connected_workspaces(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class _Session:
        is_connected = True

        def __init__(self):
            self.session_lifecycle = SimpleNamespace(
                current_workspace_root=lambda: project,
            )

    sessions = {"one": _Session(), "two": _Session()}
    bootstrap = AppBootstrap(
        build_tool_registry=lambda *_args, **_kwargs: None,
        create_session_llm=lambda *_args, **_kwargs: None,
        ws_manager=SimpleNamespace(_sessions=sessions),
        on_mcp_status_change=lambda *_args: None,
    )
    calls = []

    monkeypatch.setattr("backend.services.workspace_service.read_pr_automation", lambda _root: {"auto_fix": True})

    async def fake_status(session, data):
        calls.append((session, data))
        return True

    monkeypatch.setattr("backend.ws.handlers.workspace.handle_git_pr_status", fake_status)

    asyncio.run(bootstrap._poll_pr_automation_once())

    assert len(calls) == 1
