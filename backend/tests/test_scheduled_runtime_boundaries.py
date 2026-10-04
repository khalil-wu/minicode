from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.message import AgentEvent
from backend.agent.query_engine import QueryEngine
from backend.agent.runtime import AgentRuntime
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.scheduled_task_runner import run_scheduled_task
from backend.tasks.scheduler import ScheduledTask, ScheduledTaskRun, TaskScheduler
from backend.tools.base import PermissionLevel
from backend.tools.registry import ToolRegistry


class _Model(LLMAdapter):
    async def stream_chat(self, messages, tools=None, metadata=None):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "done"


@pytest.fixture
def scheduled_case(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = ConversationRepository(base_dir=tmp_path / "conversations")
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.services.scheduled_task_runner.ConversationRepository", lambda: repository)
    monkeypatch.setattr("backend.services.scheduled_task_runner.main_worktree_root", lambda path: Path(path).resolve())
    monkeypatch.setattr("backend.services.scheduled_task_runner.git_branch_for", lambda path: "audit")
    monkeypatch.setattr("backend.services.chat_api_service.default_runtime", lambda: runtime)
    monkeypatch.setattr("backend.services.scheduled_task_runner.default_runtime", lambda: runtime)
    monkeypatch.setattr("backend.services.chat_api_service.load_config", lambda **kwargs: AppConfig(llm=LLMSettings(api_key="")))
    monkeypatch.setattr("backend.agent.context.build_git_status_context_async", AsyncMock(return_value=""))
    monkeypatch.setattr("backend.tasks.scheduler.SCHEDULE_FILE", tmp_path / "state" / "scheduled_tasks.json")
    checker = PermissionChecker(PermissionSettings(), workspace_root=workspace)
    bootstrap = SimpleNamespace(
        mcp_manager=None, create_tool_registry=lambda *args, **kwargs: ToolRegistry(),
        create_permission_checker=lambda **kwargs: checker, create_llm=lambda **kwargs: _Model(),
    )
    yield SimpleNamespace(workspace=workspace, repository=repository, runtime=runtime, bootstrap=bootstrap)
    runtime.close()


@pytest.mark.asyncio
async def test_scheduled_journal_initialization_failure_releases_query_claim(scheduled_case, monkeypatch):
    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    task = ScheduledTask(workspace_root=str(case.workspace), conversation_id=conversation.id, isolation="workspace", prompt="Audit")
    run = ScheduledTaskRun(task_id=task.id, workspace_root=str(case.workspace))

    def unavailable_runtime():
        raise OSError("Runtime storage unavailable")

    monkeypatch.setattr("backend.services.scheduled_task_runner.default_runtime", unavailable_runtime)
    with pytest.raises(OSError, match="Runtime storage unavailable"):
        await run_scheduled_task(task, run, bootstrap=case.bootstrap)
    assert conversation_query_guards().active_claim(conversation.id) is None
    assert case.repository.get_conversation(conversation.id).transcript == []

    async def submit(self, submission):
        submission.state.reply = "Retried result"
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr("backend.services.scheduled_task_runner.default_runtime", lambda: case.runtime)
    monkeypatch.setattr(QueryEngine, "submit", submit)
    result = await run_scheduled_task(task, run, bootstrap=case.bootstrap)
    assert result["status"] == "completed"
    assert conversation_query_guards().active_claim(conversation.id) is None


@pytest.mark.asyncio
async def test_heartbeat_carries_and_refreshes_conversation_restrictions(scheduled_case, monkeypatch):
    case = scheduled_case
    conversation = case.repository.create_conversation(
        workspace_root=str(case.workspace), permission_mode="confirm",
        permission_deny_rules=["read_file"], permission_overrides={"web_fetch": "deny"},
    )
    observed = []

    async def submit(self, submission):
        permission = submission.runtime.permission_context
        provider = submission.runtime.run_context.permission_context_provider
        assert permission.tool_deny_rules == ["read_file"]
        assert permission.session_overrides["web_fetch"] is PermissionLevel.ALWAYS_DENY
        assert permission.conversation_id == conversation.id
        case.repository.update_permission_rules(conversation.id, deny_rules=["grep_files"], overrides={"read_file": "deny"})
        case.repository.update_permission_mode(conversation.id, "plan")
        refreshed = provider()
        assert refreshed.mode == "plan" and refreshed.pre_plan_mode == "confirm"
        assert refreshed.tool_deny_rules == ["grep_files"]
        assert refreshed.session_overrides == {"read_file": PermissionLevel.ALWAYS_DENY}
        case.repository.update_permission_mode(conversation.id, "bypass")
        assert provider().mode == "auto"
        observed.append(refreshed)
        submission.state.reply = "done"
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    task = ScheduledTask(
        workspace_root=str(case.workspace), conversation_id=conversation.id,
        isolation="workspace", permission_mode="auto", prompt="Read",
    )
    run = ScheduledTaskRun(task_id=task.id, workspace_root=str(case.workspace))
    result = await run_scheduled_task(task, run, bootstrap=case.bootstrap)
    assert result["status"] == "completed" and len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["confirm", "auto"])
async def test_independent_cron_uses_its_configured_mode(scheduled_case, monkeypatch, mode):
    case = scheduled_case
    observed = []

    async def submit(self, submission):
        permission = submission.runtime.permission_context
        assert permission.mode == mode
        assert permission.tool_deny_rules == [] and permission.session_overrides == {}
        observed.append(permission.conversation_id)
        submission.state.reply = "done"
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    task = ScheduledTask(workspace_root=str(case.workspace), isolation="workspace", permission_mode=mode, prompt="Read")
    run = ScheduledTaskRun(task_id=task.id, workspace_root=str(case.workspace))
    result = await run_scheduled_task(task, run, bootstrap=case.bootstrap)
    assert result["conversation_id"] == run.conversation_id == observed[0]
    assert case.repository.get_conversation(run.conversation_id).permission_mode == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_method", ["user", "shutdown"])
async def test_scheduled_worker_owns_borrowers_until_cleanup_finishes(scheduled_case, monkeypatch, cancel_method):
    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    admitted = asyncio.Event()
    release = asyncio.Event()
    borrowers = []

    async def controlled_query(**kwargs):
        borrower = asyncio.create_task(release.wait())
        kwargs["run_context"].retain_lifecycle_task(borrower, label="controlled provider borrower")
        borrowers.append(borrower)
        admitted.set()
        await asyncio.Event().wait()

    async def callback(task, run):
        return await run_scheduled_task(
            task, run, bootstrap=case.bootstrap, bind_conversation=scheduler.bind_run_conversation,
        )

    monkeypatch.setattr("backend.services.scheduled_task_runner.run_owned_rest_chat", controlled_query)
    monkeypatch.setattr("backend.async_cleanup.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.01)
    scheduler = TaskScheduler(on_fire=callback)
    task = scheduler.add_task(
        name="Cleanup", prompt="Audit", schedule="0 * * * *", workspace_root=str(case.workspace),
        conversation_id=conversation.id, isolation="workspace",
    )
    run = scheduler.run_now(task.id)
    await asyncio.wait_for(admitted.wait(), 3)
    worker = scheduler._run_tasks[run.id]
    try:
        if cancel_method == "user":
            assert scheduler.cancel_run(run.id)
            await asyncio.sleep(0)
        else:
            await scheduler.stop()
        assert run.status == "cancelled" and run.cleanup_pending
        assert not worker.done() and not borrowers[0].done()
        assert scheduler.run_now(task.id) is run
        assert conversation_query_guards().active_claim(conversation.id) is not None
    finally:
        release.set()
        await asyncio.gather(*borrowers)
        with pytest.raises(asyncio.CancelledError):
            await worker
        await asyncio.sleep(0)
    assert not run.cleanup_pending and run.cleanup_completed_at is not None
    assert run.id not in scheduler._run_tasks
    assert conversation_query_guards().active_claim(conversation.id) is None


@pytest.mark.asyncio
async def test_cron_capability_tracks_the_actual_host_scheduler(scheduled_case, monkeypatch):
    from backend.tasks import scheduler as scheduler_module
    from backend.tools.schedule_cron_tool import ScheduleCronTool, ScheduleCronListTool, ScheduleCronDeleteTool

    monkeypatch.setattr(scheduler_module, "_GLOBAL_SCHEDULER", None)
    registry = ToolRegistry()
    tools = [ScheduleCronTool(), ScheduleCronListTool(), ScheduleCronDeleteTool()]
    for tool in tools:
        registry.register(tool)
    checker = PermissionChecker(PermissionSettings())
    permission = PermissionContext(mode="bypass")
    context = ToolExecutionContext(permission=permission, workspace_root=scheduled_case.workspace)

    def visible():
        return {schema["function"]["name"] for schema in registry.get_schemas(
            permission_checker=checker, permission_context=permission,
        )}

    assert visible() == set()
    for tool, args in zip(tools, [
        {"name": "Audit", "prompt": "Check status", "cron": "0 * * * *"}, {}, {"job_id": "missing"},
    ]):
        result = await tool.execute(args, context)
        assert result.is_error and "no running scheduler" in result.content
    assert scheduler_module._GLOBAL_SCHEDULER is None

    scheduler = scheduler_module.get_global_scheduler()
    assert visible() == set()  # Constructing a host during startup is not execution readiness.

    async def on_fire(task, run):
        return {"status": "completed"}

    scheduler_module.get_global_scheduler(on_fire=on_fire)
    await scheduler.start()
    try:
        assert visible() == {tool.name for tool in tools}
        created = await tools[0].execute({"name": "Audit", "prompt": "Check status", "cron": "0 * * * *"}, context)
        assert not created.is_error
        task_id = scheduler.list_tasks(workspace_root=str(scheduled_case.workspace))[0]["id"]
        listed = await tools[1].execute({}, context)
        assert not listed.is_error and task_id in listed.content
        removed = await tools[2].execute({"job_id": task_id}, context)
        assert not removed.is_error
    finally:
        await scheduler.stop()
    assert visible() == set()


@pytest.mark.asyncio
async def test_scheduler_without_callback_does_not_expose_cron(scheduled_case, monkeypatch):
    from backend.tasks import scheduler as scheduler_module
    from backend.tools.schedule_cron_tool import ScheduleCronTool

    monkeypatch.setattr(scheduler_module, "_GLOBAL_SCHEDULER", None)
    scheduler = scheduler_module.get_global_scheduler()
    await scheduler.start()
    try:
        assert scheduler_module.get_running_scheduler() is None
        assert not ScheduleCronTool().is_capability_available()
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase, expected", [("final_answer", "Visible partial answer"), ("", "")])
async def test_cancelled_schedule_replays_one_canonical_projection_after_cleanup(scheduled_case, monkeypatch, phase, expected):
    from backend.services.conversation_projection_service import replay_pending_conversation_projections
    from backend.ws.agent_runner import _replay_pending_conversation_projections

    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    entered, release, terminal = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owners = []
    original_submit = QueryEngine.submit

    async def submit(self, submission):
        owners.append(submission.runtime.run_context)
        async with aclosing(original_submit(self, submission)) as stream:
            async for event in stream:
                if event.type == "done":
                    terminal.set()
                yield event

    class InterruptedModel(_Model):
        async def stream_chat(self, messages, tools=None, metadata=None):
            yield StreamEvent(type=StreamEventType.THINKING_CHUNK, content="Private reasoning")
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="<thinking>Hidden text</thinking>Visible partial answer", phase=phase)

            async def late_cleanup():
                await release.wait()
                owners[0].execution_journal.append_lifecycle("extension_state_committed", {
                    "message_id": f"assistant_schedule_{run.id}",
                    "context_snapshot": {"extension_state": {"late_cleanup": True}},
                })

            owners[0].retain_lifecycle_task(asyncio.create_task(late_cleanup()), label="late journal borrower")
            entered.set()
            await asyncio.Event().wait()

    case.bootstrap.create_llm = lambda **kwargs: InterruptedModel()
    monkeypatch.setattr(QueryEngine, "submit", submit)
    task = ScheduledTask(workspace_root=str(case.workspace), conversation_id=conversation.id, isolation="workspace", prompt="Audit")
    run = ScheduledTaskRun(task_id=task.id, workspace_root=str(case.workspace))
    worker = asyncio.create_task(run_scheduled_task(task, run, bootstrap=case.bootstrap))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        worker.cancel()
        await asyncio.wait_for(terminal.wait(), 3)
        assert not worker.done()
        assert [message["role"] for message in case.repository.get_conversation(conversation.id).transcript] == ["user"]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
    stored = case.repository.get_conversation(conversation.id)
    assistants = [message for message in stored.transcript if message["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["id"] == f"assistant_schedule_{run.id}"
    assert assistants[0]["terminal_status"] == "cancelled" and assistants[0]["content"] == expected
    history = stored.context_snapshot["history"]
    assert [message["content"] for message in history if message["role"] == "assistant"] == ([expected] if expected else [])
    assert "Private reasoning" not in str(history) and "Hidden text" not in str(history)
    assert stored.context_snapshot["extension_state"]["late_cleanup"] is True
    assert _replay_pending_conversation_projections is replay_pending_conversation_projections
    await replay_pending_conversation_projections(case.repository, owners[0].execution_journal, conversation_id=conversation.id)
    replayed = case.repository.get_conversation(conversation.id)
    assert len([message for message in replayed.transcript if message["role"] == "assistant"]) == 1
    assert replayed.context_snapshot == stored.context_snapshot


@pytest.mark.asyncio
async def test_schedule_recovers_staged_projection_before_admitting_retry(scheduled_case, monkeypatch):
    from backend.agent.execution_journal import execution_journal_owner

    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    admitted = case.repository.append_transcript_message(conversation.id, {"id": "prior-user", "role": "user", "content": "Prior work"})
    journal = case.runtime.execution_journal(execution_journal_owner("conversation", case.repository.store_instance_id(), conversation.id))
    journal.append_lifecycle("conversation_projection_pending", {
        "conversation_id": conversation.id,
        "assistant_message": {"id": "assistant_schedule_old", "role": "assistant", "content": "Recovered prior work"},
        "context_snapshot": {"history": [
            {"role": "user", "content": "Prior work"}, {"role": "assistant", "content": "Recovered prior work"},
        ]}, "expected_revision": admitted.revision,
    })

    async def submit(self, submission):
        history = submission.session.context_builder.export_snapshot()["history"]
        assert history[-1]["content"] == "Recovered prior work"
        submission.state.reply = "New result"
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    task = ScheduledTask(workspace_root=str(case.workspace), conversation_id=conversation.id, isolation="workspace", prompt="Continue")
    run = ScheduledTaskRun(task_id=task.id, workspace_root=str(case.workspace))
    await run_scheduled_task(task, run, bootstrap=case.bootstrap)
    assert journal.pending_conversation_projections() == []
    assert [message["content"] for message in case.repository.get_conversation(conversation.id).transcript] == [
        "Prior work", "Recovered prior work", "Continue", "New result",
    ]


@pytest.mark.asyncio
async def test_anonymous_rest_cancellation_keeps_journal_identity_without_creating_chat(scheduled_case):
    from backend.services.chat_api_service import run_rest_chat

    case = scheduled_case
    entered = asyncio.Event()

    class InterruptedModel(_Model):
        async def stream_chat(self, messages, tools=None, metadata=None):
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="REST partial", phase="final_answer")
            entered.set()
            await asyncio.Event().wait()

    case.bootstrap.create_llm = lambda **kwargs: InterruptedModel()
    worker = asyncio.create_task(run_rest_chat(
        message="Audit", max_iterations=2, bootstrap=case.bootstrap, query_engine=QueryEngine(), workspace_root=case.workspace,
    ))
    await asyncio.wait_for(entered.wait(), 3)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    projections = [projection for journal in case.runtime._execution_journals.values() for projection in journal.unprojected_terminal_projections()]
    assert len(projections) == 1
    message = projections[0]["assistant_message"]
    assert message["id"].startswith("assistant_rest_") and message["content"] == "REST partial"
    assert message["terminal_status"] == "cancelled"
    assert case.repository.list_conversations() == []
