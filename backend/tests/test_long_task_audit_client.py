import asyncio
import io
import json
import time

import pytest

from scripts.audit_long_task_session import Client


class QueueSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.commands = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.incoming.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def send(self, payload):
        await self.commands.put(json.loads(payload))

    async def close(self):
        await self.incoming.put(None)


def running_client():
    client = Client("unused", io.StringIO(), "audit-test-secret")
    client.started = time.monotonic()
    client.ws = QueueSocket()
    client.reader = asyncio.create_task(client.read())
    return client


@pytest.mark.asyncio
async def test_projection_requires_its_actual_command_receipt():
    client = running_client()
    task = asyncio.create_task(client.command("conversation.switch", conversation_id="owner-a"))
    command = await client.ws.commands.get()
    await client.ws.incoming.put({"type": "conversation.switched", "conversation_id": "owner-a", "client_command_id": "older-command"})
    await asyncio.sleep(0.11)
    assert not task.done()
    own = {"type": "conversation.switched", "conversation_id": "owner-a", "client_command_id": command["client_command_id"]}
    await client.ws.incoming.put(own)
    assert await asyncio.wait_for(task, 1) == own
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replay_first", [None, False, True])
async def test_restore_waits_for_the_canonical_owner_and_its_complete_replay(replay_first):
    client = running_client()
    task = asyncio.create_task(client.command("session.restore", last_seq=0, last_conversation_id="owner-a"))
    command = await client.ws.commands.get()
    command_id = command["client_command_id"]
    snapshot = {"type": "session.restored", "client_command_id": command_id, "active_conversation_id": "owner-a",
                "last_seq": 0, "current_seq": int(replay_first is not None), "replayed_events": int(replay_first is not None),
                "cursor_reset": False, "conversation_switched_follows": True}
    switched = {"type": "conversation.switched", "client_command_id": command_id, "conversation_id": "owner-a", "is_hydrating": True}
    replay = {"type": "session.replay", "client_command_id": command_id, "last_seq": 0, "current_seq": 1, "replayed_events": 1,
              "events": [{"type": "done", "conversation_id": "owner-a", "message_id": "answer", "status": "completed", "seq": 1, "previous_replay_seq": 0}]}
    await client.ws.incoming.put(snapshot)
    if replay_first is not None:
        await client.ws.incoming.put(replay if replay_first else switched)
    await asyncio.sleep(0.11)
    assert not task.done()
    await client.ws.incoming.put(switched if replay_first is not False else replay)
    assert await asyncio.wait_for(task, 1) == snapshot
    assert client.last_seq == int(replay_first is not None)
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [
    {"type": "session.replay", "last_seq": 0, "current_seq": 0, "replayed_events": 1, "events": []},
    {"type": "session.replay", "last_seq": 0, "current_seq": 0, "replayed_events": 1,
     "events": [{"type": "done", "seq": 0, "previous_replay_seq": 0}]},
])
async def test_replay_rejects_a_wrong_count_or_a_nonadvancing_event(replay):
    client = running_client()
    await client.ws.incoming.put(replay)
    with pytest.raises(AssertionError):
        await client.reader
    assert client.last_seq == 0


@pytest.mark.asyncio
async def test_terminal_keeps_the_owner_fence_and_accepts_a_real_completed_snapshot():
    client = running_client()
    task = asyncio.create_task(client.terminal("owner-a", "answer-a"))
    await client.ws.incoming.put({"type": "done", "conversation_id": "other-owner", "message_id": "answer-a", "status": "completed"})
    await asyncio.sleep(0.11)
    assert not task.done()
    snapshot = {"type": "session.synced", "active_conversation_id": "owner-a", "replayed_events": 0, "last_seq": 0, "current_seq": 0,
                "cursor_reset": False, "messages": [{"id": "answer-a", "terminal_status": "completed", "content": "actual completed answer"}]}
    await client.ws.incoming.put(snapshot)
    assert await asyncio.wait_for(task, 1) == snapshot
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replay_first", [False, True])
@pytest.mark.parametrize("live_already_replayed", [False, True])
async def test_early_live_622_waits_for_restore_owner_and_replay_without_rebasing(replay_first, live_already_replayed):
    client = running_client()
    client.last_seq = 334
    task = asyncio.create_task(client.command("session.restore", last_seq=334, last_conversation_id="owner-b"))
    command = await client.ws.commands.get()
    command_id = command["client_command_id"]
    early = {"type": "subagent.progress", "conversation_id": "owner-a", "seq": 622,
        "previous_replay_seq": 618, "event_id": "durable-622", "subagent_id": "real-child"}
    following = {"type": "tool_result", "conversation_id": "owner-a", "seq": 623,
        "previous_replay_seq": 622, "event_id": "durable-623", "tool_call_id": "original-call"}
    for event in ({"type": "mcp_status", "seq": 619}, {"type": "llm.model.updated", "seq": 620},
                  {"type": "client.command.ack", "seq": 621, "client_command_id": command_id}, early, following):
        await client.ws.incoming.put(event)
    await asyncio.sleep(0)
    assert client.last_seq == 334 and not client.reader.done()
    assert not any(event.get("seq") in {622, 623} for event in client.events)
    stale = {"type": "session.restored", "client_command_id": "previous-restore", "last_seq": 334,
        "current_seq": 900, "replayed_events": 0, "conversation_switched_follows": False}
    await client.ws.incoming.put(stale)
    await asyncio.sleep(0)
    assert client.last_seq == 334 and client._recovering and stale not in client.events
    replayed = [{"type": "runtime.span", "conversation_id": "owner-a", "seq": 618,
        "previous_replay_seq": 334, "event_id": "durable-618"}]
    if live_already_replayed:
        replayed.append(dict(early))
    current_seq = replayed[-1]["seq"]
    snapshot = {"type": "session.restored", "client_command_id": command_id, "active_conversation_id": "owner-b",
        "last_seq": 334, "current_seq": current_seq, "replayed_events": len(replayed),
        "cursor_reset": False, "conversation_switched_follows": True}
    switched = {"type": "conversation.switched", "client_command_id": command_id, "conversation_id": "owner-b", "is_hydrating": True}
    replay = {"type": "session.replay", "client_command_id": command_id, "last_seq": 334,
        "current_seq": current_seq, "replayed_events": len(replayed), "events": replayed}
    await client.ws.incoming.put(snapshot)
    await client.ws.incoming.put(replay if replay_first else switched)
    await asyncio.sleep(0)
    assert client._recovering and not task.done()
    assert not any(event.get("seq") == 623 for event in client.events)
    await client.ws.incoming.put(switched if replay_first else replay)
    assert await asyncio.wait_for(task, 1) == snapshot
    delivered = [event for event in client.events if "previous_replay_seq" in event]
    assert [event["seq"] for event in delivered] == [618, 622, 623]
    assert next(event for event in delivered if event["seq"] == 622) == early
    assert next(event for event in delivered if event["seq"] == 623) == following
    assert client.last_seq == 623 and not client._recovering and not client._recovery_events
    assert all(event["previous_replay_seq"] == previous for event, previous in zip(delivered, [334, 618, 622]))
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["replay_gap", "buffer_gap", "event_id_reused"])
async def test_recovery_keeps_real_gaps_and_reused_event_identity_as_errors(fault):
    client = running_client()
    client.last_seq = 334
    task = asyncio.create_task(client.command("session.restore", last_seq=334))
    command = await client.ws.commands.get()
    command_id = command["client_command_id"]
    early = {"type": "subagent.progress", "conversation_id": "owner-a", "seq": 622,
        "previous_replay_seq": 617 if fault == "buffer_gap" else 618,
        "event_id": "durable-618" if fault == "event_id_reused" else "durable-622"}
    await client.ws.incoming.put(early)
    await client.ws.incoming.put({"type": "session.restored", "client_command_id": command_id,
        "last_seq": 334, "current_seq": 618, "replayed_events": 1, "conversation_switched_follows": False})
    await client.ws.incoming.put({"type": "session.replay", "client_command_id": command_id,
        "last_seq": 334, "current_seq": 618, "replayed_events": 1, "events": [{
            "type": "runtime.span", "conversation_id": "owner-a", "seq": 618,
            "previous_replay_seq": 333 if fault == "replay_gap" else 334, "event_id": "durable-618"}]})
    with pytest.raises(AssertionError):
        await asyncio.wait_for(task, 1)
    assert client.last_seq == (334 if fault == "replay_gap" else 618)
    assert not any(event.get("seq") == 622 for event in client.events)
    await client.ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_reset", [False, True])
async def test_empty_replay_snapshot_covers_older_buffered_frames_then_releases_live(cursor_reset):
    client = running_client()
    client.last_seq = 999 if cursor_reset else 334
    task = asyncio.create_task(client.command("session.restore", last_seq=client.last_seq))
    command_id = (await client.ws.commands.get())["client_command_id"]
    older = {"type": "tool_call", "seq": 617, "previous_replay_seq": 334, "event_id": "durable-617"}
    live = {"type": "tool_result", "seq": 622, "previous_replay_seq": 618, "event_id": "durable-622"}
    await client.ws.incoming.put(older)
    await client.ws.incoming.put(live)
    snapshot = {"type": "session.restored", "client_command_id": command_id, "active_conversation_id": "owner-a",
        "last_seq": 618 if cursor_reset else 334, "current_seq": 618, "replayed_events": 0,
        "requested_last_seq": 999 if cursor_reset else 334, "cursor_reset": cursor_reset,
        "conversation_switched_follows": True}
    await client.ws.incoming.put(snapshot)
    await asyncio.sleep(0)
    assert client.last_seq == 618 and client._recovering and not task.done()
    await client.ws.incoming.put({"type": "conversation.switched", "client_command_id": command_id,
        "conversation_id": "owner-a", "is_hydrating": True})
    assert await asyncio.wait_for(task, 1) == snapshot
    assert client.last_seq == 622
    assert older not in client.events and client.events.count(live) == 1
    await client.close()


@pytest.mark.asyncio
async def test_restore_rejection_fails_without_admitting_buffered_live_frames():
    client = running_client()
    client.last_seq = 334
    task = asyncio.create_task(client.command("session.restore", last_seq=334))
    command_id = (await client.ws.commands.get())["client_command_id"]
    await client.ws.incoming.put({"type": "subagent.progress", "seq": 622, "previous_replay_seq": 618, "event_id": "durable-622"})
    rejection = {"type": "command.result", "command": "session.restore", "client_command_id": command_id,
        "level": "error", "message": "actual restore failure"}
    await client.ws.incoming.put(rejection)
    with pytest.raises(AssertionError, match="actual restore failure"):
        await asyncio.wait_for(task, 1)
    assert client.last_seq == 334 and not any(event.get("seq") == 622 for event in client.events)
    await client.ws.close()


@pytest.mark.asyncio
async def test_reconnect_arms_the_gate_before_sending_restore(monkeypatch):
    import websockets

    client = running_client()
    client.last_seq = 334
    await client.close()
    socket = QueueSocket()
    async def connect(*args, **kwargs):
        return socket
    monkeypatch.setattr(websockets, "connect", connect)
    await client.connect()
    early = {"type": "subagent.progress", "seq": 622, "previous_replay_seq": 618, "event_id": "durable-622"}
    await socket.incoming.put(early)
    await asyncio.sleep(0)
    assert client.last_seq == 334 and client._recovering and early not in client.events
    task = asyncio.create_task(client.command("session.restore", last_seq=334))
    command_id = (await socket.commands.get())["client_command_id"]
    snapshot = {"type": "session.restored", "client_command_id": command_id, "last_seq": 334,
        "current_seq": 618, "replayed_events": 1, "conversation_switched_follows": False}
    await socket.incoming.put(snapshot)
    await socket.incoming.put({"type": "session.replay", "client_command_id": command_id,
        "last_seq": 334, "current_seq": 618, "replayed_events": 1, "events": [{
            "type": "runtime.span", "seq": 618, "previous_replay_seq": 334, "event_id": "durable-618"}]})
    assert await asyncio.wait_for(task, 1) == snapshot
    assert client.events.count(early) == 1 and client.last_seq == 622
    await client.close()
