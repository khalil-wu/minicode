from __future__ import annotations

import asyncio
import json
from typing import Any

from backend.services import mcp_service


class _Manager:
    def __init__(self) -> None:
        self.reloaded = 0
        self.registered: list[str] = []
        self.started: list[str] = []
        self.removed: list[str] = []

    def get_all_status(self) -> list[dict[str, Any]]:
        return [{
            "name": "docs",
            "status": "offline",
            "transport": "stdio",
            "phase": "stopped",
            "source": "user",
        }]

    async def reload_config(self) -> None:
        self.reloaded += 1

    async def register_config(self, config: Any) -> None:
        self.registered.append(config.name)

    async def start_server(self, config: Any) -> None:
        self.started.append(config.name)

    async def remove_server(self, name: str) -> None:
        self.removed.append(name)


def _install_memory_config(monkeypatch, data: dict[str, Any]) -> dict[str, Any]:
    memory = {"data": data}

    def read_mcp_config() -> dict[str, Any]:
        return {"content": json.dumps(memory["data"])}

    def write_mcp_config(content: str, _path: Any) -> dict[str, Any]:
        memory["data"] = json.loads(content)
        return {"saved": True}

    monkeypatch.setattr(mcp_service.config_file_mod, "read_mcp_config", read_mcp_config)
    monkeypatch.setattr(mcp_service.config_file_mod, "write_mcp_config", write_mcp_config)
    return memory


async def _noop_hook(**_kwargs: Any) -> None:
    return None


def test_mcp_status_includes_editable_config_without_resolving_pass_through(monkeypatch) -> None:
    _install_memory_config(monkeypatch, {
        "servers": {
            "docs": {
                "transport": "stdio",
                "command": "node",
                "args": ["server.js"],
                "cwd": "tools/docs",
                "env": {"TOKEN": "fixed"},
                "env_vars": [{"name": "HOME_ALIAS", "source": "USERPROFILE"}],
                "auto_start": False,
            }
        }
    })

    status = mcp_service.get_mcp_status(_Manager())[0]

    assert status["editable"] is True
    assert status["command"] == "node"
    assert status["args"] == ["server.js"]
    assert status["cwd"] == "tools/docs"
    assert status["env"] == {"TOKEN": "fixed"}
    assert status["env_vars"] == [{"name": "HOME_ALIAS", "source": "USERPROFILE"}]
    assert status["auto_start"] is False


def test_update_mcp_server_round_trips_structured_settings(monkeypatch) -> None:
    memory = _install_memory_config(monkeypatch, {
        "servers": {"docs": {"transport": "stdio", "command": "python", "args": []}}
    })
    manager = _Manager()

    asyncio.run(mcp_service.update_mcp_server(
        manager,
        {
            "original_name": "docs",
            "name": "docs",
            "transport": "stdio",
            "command": "node",
            "args": ["server.js"],
            "cwd": "tools/docs",
            "env": {"TOKEN": "fixed"},
            "env_vars": [{"name": "HOME_ALIAS", "source": "USERPROFILE"}],
            "auto_start": False,
        },
        config_change_hook=_noop_hook,
    ))

    saved = memory["data"]["servers"]["docs"]
    assert saved == {
        "transport": "stdio",
        "command": "node",
        "args": ["server.js"],
        "auto_start": False,
        "env": {"TOKEN": "fixed"},
        "cwd": "tools/docs",
        "env_vars": [{"name": "HOME_ALIAS", "source": "USERPROFILE"}],
    }
    assert manager.reloaded == 1


def test_toggle_mcp_server_updates_auto_start_and_reloads(monkeypatch) -> None:
    memory = _install_memory_config(monkeypatch, {
        "servers": {"docs": {"transport": "stdio", "command": "node", "args": [], "auto_start": False}}
    })
    manager = _Manager()

    asyncio.run(mcp_service.toggle_mcp_server(
        manager,
        "docs",
        True,
        config_change_hook=_noop_hook,
    ))

    assert memory["data"]["servers"]["docs"]["auto_start"] is True
    assert manager.reloaded == 1
