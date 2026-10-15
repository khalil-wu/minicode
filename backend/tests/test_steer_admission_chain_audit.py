"""Persist and replay the WS admission callback with real history and stores."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import execution_journal_owner, turn_input_consumption_id
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.turn_input import TurnInput
from backend.agent.turn_state import AgentTurnState
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMMessage
from backend.ws import agent_runner
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.run_manager import SessionRunManager
from backend.ws.stream_state import apply_stream_event, create_stream_state, get_stream_content_blocks
from backend.ws.conversation_stream import frozen_stream_pages
from backend.ws.reasoning_batcher import ReasoningEventBatcher, ReasoningFlushDeadline


@pytest.mark.asyncio
@pytest.mark.parametrize("steered", [False, True])
async def test_admission_and_journal_use_the_admitted_inputs_identity(tmp_path, steered, monkeypatch):
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation()
    context = ContextBuilder(conversation_id=conversation.id)
    context._history_store.append(LLMMessage(role="user", content="accepted request", is_user_input=True))
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    journal = runtime.execution_journal(execution_journal_owner("conversation", repo.store_instance_id(), conversation.id))
    command_id = "command-steer" if steered else "command-main"
    message_id = "user-steer" if steered else "user-main"
    origin = "steer-window" if steered else "admission-audit"
    original = UserCommand(type="user_message", data={
        "content": "accepted request", "conversation_id": conversation.id,
        "assistant_message_id": "assistant-input", "user_message_id": message_id,
        "client_command_id": command_id, "_queued_user_message_dispatch": True,
        "_client_queue_key": origin,
    })
    events = []
    started = []

    class ProjectionLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.queued = asyncio.Event()

        async def acquire(self):
            if self.locked():
                self.queued.set()
            return await super().acquire()

    projection_lock = ProjectionLock()
    turn_state = AgentTurnState(now_ms=lambda: 20_000)
    turn_state.append_thinking("before admission", {"source": "harness"})
    stream_state = create_stream_state(conversation.id, "assistant-input", "run-main")
    apply_stream_event({conversation.id: stream_state}, conversation.id, "thinking", {
        "message_id": "assistant-input", "turn_id": "run-main",
        "content": "before admission", "source": "harness",
    })

    async def send_event(event):
        events.append(event)
        apply_stream_event({conversation.id: stream_state}, conversation.id, event.type, event.data)

    async def start(content, **kwargs):
        started.append(kwargs)

    async def persist_partial_turn():
        pass

    session = SimpleNamespace(
        _conversation_projection_lock=lambda cid: projection_lock, conversation_repo=repo,
        _extension_shutdown_requested=False, active_conversation_id=conversation.id,
        session_id="admission-audit", send_event=send_event, start_agent_run=start,
        ws_manager=None,
        running_agent_task_for=lambda cid: None,
        session_lifecycle=SimpleNamespace(schedule_task_runtime_update=lambda: None),
    )
    manager = session.run_manager = SessionRunManager(session)
    manager._notification_wakes_closed = True
    try:
        # Exercise the actual nested callback without booting unrelated provider
        # and workspace services. Its implementation is never copied into this test.
        module = ast.parse(Path(agent_runner.__file__).read_text(encoding="utf-8"))
        callbacks = [node for node in ast.walk(module) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name in {"_commit_turn_admission", "_report_turn_admission", "_publish_turn_admission", "_partial_turn_message", "_flush_pending_reasoning", "_write_partial_turn"}]
        namespace = dict(vars(agent_runner))
        namespace.update(
            self=session, parent_notification_only=False, conversation=conversation,
            run_metadata={"user_message_id": "user-main", "client_command_id": "command-main", "run_id": "run-main",
                "_client_queue_key": "admission-audit", "assistant_message_id": "assistant-input"},
            user_message="accepted request", normalized_attachments=[], persisted_context_refs=[],
            run_context_builder=context, execution_journal=journal,
            conversation_store_id=repo.store_instance_id(),
            pending_admission_journal={}, assistant_message_id="assistant-input",
            turn_state=turn_state, stream_state=stream_state, turn_started_at_ms=10_000,
            _now_ms=lambda: 20_000, assistant_artifacts=[], latest_turn_diff=None,
            reasoning_batcher=ReasoningEventBatcher(), reasoning_flush_lock=asyncio.Lock(),
            _persist_partial_turn=persist_partial_turn,
            _accepts_projection_events=lambda: True, partial_persist_lock=asyncio.Lock(),
            run_context=RunContext(execution_journal=journal, agent_runtime=runtime),
        )
        # Keep the actual partial writer's nonlocal state in a real closure.
        factory = ast.parse('def make_callbacks():\n    last_partial_persisted_at = 0.0\n    partial_history_revision = None\n').body[0]
        factory.body.extend(callbacks)
        factory.body.append(ast.Return(value=ast.Call(func=ast.Name(id="locals", ctx=ast.Load()), args=[], keywords=[])))
        module = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
        exec(compile(module, agent_runner.__file__, "exec"), namespace)
        namespace.update(namespace["make_callbacks"]())
        namespace["reasoning_deadline"] = ReasoningFlushDeadline(0.01, namespace["_flush_pending_reasoning"])
        # The original visible frame was already reduced above. The next
        # same-stream frame is the real batcher's deferred frame.
        namespace["reasoning_batcher"].push(AgentEvent(type="thinking", data={
            "message_id": "assistant-input", "turn_id": "run-main",
            "content": "before admission", "source": "harness",
        }))
        boundary = SimpleNamespace(consumed_steer=TurnInput.from_command(original, target_message_id="assistant-input") if steered else None)
        if steered:
            with monkeypatch.context() as patch:
                def fail_commit(*args, **kwargs):
                    raise OSError("admission disk write failed")
                patch.setattr(repo, "commit_turn_admission", fail_commit)
                with pytest.raises(OSError, match="admission disk write failed"):
                    await namespace["_commit_turn_admission"](boundary_input=boundary, history_start=0, history_end=1)
            assert len(turn_state.finalize(terminal_status="partial").blocks) == 1
            assert repo.get_conversation(conversation.id).transcript == []
            assert events == []
            turn_state.append_thinking(" before user", {"source": "harness"})
            namespace["reasoning_batcher"].push(AgentEvent(type="thinking", data={
                "message_id": "assistant-input", "turn_id": "run-main",
                "content": " before user", "source": "harness",
            }))
        if steered:
            # Admission queues first. The following real partial writer must
            # capture after that admission, rather than overwrite its marker
            # with an earlier snapshot after acquiring the same boundary.
            await projection_lock.acquire()
            admission = asyncio.create_task(namespace["_commit_turn_admission"](
                boundary_input=boundary, history_start=0, history_end=1))
            await projection_lock.queued.wait()
            projection_lock.queued.clear()
            partial = asyncio.create_task(namespace["_write_partial_turn"](force=True))
            await projection_lock.queued.wait()
            projection_lock.release()
            await asyncio.gather(admission, partial)
            projections = [event for event in journal.read_events()
                if event.payload.get("lifecycle") == "conversation_projection_pending"]
            receipts = [event for event in journal.read_events()
                if event.payload.get("lifecycle") == "conversation_projection_committed"]
            assert len(projections) == len(receipts) == 1
            assert receipts[0].payload["pending_event_id"] == projections[0].event_id
            assert projections[0].payload["assistant_message"]["blocks"][-1] == {
                "type": "user_input", "userMessageId": "user-steer"}
        else:
            await namespace["_commit_turn_admission"](
                boundary_input=boundary, history_start=0, history_end=1)
        saved = repo.get_conversation(conversation.id)
        assert saved.context_snapshot["turn_admissions"][message_id]["client_command_id"] == command_id
        assert saved.context_snapshot["turn_admissions"][message_id]["client_queue_key"] == origin
        assert saved.context_snapshot["turn_admissions"][message_id]["assistant_message_id"] == "assistant-input"
        if steered:
            assert [message["id"] for message in saved.transcript] == ["assistant-input", "user-steer"]
            assert saved.transcript[0]["blocks"] == [
                {"type": "thinking", "content": "before admission before user", "source": "harness"},
                {"type": "user_input", "userMessageId": "user-steer"},
            ]
        prompts = [event for event in journal.read_events() if event.event_type == "user_prompt"]
        assert prompts == []
        if steered:
            assert [event.type for event in events] == ["thinking"]
        admission_events = list(events)
        dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "commands")
        # A recovered shared dispatch receives its own command object. The
        # handler consumes its queued-dispatch marker; retain the original
        # envelope for the separate later replay, as the durable queue does.
        await dispatcher._handle_command_inner(UserCommand(type=original.type, data=dict(original.data)))
        assert events == admission_events
        assert len(started) == 1
        assert started[0]["metadata"]["_turn_admission_restored"] is True
        if steered:
            manager.turn_input_queue(conversation.id)
            manager.enqueue_user_message_as_steer(conversation.id, original)
            manager.turn_input_queue(conversation.id).pop_steer()
        await namespace["_report_turn_admission"](boundary_input=boundary)
        if steered:
            item_events = [event for event in events if event.type in {"item.started", "item.completed"}]
            assert [event.type for event in item_events] == ["item.started", "item.completed"]
            assert item_events[0].data == item_events[1].data
            assert item_events[0].data["message_id"] == "assistant-input"
            assert item_events[0].data["turn_id"] == "run-main"
            assert item_events[0].data["item"]["message"] == saved.transcript[-1]
            turn_state.append_thinking("after admission", {"source": "harness"})
            apply_stream_event({conversation.id: stream_state}, conversation.id, "thinking", {
                "message_id": "assistant-input", "turn_id": "run-main",
                "content": "after admission", "source": "harness",
            })
            ordered = turn_state.finalize(terminal_status="partial").blocks
            assert [block["type"] for block in ordered] == ["thinking", "user_input", "thinking"]
            assert get_stream_content_blocks(stream_state) == ordered
            pages = frozen_stream_pages(conversation.id, stream_state)
            assert [block for page in pages for block in page.data["content_blocks"]] == ordered
        if steered:
            assert manager.acknowledge_turn_input(conversation.id, original)
        await namespace["_publish_turn_admission"](boundary_input=boundary)
        prompts = [event for event in journal.read_events() if event.event_type == "user_prompt"]
        assert len(prompts) == 1 and prompts[0].payload["client_command_id"] == command_id
        assert prompts[0].payload["client_queue_key"] == origin
        assert prompts[0].payload["assistant_message_id"] == "assistant-input"
        consumption = journal.get_event(turn_input_consumption_id(message_id, command_id))
        assert consumption.payload["client_queue_key"] == origin
        assert consumption.payload["assistant_message_id"] == "assistant-input"
        await dispatcher._handle_command_inner(original)
        assert len(started) == 1
        assert events[-1].data["data"]["consumed"] is True
        assert len(repo.get_conversation(conversation.id).transcript) == (2 if steered else 1)
    finally:
        manager._unsubscribe_parent_notifications()
        manager.close_durable_queue()
        runtime.close(release_lease=True)
