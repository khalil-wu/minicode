import asyncio
from types import SimpleNamespace

import pytest
from mcp import types

from backend.agent.environment_selection import TurnEnvironmentSnapshot
from backend.agent.thread_sample import SampleCapture, SampleError
from backend.feature_flags import FeatureFlags
from backend.mcp.client import MCPClient, sample_interaction_handler
from backend.mcp.environment_runtime import prepare_environment_capabilities
from backend.mcp.manager import MCPServerConfig, MCPServerManager


@pytest.mark.asyncio
async def test_sample_disables_inherited_mcp_startup_without_disabling_skill_discovery(tmp_path):
    flags = FeatureFlags({"plugins": False, "executor_capability_discovery": False})
    source = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path, feature_flags=flags,
        runtime_configs=(MCPServerConfig("must-not-start", command="nonexistent-sample-peer", source="user"),))
    snapshot = TurnEnvironmentSnapshot()
    prepared = await prepare_environment_capabilities(turn_environments=snapshot, host_skill_manager=None,
        host_mcp_manager=source, feature_flags=flags, thread_id="sample", turn_id="sample-turn",
        session_source="thread_sample", isolated=False, environment_manager=None, local_cwd=tmp_path.as_uri(), mcp_disabled=True)
    try:
        assert prepared.mcp_manager._runtime_configs == ()
        assert not prepared.mcp_manager.pending_requests()
        assert prepared.skill_manager is not None
    finally:
        await prepared.aclose()
        await snapshot.aclose()
        await source.stop_all()


@pytest.mark.asyncio
async def test_actual_mcp_client_interaction_fails_the_private_sample(monkeypatch):
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    capture = SampleCapture()
    client = MCPClient("sample-mcp", elicitation_handler=capture.reject_interaction)
    client._active_tool_request_owners = {1: {"session_id": "sample", "conversation_id": "sample-thread"}}
    result = await client._sdk_elicitation_callback(SimpleNamespace(request_id=1),
        types.ElicitRequestURLParams(mode="url", message="Interaction required", url="https://example.test/sample", elicitationId="e"))
    assert result.code == types.INTERNAL_ERROR
    with pytest.raises(SampleError, match="requires client interaction"):
        await capture.result


@pytest.mark.asyncio
async def test_initialization_interaction_without_a_tool_owner_still_fails_sample():
    capture = SampleCapture()
    client = MCPClient("starting-sample-mcp")
    token = sample_interaction_handler.set(capture.reject_interaction)
    try:
        task = asyncio.create_task(client._sdk_elicitation_callback(SimpleNamespace(request_id=1),
            types.ElicitRequestURLParams(mode="url", message="Startup requires interaction", url="https://example.test/startup", elicitationId="e")))
    finally:
        sample_interaction_handler.reset(token)
    with pytest.raises(SampleError, match="requires client interaction"):
        await task
    with pytest.raises(SampleError, match="requires client interaction"):
        await capture.result
