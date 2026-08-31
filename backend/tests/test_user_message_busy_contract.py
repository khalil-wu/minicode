import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

from backend.agent.message import UserCommand
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tasks.manager import TaskManager
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.run_manager import SessionRunManager


class _NoopLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        if False:
            yield None

    async def simple_chat(self, messages):
        return ""


async def _never_finishes():
    await asyncio.Event().wait()


def _admit_fake_turn(metadata: dict) -> None:
    admission_future = metadata.get("_turn_admission_future")
    assert isinstance(admission_future, asyncio.Future)
    admission_future.set_result(None)


def test_session_cleanup_discards_all_conversation_queues():
    manager = SessionRunManager(SimpleNamespace())
    manager.enqueue_user_message("conv-a", UserCommand(type="user_message", data={"content": "a"}))
    manager.enqueue_user_message("conv-b", UserCommand(type="user_message", data={"content": "b"}))

    manager.clear_all_user_message_queues()

    assert manager.dequeue_user_message("conv-a") is None
    assert manager.dequeue_user_message("conv-b") is None


def test_parent_notification_probe_reads_outbox_without_runtime_hydration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from backend.agent import parent_notification_outbox, runtime as runtime_module

    monkeypatch.setattr(parent_notification_outbox, "OUTBOX_ROOT", tmp_path / "outbox")
    monkeypatch.setattr(runtime_module, "_DEFAULT_RUNTIME", None)

    class UnexpectedRuntimeConstruction:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("notification replay must not hydrate all agent history")

    monkeypatch.setattr(runtime_module, "AgentRuntime", UnexpectedRuntimeConstruction)
    parent_notification_outbox.enqueue_parent_notification(
        parent_run_id="run-parent",
        conversation_id="conv-notification-replay",
        session_id="session-notification-replay",
        subagent_id="worker-1",
        payload={"status": "completed"},
    )
    manager = SessionRunManager(
        SimpleNamespace(session_id="session-notification-replay")
    )

    assert manager._has_replayable_parent_notification(  # type: ignore[attr-defined]
        "conv-notification-replay"
    ) is True
    assert runtime_module.default_runtime_if_initialized() is None


def test_promote_queued_user_message_moves_the_selected_prompt_to_front():
    manager = SessionRunManager(SimpleNamespace())
    for message_id in ("assistant-1", "assistant-2", "assistant-3"):
        manager.enqueue_user_message(
            "conv-a",
            UserCommand(type="user_message", data={"assistant_message_id": message_id}),
        )

    promoted = manager.promote_queued_user_message("conv-a", "assistant-3")

    assert promoted is not None
    assert [command.data["assistant_message_id"] for command in promoted] == [
        "assistant-3",
        "assistant-1",
        "assistant-2",
    ]
    assert manager.promote_queued_user_message("conv-a", "missing") is None
    assert [
        manager.dequeue_user_message("conv-a").data["assistant_message_id"]
        for _ in range(3)
    ] == ["assistant-3", "assistant-1", "assistant-2"]


def test_delivery_complete_releases_conversation_before_task_cleanup():
    async def scenario():
        session = SimpleNamespace(schedule_task_runtime_update=lambda: None)
        manager = SessionRunManager(session)
        task = asyncio.create_task(_never_finishes())
        manager.run_tasks["conv-a"] = task
        try:
            assert manager.running_task_for("conv-a") is task
            manager.mark_delivery_complete("conv-a")
            assert manager.running_task_for("conv-a") is None
            manager.enqueue_user_message(
                "conv-a",
                UserCommand(type="user_message", data={"content": "queued"}),
            )
            assert manager.running_task_for("conv-a") is task
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_scheduler_fallback_projects_existing_durable_completion(
    tmp_path: Path,
) -> None:
    events: list[dict] = []
    session = SimpleNamespace(
        active_conversation_id="conv-durable",
        event_outbox=SimpleNamespace(
            bind_connection_generation=lambda _generation: contextlib.nullcontext(),
        ),
        _command_tasks=set(),
        _conversation_streams={},
        schedule_task_runtime_update=lambda: None,
        schedule_next_queued_user_message=lambda _conversation_id: None,
        # SessionRunManager now notifies the lifecycle owner whenever run
        # ownership changes.  This fixture only exercises the scheduler
        # fallback, so provide the narrow lifecycle seam explicitly.
        session_lifecycle=SimpleNamespace(
            schedule_task_runtime_update=lambda: None,
        ),
    )
    session.run_manager = SessionRunManager(session)

    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    run = runtime.start_run(
        run_id="run-durable",
        conversation_id="conv-durable",
        task_id="task-durable",
    )
    runtime.commit_terminal(
        run.run_id,
        "completed",
        summary="Final answer committed",
        terminal_reason="completed",
    )

    async def already_committed_run(
        *_args,
        metadata=None,
        run_context=None,
        **_kwargs,
    ):
        assert isinstance(metadata, dict)
        assert run_context is not None
        run_context.agent_runtime = runtime
        metadata["run_id"] = run.run_id

    async def capture_event(event):
        events.append(event.to_ws_message())

    session._run_agent = already_committed_run
    session.send_event = capture_event
    session._register_agent_run = WebSocketSession._register_agent_run.__get__(session)
    session._cleanup_agent_run = WebSocketSession._cleanup_agent_run.__get__(session)
    session.command_dispatcher = SimpleNamespace(
        track_command_task=lambda task: session._command_tasks.add(task)
    )
    session.task_manager = TaskManager()

    async def scenario() -> None:
        try:
            await WebSocketSession.start_agent_run(
                session,
                "hello",
                conversation_id="conv-durable",
                metadata={"assistant_message_id": "assistant-durable"},
            )
            await asyncio.gather(*tuple(session._command_tasks))
        finally:
            runtime.close(release_lease=True)

    asyncio.run(scenario())

    completed = [event for event in events if event.get("type") == "agent.run.completed"]
    done = [event for event in events if event.get("type") == "done"]
    assert len(completed) == 1
    assert completed[0]["status"] == "completed"
    assert completed[0]["terminal_reason"] == "completed"
    assert len(done) == 1
    assert done[0]["status"] == "completed"
    assert done[0]["reason"] == "completed"


def test_busy_user_message_is_queued_with_stable_message_ids(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    websocket = SimpleNamespace()
    session = WebSocketSession(
        session_id="session_busy",
        websocket=websocket,
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(
        conversation_id="conv_busy",
        title="Busy",
        workspace_root=str(tmp_path),
    )
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id

    async def capture_event(event):
        events.append(event.to_ws_message())

    async def noop_permission(*args, **kwargs):
        return True

    async def scenario():
        running_task = asyncio.create_task(_never_finishes())
        try:
            session.run_manager.run_tasks[conversation.id] = running_task

            await session.command_dispatcher._handle_command_inner(
                UserCommand(
                    type="user_message",
                    data={
                        "content": "再查上海天气",
                        "conversation_id": conversation.id,
                        "assistant_message_id": "assistant-rejected",
                    },
                )
            )
        finally:
            running_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running_task

    monkeypatch.setattr(session, "send_event", capture_event)
    monkeypatch.setattr(
        session.command_dispatcher,
        "_handle_user_message_permission",
        noop_permission,
    )

    asyncio.run(scenario())

    assert len(events) == 1
    assert events[0] == {
        "type": "user_message.queue.updated",
        "status": "queued",
        "conversation_id": conversation.id,
        "message_id": "assistant-rejected",
        "user_message_id": events[0]["user_message_id"],
        "position": 1,
    }
    assert str(events[0]["user_message_id"]).startswith("user_")
    queued = session.run_manager.dequeue_user_message(conversation.id)
    assert queued is not None
    assert queued.data["content"] == "再查上海天气"
    assert queued.data["user_message_id"] == events[0]["user_message_id"]


def test_busy_user_message_can_atomically_steer_current_turn(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    session = WebSocketSession(
        session_id="session_direct_steer",
        websocket=SimpleNamespace(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(
        conversation_id="conv_direct_steer",
        title="Direct steer",
        workspace_root=str(tmp_path),
    )
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id
    session._conversation_streams[conversation.id] = {"message_id": "assistant-current"}
    session.run_manager.turn_input_queue(conversation.id)

    async def capture_event(event):
        events.append(event.to_ws_message())

    async def noop_permission(*_args, **_kwargs):
        return True

    monkeypatch.setattr(session, "send_event", capture_event)
    monkeypatch.setattr(
        session.command_dispatcher,
        "_handle_user_message_permission",
        noop_permission,
    )

    async def scenario():
        current_task = asyncio.create_task(_never_finishes())
        session.run_manager.run_tasks[conversation.id] = current_task
        try:
            await session.command_dispatcher._handle_command_inner(UserCommand(
                type="user_message",
                data={
                    "content": "change direction now",
                    "conversation_id": conversation.id,
                    "assistant_message_id": "assistant-steer",
                    "user_message_id": "user-steer",
                    "streaming_behavior": "steer",
                },
            ))
            item = session.run_manager.turn_input_queue(conversation.id).pop_steer()
            assert item is not None
            assert item.content == "change direction now"
            assert item.target_message_id == "assistant-current"
            assert session.run_manager.dequeue_user_message(conversation.id) is None
            session.run_manager.acknowledge_turn_input(conversation.id, item.original_command)
        finally:
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await current_task
            session.run_manager.run_tasks.clear()

    asyncio.run(scenario())

    assert events == [{
        "type": "user_message.queue.updated",
        "status": "dequeued",
        "conversation_id": conversation.id,
        "message_id": "assistant-steer",
        "user_message_id": "user-steer",
        "reason": "steered_current_turn",
        "target_message_id": "assistant-current",
        "turn_mode": "steer",
    }]


def test_queued_user_messages_run_in_fifo_order(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    runs: list[str] = []
    run_metadata: list[dict] = []
    release_first = asyncio.Event()
    session = WebSocketSession(
        session_id="session_queue_fifo",
        websocket=SimpleNamespace(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(
        conversation_id="conv_fifo",
        title="FIFO",
        workspace_root=str(tmp_path),
    )
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id

    async def capture_event(event):
        events.append(event.to_ws_message())

    async def fake_run_agent(content, **_kwargs):
        _admit_fake_turn(_kwargs["metadata"])
        runs.append(content)
        run_metadata.append(dict(_kwargs.get("metadata") or {}))
        if content == "first":
            await release_first.wait()

    async def noop_permission(*_args, **_kwargs):
        return True

    monkeypatch.setattr(session, "send_event", capture_event)
    monkeypatch.setattr(session, "_run_agent", fake_run_agent)
    monkeypatch.setattr(
        session.command_dispatcher,
        "_handle_user_message_permission",
        noop_permission,
    )

    async def scenario():
        for index, content in enumerate(("first", "second", "third"), 1):
            await session.command_dispatcher._handle_command_inner(UserCommand(
                type="user_message",
                data={
                    "content": content,
                    "conversation_id": conversation.id,
                    "assistant_message_id": f"assistant-{index}",
                    "user_message_id": f"user-{index}",
                },
            ))
            await asyncio.sleep(0)
        assert runs == ["first"]
        release_first.set()
        for _ in range(100):
            if runs == ["first", "second", "third"] and not session.running_agent_task_for(conversation.id):
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert runs == ["first", "second", "third"]
    assert [metadata.get("user_message_id") for metadata in run_metadata] == [
        "user-1",
        "user-2",
        "user-3",
    ]
    queue_updates = [event for event in events if event["type"] == "user_message.queue.updated"]
    assert [(event["status"], event["message_id"]) for event in queue_updates] == [
        ("queued", "assistant-2"),
        ("queued", "assistant-3"),
        ("dequeued", "assistant-2"),
        ("dequeued", "assistant-3"),
    ]
    follow_up_events = [event for event in queue_updates if event["status"] == "dequeued"]
    assert [event.get("turn_mode") for event in follow_up_events] == ["follow_up", "follow_up"]


def test_steered_message_promotes_into_current_turn_without_interrupting(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    runs: list[tuple[str, list[dict]]] = []
    release_first = asyncio.Event()
    session = WebSocketSession(
        session_id="session_queue_steer",
        websocket=SimpleNamespace(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(
        conversation_id="conv_steer",
        title="Steer queue",
        workspace_root=str(tmp_path),
    )
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id

    async def capture_event(event):
        events.append(event.to_ws_message())

    async def fake_run_agent(content, *, attachments=None, **_kwargs):
        _admit_fake_turn(_kwargs["metadata"])
        runs.append((content, list(attachments or [])))
        if content == "first":
            await release_first.wait()

    async def noop_permission(*_args, **_kwargs):
        return True

    monkeypatch.setattr(session, "send_event", capture_event)
    monkeypatch.setattr(session, "_run_agent", fake_run_agent)
    monkeypatch.setattr(
        session.command_dispatcher,
        "_handle_user_message_permission",
        noop_permission,
    )

    third_attachment = {"file_name": "pasted-1.txt", "artifact_id": "artifact-third"}

    async def scenario():
        for index, content in enumerate(("first", "second", "third"), 1):
            await session.command_dispatcher._handle_command_inner(UserCommand(
                type="user_message",
                data={
                    "content": content,
                    "conversation_id": conversation.id,
                    "assistant_message_id": f"assistant-{index}",
                    "user_message_id": f"user-{index}",
                    "attachments": [third_attachment] if content == "third" else [],
                },
            ))
            await asyncio.sleep(0)
        await session.command_dispatcher._handle_command_inner(UserCommand(
            type="user_message.queue.steer",
            data={
                "conversation_id": conversation.id,
                "message_id": "assistant-3",
                "user_message_id": "user-3",
            },
        ))
        release_first.set()
        for _ in range(100):
            if [content for content, _ in runs] == ["first", "third", "second"] and not session.running_agent_task_for(conversation.id):
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert [content for content, _ in runs] == ["first", "third", "second"]
    assert len(runs[1][1]) == 1
    assert runs[1][1][0]["file_name"] == "pasted-1.txt"
    assert runs[1][1][0]["artifact_id"] == "artifact-third"
    queue_updates = [event for event in events if event["type"] == "user_message.queue.updated"]
    compact_updates = [
        (event["status"], event["message_id"], event.get("reason"))
        for event in queue_updates
    ]
    assert compact_updates[:3] == [
        ("queued", "assistant-2", None),
        ("queued", "assistant-3", None),
        ("dequeued", "assistant-3", "steered_current_turn"),
    ]
    assert any(status == "dequeued" and message_id == "assistant-2" for status, message_id, _ in compact_updates)
    assert not any(reason == "user_steered" for _, _, reason in compact_updates)


def test_steer_supersedes_pending_approval_without_cancelling_run(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    session = WebSocketSession(
        session_id="session_steer_approval",
        websocket=SimpleNamespace(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(
        conversation_id="conv_steer_approval",
        title="Steer approval",
        workspace_root=str(tmp_path),
    )
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id

    async def capture_event(event):
        events.append(event.to_ws_message())

    monkeypatch.setattr(session, "send_event", capture_event)

    async def scenario():
        running_task = asyncio.create_task(_never_finishes())
        session.run_manager.run_tasks[conversation.id] = running_task
        session.run_manager.run_task_ids[conversation.id] = "task-current"
        session._conversation_streams[conversation.id] = {"message_id": "assistant-current"}
        session.run_manager.turn_input_queue(conversation.id)
        session.run_manager.enqueue_user_message(
            conversation.id,
            UserCommand(
                type="user_message",
                data={
                    "content": "change direction",
                    "conversation_id": conversation.id,
                    "assistant_message_id": "assistant-steer",
                    "user_message_id": "user-steer",
                },
            ),
        )
        session.turn_wait_state.pending_approval_payloads["tool-write"] = {
            "type": "approval_request",
            "conversation_id": conversation.id,
            "tool_name": "write_file",
            "args": {"file_path": "demo.txt"},
        }
        approval_task = asyncio.create_task(session.approval_handler("tool-write"))
        await asyncio.sleep(0)

        await session.command_dispatcher._handle_command_inner(UserCommand(
            type="user_message.queue.steer",
            data={
                "conversation_id": conversation.id,
                "message_id": "assistant-steer",
            },
        ))

        approval = await approval_task
        snapshot = session.runtime_snapshot()["pending_turn_inputs"]
        run_was_cancelled = running_task.cancelled() or running_task.done()
        running_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running_task
        return approval, snapshot, run_was_cancelled

    approval, snapshot, run_was_cancelled = asyncio.run(scenario())

    assert approval == {
        "action": "reject",
        "guidance": "The user redirected the current task; this pending action was superseded.",
        "reason": "user_steer",
        "superseded": True,
    }
    assert run_was_cancelled is False
    assert len(snapshot) == 1
    assert snapshot[0] == {
        "conversation_id": conversation.id,
        "mode": "steer",
        "message_id": "assistant-steer",
        "user_message_id": "user-steer",
        "target_message_id": "assistant-current",
        "content": "change direction",
        "attachments": [],
        "position": 1,
        "queued_at_ms": snapshot[0]["queued_at_ms"],
    }
    approval_events = [event for event in events if event["type"] == "approval.cancelled"]
    assert len(approval_events) == 1
    assert approval_events[0]["reason"] == "user_steer"
    assert approval_events[0]["request_ids"] == ["tool-write"]
    queue_event = next(event for event in events if event["type"] == "user_message.queue.updated")
    assert queue_event["turn_mode"] == "steer"
    assert queue_event["target_message_id"] == "assistant-current"


def test_queued_user_message_can_be_cancelled_without_interrupting_active_run(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    session = WebSocketSession(
        session_id="session_queue_cancel",
        websocket=SimpleNamespace(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(title="Cancel queue", workspace_root=str(tmp_path))
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id

    async def capture_event(event):
        events.append(event.to_ws_message())

    async def scenario():
        running_task = asyncio.create_task(_never_finishes())
        session.run_manager.run_tasks[conversation.id] = running_task
        try:
            await session.command_dispatcher._handle_command_inner(UserCommand(
                type="user_message",
                data={
                    "content": "later",
                    "conversation_id": conversation.id,
                    "assistant_message_id": "assistant-later",
                    "user_message_id": "user-later",
                },
            ))
            await session.command_dispatcher._handle_command_inner(UserCommand(
                type="user_message.queue.cancel",
                data={
                    "conversation_id": conversation.id,
                    "message_id": "assistant-later",
                    "user_message_id": "user-later",
                },
            ))
        finally:
            running_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running_task

    monkeypatch.setattr(session, "send_event", capture_event)
    asyncio.run(scenario())

    assert session.run_manager.dequeue_user_message(conversation.id) is None
    queue_updates = [
        event for event in events if event["type"] == "user_message.queue.updated"
    ]
    assert [(event["status"], event["message_id"]) for event in queue_updates] == [
        ("queued", "assistant-later"),
        ("cancelled", "assistant-later"),
    ]


def test_message_sent_while_stop_is_finishing_stays_behind_existing_queue(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    started: list[str] = []
    session = WebSocketSession(
        session_id="session_queue_stop_transition",
        websocket=SimpleNamespace(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(title="Stop transition", workspace_root=str(tmp_path))
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id

    approvals_entered = asyncio.Event()
    release_approvals = asyncio.Event()

    async def capture_event(event):
        events.append(event.to_ws_message())

    async def noop_permission(*_args, **_kwargs):
        return True

    async def hold_approval_cleanup(**_kwargs):
        approvals_entered.set()
        await release_approvals.wait()

    async def fake_run_agent(content, **_kwargs):
        _admit_fake_turn(_kwargs["metadata"])
        started.append(content)

    monkeypatch.setattr(session, "send_event", capture_event)
    monkeypatch.setattr(
        session.command_dispatcher,
        "_handle_user_message_permission",
        noop_permission,
    )
    monkeypatch.setattr(session, "_cancel_pending_approvals", hold_approval_cleanup)
    monkeypatch.setattr(session, "_run_agent", fake_run_agent)

    async def scenario():
        current_task = asyncio.create_task(_never_finishes())
        current_cancel_event = asyncio.Event()
        session.run_manager.run_tasks[conversation.id] = current_task
        session.run_manager.run_task_ids[conversation.id] = "task-current"
        session.run_manager.cancel_events[conversation.id] = current_cancel_event

        await session.command_dispatcher._handle_command_inner(UserCommand(
            type="user_message",
            data={
                "content": "second",
                "conversation_id": conversation.id,
                "assistant_message_id": "assistant-second",
            },
        ))

        cancel_task = asyncio.create_task(session.cancel_agent_runs(
            conversation_id=conversation.id,
            reason="user_interrupted",
        ))
        await approvals_entered.wait()
        await session.command_dispatcher._handle_command_inner(UserCommand(
            type="user_message",
            data={
                "content": "third",
                "conversation_id": conversation.id,
                "assistant_message_id": "assistant-third",
            },
        ))

        queued = []
        for _index in range(2):
            command = session.run_manager.dequeue_user_message(conversation.id)
            queued.append(command)
            if command is not None:
                session.run_manager.finish_user_message_dispatch(
                    conversation.id,
                    command,
                    succeeded=True,
                )
        release_approvals.set()
        await cancel_task
        with contextlib.suppress(asyncio.CancelledError):
            await current_task
        session.run_manager.run_tasks.clear()
        session.run_manager.run_task_ids.clear()
        session.run_manager.cancel_events.clear()
        return queued, current_cancel_event.is_set()

    queued, cancel_signalled = asyncio.run(scenario())

    assert [command.data["content"] for command in queued if command is not None] == ["second", "third"]
    assert started == []
    assert cancel_signalled is True
    queue_updates = [event for event in events if event["type"] == "user_message.queue.updated"]
    assert [(event["status"], event["message_id"]) for event in queue_updates] == [
        ("queued", "assistant-second"),
        ("queued", "assistant-third"),
    ]


def test_stopping_current_run_continues_with_next_queued_message(tmp_path: Path, monkeypatch):
    events: list[dict] = []
    runs: list[str] = []
    session = WebSocketSession(
        session_id="session_queue_stop_continue",
        websocket=SimpleNamespace(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path),
        config=AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(title="Stop and continue", workspace_root=str(tmp_path))
    session.conversation_repo = repo
    session.active_conversation_id = conversation.id

    async def capture_event(event):
        events.append(event.to_ws_message())

    async def noop_permission(*_args, **_kwargs):
        return True

    async def fake_run_agent(content, **_kwargs):
        _admit_fake_turn(_kwargs["metadata"])
        runs.append(content)
        if content == "first":
            await asyncio.Event().wait()

    monkeypatch.setattr(session, "send_event", capture_event)
    monkeypatch.setattr(
        session.command_dispatcher,
        "_handle_user_message_permission",
        noop_permission,
    )
    monkeypatch.setattr(session, "_run_agent", fake_run_agent)

    async def scenario():
        await session.command_dispatcher._handle_command_inner(UserCommand(
            type="user_message",
            data={
                "content": "first",
                "conversation_id": conversation.id,
                "assistant_message_id": "assistant-first",
            },
        ))
        await asyncio.sleep(0)
        await session.command_dispatcher._handle_command_inner(UserCommand(
            type="user_message",
            data={
                "content": "second",
                "conversation_id": conversation.id,
                "assistant_message_id": "assistant-second",
            },
        ))
        assert runs == ["first"]
        assert await session.cancel_agent_runs(
            conversation_id=conversation.id,
            reason="user_interrupted",
        ) is True
        for _ in range(100):
            if runs == ["first", "second"] and session.running_agent_task_for(conversation.id) is None:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert runs == ["first", "second"]
    queue_updates = [event for event in events if event["type"] == "user_message.queue.updated"]
    assert [(event["status"], event["message_id"]) for event in queue_updates] == [
        ("queued", "assistant-second"),
        ("dequeued", "assistant-second"),
    ]
