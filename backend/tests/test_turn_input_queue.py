from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from backend.agent.message import UserCommand
from backend.agent.run_context import RunContext
from backend.agent.turn_input import MailboxDeliveryPhase, TurnInputQueue
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallStartEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.agent.loop import run_agent_loop
from backend.agent.state import AgentState
from backend.ws.run_manager import SessionRunManager
from backend.ws.handlers.misc import handle_user_message_queue_steer


def _command(message_id: str, content: str) -> UserCommand:
    return UserCommand(
        type="user_message",
        data={
            "assistant_message_id": message_id,
            "user_message_id": f"user-{message_id}",
            "content": content,
            "attachments": [{"file_name": "context.txt", "artifact_id": "artifact-1"}],
        },
    )


def test_turn_input_queue_preserves_fifo_and_original_commands() -> None:
    queue = TurnInputQueue()
    first = _command("assistant-1", "first steer")
    second = _command("assistant-2", "second steer")

    assert queue.enqueue_command(first, target_message_id="assistant-current") is not None
    assert queue.enqueue_command(second) is not None
    first_item = queue.pop_steer()
    assert first_item is not None
    assert first_item.content == "first steer"
    assert first_item.target_message_id == "assistant-current"
    assert queue.seal_and_drain_commands() == [second]
    assert queue.sealed


def test_turn_input_preserves_structured_skill_and_plugin_mentions() -> None:
    queue = TurnInputQueue()
    command = UserCommand(
        type="user_message",
        data={
            "content": "use selected capabilities",
            "skills": [{"name": "review", "path": "C:/skills/review/SKILL.md"}],
            "plugins": [{"config_name": "docs", "path": "plugin://docs"}],
        },
    )

    item = queue.enqueue_command(command)

    assert item is not None
    assert item.selected_skills == ({
        "name": "review",
        "path": "C:/skills/review/SKILL.md",
    },)
    assert item.selected_plugins == ({
        "config_name": "docs",
        "path": "plugin://docs",
    },)


def test_turn_input_snapshot_is_non_destructive_and_preserves_target_identity() -> None:
    queue = TurnInputQueue()
    command = _command("assistant-steer", "change direction")

    queued = queue.enqueue_command(
        command,
        target_message_id="assistant-current",
    )

    assert queued is not None
    assert queue.snapshot() == (queued,)
    assert queue.pending_count() == 1
    assert queue.snapshot()[0].target_message_id == "assistant-current"
    assert queue.pop_steer() is queued


def test_turn_owner_defers_late_mail_and_reopens_on_next_turn() -> None:
    queue = TurnInputQueue()

    first_epoch = queue.begin_turn("run-1")
    assert queue.mailbox_deliverable("run-1") is True
    assert queue.defer_mailbox_to_next_turn("run-1") is True
    assert queue.mailbox_phase is MailboxDeliveryPhase.NEXT_TURN
    assert queue.mailbox_deliverable("run-1") is False
    assert queue.enqueue_command(_command("late-steer", "too late")) is None

    second_epoch = queue.begin_turn("run-2")
    assert second_epoch == first_epoch + 1
    assert queue.mailbox_phase is MailboxDeliveryPhase.CURRENT_TURN
    assert queue.mailbox_deliverable("run-2") is True
    assert queue.mailbox_deliverable("run-1") is False


def test_persistent_teammate_reopens_mailbox_when_reusing_one_run_id() -> None:
    """A teammate keeps one run id across turns and must not latch NEXT_TURN.

    Its second turn re-registers the same run, so if ``begin_turn`` returned
    early without reopening delivery the teammate would never receive mail
    again for the rest of the process.
    """
    queue = TurnInputQueue()

    first_epoch = queue.begin_turn("teammate-run")
    assert queue.defer_mailbox_to_next_turn("teammate-run") is True
    assert queue.mailbox_deliverable("teammate-run") is False

    second_epoch = queue.begin_turn("teammate-run")
    assert second_epoch == first_epoch
    assert queue.mailbox_phase is MailboxDeliveryPhase.CURRENT_TURN
    assert queue.mailbox_deliverable("teammate-run") is True
    assert queue.enqueue_command(_command("steer-2", "next turn steer")) is not None

    assert queue.defer_mailbox_to_next_turn("teammate-run") is True
    assert queue.begin_turn("teammate-run") == first_epoch
    assert queue.mailbox_deliverable("teammate-run") is True


def test_run_manager_projects_atomic_turn_phase() -> None:
    manager = SessionRunManager(SimpleNamespace())
    owner = manager.turn_input_queue("conv-a")
    owner.begin_turn("run-a")
    owner.defer_mailbox_to_next_turn("run-a")

    assert manager.turn_execution_snapshot("conv-a") == [{
        "conversation_id": "conv-a",
        "turn_id": "run-a",
        "turn_epoch": 1,
        "mailbox_phase": "next_turn",
        "pending_steer_count": 0,
    }]


def test_run_manager_does_not_drop_follow_ups_after_twenty_messages() -> None:
    manager = SessionRunManager(SimpleNamespace())

    for index in range(25):
        position = manager.enqueue_user_message(
            "conv-a",
            _command(f"assistant-{index}", f"follow-up {index}"),
        )
        assert position == index + 1

    snapshot = manager.queued_user_message_snapshot()
    assert len(snapshot) == 25
    assert [item["position"] for item in snapshot] == list(range(1, 26))


def test_run_manager_restores_unconsumed_turn_inputs_on_cleanup() -> None:
    async def scenario() -> None:
        session = SimpleNamespace(
            session_lifecycle=SimpleNamespace(
                schedule_task_runtime_update=lambda: None,
            ),
            conversation_lifecycle_lock=lambda: asyncio.Lock(),
        )
        manager = SessionRunManager(session)
        queue = manager.turn_input_queue("conv-a")
        command = _command("assistant-1", "restore me")
        assert queue.enqueue_command(command) is not None
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        manager.cleanup(
            conversation_id="conv-a",
            task=task,
            task_id="task-1",
            cancel_event=asyncio.Event(),
        )
        restored = manager.dequeue_user_message("conv-a")
        assert restored is command

    asyncio.run(scenario())


def test_durable_follow_up_queue_survives_manager_recreation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.ws.run_manager.CONVERSATION_DATA_DIR",
        tmp_path / "conversations",
    )
    session = SimpleNamespace(
        session_id="session-durable",
        schedule_task_runtime_update=lambda: None,
    )
    first = SessionRunManager(session)
    first.enqueue_user_message("conv-a", _command("assistant-durable", "recover me"))

    second = SessionRunManager(session)
    restored = second.dequeue_user_message("conv-a")
    assert restored is not None
    assert restored.data["content"] == "recover me"

    second.finish_user_message_dispatch("conv-a", restored, succeeded=True)

    third = SessionRunManager(session)
    assert third.dequeue_user_message("conv-a") is None


def test_session_queue_uses_session_conversation_repository_root(tmp_path: Path) -> None:
    conversation_dir = tmp_path / "conversations"
    session = SimpleNamespace(
        session_id="session-repository-root",
        conversation_repo=SimpleNamespace(_base_dir=conversation_dir),
        schedule_task_runtime_update=lambda: None,
    )

    manager = SessionRunManager(session)

    assert manager.durable_queue is not None
    assert manager.durable_queue.path.parent == (tmp_path / "user-message-queue").resolve()


def test_durable_follow_up_queue_retries_transient_windows_replace_lock(tmp_path: Path, monkeypatch) -> None:
    from backend.ws.durable_user_queue import DurableUserMessageQueue

    queue = DurableUserMessageQueue(session_id="replace-lock", root_dir=tmp_path)
    command = _command("assistant-lock", "keep me")
    original_replace = Path.replace
    attempts = {"count": 0}

    def flaky_replace(path: Path, target: Path):
        if path.name.startswith(".replace-lock") and attempts["count"] < 2:
            attempts["count"] += 1
            raise PermissionError("transient Windows sharing violation")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    queue.save({"conv-a": [command]}, {})

    queues, inflight = queue.load()
    assert attempts["count"] == 2
    assert inflight == {}
    assert queues["conv-a"][0].data["content"] == "keep me"


def test_legacy_v3_inflight_state_is_recovered_and_upgraded(tmp_path: Path) -> None:
    from backend.ws.durable_user_queue import DurableUserMessageQueue

    queue = DurableUserMessageQueue(session_id="legacy-v3", root_dir=tmp_path)
    queue.path.write_text(
        json.dumps({
            "version": 3,
            "queues": {
                "conv-a": [{
                    "type": "user_message",
                    "data": {
                        "assistant_message_id": "assistant-queued",
                        "content": "queued",
                    },
                }],
            },
            "inflight": {
                "conv-a": {
                    "type": "user_message",
                    "data": {
                        "assistant_message_id": "assistant-inflight",
                        "content": "inflight",
                    },
                },
            },
            "turn_inputs": {
                "conv-a": [{
                    "type": "user_message",
                    "data": {
                        "assistant_message_id": "assistant-steer",
                        "content": "steer",
                    },
                }],
            },
            "client_pending": [],
            "client_inflight": {
                "cmd-legacy": {
                    "type": "session.sync",
                    "data": {"client_command_id": "cmd-legacy"},
                },
            },
        }),
        encoding="utf-8",
    )

    queues, inflight = queue.load()

    assert inflight == {}
    assert [command.data["content"] for command in queues["conv-a"]] == [
        "steer",
        "inflight",
        "queued",
    ]
    assert [
        command.data["client_command_id"]
        for command in queue.pending_client_commands()
    ] == ["cmd-legacy"]
    upgraded = json.loads(queue.path.read_text(encoding="utf-8"))
    assert upgraded["version"] == 4
    assert upgraded["inflight"] == {}
    assert upgraded["turn_inputs"] == {}
    assert upgraded["client_inflight"] == {}


def test_process_exit_releases_owner_lease_for_crash_recovery(tmp_path: Path) -> None:
    from backend.ws.durable_user_queue import DurableUserMessageQueue

    child_code = "\n".join([
        "import sys",
        "from pathlib import Path",
        "from backend.agent.message import UserCommand",
        "from backend.ws.durable_user_queue import DurableUserMessageQueue",
        "queue = DurableUserMessageQueue(session_id='process-crash', root_dir=Path(sys.argv[1]))",
        "command = UserCommand(type='session.sync', data={'client_command_id': 'cmd-process-crash'})",
        "assert queue.persist_client_command(command)",
        "assert queue.claim_client_command('cmd-process-crash') is not None",
        "print('ready', flush=True)",
        "input()",
    ])
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        recovered = DurableUserMessageQueue(
            session_id="process-crash",
            root_dir=tmp_path,
        )
        recovered.load()
        assert recovered.claim_client_command("cmd-process-crash") is None

        process.kill()
        process.wait(timeout=10)
        recovered_command = None
        deadline = time.monotonic() + 2.0
        while recovered_command is None and time.monotonic() < deadline:
            recovered_command = recovered.claim_client_command("cmd-process-crash")
            if recovered_command is None:
                time.sleep(0.02)
        assert recovered_command is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_inflight_follow_up_is_replayed_after_crash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.ws.run_manager.CONVERSATION_DATA_DIR",
        tmp_path / "conversations",
    )
    base = SimpleNamespace(session_id="session-inflight")
    first = SessionRunManager(base)
    first.enqueue_user_message("conv-a", _command("assistant-inflight", "replay me"))
    dispatched = first.dequeue_user_message("conv-a")
    assert dispatched is not None

    recovered = SessionRunManager(base)
    # A second live runtime must not reinterpret the first runtime's active
    # dispatch as a process crash.
    assert recovered.dequeue_user_message("conv-a") is None
    assert first.durable_queue is not None
    first.durable_queue.close()
    replayed = recovered.dequeue_user_message("conv-a")
    assert replayed is not None
    assert replayed.data["content"] == "replay me"


def test_promoted_turn_inputs_are_replayed_in_fifo_order_after_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "backend.ws.run_manager.CONVERSATION_DATA_DIR",
        tmp_path / "conversations",
    )
    base = SimpleNamespace(session_id="session-turn-inflight")
    first = SessionRunManager(base)
    first.enqueue_user_message("conv-a", _command("assistant-1", "first steer"))
    first.enqueue_user_message("conv-a", _command("assistant-2", "second steer"))
    first.turn_input_queue("conv-a")

    first_command = first.pop_queued_user_message("conv-a", "assistant-1")
    second_command = first.pop_queued_user_message("conv-a", "assistant-2")
    assert first_command is not None
    assert second_command is not None
    assert first.enqueue_turn_steer("conv-a", first_command) is not None
    assert first.enqueue_turn_steer("conv-a", second_command) is not None

    recovered = SessionRunManager(base)
    assert recovered.dequeue_user_message("conv-a") is None
    assert first.durable_queue is not None
    first.durable_queue.close()
    replayed_first = recovered.dequeue_user_message("conv-a")
    assert replayed_first is not None
    recovered.finish_user_message_dispatch("conv-a", replayed_first, succeeded=True)
    replayed_second = recovered.dequeue_user_message("conv-a")

    assert replayed_first.data["content"] == "first steer"
    assert replayed_second is not None
    assert replayed_second.data["content"] == "second steer"


def test_two_live_owners_cannot_claim_same_follow_up(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.ws.run_manager.CONVERSATION_DATA_DIR",
        tmp_path / "conversations",
    )
    base = SimpleNamespace(session_id="session-live-owner-claim")
    first = SessionRunManager(base)
    first.enqueue_user_message("conv-a", _command("assistant-once", "run once"))
    second = SessionRunManager(base)

    claimed = first.dequeue_user_message("conv-a")

    assert claimed is not None
    assert second.dequeue_user_message("conv-a") is None
    first.finish_user_message_dispatch("conv-a", claimed, succeeded=True)
    assert second.dequeue_user_message("conv-a") is None


def test_stale_owner_append_does_not_resurrect_completed_follow_up(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "backend.ws.run_manager.CONVERSATION_DATA_DIR",
        tmp_path / "conversations",
    )
    base = SimpleNamespace(session_id="session-stale-owner-append")
    first = SessionRunManager(base)
    old = _command("assistant-old", "old")
    first.enqueue_user_message("conv-a", old)
    stale = SessionRunManager(base)

    claimed = first.dequeue_user_message("conv-a")
    assert claimed is not None
    first.finish_user_message_dispatch("conv-a", claimed, succeeded=True)
    stale.enqueue_user_message("conv-a", _command("assistant-new", "new"))

    verifier = SessionRunManager(base)
    next_command = verifier.dequeue_user_message("conv-a")
    assert next_command is not None
    assert next_command.data["content"] == "new"
    verifier.finish_user_message_dispatch("conv-a", next_command, succeeded=True)
    assert verifier.dequeue_user_message("conv-a") is None


def test_acknowledged_turn_input_is_not_replayed_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "backend.ws.run_manager.CONVERSATION_DATA_DIR",
        tmp_path / "conversations",
    )
    base = SimpleNamespace(session_id="session-turn-ack")
    first = SessionRunManager(base)
    command = _command("assistant-ack", "consume once")
    first.enqueue_user_message("conv-a", command)
    selected = first.pop_queued_user_message("conv-a", "assistant-ack")

    assert selected is command
    assert first.acknowledge_turn_input("conv-a", selected) is True
    assert first.acknowledge_turn_input("conv-a", selected) is False

    recovered = SessionRunManager(base)
    assert recovered.dequeue_user_message("conv-a") is None


def test_cleanup_immediately_restores_preaccepted_unacknowledged_steer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "backend.ws.run_manager.CONVERSATION_DATA_DIR",
            tmp_path / "conversations",
        )
        session = SimpleNamespace(
            session_id="session-preaccepted-cancel",
            session_lifecycle=SimpleNamespace(
                schedule_task_runtime_update=lambda: None,
            ),
            conversation_lifecycle_lock=lambda: asyncio.Lock(),
        )
        manager = SessionRunManager(session)
        command = _command("assistant-steer", "keep after cancel")
        follow_up = _command("assistant-follow-up", "run after recovered steer")
        manager.enqueue_user_message("conv-a", command)
        manager.enqueue_user_message("conv-a", follow_up)
        turn_queue = manager.turn_input_queue("conv-a")
        selected = manager.pop_queued_user_message("conv-a", "assistant-steer")
        assert selected is command
        assert manager.enqueue_turn_steer("conv-a", selected) is not None
        # Provider-chunk admission removes the item from the in-memory queue,
        # but ctx.start_turn has not acknowledged it yet.
        assert turn_queue.pop_steer() is not None

        task = asyncio.create_task(asyncio.sleep(0))
        await task
        manager.cleanup(
            conversation_id="conv-a",
            task=task,
            task_id="task-1",
            cancel_event=asyncio.Event(),
        )

        restored = manager.dequeue_user_message("conv-a")
        assert restored is command
        manager.finish_user_message_dispatch("conv-a", restored, succeeded=True)
        next_command = manager.dequeue_user_message("conv-a")
        assert next_command is follow_up
        manager.finish_user_message_dispatch("conv-a", next_command, succeeded=True)
        assert SessionRunManager(session).dequeue_user_message("conv-a") is None

    asyncio.run(scenario())


def test_runtime_queue_snapshot_is_frontend_replayable() -> None:
    manager = SessionRunManager(SimpleNamespace())
    manager.enqueue_user_message("conv-a", _command("assistant-1", "queued content"))

    assert manager.queued_user_message_snapshot() == [{
        "conversation_id": "conv-a",
        "message_id": "assistant-1",
        "user_message_id": "user-assistant-1",
        "content": "queued content",
        "position": 1,
    }]


def test_direct_steer_rejects_closed_turn_without_losing_follow_up() -> None:
    manager = SessionRunManager(SimpleNamespace())
    turn_queue = manager.turn_input_queue("conv-a")
    turn_queue.defer_mailbox_to_next_turn()
    command = _command("assistant-late", "late steer")

    assert manager.enqueue_user_message_as_steer("conv-a", command) is None
    manager.enqueue_user_message("conv-a", command)
    assert manager.dequeue_user_message("conv-a") is command


def test_queue_steer_targets_active_turn_without_cancelling_it() -> None:
    async def scenario() -> None:
        events = []
        cancel_called = False
        active_task = object()

        async def send_event(event):
            events.append(event)

        session = SimpleNamespace(
            active_conversation_id="conv-a",
            send_event=send_event,
            _conversation_streams={"conv-a": {"message_id": "assistant-current"}},
        )
        manager = SessionRunManager(session)
        session.run_manager = manager
        manager.enqueue_user_message("conv-a", _command("assistant-1", "steer now"))
        manager.turn_input_queue("conv-a")
        session.running_agent_task_for = lambda conversation_id: active_task

        async def cancel(**_kwargs):
            nonlocal cancel_called
            cancel_called = True

        session.cancel_agent_runs = cancel

        await handle_user_message_queue_steer(
            session,
            {"conversation_id": "conv-a", "message_id": "assistant-1"},
        )

        item = manager.turn_input_queue("conv-a").pop_steer()
        assert item is not None
        assert item.content == "steer now"
        assert not cancel_called
        assert events[0].data["status"] == "dequeued"
        assert events[0].data["reason"] == "steered_current_turn"
        assert events[0].data["target_message_id"] == "assistant-current"
        assert events[0].data["turn_mode"] == "steer"

    asyncio.run(scenario())


class _SteerLLM(LLMAdapter):
    def __init__(self, ready: asyncio.Event, release: asyncio.Event) -> None:
        self.calls = 0
        self.ready = ready
        self.release = release

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content="draft answer",
                phase="final_answer",
                raw={"message_phase": "final_answer"},
            )
            self.ready.set()
            await self.release.wait()
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content=" abandoned tail",
                phase="final_answer",
                raw={"message_phase": "final_answer"},
            )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
            return
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="steered answer",
            phase="final_answer",
            raw={"message_phase": "final_answer"},
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages):
        return ""


def test_run_agent_loop_consumes_steer_without_cancelling_current_run(tmp_path: Path) -> None:
    async def scenario():
        ready = asyncio.Event()
        release = asyncio.Event()
        queue = TurnInputQueue()
        llm = _SteerLLM(ready, release)
        state = AgentState(user_message="initial request", max_iterations=4)
        persisted = []
        acknowledged = []
        events = []

        async def collect():
            async for event in run_agent_loop(
                user_message="initial request",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
                permission_checker=PermissionChecker(
                    settings=PermissionSettings(),
                    workspace_root=tmp_path,
                ),
                agent_settings=AgentSettings(
                    max_iterations=4,
                    live_text_streaming=True,
                ),
                token_budget=TokenBudget(),
                permission_context=PermissionContext(mode="bypass"),
                metadata={
                    "turn_input_queue": queue,
                    "persist_consumed_turn_input": persisted.append,
                    "acknowledge_consumed_turn_input": acknowledged.append,
                },
                run_context=RunContext(
                    turn_input_queue=queue,
                    persist_consumed_turn_input=persisted.append,
                    acknowledge_consumed_turn_input=acknowledged.append,
                ),
                state=state,
            ):
                events.append(event)

        task = asyncio.create_task(collect())
        await ready.wait()
        steer_command = UserCommand(
            type="user_message",
            data={
                "assistant_message_id": "assistant-steer",
                "user_message_id": "user-steer",
                "content": "please change direction",
            },
        )
        assert queue.enqueue_command(
            steer_command,
            target_message_id="assistant-current",
        ) is not None
        release.set()
        await task
        return llm, events, state, persisted, acknowledged

    llm, events, state, persisted, acknowledged = asyncio.run(scenario())
    assert llm.calls == 2
    assert state.reply == "steered answer"
    assert len(persisted) == 1
    assert persisted[0].content == "please change direction"
    assert persisted[0].target_message_id == "assistant-current"
    assert acknowledged == persisted
    assert any(event.type == "done" and event.data.get("status") == "completed" for event in events)


class _PartialToolSteerLLM(LLMAdapter):
    def __init__(self, ready: asyncio.Event, release: asyncio.Event) -> None:
        self.ready = ready
        self.release = release

    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START,
            tool_call_start=ToolCallStartEvent(id="tool-partial", name="read_file"),
        )
        self.ready.set()
        await self.release.wait()
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="partial arguments are still in flight",
            phase="commentary",
            raw={"message_phase": "commentary"},
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")

    async def simple_chat(self, messages):
        return ""


def test_provider_chunk_steer_waits_when_tool_arguments_are_partial(tmp_path: Path) -> None:
    async def scenario():
        ready = asyncio.Event()
        release = asyncio.Event()
        queue = TurnInputQueue()
        persisted = []
        events = []

        async def collect():
            async for event in run_agent_loop(
                user_message="inspect a file",
                llm=_PartialToolSteerLLM(ready, release),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(storage_dir=str(tmp_path / "partial-tool-artifacts")),
                permission_checker=PermissionChecker(
                    settings=PermissionSettings(),
                    workspace_root=tmp_path,
                ),
                agent_settings=AgentSettings(max_iterations=2, live_text_streaming=True),
                token_budget=TokenBudget(),
                permission_context=PermissionContext(mode="bypass"),
                metadata={
                    "turn_input_queue": queue,
                    "persist_consumed_turn_input": persisted.append,
                },
                state=AgentState(user_message="inspect a file", max_iterations=2),
            ):
                events.append(event)

        task = asyncio.create_task(collect())
        await ready.wait()
        assert queue.enqueue_command(_command("assistant-steer", "change direction")) is not None
        release.set()
        await task
        return queue, persisted, events

    queue, persisted, events = asyncio.run(scenario())
    assert queue.pending_count() == 1
    assert persisted == []
    assert any(
        event.type == "error" and event.data.get("error_type") == "incomplete_tool_stream"
        for event in events
    )
