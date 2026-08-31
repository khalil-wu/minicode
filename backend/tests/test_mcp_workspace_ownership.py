from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.api import _state
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.artifact.store import ArtifactStore
from backend.bootstrap.app import AppBootstrap
from backend.mcp.manager import MCPServerManager
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


def _bootstrap(*, ws_manager: Any = None, status_callback: Any = None) -> AppBootstrap:
    async def _status(_name: str, _status: Any) -> None:
        return None

    return AppBootstrap(
        build_tool_registry=lambda _store, **_kwargs: ToolRegistry(),
        create_session_llm=lambda *_args, **_kwargs: None,
        ws_manager=ws_manager,
        on_mcp_status_change=status_callback or _status,
    )


def test_bootstrap_owns_one_manager_per_canonical_workspace(monkeypatch, tmp_path) -> None:
    class _Manager:
        instances: list["_Manager"] = []

        def __init__(self, *, workspace_root: Path | None = None, **kwargs: Any) -> None:
            self.workspace_root = workspace_root
            self.registry_version = 0
            self.start_count = 0
            self.reload_count = 0
            self.stop_count = 0
            self.on_status_change = kwargs.get("on_status_change")
            self.__class__.instances.append(self)

        async def start_all(self) -> None:
            self.start_count += 1
            await asyncio.sleep(0)

        async def reload_config(self) -> None:
            self.reload_count += 1

        async def stop_all(self) -> None:
            self.stop_count += 1

    monkeypatch.setattr("backend.mcp.manager.MCPServerManager", _Manager)
    bootstrap = _bootstrap()
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    async def scenario() -> None:
        manager_a1, manager_a2, manager_b = await asyncio.gather(
            bootstrap.ensure_mcp_manager(workspace_a),
            bootstrap.ensure_mcp_manager(workspace_a / "."),
            bootstrap.ensure_mcp_manager(workspace_b),
        )
        assert manager_a1 is manager_a2
        assert manager_a1 is not manager_b
        assert manager_a1.workspace_root == workspace_a.resolve()
        assert manager_b.workspace_root == workspace_b.resolve()
        assert manager_a1.start_count == 1
        assert manager_b.start_count == 1

        await bootstrap.reload_mcp_managers()
        assert manager_a1.reload_count == 1
        assert manager_b.reload_count == 1

    asyncio.run(scenario())
    assert len(_Manager.instances) == 2


def test_failed_manager_start_does_not_poison_workspace_cache(monkeypatch, tmp_path) -> None:
    class _Manager:
        def __init__(self, **_kwargs: Any) -> None:
            self.reload_count = 0

        async def start_all(self) -> None:
            raise RuntimeError("required server failed")

        async def reload_config(self) -> None:
            self.reload_count += 1

        async def stop_all(self) -> None:
            return None

    monkeypatch.setattr("backend.mcp.manager.MCPServerManager", _Manager)
    bootstrap = _bootstrap()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def scenario() -> None:
        try:
            await bootstrap.ensure_mcp_manager(workspace)
        except RuntimeError as exc:
            assert "required server failed" in str(exc)
        else:  # pragma: no cover - failure is the point of this scenario
            raise AssertionError("failed manager startup unexpectedly succeeded")

        assert bootstrap._mcp_start_tasks == {}
        manager = await bootstrap.activate_mcp_workspace(workspace)
        assert manager.reload_count == 1

    asyncio.run(scenario())


def test_manager_serializes_initial_start_and_config_reload(monkeypatch, tmp_path) -> None:
    manager = MCPServerManager(
        config_path=tmp_path / "user.json",
        workspace_root=None,
    )

    async def scenario() -> None:
        start_entered = asyncio.Event()
        release_start = asyncio.Event()
        reload_entered = asyncio.Event()

        async def start_unlocked() -> None:
            start_entered.set()
            await release_start.wait()

        async def reload_unlocked() -> None:
            reload_entered.set()

        monkeypatch.setattr(manager, "_start_all_unlocked", start_unlocked)
        monkeypatch.setattr(manager, "_reload_config_unlocked", reload_unlocked)

        start_task = asyncio.create_task(manager.start_all())
        await start_entered.wait()
        reload_task = asyncio.create_task(manager.reload_config())
        await asyncio.sleep(0)
        assert not reload_entered.is_set()

        release_start.set()
        await asyncio.gather(start_task, reload_task)
        assert reload_entered.is_set()

    asyncio.run(scenario())


def test_fixed_manager_project_discovery_does_not_follow_process_workspace(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "backend.mcp.project_settings.is_workspace_trusted",
        lambda _workspace: True,
    )
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    (workspace_a / ".minicode").mkdir()
    (workspace_b / ".minicode").mkdir()
    (workspace_a / ".minicode" / "mcp.json").write_text(
        json.dumps({"servers": {"server-a": {"transport": "stdio", "command": "a"}}}),
        encoding="utf-8",
    )
    (workspace_b / ".minicode" / "mcp.json").write_text(
        json.dumps({"servers": {"server-b": {"transport": "stdio", "command": "b"}}}),
        encoding="utf-8",
    )
    manager_a = MCPServerManager(workspace_root=workspace_a)
    manager_b = MCPServerManager(workspace_root=workspace_b)

    monkeypatch.setattr(
        "backend.workspace.state.get_explicit_active_workspace_root",
        lambda: workspace_b,
    )
    configs_a = manager_a._load_project_configs()
    monkeypatch.setattr(
        "backend.workspace.state.get_explicit_active_workspace_root",
        lambda: workspace_a,
    )
    configs_b = manager_b._load_project_configs()

    assert [config.name for config in configs_a] == ["server-a"]
    assert [config.name for config in configs_b] == ["server-b"]
    assert manager_a.workspace_root == workspace_a.resolve()
    assert manager_b.workspace_root == workspace_b.resolve()


def test_conversation_registry_uses_its_workspace_manager_version(
    monkeypatch,
    tmp_path,
) -> None:
    class _Manager:
        def __init__(self, label: str, version: int) -> None:
            self.label = label
            self.registry_version = version

    class _Bootstrap:
        def __init__(self, managers: dict[str, _Manager]) -> None:
            self.managers = managers
            self.created_with: list[_Manager] = []

        def get_mcp_manager_for_workspace(self, workspace_root: Path | None) -> _Manager | None:
            return self.managers.get(str(Path(workspace_root).resolve())) if workspace_root else None

        def create_tool_registry(
            self,
            _store: Any,
            *,
            workspace_root: Path | None = None,
            config: Any | None = None,
            mcp_manager: _Manager | None = None,
        ) -> Any:
            del workspace_root, config
            self.created_with.append(mcp_manager)
            return object()

    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    manager_a = _Manager("a", 4)
    manager_b = _Manager("b", 10)
    bootstrap = _Bootstrap(
        {
            str(workspace_a.resolve()): manager_a,
            str(workspace_b.resolve()): manager_b,
        }
    )
    monkeypatch.setattr(_state, "bootstrap", bootstrap)

    session = object.__new__(WebSocketSession)
    session.mcp_manager = manager_b
    session.artifact_store = ArtifactStore()
    session.tool_registry = ToolRegistry()
    session._conversation_tool_registries = {}
    session.run_manager = SimpleNamespace(run_tasks={})

    first = session._conversation_tool_registry("conversation-a", workspace_root=workspace_a)
    stored = session._conversation_tool_registries["conversation-a"]
    assert bootstrap.created_with == [manager_a]
    assert len(stored) == 3
    assert stored[0] == 4

    manager_b.registry_version = 11
    assert session._conversation_tool_registry(
        "conversation-a", workspace_root=workspace_a
    ) is first
    assert bootstrap.created_with == [manager_a]

    manager_a.registry_version = 5
    assert session._conversation_tool_registry(
        "conversation-a", workspace_root=workspace_a
    ) is not first
    assert bootstrap.created_with == [manager_a, manager_a]

    stale_registry = object()
    session._conversation_tool_registries["conversation-a"] = (
        manager_a.registry_version,
        str(workspace_a.resolve()),
        object(),
        stale_registry,
    )
    rebuilt = session._conversation_tool_registry(
        "conversation-a", workspace_root=workspace_a
    )
    assert rebuilt is not stale_registry
    assert len(session._conversation_tool_registries["conversation-a"]) == 3


def test_mcp_status_broadcast_targets_only_sessions_bound_to_manager(
    monkeypatch,
) -> None:
    class _Manager:
        def get_all_status(self) -> list[dict[str, str]]:
            return [{"name": "demo", "status": "connected"}]

        def get_server_lifecycle(self, name: str) -> dict[str, str]:
            return {"server_name": name, "status": "connected"}

        def get_server_progress(self, _name: str) -> None:
            return None

    class _Session:
        def __init__(self, manager: Any) -> None:
            self.mcp_manager = manager
            self.is_connected = True
            self.events: list[Any] = []

        async def send_event(self, event: Any) -> None:
            self.events.append(event)

    class _WSManager:
        def __init__(self, sessions: list[_Session]) -> None:
            self.sessions = sessions

        def iter_sessions(self) -> tuple[_Session, ...]:
            return tuple(self.sessions)

    import backend.main as main

    manager_a = _Manager()
    manager_b = _Manager()
    session_a = _Session(manager_a)
    session_b = _Session(manager_b)
    monkeypatch.setattr(_state, "ws_manager", _WSManager([session_a, session_b]))
    monkeypatch.setattr(_state, "bootstrap", type("_Bootstrap", (), {})())

    asyncio.run(main._broadcast_mcp_status_change("demo", object(), manager_a))

    assert [event.type for event in session_a.events] == ["mcp_status", "mcp.lifecycle"]
    assert session_b.events == []


def test_server_callback_owner_requires_manager_and_active_conversation_generation() -> None:
    manager = object()
    conversation = object()

    class _Session:
        session_id = "session-a"
        is_connected = True
        conversation_repo = type(
            "_Repository",
            (),
            {"get_conversation": lambda self, value: conversation if value == "conversation-a" else None},
        )()

        session_lifecycle = SimpleNamespace(
            workspace_root_for_conversation=lambda _conversation: None,
        )

    bootstrap = _bootstrap(
        ws_manager=type(
            "_WSManager",
            (),
            {"iter_sessions": lambda self: (_Session(),)},
        )(),
    )
    bootstrap._mcp_managers["<projectless>"] = manager
    claim = conversation_query_guards().try_start(
        "conversation-a",
        owner_id="mcp-callback-owner-test",
    )
    assert claim is not None
    owner = {
        "session_id": "session-a",
        "conversation_id": "conversation-a",
        "conversation_run_generation": claim.generation,
        "mcp_manager": manager,
    }
    try:
        session, resolved = bootstrap._resolve_mcp_request_session(
            {"_minicode_owner": owner}
        )
        assert session is not None
        assert resolved is owner

        wrong_manager = {**owner, "mcp_manager": object()}
        assert bootstrap._resolve_mcp_request_session(
            {"_minicode_owner": wrong_manager}
        )[0] is None
    finally:
        conversation_query_guards().end(claim)

    assert bootstrap._resolve_mcp_request_session(
        {"_minicode_owner": owner}
    )[0] is None


def test_project_mcp_config_ignores_ancestor_directories(monkeypatch, tmp_path) -> None:
    """Only the active workspace's MiniCode MCP file may declare servers."""

    monkeypatch.setattr(
        "backend.mcp.project_settings.is_workspace_trusted",
        lambda _workspace: True,
    )
    workspace = tmp_path / "nested" / "workspace"
    workspace.mkdir(parents=True)
    (tmp_path / ".minicode").mkdir()
    (tmp_path / ".minicode" / "mcp.json").write_text(
        json.dumps({"servers": {"ancestor-server": {
            "transport": "stdio",
            "command": "evil",
        }}}),
        encoding="utf-8",
    )

    from backend.mcp.project_settings import project_mcp_config_paths

    assert project_mcp_config_paths(workspace) == (
        workspace.resolve() / ".minicode" / "mcp.json",
    )
    assert MCPServerManager(workspace_root=workspace)._load_project_configs() == []
