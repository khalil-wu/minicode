"""Configured QueryEngine commands reach the actual native network prerequisite."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import sys

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.environment_selection import TurnEnvironmentRequest
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.environments import EnvironmentManager
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.terminal.manager import BackgroundCommandManager
from backend.tests.test_environments import configure
from backend.tests.test_managed_network_runtime import source_policy
from backend.tests.test_native_exec_policy_chain import setup
from backend.tests.test_query_native_command_start_admission_chain import NativeCommandModel, shell_path
from backend.tools.command_tool import RunCommandTool
from backend.workspace.executor_local import LocalExecutionEnvironmentBackend


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows managed-network setup prerequisite")
async def test_configured_query_keeps_frozen_network_policy_and_refuses_unprovisioned_workload(tmp_path):
    host = setup(tmp_path, approval_policy="never")
    controller = source_policy()["controller"]
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(host.home / "settings.json")),
        {"windows": {"sandbox": "elevated"}, "permissions": {"network": controller}}),))
    settings = AgentSettings(max_iterations=3, max_turn_seconds=30, stream_max_attempts=0, code_mode_only=False)
    budget = TokenBudget(total=96000)
    marker = host.workspace / "unprovisioned-workload-must-not-start"
    call = ToolCallEvent(id="actual-managed-network-command", name="run_command", arguments={
        "command": f"Set-Content -LiteralPath {shell_path(marker)} -Value 'must not start'", "login": False, "yield_time_ms": 10000})
    model = NativeCommandModel(host.snapshot.config.llm, call)
    admitted = replace(host.snapshot, config=replace(host.snapshot.config, agent=settings, token_budget=budget,
        config_layer_stack=stack), llm=model)
    captured = []

    class ObservedCommand(RunCommandTool):
        async def execute(self, arguments, context):
            request = context.final_tool_request
            policy = request.exec_policy.command.policy.network_policy
            assert policy is not None
            assert policy.identity()["environment_id"] == "local"
            assert policy.source_input["controller"] == controller
            assert request.digest == request.exec_policy.command.execution_digest
            captured.append(request)
            return await super().execute(arguments, context)

    host.registry.unregister("run_command")
    host.registry.register(ObservedCommand(host.context.artifact_store))
    config_dir = tmp_path / "executors"
    config_dir.mkdir()
    configure(config_dir, [], default="local", include_local=True)
    local = LocalExecutionEnvironmentBackend()
    manager = EnvironmentManager(config_dir, local_backend=local)
    background = BackgroundCommandManager(session_id="actual-network-prerequisite")
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    run = RunContext(agent_runtime=runtime, model_execution=admitted, environment_manager=manager,
        environment_requests=(TurnEnvironmentRequest("local", host.workspace.as_uri(), (host.workspace.as_uri(),)),))
    builder = ContextBuilder(llm=model, token_budget=budget, conversation_id="actual-network-query", workspace_root=host.workspace)
    session = AgentSession(llm=model, tool_registry=host.registry, artifact_store=host.context.artifact_store,
        permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), host.workspace),
        agent_settings=settings, token_budget=budget, context_builder=builder)
    state = AgentState(user_message="Run the command with the configured managed network.", conversation_id="actual-network-query", workspace_root=host.workspace)
    try:
        events = [event async for event in QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
            runtime=AgentLoopSessionContext(session_id="actual-network-prerequisite", workspace_root=host.workspace,
                run_context=run, background_manager=background, permission_context=PermissionContext(mode="confirm", approval_policy="never",
                    sandbox_mode="workspace-write", workspace_root=host.workspace), cancel_event=asyncio.Event())))]
        assert state.terminal_status == "completed", [(event.type, event.data) for event in events if event.type in {"error", "done"}]
        assert len(captured) == 1 and model.calls == 2
        result = next(event.data for event in events if event.type == "tool_result")
        assert result["is_error"] and "explicit MiniCode sandbox setup action" in result["summary"], result
        assert not marker.exists() and not local.process.processes
        native = manager.network_runtime()
        status = native.local_prerequisite.status()
        assert status["required"] and not status["ready"]
        assert not native.leases and not native.deciders
        command, = background._commands.values()
        assert command.status == "failed" and command.pid is None and not command.cleanup_pending
        assert command.exit_code is None
    finally:
        await background.shutdown()
        await session.aclose()
        host.context.artifact_store.shutdown()
        runtime.close(release_lease=True)
        await manager.close()
