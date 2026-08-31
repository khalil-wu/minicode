"""P1-1: MCP tool hot-reload into a live WebSocket session.

A WS session holds a single tool_registry. Before this feature, connecting an
MCP server after the session existed only bumped mcp_registry_version (which
invalidates the schema cache) but never re-registered the new proxy into the
session's registry, so new MCP tools stayed invisible until backend restart.

These tests drive the real version-read path
(_mcp_registry_version -> backend.main.get_mcp_manager -> _state.bootstrap) and
the real rebuild path (bootstrap.create_tool_registry).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import backend.main as main_mod
from backend.api import _state
from backend.artifact.store import ArtifactStore
from backend.bootstrap.app import AppBootstrap
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.mcp.client import MCPToolDef
from backend.permissions.checker import PermissionChecker
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


class _SilentLLM(LLMAdapter):
    async def stream_chat(self, messages: list[LLMMessage], tools=None):
        if False:  # pragma: no cover - never yields
            yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return ""


class _FakeProxyTool(BaseTool):
    """Minimal stand-in for an MCPToolProxy."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"fake mcp proxy {name}"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="ok")


class _FakeMcpManager:
    """Stands in for MCPServerManager: a bumpable version + connected tool set."""

    def __init__(self) -> None:
        self.registry_version = 0
        self.connected: set[str] = set()


class _FakeBootstrap:
    """Rebuilds a registry that mirrors the manager's currently connected tools."""

    def __init__(self, manager: _FakeMcpManager) -> None:
        self.mcp_manager = manager

    def create_tool_registry(self, artifact_store, **_kwargs) -> ToolRegistry:
        registry = ToolRegistry()
        for name in sorted(self.mcp_manager.connected):
            registry.register(_FakeProxyTool(name))
        return registry


class _NotDoneTask:
    """A stand-in asyncio.Task that reports itself as still running."""

    def done(self) -> bool:
        return False


def _install_fake_bootstrap(monkeypatch) -> _FakeMcpManager:
    manager = _FakeMcpManager()
    monkeypatch.setattr(_state, "bootstrap", _FakeBootstrap(manager))
    return manager


def _make_session(monkeypatch, tmp_path) -> WebSocketSession:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    return WebSocketSession(
        session_id="sess-mcp-hotreload",
        websocket=_FakeWebSocket(),
        llm=_SilentLLM(),
        artifact_store=ArtifactStore(),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        config=AppConfig(llm=LLMSettings(api_key="")),
    )


def test_mcp_connect_hot_reloads_into_live_session(monkeypatch, tmp_path) -> None:
    manager = _install_fake_bootstrap(monkeypatch)
    session = _make_session(monkeypatch, tmp_path)

    assert session._mcp_registry_version_snapshot == 0
    assert "mcp__demo__echo" not in session.tool_registry.list_tools()

    # MCP server connects: a new proxy appears and the registry generation bumps.
    manager.connected.add("mcp__demo__echo")
    manager.registry_version = 1

    assert session.refresh_tool_registry_if_mcp_changed() is True
    assert "mcp__demo__echo" in session.tool_registry.list_tools()
    # ...and shows up in the schema snapshot (exposure-independent view list).
    snapshot_names = {view.name for view in session.tool_registry.build_schema_views()}
    assert "mcp__demo__echo" in snapshot_names
    assert session._mcp_registry_version_snapshot == 1


def test_unchanged_version_does_not_rebuild(monkeypatch, tmp_path) -> None:
    manager = _install_fake_bootstrap(monkeypatch)
    session = _make_session(monkeypatch, tmp_path)

    manager.connected.add("mcp__demo__echo")
    manager.registry_version = 1
    assert session.refresh_tool_registry_if_mcp_changed() is True
    registry_after_first = session.tool_registry

    # Same version => no work, same registry object retained.
    assert session.refresh_tool_registry_if_mcp_changed() is False
    assert session.tool_registry is registry_after_first


def test_active_run_defers_then_refreshes_next_round(monkeypatch, tmp_path) -> None:
    manager = _install_fake_bootstrap(monkeypatch)
    session = _make_session(monkeypatch, tmp_path)

    manager.connected.add("mcp__demo__echo")
    manager.registry_version = 1
    session.run_manager.run_tasks["active"] = _NotDoneTask()  # type: ignore[assignment]

    # MCP status hooks must not swap the registry mid-run.
    assert session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False) is False
    assert "mcp__demo__echo" not in session.tool_registry.list_tools()
    # Snapshot stays stale so the next refresh still triggers.
    assert session._mcp_registry_version_snapshot == 0

    # Run finishes -> next refresh picks up the change.
    session.run_manager.run_tasks.clear()
    assert session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False) is True
    assert "mcp__demo__echo" in session.tool_registry.list_tools()


def test_mcp_remove_drops_proxy_from_next_snapshot(monkeypatch, tmp_path) -> None:
    manager = _install_fake_bootstrap(monkeypatch)
    session = _make_session(monkeypatch, tmp_path)

    manager.connected.add("mcp__demo__echo")
    manager.registry_version = 1
    session.refresh_tool_registry_if_mcp_changed()
    assert "mcp__demo__echo" in session.tool_registry.list_tools()

    # Server removed: drop the tool, bump the generation.
    manager.connected.discard("mcp__demo__echo")
    manager.registry_version = 2
    assert session.refresh_tool_registry_if_mcp_changed() is True
    assert "mcp__demo__echo" not in session.tool_registry.list_tools()


def test_usage_inspect_refreshes_registry_before_snapshot(monkeypatch, tmp_path) -> None:
    """The session.usage.inspect trigger site rebuilds before snapshotting."""
    from backend.ws.handlers.session import handle_session_usage_inspect

    manager = _install_fake_bootstrap(monkeypatch)
    session = _make_session(monkeypatch, tmp_path)

    manager.connected.add("mcp__demo__echo")
    manager.registry_version = 1

    asyncio.run(handle_session_usage_inspect(session, {}))

    assert "mcp__demo__echo" in session.tool_registry.list_tools()


def test_no_bootstrap_is_safe_noop(monkeypatch, tmp_path) -> None:
    """Before bootstrap exists, refresh must not raise and must not update snapshot."""
    # Bootstrap present (version 0) only for construction, then removed.
    _install_fake_bootstrap(monkeypatch)
    session = _make_session(monkeypatch, tmp_path)
    monkeypatch.setattr(_state, "bootstrap", None)

    # Even if a version were reported, no bootstrap => cannot rebuild.
    assert session.refresh_tool_registry_if_mcp_changed() is False
    assert session.tool_registry.list_tools() == []


# ---------------------------------------------------------------------------
# Real composition-root tests.
#
# The fakes above bypass the production wiring
# (AppBootstrap -> services.tool_registry_factory.build_tool_registry ->
# register_mcp_tools). These tests drive that real chain so they catch a missing
# mcp_manager owner projection before a session registry is published.
# ---------------------------------------------------------------------------


class _StubMcpClient:
    connected = True


class _RealPathMcpManager:
    """Minimal MCPServerManager stand-in exposing the real registration API."""

    def __init__(self) -> None:
        self.registry_version = 0
        self._servers: dict[str, list[MCPToolDef]] = {}

    def connect(self, server: str, tooldefs: list[MCPToolDef]) -> None:
        self._servers[server] = list(tooldefs)
        self.registry_version += 1

    def disconnect(self, server: str) -> None:
        self._servers.pop(server, None)
        self.registry_version += 1

    def get_all_tools(self) -> dict[str, list[MCPToolDef]]:
        return {name: list(defs) for name, defs in self._servers.items()}

    def get_client(self, server: str):
        return _StubMcpClient() if server in self._servers else None


def _make_real_bootstrap(manager: _RealPathMcpManager) -> AppBootstrap:
    async def _noop_status(name, status):  # pragma: no cover - never invoked here
        return None

    bootstrap = AppBootstrap(
        build_tool_registry=main_mod.build_tool_registry,
        create_session_llm=lambda *a, **k: None,
        ws_manager=None,
        on_mcp_status_change=_noop_status,
    )
    bootstrap.mcp_manager = manager
    return bootstrap


def test_real_composition_root_registers_mcp_proxy(monkeypatch) -> None:
    """AppBootstrap.create_tool_registry must forward mcp_manager so MCP proxies
    actually register. Guards the production wiring that the fakes above skip."""
    manager = _RealPathMcpManager()
    manager.connect("demo", [MCPToolDef(name="echo", description="Echo tool")])
    bootstrap = _make_real_bootstrap(manager)
    monkeypatch.setattr(_state, "bootstrap", bootstrap)

    registry = bootstrap.create_tool_registry(ArtifactStore())

    assert "mcp__demo__echo" in registry.list_tools()


def test_status_capability_snapshot_uses_bootstrap_mcp_proxies(monkeypatch) -> None:
    """The /api/status and /api/doctor capability snapshot must use the same
    bootstrap registry path as agent sessions, otherwise Doctor reports stale
    or missing MCP tools while the runtime can call them."""
    from backend.api import routes_health

    manager = _RealPathMcpManager()
    manager.connect("demo", [MCPToolDef(name="echo", description="Echo tool")])
    bootstrap = _make_real_bootstrap(manager)
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    _state.capability_cache_payload = None
    _state.capability_cache_expires_at = 0.0

    snapshot = routes_health._build_capability_status_payload()
    view_names = {view["name"] for view in snapshot["tool_views"]}

    assert "mcp__demo__echo" in view_names
    assert snapshot["summary"]["mcp_proxy_tools"] >= 1


def test_status_capability_failure_does_not_publish_a_fake_empty_inventory(monkeypatch) -> None:
    from backend.api import routes_health

    bootstrap = SimpleNamespace(
        build_capability_snapshot=lambda: (_ for _ in ()).throw(
            RuntimeError("registry unavailable")
        )
    )
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    _state.capability_cache_payload = None
    _state.capability_cache_expires_at = 0.0

    snapshot = routes_health._build_capability_status_payload()

    assert snapshot["status"] == "error"
    assert snapshot["available"] is False
    assert snapshot["error"]["type"] == "capability_snapshot_failed"
    assert "tools" not in snapshot
    assert "commands" not in snapshot
    assert "skills" not in snapshot


def test_session_refresh_uses_real_bootstrap_path(monkeypatch, tmp_path) -> None:
    """End-to-end: session.refresh -> AppBootstrap -> canonical registry factory
    -> register_mcp_tools surfaces a newly connected MCP proxy in the session."""
    manager = _RealPathMcpManager()
    bootstrap = _make_real_bootstrap(manager)
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    session = _make_session(monkeypatch, tmp_path)

    assert session._mcp_registry_version_snapshot == 0
    assert "mcp__demo__echo" not in session.tool_registry.list_tools()

    # MCP server connects through the manager (bumps registry_version).
    manager.connect("demo", [MCPToolDef(name="echo", description="Echo tool")])

    assert session.refresh_tool_registry_if_mcp_changed() is True
    assert "mcp__demo__echo" in session.tool_registry.list_tools()

    # And a subsequent disconnect drops it on the next refresh.
    manager.disconnect("demo")
    assert session.refresh_tool_registry_if_mcp_changed() is True
    assert "mcp__demo__echo" not in session.tool_registry.list_tools()


def test_runtime_snapshot_includes_compact_mcp_summary(monkeypatch, tmp_path) -> None:
    """runtime_snapshot carries connected/failed/auth_required counts + short statuses."""
    _install_fake_bootstrap(monkeypatch)
    session = _make_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "backend.api.routes_health.get_mcp_status",
        lambda: [
            {"name": "a", "status": "connected", "phase": "connected"},
            {"name": "b", "status": "error", "phase": "auth_required"},
            {"name": "c", "status": "error", "phase": "failed"},
        ],
    )

    summary = session.runtime_snapshot()["mcp"]

    assert summary["connected"] == 1
    assert summary["auth_required"] == 1
    assert summary["failed"] == 1
    assert {s["name"] for s in summary["servers"]} == {"a", "b", "c"}
    # Stays compact: connection/auth state only, no tools or error payloads.
    assert all(
        set(s.keys()) == {"name", "status", "phase", "auth_status"}
        for s in summary["servers"]
    )


def test_mcp_status_broadcast_invalidates_status_and_capability_caches(monkeypatch) -> None:
    """MCP topology changes must not leave /api/status or /api/doctor stale."""

    class _BroadcastManager:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def broadcast_event(self, event: object) -> None:
            self.events.append(event)

    class _LifecycleManager:
        def get_server_lifecycle(self, name: str) -> dict[str, object]:
            return {"server_name": name, "status": "connected", "phase": "connected"}

        def get_server_progress(self, name: str) -> None:
            return None

    class _CachedBootstrap:
        def __init__(self) -> None:
            self.mcp_manager = _LifecycleManager()
            self._status_cache_payload = {"stale": True}
            self._status_cache_expires_at = 999999.0

        def get_mcp_status(self) -> list[dict[str, object]]:
            return [{"name": "demo", "status": "connected", "tools_count": 1}]

    ws_manager = _BroadcastManager()
    bootstrap = _CachedBootstrap()
    monkeypatch.setattr(_state, "ws_manager", ws_manager)
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    _state.status_cache_payload = {"stale": True}
    _state.status_cache_expires_at = 999999.0
    _state.capability_cache_payload = {"stale": True}
    _state.capability_cache_expires_at = 999999.0

    asyncio.run(main_mod._broadcast_mcp_status_change("demo", object()))

    assert _state.status_cache_payload is None
    assert _state.status_cache_expires_at == 0.0
    assert _state.capability_cache_payload is None
    assert _state.capability_cache_expires_at == 0.0
    assert bootstrap._status_cache_payload is None
    assert bootstrap._status_cache_expires_at == 0.0
    assert len(ws_manager.events) == 2
