from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json

import pytest

from backend.agent.execution_journal import ExecutionJournal, ExecutionJournalCorruptionError
from backend.conversations.projection_log import value_change
from backend.tests.test_extension_execution_actions import run_child


def snapshot():
    return {
        "history": [{"role": "user", "content": "original 附件\r\n" * 400}],
        "provider_items": [{"type": "reasoning", "encrypted_content": "opaque+/=="}],
        "extension_state": {"entries": [{"id": str(i), "data": "x" * 1024} for i in range(64)]},
        "persistent_notes": ["preserve this fact"],
    }


def append_snapshot(journal, state, *, owner="conversation", event_id=None):
    payload = {"conversation_id": owner, "context_snapshot": state}
    if event_id:
        return journal.append_once("system", payload, event_id=event_id)
    return journal.append("system", payload)


def test_initial_snapshot_owns_its_serialized_payload_without_recursive_dataclass_export(tmp_path, monkeypatch):
    import backend.agent.execution_journal as module

    journal = ExecutionJournal("serialized-owner", base_dir=tmp_path)
    payload = {"conversation_id": "owner", "context_snapshot": {"history": [
        {"role": "user", "content": "中文\r\nexact input", "provider_items": [], "media": {"items": [1, True, None]}}
        for _ in range(1000)
    ]}}
    expected = deepcopy(payload)
    def no_recursive_export(*args, **kwargs):
        raise AssertionError("A JSON journal payload was copied through asdict")
    with monkeypatch.context() as scoped:
        scoped.setattr(module, "asdict", no_recursive_export)
        event = journal.append_once("system", payload, event_id="initial")
    payload["context_snapshot"]["history"][0]["media"]["items"].append("caller changed")
    assert event.payload == expected
    exported = event.to_dict()
    exported["payload"]["context_snapshot"]["history"][0]["content"] = "export changed"
    assert event.payload == expected
    assert journal.append_once("system", expected, event_id="initial") is event
    cold = ExecutionJournal("serialized-owner", base_dir=tmp_path).read_events()
    assert cold[0].to_dict() == event.to_dict()
    assert json.loads(journal.path.read_text(encoding="utf-8")) == event.to_dict()


def test_journal_json_preserves_dataclass_leaf_encoding_and_rejects_unserializable_facts(tmp_path):
    from backend.agent.execution_journal import JournalEvent

    @dataclass
    class Fact:
        text: str
        values: list

    fact = Fact("exact 中文", [{"ok": True, "count": 1}])
    candidate = JournalEvent("system", "leaf-owner", payload={"fact": fact}, event_id="leaf", ts_ms=1)
    assert candidate.to_json() == json.dumps(candidate.to_dict(), ensure_ascii=False, separators=(",", ":"))
    journal = ExecutionJournal("leaf-owner", base_dir=tmp_path)
    event = journal.append("system", {"fact": fact}, event_id="leaf", ts_ms=1)
    fact.values[0]["count"] = 3
    assert event.payload["fact"]["values"][0]["count"] == 1
    before = journal.path.read_bytes()
    with pytest.raises(TypeError):
        journal.append("system", {"unsupported": object()})
    assert journal.path.read_bytes() == before
    following = journal.append("system", {"following": "accepted"})
    assert following.seq == event.seq + 1
    assert [item.to_dict() for item in journal.read_events()] == [item.to_dict() for item in ExecutionJournal("leaf-owner", base_dir=tmp_path).read_events()]


@pytest.mark.parametrize("mode", ["append", "once", "batch"])
def test_incremental_snapshot_serializes_only_the_change_and_owns_submitted_values(tmp_path, monkeypatch, mode):
    from backend.agent.execution_journal import JournalEvent

    journal = ExecutionJournal("owned-deltas", base_dir=tmp_path)
    original = {"history": [{"role": "user", "content": str(i)} for i in range(1000)]}
    first = append_snapshot(journal, original)
    changed = deepcopy(original)
    changed["history"].append({"role": "user", "content": "next"})
    payload = {"conversation_id": "conversation", "context_snapshot": changed, "metadata": {"label": "owned"}}
    serialize = JournalEvent.to_json
    def bounded(event):
        assert "context_snapshot" not in event.payload, "An incremental append traversed the whole snapshot"
        assert len(json.dumps(event.payload)) < 1000
        return serialize(event)
    with monkeypatch.context() as patch:
        patch.setattr(JournalEvent, "to_json", bounded)
        if mode == "append":
            event = journal.append("system", payload, event_id="next")
        elif mode == "once":
            event = journal.append_once("system", payload, event_id="next")
        else:
            event, = journal.append_once_many([("system", payload, "next")])
    changed["history"][0]["content"] = "caller changed old item"
    changed["history"][-1]["content"] = "caller changed new item"
    payload["metadata"]["label"] = "caller changed metadata"
    assert first.payload["context_snapshot"] == original
    assert event.payload["context_snapshot"]["history"][-1]["content"] == "next"
    assert event.payload["metadata"]["label"] == "owned"
    assert journal.read_events()[-1].to_dict() == ExecutionJournal("owned-deltas", base_dir=tmp_path).read_events()[-1].to_dict()


def test_context_changes_preserve_each_boundary_across_writers_and_cold_recovery(tmp_path):
    first = ExecutionJournal("snapshots", base_dir=tmp_path)
    original = snapshot()
    baseline = deepcopy(original)
    seed = append_snapshot(first, original, event_id="seed")
    # Mutating the submitted object must not rewrite the durable delta base.
    original["history"][0]["content"] = "uncommitted caller mutation"
    second = ExecutionJournal("snapshots", base_dir=tmp_path)
    changed = deepcopy(baseline)
    changed["history"].append({"role": "assistant", "content": "answer"})
    changed["extension_state"]["entries"][31]["data"] += " updated"
    changed.pop("persistent_notes")
    appended = append_snapshot(second, changed, event_id="changed")
    unrelated = {"history": [{"role": "user", "content": "other conversation"}]}
    append_snapshot(first, unrelated, owner="other")
    compacted = {**changed, "history": [{"role": "user", "content": "compacted summary"}]}
    append_snapshot(first, compacted, event_id="compacted")
    append_snapshot(second, compacted, event_id="identical")
    cold = ExecutionJournal("snapshots", base_dir=tmp_path)
    events = cold.read_events()
    assert [event.payload["context_snapshot"] for event in events] == [baseline, changed, unrelated, compacted, compacted]
    assert cold.append_once("system", appended.payload, event_id="changed").seq == appended.seq
    assert seed.payload["context_snapshot"] == baseline
    assert len(cold.read_events()) == 5
    lines = cold.path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert "context_snapshot" in rows[0]["payload"]
    assert "context_snapshot" in rows[2]["payload"]
    assert rows[3]["payload"]["context_snapshot_change"]["base_event_id"] == "changed"
    assert rows[4]["payload"]["context_snapshot_change"]["change"] is None
    assert all(len(lines[index].encode("utf-8")) < 2048 for index in (1, 3, 4))


def test_legacy_full_snapshot_can_seed_new_deltas_and_incomplete_tail_recovery(tmp_path):
    journal = ExecutionJournal("legacy", base_dir=tmp_path)
    append_snapshot(journal, snapshot())
    raw = json.loads(journal.path.read_text(encoding="utf-8"))
    raw["schema_version"] = 6
    journal.path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    writer = ExecutionJournal("legacy", base_dir=tmp_path)
    changed = {**snapshot(), "history": []}
    append_snapshot(writer, changed)
    with writer.path.open("ab") as handle:
        handle.write(b'{"schema_version":7,"payload":')
    cold = ExecutionJournal("legacy", base_dir=tmp_path)
    assert cold.unacknowledged_tail_records == 1
    assert cold.read_events()[-1].payload["context_snapshot"] == changed
    append_snapshot(cold, snapshot())
    assert ExecutionJournal("legacy", base_dir=tmp_path).read_events()[-1].payload["context_snapshot"] == snapshot()


@pytest.mark.parametrize("snapshot_ahead", [False, True])
def test_context_base_reuses_private_facts_without_changing_snapshot_boundaries(tmp_path, snapshot_ahead):
    journal = ExecutionJournal("private-facts", base_dir=tmp_path)
    before = snapshot()
    after = deepcopy(before)
    after["extension_state"]["entries"].append({"id": "large", "data": "y" * 65536})
    after["extension_cursor"] = {"run_id": "run", "revision": 1}
    seed = after if snapshot_ahead else before
    append_snapshot(journal, seed)
    journal.append_lifecycle("extension_state_delta", {
        "conversation_id": "conversation", "run_id": "run", "base_revision": 0, "revision": 1,
        "extension_changes": [value_change(before["extension_state"], after["extension_state"])],
    })
    # A different conversation's private facts must not enter this baseline.
    journal.append_lifecycle("extension_state_delta", {
        "conversation_id": "other", "run_id": "other-run", "base_revision": 0, "revision": 1,
        "extension_changes": [{"value": {"name": "unrelated"}}],
    })
    append_snapshot(journal, after)
    cold = ExecutionJournal("private-facts", base_dir=tmp_path)
    events = cold.read_events()
    assert events[0].payload["context_snapshot"] == seed
    assert events[-1].payload["context_snapshot"] == after
    assert len(cold.path.read_bytes().splitlines()[-1]) < 512
    # Rollback is a real state replacement, even after a later private fact.
    append_snapshot(cold, before)
    assert ExecutionJournal("private-facts", base_dir=tmp_path).read_events()[-1].payload["context_snapshot"] == before


@pytest.mark.parametrize("damage", ["missing_base", "wrong_owner", "bad_offset"])
def test_persisted_context_delta_rejects_an_unrelated_base(tmp_path, damage):
    journal = ExecutionJournal("corrupt", base_dir=tmp_path)
    append_snapshot(journal, {"history": [], "note": "prefix"})
    append_snapshot(journal, {"history": [], "note": "prefix tail"})
    rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
    payload = rows[-1]["payload"]
    if damage == "missing_base":
        payload["context_snapshot_change"]["base_event_id"] = "missing"
    elif damage == "wrong_owner":
        payload["conversation_id"] = "another"
    else:
        payload["context_snapshot_change"]["change"]["fields"]["note"]["at"] = 999
    journal.path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ExecutionJournalCorruptionError, match="context snapshot change"):
        ExecutionJournal("corrupt", base_dir=tmp_path).read_events()


@pytest.mark.asyncio
async def test_real_query_terminal_crash_windows_restore_deltas_and_end_hook_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))

    def factory(api):
        api.on("before_agent_start", lambda event, ctx: api.append_entry("before", "private" * 1024))
        api.on("agent_end", lambda event, ctx: api.append_entry("end", "finished"))

    async def behavior(model, messages):
        return None

    _, builder, journal, _, state, _, _ = await run_child(tmp_path, monkeypatch, factory, behavior)
    assert state.terminal_status == "completed"
    events = journal.read_events()
    lines = journal.path.read_bytes().splitlines(keepends=True)
    intent = next(event for event in events if event.payload.get("lifecycle") == "terminal_intent")
    receipt = next(event for event in events if event.payload.get("lifecycle") == "runtime_terminal_committed")
    assistant = next(event for event in events if event.event_type == "assistant")
    terminal = next(event for event in events if event.event_type == "terminal")
    end = next(event for event in events if event.seq > terminal.seq and event.payload.get("lifecycle") == "extension_state_delta")
    assert "context_snapshot_change" in json.loads(lines[intent.seq - 1])["payload"]
    assert "context_snapshot_change" in json.loads(lines[assistant.seq - 1])["payload"]
    for boundary in (intent, receipt, assistant, terminal, end):
        root = tmp_path / f"crash-{boundary.seq}"
        path = ExecutionJournal(journal.agent_id, base_dir=root).path
        path.write_bytes(b"".join(lines[:boundary.seq]))
        recovered = ExecutionJournal(journal.agent_id, base_dir=root)
        projections = recovered.unprojected_terminal_projections()
        if boundary is intent:
            assert projections == []
            continue
        assert len(projections) == 1
        restored = projections[0]["context_snapshot"]
        assert restored["history"] == intent.payload["context_snapshot"]["history"]
        assert restored["extension_state"] == (builder.extension_state if boundary is end else intent.payload["context_snapshot"]["extension_state"])
        assert projections[0]["assistant_message"]["content"] == state.reply
