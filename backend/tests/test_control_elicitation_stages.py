from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.control_tools import ControlToolRouter, RoutedToolResult
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.public_projection import project_public_tool_call
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.agent_user_tools import RequestUserInputTool
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import AgentExecutionProfile, AGENT_EXECUTION_PROFILE_METADATA_KEY
from backend.tools.toolsets import ToolsetPolicy
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.handler import WebSocketSession
from backend.ws.run_manager import SessionRunManager
from backend.ws.stream_state import create_stream_state, apply_stream_event

QUESTIONS = [{"id": "storage", "header": "Storage", "question": "Where should the output go?",
              "options": [{"label": "Workspace (Recommended)", "description": "Keep it in the current workspace."},
                          {"label": "Folder", "description": "Use a separate folder."}]},
             {"id": "format", "header": "Format", "question": "Which format should be used?",
              "options": [{"label": "Text (Recommended)", "description": "Save an editable text file."},
                          {"label": "PDF", "description": "Save a printable document."}]}]
ANSWERS = {"storage": {"answers": ["Workspace (Recommended)"]}, "format": {"answers": ["custom format"]}}


def context_for(tmp_path, *, permission_mode="confirm", child=False):
    context = ToolExecutionContext(permission=PermissionContext(mode=permission_mode, workspace_root=tmp_path),
        workspace_root=tmp_path, conversation_id="input-conversation", run_context=RunContext())
    if child:
        context.metadata[AGENT_EXECUTION_PROFILE_METADATA_KEY] = AgentExecutionProfile()
    return context


def state_for(mode="plan"):
    state = AgentState(user_message="Clarify the output")
    state.prompt_context["collaboration_mode"] = mode
    return state


def call_for(questions=None):
    return ToolCallEvent(id="input-call", name="request_user_input", arguments={"questions": deepcopy(QUESTIONS if questions is None else questions)})


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"answers": ANSWERS}, {"answers": {"format": ANSWERS["format"]}}, {"answers": {}}])
async def test_canonical_questions_yield_before_wait_and_answers_preserve_skip(tmp_path, response):
    waiter = AsyncMock(return_value=response)
    hooks = SimpleNamespace(run_elicitation=AsyncMock(side_effect=AssertionError("builtin input is not MCP")),
                            run_elicitation_result=AsyncMock(side_effect=AssertionError("builtin input is not MCP")))
    router = ControlToolRouter(state=state_for(), approval_handler=waiter, skill_manager=None,
        tool_context=context_for(tmp_path), hook_manager=hooks)
    call = call_for()
    stream = router.run(call)
    event = await anext(stream)
    assert event.type == "request_user_input"
    assert event.data["isBlocking"] is True
    assert event.data["questions"] == [dict(question, isOther=True, isSecret=False) for question in QUESTIONS]
    assert call.arguments["questions"] == QUESTIONS
    waiter.assert_not_called()
    result = await anext(stream)
    assert isinstance(result, RoutedToolResult)
    assert json.loads(result.result.content) == response
    assert not result.result.is_error
    waiter.assert_awaited_once_with("input-call")
    hooks.run_elicitation.assert_not_called()
    hooks.run_elicitation_result.assert_not_called()
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,permission_mode,default_enabled,child,expected", [
    ("plan", "plan", False, False, None), ("plan", "bypass", False, False, None),
    ("default", "plan", False, False, "unavailable in Default mode"),
    ("default", "confirm", True, False, None),
    ("plan", "bypass", True, True, "only be used by the root thread"),
])
async def test_root_mode_gate_is_independent_from_filesystem_permissions(tmp_path, mode, permission_mode, default_enabled, child, expected):
    waiter = AsyncMock(return_value={"answers": {}})
    tool = RequestUserInputTool(default_mode_enabled=default_enabled)
    router = ControlToolRouter(state=state_for(mode), approval_handler=waiter, skill_manager=None,
        tool_context=context_for(tmp_path, permission_mode=permission_mode, child=child), available_modes=tool.available_modes)
    events = [event async for event in router.run(call_for())]
    if expected:
        assert len(events) == 1 and events[0].result.is_error
        assert expected in events[0].result.content
        waiter.assert_not_called()
    else:
        assert events[0].data["isBlocking"] is (mode == "plan")
        assert json.loads(events[1].result.content) == {"answers": {}}


@pytest.mark.asyncio
async def test_empty_options_reject_before_question_and_user_cancel_is_real_failure(tmp_path):
    waiter = AsyncMock(return_value={"action": "cancel"})
    router = ControlToolRouter(state=state_for(), approval_handler=waiter, skill_manager=None, tool_context=context_for(tmp_path))
    bad = deepcopy(QUESTIONS); bad[0]["options"] = []
    events = [event async for event in router.run(call_for(bad))]
    assert len(events) == 1 and "non-empty options" in events[0].result.content
    waiter.assert_not_called()
    events = [event async for event in router.run(call_for())]
    assert events[1].result.status == "cancelled" and events[1].result.is_error
    assert events[1].result.content == "request_user_input was cancelled before receiving a response"


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [ANSWERS, {"storage": {"answers": ["custom " * 12_000]}}])
async def test_real_control_pipeline_persists_answers_and_reopens_journal(tmp_path, answers):
    registry = ToolRegistry(); registry.register(RequestUserInputTool())
    context = context_for(tmp_path, permission_mode="plan")
    state = state_for(); builder = ContextBuilder(conversation_id="input-conversation", workspace_root=tmp_path)
    runner = ToolBatchRunner(ctx=builder, state=state, tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), approval_handler=AsyncMock(return_value={"answers": answers}),
        skill_manager=None, permission_context=context.permission, tool_ctx=context)
    events = [event async for event in runner.run([call_for()])]
    assert not any(event.type == "approval_request" for event in events)
    assert sum(event.type == "request_user_input" for event in events) == 1
    start = next(event for event in events if event.type == "tool_call")
    result = next(event for event in events if event.type == "tool_result")
    assert result.data["status"] == "success" and json.loads(result.data["summary"]) == {"answers": answers}
    assert result.data.get("artifact_id") is None
    assert builder._enforce_per_message_tool_budget() == 0
    journal = ExecutionJournal("input_run", base_dir=tmp_path / "journal")
    journal.append_tool_use(start.data)
    journal.append_tool_result(result.data, tool_name="request_user_input")
    replay = ExecutionJournal("input_run", base_dir=tmp_path / "journal").read_events()
    assert replay[0].payload["tool_call"]["arguments"] == {"questions": QUESTIONS}
    assert json.loads(replay[-1].payload["content"]) == {"answers": answers}
    public = project_public_tool_call({**replay[-1].payload, "id": "input-call", "name": "request_user_input", "summary": replay[-1].payload["content"]})
    if answers is ANSWERS:
        assert json.loads(public["summary"]) == {"answers": answers}
    restored = ContextBuilder(conversation_id="input-conversation", workspace_root=tmp_path)
    restored.load_snapshot(builder.export_snapshot())
    assert any(message.role == "tool" and json.loads(message.content) == {"answers": answers} for message in restored._history)


@pytest.mark.asyncio
async def test_owned_waiter_wire_ack_and_recovery_keep_all_questions(tmp_path):
    session = object.__new__(WebSocketSession)
    session.session_id = "input-session"
    session.config = SimpleNamespace(agent=SimpleNamespace(approval_timeout_seconds=0.001))
    session.approval_diff_cache = {}
    session.send_event = AsyncMock()
    session.emit_command_result = AsyncMock()
    router = ControlToolRouter(state=state_for(), approval_handler=session.approval_handler,
        skill_manager=None, tool_context=context_for(tmp_path))
    stream = router.run(call_for())
    event = await anext(stream)
    event.data.update(conversation_id="input-conversation", turn_id="input-turn", message_id="input-message")
    payload = session._build_ws_payload(event)
    assert payload["request"]["subtype"] == "request_user_input"
    assert payload["request"]["questions"] == event.data["questions"]
    session.turn_wait_state.pending_approval_payloads["input-call"] = payload
    streams = {"input-conversation": create_stream_state("input-conversation", "input-message", "input-turn")}
    apply_stream_event(streams, "input-conversation", event.type, event.data)
    response_task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert "input-call" in session.turn_wait_state.pending_user_input
    assert not session.turn_wait_state.pending_approvals and not session.turn_wait_state.pending_elicitations
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "commands")
    data = {"request_id": "input-call", "conversation_id": "input-conversation", "turn_id": "input-turn", "message_id": "input-message",
            "response": {"subtype": "success", "response": {"answers": ANSWERS}}}
    await dispatcher._handle_control_response(UserCommand(type="control_response", data={**data, "conversation_id": "foreign"}))
    assert not response_task.done()
    await dispatcher._handle_control_response(UserCommand(type="control_response", data=data))
    result = await response_task
    assert json.loads(result.result.content) == {"answers": ANSWERS}
    assert session.send_event.await_args.args[0].type == "command.result"
    assert session.send_event.await_args.args[0].data["level"] == "success"
    assert not session.turn_wait_state.pending_user_input and not session.turn_wait_state.pending_approval_payloads
    await stream.aclose()


def test_canonical_schema_and_code_mode_exposure_use_the_source_contract(tmp_path):
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"), workspace_root=tmp_path, config=AppConfig(llm=LLMSettings(api_key="test-input-fixture")))
    assert registry.get_tool("ask_user") is None
    tool = registry.get_tool("request_user_input")
    assert tool.available_modes == ("plan",)
    assert FeatureFlags().enabled("default_mode_request_user_input") is False
    view = next(view for view in registry.build_schema_views(toolset_policy=ToolsetPolicy(code_mode_only=True),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), permission_context=PermissionContext(mode="plan", workspace_root=tmp_path))
        if view.name == "request_user_input")
    assert view.direct and not view.code_mode_available
    parameters = view.schema["function"]["parameters"]
    assert set(parameters["properties"]) == {"questions"} and parameters["required"] == ["questions"]
    question = parameters["properties"]["questions"]["items"]
    assert set(question["properties"]) == {"id", "header", "question", "options"}
    assert question["properties"]["options"]["items"]["required"] == ["label", "description"]
    assert tool.get_schema().strict is False
    assert view.schema["function"].get("strict", False) is False


@pytest.mark.asyncio
async def test_cancel_response_interrupts_the_owned_turn_and_retains_other_conversation_input(tmp_path):
    session = object.__new__(WebSocketSession)
    session.session_id = "cancel-input-session"
    session._cleanup_tasks = set()
    session.conversation_repo = SimpleNamespace(_base_dir=tmp_path / "conversations")
    session.config = SimpleNamespace(agent=SimpleNamespace(approval_timeout_seconds=None))
    session.approval_diff_cache = {}
    session.send_event = AsyncMock()
    session.session_lifecycle = SimpleNamespace(schedule_task_runtime_update=lambda: None)
    session._conversation_streams = {"input-conversation": create_stream_state("input-conversation", "input-message", "input-turn")}
    manager = SessionRunManager(session)
    manager.watch_conversation_notifications = lambda _id: None
    # No child agents exist in this scenario; the actual main task and pending
    # input use SessionRunManager.cancel and SessionApprovalRuntime cleanup.
    manager._cancel_run_tree = AsyncMock()
    session.run_manager = manager
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "cancel-commands")
    router = ControlToolRouter(state=state_for(), approval_handler=session.approval_handler,
        skill_manager=None, tool_context=context_for(tmp_path))
    stream = router.run(call_for())
    event = await anext(stream)
    event.data.update(conversation_id="input-conversation", turn_id="input-turn", message_id="input-message")
    session.turn_wait_state.pending_approval_payloads["input-call"] = session._build_ws_payload(event)
    foreign = asyncio.get_running_loop().create_future()
    session.turn_wait_state.register_waiter("foreign-input", foreign, kind="user_input")
    session.turn_wait_state.pending_approval_payloads["foreign-input"] = {"conversation_id": "foreign"}
    continued = []
    async def owned_turn():
        await anext(stream)
        continued.append("model continuation")
    task = asyncio.create_task(owned_turn())
    cancelled = asyncio.Event()
    manager.register(conversation_id="input-conversation", task=task, task_id="input-run",
        cancel_event=cancelled, active_conversation_id="input-conversation")
    await asyncio.sleep(0)
    try:
        await dispatcher._handle_control_response(UserCommand(type="control_response", data={
            "request_id": "input-call", "conversation_id": "input-conversation", "turn_id": "input-turn", "message_id": "input-message",
            "response": {"subtype": "success", "response": {"action": "cancel"}}}))
        assert cancelled.is_set() and task.cancelled()
        assert continued == [] and not foreign.done()
        assert session.turn_wait_state.pending_user_input == {"foreign-input": foreign}
        assert any(call.args[0].type == "approval.cancelled" and call.args[0].data["request_ids"] == ["input-call"]
                   for call in session.send_event.await_args_list)
        assert session.send_event.await_args.args[0].type == "command.result"
    finally:
        foreign.cancel()
        manager.stop_notification_wake_intake()
        manager.close_durable_queue()
        await stream.aclose()


def test_host_dependency_question_does_not_invent_a_model_tool_call_in_replay():
    streams = {"input-conversation": create_stream_state("input-conversation", "input-message", "input-turn")}
    apply_stream_event(streams, "input-conversation", "request_user_input", {
        "tool_call_id": "mcp-deps-owner", "questions": QUESTIONS, "isBlocking": True,
        "conversation_id": "input-conversation", "turn_id": "input-turn", "message_id": "input-message"})
    assert streams["input-conversation"]["tool_calls"] == {}
