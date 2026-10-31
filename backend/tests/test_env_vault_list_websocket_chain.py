from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.main import app
from backend.vault.store import EnvVault
from tests.test_conversations import _install_noop_llm, _receive_until_event


def test_settings_inventory_returns_metadata_and_injection_uses_each_scope(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    credentials = {}
    monkeypatch.setattr("backend.vault.store.keyring.get_password", lambda service, name: credentials.get((service, name)))
    monkeypatch.setattr("backend.vault.store.keyring.set_password", lambda service, name, value: credentials.__setitem__((service, name), value))
    monkeypatch.setattr("backend.vault.store.keyring.delete_password", lambda service, name: credentials.pop((service, name), None))
    vault = EnvVault(tmp_path / "vault.json")
    vault.set("GLOBAL_EXAMPLE", "global-value-example", scope="global")
    vault.set("COMMAND_EXAMPLE", "command-value-example", scope="run_command")
    vault.set("MCP_EXAMPLE", "mcp-value-example", scope="mcp:one")
    monkeypatch.setattr("backend.services.env_vault_service._new_vault", lambda: vault)

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=env-list-review") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            websocket.send_json({"type": "env.list", "client_command_id": "env-review"})
            entries = _receive_until_event(websocket, "env.list")["entries"]
            receipt = _receive_until_event(websocket, "command.result")
            assert receipt["command"] == "env.list" and receipt["level"] == "success"
    assert {item["name"]: item["scope"] for item in entries} == {
        "GLOBAL_EXAMPLE": "global", "COMMAND_EXAMPLE": "run_command", "MCP_EXAMPLE": "mcp:one",
    }
    serialized = json.dumps(entries)
    assert all(value not in serialized for value in ("global-value-example", "command-value-example", "mcp-value-example"))
    assert vault.inject_into_env("run_command") == {"GLOBAL_EXAMPLE": "global-value-example", "COMMAND_EXAMPLE": "command-value-example"}
    assert vault.inject_into_env("mcp:one") == {"GLOBAL_EXAMPLE": "global-value-example", "MCP_EXAMPLE": "mcp-value-example"}
