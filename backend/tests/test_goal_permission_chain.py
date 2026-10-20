import pytest

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import batch_tool_calls, execute_tool_batch
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.services.goal_runtime import new_goal
from backend.tools.agent_tools import TaskTool
from backend.tools.base import PermissionLevel
from backend.tools.collaboration_tools import SpawnAgentTool
from backend.tools.goal_tools import GoalTool
from backend.tools.registry import ToolRegistry
from backend.tools.swarm_tools import FollowupTaskTool, SourceSendMessageTool


async def run_goal_calls(repository, conversation_id, calls, *, denied=(), mode="confirm"):
    config = AppConfig(llm=LLMSettings(api_key="offline"))
    registry = ToolRegistry()
    for name in ("get_goal", "create_goal", "update_goal"):
        registry.register(GoalTool(name, config))
    checker = PermissionChecker(PermissionSettings())
    permission = checker.build_context(mode=mode, tool_deny_rules=list(denied))
    owner = RunContext(conversation_repository=repository)
    goal_events = []
    async def emit(kind, payload):
        if kind == "goal.updated":
            goal_events.append(payload)
    context = ToolExecutionContext(permission=permission, conversation_id=conversation_id,
        permission_checker=checker, run_context=owner, tool_registry=registry, emit_event=emit)
    async def unexpected_approval(*_args, **_kwargs):
        pytest.fail("Local goal state must not request filesystem/external approval")
    events = [event async for event in execute_tool_batch(calls, ctx=ContextBuilder(),
        state=AgentState(user_message="Explicitly finish this goal"), tool_registry=registry,
        permission_checker=checker, approval_handler=unexpected_approval,
        skill_manager=None, permission_context=permission, tool_ctx=context)]
    return registry, events, goal_events


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["confirm", "plan"])
async def test_local_goal_create_get_update_execute_serially_without_external_approval(tmp_path, mode):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    calls = [ToolCallEvent(id="create", name="create_goal", arguments={"objective": "Finish the explicit goal"}),
        ToolCallEvent(id="get", name="get_goal", arguments={}),
        ToolCallEvent(id="update", name="update_goal", arguments={"status": "complete"})]
    registry, events, goal_events = await run_goal_calls(repository, conversation.id, calls, mode=mode)
    assert batch_tool_calls(calls, registry) == [(False, [call]) for call in calls]
    results = [event.data for event in events if event.type == "tool_result"]
    assert len(results) == 3 and all(not result["is_error"] for result in results)
    assert [result["id"] for result in results] == ["create", "get", "update"]
    assert [event["goal"]["status"] for event in goal_events] == ["active", "active", "complete"]
    assert repository.get_conversation(conversation.id).goal["status"] == "complete"
    for call in calls:
        tool = registry.get_tool(call.name)
        assert tool.get_side_effect_kind(call.arguments) == "none"
        assert tool.is_read_only(call.arguments) is False
        assert not tool.is_concurrency_safe(call.arguments)
    assert not any(event.type == "control_request" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["confirm", "plan"])
@pytest.mark.parametrize("name,args", [("create_goal", {"objective": "Explicit goal"}),
    ("get_goal", {}), ("update_goal", {"status": "complete"})])
async def test_goal_tools_still_obey_explicit_denials(tmp_path, name, args, mode):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    if name != "create_goal":
        repository.update_goal(conversation.id, new_goal("Existing explicit goal"))
    before = repository.get_conversation(conversation.id).goal
    _registry, events, goal_events = await run_goal_calls(repository, conversation.id,
        [ToolCallEvent(id="denied", name=name, arguments=args)], denied=[name], mode=mode)
    results = [event.data for event in events if event.type == "tool_result"]
    assert len(results) == 1 and results[0]["is_error"]
    assert repository.get_conversation(conversation.id).goal == before
    assert goal_events == []


@pytest.mark.parametrize("name,args", [("spawn_agent", {"message": "Inspect source", "task_name": "worker"}),
    ("send_message", {"target": "/root/worker", "message": "Read the current file"}),
    ("followup_task", {"target": "/root/worker", "message": "Continue the assigned check"})])
def test_confirm_source_coordination_has_no_external_approval_floor_and_preserves_denials(tmp_path, name, args):
    task = TaskTool(artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"))
    tools = {"spawn_agent": SpawnAgentTool(task), "send_message": SourceSendMessageTool(), "followup_task": FollowupTaskTool()}
    tool = tools[name]
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    context = checker.build_context(mode="confirm")
    assert tool.get_side_effect_kind(args) == "none"
    assert checker.check(name, args, context=context, tool=tool) == PermissionLevel.AUTO
    denied = checker.build_context(mode="confirm", tool_deny_rules=[name])
    assert checker.check(name, args, context=denied, tool=tool) == PermissionLevel.ALWAYS_DENY
