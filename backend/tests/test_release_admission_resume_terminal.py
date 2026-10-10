from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from backend.agent.message import AgentEvent
from backend.conversations.repository import ConversationRepository
from backend.tests.test_ws_recovered_command_owner_chain import _session, _drain
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.stream_state import create_stream_state, apply_stream_event
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.event_outbox import EventOutbox
from backend.ws.payload_contracts import validate_session_projection_payload


@pytest.mark.asyncio
async def test_first_input_owner_survives_admission_without_command_completion(tmp_path):
    queue_root = tmp_path / "queue"
    queue = DurableUserMessageQueue(session_id="recovered-session", root_dir=queue_root)
    repository = ConversationRepository(tmp_path / "conversations")
    first, sent = _session(tmp_path, queue)
    first.conversation_repo = repository

    def ensure_first():
        first.active_conversation_id = repository.create_conversation(workspace_root=str(tmp_path)).id

    first._ensure_active_conversation = ensure_first
    command = {"type": "user_message", "content": "inspect project", "client_command_id": "cmd_first",
               "user_message_id": "u-first", "assistant_message_id": "a-first"}
    messages = iter([json.dumps(command)])

    async def receive():
        try:
            return next(messages)
        except StopIteration:
            raise WebSocketDisconnect()

    first.ws = SimpleNamespace(receive_text=receive)
    dispatcher = SessionCommandDispatcher(first, root_dir=tmp_path / "receipts")
    dispatcher._schedule_durable_client_command = lambda *_args: None
    try:
        with pytest.raises(WebSocketDisconnect):
            await dispatcher.run(1)
        pending = queue.pending_client_commands()
        assert first.active_conversation_id is None
        assert "conversation_id" not in pending[0].data
        claimed = queue.claim_client_command("cmd_first")
        async with first.conversation_lifecycle_lock():
            first._ensure_active_conversation()
            dispatcher._bind_user_message_owner(claimed, first.active_conversation_id)
        owner = claimed.data["conversation_id"]
        assert owner == first.active_conversation_id
        assert sent[0]["type"] == "client.command.ack"
        repository.commit_turn_admission(owner,
            user_message={"id": "u-first", "role": "user", "content": "inspect project", "timestamp": 1},
            context_snapshot={"turn_admissions": {"u-first": {"client_command_id": "cmd_first", "run_id": "first-run"}}})
    finally:
        queue.close()

    # The process disappeared after the canonical admission, before its
    # command completion record. The cold dispatcher sees the same owner.
    recovered = DurableUserMessageQueue(session_id="recovered-session", root_dir=queue_root)
    second, _ = _session(tmp_path, recovered)
    second.conversation_repo = ConversationRepository(tmp_path / "conversations")
    dispatcher = SessionCommandDispatcher(second, root_dir=tmp_path / "receipts")
    replayed = []

    async def admission_only_model(received):
        replayed.append(received.data["conversation_id"])
        current = second.conversation_repo.get_conversation(received.data["conversation_id"])
        assert "u-first" in current.context_snapshot["turn_admissions"]
        assert [item["id"] for item in current.transcript] == ["u-first"]

    dispatcher._handle_command_inner = admission_only_model
    try:
        await dispatcher._replay_pending_client_commands(1)
        await _drain(dispatcher)
        assert replayed == [owner]
        assert len(second.conversation_repo.list_conversations()) == 1
        assert recovered.pending_client_commands() == []
    finally:
        await _drain(dispatcher)
        recovered.close()


@pytest.mark.asyncio
async def test_first_input_owner_waits_for_prior_selection_without_blocking_interrupt(tmp_path):
    queue = DurableUserMessageQueue(session_id="recovered-session", root_dir=tmp_path / "queue")
    session, _ = _session(tmp_path, queue)
    session.active_conversation_id = "conversation-a"
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "receipts")
    switched = asyncio.Event()
    release = asyncio.Event()
    interrupted = asyncio.Event()
    admitted = []

    async def handle(command):
        if command.type == "conversation.switch":
            switched.set()
            await release.wait()
            session.active_conversation_id = "conversation-b"
        elif command.type == "interrupt":
            interrupted.set()
        elif command.type == "user_message":
            dispatcher._bind_user_message_owner(command, session.active_conversation_id)
            admitted.append(command.data["conversation_id"])

    dispatcher._handle_command_inner = handle
    from backend.agent.message import UserCommand
    switch = asyncio.create_task(dispatcher._handle_command(
        UserCommand(type="conversation.switch", data={"conversation_id": "conversation-b"})))
    await switched.wait()
    incoming = iter([
        json.dumps({"type": "user_message", "content": "inspect", "client_command_id": "cmd_unscoped"}),
        json.dumps({"type": "interrupt", "client_command_id": "cmd_interrupt"}),
    ])

    async def receive():
        try:
            return next(incoming)
        except StopIteration:
            raise WebSocketDisconnect()

    session.ws = SimpleNamespace(receive_text=receive)
    try:
        with pytest.raises(WebSocketDisconnect):
            await dispatcher.run(1)
        await asyncio.wait_for(interrupted.wait(), 1)
        assert admitted == []
        assert queue.has_client_command("cmd_unscoped")
        assert "conversation_id" not in queue._client_inflight["cmd_unscoped"].data
        release.set()
        await switch
        await _drain(dispatcher)
        assert admitted == ["conversation-b"]
    finally:
        release.set()
        await switch
        await _drain(dispatcher)
        queue.close()


@pytest.mark.asyncio
async def test_terminal_does_not_read_unrelated_locked_workspace_files(tmp_path, monkeypatch):
    from backend.tests.test_committed_turn_changes import _NoProvider
    from backend.agent.context import ContextBuilder
    from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
    from backend.agent.loop_session import AgentLoopSessionContext
    from backend.agent.run_context import RunContext
    from backend.agent.runtime import AgentRuntime
    from backend.agent.state import AgentState
    from backend.agent.execution_journal import ExecutionJournal
    from backend.artifact.store import ArtifactStore
    from backend.config import AgentSettings, PermissionSettings, TokenBudget
    from backend.permissions.checker import PermissionChecker
    from backend.tools.registry import ToolRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _NoProvider()
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime" / "metrics.jsonl",
        swarm_store_dir=tmp_path / "runtime" / "swarm", enable_lease_heartbeat=False)
    context = RunContext(agent_runtime=runtime,
        execution_journal=ExecutionJournal("terminal-owner", base_dir=tmp_path / "journals"))
    state = AgentState(user_message="inspect", workspace_root=workspace, conversation_id="owner")

    locked = workspace / "plot.svg"
    locked.write_text("<svg />", encoding="utf-8")
    original_read = Path.read_bytes
    reads = []

    def read_locked_file(self):
        if self == locked:
            reads.append(self)
            raise PermissionError("locked plot.svg")
        return original_read(self)

    monkeypatch.setattr(Path, "read_bytes", read_locked_file)

    async def accepted_runner(**_kwargs):
        state.reply = "Inspection completed."
        yield AgentEvent.agent_message_completed(state.reply, item_id="final", source="model_final")
        yield AgentEvent.done(status="completed")

    session = AgentSession(llm=adapter, tool_registry=ToolRegistry(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        permission_checker=PermissionChecker(PermissionSettings(), workspace),
        agent_settings=AgentSettings(max_turn_seconds=10), token_budget=TokenBudget(),
        context_builder=ContextBuilder(llm=adapter))
    try:
        events = [event async for event in QueryEngine(runner=accepted_runner).submit(QuerySubmission(
            session=session, state=state, user_message=state.user_message,
            runtime=AgentLoopSessionContext(workspace_root=workspace, run_context=context)))]
        assert [event.data["status"] for event in events if event.type == "done"] == ["completed"]
        assert state.terminal_status == "completed" and not session.active_turn
        assert reads == []
        assert not any(event.type == "error" for event in events)
        journal_events = context.execution_journal.read_events()
        assert sum(event.event_type == "terminal" for event in journal_events) == 1
        assert not any(event.event_type == "error" for event in journal_events)
        assert not any(event.type == "turn.diff.updated" for event in events)
    finally:
        await session.aclose()
        runtime.close(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_count", "block_count"), [(512, 1024), (513, 1025), (1800, 3200)])
async def test_live_restore_pages_preserve_all_tools_and_blocks(tmp_path, tool_count, block_count):
    stream = create_stream_state("owner", "assistant", "turn")
    streams = {"owner": stream}
    for index in range(tool_count):
        data = {"conversation_id": "owner", "message_id": "assistant", "turn_id": "turn",
                "id": f"tool-{index}", "name": "read_file", "args": {"file_path": f"file-{index}"}}
        apply_stream_event(streams, "owner", "tool_call", data)
        apply_stream_event(streams, "owner", "tool_result", {**data, "status": "success", "output": "ok"})
    for index in range(block_count - tool_count):
        apply_stream_event(streams, "owner", "agent.item", {
            "conversation_id": "owner", "message_id": "assistant", "turn_id": "turn",
            "id": f"narration-{index}", "kind": "process_text", "content": f"Step {index}", "status": "completed"})

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    class Owner(SessionApprovalRuntimeMixin):
        async def send_event(self, event):
            assert await self.outbox.send_payload(event.to_ws_message(), log_context="restore-test")

    socket = Socket()
    task = asyncio.create_task(asyncio.Event().wait())
    owner = Owner()
    owner.turn_wait_state = SimpleNamespace(pending_approval_payloads={})
    owner.run_manager = SimpleNamespace(run_tasks={"owner": task})
    owner._conversation_streams = streams
    owner.outbox = EventOutbox(session_id="restore", websocket=socket,
        replay_root=tmp_path / "events", replay_limit=1000, cleanup_tasks=set(), has_active_run=lambda: True,
        requires_conversation_owner=lambda *_args: True, workspace_scoped_event_types=set())
    try:
        await owner.reemit_pending_state()
        for page in socket.sent:
            validate_session_projection_payload(page)
        assert sum(len(page["tool_states"]) for page in socket.sent) == tool_count
        assert sum(len(page["content_blocks"]) for page in socket.sent) == block_count
        assert [tool["id"] for page in socket.sent for tool in page["tool_states"]] == list(stream["tool_calls"])
        if len(socket.sent) > 1:
            assert len({page["snapshot_id"] for page in socket.sent}) == 1
            assert [page["snapshot_part"] for page in socket.sent] == list(range(len(socket.sent)))
            assert [page["snapshot_complete"] for page in socket.sent] == [False] * (len(socket.sent) - 1) + [True]
        else:
            assert "snapshot_id" not in socket.sent[0]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await owner.outbox.drain_persistence()


@pytest.mark.asyncio
async def test_live_source_sequence_matches_snapshot_and_resume_does_not_mutate_it():
    from backend.tests.test_ws_event_semantic_projection import _minimal_event_session
    session = _minimal_event_session()
    stream = create_stream_state("conversation-1", "assistant-1", "turn-1")
    session._conversation_streams = {"conversation-1": stream}
    event = AgentEvent.agent_message_delta("first", item_id="item-1")
    event.data.update(conversation_id="conversation-1", message_id="assistant-1", turn_id="turn-1")
    await session.send_event(event)
    assert session.send_payload.await_args.args[0]["source_event_seq"] == stream["event_seq"] == 1
    await session.send_event(AgentEvent.stream_resume("conversation-1", "assistant-1", [], [],
        turn_id="turn-1", event_seq=stream["event_seq"]))
    assert stream["event_seq"] == 1
    assert "source_event_seq" not in session.send_payload.await_args.args[0]
