"""Native policy is prepared alongside environments and joined before sampling."""
import asyncio
import threading

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.context import PermissionContext
from backend.sdk import create_session
from backend.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_policy", [False, True])
async def test_policy_worker_overlaps_discovery_and_failure_never_samples(tmp_path, monkeypatch, fail_policy):
    from backend.permissions import exec_policy
    from backend.agent import environment_runtime

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_owner = exec_policy.ExecPolicyOwner
    original_refresh = environment_runtime.refresh_run_environment_capabilities
    sampled = []

    def prepare_policy(**kwargs):
        entered.set()
        assert release.wait(5), "environment preparation never overlapped policy loading"
        try:
            if fail_policy:
                raise ValueError("actual policy preparation rejected")
            return original_owner(**kwargs)
        finally:
            finished.set()

    async def prepare_environment(*args, **kwargs):
        assert await asyncio.to_thread(entered.wait, 2), "policy preparation did not start alongside discovery"
        release.set()
        return await original_refresh(*args, **kwargs)

    class Model(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            assert finished.is_set()
            sampled.append(True)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Verified", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(self, messages):
            return ""

    monkeypatch.setattr(exec_policy, "ExecPolicyOwner", prepare_policy)
    monkeypatch.setattr(environment_runtime, "refresh_run_environment_capabilities", prepare_environment)
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.json")), {}),))
    config = AppConfig(llm=LLMSettings(api_key="fixture"), config_layer_stack=stack)
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    run = RunContext(agent_runtime=runtime)
    session = create_session(session_id="policy-preparation", workspace_root=tmp_path,
        context_builder=ContextBuilder(), run_context=run, config=config, llm=Model(),
        artifact_store=artifacts, tool_registry=ToolRegistry(), permission_context=PermissionContext(mode="bypass"))
    try:
        events = [event async for event in session.query("Answer after the actual policy is ready")]
        assert finished.is_set()
        if fail_policy:
            assert not sampled
            assert any(event.type == "error" for event in events), [(event.type, event.data) for event in events]
            assert any(event.type == "done" and event.data.get("status") == "failed" for event in events)
            assert run.exec_policy_owner is None
        else:
            assert sampled == [True]
            assert any(event.type == "done" and event.data.get("status") == "completed" for event in events)
            assert run.exec_policy_owner is session.context_builder._exec_policy_owner
    finally:
        release.set()
        await session.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)
