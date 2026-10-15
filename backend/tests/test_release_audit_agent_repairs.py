"""Release-audit regressions across agent identity, Stop and durable user input."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.message import UserCommand
from backend.agent.tool_projection import display_summary_for_result
from backend.agent.tool_events import status_for_result
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_control_plane import AgentControlPlane
from backend.tools.base import ToolResult
from backend.tools.subagent_control_tools import TaskStatusTool, TaskStopTool
from backend.tools.swarm_tools import SendMessageTool
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.misc import handle_interrupt_command
from backend.ws.turn_wait_state import TurnWaitState
from backend.tests.test_stop_queue_chain_audit import session


@pytest.fixture
def runtime(tmp_path):
    value = AgentRuntime(metrics_file=tmp_path / 'metrics.jsonl', swarm_store_dir=tmp_path / 'swarm', enable_lease_heartbeat=False)
    yield value
    value.close(release_lease=True)


def context(runtime, run_id, conversation_id='owned-conversation'):
    return ToolExecutionContext(permission=PermissionContext(), conversation_id=conversation_id,
        metadata={'run_id': run_id}, run_context=RunContext(agent_runtime=runtime))


def start_child(runtime, subagent_id, *, parent='turn-one', pending=False):
    return runtime.start_subagent(subagent_id=subagent_id, parent_run_id=parent,
        agent_type='general-purpose', background=True, pending=pending)


def finish_child(runtime, record, status):
    runtime.complete_subagent(record.subagent_id, status,
        agent_path=record.agent_path, mailbox_epoch=record.mailbox_epoch)
    runtime.store_subagent_result(record.subagent_id, status=status, content=f'Actual state: {status}',
        agent_path=record.agent_path, mailbox_epoch=record.mailbox_epoch)


@pytest.mark.asyncio
async def test_later_parent_turn_can_observe_and_stop_its_background_worker(runtime):
    runtime.start_run(run_id='turn-one', conversation_id='owned-conversation')
    record = start_child(runtime, 'persistent-child')
    worker = asyncio.create_task(asyncio.Event().wait())
    runtime.register_subagent_task(record.subagent_id, worker, parent_run_id='turn-one', background=True)
    await asyncio.sleep(0)
    runtime.complete_run('turn-one')
    runtime.start_run(run_id='turn-two', conversation_id='owned-conversation')
    caller = context(runtime, 'turn-two')
    try:
        inspected = await TaskStatusTool().execute({'subagent_id': record.subagent_id}, caller)
        listed = await TaskStatusTool().execute({}, caller)
        assert inspected.status == 'running' and not inspected.is_error
        assert record.subagent_id in listed.content
        stopped = await TaskStopTool().execute({'subagent_id': record.subagent_id}, caller)
        assert stopped.status == 'cancelled' and worker.cancelling() == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        runtime.release_subagent_task(record.subagent_id, expected_task=worker)


@pytest.mark.asyncio
async def test_cross_turn_lifecycle_scope_does_not_cross_conversations_or_child_siblings(runtime):
    runtime.start_run(run_id='turn-one', conversation_id='owned-conversation')
    child = start_child(runtime, 'child')
    sibling = start_child(runtime, 'sibling')
    nested = start_child(runtime, 'nested', parent=child.subagent_id)
    runtime.start_run(run_id='foreign-turn', conversation_id='foreign-conversation')
    foreign = start_child(runtime, 'foreign-child', parent='foreign-turn')
    runtime.start_run(run_id='turn-two', conversation_id='owned-conversation')
    rejected = await TaskStatusTool().execute({'subagent_id': foreign.subagent_id}, context(runtime, 'turn-two'))
    assert rejected.is_error and rejected.status == 'forbidden'
    child_control = AgentControlPlane(context(runtime, child.subagent_id), runtime=runtime)
    assert child_control.resolve_target(nested.subagent_id, scope='lifecycle') is not None
    assert child_control.resolve_target(sibling.subagent_id, scope='lifecycle') is None
    assert child_control.resolve_target(foreign.subagent_id, scope='lifecycle') is None


@pytest.mark.asyncio
async def test_actorless_sdk_context_cannot_observe_or_interrupt_by_conversation_alone(runtime):
    runtime.start_run(run_id="turn-one", conversation_id="owned-conversation")
    child = start_child(runtime, "owned-child")
    caller = ToolExecutionContext(permission=PermissionContext(), conversation_id="owned-conversation",
        run_context=RunContext(agent_runtime=runtime))
    control = AgentControlPlane(caller, runtime=runtime)
    assert not control.has_actor_identity
    assert control.agents_for_scope("lifecycle") == []
    listed = await TaskStatusTool().execute({}, caller)
    inspected = await TaskStatusTool().execute({"subagent_id": child.subagent_id}, caller)
    interrupted = await TaskStopTool().execute({"subagent_id": child.subagent_id}, caller)
    assert child.subagent_id not in listed.content
    assert inspected.is_error and inspected.status == "forbidden"
    assert interrupted.is_error and interrupted.status == "forbidden"
    assert runtime.get_subagent(child.subagent_id).status == "running"


@pytest.mark.asyncio
async def test_mail_sent_to_pending_worker_survives_promotion_in_same_incarnation(runtime):
    runtime.start_run(run_id='turn-one', conversation_id='owned-conversation')
    queued = start_child(runtime, 'pending-child', pending=True)
    result = await SendMessageTool().execute({'recipient': queued.subagent_id, 'message': 'Apply the later instruction.'}, context(runtime, 'turn-one'))
    assert not result.is_error
    promoted = start_child(runtime, queued.subagent_id)
    assert promoted.mailbox_epoch == queued.mailbox_epoch
    messages = runtime.list_swarm_messages(participant_id=promoted.subagent_id,
        mailbox_epoch=promoted.mailbox_epoch, conversation_id='owned-conversation')
    assert [message.content for message in messages] == ['Apply the later instruction.']
    assert messages[0].recipient_mailbox_epoch == promoted.mailbox_epoch


@pytest.mark.parametrize('status', ['completed', 'partial', 'failed', 'cancelled', 'interrupted', 'pending'])
@pytest.mark.asyncio
async def test_task_list_and_batch_keep_exact_uniform_child_status(runtime, status):
    runtime.start_run(run_id='turn-one', conversation_id='owned-conversation')
    records = [start_child(runtime, f'child-{index}', pending=status == 'pending') for index in range(2)]
    if status != 'pending':
        for record in records:
            finish_child(runtime, record, status)
    caller = context(runtime, 'turn-one')
    listed = await TaskStatusTool().execute({}, caller)
    batch = await TaskStatusTool().execute({'subagent_ids': [record.subagent_id for record in records]}, caller)
    assert listed.status == batch.status == status
    assert all(f'[{status}]' in listed.content for _ in records)
    assert f'2 delegated task(s): {status}' == batch.display_summary


@pytest.mark.asyncio
async def test_mixed_terminal_task_results_cannot_claim_completed(runtime):
    runtime.start_run(run_id='turn-one', conversation_id='owned-conversation')
    records = [start_child(runtime, f'child-{index}') for index in range(2)]
    for record, status in zip(records, ['completed', 'cancelled']):
        finish_child(runtime, record, status)
    caller = context(runtime, 'turn-one')
    listed = await TaskStatusTool().execute({}, caller)
    batch = await TaskStatusTool().execute({'subagent_ids': [record.subagent_id for record in records]}, caller)
    assert listed.status == batch.status == 'partial'
    assert batch.display_summary == '2 delegated task(s): 1 completed, 1 cancelled'


def test_partial_apply_patch_result_is_not_labelled_completed():
    summary = display_summary_for_result(ToolCallEvent(id='patch', name='apply_patch', arguments={}),
        ToolResult(content='One file committed and another edit failed.', status='partial'), status='partial')
    assert summary == 'apply_patch'
    assert status_for_result(ToolResult(content='One file committed and another edit failed.', status='partial')) == 'partial'


def test_interrupted_child_state_maps_to_stopped_tool_outcome():
    assert status_for_result(ToolResult(content="The worker was interrupted.", status="interrupted")) == "cancelled"
    # A successfully queued or accepted control operation is finished even
    # though the delegated worker has not started yet.
    assert status_for_result(ToolResult(content="Queued for the worker.", status="pending")) == "success"


def command(index=0):
    return UserCommand(type='user_message', data={'client_command_id': f'cancelled-input-{index}',
        'conversation_id': 'owned-conversation', 'content': f'未准入的原始输入 {index}\nwith attachment'})


def test_cancelled_originals_are_preserved_without_growing_the_hot_queue(tmp_path):
    queue = DurableUserMessageQueue(session_id='isolated-window', root_dir=tmp_path)
    receipts = {}
    try:
        for index in range(32):
            original = command(index)
            queue.persist_client_command(original)
            receipts[index] = queue.cancel_client_command(original, reason='user_interrupted')
        payload = json.loads(queue.path.read_text(encoding='utf-8'))
        assert payload['client_cancelled'] == {} and payload['client_pending'] == []
        assert queue._client_cancelled == {}
        assert len(list(queue._cancelled_receipt_dir.glob('*.json'))) == 32
    finally:
        queue.close()
    cold = DurableUserMessageQueue(session_id='isolated-window', root_dir=tmp_path)
    try:
        for index, receipt in receipts.items():
            assert cold.cancelled_client_command(f'cancelled-input-{index}') == receipt
            assert receipt['command']['data']['content'] == command(index).data['content']
            assert cold.claim_client_command(f'cancelled-input-{index}') is None
        assert cold.pending_client_commands() == []
    finally:
        cold.close()


def test_legacy_cancelled_map_migrates_before_duplicate_pending_recovery(tmp_path):
    queue = DurableUserMessageQueue(session_id='legacy-window', root_dir=tmp_path)
    original = command()
    queue.persist_client_command(original)
    receipt = queue.cancel_client_command(original, reason='user_interrupted')
    payload = json.loads(queue.path.read_text(encoding='utf-8'))
    payload.update(version=6, client_cancelled={'cancelled-input-0': receipt}, client_pending=[receipt['command']])
    queue._cancelled_receipt_path('cancelled-input-0').unlink()
    queue.path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    queue.close()
    cold = DurableUserMessageQueue(session_id='legacy-window', root_dir=tmp_path)
    try:
        assert cold.pending_client_commands() == []
        assert cold.cancelled_client_command('cancelled-input-0') == receipt
        assert json.loads(cold.path.read_text(encoding='utf-8'))['client_cancelled'] == {}
        assert cold.claim_client_command('cancelled-input-0') is None
    finally:
        cold.close()


def test_committed_cancellation_suppresses_replay_when_queue_publish_failed(tmp_path, monkeypatch):
    queue = DurableUserMessageQueue(session_id='crash-window', root_dir=tmp_path)
    original = command()
    queue.persist_client_command(original)
    def full_disk():
        raise OSError('isolated injected full disk at queue publish')
    monkeypatch.setattr(queue, '_write_current_unlocked', full_disk)
    with pytest.raises(OSError, match='full disk'):
        queue.cancel_client_command(original, reason='user_interrupted')
    receipt_bytes = queue._cancelled_receipt_path('cancelled-input-0').read_bytes()
    queue.close()
    cold = DurableUserMessageQueue(session_id='crash-window', root_dir=tmp_path)
    try:
        assert cold.pending_client_commands() == []
        assert cold.claim_client_command('cancelled-input-0') is None
        assert cold.cancelled_client_command('cancelled-input-0')['command']['data']['content'] == original.data['content']
        assert cold._cancelled_receipt_path('cancelled-input-0').read_bytes() == receipt_bytes
    finally:
        cold.close()


def test_cancel_archive_failure_retains_the_original_pending_input(tmp_path, monkeypatch):
    queue = DurableUserMessageQueue(session_id='archive-write-failed', root_dir=tmp_path)
    original = command()
    queue.persist_client_command(original)
    before = queue.path.read_bytes()
    def fail_archive(*args):
        raise OSError('isolated archive write failed')
    monkeypatch.setattr(queue, '_archive_cancelled_receipt_unlocked', fail_archive)
    try:
        with pytest.raises(OSError, match='archive write failed'):
            queue.cancel_client_command(original, reason='user_interrupted')
        assert queue.path.read_bytes() == before
        assert queue.pending_client_commands()[0].data == original.data
    finally:
        queue.close()


@pytest.mark.parametrize("lane", ["queues", "inflight", "turn_inputs", "client_pending", "client_inflight"])
def test_shared_stop_intent_suppresses_all_lanes_before_cold_owner_recovery(tmp_path, monkeypatch, lane):
    queue = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    original, fresh = command(), command(1)
    original.data["attachments"] = [{"name": "原始附件.png", "id": "original-attachment"}]
    queued = {"owned-conversation": [original, fresh]} if lane == "queues" else {}
    inflight = {"owned-conversation": original} if lane == "inflight" else {}
    turn_inputs = {"owned-conversation": [original, fresh]} if lane == "turn_inputs" else {}
    queue.save(queued, inflight, turn_inputs=turn_inputs)
    if lane.startswith("client_"):
        queue.persist_client_command(original)
        queue.persist_client_command(fresh)
        if lane == "client_inflight":
            queue.claim_client_command("cancelled-input-0")
    receipt = queue.prepare_client_cancellation(original, reason="user_interrupted")
    receipt["client_queue_key"] = "original-window"
    def fail_publish():
        raise OSError("isolated shared queue publish failed after durable Stop intent")
    monkeypatch.setattr(queue, "_write_current_unlocked", fail_publish)
    with pytest.raises(OSError, match="after durable Stop intent"):
        queue.cancel_client_command(original, reason="user_interrupted", cancellation=receipt)
    archived = queue._cancelled_receipt_path("cancelled-input-0").read_bytes()
    queue.close()
    cold = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    try:
        queues, inflight = cold.load()
        assert "owned-conversation" not in inflight
        assert all(item.data["client_command_id"] != "cancelled-input-0" for items in queues.values() for item in items)
        assert all(item.data["client_command_id"] != "cancelled-input-0" for item in cold.pending_client_commands())
        assert cold.claim_client_command("cancelled-input-0") is None
        assert cold.cancelled_client_command("cancelled-input-0") == receipt
        assert cold._cancelled_receipt_path("cancelled-input-0").read_bytes() == archived
        if lane in {"queues", "turn_inputs"}:
            assert cold.claim_user_message("owned-conversation").data == fresh.data
        elif lane.startswith("client_"):
            assert cold.claim_client_command("cancelled-input-1").data == fresh.data
        else:
            assert cold.claim_user_message("owned-conversation") is None
    finally:
        cold.close()


def test_stale_save_cannot_reinsert_input_after_stop_and_retains_new_content(tmp_path):
    queue = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    original, fresh = command(), command(1)
    snapshot = {"owned-conversation": [original, fresh]}
    try:
        queue.save(snapshot, {})
        assert queue.claim_user_message("owned-conversation").data == original.data
        queue.cancel_client_command(original, reason="user_interrupted")
        assert queue.settle_user_message("owned-conversation", original, succeeded=False)
        queue.save(snapshot, {})
        assert [item.data for item in queue.pending_user_messages("owned-conversation")] == [fresh.data]
        assert queue.claim_user_message("owned-conversation").data == fresh.data
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_shared_commit_window_mirror_failure_reuses_first_stop_and_prevents_dispatch(session, monkeypatch):
    original = command()
    original.data.update(conversation_id="conv_stop_owner", attachments=[{"id": "owned-original", "name": "保留.png"}])
    client, shared = session.run_manager.durable_client_commands, session.run_manager.durable_queue
    client.persist_client_command(original)
    shared.save({"conv_stop_owner": [original]}, {})
    assert shared.claim_user_message("conv_stop_owner").data == original.data
    mirror = client.cancel_client_command
    def fail_mirror(*args, **kwargs):
        raise OSError("isolated window mirror publish failure")
    monkeypatch.setattr(client, "cancel_client_command", fail_mirror)
    with pytest.raises(OSError, match="window mirror"):
        session.command_dispatcher.record_cancelled_user_message(original, reason="user_interrupted")
    authority = shared.cancelled_client_command("cancelled-input-0", client_queue_key=client.path.stem)
    authority_bytes = shared._cancelled_receipt_path("cancelled-input-0", client.path.stem).read_bytes()
    assert client.cancelled_client_command("cancelled-input-0") is None
    assert shared.claim_user_message("conv_stop_owner") is None
    monkeypatch.setattr(client, "cancel_client_command", mirror)
    session.command_dispatcher.record_cancelled_user_message(original, reason="second-Stop-must-not-replace-first")
    assert client.cancelled_client_command("cancelled-input-0") == authority
    assert authority["reason"] == "user_interrupted"
    assert shared._cancelled_receipt_path("cancelled-input-0", client.path.stem).read_bytes() == authority_bytes
    # The actual durable dispatch entry returns the cancelled receipt before
    # permission admission, model startup, or any user command can run.
    async def must_not_dispatch(*args, **kwargs):
        raise AssertionError("Stopped original must never reach command/provider dispatch")
    monkeypatch.setattr(session.command_dispatcher, "_dispatch_client_command", must_not_dispatch)
    await session.command_dispatcher._run_durable_client_command(client_command_id="cancelled-input-0", connection_generation=1)
    assert session.events[-1].type == "command.result"
    assert session.events[-1].data["data"]["cancelled"] is True
    assert session.started == [] and client.pending_client_commands() == []


@pytest.mark.parametrize("source_marked_migrated", [False, True])
def test_legacy_window_stop_migrates_before_shared_stale_dispatch_recovery(tmp_path, source_marked_migrated):
    client = DurableUserMessageQueue(session_id="legacy-window", root_dir=tmp_path)
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    original, fresh = command(), command(1)
    original.data["attachments"] = [{"id": "original", "name": "不能丢.png"}]
    try:
        shared.save({"owned-conversation": [original, fresh]}, {})
        shared.claim_user_message("owned-conversation")
        receipt = client.cancel_client_command(original, reason="user_interrupted")
        if source_marked_migrated:
            payload = json.loads(shared.path.read_text(encoding="utf-8"))
            payload["migrated_sources"] = [client.path.name]
            shared.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        receipt_bytes = client._cancelled_receipt_path("cancelled-input-0").read_bytes()
    finally:
        client.close()
        shared.close()
    cold_client = DurableUserMessageQueue(session_id="legacy-window", root_dir=tmp_path)
    cold_shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    try:
        assert cold_client.migrate_user_messages_to(cold_shared) == 0
        assert cold_shared.cancelled_client_command("cancelled-input-0", client_queue_key="legacy-window") == receipt
        assert cold_shared.claim_user_message("owned-conversation").data == fresh.data
        assert cold_client._cancelled_receipt_path("cancelled-input-0").read_bytes() == receipt_bytes
        assert cold_shared.settle_user_message("owned-conversation", fresh, succeeded=True)
        assert cold_shared.claim_user_message("owned-conversation") is None
        assert cold_client.migrate_user_messages_to(cold_shared) == 0
        assert cold_shared.cancelled_client_command("cancelled-input-0", client_queue_key="legacy-window") == receipt
    finally:
        cold_client.close()
        cold_shared.close()


def test_manager_upgrade_discovers_legacy_receipt_after_missing_first_queue_publish(tmp_path):
    from types import SimpleNamespace
    from backend.ws.run_manager import SessionRunManager
    base = tmp_path / "data/user-message-queue"
    client = DurableUserMessageQueue(session_id="legacy-window", root_dir=base)
    shared = DurableUserMessageQueue(session_id="shared", root_dir=base / "conversations", shared_inputs=True)
    original, fresh = command(), command(1)
    try:
        shared.save({"owned-conversation": [original, fresh]}, {})
        shared.claim_user_message("owned-conversation")
        receipt = client.cancel_client_command(original, reason="user_interrupted")
        assert not client.path.exists() and client._cancelled_receipt_path("cancelled-input-0").exists()
    finally:
        client.close()
        shared.close()
    manager = SessionRunManager(SimpleNamespace(session_id="new-window",
        conversation_repo=SimpleNamespace(_base_dir=tmp_path / "data/conversations"),
        session_lifecycle=SimpleNamespace(schedule_task_runtime_update=lambda: None)))
    try:
        assert manager.durable_queue.cancelled_client_command("cancelled-input-0", client_queue_key="legacy-window") == receipt
        assert manager.dequeue_user_message("owned-conversation").data == fresh.data
    finally:
        manager._unsubscribe_parent_notifications()
        manager.close_durable_queue()


@pytest.mark.parametrize("same_owner,same_body", [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.asyncio
async def test_two_producers_same_local_id_keep_independent_stop_originals(tmp_path, same_owner, same_body):
    first = DurableUserMessageQueue(session_id="producer-first", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="producer-second", root_dir=tmp_path)
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    first_input, second_input = command(), command()
    first_input.data.update(_client_queue_key=first.path.stem, user_message_id="same-local-user", assistant_message_id="same-local-assistant")
    second_input.data.update(_client_queue_key=second.path.stem, user_message_id="same-local-user", assistant_message_id="same-local-assistant")
    if not same_owner:
        second_input.data["conversation_id"] = "another-conversation"
    if not same_body:
        second_input.data["content"] = "另一producer的独立原文\n保留"
    first_dispatcher = SessionCommandDispatcher(SimpleNamespace(session_id=first.path.stem,
        run_manager=SimpleNamespace(durable_client_commands=first, durable_queue=shared)), root_dir=tmp_path / "dedup")
    second_dispatcher = SessionCommandDispatcher(SimpleNamespace(session_id=second.path.stem,
        run_manager=SimpleNamespace(durable_client_commands=second, durable_queue=shared)), root_dir=tmp_path / "dedup")
    try:
        assert first.persist_client_command(first_input) and second.persist_client_command(second_input)
        shared.save({"owned-conversation": [first_input, second_input]} if same_owner else {
            "owned-conversation": [first_input], "another-conversation": [second_input]}, {})
        first_dispatcher.record_cancelled_user_message(first_input, reason="user_interrupted")
        remaining = [item for items in shared.pending_user_message_snapshot().values() for item in items]
        assert [item.data for item in remaining] == [second_input.data]
        first_record = await first_dispatcher.cancelled_user_message("cancelled-input-0")
        assert first_record["client_queue_key"] == first.path.stem
        assert first_record["command"]["data"] == {key: value for key, value in first_input.data.items() if not key.startswith("_")}
        assert await second_dispatcher.cancelled_user_message("cancelled-input-0") is None
        assert second.pending_client_commands()[0].data["content"] == second_input.data["content"]
        second_dispatcher.record_cancelled_user_message(second_input, reason="independent-second-Stop")
        second_record = await second_dispatcher.cancelled_user_message("cancelled-input-0")
        assert second_record["client_queue_key"] == second.path.stem
        assert second_record["reason"] == "independent-second-Stop" and first_record["reason"] == "user_interrupted"
        assert shared._cancelled_receipt_path("cancelled-input-0", first.path.stem) != shared._cancelled_receipt_path("cancelled-input-0", second.path.stem)
        assert shared.pending_user_message_snapshot() == {}
        assert first.cancelled_client_command("cancelled-input-0") == first_record
        assert second.cancelled_client_command("cancelled-input-0") == second_record
    finally:
        first.close()
        second.close()
        shared.close()
    cold = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    try:
        assert cold.cancelled_client_command("cancelled-input-0", client_queue_key="producer-first") == first_record
        assert cold.cancelled_client_command("cancelled-input-0", client_queue_key="producer-second") == second_record
        assert cold.pending_user_message_snapshot() == {}
    finally:
        cold.close()


@pytest.mark.asyncio
async def test_foreign_origin_queued_stop_does_not_capture_current_window_same_id(session):
    client, shared = session.run_manager.durable_client_commands, session.run_manager.durable_queue
    current, older = command(), command()
    current.data.update(conversation_id="conv_stop_owner", content="当前窗口同id的独立新输入")
    older.data.update(conversation_id="conv_stop_owner", content="旧窗口正在调度的原文", _client_queue_key="closed-old-window")
    client.persist_client_command(current)
    shared.save({"conv_stop_owner": [older]}, {})
    session.command_dispatcher.record_cancelled_user_message(older, reason="user_interrupted")
    record = shared.cancelled_client_command("cancelled-input-0", client_queue_key="closed-old-window")
    assert record["command"]["data"]["content"] == older.data["content"]
    assert client.cancelled_client_command("cancelled-input-0") is None
    assert client.pending_client_commands()[0].data == current.data
    assert await session.command_dispatcher.cancelled_user_message("cancelled-input-0") is None
    await session.command_dispatcher.emit_cancelled_user_message(older)
    assert session.events[-1].data["data"]["cancelled"] is True


def test_ambiguous_legacy_same_id_owner_body_without_origin_is_preserved(tmp_path):
    source = DurableUserMessageQueue(session_id="old-window", root_dir=tmp_path)
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    first, second = command(), command()
    try:
        shared.path.parent.mkdir(parents=True, exist_ok=True)
        shared.path.write_text(json.dumps({"version": 6, "queues": {"owned-conversation": [
            {"type": first.type, "data": first.data}, {"type": second.type, "data": second.data}]},
            "inflight": {}, "turn_inputs": {}, "client_pending": [], "client_inflight": {}, "ownership": {}}), encoding="utf-8")
        receipt = source.prepare_client_cancellation(first, reason="user_interrupted")
        before = shared.path.read_bytes()
        with pytest.raises(ValueError, match="ambiguous original windows"):
            shared.cancel_client_command(first, reason="user_interrupted", cancellation=receipt)
        assert shared.path.read_bytes() == before
        assert not shared._cancelled_receipt_path("cancelled-input-0", source.path.stem).exists()
        assert len(json.loads(shared.path.read_text(encoding="utf-8"))["queues"]["owned-conversation"]) == 2
    finally:
        source.close()
        shared.close()


@pytest.mark.asyncio
async def test_ws_forged_producer_scope_is_overwritten_and_cold_client_claim_rebinds(session, monkeypatch):
    original = command()
    original.data.update(conversation_id="conv_stop_owner", _client_queue_key="forged-other-window", _legacy_user_message_identity=True, _queued_user_message_dispatch=True)
    shared = session.run_manager.durable_queue
    other_record = {"command": {"type": original.type, "data": {key: value for key, value in original.data.items() if not key.startswith("_")}},
        "reason": "other-window-Stop", "cancelled_at": 1.0, "client_queue_key": "forged-other-window"}
    shared.cancel_client_command(original, reason=other_record["reason"], cancellation=other_record)
    acknowledgements, scheduled, dispatched = [], [], []
    receives = 0
    async def receive():
        nonlocal receives
        receives += 1
        if receives == 1:
            return json.dumps({"type": original.type, **original.data}, ensure_ascii=False)
        session.is_connected = False
        return "{}"
    async def send(payload, **kwargs):
        acknowledgements.append(payload)
    async def dispatch(command, **kwargs):
        dispatched.append(dict(command.data))
        return True
    session.is_connected = True
    session.ws = SimpleNamespace(receive_text=receive)
    session.send_payload = send
    monkeypatch.setattr(session.command_dispatcher, "_schedule_durable_client_command",
        lambda command_id, generation, *, command_type: scheduled.append((command_id, command_type)))
    monkeypatch.setattr(session.command_dispatcher, "_handle_command", dispatch)
    await session.command_dispatcher.run(1)
    assert scheduled == [("cancelled-input-0", "user_message")]
    assert acknowledgements[0].get("duplicate") is None
    await session.command_dispatcher._run_durable_client_command(client_command_id="cancelled-input-0", connection_generation=1)
    assert dispatched[0]["_client_queue_key"] == session.run_manager.durable_client_commands.path.stem
    assert "_legacy_user_message_identity" not in dispatched[0]
    assert "_queued_user_message_dispatch" not in dispatched[0]
    assert dispatched[0]["content"] == original.data["content"]
    assert session.started == []


@pytest.mark.asyncio
async def test_raw_transient_input_cannot_claim_internal_queue_dispatch_identity(session, monkeypatch):
    raw = {"type": "user_message", "conversation_id": "conv_stop_owner", "content": "保留正文",
        "_client_queue_key": "forged-other-window", "_legacy_user_message_identity": True,
        "_queued_user_message_dispatch": True}
    receives, captured = 0, []
    async def receive():
        nonlocal receives
        receives += 1
        if receives == 1:
            return json.dumps(raw, ensure_ascii=False)
        session.is_connected = False
        return "{}"
    session.is_connected = True
    session.ws = SimpleNamespace(receive_text=receive)
    monkeypatch.setattr(session.command_dispatcher, "_schedule_transient_client_command",
        lambda command, generation: captured.append(dict(command.data)))
    await session.command_dispatcher.run(1)
    assert captured == [{key: value for key, value in raw.items() if key != "type" and not key.startswith("_")}]
    assert session.started == []


def test_window_legacy_receipt_without_origin_keeps_original_bytes_on_shared_migration(tmp_path):
    client = DurableUserMessageQueue(session_id="legacy-window", root_dir=tmp_path)
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    original = command()
    try:
        receipt = client.cancel_client_command(original, reason="user_interrupted")
        receipt.pop("client_queue_key")
        archive = client._cancelled_receipt_path("cancelled-input-0")
        archive.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        before = archive.read_bytes()
        assert client.migrate_user_messages_to(shared) == 0
        stored = shared.cancelled_client_command("cancelled-input-0", client_queue_key="legacy-window")
        assert stored == {**receipt, "client_queue_key": "legacy-window"}
        assert archive.read_bytes() == before
        assert client.cancelled_client_command("cancelled-input-0") == receipt
    finally:
        client.close()
        shared.close()


@pytest.mark.parametrize("inflight", [False, True])
def test_schema6_missing_message_identity_marker_survives_cold_queue_rewrites(tmp_path, inflight):
    queue = DurableUserMessageQueue(session_id="old-reader", root_dir=tmp_path)
    original = command()
    try:
        queue.persist_client_command(original)
        if inflight:
            queue.claim_client_command("cancelled-input-0")
        payload = json.loads(queue.path.read_text(encoding="utf-8"))
        payload["version"] = 6
        queue.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    finally:
        queue.close()
    cold = DurableUserMessageQueue(session_id="old-reader", root_dir=tmp_path)
    try:
        loaded = cold.claim_client_command("cancelled-input-0")
        assert loaded.data["_legacy_user_message_identity"] is True
        assert loaded.data["content"] == original.data["content"]
        assert cold.release_client_command("cancelled-input-0")
        rewritten = json.loads(cold.path.read_text(encoding="utf-8"))
        assert rewritten["version"] == 7
        assert rewritten["client_pending"][0]["data"]["_legacy_user_message_identity"] is True
    finally:
        cold.close()
    again = DurableUserMessageQueue(session_id="old-reader", root_dir=tmp_path)
    try:
        assert again.claim_client_command("cancelled-input-0").data["_legacy_user_message_identity"] is True
    finally:
        again.close()


def test_legacy_scope_candidates_use_saved_transport_and_transcript_fields():
    original = command()
    original.data.update(user_message_id="one-user", assistant_message_id="one-assistant", window_owner_id="owner-first")
    same = UserCommand(type=original.type, data=dict(original.data))
    other = UserCommand(type=original.type, data={**original.data, "window_owner_id": "owner-second"})
    assert DurableUserMessageQueue.legacy_input_origins(original, candidates=[("first", same), ("first", same), ("second", other)]) == {"first"}
    assert DurableUserMessageQueue.legacy_input_origins(original, candidates=[("first", same), ("second", same)]) == {"first", "second"}


def test_one_stop_receipt_does_not_guess_between_two_legacy_accepted_producers(tmp_path):
    first = DurableUserMessageQueue(session_id="producer-first", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="producer-second", root_dir=tmp_path)
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    original = command()
    try:
        first.persist_client_command(original)
        second.persist_client_command(original)
        receipt = first.cancel_client_command(original, reason="user_interrupted")
        shared.path.parent.mkdir(parents=True, exist_ok=True)
        # One unscoped old shared command alone is still ambiguous: the second
        # physical window accepted the same input and has no Stop receipt.
        payload = {"version": 6, "queues": {"owned-conversation": [receipt["command"]]},
            "inflight": {}, "turn_inputs": {}, "client_pending": [], "client_inflight": {}, "ownership": {}}
        shared.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = shared.path.read_bytes()
        with pytest.raises(ValueError, match="ambiguous original windows"):
            first.migrate_user_messages_to(shared)
        assert shared.path.read_bytes() == before
        assert not shared._cancelled_receipt_path("cancelled-input-0", first.path.stem).exists()
        assert second.pending_client_commands()[0].data == original.data
    finally:
        first.close()
        second.close()
        shared.close()


def test_one_legacy_input_mirrored_across_lanes_uses_one_proven_origin(tmp_path):
    client = DurableUserMessageQueue(session_id="one-proven-window", root_dir=tmp_path)
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "conversations", shared_inputs=True)
    original, fresh = command(), command(1)
    try:
        client.persist_client_command(original)
        client.persist_client_command(fresh)
        receipt = client.cancel_client_command(original, reason="user_interrupted")
        fresh_payload = {"type": fresh.type, "data": fresh.data}
        payload = {"version": 6, "queues": {"owned-conversation": [receipt["command"], fresh_payload]},
            "inflight": {"owned-conversation": receipt["command"]},
            "turn_inputs": {"owned-conversation": [receipt["command"]]},
            "client_pending": [], "client_inflight": {}, "ownership": {}}
        shared.path.parent.mkdir(parents=True, exist_ok=True)
        shared.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        assert client.migrate_user_messages_to(shared) == 0
        pending = shared.pending_user_messages("owned-conversation")
        assert len(pending) == 1 and pending[0].data["client_command_id"] == "cancelled-input-1"
        assert pending[0].data["_client_queue_key"] == client.path.stem
        assert shared.claim_user_message("owned-conversation").data["content"] == fresh.data["content"]
    finally:
        client.close()
        shared.close()


def test_malformed_legacy_cancellation_preserves_original_bytes_with_error_evidence(tmp_path):
    queue = DurableUserMessageQueue(session_id='invalid-receipt', root_dir=tmp_path)
    original = command()
    queue.persist_client_command(original)
    payload = json.loads(queue.path.read_text(encoding='utf-8'))
    payload['client_cancelled'] = ['wrong-shape']
    malformed = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    queue.path.write_bytes(malformed)
    try:
        queue.load()
        assert queue.load_error['reason'] == 'unexpected_payload'
        assert Path(queue.load_error['quarantined_to']).read_bytes() == malformed
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_stop_of_local_run_without_client_id_before_runner_first_step(session):
    entered, stops = [], []
    async def runner(*args, **kwargs):
        entered.append(True)
        await asyncio.Event().wait()
    def create(kind, coro):
        from types import SimpleNamespace
        stops.append(asyncio.create_task(handle_interrupt_command(session,
            {'conversation_id': 'conv_stop_owner', 'message_id': 'assistant_input'})))
        return SimpleNamespace(id='local-run', task=asyncio.create_task(coro))
    session._run_agent = runner
    session.task_manager.create = create
    result = await WebSocketSession.start_agent_run(session, 'local resume',
        conversation_id='conv_stop_owner', metadata={'assistant_message_id': 'assistant_input'})
    await asyncio.gather(*stops)
    assert result == 'local-run' and entered == []
    assert [(event.type, event.data.get('status')) for event in session.events if event.type == 'done'] == [('done', 'cancelled')]
    assert not [event for event in session.events if event.type == 'command.result']


@pytest.mark.asyncio
async def test_stop_signals_execution_and_keeps_loop_responsive_during_real_foreign_file_lock(session, tmp_path):
    from backend.atomic_io import _MUTATION_LOCK_ROOT, canonical_file_path_key
    import hashlib
    queue = session.run_manager.durable_client_commands
    original = command()
    original.data["conversation_id"] = "conv_stop_owner"
    queue.persist_client_command(original)
    lockname = hashlib.sha256(canonical_file_path_key(queue.path).encode('utf-8')).hexdigest() + '.lock'
    child_source = "from filelock import FileLock; import sys; lock=FileLock(sys.argv[1]); lock.acquire(); print('LOCKED', flush=True); sys.stdin.readline(); lock.release()"
    process = subprocess.Popen([sys.executable, '-u', '-c', child_source, str(_MUTATION_LOCK_ROOT / lockname)],
        cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stopping = None
    worker = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10) == 'LOCKED\n'
        stopped = asyncio.Event()
        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        worker = asyncio.create_task(work())
        await asyncio.sleep(0)
        run_context = RunContext(record_user_cancelled=lambda reason: queue.cancel_client_command(original, reason=reason))
        session.run_manager.register(conversation_id='conv_stop_owner', task=worker, task_id='lock-owned-run',
            cancel_event=asyncio.Event(), active_conversation_id='conv_stop_owner', run_context=run_context)
        session._conversation_streams['conv_stop_owner'] = {'message_id': 'assistant_input'}
        stopping = asyncio.create_task(session.run_manager.cancel(conversation_id='conv_stop_owner', reason='user_interrupted'))
        await asyncio.wait_for(stopped.wait(), 3)
        assert worker.cancelled() and not stopping.done()
        assert run_context.user_cancel_persistence is not None and not run_context.user_cancel_persistence.done()
        heartbeat = []
        for index in range(4):
            await asyncio.sleep(0)
            heartbeat.append(index)
        assert heartbeat == [0, 1, 2, 3]
        process.stdin.write('\n')
        process.stdin.flush()
        await asyncio.wait_for(stopping, 10)
        assert await asyncio.to_thread(process.wait, 5) == 0
        assert queue.cancelled_client_command('cancelled-input-0')['command']['data']['content'] == original.data['content']
    finally:
        if process.poll() is None:
            process.stdin.write('\n')
            process.stdin.flush()
            await asyncio.to_thread(process.wait, 5)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.asyncio
async def test_stop_write_error_is_visible_after_execution_has_actually_stopped(session, monkeypatch):
    queue = session.run_manager.durable_client_commands
    original = command()
    original.data["conversation_id"] = "conv_stop_owner"
    queue.persist_client_command(original)
    async def work():
        await asyncio.Event().wait()
    worker = asyncio.create_task(work())
    await asyncio.sleep(0)
    def cannot_save(reason):
        raise OSError('isolated Stop disk failure')
    run_context = RunContext(record_user_cancelled=cannot_save)
    cancel_event = asyncio.Event()
    session.run_manager.register(conversation_id='conv_stop_owner', task=worker, task_id='write-failed-run',
        cancel_event=cancel_event, active_conversation_id='conv_stop_owner', run_context=run_context)
    session._conversation_streams['conv_stop_owner'] = {'message_id': 'assistant_input'}
    with pytest.raises(ExceptionGroup, match='durable input state') as caught:
        await session.run_manager.cancel(conversation_id='conv_stop_owner', reason='user_interrupted')
    assert worker.cancelled()
    assert 'isolated Stop disk failure' in str(caught.value.exceptions[0])
    assert not [event for event in session.events if event.type == 'command.result']
    assert queue.pending_client_commands()[0].data['content'] == original.data['content']
    session._cleanup_agent_run(conversation_id='conv_stop_owner', task=worker,
        task_id='write-failed-run', cancel_event=cancel_event)
    assert queue.pending_client_commands()[0].data['content'] == original.data['content']


class ApprovalHost(SessionApprovalRuntimeMixin):
    def __init__(self):
        self.turn_wait_state = TurnWaitState()
        self.events = []
        self.approval_diff_cache = {}
    async def send_event(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_concurrent_approval_cancel_owners_share_one_request_claim():
    host = ApprovalHost()
    request_id = 'concurrent-approval'
    host.turn_wait_state.pending_approval_payloads[request_id] = {'conversation_id': 'owned',
        'type': 'control_request', 'request': {'subtype': 'can_use_tool', 'tool_name': 'write_file', 'input': {}}}
    waiting = asyncio.create_task(host.approval_handler(request_id))
    await asyncio.sleep(0)
    notifying = host._cancel_pending_approvals(conversation_id='owned', reason='user_interrupted')
    await asyncio.gather(waiting, notifying, return_exceptions=True)
    assert len(host.events) == 1 and host.events[0].type == 'approval.cancelled'
    assert host.events[0].data['request_ids'] == [request_id]
    assert host.turn_wait_state.approval_notification_claims == {}
    assert not hasattr(host, '_settled_approval_notifications')


@pytest.mark.asyncio
async def test_approval_notification_retention_tracks_live_requests_only():
    host = ApprovalHost()
    for index in range(128):
        request_id = f'approval-{index}'
        future = asyncio.get_running_loop().create_future()
        host.turn_wait_state.register_waiter(request_id, future)
        host.turn_wait_state.pending_approval_payloads[request_id] = {'conversation_id': 'owned'}
        await host.emit_approval_cancelled_once([request_id], reason='timeout', conversation_id='owned')
        await host.emit_approval_cancelled_once([request_id], reason='duplicate', conversation_id='owned')
        future.cancel()
        host.turn_wait_state.remove_waiter(request_id)
        host.turn_wait_state.pending_approval_payloads.pop(request_id)
        assert host.turn_wait_state.approval_notification_claims == {}
    assert len(host.events) == 128
    assert not hasattr(host, '_settled_approval_notifications')


@pytest.mark.asyncio
async def test_queue_owner_lease_is_process_owned_across_worker_threads(tmp_path):
    queue = DurableUserMessageQueue(session_id="worker-owned-queue", root_dir=tmp_path)
    original = command()
    try:
        assert await asyncio.to_thread(queue.persist_client_command, original)
        receipt = await asyncio.to_thread(queue.cancel_client_command, original, reason="user_interrupted")
        assert queue.cancelled_client_command("cancelled-input-0") == receipt
        await asyncio.to_thread(queue.close)
        with pytest.raises(RuntimeError, match="lease is closed"):
            queue.pending_client_commands()
    finally:
        queue.close()
    cold = DurableUserMessageQueue(session_id="worker-owned-queue", root_dir=tmp_path)
    try:
        assert await asyncio.to_thread(cold.cancelled_client_command, "cancelled-input-0") == receipt
        assert cold.pending_client_commands() == []
    finally:
        cold.close()


@pytest.mark.parametrize("write_fails", [False, True])
@pytest.mark.asyncio
async def test_stop_caller_cancel_and_shutdown_keep_one_durable_writer_owner(session, write_fails):
    import threading
    entered = threading.Event()
    release = threading.Event()
    writes = []
    queue = session.run_manager.durable_client_commands
    original = command()
    original.data["conversation_id"] = "conv_stop_owner"
    queue.persist_client_command(original)
    async def work():
        await asyncio.Event().wait()
    worker = asyncio.create_task(work())
    await asyncio.sleep(0)
    def save(reason):
        writes.append(reason)
        entered.set()
        release.wait()
        if write_fails:
            raise OSError("isolated shared writer failure")
        queue.cancel_client_command(original, reason=reason)
    run_context = RunContext(record_user_cancelled=save)
    cancel_event = asyncio.Event()
    session.run_manager.register(conversation_id="conv_stop_owner", task=worker, task_id="shared-writer-run",
        cancel_event=cancel_event, active_conversation_id="conv_stop_owner", run_context=run_context)
    session._conversation_streams["conv_stop_owner"] = {"message_id": "assistant_input"}
    stopping = asyncio.create_task(session.run_manager.cancel(conversation_id="conv_stop_owner", reason="user_interrupted"))
    shutdown = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
        persistence = run_context.user_cancel_persistence
        assert persistence is not None and worker.cancelled()
        stopping.cancel()
        shutdown = asyncio.create_task(session.run_manager.cancel(conversation_id="conv_stop_owner", reason="session_shutdown"))
        await asyncio.sleep(0)
        assert not persistence.cancelled() and not persistence.done()
        assert run_context.cancel_reason == "user_interrupted"
        release.set()
        results = await asyncio.gather(stopping, shutdown, return_exceptions=True)
        assert writes == ["user_interrupted"]
        assert not persistence.cancelled()
        if write_fails:
            assert all(isinstance(result, ExceptionGroup) for result in results)
            assert all("isolated shared writer failure" in str(result.exceptions[0]) for result in results)
            assert queue.pending_client_commands()[0].data["content"] == original.data["content"]
        else:
            assert results == [True, True]
            assert queue.cancelled_client_command("cancelled-input-0")["command"]["data"]["content"] == original.data["content"]
    finally:
        release.set()
        await asyncio.gather(stopping, *([shutdown] if shutdown is not None else []), return_exceptions=True)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        session._cleanup_agent_run(conversation_id="conv_stop_owner", task=worker,
            task_id="shared-writer-run", cancel_event=cancel_event)
    if write_fails:
        assert queue.pending_client_commands()[0].data["content"] == original.data["content"]
