import json
from pathlib import Path

from backend.agent.execution_journal import ExecutionJournal
from backend.agent.journal_compaction import (
    COMMITTED,
    COMPACT_MIN_BYTES,
    DELTA,
    PENDING,
    compact_journal_file,
)

AGENT = "conversation_" + "ab" * 32
OWNER = "conv-1"


def _event(seq, event_type, payload, event_id=None):
    return {"event_type": event_type, "agent_id": AGENT, "payload": payload, "event_id": event_id or f"evt-{seq}",
            "seq": seq, "ts_ms": 1_700_000_000_000 + seq, "parent_event_id": "", "schema_version": 7}


def legacy_events(*, bulk=40_000, projections=6):
    events = []

    def add(event_type, payload):
        events.append(_event(len(events) + 1, event_type, payload))
        return events[-1]["event_id"]

    history = [{"role": "user", "content": "hello"}]
    add("system", {"lifecycle": "turn_started", "conversation_id": OWNER, "run_id": "run-1",
                   "context_snapshot": {"history": history, "extension_state": {}}})
    add("user_prompt", {"conversation_id": OWNER, "user_message_id": "u-1", "content": "hello"})
    add("tool_use", {"tool_call": {"id": "call-1", "name": "read_file", "arguments": {"path": "a"}}})
    add("tool_result", {"tool_call_id": "call-1", "content": "ok"})
    for index in range(projections):
        snapshot = {"history": history + [{"role": "assistant", "content": "x" * bulk, "tool_calls": []}],
                    "extension_state": {}, "turn_admissions": {}}
        pending = add("system", {
            "lifecycle": PENDING, "conversation_id": OWNER, "run_id": "run-1",
            "assistant_message": {"id": "m-1", "role": "assistant", "content": "y" * bulk,
                                  "blocks": [{"type": "text", "content": "y" * bulk}]},
            "context_snapshot": snapshot, "summary": None, "expected_revision": index + 1,
        })
        add("system", {"lifecycle": COMMITTED, "conversation_id": OWNER, "pending_event_id": pending,
                       "message_id": "m-1", "run_id": "run-1", "revision": index + 2})
    # A projection without its receipt is replayed by recovery and must survive untouched.
    add("system", {
        "lifecycle": PENDING, "conversation_id": OWNER, "run_id": "run-1",
        "assistant_message": {"id": "m-2", "role": "assistant", "content": "z" * bulk, "blocks": []},
        "context_snapshot": {"history": history, "extension_state": {}}, "summary": None, "expected_revision": 9,
    })
    add("tool_use", {"tool_call": {"id": "call-2", "name": "write_file", "arguments": {"path": "b"}}})
    return events


def write_journal(root: Path, events) -> Path:
    path = root / AGENT / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events),
                    encoding="utf-8")
    return path


def recovery_views(root: Path) -> dict:
    journal = ExecutionJournal(AGENT, base_dir=root)
    events = journal.read_events()
    return {
        "identity": [(event.seq, event.event_id, event.event_type, event.payload.get("lifecycle")) for event in events],
        "history": journal.reconstruct_history(),
        "context_snapshot": journal.reconstruct_context_snapshot(),
        "unresolved": journal.unresolved_tool_uses(),
        "pending": [(event.event_id, event.payload) for event in journal.pending_conversation_projections()],
        "projection_heads": {key: value[0] for key, value in journal._projection_heads.items()},
        "context_heads": {key: value[0] for key, value in journal._context_heads.items()},
        "tool_states": {key: value[0] for key, value in journal._tool_states.items()},
    }


def test_compaction_keeps_every_recovery_view_identical(tmp_path):
    events = legacy_events()
    pristine = write_journal(tmp_path / "pristine", events)
    compacted = write_journal(tmp_path / "compacted", events)
    before = compacted.stat().st_size

    # The newest projection of m-1 stays a full head; the five it superseded are stubbed.
    assert compact_journal_file(compacted, min_bytes=0) == 5
    assert compacted.stat().st_size < before / 2
    assert recovery_views(tmp_path / "compacted") == recovery_views(tmp_path / "pristine")

    journal = ExecutionJournal(AGENT, base_dir=tmp_path / "compacted")
    stubs = [event for event in journal.read_events() if event.payload.get("compacted")]
    assert len(stubs) == 5
    assert all(event.payload["assistant_message"] == {"id": "m-1"} for event in stubs)
    assert all("context_snapshot" not in event.payload for event in stubs)
    assert journal.get_event(stubs[0].event_id) is stubs[0]
    assert pristine.read_bytes() != compacted.read_bytes()


def test_compaction_is_idempotent_and_skips_journals_without_bulk(tmp_path):
    path = write_journal(tmp_path, legacy_events())
    assert compact_journal_file(path, min_bytes=0) == 5
    after = path.read_bytes()
    assert compact_journal_file(path, min_bytes=0) == 0
    assert path.read_bytes() == after

    small = write_journal(tmp_path / "small", legacy_events(bulk=40))
    assert compact_journal_file(small, min_bytes=0) == 0
    assert compact_journal_file(write_journal(tmp_path / "under-budget", legacy_events())) == 0


def test_compaction_leaves_irregular_journals_untouched(tmp_path):
    events = legacy_events()
    broken = write_journal(tmp_path / "broken", events)
    broken.write_text(broken.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    original = broken.read_bytes()
    assert compact_journal_file(broken, min_bytes=0) == 0
    assert broken.read_bytes() == original

    skewed = list(events)
    skewed[3] = {**skewed[3], "seq": 99}
    gap = write_journal(tmp_path / "gap", skewed)
    original = gap.read_bytes()
    assert compact_journal_file(gap, min_bytes=0) == 0
    assert gap.read_bytes() == original


def test_opening_an_oversized_legacy_journal_compacts_it_and_keeps_deltas_decodable(tmp_path):
    path = write_journal(tmp_path, legacy_events(bulk=250_000, projections=10))
    assert path.stat().st_size > COMPACT_MIN_BYTES

    journal = ExecutionJournal(AGENT, base_dir=tmp_path)
    assert path.stat().st_size < COMPACT_MIN_BYTES / 2
    events = journal.read_events()
    assert [event.seq for event in events] == list(range(1, len(events) + 1))

    # The surviving head of m-2 still anchors a delta written by the current code.
    journal.append_lifecycle(PENDING, {
        "conversation_id": OWNER, "run_id": "run-1", "assistant_message": {"id": "m-2", "role": "assistant",
        "content": "z" * 250_000 + " more", "blocks": []}, "context_delta": {"set": {}, "removed": []},
        "partial": True, "summary": None, "expected_revision": 9,
    })
    reopened = ExecutionJournal(AGENT, base_dir=tmp_path)
    events = reopened.read_events()
    head = next(event for event in events if event.payload.get("lifecycle") == PENDING
                and event.payload.get("assistant_message", {}).get("id") == "m-2")
    tail = events[-1]
    assert tail.payload["lifecycle"] == DELTA
    assert tail.payload["base_event_id"] == head.event_id
    pending = reopened.pending_conversation_projections()
    assert {event.payload["assistant_message"]["id"] for event in pending} == {"m-2"}
    assert pending[-1].payload["assistant_message"]["content"].endswith(" more")
