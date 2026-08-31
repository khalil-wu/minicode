from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.commands.registry import CommandRegistry
from backend.workspace.state import (
    clear_active_workspace_root,
    get_explicit_active_workspace_root,
    set_active_workspace_root,
)
from backend.ws.session_lifecycle import SessionLifecycle


class _Context:
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.root_path = str(root)
        self._fail = fail

    async def initialize(self):
        if self._fail:
            raise RuntimeError("index failed")
        return object()


class _Session:
    def __init__(self, old_context: _Context) -> None:
        self.active_conversation_id = ""
        self.event_outbox = SimpleNamespace(client_command_id="")
        self.command_registry = CommandRegistry()
        self.skill_manager = None
        self.mcp_manager = None
        self.restarted: list[Path] = []
        self.events = []
        self.payloads: list[dict] = []
        self.session_id = "session-workspace-rollback"
        self.is_connected = True
        self.session_lifecycle = SessionLifecycle(self)
        self.session_lifecycle.workspace_context = old_context
        self.session_lifecycle.restart_file_watcher = self._record_file_watcher_restart

    async def _run_cwd_changed_hook(self, *, old_cwd: str, new_cwd: str) -> None:
        return None

    def _record_file_watcher_restart(self, workspace_root: Path) -> None:
        self.restarted.append(Path(workspace_root).resolve())

    async def send_event(self, event) -> None:
        self.events.append(event)

    async def send_payload(
        self,
        payload: dict,
        *,
        log_context: str = "",
        **_kwargs,
    ) -> bool:
        self.payloads.append(dict(payload))
        return True

    def runtime_capabilities_payload(self, *, source: str = "session") -> dict:
        return {
            "type": "runtime.capabilities",
            "session_id": self.session_id,
            "source": source,
            "capabilities": {},
        }

    async def send_runtime_capabilities(self, *, source: str) -> None:
        return None

    def refresh_tool_registry_if_mcp_changed(
        self, *, allow_when_busy: bool = True
    ) -> bool:
        return False


def test_workspace_activation_failure_rolls_back_context_root_and_watcher(monkeypatch, tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    old_context = _Context(old_root)
    new_context = _Context(new_root, fail=True)
    session = _Session(old_context)
    set_active_workspace_root(old_root)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    monkeypatch.setattr(
        "backend.services.workspace_service.create_workspace_context",
        lambda project_path: new_context,
    )

    try:
        result = asyncio.run(
            session.session_lifecycle.activate_workspace_path(
                str(new_root),
                wait_for_initialize=True,
                error_command=None,
            )
        )
        active_root = get_explicit_active_workspace_root()
    finally:
        clear_active_workspace_root()

    assert result is False
    assert session.session_lifecycle.workspace_context is old_context
    assert active_root == old_root.resolve()
    assert session.restarted == [new_root.resolve(), old_root.resolve()]
    assert session.events and "index failed" in session.events[-1].data["message"]


def test_background_workspace_index_failure_keeps_the_committed_new_owner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    old_context = _Context(old_root)
    new_context = _Context(new_root, fail=True)
    session = _Session(old_context)
    set_active_workspace_root(old_root)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    monkeypatch.setattr(
        "backend.services.workspace_service.create_workspace_context",
        lambda project_path: new_context,
    )

    from backend.api import _state

    class _Bootstrap:
        async def begin_mcp_workspace_activation(self, _workspace_root):
            manager = object()

            async def ready():
                return manager

            return manager, asyncio.create_task(ready())

        async def activate_mcp_workspace(self, _workspace_root):
            return object()

    monkeypatch.setattr(_state, "bootstrap", _Bootstrap())

    async def scenario() -> tuple[bool, Path | None]:
        result = await session.session_lifecycle.activate_workspace_path(
            str(new_root),
            wait_for_initialize=False,
            error_command=None,
        )
        context_task = session.session_lifecycle.workspace_context_task
        assert context_task is not None
        await context_task
        mcp_task = session.session_lifecycle.workspace_mcp_task
        if mcp_task is not None:
            await mcp_task
        return result, get_explicit_active_workspace_root()

    try:
        result, active_root = asyncio.run(scenario())
    finally:
        clear_active_workspace_root()

    assert result is True
    assert session.session_lifecycle.workspace_context is new_context
    # Workspace ownership is session-scoped. Global active-workspace state is
    # intentionally unchanged by activation, including the background index
    # path, so another session cannot observe this session's directory.
    assert active_root == old_root.resolve()
    assert session.restarted == [new_root.resolve()]
    assert session.events and "index failed" in session.events[-1].data["message"]
