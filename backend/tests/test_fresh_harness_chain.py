"""Fresh control/wire/product regressions found by the 2026-09-30 audit."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.artifact.store import ArtifactStore
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.anthropic_protocol import anthropic_tool_input_schema
from backend.llm.errors import classify_llm_error
from backend.services.checkpoint_service import RunCheckpointResumeResult
from backend.tools.agent_tools import TaskTool
from backend.tools.base import validate_tool_input
from backend.ws.handlers.misc import handle_agent_resume


def test_messages_projects_task_union_without_weakening_execution_contract(tmp_path):
    tool = TaskTool(artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"))
    canonical = tool.get_schema().parameters
    original = deepcopy(canonical)
    model_tool = tool.model_schema().to_openai_tool()
    wire = AnthropicAdapter._convert_tools([model_tool])[0]["input_schema"]
    assert set(wire).isdisjoint({"anyOf", "oneOf", "allOf"})
    assert set(wire["properties"]) == set(canonical["properties"])
    assert "parallel_tasks" in wire["description"]
    assert canonical == original
    assert validate_tool_input(tool, {})
    assert not validate_tool_input(tool, {"description": "inspect", "prompt": "inspect source"})
    assert not validate_tool_input(tool, {"parallel_tasks": [
        {"description": "amounts", "prompt": "inspect amounts"},
        {"description": "stock", "prompt": "inspect stock"},
    ]})


def test_messages_preserves_nested_unions_and_root_branch_fields():
    schema = {"type": "object", "properties": {
        "mode": {"oneOf": [{"const": "fast"}, {"const": "slow"}]},
    }, "oneOf": [
        {"properties": {"path": {"type": "string"}}, "required": ["path"]},
        {"properties": {"id": {"type": "integer"}}, "required": ["id"]},
    ]}
    original = deepcopy(schema)
    projected = anthropic_tool_input_schema(schema)
    assert "oneOf" not in projected
    assert set(projected["properties"]) == {"mode", "path", "id"}
    assert projected["properties"]["mode"] == schema["properties"]["mode"]
    assert "required" not in projected
    assert schema == original
    projected["properties"]["mode"]["oneOf"][0]["const"] = "changed"
    assert schema == original


def test_messages_root_allof_retains_required_fields():
    projected = anthropic_tool_input_schema({"type": "object", "required": ["base"], "allOf": [
        {"properties": {"x": {"type": "string"}}, "required": ["x"]},
        {"properties": {"y": {"type": "number"}}, "required": ["y"]},
    ]})
    assert projected["required"] == ["base", "x", "y"]
    assert set(projected["properties"]) == {"x", "y"}
    assert "allOf" not in projected


def test_messages_schema_cache_keeps_live_descriptions():
    adapter = AnthropicAdapter(api_key="", model="claude-opus-4-8")
    first = {"type": "function", "function": {"name": "read", "description": "Use old_route", "parameters": {"type": "object"}}}
    second = deepcopy(first)
    second["function"]["description"] = "Use current_route"
    cached_first = adapter._convert_tools_cached([first])[0]
    cached_second = adapter._convert_tools_cached([second])[0]
    assert cached_second["description"] == "Use current_route"
    assert adapter._convert_tools_cached([second])[0] is cached_second
    assert cached_first["description"] == "Use old_route"


def test_schema_rejection_is_a_nonretryable_protocol_failure():
    request = httpx.Request("POST", "https://provider.invalid/v1/messages")
    response = httpx.Response(400, request=request, json={"error": {"message":
        '{"message":"input_schema does not support anyOf at the top level","reason":"TOOL_SCHEMA_INVALID"}'}})
    error = httpx.HTTPStatusError("Bad Request", request=request, response=response)
    classification = classify_llm_error(error)
    assert classification.provider_error_type == "protocol"
    assert classification.fatal is True
    assert classification.retryable is False


def _resume_session(start):
    return SimpleNamespace(session_id="session", active_conversation_id="conv",
        conversation_repo=None, resolve_requested_workspace=lambda _: Path.cwd(),
        session_lifecycle=SimpleNamespace(workspace_root=None, current_workspace_root=lambda: None),
        start_agent_run=start, send_payload=AsyncMock(return_value=True), send_event=AsyncMock(),
        emit_command_result=AsyncMock())


def _checkpoint():
    return RunCheckpointResumeResult(session_id="session", conversation_id="conv",
        run_id="checkpoint-old", iteration=3, stopped_reason="api_error", user_message="fix code")


def test_resume_success_is_published_only_after_turn_admission(monkeypatch):
    monkeypatch.setattr("backend.services.checkpoint_service.prepare_run_checkpoint_resume", lambda **_: _checkpoint())
    async def scenario():
        entered, admitted = asyncio.Event(), asyncio.Event()
        async def start(*_, **__):
            entered.set()
            await admitted.wait()
            return "new-task"
        session = _resume_session(start)
        handler = asyncio.create_task(handle_agent_resume(session, {"request_id": "request"}))
        await entered.wait()
        session.send_payload.assert_not_awaited()
        admitted.set()
        assert await handler
        payload = session.send_payload.await_args.args[0]
        assert payload["resumed"] is True
        assert payload["checkpoint_run_id"] == "checkpoint-old"
        assert "run_id" not in payload
        assert payload["request_id"] == "request"
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("conversation already has a live run"), ValueError("admission failed")])
def test_resume_rejected_at_admission_does_not_claim_success(monkeypatch, error):
    monkeypatch.setattr("backend.services.checkpoint_service.prepare_run_checkpoint_resume", lambda **_: _checkpoint())
    session = _resume_session(AsyncMock(side_effect=error))
    assert asyncio.run(handle_agent_resume(session, {"type": "agent.resume", "request_id": "request"}))
    payload = session.send_payload.await_args.args[0]
    assert payload["resumed"] is False
    assert payload["request_id"] == "request"
    assert str(error) in payload["message"]
    assert session.send_event.await_args.args[0].data["level"] == "error"


def test_native_runtime_discovery_uses_development_bundle_without_env_override(monkeypatch, tmp_path):
    from backend.sandbox import windows_native
    monkeypatch.setattr(windows_native, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MINICODE_WINDOWS_SANDBOX_EXECUTABLE", raising=False)
    monkeypatch.setenv("MINICODE_APP_RESOURCES_DIR", str(tmp_path))
    assert list(windows_native._candidate_executables()) == [
        tmp_path / "windows-sandbox/codex.exe", tmp_path / "desktop/windows-sandbox-runtime/codex.exe"]
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_EXECUTABLE", str(tmp_path / "explicit.exe"))
    assert list(windows_native._candidate_executables()) == [tmp_path / "explicit.exe"]


def test_partial_response_keeps_the_failure_visible_and_durable():
    from backend.agent.recovery_controller import RecoveryController, RecoveryDependencies, RecoveryProfile
    from backend.agent.stream_attempt import StreamTextState
    from backend.agent.state import AgentState
    from backend.llm.base import UsageInfo
    state = AgentState(user_message="fix code")
    context = SimpleNamespace(append_assistant=lambda _: None)
    projection = object()
    controller = RecoveryController(state=state, ctx=context, dependencies=RecoveryDependencies(
        scrub_thinking_tags=lambda text: text, usage_terminal_projection=lambda *a, **k: projection,
        run_stop_failure_hook=AsyncMock()))
    profile = RecoveryProfile.stream_interrupted(error_message="Provider content block never ended",
        error_type="api", failed_stopped_reason="api_error", recoverable=False,
        provider_error_type="protocol", error_code="nested_content_block_start")
    async def collect():
        return [event async for event in controller.finish(usage=UsageInfo(),
            stream_text=StreamTextState(iteration_id="iter:1"), full_text="I will inspect the files.",
            pending_tool_calls=[], profile=profile)]
    events = asyncio.run(collect())
    assert events[0].type == "error"
    assert events[0].data["error_code"] == "nested_content_block_start"
    assert events[0].data["provider_error_type"] == "protocol"
    assert events[-1] is projection
    assert state.terminal_status == "partial"
    assert state.reply == "I will inspect the files."


def test_slash_resume_dispatches_the_canonical_control_command(monkeypatch):
    from backend.commands import slash_commands
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(slash_commands, "_dispatch_command", dispatch)
    session = SimpleNamespace()
    assert asyncio.run(slash_commands._handle_resume(session, "", None)) == (True, "")
    dispatch.assert_awaited_once_with(session, "agent.resume", {"source": "slash:/resume"})
