from __future__ import annotations

import asyncio
from itertools import count
from types import SimpleNamespace

import pytest
import pytest_asyncio
from starlette.websockets import WebSocketState

from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.permissions.checker import PermissionChecker
from backend.tasks import scheduler as scheduler_module
from backend.tasks.scheduler import ScheduledTaskRun, TaskScheduler
from backend.tools.registry import ToolRegistry
from backend.ws.command_scope import resolve_command_scope
from backend.ws.handler import WebSocketSession
from backend.ws.handlers import mcp as handlers


class Socket:
    client_state = WebSocketState.CONNECTED
    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


async def drain_commands(session):
    dispatcher = session.command_dispatcher
    while dispatcher.command_tasks or dispatcher.execution_command_tasks:
        await asyncio.gather(*(dispatcher.command_tasks | dispatcher.execution_command_tasks))
    await session.event_outbox.drain_delivery()
    await session.event_outbox.drain_persistence()


@pytest_asyncio.fixture
async def resource_case(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")
    project_a, execution_a = tmp_path / "project-a", tmp_path / "project-a" / "worktrees" / "owned-a"
    root_b, root_c = tmp_path / "project-b", tmp_path / "project-b" / "child"
    for root in (execution_a, root_b, root_c):
        root.mkdir(parents=True, exist_ok=True)
    repository = ConversationRepository(tmp_path / "conversations")
    owner = repository.create_conversation(conversation_id="conv_resource_a", workspace_root=str(project_a),
        execution_cwd=str(execution_a), worktree_path=str(execution_a), git_isolated=True)
    reader = repository.create_conversation(conversation_id="conv_resource_b", workspace_root=str(root_b),
        transcript=[{"role": "user", "content": "Retain B's input"}],
        model_selection={"provider": "openai", "model": "reader-model", "reasoning_effort": "low"})
    target = repository.create_conversation(conversation_id="conv_resource_c", workspace_root=str(root_c))
    same_project = repository.create_conversation(conversation_id="conv_resource_d", workspace_root=str(root_b))
    scheduler = TaskScheduler()
    monkeypatch.setattr(handlers, "_get_scheduler", lambda _session: scheduler)
    sessions = []

    def new_session():
        socket = Socket()
        session = WebSocketSession(session_id="scheduler-resource-session", websocket=socket, llm=None,
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings(), root_b),
            config=AppConfig(llm=LLMSettings(api_key="fixture", model="reader-model")), mcp_manager=None)
        session.conversation_repo = repository
        session.active_conversation_id = reader.id
        session.session_lifecycle.workspace_root = root_b
        sessions.append(session)
        return session, socket

    session, socket = new_session()
    command_ids = count()

    async def dispatch(command_type, *, host=session, transport=socket, **fields):
        command_id = f"resource-command-{next(command_ids)}"
        command = UserCommand.from_ws_message({"type": command_type, "client_command_id": command_id, **fields})
        assert await host.command_dispatcher._dispatch_client_command(command, host.connection_generation)
        await host.event_outbox.drain_delivery()
        messages = [message for message in transport.messages if message.get("client_command_id") == command_id]
        receipt = next(message for message in messages if message["type"] == "command.result")
        return receipt, messages

    yield SimpleNamespace(session=session, socket=socket, scheduler=scheduler, repository=repository,
        owner=owner, reader=reader, target=target, same_project=same_project, execution_a=execution_a,
        root_b=root_b, root_c=root_c, dispatch=dispatch, new_session=new_session)
    for host in sessions:
        await drain_commands(host)
        await host.run_manager.shutdown_notification_wakes()
        host.run_manager.close_durable_queue()


@pytest.mark.asyncio
async def test_runs_in_reads_each_real_chat_scope_without_navigating_the_reader(resource_case):
    case = resource_case
    roots = {case.owner.id: case.execution_a, case.reader.id: case.root_b,
        case.target.id: case.root_c, case.same_project.id: case.root_b}
    tasks = {str(root): case.scheduler.add_task(f"Task at {root.name}", "Keep its owner", "0 9 * * *", workspace_root=str(root))
        for root in {case.execution_a, case.root_b, case.root_c}}
    for conversation_id, root in roots.items():
        receipt, messages = await case.dispatch("scheduler.list", owner_conversation_id=conversation_id, workspace_root=str(root))
        assert receipt["level"] == "success"
        assert receipt["conversation_id"] == receipt["data"]["conversation_id"] == conversation_id
        assert receipt["workspace_root"] == receipt["data"]["workspace_root"] == str(root)
        assert [task["id"] for task in receipt["data"]["tasks"]] == [tasks[str(root)].id]
        snapshot = next(message for message in messages if message["type"] == "scheduler.list")
        assert snapshot["conversation_id"] == conversation_id and snapshot["workspace_root"] == str(root)
        assert case.session.active_conversation_id == case.reader.id
        assert case.session.session_lifecycle.workspace_root == case.root_b
    with pytest.raises(ValueError, match="owner is stale"):
        resolve_command_scope(case.session, {"owner_conversation_id": case.owner.id, "workspace_root": str(case.execution_a)})


@pytest.mark.asyncio
async def test_hidden_editor_keeps_caller_target_and_standalone_chat_distinct_through_real_dispatch(resource_case):
    case = resource_case
    original_reader = case.repository.get_conversation(case.reader.id).to_dict()
    scope = {"owner_conversation_id": case.owner.id, "workspace_root": str(case.execution_a)}
    configuration = {"name": "A's automation", "prompt": "Continue B", "schedule": "RRULE:FREQ=MINUTELY;INTERVAL=30",
        "permission_mode": "auto", "timezone": "UTC", "isolation": "workspace", "kind": "heartbeat",
        "conversation_id": case.reader.id}
    added, _ = await case.dispatch("scheduler.add", **scope, **configuration)
    assert added["level"] == "info"
    task = added["data"]["task"]
    assert task["workspace_root"] == str(case.execution_a) and task["conversation_id"] == case.reader.id
    assert added["conversation_id"] == added["data"]["conversation_id"] == case.owner.id

    edited, _ = await case.dispatch("scheduler.update", **scope, **{**configuration, "task_id": task["id"],
        "prompt": "Continue C after autosave", "conversation_id": case.target.id})
    assert edited["level"] == "info" and edited["data"]["task"]["conversation_id"] == case.target.id
    directed, messages = await case.dispatch("scheduler.list", owner_conversation_id=case.target.id,
        workspace_root=str(case.root_c), task_workspace_root=str(case.execution_a))
    assert directed["level"] == "success" and directed["conversation_id"] == case.target.id
    assert directed["data"]["workspace_root"] == str(case.root_c)
    assert next(message for message in messages if message["type"] == "scheduler.list")["workspace_root"] == str(case.execution_a)
    history, _ = await case.dispatch("scheduler.history", **scope, task_id=task["id"])
    assert history["level"] == "info" and history["conversation_id"] == case.owner.id and history["data"]["runs"] == []
    for enabled in (False, True):
        toggled, _ = await case.dispatch("scheduler.toggle", **scope, task_id=task["id"], enabled=enabled)
        assert toggled["level"] == "info" and toggled["data"]["task"]["enabled"] is enabled
        assert toggled["conversation_id"] == case.owner.id

    standalone, _ = await case.dispatch("scheduler.add", **scope, **{**configuration, "kind": "cron", "conversation_id": "",
        "prompt": "Create a new chat on each run", "execution_workspace_root": str(case.root_c)})
    assert standalone["level"] == "info" and standalone["data"]["task"]["conversation_id"] == ""
    assert standalone["data"]["task"]["kind"] == "cron"
    assert standalone["data"]["task"]["execution_workspace_root"] == str(case.root_c)
    removed, _ = await case.dispatch("scheduler.remove", **scope, task_id=task["id"])
    assert removed["level"] == "info" and removed["data"]["task_id"] == task["id"]
    assert [row["id"] for row in case.scheduler.list_tasks(workspace_root=str(case.execution_a))] == [standalone["data"]["task"]["id"]]
    assert case.session.active_conversation_id == case.reader.id and case.session.session_lifecycle.workspace_root == case.root_b
    assert case.session.selected_model == "reader-model"
    assert case.repository.get_conversation(case.reader.id).to_dict() == original_reader


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["wrong-directory", "missing-owner", "implicit-owner"])
async def test_resource_opt_in_still_requires_the_real_owner_and_its_saved_directory(resource_case, invalid):
    case = resource_case
    scope = {"owner_conversation_id": case.owner.id, "workspace_root": str(case.execution_a)}
    if invalid == "wrong-directory":
        scope["workspace_root"] = str(case.root_b)
        expected = "Command workspace does not match the conversation workspace"
    elif invalid == "missing-owner":
        scope["owner_conversation_id"] = "conv_resource_missing"
        expected = "Conversation 'conv_resource_missing' was not found"
    else:
        scope = {"conversation_id": case.owner.id, "workspace_root": str(case.execution_a)}
        expected = "Command conversation owner is stale; switch back to the originating conversation and retry"
    receipt, messages = await case.dispatch("scheduler.list", **scope)
    assert receipt["level"] == "error" and receipt["message"] == expected
    assert not any(message["type"] == "scheduler.list" for message in messages)
    assert case.session.active_conversation_id == case.reader.id and case.session.session_lifecycle.workspace_root == case.root_b


@pytest.mark.asyncio
async def test_cold_recovery_dispatches_the_explicit_resource_owner_while_selected_actions_wait(resource_case):
    case = resource_case
    task = case.scheduler.add_task("Before crash", "Keep its target", "0 9 * * *", workspace_root=str(case.execution_a),
        conversation_id=case.target.id)
    saved = UserCommand(type="scheduler.update", data={"client_command_id": "saved-resource-edit",
        "owner_conversation_id": case.owner.id, "workspace_root": str(case.execution_a), "task_id": task.id,
        "name": "Recovered autosave", "prompt": "Only edit A's task", "schedule": "RRULE:FREQ=MINUTELY;INTERVAL=30",
        "permission_mode": "auto", "timezone": "UTC", "isolation": "workspace", "kind": "heartbeat", "conversation_id": case.target.id})
    selected = UserCommand(type="subagent.cancel", data={"client_command_id": "saved-selected-action",
        "owner_conversation_id": case.owner.id, "workspace_root": str(case.execution_a), "subagent_id": "child"})
    original_queue = case.session.run_manager.durable_client_commands
    assert original_queue.persist_client_command(saved) and original_queue.persist_client_command(selected)
    case.session.run_manager.close_durable_queue()

    cold, socket = case.new_session()
    dispatcher = cold.command_dispatcher
    await dispatcher._replay_pending_client_commands(cold.connection_generation)
    await drain_commands(cold)
    receipt = next(message for message in socket.messages
        if message["type"] == "command.result" and message.get("client_command_id") == "saved-resource-edit")
    assert receipt["level"] == "info" and receipt["conversation_id"] == case.owner.id
    assert receipt["data"]["task"]["conversation_id"] == case.target.id
    assert case.scheduler.list_tasks(workspace_root=str(case.execution_a))[0]["name"] == "Recovered autosave"
    assert cold.active_conversation_id == case.reader.id and cold.session_lifecycle.workspace_root == case.root_b
    assert [command.data["client_command_id"] for command in cold.run_manager.durable_client_commands.pending_client_commands()] == ["saved-selected-action"]
    assert dispatcher._client_command_seen(saved) and not dispatcher._client_command_seen(selected)


@pytest.mark.asyncio
async def test_global_scheduler_list_reads_persisted_orphan_roots_without_a_chat_projection(resource_case, tmp_path, monkeypatch):
    case = resource_case
    orphan_root = tmp_path / "scheduler-only-project"
    orphan_root.mkdir()
    tasks = [case.scheduler.add_task("Owned task", "Inspect A", "0 9 * * *", workspace_root=str(case.execution_a)),
        case.scheduler.add_task("Orphan cron", "No conversation owns this root", "0 10 * * *", workspace_root=str(orphan_root)),
        case.scheduler.add_task("Legacy unbound task", "Keep it visible", "0 11 * * *", workspace_root="")]
    restored = TaskScheduler()
    monkeypatch.setattr(handlers, "_get_scheduler", lambda _session: restored)

    receipt, messages = await case.dispatch("scheduler.list", scheduler_scope="global")

    assert receipt["level"] == "success"
    assert {task["id"] for task in receipt["data"]["tasks"]} == {task.id for task in tasks}
    assert receipt["data"]["scheduler_scope"] == "global"
    assert not any(message["type"] == "scheduler.list" for message in messages)
    assert case.session.active_conversation_id == case.reader.id
    assert case.session.session_lifecycle.workspace_root == case.root_b
    assert case.repository.get_conversation(case.reader.id).transcript[0]["content"] == "Retain B's input"


@pytest.mark.asyncio
async def test_global_scheduler_mutation_uses_the_captured_task_root_without_borrowing_a_chat(resource_case, tmp_path):
    case = resource_case
    orphan_root = tmp_path / "orphan-task-storage"
    orphan_root.mkdir()
    original = case.scheduler.add_task("Orphan task", "Original prompt", "0 9 * * *", workspace_root=str(orphan_root))
    unrelated = case.scheduler.add_task("Reader task", "Leave this alone", "0 10 * * *", workspace_root=str(case.root_b))
    configuration = {"task_id": original.id, "scheduler_scope": "global", "task_workspace_root": str(orphan_root),
        "name": "Edited orphan", "prompt": "Saved through its actual storage root", "schedule": "0 11 * * *",
        "kind": "cron", "permission_mode": "confirm", "timezone": "UTC", "isolation": "workspace"}

    receipt, messages = await case.dispatch("scheduler.update", **configuration)

    assert receipt["level"] == "info"
    assert receipt["data"]["task"]["workspace_root"] == str(orphan_root)
    assert receipt["data"]["task"]["prompt"] == configuration["prompt"]
    assert not any(message["type"] == "scheduler.list" for message in messages)
    assert case.scheduler.list_tasks(workspace_root=str(case.root_b))[0]["id"] == unrelated.id
    assert case.session.active_conversation_id == case.reader.id and case.session.session_lifecycle.workspace_root == case.root_b

    rejected, _ = await case.dispatch("scheduler.update", **{**configuration, "task_workspace_root": str(case.root_b), "prompt": "Wrong root"})
    assert rejected["level"] == "error" and "not found" in rejected["message"]
    assert case.scheduler.list_tasks(workspace_root=str(orphan_root))[0]["prompt"] == configuration["prompt"]


@pytest.mark.asyncio
async def test_global_scheduler_toggle_and_history_keep_the_task_and_run_resource_root(resource_case, tmp_path):
    case = resource_case
    orphan_root = tmp_path / "orphan-history"
    orphan_root.mkdir()
    task = case.scheduler.add_task("Orphan history", "Keep real errors", "0 9 * * *", workspace_root=str(orphan_root))
    run = ScheduledTaskRun(id="orphan-real-failure", task_id=task.id, status="failed", workspace_root=str(orphan_root),
        result_summary="Original scheduled command failed", error="Real scheduler failure")
    other = ScheduledTaskRun(id="other-root-run", task_id=task.id, status="completed", workspace_root=str(case.root_b))
    case.scheduler._runs.update({run.id: run, other.id: other})
    scope = {"scheduler_scope": "global", "task_workspace_root": str(orphan_root), "task_id": task.id}

    history, messages = await case.dispatch("scheduler.history", offset=0, limit=50, **scope)
    assert [row["id"] for row in history["data"]["runs"]] == [run.id]
    assert history["data"]["runs"][0]["error"] == "Real scheduler failure"
    assert not any(message["type"] == "scheduler.list" for message in messages)

    receipt, messages = await case.dispatch("scheduler.toggle", enabled=False, **scope)
    assert receipt["data"]["task"]["enabled"] is False
    assert receipt["data"]["task"]["workspace_root"] == str(orphan_root)
    assert not any(message["type"] == "scheduler.list" for message in messages)
    assert case.session.active_conversation_id == case.reader.id and case.session.session_lifecycle.workspace_root == case.root_b
