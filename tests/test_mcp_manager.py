import json
import asyncio

import pytest

from backend.commands.plugins import default_plugin_roots
from backend.mcp.oauth import MCPAuthenticationRequired, create_loopback_callback
from backend.mcp.manager import (
    MCPAuthStatus,
    MCPServerConfig,
    MCPServerManager,
    MCPServerState,
    ServerStatus,
)


def test_mcp_manager_uses_local_minicode_config_as_user_source(tmp_path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "shared-search": {
                        "transport": "http",
                        "url": "https://mcp.example/search",
                    },
                    "local-worker": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "local.worker"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    manager = MCPServerManager(config_path=tmp_path / ".mcp.json")

    configs = [
        config for config in manager.load_config()
        if not config.source.startswith("plugin:")
    ]

    assert [config.name for config in configs] == [
        "shared-search",
        "local-worker",
    ]
    assert all(config.source == "user" for config in configs)
    assert configs[0].transport == "http"
    assert configs[0].url == "https://mcp.example/search"
    assert configs[1].transport == "stdio"
    assert configs[1].command == "python"


def test_default_plugin_roots_do_not_activate_the_minicode_download_cache(monkeypatch, tmp_path) -> None:
    minicode_home = tmp_path / ".minicode"
    monkeypatch.setenv("MINICODE_HOME", str(minicode_home))

    roots = {path.resolve() for path in default_plugin_roots()}

    assert (minicode_home / "plugins").resolve() not in roots


def test_mcp_manager_resolves_declared_env_and_rejects_missing_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MINICODE_SEARCH_URL", "https://mcp.example/search")
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "shared-search": {
                        "transport": "http",
                        "url": "${MINICODE_SEARCH_URL}",
                    },
                    "docs": {
                        "transport": "http",
                        "url": "${MINICODE_DOCS_URL}",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    manager = MCPServerManager(config_path=config_path)
    with pytest.raises(ValueError, match="MINICODE_DOCS_URL"):
        manager.load_config()


def test_mcp_manager_preserves_user_transport_and_url(tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(json.dumps({"servers": {"browser": {
        "transport": "http",
        "url": "https://mcp.example/browser",
    }}}), encoding="utf-8")
    manager = MCPServerManager(config_path=config_path)

    loaded = [
        config for config in manager.load_config()
        if not config.source.startswith("plugin:")
    ]
    status = [
        {
            "name": cfg.name,
            "source": cfg.source,
            "transport": cfg.transport,
            "url": cfg.url,
        }
        for cfg in loaded
    ]

    assert status == [
        {
            "name": "browser",
            "source": "user",
            "transport": "http",
            "url": "https://mcp.example/browser",
        }
    ]


# ── Lifecycle phase classification (P2: auth/session/progress) ──


def _state(status: ServerStatus, last_error: str = "") -> MCPServerState:
    state = MCPServerState(config=MCPServerConfig(name="demo"))
    state.status = status
    state.last_error = last_error
    return state


def test_auth_like_error_is_classified_as_auth_required() -> None:
    life = _state(ServerStatus.ERROR, "HTTP 401 Unauthorized").to_lifecycle_dict()
    assert life["phase"] == "auth_required"
    assert life["requires_user_action"] is True
    assert life["recoverable"] is False
    assert life["server_name"] == "demo"


def test_plain_auth_words_do_not_turn_a_recoverable_mcp_error_into_auth_required() -> None:
    for message in (
        "tool returned an auth field",
        "login page selector was not found",
        "token budget exceeded while processing response",
        "credential formatter raised a validation error",
    ):
        life = _state(ServerStatus.ERROR, message).to_lifecycle_dict()
        assert life["phase"] == "failed"
        assert life["recoverable"] is True
        assert life["requires_user_action"] is False
    assert life["status"] == "error"


def test_expired_credentials_are_classified_as_expired() -> None:
    life = _state(ServerStatus.ERROR, "OAuth token expired; please re-auth").to_lifecycle_dict()
    assert life["phase"] == "expired"
    assert life["requires_user_action"] is True
    assert life["recoverable"] is False


def test_connection_failure_is_classified_as_failed_and_recoverable() -> None:
    life = _state(ServerStatus.ERROR, "ConnectionError: connection refused").to_lifecycle_dict()
    assert life["phase"] == "failed"
    assert life["recoverable"] is True
    assert life["requires_user_action"] is False


def test_reconnecting_status_maps_to_reconnecting_phase_and_progress() -> None:
    state = _state(ServerStatus.RECONNECTING)
    assert state.to_lifecycle_dict()["phase"] == "reconnecting"
    progress = state.to_progress_dict()
    assert progress is not None
    assert progress["operation"] == "reconnect"
    assert progress["status"] == "running"


def test_stopped_server_has_no_progress() -> None:
    state = _state(ServerStatus.OFFLINE)
    assert state.to_progress_dict() is None
    assert state.to_lifecycle_dict()["phase"] == "stopped"


def test_to_status_dict_exposes_minicode_lifecycle_projection() -> None:
    state = _state(ServerStatus.CONNECTED)
    payload = state.to_status_dict()
    for key in ("name", "status", "tools_count", "error", "source", "priority", "transport"):
        assert key in payload
    assert payload["status"] == "connected"
    assert payload["phase"] == "connected"
    assert payload["recoverable"] is True
    assert payload["requires_user_action"] is False
    assert payload["auth_status"] == "unsupported"


def test_to_status_dict_exposes_negotiated_mcp_capabilities_without_listing() -> None:
    state = _state(ServerStatus.CONNECTED)

    class _Capabilities:
        tools = True
        resources = True
        resources_subscribe = False
        resources_list_changed = True
        prompts = True
        logging = False

    state.client = type("_Client", (), {
        "server_capabilities": _Capabilities(),
        "cleanup_status": {
            "pending": False,
            "reason": "",
            "requested_at": None,
            "completed_at": None,
        },
    })()
    assert state.to_status_dict()["capabilities"] == {
        "tools": True,
        "resources": True,
        "resources_subscribe": False,
        "resources_list_changed": True,
        "prompts": True,
        "logging": False,
    }


def test_mcp_auth_status_is_independent_from_connection_lifecycle() -> None:
    state = MCPServerState(
        config=MCPServerConfig(name="github", transport="http", url="https://example.test/mcp"),
        status=ServerStatus.ERROR,
        auth_status=MCPAuthStatus.NOT_LOGGED_IN,
        last_error="HTTP 401 Unauthorized",
    )

    payload = state.to_status_dict()
    assert payload["phase"] == "auth_required"
    assert payload["auth_status"] == "not_logged_in"


def test_local_app_setup_metadata_round_trips_to_status_until_connected(tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "figma-desktop": {
                        "transport": "http",
                        "url": "http://127.0.0.1:3845/mcp",
                        "requires_user_action": True,
                        "setup_hint": "Open Figma Desktop and enable the Dev Mode MCP server.",
                        "docs_url": "https://help.figma.com/",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manager = MCPServerManager(config_path=config_path)
    config = next(config for config in manager.load_config() if config.name == "figma-desktop")
    state = MCPServerState(config=config)

    stopped = state.to_status_dict()
    assert stopped["requires_user_action"] is True
    assert stopped["setup_hint"].startswith("Open Figma Desktop")
    assert stopped["docs_url"] == "https://help.figma.com/"

    state.status = ServerStatus.CONNECTED
    connected = state.to_status_dict()
    assert connected["requires_user_action"] is False
    assert connected["setup_hint"].startswith("Open Figma Desktop")


def test_manager_get_server_lifecycle_and_progress(tmp_path) -> None:
    manager = MCPServerManager(config_path=tmp_path / ".mcp.json")
    assert manager.get_server_lifecycle("missing") is None
    assert manager.get_server_progress("missing") is None

    manager._servers["demo"] = _state(ServerStatus.ERROR, "403 forbidden: invalid api key")
    life = manager.get_server_lifecycle("demo")
    assert life is not None
    assert life["phase"] == "auth_required"
    assert manager.get_server_progress("demo")["status"] == "failed"


class _ConnectFailsClient:
    instances: list["_ConnectFailsClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        _ConnectFailsClient.instances.append(self)

    @property
    def connected(self) -> bool:
        return False

    async def connect(self) -> None:
        raise ConnectionError("figma desktop is not listening")

    async def list_tools(self) -> list[object]:
        return []

    async def close(self) -> None:
        self.closed = True
        return True


def test_manager_closes_and_discards_client_after_failed_start(monkeypatch) -> None:
    _ConnectFailsClient.instances = []
    monkeypatch.setattr("backend.mcp.manager.MCPClient", _ConnectFailsClient)

    async def scenario() -> MCPServerManager:
        manager = MCPServerManager()
        config = MCPServerConfig(
            name="figma-desktop",
            transport="http",
            url="http://127.0.0.1:3845/mcp",
        )
        await manager.start_server(config)
        await manager.start_server(config)
        return manager

    manager = asyncio.run(scenario())
    state = manager._servers["figma-desktop"]

    assert state.status == ServerStatus.ERROR
    assert state.client is None
    assert len(_ConnectFailsClient.instances) == 2
    assert all(client.closed for client in _ConnectFailsClient.instances)


class _OAuthGateClient:
    instances: list["_OAuthGateClient"] = []

    def __init__(self, *args, interactive_oauth: bool = False, **kwargs) -> None:
        self.interactive_oauth = interactive_oauth
        self.closed = False
        self._connected = False
        _OAuthGateClient.instances.append(self)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def has_valid_token(self) -> bool:
        return self.interactive_oauth and self._connected

    @property
    def server_capabilities(self):
        return type("_Capabilities", (), {
            "tools": True,
            "resources": False,
            "resources_subscribe": False,
            "resources_list_changed": False,
            "prompts": False,
            "logging": False,
        })()

    @property
    def cleanup_status(self) -> dict[str, object]:
        return {
            "pending": False,
            "reason": "",
            "requested_at": None,
            "completed_at": None,
        }

    async def connect(self) -> None:
        if not self.interactive_oauth:
            raise MCPAuthenticationRequired("https://auth.example/authorize")
        self._connected = True

    async def list_tools(self) -> list[object]:
        return []

    async def close(self) -> None:
        self.closed = True
        self._connected = False
        return True


def test_mcp_oauth_is_noninteractive_at_startup_and_single_flight_on_login(monkeypatch) -> None:
    _OAuthGateClient.instances = []
    monkeypatch.setattr("backend.mcp.manager.MCPClient", _OAuthGateClient)

    async def scenario() -> MCPServerManager:
        manager = MCPServerManager()
        config = MCPServerConfig(
            name="linear",
            transport="http",
            url="https://mcp.linear.app/mcp",
        )

        # Concurrent startup/config refresh attempts produce one silent auth
        # probe and settle in auth_required without launching a browser.
        await asyncio.gather(manager.start_server(config), manager.start_server(config))
        state = manager._servers["linear"]
        assert state.to_lifecycle_dict()["phase"] == "auth_required"
        assert state.to_status_dict()["auth_status"] == "not_logged_in"
        assert len(_OAuthGateClient.instances) == 1
        assert _OAuthGateClient.instances[0].interactive_oauth is False

        # Concurrent explicit login commands also collapse to one SDK flow.
        await asyncio.gather(manager.oauth_login("linear"), manager.oauth_login("linear"))
        return manager

    manager = asyncio.run(scenario())

    assert manager._servers["linear"].status == ServerStatus.CONNECTED
    assert manager._servers["linear"].to_status_dict()["auth_status"] == "oauth"
    assert [client.interactive_oauth for client in _OAuthGateClient.instances] == [False, True]


def test_noninteractive_oauth_probe_never_opens_a_browser(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        "backend.mcp.oauth.webbrowser.open",
        lambda url, *args: opened.append(url) or True,
    )

    async def scenario() -> None:
        callback = await create_loopback_callback(interactive=False)
        try:
            with pytest.raises(MCPAuthenticationRequired):
                await callback.redirect("https://auth.example/authorize")
        finally:
            await callback.close()

    asyncio.run(scenario())

    assert opened == []


def test_manager_register_config_tracks_server_without_starting() -> None:
    async def scenario() -> MCPServerManager:
        manager = MCPServerManager()
        await manager.register_config(
            MCPServerConfig(
                name="figma-desktop",
                transport="http",
                url="http://127.0.0.1:3845/mcp",
                auto_start=False,
                requires_user_action=True,
                setup_hint="Open Figma Desktop and enable the Dev Mode MCP server.",
                docs_url="https://help.figma.com/",
            )
        )
        return manager

    manager = asyncio.run(scenario())

    status = manager.get_all_status()[0]
    assert status["name"] == "figma-desktop"
    assert status["status"] == "offline"
    assert status["phase"] == "stopped"
    assert status["requires_user_action"] is True
    assert status["setup_hint"].startswith("Open Figma Desktop")


def test_manager_remove_server_deletes_runtime_state() -> None:
    async def scenario() -> MCPServerManager:
        manager = MCPServerManager()
        await manager.register_config(MCPServerConfig(name="figma-desktop", command="node"))
        await manager.remove_server("figma-desktop")
        return manager

    manager = asyncio.run(scenario())

    assert manager.get_all_status() == []
