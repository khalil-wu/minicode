"""Read refreshes never put disk admission ahead of the next user input."""
import asyncio
import json

import pytest

from backend.agent.message import AgentEvent
from backend.tests.test_control_decision_receipts import session, ReceiptSocket
from backend.ws.command_dispatcher import READ_ONLY_REFRESH_COMMAND_TYPES


async def close_reader(reader, dispatcher):
    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
    await asyncio.gather(*tuple(dispatcher.command_tasks), *tuple(dispatcher.execution_command_tasks), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", sorted(READ_ONLY_REFRESH_COMMAND_TYPES))
async def test_refresh_keeps_retry_ownership_and_user_input_reaches_disk_while_read_waits(session, monkeypatch, command_type):
    dispatcher = session.command_dispatcher
    session._conversation_lifecycle_lock = asyncio.Lock()
    queue = session.run_manager.durable_client_commands
    persist = queue.persist_client_command
    persisted = []
    entered, release, input_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owners = []

    def persisted_command(command):
        assert command.type not in READ_ONLY_REFRESH_COMMAND_TYPES
        persisted.append(command.type)
        return persist(command)

    async def handle(command):
        if command.type == command_type:
            owners.append(command.data.get("owner_conversation_id"))
            entered.set()
            await release.wait()
            await session.send_event(AgentEvent.command_result(command_type, "current projection", level="success"))
        else:
            input_seen.set()

    monkeypatch.setattr(queue, "persist_client_command", persisted_command)
    monkeypatch.setattr(dispatcher, "_handle_command_inner", handle)
    reader = asyncio.create_task(dispatcher.run(session.connection_generation))
    try:
        session.ws.inbound.put_nowait(json.dumps({"type": command_type, "client_command_id": "read-1"}))
        await asyncio.wait_for(entered.wait(), 2)
        assert not any(item["type"] == "client.command.ack" for item in session.ws.sent)
        session.ws.inbound.put_nowait(json.dumps({"type": "user_message", "client_command_id": "input-1",
            "conversation_id": "conv_receipts", "content": "Continue"}))
        await asyncio.wait_for(input_seen.wait(), 2)
        assert persisted == ["user_message"]
        assert not queue.has_client_command("read-1")
        release.set()
        await asyncio.gather(*tuple(dispatcher.command_tasks))
        await session.event_outbox.drain_delivery()
        kinds = [item["type"] for item in session.ws.sent if item.get("client_command_id") == "read-1"]
        assert kinds == ["command.result", "client.command.ack"]
        assert owners == ["conv_receipts"]
        assert "read-1" not in dispatcher.recent_client_command_id_set
    finally:
        release.set()
        await close_reader(reader, dispatcher)


@pytest.mark.asyncio
async def test_old_connection_read_does_not_acknowledge_or_publish_to_replacement(session, monkeypatch):
    dispatcher = session.command_dispatcher
    original = session.ws
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(command):
        entered.set()
        await release.wait()
        await session.send_event(AgentEvent.command_result(command.type, "old projection", level="success"))

    monkeypatch.setattr(dispatcher, "_handle_command_inner", handle)
    reader = asyncio.create_task(dispatcher.run(session.connection_generation))
    try:
        original.inbound.put_nowait(json.dumps({"type": "skills.list", "client_command_id": "same-read"}))
        await asyncio.wait_for(entered.wait(), 2)
        replacement = ReceiptSocket()
        session.event_outbox.attach_websocket(replacement)
        release.set()
        await asyncio.gather(*tuple(dispatcher.command_tasks))
        await session.event_outbox.drain_delivery()
        assert not any(item.get("client_command_id") == "same-read" for item in replacement.sent)
    finally:
        release.set()
        await close_reader(reader, dispatcher)
