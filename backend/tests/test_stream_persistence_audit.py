from __future__ import annotations

import asyncio
import copy
import json
import threading
import tracemalloc
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import UserCommand
from backend.agent.turn_input import TurnInputQueue
from backend.checkpoint.store import CheckpointRecord
from backend.conversations.repository import ConversationRepository
from backend.ws.agent_runner import SessionAgentRunnerMixin, _commit_automatic_compaction, _replay_pending_conversation_projections
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.event_log import ReplayLogReadStatus
from backend.ws.event_outbox import EventOutbox
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.misc import handle_checkpoint_rewind
from backend.ws.run_manager import SessionRunManager
from backend.ws.turn_wait_state import TurnWaitState


def test_failed_journal_open_does_not_poison_future_sequence(tmp_path, monkeypatch):
    journal = ExecutionJournal("fixture", base_dir=tmp_path)
    journal.append("system", {"first": True})
    original = Path.open
    def denied(path, mode="r", *args, **kwargs):
        if path == journal.path and mode == "a":
            raise PermissionError("temporary denial")
        return original(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", denied)
        with pytest.raises(PermissionError):
            journal.append("system", {"failed": True})
    assert journal.append("system", {"after": True}).seq == 2
    assert [event.seq for event in ExecutionJournal("fixture", base_dir=tmp_path).read_events()] == [1, 2]


@pytest.mark.asyncio
async def test_buffered_replay_burst_uses_bounded_memory():
    class Socket:
        async def send_json(self, payload):
            pass
    class Store:
        path = Path("unused-fixture.jsonl")
        root_dir = Path(".")
        read_status = ReplayLogReadStatus()
        def append(self, payload):
            pass
        def rewrite(self, payload):
            pass
    box = EventOutbox(session_id="probe", websocket=Socket(), replay_root=Path("."), replay_limit=1000,
        cleanup_tasks=set(), has_active_run=lambda: True, requires_conversation_owner=lambda *_: False,
        workspace_scoped_event_types=set(), replay_state=(Store(), []))
    tracemalloc.start()
    try:
        for _ in range(1000):
            await box.send_payload({"type": "agent_message.delta", "conversation_id": "conv_probe", "message_id": "m", "item_id": "a", "delta": "hello"}, log_context="test")
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 32 * 1024 * 1024
    finally:
        tracemalloc.stop()
        await box.drain_persistence()


def test_reader_never_caches_old_content_under_a_new_generation(tmp_path):
    writer = ConversationRepository(tmp_path)
    record = writer.create_conversation(title="before")
    reader = ConversationRepository(tmp_path)
    loaded, release, written = threading.Event(), threading.Event(), threading.Event()
    original_load = reader._load_record
    errors = []
    def load(owner):
        value = original_load(owner)
        loaded.set()
        assert release.wait(3)
        return value
    reader._load_record = load
    def read():
        try:
            reader.get_conversation(record.id)
        except Exception as exc:
            errors.append(exc)
    def write():
        try:
            writer.rename_conversation(record.id, "after")
        except Exception as exc:
            errors.append(exc)
        finally:
            written.set()
    read_thread = threading.Thread(target=read)
    write_thread = threading.Thread(target=write)
    read_thread.start()
    assert loaded.wait(3)
    write_thread.start()
    written.wait(.2)
    release.set()
    read_thread.join(3)
    write_thread.join(3)
    reader._load_record = original_load
    assert not read_thread.is_alive() and not write_thread.is_alive()
    assert not errors
    assert reader.get_conversation(record.id).title == "after"


def test_modern_utf8_literals_survive_repository_reload(tmp_path):
    text = "Debug the literal string \u00c3\u00a9 without changing it."
    repository = ConversationRepository(tmp_path)
    record = repository.create_conversation(transcript=[{"id": "u1", "role": "user", "content": text}],
        context_snapshot={"history": [{"role": "user", "content": text}]})
    loaded = ConversationRepository(tmp_path).get_conversation(record.id)
    assert loaded.transcript[0]["content"] == text
    assert loaded.context_snapshot["history"][0]["content"] == text


@pytest.mark.asyncio
async def test_failed_hydration_is_observed_and_closes_loading_projection(monkeypatch):
    context = SimpleNamespace(load_snapshot_partial=lambda snapshot, **kwargs: [{"role": "user", "content": "old"}])
    repository = SimpleNamespace(get_conversation=lambda owner: SimpleNamespace(revision=1))
    runtime = ConversationRuntime(conversation_repo=repository, context_builder=context, build_summary_from_transcript=lambda *args: "")
    runtime._restore_plan_snapshot = lambda owner, snapshot: snapshot
    runtime.active_conversation_id = "conv_probe"
    notified = []
    async def complete(owner):
        notified.append(owner)
    def fail(history):
        raise ValueError("decode failed")
    monkeypatch.setattr(ContextBuilder, "deserialize_snapshot_history", fail)
    runtime.load_active_conversation_snapshot("conv_probe", {}, notify=True, on_hydration_complete=complete)
    await asyncio.gather(runtime._hydration_task, return_exceptions=True)
    with pytest.raises(RuntimeError, match="hydration failed"):
        await runtime.wait_for_hydration("conv_probe")
    assert notified == ["conv_probe"]


@pytest.mark.asyncio
async def test_later_success_supersedes_a_failed_partial_projection(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    journal = ExecutionJournal("fixture", base_dir=tmp_path / "journal")
    record = repository.create_conversation()
    early = {"conversation_id": record.id, "assistant_message": {"id": "a1", "role": "assistant", "content": "partial"},
             "context_snapshot": {"history": []}, "expected_revision": record.revision}
    journal.append_lifecycle("conversation_projection_pending", early)
    with monkeypatch.context() as patch:
        def fail(*args):
            raise PermissionError("temporary write failure")
        patch.setattr(repository, "_write_generation", fail)
        with pytest.raises(PermissionError):
            repository.commit_turn_projection(record.id, **{key: value for key, value in early.items() if key != "conversation_id"})
    final = {**early, "assistant_message": {"id": "a1", "role": "assistant", "content": "final", "terminal_status": "completed"}}
    pending = journal.append_lifecycle("conversation_projection_pending", final)
    committed = repository.commit_turn_projection(record.id, **{key: value for key, value in final.items() if key != "conversation_id"})
    journal.append_lifecycle("conversation_projection_committed", {"conversation_id": record.id, "pending_event_id": pending.event_id,
        "conversation_revision": committed.revision, "message_id": "a1"})
    assert journal.pending_conversation_projections() == []
    await _replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    assert repository.get_conversation(record.id).transcript[-1]["content"] == "final"


@pytest.mark.asyncio
async def test_extension_reload_keeps_project_configuration(tmp_path, monkeypatch):
    import backend.ws.agent_runner as module
    class StopProbe(BaseException):
        pass
    class State(dict):
        @property
        def runtime(self):
            return self["runtime"]
    class Source:
        def __init__(self, **kwargs):
            pass
        def fingerprint(self):
            return "new"
        async def load(self, **kwargs):
            return SimpleNamespace(loader=SimpleNamespace(clear_cache=lambda: None))
    registry, config, mcp = object(), object(), object()
    state = State(lock=asyncio.Lock(), runtime=SimpleNamespace(active=True), registry=registry, workspace_key="project", fingerprint="old")
    captured = {}
    def build(owner, **kwargs):
        captured.update(kwargs)
        raise StopProbe()
    host = SimpleNamespace(conversation_repo=SimpleNamespace(get_conversation=lambda owner: SimpleNamespace(id=owner)),
        _extension_runtime_state=lambda owner: state, _extension_workspace_key=lambda path: "project",
        _conversation_tool_registries={"conv_probe": (1, "project", registry)}, _on_model_runtime_changed=lambda *args: None,
        _build_conversation_tool_registry=build, _mcp_manager_for_workspace=lambda path: mcp, session_id="fixture")
    monkeypatch.setattr(module, "ExtensionCapabilitySource", Source)
    monkeypatch.setattr(module, "is_workspace_trusted", lambda path: True)
    monkeypatch.setattr(module, "load_config", lambda **kwargs: config)
    with pytest.raises(StopProbe):
        await SessionAgentRunnerMixin._ensure_lifecycle_runtime(host, conversation_id="conv_probe", workspace_root=tmp_path, tool_registry=registry, force_reload=True)
    assert captured == {"workspace_root": tmp_path, "config": config, "mcp_manager": mcp}


def queue_manager(queue):
    manager = SessionRunManager.__new__(SessionRunManager)
    manager._durable_queue = queue
    manager._user_message_queues = {}
    manager._inflight_user_messages = {}
    manager._durable_turn_inputs = {}
    manager._turn_input_queues = {"conv_probe": TurnInputQueue()}
    manager._turn_input_queues["conv_probe"].begin_turn("r1")
    return manager


def test_failed_steer_write_is_not_published_or_saved_later(tmp_path, monkeypatch):
    queue = DurableUserMessageQueue(session_id="fixture", root_dir=tmp_path)
    try:
        queue.save({}, {}, {})
        manager = queue_manager(queue)
        command = UserCommand("user_message", {"content": "steer once", "assistant_message_id": "a1", "user_message_id": "u1"})
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise PermissionError("temporary denial")
            patch.setattr("backend.ws.durable_user_queue.atomic_write_text", fail)
            with pytest.raises(PermissionError):
                manager.enqueue_user_message_as_steer("conv_probe", command, target_message_id="active")
        assert manager.pending_turn_input_snapshot() == []
        manager._persist_user_queues()
        assert json.loads(queue.path.read_text())["turn_inputs"] == {}
    finally:
        queue.close()


def test_transient_queue_read_denial_keeps_the_canonical_file(tmp_path, monkeypatch):
    queue = DurableUserMessageQueue(session_id="fixture", root_dir=tmp_path)
    try:
        queue.save({"conv_probe": [UserCommand("user_message", {"content": "saved"})]}, {}, {})
        original = Path.read_text
        def fail(path, *args, **kwargs):
            if path == queue.path:
                raise PermissionError("temporary denial")
            return original(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", fail)
            with pytest.raises(PermissionError):
                queue.load()
        assert queue.path.exists()
        assert len(queue.pending_user_messages("conv_probe")) == 1
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_automatic_compaction_rebases_surviving_admissions(tmp_path):
    repository = ConversationRepository(tmp_path)
    history = [{"role": "user", "content": str(index)} for index in range(6)]
    before = {"history": history, "turn_admissions": {"u5": {"history_start": 5, "history_end": 6}}}
    after = {"history": [{"role": "user", "content": "summary"}, *history[4:]]}
    record = repository.create_conversation(context_snapshot=before)
    await _commit_automatic_compaction(repository, conversation_id=record.id,
        context_builder=SimpleNamespace(export_snapshot=lambda: copy.deepcopy(after)), summary="compacted")
    assert repository.get_conversation(record.id).context_snapshot["turn_admissions"]["u5"] == {"history_start": 2, "history_end": 3}


class ApprovalSession(SessionApprovalRuntimeMixin):
    def __init__(self):
        self.session_id = "fixture"
        self.active_conversation_id = "conv_probe"
        self.turn_wait_state = TurnWaitState()
        self.approval_diff_cache = {}
        self.run_manager = SimpleNamespace(run_tasks={})
        self.sent = []
    async def send_payload(self, payload, **kwargs):
        self.sent.append(payload)
        return True


def approval_payload():
    return {"type": "control_request", "request_id": "tool1", "conversation_id": "conv_probe", "turn_id": "r1", "message_id": "a1",
        "request": {"subtype": "can_use_tool", "tool_name": "run_command", "input": {"command": "echo fixture"}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_fast_approval_paths_clear_replay_state(cached):
    session = ApprovalSession()
    payload = approval_payload()
    session.turn_wait_state.pending_approval_payloads["tool1"] = payload
    if cached:
        session._mark_session_approved("run_command", payload["request"]["input"], payload=payload)
    else:
        session._resolve_pending_approval("tool1", {"action": "approve", "remember_for_session": True})
    assert (await session.approval_handler("tool1"))["action"] == "approve"
    assert session.turn_wait_state.waiter_ids() == set()
    await session.reemit_pending_state()
    assert session.sent == []
    assert session._is_session_approved("run_command", payload["request"]["input"], payload=payload)


@pytest.mark.asyncio
async def test_error_control_response_retains_its_owner():
    session = ApprovalSession()
    payload = approval_payload()
    session.turn_wait_state.pending_approval_payloads["tool1"] = payload
    future = asyncio.get_running_loop().create_future()
    session.turn_wait_state.register_waiter("tool1", future)
    dispatcher = SessionCommandDispatcher.__new__(SessionCommandDispatcher)
    dispatcher._session = session
    data = {key: payload[key] for key in ("request_id", "conversation_id", "turn_id", "message_id")}
    data["response"] = {"request_id": "tool1", "subtype": "error", "error": "widget failed"}
    await dispatcher._handle_control_response(UserCommand("control_response", data))
    assert future.done()
    assert future.result()["action"] == "reject"


@pytest.mark.asyncio
async def test_priority_control_can_release_saturated_normal_command_slots():
    dispatcher = SessionCommandDispatcher.__new__(SessionCommandDispatcher)
    dispatcher._command_semaphore = asyncio.Semaphore(20)
    dispatcher._command_tasks = set()
    dispatcher._session = SimpleNamespace(event_outbox=SimpleNamespace(bind_client_command=lambda *args: nullcontext()))
    lifecycle, approved = asyncio.Lock(), asyncio.Event()
    async def handle(command, **kwargs):
        if command.type == "control_response":
            approved.set()
            return
        async with lifecycle:
            if command.type == "terminal.exec":
                await approved.wait()
    dispatcher._handle_command = handle
    dispatcher._schedule_transient_client_command(UserCommand("terminal.exec", {}), 1)
    for _ in range(19):
        dispatcher._schedule_transient_client_command(UserCommand("terminal.resize", {}), 1)
    await asyncio.sleep(0)
    dispatcher._schedule_transient_client_command(UserCommand("control_response", {}), 1)
    try:
        await asyncio.wait_for(approved.wait(), timeout=1)
    finally:
        approved.set()
        await asyncio.gather(*tuple(dispatcher.command_tasks))


@pytest.mark.asyncio
async def test_start_boundary_assigns_identity_for_non_transport_prompts():
    captured, cleanup = [], []
    async def run(content, *, metadata, **kwargs):
        captured.append(metadata)
        metadata["_turn_admission_future"].set_result(None)
    def create(name, coroutine):
        return SimpleNamespace(id="task1", task=asyncio.create_task(coroutine))
    host = SimpleNamespace(run_manager=SimpleNamespace(turn_input_queue=lambda owner: TurnInputQueue(), is_delivery_complete=lambda *args: True),
        event_outbox=SimpleNamespace(bind_connection_generation=lambda value: nullcontext()), task_manager=SimpleNamespace(create=create),
        _run_agent=run, _register_agent_run=lambda **kwargs: None, _cleanup_agent_run=lambda **kwargs: None,
        command_dispatcher=SimpleNamespace(track_command_task=cleanup.append))
    await WebSocketSession.start_agent_run(host, "authorized PR follow-up", conversation_id="conv_probe")
    await asyncio.gather(*cleanup)
    assert captured[0]["user_message_id"].startswith("user_")


@pytest.mark.asyncio
async def test_rewind_refuses_a_foreign_active_turn(tmp_path):
    owner = "conv_rewind_probe"
    record = CheckpointRecord(id="chk_probe", conversation_id=owner, session_id="fixture", tool_call_id="t1", tool_name="write_file",
        workspace_root=str(tmp_path), paths=[], files=[])
    effects = []
    async def rewind(checkpoint_id):
        effects.append(checkpoint_id)
        return record
    async def send(*args, **kwargs):
        return True
    host = SimpleNamespace(active_conversation_id=owner, session_id="fixture",
        conversation_repo=SimpleNamespace(get_conversation=lambda key: SimpleNamespace(id=owner, workspace_root=str(tmp_path), worktree_path="")),
        session_lifecycle=SimpleNamespace(workspace_root=tmp_path, current_workspace_root=lambda: tmp_path),
        resolve_requested_workspace=lambda value: tmp_path, running_agent_task_for=lambda owner: None,
        checkpoint_manager=SimpleNamespace(get=lambda value: record, rewind=rewind), send_payload=send, send_event=send)
    guards = conversation_query_guards()
    claim = guards.try_start(owner, owner_id="other-session")
    try:
        await handle_checkpoint_rewind(host, {"checkpoint_id": record.id, "conversation_id": owner})
        assert guards.owns(claim)
        assert effects == []
    finally:
        guards.end(claim)
