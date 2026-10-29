from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.environment_selection import TurnEnvironmentSnapshot
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.mcp.environment_runtime import prepare_environment_capabilities
from backend.mcp.manager import MCPServerConfig, MCPServerManager


@pytest.mark.asyncio
async def test_environment_preparation_captures_host_layers_once_and_refreshes_them_next_step(tmp_path, monkeypatch):
    flags = FeatureFlags({"plugins": False, "executor_capability_discovery": False})
    source = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path,
        managed_settings_dir=tmp_path / "managed", requirements_path=tmp_path / "requirements.toml", feature_flags=flags)
    allowed = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.toml")), {}),))
    denied = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.toml")),
        {"denied_mcp_servers": [{"name": "controlled"}]}),))
    load_stack = Mock(side_effect=[allowed, denied])
    monkeypatch.setattr("backend.config.load_config_layer_stack", load_stack)
    plugins = Mock(return_value=[])
    monkeypatch.setattr(source, "_load_plugin_configs", plugins)
    monkeypatch.setattr(source, "_load_enterprise_configs", lambda: (False, []))
    config = MCPServerConfig("controlled", command="unused-test-transport", source="user", auto_start=False)
    monkeypatch.setattr(source, "_load_local_configs", lambda: [deepcopy(config)])
    monkeypatch.setattr(source, "_load_project_configs", lambda: [])
    # Exercise real preparation/catalog/policy owners without launching a peer.
    monkeypatch.setattr(MCPServerManager, "start_all", AsyncMock())
    snapshot = TurnEnvironmentSnapshot()
    prepared = []
    try:
        for index in range(2):
            current = await prepare_environment_capabilities(turn_environments=snapshot, host_skill_manager=None,
                host_mcp_manager=source, feature_flags=flags, thread_id="owner", turn_id=f"turn-{index}",
                session_source="desktop", isolated=True, environment_manager=None, local_cwd=tmp_path.as_uri(),
                previous=prepared[-1] if prepared else None)
            prepared.append(current)
            assert load_stack.call_count == index + 1
        plugins.assert_any_call(config_stack=allowed)
        plugins.assert_any_call(config_stack=denied)
        assert prepared[0].mcp_manager._runtime_configs[0].enabled is True
        assert prepared[1].mcp_manager._runtime_configs[0].enabled is False
        assert prepared[1].mcp_manager._runtime_configs[0].disabled_reason == "Blocked by MiniCode MCP policy"
        for call in load_stack.call_args_list:
            assert call.kwargs == {"cwd": tmp_path, "requirements_path": source._requirements_path,
                "managed_settings_dir": source._managed_settings_dir}
    finally:
        for item in prepared:
            await item.aclose()
        await snapshot.aclose()
        await source.stop_all()
