from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.context import ContextBuilder
from backend.agent.mailbox_delivery import (
    inject_subagent_mailbox_updates as _inject_subagent_mailbox_updates,
)
from backend.agent.state import AgentState
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.swarm_tools import (
    MessageListTool,
    SendMessageTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskOutputTool,
    TaskUpdateTool,
    TeamCreateTool,
    TeamDeleteTool,
    TeamListTool,
)


def _subagent_fence(runtime: AgentRuntime, subagent_id: str) -> dict[str, object]:
    record = runtime.get_subagent(subagent_id)
    assert record is not None
    return {"agent_path": record.agent_path, "mailbox_epoch": record.mailbox_epoch}


def _context(runtime: AgentRuntime) -> tuple[ToolExecutionContext, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    return (
        ToolExecutionContext(
            permission=PermissionContext(),
            task_id="parent-task",
            conversation_id="conversation-1",
            emit_event=emit,
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        ),
        events,
    )


def test_send_message_records_and_emits_swarm_message(tmp_path) -> None:
    asyncio.run(_test_send_message_records_and_emits_swarm_message(tmp_path))


async def _test_send_message_records_and_emits_swarm_message(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context, events = _context(runtime)

    result = await SendMessageTool().execute(
        {"recipient": "subagent-abc12345", "message": "Please verify the parser."},
        context=context,
    )

    assert not result.is_error
    assert "sent from parent-run to subagent-abc12345" in result.content
    messages = runtime.list_swarm_messages(participant_id="subagent-abc12345")
    assert len(messages) == 1
    assert messages[0].content == "Please verify the parser."
    assert events == [
        (
            "subagent.event",
            {
                "subagent_id": "subagent-abc12345",
                "event": {"type": "message", "message": messages[0].public_dict()},
            },
        )
    ]


def test_shared_swarm_task_lifecycle(tmp_path) -> None:
    asyncio.run(_test_shared_swarm_task_lifecycle(tmp_path))


def test_lifecycle_response_fence_is_one_shot_and_incarnation_scoped(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    record = runtime.start_subagent(
        subagent_id="subagent-fenced",
        parent_run_id="parent-run",
        agent_type="general-purpose",
        teammate_name="worker",
        team_name="team-a",
        plan_mode_required=True,
    )
    runtime.update_subagent_lifecycle(
        record.subagent_id,
        agent_path=record.agent_path,
        mailbox_epoch=record.mailbox_epoch,
        awaiting_plan_approval=True,
        active_plan_request_id="plan-1",
    )

    token = runtime.reserve_lifecycle_response(
        response_kind="plan_approval_response",
        participant_id=record.subagent_id,
        mailbox_epoch=record.mailbox_epoch,
        request_id="plan-1",
        target_id="parent-run",
        expected_active_plan_request_id="plan-1",
    )
    assert token
    assert runtime.reserve_lifecycle_response(
        response_kind="plan_approval_response",
        participant_id=record.subagent_id,
        mailbox_epoch=record.mailbox_epoch,
        request_id="plan-1",
        target_id="parent-run",
        expected_active_plan_request_id="plan-1",
    ) == ""
    assert runtime.commit_lifecycle_response(
        response_kind="plan_approval_response",
        participant_id=record.subagent_id,
        mailbox_epoch=record.mailbox_epoch,
        request_id="plan-1",
        reservation_token=token,
    ) is True
    assert runtime.reserve_lifecycle_response(
        response_kind="plan_approval_response",
        participant_id=record.subagent_id,
        mailbox_epoch=record.mailbox_epoch,
        request_id="plan-1",
        target_id="parent-run",
        expected_active_plan_request_id="plan-1",
    ) == ""


def test_lifecycle_response_fence_allows_one_winner_under_concurrent_plan_and_shutdown_races(
    tmp_path,
) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")

    def race_reservations(*, response_kind: str, request_id: str, plan: bool) -> None:
        record = runtime.start_subagent(
            subagent_id=f"subagent-{response_kind}",
            parent_run_id="parent-run",
            agent_type="general-purpose",
            plan_mode_required=plan,
        )
        if plan:
            runtime.update_subagent_lifecycle(
                record.subagent_id,
                agent_path=record.agent_path,
                mailbox_epoch=record.mailbox_epoch,
                awaiting_plan_approval=True,
                active_plan_request_id=request_id,
            )

        def reserve() -> str:
            return runtime.reserve_lifecycle_response(
                response_kind=response_kind,
                participant_id=record.subagent_id,
                mailbox_epoch=record.mailbox_epoch,
                request_id=request_id,
                target_id="parent-run",
                expected_active_plan_request_id=request_id if plan else None,
            )

        with ThreadPoolExecutor(max_workers=32) as executor:
            tokens = list(executor.map(lambda _index: reserve(), range(128)))
        winners = [token for token in tokens if token]
        assert len(winners) == 1
        assert runtime.commit_lifecycle_response(
            response_kind=response_kind,
            participant_id=record.subagent_id,
            mailbox_epoch=record.mailbox_epoch,
            request_id=request_id,
            reservation_token=winners[0],
        ) is True
        assert reserve() == ""

    race_reservations(
        response_kind="plan_approval_response",
        request_id="plan-race",
        plan=True,
    )
    race_reservations(
        response_kind="shutdown_response",
        request_id="shutdown-race",
        plan=False,
    )


def test_runtime_preserves_caller_message_id_for_optimistic_reconciliation(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    message = runtime.send_swarm_message(
        sender_id="user",
        recipient_id="subagent-1",
        content="Check the edge cases.",
        conversation_id="conversation-1",
        message_id="msg-client-1",
    )
    assert message.message_id == "msg-client-1"
    assert runtime.list_swarm_messages(participant_id="subagent-1")[0].message_id == "msg-client-1"


def test_mailbox_messages_are_scoped_to_the_target_incarnation(tmp_path) -> None:
    asyncio.run(_test_mailbox_messages_are_scoped_to_the_target_incarnation(tmp_path))


async def _test_mailbox_messages_are_scoped_to_the_target_incarnation(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(
        run_id="parent-run",
        conversation_id="conversation-1",
    )
    first = runtime.start_subagent(
        subagent_id="subagent-reused",
        parent_run_id="parent-run",
        agent_type="reviewer",
    )
    stale = runtime.send_swarm_message(
        sender_id="parent-run",
        recipient_id="subagent-reused",
        content="old incarnation instruction",
        conversation_id="conversation-1",
    )
    assert stale.recipient_mailbox_epoch == first.mailbox_epoch
    runtime.complete_subagent(
        "subagent-reused",
        "completed",
        **_subagent_fence(runtime, "subagent-reused"),
    )

    resumed_prompts: list[str] = []

    class ResumeTaskTool:
        async def resume_background_subtask(self, *, subagent_id, prompt, context):
            resumed_prompts.append(prompt)
            runtime.start_subagent(
                subagent_id=subagent_id,
                parent_run_id="parent-run",
                agent_type="reviewer",
            )
            return subagent_id

    class Registry:
        def get_tool(self, name):
            return ResumeTaskTool() if name == "task" else None

    context, _events = _context(runtime)
    context.metadata["_tool_registry"] = Registry()
    resumed_result = await SendMessageTool().execute(
        {"recipient": "subagent-reused", "message": "resume with this instruction"},
        context=context,
    )
    assert not resumed_result.is_error
    assert resumed_result.status == "running"
    assert resumed_prompts == ["resume with this instruction"]

    second = runtime.get_subagent("subagent-reused")
    assert second is not None
    assert second.mailbox_epoch == first.mailbox_epoch + 1
    current = runtime.list_swarm_messages(
        participant_id="subagent-reused",
        conversation_id="conversation-1",
        mailbox_epoch=second.mailbox_epoch,
    )
    assert current == []


def test_subagent_mailbox_injection_seals_stale_epoch_and_advances_highwater(tmp_path) -> None:
    asyncio.run(_test_subagent_mailbox_injection_seals_stale_epoch_and_advances_highwater(tmp_path))


async def _test_subagent_mailbox_injection_seals_stale_epoch_and_advances_highwater(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
    first = runtime.start_subagent(
        subagent_id="subagent-mailbox",
        parent_run_id="parent-run",
        agent_type="reviewer",
    )
    stale = runtime.send_swarm_message(
        sender_id="parent-run",
        recipient_id="subagent-mailbox",
        content="stale instruction",
        conversation_id="conversation-1",
    )
    runtime.complete_subagent(
        "subagent-mailbox",
        "completed",
        **_subagent_fence(runtime, "subagent-mailbox"),
    )
    second = runtime.start_subagent(
        subagent_id="subagent-mailbox",
        parent_run_id="parent-run",
        agent_type="reviewer",
    )
    current = runtime.send_swarm_message(
        sender_id="parent-run",
        recipient_id="subagent-mailbox",
        content="current instruction",
        conversation_id="conversation-1",
    )
    assert stale.recipient_mailbox_epoch == first.mailbox_epoch
    assert current.recipient_mailbox_epoch == second.mailbox_epoch

    context = ContextBuilder()
    state = AgentState(user_message="continue")
    events: list[tuple[str, dict]] = []

    async def emit_event(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    injected = await _inject_subagent_mailbox_updates(
        ctx=context,
        state=state,
        metadata={
            "agent_mode": "subagent",
            "run_id": "subagent-mailbox",
        },
        conversation_id="conversation-1",
        emit_event=emit_event,
        run_context=RunContext(agent_runtime=runtime),
    )

    assert injected == 1
    assert "current instruction" in context._history[-1].content
    assert "stale instruction" not in context._history[-1].content
    assert state.prompt_context["subagent_mailbox_highwater:subagent-mailbox"] == current.seq
    assert events[-1][0] == "subagent.mailbox"
    assert events[-1][1]["conversation_id"] == "conversation-1"
    assert any(
        transition.get("reason") == "subagent_mailbox_stale_sealed"
        and transition.get("details", {}).get("stale_count") == 1
        for transition in state.transition_history
    )


def test_subagent_cannot_read_a_sibling_mailbox(tmp_path) -> None:
    asyncio.run(_test_subagent_cannot_read_a_sibling_mailbox(tmp_path))


async def _test_subagent_cannot_read_a_sibling_mailbox(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
    for subagent_id in ("subagent-a", "subagent-b"):
        runtime.start_subagent(
            subagent_id=subagent_id,
            parent_run_id="parent-run",
            agent_type="reviewer",
        )
    context = ToolExecutionContext(
        permission=PermissionContext(),
        conversation_id="conversation-1",
        metadata={
            "run_id": "subagent-a",
            "agent_mode": "subagent",
            **_subagent_fence(runtime, "subagent-a"),
        },
        run_context=RunContext(agent_runtime=runtime),
    )

    result = await MessageListTool().execute(
        {"participant_id": "subagent-b"},
        context=context,
    )

    assert result.is_error
    assert "only read their own mailbox" in result.content


def test_broadcast_mail_reaches_live_participants_and_sealed_senders_are_rejected(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
    runtime.start_subagent(
        subagent_id="subagent-live",
        parent_run_id="parent-run",
        agent_type="reviewer",
    )
    broadcast = runtime.send_swarm_message(
        sender_id="parent-run",
        recipient_id="all",
        content="team-wide update",
        conversation_id="conversation-1",
    )

    visible = runtime.list_swarm_messages(
        participant_id="subagent-live",
        conversation_id="conversation-1",
        mailbox_epoch=1,
    )
    assert [message.message_id for message in visible] == [broadcast.message_id]

    runtime.complete_subagent(
        "subagent-live",
        "completed",
        **_subagent_fence(runtime, "subagent-live"),
    )
    try:
        runtime.send_swarm_message(
            sender_id="subagent-live",
            recipient_id="parent",
            content="late terminal callback",
            conversation_id="conversation-1",
        )
    except ValueError as exc:
        assert "sealed subagent" in str(exc)
    else:  # pragma: no cover - explicit terminal safety assertion
        raise AssertionError("sealed subagent unexpectedly sent a mailbox message")


def test_parent_message_list_defaults_to_parent_mailbox(tmp_path) -> None:
    asyncio.run(_test_parent_message_list_defaults_to_parent_mailbox(tmp_path))


async def _test_parent_message_list_defaults_to_parent_mailbox(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
    runtime.start_subagent(
        subagent_id="subagent-child",
        parent_run_id="parent-run",
        agent_type="reviewer",
    )
    child_context = ToolExecutionContext(
        permission=PermissionContext(),
        conversation_id="conversation-1",
        metadata={
            "run_id": "subagent-child",
            "agent_mode": "subagent",
            **_subagent_fence(runtime, "subagent-child"),
        },
        run_context=RunContext(agent_runtime=runtime),
    )
    await SendMessageTool().execute(
        {"recipient": "parent", "message": "child result is ready"},
        context=child_context,
    )
    parent_context, _events = _context(runtime)

    listing = await MessageListTool().execute({}, context=parent_context)

    assert not listing.is_error
    assert "subagent-child -> parent: child result is ready" in listing.content


def test_new_parent_turn_can_steer_detached_child_but_sealed_old_run_cannot(tmp_path) -> None:
    asyncio.run(_test_new_parent_turn_can_steer_detached_child_but_sealed_old_run_cannot(tmp_path))


async def _test_new_parent_turn_can_steer_detached_child_but_sealed_old_run_cannot(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(run_id="parent-old", conversation_id="conversation-1")
    child = runtime.start_subagent(
        subagent_id="subagent-detached",
        parent_run_id="parent-old",
        agent_type="reviewer",
        background=True,
    )
    runtime.complete_run("parent-old", "completed")
    runtime.start_run(run_id="parent-new", conversation_id="conversation-1")
    new_context = ToolExecutionContext(
        permission=PermissionContext(),
        conversation_id="conversation-1",
        metadata={"run_id": "parent-new"},
        run_context=RunContext(agent_runtime=runtime),
    )

    delivered = await SendMessageTool().execute(
        {"recipient": child.subagent_id, "message": "continue with the new turn constraints"},
        context=new_context,
    )

    assert not delivered.is_error
    current = runtime.list_swarm_messages(
        participant_id=child.subagent_id,
        conversation_id="conversation-1",
        mailbox_epoch=child.mailbox_epoch,
    )
    assert [message.content for message in current] == ["continue with the new turn constraints"]

    try:
        runtime.send_swarm_message(
            sender_id="parent-old",
            recipient_id=child.subagent_id,
            content="late callback from completed parent",
            conversation_id="conversation-1",
        )
    except ValueError as exc:
        assert "sealed run" in str(exc)
    else:  # pragma: no cover - explicit terminal safety assertion
        raise AssertionError("sealed parent run unexpectedly sent mailbox mail")


def test_mailbox_epoch_and_pending_resume_message_survive_runtime_recreation(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    first_runtime = AgentRuntime(metrics_file=metrics_file)
    parent = first_runtime.start_run(run_id="parent-old", conversation_id="conversation-1")
    child = first_runtime.start_subagent(
        subagent_id="subagent-persisted",
        parent_run_id=parent.run_id,
        agent_type="reviewer",
    )
    first_runtime.complete_subagent(
        child.subagent_id,
        "completed",
        agent_path=child.agent_path,
        mailbox_epoch=child.mailbox_epoch,
    )
    first_runtime.complete_run(parent.run_id)
    pending = first_runtime.send_swarm_message(
        sender_id="user",
        recipient_id=child.subagent_id,
        content="resume after process recreation",
        conversation_id="conversation-1",
    )
    assert pending.recipient_mailbox_epoch == child.mailbox_epoch + 1

    restored = AgentRuntime(metrics_file=metrics_file)
    new_parent = restored.start_run(run_id="parent-new", conversation_id="conversation-1")
    resumed = restored.start_subagent(
        subagent_id=child.subagent_id,
        parent_run_id=new_parent.run_id,
        agent_type="reviewer",
    )
    messages = restored.list_swarm_messages(
        participant_id=child.subagent_id,
        conversation_id="conversation-1",
        mailbox_epoch=resumed.mailbox_epoch,
    )

    assert resumed.agent_path == child.agent_path
    assert resumed.mailbox_epoch == child.mailbox_epoch + 1
    assert [message.content for message in messages] == ["resume after process recreation"]


async def _test_shared_swarm_task_lifecycle(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context, events = _context(runtime)

    create_result = await TaskCreateTool().execute(
        {
            "title": "Audit multi-agent state",
            "description": "Check runtime snapshots and UI event flow.",
            "assignee": "subagent-audit",
            "priority": "high",
        },
        context=context,
    )
    task_id = runtime.list_swarm_tasks()[0].task_id
    assert task_id in create_result.content

    list_result = await TaskListTool().execute({"assignee": "subagent-audit"}, context=context)
    assert task_id in list_result.content
    assert "Audit multi-agent state" in list_result.content

    update_result = await TaskUpdateTool().execute(
        {"task_id": task_id, "status": "in_progress"},
        context=context,
    )
    assert "in_progress" in update_result.content

    output_result = await TaskOutputTool().execute(
        {
            "task_id": task_id,
            "content": "Runtime and UI events are connected.",
            "status": "completed",
        },
        context=context,
    )
    assert not output_result.is_error

    get_result = await TaskGetTool().execute({"task_id": task_id}, context=context)
    assert "completed" in get_result.content
    assert "Runtime and UI events are connected." in get_result.content

    task_events = [data["event"]["type"] for event_type, data in events if event_type == "subagent.event"]
    assert task_events == ["task_created", "task_updated", "task_output"]
    assert runtime.list_runs(include_subagents=True)["swarm_tasks"][0]["status"] == "completed"


def test_swarm_board_persists_across_runtime_instances(tmp_path) -> None:
    asyncio.run(_test_swarm_board_persists_across_runtime_instances(tmp_path))


async def _test_swarm_board_persists_across_runtime_instances(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    runtime = AgentRuntime(metrics_file=metrics_file)
    context, _events = _context(runtime)

    await SendMessageTool().execute(
        {"recipient": "subagent-persist", "message": "Persistent mailbox entry."},
        context=context,
    )
    await TaskCreateTool().execute(
        {"title": "Persist shared task", "assignee": "subagent-persist"},
        context=context,
    )

    restored = AgentRuntime(metrics_file=metrics_file)
    assert restored.list_swarm_messages(participant_id="subagent-persist")[0].content == "Persistent mailbox entry."
    assert restored.list_swarm_tasks(assignee="subagent-persist")[0].title == "Persist shared task"
    assert (tmp_path / "swarm" / "swarm.sqlite3").is_file()
    assert not list((tmp_path / "swarm").glob("*.lock"))


def test_swarm_task_dependencies_are_reciprocal(tmp_path) -> None:
    asyncio.run(_test_swarm_task_dependencies_are_reciprocal(tmp_path))


async def _test_swarm_task_dependencies_are_reciprocal(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context, _events = _context(runtime)

    await TaskCreateTool().execute({"title": "Prepare API"}, context=context)
    blocker_id = runtime.list_swarm_tasks()[0].task_id
    await TaskCreateTool().execute(
        {
            "title": "Build UI",
            "blocked_by": [blocker_id],
        },
        context=context,
    )
    blocked_id = [task.task_id for task in runtime.list_swarm_tasks() if task.task_id != blocker_id][0]

    blocker = runtime.get_swarm_task(blocker_id)
    blocked = runtime.get_swarm_task(blocked_id)
    assert blocker is not None
    assert blocked is not None
    assert blocked.blocked_by == [blocker_id]
    assert blocker.blocks == [blocked_id]

    get_result = await TaskGetTool().execute({"task_id": blocked_id}, context=context)
    assert f"Blocked by: {blocker_id}" in get_result.content




def test_swarm_task_single_record_access_is_conversation_scoped(tmp_path) -> None:
    asyncio.run(_test_swarm_task_single_record_access_is_conversation_scoped(tmp_path))


async def _test_swarm_task_single_record_access_is_conversation_scoped(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context_one, _events = _context(runtime)
    context_two = ToolExecutionContext(
        permission=PermissionContext(),
        task_id="parent-task-two",
        conversation_id="conversation-2",
        metadata={"run_id": "parent-run-two"},
        run_context=RunContext(agent_runtime=runtime),
    )

    await TaskCreateTool().execute({"title": "private task"}, context=context_one)
    task_id = runtime.list_swarm_tasks(conversation_id="conversation-1")[0].task_id

    get_result = await TaskGetTool().execute({"task_id": task_id}, context=context_two)
    update_result = await TaskUpdateTool().execute(
        {"task_id": task_id, "status": "completed"},
        context=context_two,
    )
    output_result = await TaskOutputTool().execute(
        {"task_id": task_id, "content": "cross-owner leak"},
        context=context_two,
    )

    assert get_result.is_error
    assert update_result.is_error
    assert output_result.is_error
    task = runtime.get_swarm_task(task_id, conversation_id="conversation-1")
    assert task is not None
    assert task.status == "pending"
    assert task.outputs == []


def test_swarm_task_dependencies_ignore_cross_conversation_ids(tmp_path) -> None:
    asyncio.run(_test_swarm_task_dependencies_ignore_cross_conversation_ids(tmp_path))


async def _test_swarm_task_dependencies_ignore_cross_conversation_ids(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context_one, _events = _context(runtime)
    context_two = ToolExecutionContext(
        permission=PermissionContext(),
        task_id="parent-task-two",
        conversation_id="conversation-2",
        metadata={"run_id": "parent-run-two"},
        run_context=RunContext(agent_runtime=runtime),
    )

    await TaskCreateTool().execute({"title": "conversation one blocker"}, context=context_one)
    blocker_id = runtime.list_swarm_tasks(conversation_id="conversation-1")[0].task_id
    await TaskCreateTool().execute(
        {"title": "conversation two blocked", "blocked_by": [blocker_id]},
        context=context_two,
    )
    blocked_id = runtime.list_swarm_tasks(conversation_id="conversation-2")[0].task_id

    blocked = runtime.get_swarm_task(blocked_id, conversation_id="conversation-2")
    blocker = runtime.get_swarm_task(blocker_id, conversation_id="conversation-1")
    assert blocked is not None
    assert blocker is not None
    assert blocked.blocked_by == []
    assert blocker.blocks == []


def test_swarm_high_water_incremental_reads(tmp_path) -> None:
    asyncio.run(_test_swarm_high_water_incremental_reads(tmp_path))


async def _test_swarm_high_water_incremental_reads(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context, _events = _context(runtime)

    await SendMessageTool().execute(
        {"recipient": "subagent-reader", "message": "first"},
        context=context,
    )
    first = await MessageListTool().execute({"participant_id": "subagent-reader"}, context=context)
    assert "first" in first.content
    assert "High-water: 1" in first.content

    await SendMessageTool().execute(
        {"recipient": "subagent-reader", "message": "second"},
        context=context,
    )
    second = await MessageListTool().execute(
        {"participant_id": "subagent-reader", "since_seq": 1},
        context=context,
    )
    assert "first" not in second.content
    assert "second" in second.content
    assert "High-water: 2" in second.content

    await TaskCreateTool().execute({"title": "task one"}, context=context)
    first_task_seq = runtime.list_swarm_tasks()[0].seq
    await TaskCreateTool().execute({"title": "task two"}, context=context)
    task_list = await TaskListTool().execute({"since_seq": first_task_seq}, context=context)
    assert "task one" not in task_list.content
    assert "task two" in task_list.content


def test_swarm_team_lifecycle_is_persistent_and_visible(tmp_path) -> None:
    asyncio.run(_test_swarm_team_lifecycle_is_persistent_and_visible(tmp_path))


async def _test_swarm_team_lifecycle_is_persistent_and_visible(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context, events = _context(runtime)

    create_result = await TeamCreateTool().execute(
        {
            "team_name": "release-squad",
            "description": "Coordinate implementation and verification.",
            "members": [
                {"id": "implementer", "role": "code changes", "agent_type": "implement"},
                {"id": "verifier", "role": "test and review", "agent_type": "verification"},
            ],
        },
        context=context,
    )
    assert not create_result.is_error
    assert "release-squad" in create_result.content

    list_result = await TeamListTool().execute({}, context=context)
    assert "release-squad" in list_result.content
    assert "team-lead@release-squad: team-lead [team-lead]" in list_result.content

    restored = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    teams = restored.list_swarm_teams(conversation_id="conversation-1")
    assert len(teams) == 1
    assert teams[0].team_name == "release-squad"
    assert [member.id for member in teams[0].members] == ["team-lead@release-squad"]

    delete_result = await TeamDeleteTool().execute({"team_name": "release-squad"}, context=context)
    assert not delete_result.is_error
    assert restored.list_swarm_teams(conversation_id="conversation-1") == []
    assert runtime.list_swarm_teams(conversation_id="conversation-1") == []

    event_types = [data["event"]["type"] for event_type, data in events if event_type == "subagent.event"]
    assert event_types == ["team_created", "team_deleted"]


def test_swarm_team_create_rejects_second_team_for_same_leader(tmp_path) -> None:
    asyncio.run(_test_swarm_team_create_rejects_second_team_for_same_leader(tmp_path))


async def _test_swarm_team_create_rejects_second_team_for_same_leader(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    context, _events = _context(runtime)

    first = await TeamCreateTool().execute({"team_name": "audit"}, context=context)
    second = await TeamCreateTool().execute({"team_name": "audit"}, context=context)

    assert not first.is_error
    assert second.is_error
    assert 'Already leading team "audit"' in second.content
    teams = runtime.list_swarm_teams(conversation_id="conversation-1")
    assert len(teams) == 1
    assert teams[0].members[0].id == "team-lead@audit"
