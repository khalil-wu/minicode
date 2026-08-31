from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.mcp import config_file as config_file_mod
from backend.ws.handlers.mcp import (
    handle_env_set,
    handle_mcp_add,
    handle_mcp_remove,
    handle_scheduler_add,
    handle_scheduler_list,
)


class _FakeSession:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.refreshed = False
        self.active_conversation_id = "conv-test"
        workspace_root = Path.cwd()
        self.session_lifecycle = SimpleNamespace(
            current_workspace_root=lambda: workspace_root,
            workspace_root=workspace_root,
        )

    @staticmethod
    def resolve_requested_workspace(_requested: str | None = None) -> str:
        return str(Path.cwd())

    async def send_payload(self, payload: dict[str, Any], log_context: str = "") -> None:
        self.payloads.append(payload)

    async def send_event(self, event: Any) -> None:
        self.events.append(event.to_ws_message())

    def refresh_tool_registry_if_mcp_changed(self, allow_when_busy: bool = False) -> bool:
        self.refreshed = True
        return True


class _FakeMcpManager:
    def __init__(self) -> None:
        self.started: list[Any] = []
        self.saved: list[Any] = []
        self.stopped: list[str] = []
        self.reloads = 0

    async def start_server(self, config: Any) -> None:
        self.started.append(config)

    async def register_config(self, config: Any) -> None:
        self.saved.append(config)

    async def stop_server(self, name: str) -> None:
        self.stopped.append(name)

    async def remove_server(self, name: str) -> None:
        self.stopped.append(name)

    async def reload_config(self) -> None:
        self.reloads += 1
        self.started = []
        self.saved = []
        payload = json.loads(config_file_mod.read_mcp_config()["content"])
        for name, entry in payload.get("servers", {}).items():
            config = SimpleNamespace(
                name=name,
                transport=entry["transport"],
                command=entry.get("command", ""),
                url=entry.get("url"),
                requires_user_action=bool(entry.get("requires_user_action", False)),
                setup_hint=entry.get("setup_hint", ""),
                docs_url=entry.get("docs_url", ""),
            )
            if entry.get("auto_start", True):
                self.started.append(config)
            else:
                self.saved.append(config)

    def get_all_status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": config.name,
                "status": "connected",
                "tools_count": 0,
                "transport": config.transport,
                "source": "user",
                "phase": "connected",
                "recoverable": True,
                "requires_user_action": False,
            }
            for config in self.started
        ] + [
            {
                "name": config.name,
                "status": "offline",
                "tools_count": 0,
                "transport": config.transport,
                "source": "user",
                "phase": "stopped",
                "recoverable": True,
                "requires_user_action": bool(config.requires_user_action),
                "setup_hint": config.setup_hint,
                "docs_url": config.docs_url,
            }
            for config in self.saved
            if config.name not in {started.name for started in self.started}
        ]


def _patch_mcp_config(monkeypatch: Any, config_path: Path) -> None:
    read_mcp_config = config_file_mod.read_mcp_config
    write_mcp_config = config_file_mod.write_mcp_config

    monkeypatch.setattr(config_file_mod, "read_mcp_config", lambda _path=None: read_mcp_config(config_path))
    monkeypatch.setattr(
        config_file_mod,
        "write_mcp_config",
        lambda content, _path=None: write_mcp_config(content, config_path),
    )


def _last_payload(session: _FakeSession, event_type: str) -> dict[str, Any]:
    matches = [payload for payload in session.payloads if payload.get("type") == event_type]
    assert matches
    return matches[-1]


def test_env_set_emits_an_authoritative_success_result(monkeypatch) -> None:
    class _Result:
        entries = [{"name": "DEMO_TOKEN", "description": "", "scope": "global"}]

    monkeypatch.setattr("backend.services.env_vault_service.set_env_entry", lambda _data: _Result())
    session = _FakeSession()

    asyncio.run(handle_env_set(session, {"name": "DEMO_TOKEN", "value": "secret"}))

    assert _last_payload(session, "env.list")["entries"][0]["name"] == "DEMO_TOKEN"
    assert session.events[-1]["type"] == "command.result"
    assert session.events[-1]["command"] == "env.set"


def test_scheduler_handlers_share_global_fallback_scheduler(monkeypatch, tmp_path) -> None:
    from backend.api import _state as api_state
    from backend.tasks import scheduler as scheduler_module

    monkeypatch.setattr(api_state, "bootstrap", None)
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")
    scheduler_module.reset_global_scheduler_for_tests()

    add_session = _FakeSession()
    asyncio.run(handle_scheduler_add(
        add_session,
        {
            "name": "Daily check",
            "prompt": "inspect the repo",
            "schedule": "0 9 * * 1-5",
        },
    ))

    list_session = _FakeSession()
    asyncio.run(handle_scheduler_list(list_session, {}))

    tasks = _last_payload(list_session, "scheduler.list")["tasks"]
    assert [task["name"] for task in tasks] == ["Daily check"]
    assert add_session.events[-1]["type"] == "command.result"
    assert add_session.events[-1]["command"] == "scheduler.add"


def test_mcp_add_persists_manual_http_server_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    manager = _FakeMcpManager()
    session = _FakeSession()
    _patch_mcp_config(monkeypatch, config_path)
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: manager)

    asyncio.run(handle_mcp_add(
        session,
        {
            "name": "local-browser",
            "transport": "http",
            "url": "http://127.0.0.1:8931/mcp",
        },
    ))

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    server = saved["servers"]["local-browser"]
    assert server["transport"] == "http"
    assert "type" not in server
    assert server["auto_start"] is True
    assert server["url"] == "http://127.0.0.1:8931/mcp"
    assert "command" not in server
    assert manager.started[0].name == "local-browser"
    assert session.refreshed is True
    assert session.events[-1]["type"] == "command.result"
    assert session.events[-1]["command"] == "mcp.add"


def test_mcp_add_reports_invalid_http_server_without_mutating_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    manager = _FakeMcpManager()
    session = _FakeSession()
    _patch_mcp_config(monkeypatch, config_path)
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: manager)

    asyncio.run(handle_mcp_add(
        session,
        {
            "name": "broken-http",
            "transport": "http",
        },
    ))

    assert manager.started == []
    assert not config_path.exists()
    assert session.events
    assert session.events[-1]["type"] == "command.result"
    assert "url" in session.events[-1]["message"].lower()


def test_mcp_add_persists_manual_stdio_server_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    manager = _FakeMcpManager()
    session = _FakeSession()
    _patch_mcp_config(monkeypatch, config_path)
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: manager)

    asyncio.run(handle_mcp_add(
        session,
        {
            "name": "local-playwright",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
            "env": {"DEBUG": "pw:mcp"},
        },
    ))

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    server = saved["servers"]["local-playwright"]
    assert server["transport"] == "stdio"
    assert "type" not in server
    assert server["auto_start"] is True
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@playwright/mcp@latest"]
    assert server["env"] == {"DEBUG": "pw:mcp"}


def test_mcp_remove_deletes_server_from_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "keep": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "keep"],
                    },
                    "remove-me": {
                        "transport": "http",
                        "url": "http://127.0.0.1:8931/mcp",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    manager = _FakeMcpManager()
    session = _FakeSession()
    _patch_mcp_config(monkeypatch, config_path)
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: manager)

    asyncio.run(handle_mcp_remove(session, {"name": "remove-me"}))

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "remove-me" not in saved["servers"]
    assert "keep" in saved["servers"]
    assert session.refreshed is True
    assert session.events[-1]["type"] == "command.result"
    assert session.events[-1]["command"] == "mcp.remove"
