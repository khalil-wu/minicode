import asyncio
import json
import pytest
from dataclasses import replace

from backend.agent.context import ContextBuilder
from backend.agent.message import UserCommand
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.turn_input import TurnInputQueue
from backend.agent.turn_state import complete_agent_message_block
from backend.agents.source_roles import source_agent_options
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.conversations.repository import ConversationRepository
from backend.conversations.public_projection import project_public_transcript_message
from backend.llm.base import UsageInfo
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.goal_runtime import GoalTurnAccounting, new_goal
from backend.services.conversation_goal_service import prepare_goal_action
from backend.tools.agent_user_tools import RequestUserInputAsyncTool
from backend.tools.collaboration_tools import ListAgentsTool, wait_for_coordination
from backend.tools.agent_control_plane import AgentControlPlane
from backend.tools.goal_tools import GoalTool
from backend.tools.agent_tools import _child_coordination_options
from backend.tools.subagent_context import build_agent_execution_profile, subagent_toolset_policy


def test_goal_tools_charge_non_cached_root_and_child_usage_and_preserve_budget_stop(tmp_path):
    async def run():
        repository = ConversationRepository(tmp_path / "conversations")
        conversation = repository.create_conversation()
        config = AppConfig(llm=LLMSettings(api_key="offline"))
        accounting = GoalTurnAccounting(repository, conversation.id)
        owner = RunContext(conversation_repository=repository, goal_accounting=accounting)
        context = ToolExecutionContext(permission=PermissionContext(), conversation_id=conversation.id, run_context=owner)
        create = GoalTool("create_goal", config)
        accepted = await create.execute({"objective": "Finish the actual change", "token_budget": 80}, context)
        assert json.loads(accepted.content)["goal"]["status"] == "active"
        assert not repository.get_conversation(conversation.id).context_snapshot.get("guardian_evidence")
        rejected = await create.execute({"objective": "Smaller replacement"}, context)
        assert rejected.is_error
        root, child = UsageInfo(), UsageInfo()
        accounting.register_usage("root", root)
        accounting.register_usage("child", child)
        root.input_tokens, root.cache_read_input_tokens, root.output_tokens = 100, 80, 20
        child.input_tokens, child.output_tokens = 30, 20
        goal = accounting.account()
        assert goal["tokens_used"] == 90
        assert goal["status"] == "budget_limited"
        assert accounting.account()["tokens_used"] == 90
        reply = await GoalTool("update_goal", config).execute({"status": "complete"}, context)
        assert json.loads(reply.content)["goal"]["status"] == "budget_limited"
        assert json.loads(reply.content)["remaining_tokens"] == 0
        restored = ConversationRepository(tmp_path / "conversations").get_conversation(conversation.id)
        assert restored.goal["text"] == "Finish the actual change"
        assert restored.goal["tokens_used"] == 90
    asyncio.run(run())


def test_goal_empty_and_execution_failure_audits_are_consecutive(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    repository.update_goal(conversation.id, new_goal("Goal with a real blocker"))
    for turn in range(3):
        accounting = GoalTurnAccounting(repository, conversation.id, automatic=True)
        goal = accounting.finish(status="completed", content="", has_activity=False)
        assert goal["status"] == ("blocked" if turn == 2 else "active")
    repository.update_goal(conversation.id, new_goal("Fresh goal"))
    GoalTurnAccounting(repository, conversation.id).execution_unavailable()
    GoalTurnAccounting(repository, conversation.id).finish(status="completed", content="Verified progress")
    for _ in range(2):
        assert GoalTurnAccounting(repository, conversation.id).execution_unavailable()["status"] == "active"
    assert GoalTurnAccounting(repository, conversation.id).execution_unavailable()["status"] == "blocked"


def test_async_questions_return_without_a_waiter_and_survive_turn_projection():
    async def run():
        events = []
        async def emit(kind, data):
            events.append((kind, data))
        context = ToolExecutionContext(permission=PermissionContext(), tool_call_id="question-1", emit_event=emit)
        result = await RequestUserInputAsyncTool().execute({"questions": [{"title": "Which module?"}]}, context)
        assert json.loads(result.content) == {"accepted": True}
        assert [kind for kind, _ in events] == ["item.started", "item.completed"]
        blocks = []
        complete_agent_message_block(blocks, events[-1][1]["item"])
        assert blocks[0]["delivery"] == "async"
        assert blocks[0]["questions"] == [{"title": "Which module?"}]
        assert blocks[0]["source"] == "async"
        restored = project_public_transcript_message({"id": "assistant", "role": "assistant", "content": "", "blocks": blocks})
        assert restored["blocks"][0]["questions"] == [{"title": "Which module?"}]
        assert restored["blocks"][0]["delivery"] == "async"
    asyncio.run(run())


def test_agent_wait_wakes_on_mail_and_steer_without_consuming_input(tmp_path):
    async def run():
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        root = runtime.start_run(conversation_id="owner", run_id="root-turn")
        child = runtime.start_subagent(subagent_id="child", parent_run_id=root.run_id, agent_type="default", task_id="worker", agent_path_segment="worker")
        queue = TurnInputQueue()
        queue.begin_turn(root.run_id)
        context = ToolExecutionContext(permission=PermissionContext(), conversation_id="owner", metadata={"run_id": root.run_id},
            run_context=RunContext(agent_runtime=runtime, turn_input_queue=queue))
        context.metadata["_agent_wait_cursor"] = runtime.agent_activity_cursor()
        pending = asyncio.create_task(wait_for_coordination(context, timeout_seconds=2))
        await asyncio.sleep(0)
        runtime.send_swarm_message(sender_id=child.subagent_id, recipient_id="parent", content="Need one detail", conversation_id="owner", sender_mailbox_epoch=child.mailbox_epoch)
        assert (await pending)["kind"] == "message"
        claims = runtime.claim_swarm_messages(participant_id="parent", mailbox_epoch=0, conversation_id="owner")
        assert runtime.ack_swarm_message_claims(claims) == 1
        pending = asyncio.create_task(wait_for_coordination(context, timeout_seconds=2))
        await asyncio.sleep(0)
        queue.enqueue_command(UserCommand(type="user_message", data={"content": "Use module A", "user_message_id": "answer", "conversation_id": "owner"}))
        assert (await pending)["kind"] == "steer"
        assert queue.pending_count() == 1
        agents = json.loads((await ListAgentsTool().execute({}, context)).content)["agents"]
        assert [agent["agent_name"] for agent in agents] == ["/root", "/root/worker"]
    asyncio.run(run())


def test_source_v2_limits_are_scoped_and_include_the_root(tmp_path):
    config = AppConfig(llm=LLMSettings(api_key="offline", model="gpt-6.1-sol"))
    options = source_agent_options(config)
    assert options["version"] == "v2" and options["max_children"] == 3
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    for conversation_id in ("first", "second"):
        runtime.start_run(conversation_id=conversation_id, run_id=conversation_id)
        runtime.configure_subagent_limit(conversation_id, 1)
    runtime.start_subagent(subagent_id="first-child", parent_run_id="first", agent_type="default")
    assert runtime.try_reserve_subagent_slots(["second-child"], conversation_id="second")
    assert not runtime.try_reserve_subagent_slots(["extra-child"], conversation_id="first")


def test_goal_continuation_is_developer_steering_and_not_human_input():
    async def run():
        context = ContextBuilder()
        message = await context.start_turn("Continue the user-provided goal", AgentState(user_message="Continue the goal"), input_role="developer")
        assert message.role == "developer"
        assert message.is_user_input is False
        assert message.accepted_input == {}
    asyncio.run(run())


def test_v2_names_and_scope_survive_the_next_root_turn(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(conversation_id="chat", run_id="old-root")
    runtime.configure_subagent_limit("chat", 3, source_v2=True)
    child = runtime.start_subagent(subagent_id="worker-id", parent_run_id="old-root", agent_type="default", task_id="worker", agent_path_segment="worker")
    runtime.complete_run("old-root")
    runtime.start_run(conversation_id="chat", run_id="next-root")
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="chat", metadata={"run_id": "next-root"},
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
    control = AgentControlPlane(context)
    assert control.resolve_target("/root/worker", scope="tree").subagent_id == child.subagent_id
    assert control.root_tree_record()["status"] == "running"
    with pytest.raises(RuntimeError, match="already owned"):
        runtime.validate_subagent_task_registration("duplicate", parent_run_id="next-root", canonical_task_name="worker", agent_path_segment="worker")
    runtime.start_run(conversation_id="other-chat", run_id="other-root")
    other = ToolExecutionContext(permission=PermissionContext(), conversation_id="other-chat", metadata={"run_id": "other-root"},
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
    assert AgentControlPlane(other).resolve_target("/root/worker", scope="tree") is None


@pytest.mark.parametrize("background", [False, True])
def test_v1_nested_delegation_grant_ends_at_the_configured_child_depth(tmp_path, background):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(conversation_id="chat", run_id="root")
    config = AppConfig(llm=LLMSettings(api_key="offline", model="fixture-v1"),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"), {"agents": {"max_depth": 2}}),)))
    root = ToolExecutionContext(permission=PermissionContext(), conversation_id="chat", metadata={"run_id": "root"},
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v1"))
    options = _child_coordination_options(root, config)
    assert options["version"] == "v1" and options["nested_delegation"]
    profile = build_agent_execution_profile(background=background, nested_delegation=options["nested_delegation"])
    policy = subagent_toolset_policy(execution_profile=profile)
    assert profile.can_delegate_background and profile.can_delegate_foreground
    assert "task" not in policy.disabled_tools
    child = runtime.start_subagent(subagent_id="child", parent_run_id="root", agent_type="default", agent_path_segment="child")
    context = replace(root, permission=PermissionContext(source="subagent:default"),
        metadata={"agent_id": child.subagent_id, "_agent_execution_profile": profile})
    leaf_options = _child_coordination_options(context, config)
    assert not leaf_options["nested_delegation"]
    leaf = build_agent_execution_profile(background=background, nested_delegation=leaf_options["nested_delegation"])
    assert "task" in subagent_toolset_policy(execution_profile=leaf).disabled_tools
    runtime.close(release_lease=True)


def test_goal_external_edit_steers_current_context_and_pause_stops_goal_work_without_cancelling_turn(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    original = new_goal("Original objective", token_budget=100)
    repository.update_goal(conversation.id, original)
    accounting = GoalTurnAccounting(repository, conversation.id)
    usage = UsageInfo(input_tokens=20)
    accounting.register_usage("root", usage)
    usage.output_tokens = 5
    current = accounting.account()
    updated = prepare_goal_action({"text": "Use <module A> & keep drafts"}, conversation_id=conversation.id, current_goal=current, source="frontend.ui")
    record = repository.update_goal(conversation.id, updated.next_goal)
    accounting.apply_external_update(record.goal, current)
    context = ContextBuilder()
    for prompt in accounting.take_steering():
        context.append_developer_context(prompt)
    assert accounting.take_steering() == []
    snapshot = context.export_snapshot()
    message = snapshot["history"][-1]
    assert message["role"] == "developer"
    assert "Use &lt;module A&gt; &amp; keep drafts" in message["content"]
    assert not message.get("is_user_input", False)
    assert not message.get("accepted_input")
    usage.output_tokens = 9
    assert accounting.account()["tokens_used"] == 9
    current = repository.get_conversation(conversation.id).goal
    paused = prepare_goal_action({"action": "pause"}, conversation_id=conversation.id, current_goal=current, source="frontend.ui")
    repository.update_goal(conversation.id, paused.next_goal)
    accounting.apply_external_update(paused.next_goal, current)
    usage.output_tokens = 40
    assert accounting.account() is None
    assert not repository.get_conversation(conversation.id).goal["_continuation_pending"]
    resumed = prepare_goal_action({"action": "resume"}, conversation_id=conversation.id, current_goal=paused.next_goal, source="frontend.ui")
    repository.update_goal(conversation.id, resumed.next_goal)
    accounting.apply_external_update(resumed.next_goal, paused.next_goal)
    usage.output_tokens = 43
    assert accounting.account()["tokens_used"] == 12


def test_external_goal_pause_preserves_exhausted_budget():
    goal = {**new_goal("Finish only with evidence", token_budget=10), "tokens_used": 10, "status": "budget_limited"}
    action = prepare_goal_action({"action": "pause"}, conversation_id="chat", current_goal=goal, source="frontend.ui")
    assert action.next_goal["status"] == "budget_limited"
    assert action.next_goal["_continuation_pending"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("version, expected_tool", [("v1", "task"), ("v2", None)])
async def test_real_child_provider_uses_the_parent_mode_and_nested_profile(tmp_path, version, expected_tool):
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.artifact.store import ArtifactStore
    from backend.config import PermissionSettings
    from backend.permissions.checker import PermissionChecker
    from backend.tests.test_source_agent_roles import RecordingModel, role_stack
    from backend.tools.agent_tools import TaskTool
    from backend.tools.registry import ToolRegistry

    # A V2 parent retains its actor protocol and nested profile. The receiving
    # V1 model still cannot advertise V2 tools or switch back to legacy task.
    config = role_stack(tmp_path, {"max_depth": 2})
    config.agent = replace(config.agent, code_mode_only=False)
    model = RecordingModel()
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", conversation_id="conversation")
    registry = ToolRegistry()
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    task = TaskTool(artifact_store=artifacts, workspace_root=tmp_path)
    registry.register(task)
    snapshot = ModelExecutionSnapshot(config=config, llm=model, provider="custom", model="source-model")
    parent = ToolExecutionContext(permission=checker.build_context(mode="bypass"), workspace_root=tmp_path,
        conversation_id="conversation", session_id="session", metadata={"run_id": "parent", "turn_id": "parent-turn"},
        tool_registry=registry, permission_checker=checker, llm=model, model_execution=snapshot,
        run_context=RunContext(agent_runtime=runtime, model_execution=snapshot, multi_agent_version=version))
    try:
        result = await task.execute({"description": "Inspect child surface", "prompt": "Report the assigned result.", "task_name": "worker"}, parent)
        assert not result.is_error, result.content
        names = {tool["function"]["name"] for tool in model.calls[0][1]}
        if expected_tool is not None:
            assert expected_tool in names
        else:
            from backend.tools.toolsets import SOURCE_V2_COORDINATION_TOOLS
            assert not names & (SOURCE_V2_COORDINATION_TOOLS | {"task"})
        record = runtime.get_subagent(result.runtime_metadata["subagent_id"])
        assert record.resume_config["source_spawn_version"] == version
        assert record.resume_config["execution_profile"]["delegation"] == "any"
    finally:
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_v2_sibling_idle_followup_uses_root_owners_and_preserves_nested_parent(tmp_path):
    from backend.agent.agent_identity import coordination_agent_id
    from backend.tools.swarm_tools import FollowupTaskTool

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    runtime.start_run(run_id="root-turn", conversation_id="chat")
    runtime.configure_subagent_limit("chat", 3, source_v2=True)
    branch = runtime.start_subagent(subagent_id="branch", parent_run_id="root-turn", agent_type="default", agent_path_segment="branch")
    sender = runtime.start_subagent(subagent_id="sender", parent_run_id="root-turn", agent_type="default", agent_path_segment="sender")
    recipient = runtime.start_subagent(subagent_id="nested", parent_run_id=branch.subagent_id, agent_type="default", agent_path_segment="nested")
    runtime.complete_subagent(recipient.subagent_id, "completed", agent_path=recipient.agent_path, mailbox_epoch=recipient.mailbox_epoch)
    bound_contexts, prompts, finished_receipts = [], [], []

    class ResumeTaskTool:
        def bind_mailbox_resumer(self, subagent_id, context, *, new_session=False):
            bound_contexts.append(context)
            async def resume(claims):
                prompts.extend(claim.message.content for claim in claims)
                runtime.start_subagent(subagent_id=subagent_id, parent_run_id=coordination_agent_id(context.metadata), agent_type="default")
                finished, started = (asyncio.get_running_loop().create_future() for _ in range(2))
                finished_receipts.append(finished)
                runtime.register_subagent_execution(subagent_id, finished, started)
                runtime.ack_swarm_message_claims(claims)
                started.set_result(None)
            runtime.bind_subagent_mailbox_resumer(subagent_id, resume, new_session=new_session)

    class Registry:
        def get_tool(self, name):
            return ResumeTaskTool() if name == "task" else None

    root_context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="chat", tool_registry=Registry(),
        workspace_root=tmp_path, metadata={"run_id": "root-turn", "turn_id": "root-turn"},
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
    source = replace(root_context, permission=PermissionContext(source="subagent:default"), tool_call_id="followup-call",
        metadata={"run_id": "sender-run", "agent_id": sender.subagent_id, "assistant_message_id": "sender-ui",
            "agent_path": sender.agent_path, "mailbox_epoch": sender.mailbox_epoch,
            "_agent_execution_profile": build_agent_execution_profile(multi_agent_v2=True)},
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2", coordination_root_context=root_context))
    try:
        result = await FollowupTaskTool().execute({"target": "/root/branch/nested", "message": "Follow up with the current result"}, source)
        assert not result.is_error, result.content
        assert prompts == ["Follow up with the current result"]
        assert bound_contexts[0].run_context is root_context.run_context
        assert bound_contexts[0].permission == root_context.permission
        assert bound_contexts[0].tool_call_id == "followup-call"
        assert bound_contexts[0].metadata["assistant_message_id"] == "sender-ui"
        resumed = runtime.get_subagent(recipient.subagent_id)
        assert resumed.parent_run_id == branch.subagent_id
        assert resumed.agent_path == recipient.agent_path
        assert resumed.mailbox_epoch == recipient.mailbox_epoch + 1
        runtime.complete_subagent(resumed.subagent_id, "completed", agent_path=resumed.agent_path, mailbox_epoch=resumed.mailbox_epoch)
        finished_receipts[0].set_result(None)
        await runtime._subagent_mailbox_turns[resumed.subagent_id][0]
    finally:
        runtime.close(release_lease=True)


def test_external_goal_provenance_is_durable_without_splitting_running_tool_results(tmp_path):
    from backend.agent.guardian.retained import GuardianEvidenceOwner
    from backend.llm.base import ToolCallEvent
    from backend.llm.responses_input import admitted_response_items
    from backend.tools.base import ToolResult

    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    context = ContextBuilder()
    context.append_assistant_tool_calls([ToolCallEvent(id="read-1", name="read_file", arguments={"path": "feature.py"})])
    before = context.export_snapshot()
    saved, message = repository.append_user_goal_update(conversation.id,
        objective="Update feature.py and preserve drafts", context_builder=context)
    assert saved.goal == {} and saved.transcript == []
    assert context.history_length == len(before["history"])
    assert saved.context_snapshot["history"] == before["history"]
    retained = GuardianEvidenceOwner.from_checkpoint(saved.context_snapshot["guardian_evidence"])
    assert retained.retained.user_messages[-1].value.complete
    assert "Update feature.py and preserve drafts" in retained.retained.user_messages[-1].value.text
    assert context.guardian_evidence.retained.user_messages[-1].value == retained.retained.user_messages[-1].value
    context.append_tool_result("read-1", "read_file", ToolResult(content="actual file content"))
    context.append_recorded_user_goal_update(message)
    assert [item["role"] for item in context.export_snapshot()["history"]] == ["assistant", "tool", "user"]
    assert message.is_user_input is False and message.accepted_input["kind"] == "user.goal"
    projected = admitted_response_items(message, {"role": "user", "content": message.content})
    assert projected[0]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] == ["user.goal"]
    assert projected[0]["id"] == message.accepted_input["model_item_id"]
    assert context.latest_admitted_turn_id() is None
    count = context.history_length
    context.append_recorded_user_goal_update(message)
    assert context.history_length == count


def test_goal_objective_evidence_limit_omits_the_whole_objective_and_survives_reload(tmp_path):
    from backend.llm.responses_input import admitted_response_items

    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    objective = "Preserve all restrictions: " + "中" * 300
    saved, message = repository.append_user_goal_update(conversation.id, objective=objective)
    assert objective not in message.content
    assert message.accepted_input["kind"] == "user.goal.omitted"
    projected = admitted_response_items(message, {"role": "user", "content": message.content})
    assert projected[0]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] == ["user.goal.omitted"]
    restored = ContextBuilder()
    restored.load_snapshot(saved.context_snapshot)
    assert restored.export_snapshot()["history"][-1]["accepted_input"]["kind"] == "user.goal.omitted"
    assert not restored.guardian_evidence.retained.user_messages[-1].value.complete
    assert restored.guardian_evidence.root_projection()[1] is False


def test_failed_goal_instruction_persistence_leaves_live_authorization_unchanged(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    context = ContextBuilder()
    before = context.export_snapshot()
    def fail(_record):
        raise OSError("Actual storage write failure")
    monkeypatch.setattr(repository, "_commit_record", fail)
    with pytest.raises(OSError, match="Actual storage write failure"):
        repository.append_user_goal_update(conversation.id, objective="Authorize actual goal work", context_builder=context)
    assert context.export_snapshot() == before
