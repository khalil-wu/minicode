"""Actual child Guardian circuit breaking through QueryEngine, terminal and outbox."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from backend.agent.guardian.action import GuardianAction
from backend.agent.guardian.host import GuardianHostState
from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.tool_execution import run_tool_with_timeout
from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.tests.test_codex_guardian import ControlledRuntime
from backend.tests.test_source_v2_configuration_chain import v2_case
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["guardian_event", "guardian_handler", "user"])
async def test_real_child_stop_preserves_guardian_identity_and_partial_output(tmp_path, monkeypatch, stop):
    hosts, captured = [], []
    rationale = "The requested action exceeds the user's authorized scope."
    monkeypatch.setattr(ControlledRuntime, "outputs", [json.dumps({"outcome": "deny", "rationale": rationale})] * 3)
    monkeypatch.setattr(ControlledRuntime, "instances", [])
    monkeypatch.setattr(ControlledRuntime, "on_run", None)

    class ControlledHost(GuardianHostState):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.service.pool.runtime_factory = ControlledRuntime
            hosts.append(self)

    monkeypatch.setattr("backend.agent.query_engine.GuardianHostState", ControlledHost)

    class Probe(BaseTool):
        name = "guardian_probe"
        description = "Controlled review; performs no external action"
        tool_exposure = "core"
        permission = PermissionLevel.AUTO
        read_only = True

        def get_schema(self):
            return ToolSchema(self.name, self.description, {"type": "object",
                "properties": {"attempt": {"type": "integer"}}, "required": ["attempt"]})

        async def execute(self, args, context=None):
            captured.append(context)
            service = context.run_context.guardian_review_service
            if stop == "guardian_handler":
                async def interrupt(turn_id, message):
                    assert context.run_context.turn_abort_error["message"] == message
                    context.run_context.agent_runtime.cancel_subagent_task(context.metadata["agent_id"], reason="interrupted")

                service.interruption_handler = interrupt
            action = GuardianAction.freeze({"tool": "exec_command", "command": ["controlled-review-only"],
                "cwd": str(tmp_path), "sandbox_permissions": "use_default", "tty": False},
                call_id=context.tool_call_id, turn_id=context.turn_id,
                request_digest=context.authorization_digest, policy_identity=context.authorization_policy_identity,
                environment_id=context.environment_id)
            receipt = await service.decide(action, context, required=True)
            assert receipt.status == "denied", receipt
            return ToolResult(content=receipt.rationale, is_error=True, status="failed")

    config, registry, snapshot, _ = v2_case(tmp_path)
    config.agent = replace(config.agent, code_mode_only=False, max_iterations=5)
    config.llm = replace(config.llm, reasoning_effort="off", reasoning_effort_levels=("off",))
    registry.register(Probe())

    class Model(LLMAdapter):
        _settings = config.llm

        def __init__(self):
            self.calls = 0
            self.partial_visible = asyncio.Event()

        async def stream_chat(self, messages, tools=None, metadata=None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Original partial output", phase="final_answer")
                self.partial_visible.set()
                if stop == "user":
                    await asyncio.Event().wait()
            assert self.calls <= 3
            yield StreamEvent(StreamEventType.TOOL_CALL,
                tool_calls=[ToolCallEvent(f"review-{self.calls}", "guardian_probe", {"attempt": self.calls})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")

        async def simple_chat(self, messages, *, max_tokens=None):
            raise AssertionError("The controlled child must not start a side model request")

    model = Model()
    snapshot = replace(snapshot, config=config, llm=model, thinking_level="off")
    policy = effective_toolset_policy(base_policy=None, tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=tmp_path, permission_mode="confirm",
        tool_mode="direct", model_execution=snapshot)
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)
    runtime.start_run(run_id="root", session_id="session", conversation_id="conversation")
    checker = PermissionChecker(config.permissions, workspace_root=tmp_path)
    context = ToolExecutionContext(permission=checker.build_context(mode="confirm"), permission_checker=checker,
        workspace_root=tmp_path, session_id="session", conversation_id="conversation", llm=model,
        model_execution=snapshot, tool_registry=registry, run_context=RunContext(agent_runtime=runtime,
            model_execution=snapshot, multi_agent_version="v2"),
        metadata={"run_id": "root", ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy})
    context.run_context.context_builder = ContextBuilder()
    context.run_context.context_builder.append_user("Perform the controlled review")
    try:
        started = await run_tool_with_timeout(ToolCallEvent("spawn-worker", "spawn_agent",
            {"task_name": "worker", "message": "Perform the controlled review", "fork_turns": "all"},
            native_type="function_call", native_namespace="collaboration", native_name="spawn_agent"), registry, context)
        assert not started.is_error, started.content
        child_id = started.runtime_metadata["subagent_id"]
        if stop == "user":
            await model.partial_visible.wait()
            assert runtime.cancel_subagent_task(child_id, reason="interrupted") == "cancelled"
        await runtime.wait_for_subagent_execution(child_id)
        child = runtime.get_subagent(child_id)
        result = runtime.get_subagent_snapshot(child_id)["result"]
        assert child.status == result["status"] == "cancelled"
        assert result["content"] == "Original partial output"
        if stop == "user":
            assert result["error"] == "cancelled" and result["terminal_reason"] == "interrupted"
            assert runtime.list_parent_notifications(conversation_id="conversation") == []
        else:
            assert len(captured) == 3 and model.calls == 3
            abort_error = captured[-1].run_context.turn_abort_error
            assert abort_error["codex_error_info"] == "too_many_denials"
            assert result["terminal_reason"] == "guardian_denials" and result["error"] == abort_error["message"]
            assert "3 consecutive" in result["error"]
            notification, = runtime.list_parent_notifications(conversation_id="conversation")
            assert notification["status"] == "pending"
            assert notification["payload"]["codex_error_info"] == "too_many_denials"
            assert notification["payload"]["content"] == "Original partial output"
            snapshot = runtime.subagent_session(child_id).context_builder.export_snapshot()
            assert rationale in json.dumps(snapshot["guardian_evidence"])
            terminal = [event["payload"] for event in runtime.load_agent_transcript(child_id)["events"]
                if event["event_type"] == "terminal"][-1]
            assert terminal["error"] == {"message": abort_error["message"], "codex_error_info": "too_many_denials"}
    finally:
        await runtime.aclose(release_lease=True)
