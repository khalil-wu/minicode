from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from backend.mcp.client import MCPClient
from backend.mcp.config_file import write_mcp_config
from backend.mcp.manager import MCPServerConfig, MCPServerManager, MCPServerState
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.vault import EnvVault


@pytest.fixture
def execution_vault(tmp_path, monkeypatch):
    import backend.vault.store as store

    secrets = {}
    monkeypatch.setattr(store, "VAULT_FILE", tmp_path / "vault.json")
    monkeypatch.setattr(store.keyring, "get_password", lambda service, name: secrets.get((service, name)))
    monkeypatch.setattr(store.keyring, "set_password", lambda service, name, value: secrets.__setitem__((service, name), value))
    monkeypatch.setattr(store.keyring, "delete_password", lambda service, name: secrets.pop((service, name), None))
    vault = EnvVault()
    vault.set("VAULT_GLOBAL", "global")
    vault.set("VAULT_SHARED", "saved")
    vault.set("VAULT_SHELL", "shell", scope="run_command")
    vault.set("VAULT_MCP", "server", scope="mcp:plugin:tools@local:helper")
    vault.set("VAULT_OTHER", "other", scope="mcp:other")
    for name in ("OPENAI_API_KEY", "CUSTOM_API_KEY_ENDPOINT", "MINICODE_CUSTOM_IMAGE_API_KEY_ENDPOINT", "MINICODE_PROVIDER_CREDENTIAL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
        vault.set(name, "provider-only")
    return vault


def test_shell_launch_reads_scoped_vault_values_without_changing_host_env(execution_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_SHARED", "host")
    before = dict(os.environ)
    runner = SandboxRunner(SandboxPolicy(workspace_root=tmp_path, env_overrides={"VAULT_SHARED": "command"}))
    env = runner._build_env()
    assert env["VAULT_GLOBAL"] == "global"
    assert env["VAULT_SHELL"] == "shell"
    assert env["VAULT_SHARED"] == "command"
    assert "VAULT_MCP" not in env and "VAULT_OTHER" not in env
    assert "OPENAI_API_KEY" not in env
    assert "MINICODE_PROVIDER_CREDENTIAL_PROVIDER" not in env
    execution_vault.set("VAULT_GLOBAL", "updated")
    assert runner._build_env()["VAULT_GLOBAL"] == "updated"
    assert dict(os.environ) == before


def test_missing_global_credential_does_not_block_shell_or_scoped_mcp_startup(execution_vault, tmp_path, monkeypatch):
    import backend.vault.store as store

    read_credential = store.keyring.get_password
    monkeypatch.setattr(store.keyring, "get_password", lambda service, name: None if name == "VAULT_GLOBAL" else read_credential(service, name))
    monkeypatch.delenv("VAULT_GLOBAL", raising=False)
    runner = SandboxRunner(SandboxPolicy(workspace_root=tmp_path))
    environment = runner._build_env()
    assert "VAULT_GLOBAL" not in environment
    assert environment["VAULT_SHELL"] == "shell"
    assert environment["VAULT_SHARED"] == "saved"

    monkeypatch.setattr("backend.mcp.client.stdio_client", lambda params: params)
    client = MCPClient("plugin:tools@local:helper", command="node")
    params = asyncio.run(client._sdk_transport_context())
    assert "VAULT_GLOBAL" not in params.env
    assert params.env["VAULT_MCP"] == "server"
    assert "VAULT_SHELL" not in params.env
    assert execution_vault.list_names()[0]["credential_status"] == "missing"


def test_mcp_transport_receives_only_its_scope_and_explicit_env_wins(execution_vault, monkeypatch):
    monkeypatch.setattr("backend.mcp.client.stdio_client", lambda params: params)
    before = dict(os.environ)
    client = MCPClient("plugin:tools@local:helper", command="node", env={"VAULT_SHARED": "literal"})
    params = asyncio.run(client._sdk_transport_context())
    assert params.env["VAULT_GLOBAL"] == "global"
    assert params.env["VAULT_MCP"] == "server"
    assert params.env["VAULT_SHARED"] == "literal"
    assert "VAULT_SHELL" not in params.env and "VAULT_OTHER" not in params.env
    assert "OPENAI_API_KEY" not in params.env
    assert "MINICODE_PROVIDER_CREDENTIAL_PROVIDER" not in params.env
    assert dict(os.environ) == before


def test_mcp_templates_and_passthrough_resolve_the_same_scoped_environment(execution_vault, tmp_path):
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path)
    config = manager._config_from_mapping("plugin:tools@local:helper", {
        "transport": "stdio", "command": "node", "args": ["${VAULT_MCP}", "${VAULT_SHARED}"],
        "env": {"VAULT_SHARED": "literal", "TARGET": "explicit"},
        "env_vars": ["VAULT_MCP", {"name": "TARGET", "source": "VAULT_GLOBAL"}],
    }, source="user", priority=0, base_dir=tmp_path)
    assert config.args == ["server", "literal"]
    assert config.env["VAULT_MCP"] == "server"
    assert config.env["TARGET"] == "explicit"
    execution_vault.set("VAULT_URL", "https://fixture.invalid/mcp", scope="mcp:remote")
    source = json.dumps({"servers": {"remote": {"transport": "http", "url": "${VAULT_URL}"}}})
    receipt = write_mcp_config(source, tmp_path / "remote.json")
    assert "${VAULT_URL}" in receipt["config"]["content"]
    remote = manager._config_from_mapping("remote", {"transport": "http", "url": "${VAULT_URL}"}, source="user", priority=0, base_dir=tmp_path)
    assert remote.url == "https://fixture.invalid/mcp"


def test_mcp_restart_reloads_changed_variable_references_before_launch(execution_vault, tmp_path, monkeypatch):
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path)
    name = "plugin:tools@local:helper"
    old = MCPServerConfig(name=name, command="node", env={"VAULT_MCP": "old"})
    manager._servers[name] = MCPServerState(config=old)
    execution_vault.set("VAULT_MCP", "changed", scope=f"mcp:{name}")
    updated = manager._config_from_mapping(name, {"transport": "stdio", "command": "node", "env_vars": ["VAULT_MCP"]}, source="user", priority=0, base_dir=tmp_path)
    monkeypatch.setattr(manager, "load_config", lambda: [updated])
    monkeypatch.setattr(manager, "stop_server", AsyncMock(return_value=True))
    start = AsyncMock()
    monkeypatch.setattr(manager, "start_server", start)
    asyncio.run(manager.restart_server(name))
    start.assert_awaited_once_with(updated, force=True)
    assert updated.env["VAULT_MCP"] == "changed"
