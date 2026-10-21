import asyncio
import io
import json
import time
import subprocess
import sys

import pytest

from scripts.audit_long_task_session import CHILD_PROBE, Client, successful_file_reads


def test_source_snapshot_includes_actual_runtime_resources_and_excludes_python_cache(tmp_path, monkeypatch):
    import scripts.audit_long_task_session as audit
    runtime = tmp_path / "backend"
    (runtime / "resources").mkdir(parents=True)
    (runtime / "__pycache__").mkdir()
    (runtime / "tests").mkdir()
    for path in (runtime / "runtime.py", runtime / "vm.js", runtime / "resources" / "models.json", runtime / "resources" / "native.exe"):
        path.write_bytes(b"frozen runtime input")
    (runtime / "__pycache__" / "runtime.pyc").write_bytes(b"generated cache")
    (runtime / "tests" / "test_fixture.py").write_bytes(b"test only")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    before = audit.backend_source_snapshot()
    assert set(before) == {"backend/runtime.py", "backend/vm.js", "backend/resources/models.json", "backend/resources/native.exe"}
    (runtime / "resources" / "models.json").write_bytes(b"changed model capability")
    assert audit.backend_source_snapshot() != before


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
async def test_terminal_uses_the_explicit_endurance_observation_budget(monkeypatch):
    client = Client("unused", io.StringIO(), "audit-test-secret")
    received = {}
    async def observe(predicate, label, *, timeout):
        received.update(label=label, timeout=timeout)
        return {"type": "done", "conversation_id": "owner", "message_id": "answer", "status": "completed"}
    monkeypatch.setattr(client, "until", observe)
    await client.terminal("owner", "answer", timeout=4800)
    assert received == {"label": "terminal answer", "timeout": 4800}


@pytest.mark.parametrize("correct", [True, False])
def test_child_endurance_probe_checks_actual_business_rows_and_preserves_its_pid(tmp_path, correct):
    solution = ("from decimal import Decimal\n"
        "def reconcile(rows):\n"
        "    unique = {}\n"
        "    for row in rows: unique.setdefault(row['id'], row)\n"
        "    totals = {}\n"
        "    for row in unique.values(): totals[row['account']] = totals.get(row['account'], Decimal(0)) + Decimal(row['amount'])\n"
        "    return {key:format(value,'.2f') for key,value in totals.items()}\n")
    (tmp_path / "solution.py").write_text(solution if correct else "def reconcile(rows): return {}\n", encoding="utf-8")
    (tmp_path / "child_probe.py").write_text(CHILD_PROBE, encoding="utf-8")
    process = subprocess.run([sys.executable, "-X", "utf8", "child_probe.py", "--seconds", "1"], cwd=tmp_path, capture_output=True, text=True)
    if correct:
        assert process.returncode == 0, process.stderr
        result = json.loads((tmp_path / "child-result.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (tmp_path / "child-progress.jsonl").read_text(encoding="utf-8").splitlines()]
        assert result["passed"] and result["elapsed"] >= 1 and result["checked_rows"] >= 300
        assert {row["pid"] for row in rows} == {result["pid"]}
        assert "CHILD_PROBE_OK" in process.stdout
    else:
        assert process.returncode != 0 and "AssertionError" in process.stderr
        assert not (tmp_path / "child-result.json").exists()


def test_endurance_read_evidence_requires_success_and_the_actual_message_owner():
    def call(identity, message, path):
        return {"type": "tool_call", "name": "read_file", "id": identity, "conversation_id": "b", "message_id": message, "args": {"file_path": path}}
    def result(identity, message, failed=False):
        return {"type": "tool_result", "id": identity, "conversation_id": "b", "message_id": message, "is_error": failed}
    events = [call("old", "old", "B_NOTE.txt"), result("old", "old"), call("failed", "new", "progress.jsonl"), result("failed", "new", True)]
    with pytest.raises(AssertionError):
        successful_file_reads(events, conversation_id="b", message_id="new", required_files=("B_NOTE.txt", "progress.jsonl"))
    events += [call("note", "new", "B_NOTE.txt"), result("note", "new"), call("tail", "new", "progress.jsonl"), result("tail", "new")]
    assert successful_file_reads(events, conversation_id="b", message_id="new", required_files=("B_NOTE.txt", "progress.jsonl")) == {"B_NOTE.txt": "note", "progress.jsonl": "tail"}


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
