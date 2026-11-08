"""Ordinary startup and tool schemas do not activate native credential storage."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


def test_empty_stores_and_real_tool_catalog_do_not_import_keyring(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    script = r'''
import importlib.abc
from pathlib import Path
import sys

class NoCredentialDiscovery(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "keyring":
            raise AssertionError("Ordinary startup reached the native credential backend")

sys.meta_path.insert(0, NoCredentialDiscovery())
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayerStack
from backend.mcp.manager import MCPServerManager
from backend.mcp.oauth import TokenStore
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.toolsets import ToolsetPolicy
from backend.vault.store import EnvVault

workspace = Path(sys.argv[1])
vault = EnvVault(workspace / "vault.json")
assert vault.get("absent") is None
assert vault.list_names() == []
assert vault.inject_into_env() == {}
tokens = TokenStore(workspace / "tokens.json")
tokens.for_server("https://example.invalid/mcp").sdk_storage("fixture")
MCPServerManager(config_path=workspace / "mcp.json", workspace_root=None)
artifacts = ArtifactStore(storage_dir=workspace / "artifacts")
try:
    config = AppConfig(llm=LLMSettings(api_key=""), config_layer_stack=ConfigLayerStack(()))
    registry = build_tool_registry(artifacts, workspace_root=workspace, config=config, mcp_manager=None)
    schemas = registry.get_schemas(
        permission_checker=PermissionChecker(config.permissions, workspace),
        permission_context=PermissionContext(mode="confirm", workspace_root=workspace),
        toolset_policy=ToolsetPolicy(code_mode_only=True, environment_count=1),
    )
    executor = next(schema["function"] for schema in schemas if schema["function"]["name"] == "tool_exec")
    assert "tools.read_file" in executor["description"]
    assert "keyring" not in sys.modules
finally:
    artifacts.shutdown()
'''
    environment = {**os.environ, "PYTHONPATH": str(root),
        "MINICODE_USER_DATA_DIR": str(tmp_path), "MINICODE_STATE_ROOT": str(tmp_path),
        "MINICODE_CONFIG_DIR": str(tmp_path / "config"),
        "MINICODE_SYSTEM_CONFIG_FILE": str(tmp_path / "system.toml"),
        "MINICODE_REQUIREMENTS_FILE": str(tmp_path / "requirements.toml"),
        "MINICODE_MANAGED_SETTINGS_DIR": str(tmp_path / "managed")}
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path)],
        cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.asyncio
async def test_sdk_backend_initialization_runs_in_the_credential_worker(monkeypatch, tmp_path: Path):
    from backend.mcp import oauth

    caller = threading.get_ident()
    workers = []
    backend = SimpleNamespace(get_password=lambda service, name: "stored-token")

    def load():
        workers.append(threading.get_ident())
        return backend, (RuntimeError,), LookupError

    monkeypatch.setattr(oauth, "load_credential_backend", load)
    storage = oauth.SDKTokenStorage("fixture", "server", tmp_path / "tokens.json")
    assert await storage._get("tokens") == "stored-token"
    assert workers and all(worker != caller for worker in workers)


@pytest.mark.asyncio
async def test_sdk_cancellation_drains_backend_initialization_and_write(monkeypatch, tmp_path: Path):
    from backend.mcp import oauth

    entered, release = threading.Event(), threading.Event()
    writes = []
    backend = SimpleNamespace(set_password=lambda *args: writes.append(args))

    def load():
        entered.set()
        assert release.wait(5), "test must release the native credential worker"
        return backend, (RuntimeError,), LookupError

    monkeypatch.setattr(oauth, "load_credential_backend", load)
    storage = oauth.SDKTokenStorage("fixture", "server", tmp_path / "tokens.json")
    write = asyncio.create_task(storage._set("tokens", "new-token"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        write.cancel()
        await asyncio.sleep(0)
        assert not write.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert writes == [("fixture", "server:tokens", "new-token")]
