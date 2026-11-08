from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event
from unittest.mock import Mock

import pytest

from backend.agent.message import UserCommand
from backend.tests.test_control_decision_receipts import session
from backend.ws.durable_user_queue import DurableUserMessageQueue


def command(identity="input", owner="conv_receipts", content="Original input"):
    return UserCommand("user_message", {"client_command_id": identity, "conversation_id": owner,
        "content": content, "user_message_id": identity + "-user", "assistant_message_id": identity + "-assistant",
        "attachments": [{"file_name": "owned.txt", "artifact_id": "artifact"}]})


def test_admission_reads_once_and_returns_the_original_pending_owner(tmp_path, monkeypatch):
    queue = DurableUserMessageQueue(session_id="window", root_dir=tmp_path)
    read = Mock(wraps=queue._read_state_unlocked)
    write = Mock(wraps=queue._write_current_unlocked)
    monkeypatch.setattr(queue, "_read_state_unlocked", read)
    monkeypatch.setattr(queue, "_write_current_unlocked", write)
    original = command()
    try:
        assert queue.admit_client_command(original).status == "accepted"
        assert read.call_count == write.call_count == 1
        read.reset_mock()
        duplicate = queue.admit_client_command(command(owner="other", content="Not the accepted input"), accept_new=False)
        assert duplicate.status == "pending" and duplicate.command == original
        assert read.call_count == 1 and write.call_count == 1
        duplicate.command.data["attachments"][0]["file_name"] = "caller changed"
        assert queue._client_pending[0].data["attachments"][0]["file_name"] == "owned.txt"
        assert queue.claim_client_command("input") == original
        assert queue.complete_client_command("input")
        read.reset_mock()
        assert queue.admit_client_command(original, accept_new=False).status == "completed"
        assert read.call_count == 1
        assert queue.admit_client_command(command("new"), accept_new=False).status == "backlog"
        assert not queue.has_client_command("new")
    finally:
        queue.close()


def test_failed_admission_has_no_acknowledgeable_state_or_partial_pending_record(tmp_path, monkeypatch):
    queue = DurableUserMessageQueue(session_id="window", root_dir=tmp_path)
    original = queue._write_current_unlocked
    try:
        monkeypatch.setattr(queue, "_write_current_unlocked", Mock(side_effect=OSError("durability failure")))
        with pytest.raises(OSError, match="durability failure"):
            queue.admit_client_command(command())
        assert not queue._client_pending
        monkeypatch.setattr(queue, "_write_current_unlocked", original)
        assert not queue.has_client_command("input")
        assert queue.admit_client_command(command()).status == "accepted"
    finally:
        queue.close()


def test_competing_connections_accept_one_original_and_leave_claim_exclusive(tmp_path):
    first = DurableUserMessageQueue(session_id="window", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="window", root_dir=tmp_path)
    barrier = Barrier(2)
    def accept(queue, body):
        barrier.wait(timeout=3)
        return queue.admit_client_command(command(content=body))
    try:
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(accept, first, "first"), pool.submit(accept, second, "second")
            results = [a.result(timeout=5), b.result(timeout=5)]
        assert sorted(result.status for result in results) == ["accepted", "pending"]
        original = next(result.command for result in results if result.status == "pending")
        assert first.claim_client_command("input") == original
        assert second.claim_client_command("input") is None
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_receive_combines_local_receipt_checks_and_preserves_owner_on_duplicate(session, tmp_path, monkeypatch):
    client = session.run_manager.durable_client_commands
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "shared", shared_inputs=True)
    session.run_manager.durable_queue = shared
    reads = Mock(wraps=client._read_state_unlocked)
    monkeypatch.setattr(client, "_read_state_unlocked", reads)
    shared_reads = Mock(wraps=shared._read_state_unlocked)
    monkeypatch.setattr(shared, "_read_state_unlocked", shared_reads)
    dispatches = asyncio.Queue()
    scheduled = Mock(side_effect=lambda *args, **kwargs: dispatches.put_nowait(kwargs))
    monkeypatch.setattr(session.command_dispatcher, "_schedule_durable_client_command", scheduled)
    monkeypatch.setattr(session.command_dispatcher, "_recovered_command_owner_ready", lambda _: True)
    running = asyncio.create_task(session.command_dispatcher.run(session.connection_generation))
    try:
        original = command()
        await session.ws.inbound.put(json.dumps({"type": original.type, **original.data}))
        ack = await asyncio.wait_for(session.ws.acks.get(), 3)
        await asyncio.wait_for(dispatches.get(), 3)
        assert ack.get("accepted") is not False and not ack.get("duplicate")
        assert reads.call_count == shared_reads.call_count == 1
        assert scheduled.call_count == 1
        session.command_dispatcher._max_command_tasks = 0
        duplicate = command(owner="other", content="Changed retry")
        await session.ws.inbound.put(json.dumps({"type": duplicate.type, **duplicate.data}))
        ack = await asyncio.wait_for(session.ws.acks.get(), 3)
        await asyncio.wait_for(dispatches.get(), 3)
        assert ack["duplicate"] is True
        assert scheduled.call_count == 2
        assert scheduled.call_args.kwargs["command"].data["conversation_id"] == original.data["conversation_id"]
        assert scheduled.call_args.kwargs["command"].data["content"] == original.data["content"]
        fresh = command("blocked")
        await session.ws.inbound.put(json.dumps({"type": fresh.type, **fresh.data}))
        ack = await asyncio.wait_for(session.ws.acks.get(), 3)
        assert ack["accepted"] is False and ack["reason"] == "command.backlog"
        assert not client.has_client_command("blocked") and scheduled.call_count == 2
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        shared.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_owner", ["shared", "client"])
async def test_cold_stop_receipt_wins_over_admission_and_is_mirrored(session, tmp_path, monkeypatch, receipt_owner):
    client = session.run_manager.durable_client_commands
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "shared", shared_inputs=True)
    session.run_manager.durable_queue = shared
    original = command()
    stop = client.prepare_client_cancellation(original, reason="user_interrupted", client_queue_key=client.path.stem)
    selected = shared if receipt_owner == "shared" else client
    selected.cancel_client_command(original, reason="user_interrupted", cancellation=stop)
    scheduled = Mock()
    monkeypatch.setattr(session.command_dispatcher, "_schedule_durable_client_command", scheduled)
    running = asyncio.create_task(session.command_dispatcher.run(session.connection_generation))
    try:
        await session.ws.inbound.put(json.dumps({"type": original.type, **original.data}))
        ack = await asyncio.wait_for(session.ws.acks.get(), 3)
        result, _ = await asyncio.wait_for(session.ws.results.get(), 3)
        assert ack["duplicate"] is True
        assert result["data"]["cancelled"] is True and result["data"]["admitted"] is False
        scheduled.assert_not_called()
        assert client.cancelled_client_command("input")["command"] == stop["command"]
        assert shared.cancelled_client_command("input", client_queue_key=client.path.stem)["command"] == stop["command"]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        shared.close()


@pytest.mark.asyncio
async def test_cancelled_receiver_keeps_the_acceptance_writer_owned_and_leaves_replayable_input(session, monkeypatch):
    queue = session.run_manager.durable_client_commands
    entered, release = Event(), Event()
    write = queue._write_current_unlocked
    def held():
        entered.set()
        assert release.wait(5)
        return write()
    monkeypatch.setattr(queue, "_write_current_unlocked", held)
    scheduled = Mock()
    monkeypatch.setattr(session.command_dispatcher, "_schedule_durable_client_command", scheduled)
    running = asyncio.create_task(session.command_dispatcher.run(session.connection_generation))
    try:
        original = command()
        await session.ws.inbound.put(json.dumps({"type": original.type, **original.data}))
        assert await asyncio.to_thread(entered.wait, 3)
        assert session.ws.acks.empty()
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert session.ws.acks.empty()
        scheduled.assert_not_called()
        assert queue.claim_client_command("input").data["content"] == "Original input"
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
