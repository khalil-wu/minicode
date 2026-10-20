"""Source desktop gate and core submission through real scheduler/turn owners."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from backend.agent.context import ContextBuilder
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.conversation_query_guard import ConversationQueryGuardRegistry
from backend.agent.message import UserCommand
from backend.agent.query_engine import QueryEngine
from backend.agent.run_context import RunContext
from backend.agent.turn_input import MailboxDeliveryPhase, ScheduledInputCommand, TurnInput, TurnInputQueue
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.services import scheduled_task_runner as runner
from backend.services.scheduler_service import add_scheduled_task, update_scheduled_task
from backend.services.scheduler_service import run_scheduled_task_now, SchedulerServiceError
from backend.tasks.scheduler import ScheduledTask, ScheduledTaskRun, TaskScheduler
from backend.tests.test_agent_runner_done_fallback import _Session
from backend.tests.test_scheduled_runtime_boundaries import scheduled_case
from backend.tests.test_ws_cold_connection import _connection
from backend.tools.file_tools import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.manager import WebSocketManager
from backend.ws.payload_contracts import is_non_replayable_event_type
from backend.ws.stream_state import apply_stream_event


class BoundaryModel(LLMAdapter):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.messages = []

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        self.messages.append(messages)
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="actual-read", name="read_file", arguments={"file_path": "marker.txt"})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Actual shared turn completed.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)

    async def simple_chat(self, messages, *, max_tokens=None):
        return "No side request is needed."


class FinalItemModel(BoundaryModel):
    def __init__(self):
        super().__init__()
        self.finish_response = asyncio.Event()

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        self.messages.append(messages)
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            item = {"type": "message", "id": "candidate-item", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Candidate answer."}]}
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Candidate answer.", phase="final_answer", item_id="candidate-item")
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="", phase="final_answer", item_id="candidate-item",
                lifecycle="item_completed", provider_items=[item])
            await self.finish_response.wait()
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)
        else:
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Actual shared turn completed.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)


@asynccontextmanager
async def real_ws_owner(case, monkeypatch, *, model=None):
    events = []
    session = _Session(case.workspace, events)
    collect_event = session.send_event

    async def project_event(event):
        apply_stream_event(session._conversation_streams, str(event.data.get("conversation_id") or ""), event.type, event.data)
        await collect_event(event)

    session.send_event = project_event
    session.query_engine = QueryEngine()
    model = model or BoundaryModel()
    config = AppConfig(llm=LLMSettings(api_key="controlled", model="gpt-test"))
    session.config = config
    session.llm = model
    from backend.ws.command_handlers import SessionCommandHandlersMixin

    session.permission_context_for_conversation = lambda conversation, source="agent.run": session.permission_checker.build_context(
        mode="bypass", sandbox_mode="danger-full-access", source=source)
    session.permission_context = session.permission_context_for_conversation(session.active_conversation)
    session._provider_override_active = False
    session._resolve_models_source = lambda provider, settings=None: "configured"
    session.refresh_llm_selection = SessionCommandHandlersMixin.refresh_llm_selection.__get__(session)
    session.conversation_repo.update_memory_mode(session.active_conversation_id, "disabled")
    (case.workspace / "marker.txt").write_text("ACTUAL_READ", encoding="utf-8")
    session.tool_registry.register(ReadFileTool(session.artifact_store))
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: case.runtime)
    monkeypatch.setattr("backend.config.load_config", lambda **kwargs: config)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **kwargs: config)
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *args, **kwargs: model)
    monkeypatch.setattr("backend.memory.generation.schedule_memory_startup", lambda **kwargs: None)
    monkeypatch.setattr(runner, "ConversationRepository", lambda: session.conversation_repo)
    context = RunContext(turn_input_queue=session.run_manager.turn_input_queue(session.active_conversation_id))
    cancel = asyncio.Event()
    task = None

    async def launch():
        nonlocal task
        task = asyncio.create_task(session._run_agent("Foreground instruction.",
            conversation_id=session.active_conversation_id,
            metadata={"_run_task_id": "actual-foreground", "assistant_message_id": "actual-answer", "user_message_id": "human-input"},
            run_context=context, cancel_event=cancel))
        session.run_manager.register(conversation_id=session.active_conversation_id, task=task,
            task_id="actual-foreground", cancel_event=cancel, active_conversation_id=session.active_conversation_id,
            run_context=context)
        async with asyncio.timeout(8):
            await model.started.wait()
        return task

    try:
        yield SimpleNamespace(session=session, model=model, context=context, launch=launch, events=events)
    finally:
        model.release.set()
        if task is not None:
            await task
            session.run_manager.cleanup(conversation_id=session.active_conversation_id,
                task=task, task_id="actual-foreground", cancel_event=cancel)
        await session.run_manager.shutdown_notification_wakes()
        session.run_manager.close_durable_queue()
        await session.artifact_store.flush()


def due_task(scheduler, case, conversation_id, name="Heartbeat"):
    task = scheduler.add_task(name, "  Preserve scheduled instructions.\n", "* * * * *",
        workspace_root=str(case.workspace), conversation_id=conversation_id,
        kind="heartbeat", isolation="workspace")
    task.next_run_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    scheduler._save()
    return task


@pytest.mark.asyncio
async def test_running_real_turn_defers_before_claim_and_manual_reports_busy(scheduled_case, monkeypatch):
    case = scheduled_case
    async with real_ws_owner(case, monkeypatch) as owner:
        await owner.launch()
        async def unexpected_runner(*_args):
            raise AssertionError("A busy heartbeat reached the execution callback")

        scheduler = TaskScheduler(on_fire=unexpected_runner)
        task = due_task(scheduler, case, owner.session.active_conversation_id)
        now = datetime.now(UTC)
        scheduler._tick(now)
        assert scheduler._runs == {} and scheduler._run_tasks == {}
        assert task.last_run_at is None and task.last_run_id is None and task.last_run_status is None
        assert now < datetime.fromisoformat(task.next_run_at) <= now + timedelta(seconds=60)
        cold = TaskScheduler()
        assert cold._tasks[task.id].next_run_at == task.next_run_at
        assert cold._tasks[task.id].last_run_at is None
        with pytest.raises(ValueError, match="Heartbeat thread is busy right now"):
            scheduler.run_now(task.id)
        assert scheduler._runs == {} and owner.model.calls == 1


@pytest.mark.asyncio
async def test_post_gate_race_steers_real_ws_owner_and_uses_its_canonical_terminal(scheduled_case, monkeypatch):
    case = scheduled_case
    allow_submission = asyncio.Event()
    async with real_ws_owner(case, monkeypatch) as owner:
        async def execute(task, run):
            await allow_submission.wait()
            return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

        scheduler = TaskScheduler(on_fire=execute)
        task = due_task(scheduler, case, owner.session.active_conversation_id)
        scheduler._tick(datetime.now(UTC))
        run = scheduler._runs[task.last_run_id]
        foreground = await owner.launch()
        original_claim = conversation_query_guards().active_claim(owner.session.active_conversation_id)
        allow_submission.set()
        async with asyncio.timeout(5):
            while owner.context.turn_input_queue.pending_count() != 1:
                await asyncio.sleep(0)
        assert conversation_query_guards().active_claim(owner.session.active_conversation_id) == original_claim
        assert owner.model.calls == 1
        queued = owner.context.turn_input_queue.snapshot()[0]
        assert queued.input_origin == "heartbeat"
        assert isinstance(queued.original_command, ScheduledInputCommand)
        owner.model.release.set()
        await foreground
        await asyncio.gather(*list(scheduler._run_tasks.values()))
        assert owner.model.calls == 2
        assert run.status == "completed", run.error
        terminal = owner.context.turn_terminal_receipt.result()
        assert run.actual_turn_id == terminal["turn_id"]
        assert run.message_id == "actual-answer"
        assert task.last_run_at is not None
        stored = owner.session.conversation_repo.get_conversation(owner.session.active_conversation_id)
        users = [message for message in stored.transcript if message["role"] == "user"]
        assert [message["content"] for message in users] == ["Foreground instruction.", task.prompt]
        assert len({message["id"] for message in stored.transcript if message["role"] == "assistant"}) == 1
        cold = ContextBuilder()
        cold.load_snapshot(stored.context_snapshot)
        assert [message.input_origin for message in cold._history if message.is_user_input] == ["user", "heartbeat"]
        assert cold._history[-1].content == "Actual shared turn completed."
        assert TaskScheduler()._runs[run.id].actual_turn_id == terminal["turn_id"]


@pytest.mark.asyncio
async def test_post_gate_race_steers_actual_detached_owner_without_second_query(scheduled_case):
    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    (case.workspace / "marker.txt").write_text("DETACHED_READ", encoding="utf-8")
    model = BoundaryModel()
    registry = ToolRegistry()
    registry.register(ReadFileTool(ArtifactStore(storage_dir=case.workspace.parent / "detached-artifacts")))
    case.bootstrap.create_tool_registry = lambda *args, **kwargs: registry
    case.bootstrap.create_llm = lambda **kwargs: model
    submit = asyncio.Event()

    async def execute(task, run):
        await submit.wait()
        return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

    scheduler = TaskScheduler(on_fire=execute)
    task = due_task(scheduler, case, conversation.id)
    scheduler._tick(datetime.now(UTC))
    run = scheduler._runs[task.last_run_id]
    foreground_definition = ScheduledTask(id="actual-parent", prompt="Original detached instruction.",
        workspace_root=str(case.workspace), conversation_id=conversation.id, isolation="workspace", kind="cron")
    foreground_run = ScheduledTaskRun(id="actual-parent-run", task_id=foreground_definition.id, conversation_id=conversation.id)
    foreground = asyncio.create_task(runner.run_scheduled_task(foreground_definition, foreground_run, bootstrap=case.bootstrap))
    try:
        async with asyncio.timeout(8):
            await model.started.wait()
        original_owner = conversation_query_guards().active_owner(conversation.id)
        submit.set()
        async with asyncio.timeout(5):
            while original_owner.turn_input_queue.pending_count() != 1:
                await asyncio.sleep(0)
        assert model.calls == 1
        model.release.set()
        result = await foreground
        await asyncio.gather(*list(scheduler._run_tasks.values()))
        assert result["status"] == "completed" and run.status == "completed", (result, run.error)
        assert model.calls == 2 and run.actual_turn_id == foreground_run.id
        stored = case.repository.get_conversation(conversation.id)
        assert [message["content"] for message in stored.transcript if message["role"] == "user"] == [foreground_definition.prompt, task.prompt]
        assert len([message for message in stored.transcript if message["role"] == "assistant"]) == 1
        cold = ContextBuilder()
        cold.load_snapshot(stored.context_snapshot)
        assert [message.input_origin for message in cold._history if message.is_user_input] == ["user", "heartbeat"]
    finally:
        model.release.set()
        submit.set()
        await foreground
        await asyncio.gather(*list(scheduler._run_tasks.values()))


@pytest.mark.asyncio
async def test_host_input_cold_queue_retains_origin_and_public_command_cannot_set_it(scheduled_case, monkeypatch):
    case = scheduled_case
    async with real_ws_owner(case, monkeypatch) as owner:
        command = ScheduledInputCommand(type="user_message", data={"content": "<heartbeat>Private input</heartbeat>",
            "user_message_id": "durable-heartbeat", "assistant_message_id": "unused", "conversation_id": owner.session.active_conversation_id},
            turn_trigger="automation_heartbeat_scheduled", public_content="Public prompt.", task_id="schedule", scheduled_run_id="run")
        queue = owner.session.run_manager.turn_input_queue(owner.session.active_conversation_id)
        queue.begin_turn("actual-old-turn")
        owner.session._conversation_streams[owner.session.active_conversation_id] = {
            "turn_id": "actual-old-turn", "message_id": "actual-old-answer"}
        owner.session.run_manager.run_tasks[owner.session.active_conversation_id] = asyncio.create_task(asyncio.Event().wait())
        assert owner.session.run_manager.enqueue_scheduled_input(owner.session.active_conversation_id, command, expected_turn_id="actual-old-turn") is not None
        path = owner.session.run_manager.durable_queue.path
        owner.session.run_manager.close_durable_queue()
        cold_store = DurableUserMessageQueue(session_id="shared", root_dir=path.parent, shared_inputs=True)
        try:
            queues, _inflight = cold_store.load()
            recovered = queues[owner.session.active_conversation_id][0]
            assert isinstance(recovered, ScheduledInputCommand)
            assert TurnInput.from_command(recovered).input_origin == "heartbeat"
            assert recovered.public_content == "Public prompt."
            public = UserCommand.from_ws_message({"type": "user_message", "content": command.data["content"],
                "host_scheduled_input": {"turn_trigger": "automation_heartbeat_scheduled"}, "input_origin": "heartbeat"})
            assert TurnInput.from_command(public).input_origin == "user"
        finally:
            cold_store.close()


@pytest.mark.asyncio
async def test_target_serial_gate_tick_limit_and_real_manual_trigger(scheduled_case):
    case = scheduled_case
    blocker = asyncio.Event()
    started = []

    async def execute(task, run):
        started.append(run.id)
        await blocker.wait()
        return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

    scheduler = TaskScheduler(on_fire=execute)
    conversations = [case.repository.create_conversation(workspace_root=str(case.workspace)) for _ in range(4)]
    tasks = [due_task(scheduler, case, conversation.id, str(index)) for index, conversation in enumerate(conversations)]
    twin = due_task(scheduler, case, conversations[0].id, "same-target")
    try:
        now = datetime.now(UTC)
        scheduler._tick(now)
        assert len(scheduler._runs) == 3 and tasks[3].last_run_at is None
        scheduler._tick(now)
        assert twin.last_run_at is None and len(scheduler._runs) == 4
        assert now < datetime.fromisoformat(twin.next_run_at) <= now + timedelta(seconds=60)
        blocker.set()
        await asyncio.gather(*list(scheduler._run_tasks.values()))
        manual = scheduler.run_now(tasks[0].id)
        assert manual.turn_trigger == "automation_heartbeat_run_now"
        await scheduler._run_tasks[manual.id]
        assert TaskScheduler()._runs[manual.id].turn_trigger == "automation_heartbeat_run_now"
        assert all(run.status == "completed" for run in scheduler._runs.values())
    finally:
        blocker.set()
        await asyncio.gather(*list(scheduler._run_tasks.values()))


@pytest.mark.asyncio
async def test_stale_admitted_callback_cannot_start_after_host_drain_and_reopen(scheduled_case):
    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    release = asyncio.Event()
    provider_calls = []
    case.bootstrap.create_llm = lambda **kwargs: provider_calls.append(kwargs)

    async def execute(task, run):
        await release.wait()
        return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

    scheduler = TaskScheduler(on_fire=execute)
    task = due_task(scheduler, case, conversation.id)
    scheduler._tick(datetime.now(UTC))
    run = scheduler._runs[task.last_run_id]
    guards = conversation_query_guards()
    guards.begin_drain()
    assert guards.try_start("new-after-close", owner_id="test") is None
    guards.open_admission()
    release.set()
    await scheduler._run_tasks[run.id]
    assert run.status == "failed" and run.error == "server_draining"
    assert provider_calls == [] and case.repository.get_conversation(conversation.id).transcript == []


@pytest.mark.asyncio
async def test_renderer_observer_bypasses_restore_lock_and_never_becomes_durable(tmp_path):
    manager = WebSocketManager()
    session, generation = await manager.connect(**_connection(tmp_path, "heartbeat-observer"))
    conversation = session.conversation_repo.create_conversation(workspace_root=str(tmp_path))
    session.active_conversation_id = conversation.id
    frames = [json.dumps({"type": "scheduler.heartbeat.renderer_state", "conversation_id": conversation.id,
        "is_eligible": False, "reason": "resuming"})]

    async def receive():
        if frames:
            return frames.pop()
        raise WebSocketDisconnect()

    session.ws.receive_text = receive
    try:
        async with session.conversation_lifecycle_lock():
            with pytest.raises(WebSocketDisconnect):
                async with asyncio.timeout(2):
                    await session.command_dispatcher.run(generation)
        assert manager.heartbeat_renderer_block_reason(conversation.id) == "resuming"
        assert session.run_manager.durable_client_commands.pending_client_commands() == []
        assert not any(frame.get("type") in {"client.command.ack", "command.result"} for frame in session.ws.sent)
        assert not manager.record_heartbeat_renderer_state(session, generation - 1, {
            "conversation_id": conversation.id, "is_eligible": True, "reason": None})
        state = manager._heartbeat_renderer_states[conversation.id]
        manager._heartbeat_renderer_states[conversation.id] = (*state[:-1], state[-1] - 121)
        assert manager.heartbeat_renderer_block_reason(conversation.id) is None
        manager.request_heartbeat_renderer_state(conversation.id, workspace_root=str(tmp_path))
        await asyncio.gather(*tuple(session.cleanup_tasks))
        await session.event_outbox.drain_delivery()
        assert session.ws.sent[-1]["type"] == "scheduler.heartbeat.renderer_state.request"
        assert is_non_replayable_event_type(session.ws.sent[-1]["type"])
        reattached, new_generation = await manager.connect(**_connection(tmp_path, "heartbeat-observer"))
        assert reattached is session and new_generation != generation
        assert (session.session_id, generation) not in manager._heartbeat_renderer_requests
        assert manager.heartbeat_renderer_block_reason(conversation.id) is None
        assert not manager.record_heartbeat_renderer_state(session, generation, {
            "conversation_id": conversation.id, "is_eligible": False, "reason": "resuming"})
    finally:
        await manager.shutdown(reason="heartbeat-observer-test")


def test_task_create_update_and_reload_preserve_exact_prompt(scheduled_case):
    case = scheduled_case
    scheduler = TaskScheduler()
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    data = {"name": "Exact prompt", "prompt": "  A\n\n", "schedule": "* * * * *", "kind": "heartbeat",
        "conversation_id": conversation.id, "isolation": "workspace"}
    add_scheduled_task(scheduler, data, workspace_root=str(case.workspace))
    task = next(iter(scheduler._tasks.values()))
    assert task.prompt == "  A\n\n"
    data.update(task_id=task.id, prompt="\n A \n")
    update_scheduled_task(scheduler, data, workspace_root=str(case.workspace))
    assert TaskScheduler()._tasks[task.id].prompt == "\n A \n"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_before_start", [False, True])
async def test_manual_service_waits_for_real_start_ack_without_cancelling_owner(scheduled_case, monkeypatch, timeout_before_start):
    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))
    case.repository.update_agent_mode(conversation.id, "plan")
    (case.workspace / "marker.txt").write_text("MANUAL_READ", encoding="utf-8")
    model = BoundaryModel()
    registry = ToolRegistry()
    registry.register(ReadFileTool(ArtifactStore(storage_dir=case.workspace.parent / "manual-artifacts")))
    case.bootstrap.create_tool_registry = lambda *args, **kwargs: registry
    case.bootstrap.create_llm = lambda **kwargs: model
    release_submission = asyncio.Event()

    async def execute(task, run):
        await release_submission.wait()
        return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

    scheduler = TaskScheduler(on_fire=execute)
    task = due_task(scheduler, case, conversation.id)
    if timeout_before_start:
        monkeypatch.setattr("backend.services.scheduler_service.HEARTBEAT_START_TIMEOUT_SECONDS", 0.01)
    request = asyncio.create_task(run_scheduled_task_now(scheduler, {"task_id": task.id}, workspace_root=str(case.workspace)))
    try:
        async with asyncio.timeout(3):
            while task.last_run_id is None:
                await asyncio.sleep(0)
        run = scheduler._runs[task.last_run_id]
        assert not request.done() and not run.start_receipt.done()
        if timeout_before_start:
            with pytest.raises(SchedulerServiceError, match="Timed out waiting for heartbeat automation to start"):
                await request
            assert not run.start_receipt.cancelled() and not scheduler._run_tasks[run.id].done()
        release_submission.set()
        async with asyncio.timeout(8):
            await model.started.wait()
        if not timeout_before_start:
            await request
        assert run.start_receipt.result() == {"status": "started", "turn_id": run.id}
        assert run.status == "running" and model.calls == 1
        assert any(message.role == "developer" and "# Plan Mode (Conversational)" in message.content for message in model.messages[0])
    finally:
        release_submission.set()
        model.release.set()
        await asyncio.gather(*list(scheduler._run_tasks.values()))
    assert run.status == "completed" and run.turn_trigger == "automation_heartbeat_run_now"


@pytest.mark.asyncio
async def test_scheduled_wait_timer_preserves_actual_turn_and_borrower_then_rechecks_due(scheduled_case, monkeypatch):
    case = scheduled_case
    monkeypatch.setattr(runner, "HEARTBEAT_TERMINAL_WAIT_SECONDS", 0.01)
    submit = asyncio.Event()
    borrower_release = asyncio.Event()
    async with real_ws_owner(case, monkeypatch) as owner:
        async def execute(task, run):
            await submit.wait()
            return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

        scheduler = TaskScheduler(on_fire=execute)
        task = due_task(scheduler, case, owner.session.active_conversation_id)
        scheduler._tick(datetime.now(UTC))
        run = scheduler._runs[task.last_run_id]
        foreground = await owner.launch()

        async def callback():
            await borrower_release.wait()
            (case.workspace / "borrower-completed.txt").write_text("settled", encoding="utf-8")

        borrower = asyncio.create_task(callback())
        owner.context.retain_lifecycle_task(borrower, label="actual pending callback")
        claim = conversation_query_guards().active_claim(owner.session.active_conversation_id)
        submit.set()
        try:
            async with asyncio.timeout(3):
                while not run.scheduler_wait_elapsed:
                    await asyncio.sleep(0)
            assert run.status == "running" and run.finished_at is None
            assert not foreground.cancelled() and owner.model.calls == 1
            assert conversation_query_guards().active_claim(owner.session.active_conversation_id) == claim
            task.next_run_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            now = datetime.now(UTC) + timedelta(seconds=61)
            previous_fire = task.last_run_at
            scheduler._tick(now)
            assert len(scheduler._runs) == 1 and task.last_run_at == previous_fire
            assert now < datetime.fromisoformat(task.next_run_at) <= now + timedelta(seconds=60)
            owner.model.release.set()
            async with asyncio.timeout(5):
                await asyncio.shield(owner.context.turn_terminal_receipt)
            assert owner.context.turn_terminal_receipt.result()["status"] == "completed"
            assert run.status == "running" and not borrower.done()
            borrower_release.set()
            await borrower
            await foreground
            await asyncio.gather(*list(scheduler._run_tasks.values()))
            assert run.status == "completed" and run.error == ""
            assert (case.workspace / "borrower-completed.txt").read_text(encoding="utf-8") == "settled"
            assert "scheduler_wait_elapsed" not in run.to_dict() and "start_receipt" not in run.to_dict()
        finally:
            borrower_release.set()
            owner.model.release.set()
            await borrower
            await foreground
            await asyncio.gather(*list(scheduler._run_tasks.values()))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,schema,closed,reason", [
    ("compact", None, False, "active_turn_not_steerable"),
    ("review", None, False, "active_turn_not_steerable"),
    ("regular", {"type": "integer"}, False, "active_turn_output_schema_mismatch"),
    ("regular", None, True, "no_active_turn"),
])
async def test_atomic_core_refuses_nonsteerable_schema_or_final_boundary(kind, schema, closed, reason):
    guards = ConversationQueryGuardRegistry()
    queue = TurnInputQueue()
    queue.begin_turn("actual-original-turn")
    if closed:
        queue.seal_and_drain_commands()
    current = RunContext(turn_input_queue=queue, turn_kind=kind)
    current.prepare_turn_owner()
    current.accept_scheduled_input = queue.enqueue_command
    claim = guards.try_start("conv_core_contract", owner_id="actual-owner", owner=current)
    command = ScheduledInputCommand(type="user_message", data={"content": "Exact heartbeat instruction"},
        turn_trigger="automation_heartbeat_scheduled")
    result = guards.start_or_steer("conv_core_contract", owner_id="scheduler-race", owner=RunContext(), command=command,
        required_final_output_json_schema=schema)
    assert result.mode == "not_submitted" and result.reason == reason
    assert guards.active_claim("conv_core_contract") == claim and queue.pending_count() == 0
    guards.end(claim)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane,subtype,reason", [
    ("user_input", "request_user_input", "waiting_on_user_input"),
    ("approval", "can_use_tool", "waiting_on_approval"),
    ("elicitation", "elicitation", "waiting_on_approval"),
])
async def test_real_request_lanes_block_both_entries_and_manual_ignores_renderer_only(scheduled_case, lane, subtype, reason):
    case = scheduled_case
    manager = WebSocketManager()
    session, generation = await manager.connect(**_connection(case.workspace, "heartbeat-lanes"))
    conversation = session.conversation_repo.create_conversation(workspace_root=str(case.workspace))
    session.active_conversation_id = conversation.id
    manager.load_conversation_execution(conversation.id, session)
    case.bootstrap.ws_manager = manager
    future = asyncio.get_running_loop().create_future()
    session.turn_wait_state.register_waiter("actual-request", future, kind=lane)
    session.turn_wait_state.pending_approval_payloads["actual-request"] = {
        "type": "control_request", "request_id": "actual-request", "conversation_id": conversation.id,
        "request": {"subtype": subtype},
    }

    async def execute(task, run):
        return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

    scheduler = TaskScheduler(on_fire=execute)
    scheduler.set_heartbeat_gate(lambda task, manual=False: runner.heartbeat_block_reason(task, bootstrap=case.bootstrap, manual=manual))
    task = due_task(scheduler, case, conversation.id)
    try:
        assert runner.heartbeat_block_reason(task, bootstrap=case.bootstrap, manual=True) == reason
        scheduler._tick(datetime.now(UTC))
        assert scheduler._runs == {} and task.last_run_at is None
        with pytest.raises(ValueError, match="busy right now"):
            scheduler.run_now(task.id)
        assert not future.done() and "actual-request" in session.turn_wait_state.pending_approval_payloads
        session.turn_wait_state.remove_waiter("actual-request")
        session.turn_wait_state.pending_approval_payloads.pop("actual-request")
        future.set_result(None)
        assert manager.record_heartbeat_renderer_state(session, generation, {
            "conversation_id": conversation.id, "is_eligible": False, "reason": "resuming"})
        task.next_run_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        scheduler._tick(datetime.now(UTC))
        assert scheduler._runs == {}
        result = await run_scheduled_task_now(scheduler, {"task_id": task.id}, workspace_root=str(case.workspace))
        await asyncio.gather(*list(scheduler._run_tasks.values()))
        assert result.runs[0]["turn_trigger"] == "automation_heartbeat_run_now"
        assert scheduler._runs[task.last_run_id].status == "completed"
    finally:
        if not future.done():
            future.set_result(None)
        await asyncio.gather(*list(scheduler._run_tasks.values()))
        await manager.shutdown(reason="heartbeat-request-lanes-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("input_origin", ["user", "heartbeat"])
async def test_completed_item_only_defers_mail_and_explicit_input_reopens_same_regular_turn(scheduled_case, monkeypatch, input_origin):
    case = scheduled_case
    model = FinalItemModel()
    submission = asyncio.Event()
    scheduler = None
    async with real_ws_owner(case, monkeypatch, model=model) as owner:
        if input_origin == "heartbeat":
            async def execute(task, run):
                await submission.wait()
                return await runner.run_scheduled_task(task, run, bootstrap=case.bootstrap)

            scheduler = TaskScheduler(on_fire=execute)
            task = due_task(scheduler, case, owner.session.active_conversation_id)
            scheduler._tick(datetime.now(UTC))
        foreground = await owner.launch()
        queue = owner.context.turn_input_queue
        turn_id, epoch = queue.turn_id, queue.turn_epoch
        original_claim = conversation_query_guards().active_claim(owner.session.active_conversation_id)
        model.release.set()
        try:
            async with asyncio.timeout(5):
                while queue.mailbox_phase is not MailboxDeliveryPhase.NEXT_TURN:
                    await asyncio.sleep(0)
            assert not owner.context.turn_terminal_receipt.done()
            if input_origin == "heartbeat":
                submission.set()
                async with asyncio.timeout(5):
                    while queue.pending_count() == 0:
                        await asyncio.sleep(0)
            else:
                item = owner.session.run_manager.enqueue_user_message_as_steer(owner.session.active_conversation_id,
                    UserCommand(type="user_message", data={"content": "New explicit human input.",
                        "user_message_id": "late-human-input", "assistant_message_id": "never-a-second-turn"}),
                    expected_turn_id=turn_id, target_message_id="actual-answer")
                assert item is not None
            assert queue.turn_id == turn_id and queue.turn_epoch == epoch
            assert queue.mailbox_phase is MailboxDeliveryPhase.CURRENT_TURN
            assert queue.snapshot()[0].input_origin == input_origin
            assert conversation_query_guards().active_claim(owner.session.active_conversation_id) == original_claim
            model.finish_response.set()
            await foreground
            if scheduler is not None:
                await asyncio.gather(*list(scheduler._run_tasks.values()))
                assert scheduler._runs[task.last_run_id].status == "completed"
            assert model.calls == 2 and owner.context.turn_terminal_receipt.result()["turn_id"] == turn_id
            assert owner.session.run_manager.enqueue_user_message_as_steer(owner.session.active_conversation_id,
                UserCommand(type="user_message", data={"content": "After canonical terminal."}), expected_turn_id=turn_id) is None
        finally:
            model.finish_response.set()
            submission.set()
            await foreground
            if scheduler is not None:
                await asyncio.gather(*list(scheduler._run_tasks.values()))
