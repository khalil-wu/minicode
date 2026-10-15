"""Rollback projects current Stop intent without executing or discarding input."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agent.message import UserCommand
from backend.bootstrap import prepare_rollback as rollback_module
from backend.ws.durable_user_queue import DurableUserMessageQueue


def command(command_id: str, content: str = "保留原文\nexact input\t42") -> UserCommand:
    return UserCommand(type="user_message", data={"client_command_id": command_id,
        "conversation_id": "conv_rollback_owned", "user_message_id": "user_original",
        "assistant_message_id": "assistant_original", "content": content,
        "attachments": [{"id": "fixture_attachment", "name": "原附件.png"}]})


def queue(root: Path, session_id: str = "rollback-window") -> DurableUserMessageQueue:
    return DurableUserMessageQueue(session_id=session_id, root_dir=root / "data/user-message-queue")


def cancelled(root: Path, session_id: str = "rollback-window", command_id: str = "original-input"):
    value = queue(root, session_id)
    original = command(command_id)
    value.persist_client_command(original)
    record = value.cancel_client_command(original, reason="user_interrupted")
    path = value.path
    archive = value._cancelled_receipt_path(command_id)
    value.close()
    return path, archive, record


def test_rollback_projects_complete_receipt_retains_new_input_and_is_repeatable(tmp_path):
    path, archive, record = cancelled(tmp_path)
    original_archive = archive.read_bytes()
    value = queue(tmp_path)
    fresh = command("fresh-input", "这是用户后来新增的内容")
    value.persist_client_command(fresh)
    value.close()
    original = json.loads(path.read_text(encoding="utf-8"))
    outcome = rollback_module.prepare_rollback(tmp_path)
    projected = json.loads(path.read_text(encoding="utf-8"))
    assert outcome["ok"] and outcome["rewritten_count"] == 1 and outcome["schema_version"] == 6
    assert projected["version"] == 6
    assert projected["client_cancelled"] == {"original-input": record}
    assert projected["client_pending"] == original["client_pending"]
    assert projected["client_pending"][0]["data"]["content"] == fresh.data["content"]
    assert archive.read_bytes() == original_archive
    prepared_bytes = path.read_bytes()
    again = rollback_module.prepare_rollback(tmp_path)
    assert again["rewritten_count"] == 0 and again["cancellation_count"] == 1
    assert path.read_bytes() == prepared_bytes and archive.read_bytes() == original_archive
    # Going back to the new binary reclaims only its hot map; original content
    # remains in the same immutable receipt and fresh work remains pending.
    current = queue(tmp_path)
    try:
        assert current.cancelled_client_command("original-input") == record
        assert [item.data["client_command_id"] for item in current.pending_client_commands()] == ["fresh-input"]
        assert json.loads(path.read_text(encoding="utf-8"))["client_cancelled"] == {}
    finally:
        current.close()


def test_live_owner_blocks_rollback_before_any_queue_is_changed(tmp_path):
    path, archive, _ = cancelled(tmp_path)
    live = queue(tmp_path)
    before = path.read_bytes()
    archive_before = archive.read_bytes()
    try:
        with pytest.raises(rollback_module.RollbackPreparationError, match="live user-message queue owner"):
            rollback_module.prepare_rollback(tmp_path)
        assert path.read_bytes() == before and archive.read_bytes() == archive_before
    finally:
        live.close()


def test_different_new_content_for_cancelled_id_blocks_all_profile_mutations(tmp_path):
    first, _, _ = cancelled(tmp_path, "first-window", "first-input")
    second, archive, receipt = cancelled(tmp_path, "second-window", "second-input")
    payload = json.loads(second.read_text(encoding="utf-8"))
    changed = command("second-input", "另一个真正不同的新输入")
    payload["client_pending"] = [{"type": changed.type, "data": changed.data}]
    second.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = {path: path.read_bytes() for path in (first, second, archive)}
    with pytest.raises(rollback_module.RollbackPreparationError, match="different input"):
        rollback_module.prepare_rollback(tmp_path)
    assert all(path.read_bytes() == original for path, original in before.items())


def test_archive_then_queue_crash_window_is_projected_without_replaying_input(tmp_path):
    path, _, record = cancelled(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["client_inflight"] = {"original-input": record["command"]}
    payload["ownership"]["client_inflight"] = {"original-input": "old-closed-owner"}
    payload["client_pending"] = [record["command"]]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rollback_module.prepare_rollback(tmp_path)
    prepared = json.loads(path.read_text(encoding="utf-8"))
    assert prepared["client_pending"] == []
    assert prepared["ownership"]["client_inflight"] == {}
    assert prepared["client_inflight"] == {}
    assert prepared["client_cancelled"] == {"original-input": record}


def test_io_failure_is_visible_and_partial_atomic_projection_can_be_repeated(tmp_path, monkeypatch):
    first, first_archive, first_record = cancelled(tmp_path, "a-window", "first-input")
    second, second_archive, second_record = cancelled(tmp_path, "b-window", "second-input")
    saved_archives = {path: path.read_bytes() for path in (first_archive, second_archive)}
    original_second = second.read_bytes()
    write = rollback_module.atomic_write_text
    def disk_failure(path, content):
        if path == second:
            raise OSError("isolated rollback disk-full injection")
        return write(path, content)
    monkeypatch.setattr(rollback_module, "atomic_write_text", disk_failure)
    with pytest.raises(OSError, match="disk-full injection"):
        rollback_module.prepare_rollback(tmp_path)
    assert json.loads(first.read_text(encoding="utf-8"))["client_cancelled"]["first-input"] == first_record
    assert second.read_bytes() == original_second
    assert all(path.read_bytes() == original for path, original in saved_archives.items())
    monkeypatch.setattr(rollback_module, "atomic_write_text", write)
    assert rollback_module.prepare_rollback(tmp_path)["rewritten_count"] == 1
    assert json.loads(second.read_text(encoding="utf-8"))["client_cancelled"]["second-input"] == second_record


def test_corrupt_receipt_is_preserved_and_blocks_projection(tmp_path):
    path, archive, _ = cancelled(tmp_path)
    archive.write_bytes(b'{"command":')
    before = path.read_bytes()
    with pytest.raises(json.JSONDecodeError):
        rollback_module.prepare_rollback(tmp_path)
    assert path.read_bytes() == before and archive.read_bytes() == b'{"command":'


def test_cli_reports_aggregate_success_and_nonzero_failures_without_user_content(tmp_path, monkeypatch, capsys):
    path, archive, _ = cancelled(tmp_path)
    monkeypatch.setattr(rollback_module.sys, "argv", ["prepare_rollback", "--state-root", str(tmp_path)])
    assert rollback_module.main() == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["ok"] is True and report["schema_version"] == 6
    assert report["rewritten_count"] == 1 and "exact input" not in output.out
    prepared = path.read_bytes()
    archive.write_bytes(b'{"command":')
    assert rollback_module.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.err)["ok"] is False and output.out == ""
    assert path.read_bytes() == prepared


def test_nested_symlink_outside_state_root_cannot_be_modified(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside-owned-fixture")
    outside.mkdir()
    value = outside / "user-message-queue"
    value.mkdir()
    link = tmp_path / "data"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"OS directory-symlink creation unavailable: {type(exc).__name__}")
    with pytest.raises(rollback_module.RollbackPreparationError, match="escapes"):
        rollback_module.prepare_rollback(tmp_path)
    assert list(value.iterdir()) == []


@pytest.mark.parametrize("shared_first", [False, True])
def test_cross_store_stop_clears_every_automatic_lane_and_recovers_original_window(tmp_path, shared_first):
    base = tmp_path / "data/user-message-queue"
    client = queue(tmp_path, "original-window")
    other = queue(tmp_path, "unrelated-window")
    shared = DurableUserMessageQueue(session_id="shared", root_dir=base / "conversations", shared_inputs=True)
    original = command("stopped-original")
    fresh = command("fresh-input", "后来新增的正文及附件必须原样保留")
    try:
        original.data["_client_queue_key"] = client.path.stem
        client.persist_client_command(original)
        record = client.prepare_client_cancellation(original, reason="user_interrupted")
        target = shared if shared_first else client
        target.cancel_client_command(original, reason="user_interrupted", cancellation=record)
        path = shared.path
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else rollback_module._read_queue(path)
        owner = "conv_rollback_owned"
        stopped = record["command"]
        new = {"type": fresh.type, "data": fresh.data}
        payload.update(queues={owner: [stopped, new]}, inflight={owner: stopped},
            turn_inputs={owner: [stopped, new]}, client_pending=[stopped, new],
            client_inflight={"stopped-original": stopped, "fresh-input": new},
            ownership={"inflight": {owner: "closed-owner"}, "turn_inputs": {owner: "closed-owner"},
                "client_inflight": {"stopped-original": "closed-owner", "fresh-input": "closed-owner"}})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        other.persist_client_command(command("unrelated-fresh", "其他窗口的内容"))
        unrelated_before = json.loads(other.path.read_text(encoding="utf-8"))
        archive_path = target._cancelled_receipt_path("stopped-original", client.path.stem)
        archive_before = archive_path.read_bytes()
        client_path, other_path = client.path, other.path
    finally:
        client.close()
        shared.close()
        other.close()
    rollback_module.prepare_rollback(tmp_path)
    prepared = json.loads(path.read_text(encoding="utf-8"))
    assert prepared["queues"] == {owner: [new]}
    assert prepared["inflight"] == {} and prepared["ownership"]["inflight"] == {}
    assert prepared["turn_inputs"] == {owner: [new]}
    assert prepared["ownership"]["turn_inputs"] == {owner: "closed-owner"}
    assert prepared["client_pending"] == [new] and prepared["client_inflight"] == {"fresh-input": new}
    assert prepared["ownership"]["client_inflight"] == {"fresh-input": "closed-owner"}
    assert prepared["client_cancelled"] == {}
    restored_window = json.loads(client_path.read_text(encoding="utf-8"))
    assert restored_window["client_pending"] == []
    assert restored_window["client_cancelled"] == {"stopped-original": record}
    unrelated = json.loads(other_path.read_text(encoding="utf-8"))
    assert unrelated["client_pending"] == unrelated_before["client_pending"]
    assert unrelated["client_cancelled"] == {}
    assert archive_path.read_bytes() == archive_before
    before = {item: item.read_bytes() for item in (path, client_path, other_path)}
    assert rollback_module.prepare_rollback(tmp_path)["rewritten_count"] == 0
    assert all(item.read_bytes() == data for item, data in before.items())


def test_shared_only_stop_recovers_missing_origin_window_without_copying_to_every_window(tmp_path):
    shared = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "data/user-message-queue/conversations", shared_inputs=True)
    original = command("shared-committed")
    original.data["_client_queue_key"] = "window-before-crash"
    record = shared.prepare_client_cancellation(original, reason="user_interrupted")
    shared.cancel_client_command(original, reason="user_interrupted", cancellation=record)
    shared.close()
    rollback_module.prepare_rollback(tmp_path)
    origin = tmp_path / "data/user-message-queue/window-before-crash.json"
    assert json.loads(origin.read_text(encoding="utf-8"))["client_cancelled"] == {"shared-committed": record}


def test_conflicting_profile_metadata_or_bound_owner_blocks_before_all_mutations(tmp_path):
    first, archive, receipt = cancelled(tmp_path)
    shared = tmp_path / "data/user-message-queue/conversations/shared.json"
    shared.parent.mkdir(parents=True)
    payload = rollback_module._read_queue(shared)
    conflicting = {**receipt["command"], "data": {**receipt["command"]["data"], "conversation_id": "another-conversation", "_client_queue_key": "rollback-window"}}
    payload["inflight"] = {"another-conversation": conflicting}
    shared.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = {item: item.read_bytes() for item in (first, archive, shared)}
    with pytest.raises(rollback_module.RollbackPreparationError, match="another conversation"):
        rollback_module.prepare_rollback(tmp_path)
    assert all(item.read_bytes() == data for item, data in before.items())


def test_invalid_original_window_provenance_cannot_escape_profile(tmp_path):
    path, archive, record = cancelled(tmp_path)
    record["client_queue_key"] = "../outside"
    archive.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    before = {item: item.read_bytes() for item in (path, archive)}
    with pytest.raises(rollback_module.RollbackPreparationError, match="original window identity"):
        rollback_module.prepare_rollback(tmp_path)
    assert all(item.read_bytes() == data for item, data in before.items())


@pytest.mark.parametrize("same_owner", [False, True])
@pytest.mark.parametrize("same_body", [False, True])
def test_rollback_same_local_id_keeps_the_other_producers_input(tmp_path, same_owner, same_body):
    base = tmp_path / "data/user-message-queue"
    first, second = queue(tmp_path, "first-window"), queue(tmp_path, "second-window")
    shared = DurableUserMessageQueue(session_id="shared", root_dir=base / "conversations", shared_inputs=True)
    a = command("local-id-1", "first original input")
    b = command("local-id-1", a.data["content"] if same_body else "another original input")
    b.data["conversation_id"] = a.data["conversation_id"] if same_owner else "conv_other_producer"
    a.data["_client_queue_key"], b.data["_client_queue_key"] = first.path.stem, second.path.stem
    try:
        assert first.persist_client_command(a) and second.persist_client_command(b)
        shared.save({a.data["conversation_id"]: [a]}, {}, {})
        before = json.loads(shared.path.read_text(encoding="utf-8"))
        record = first.prepare_client_cancellation(a, reason="user_interrupted")
        shared.cancel_client_command(a, reason="user_interrupted", cancellation=record)
        archive = shared._cancelled_receipt_path("local-id-1", first.path.stem)
        archived_bytes = archive.read_bytes()
        # Fault injection: the intent is durable but the old queue publish has
        # not happened; an independently accepted second request is also there.
        before["queues"].setdefault(b.data["conversation_id"], []).append({"type": b.type, "data": dict(b.data)})
        shared.path.write_text(json.dumps(before), encoding="utf-8")
        shared_path, first_path, second_path = shared.path, first.path, second.path
        second_original = json.loads(second_path.read_text(encoding="utf-8"))["client_pending"]
    finally:
        first.close()
        second.close()
        shared.close()
    rollback_module.prepare_rollback(tmp_path)
    projected = json.loads(shared_path.read_text(encoding="utf-8"))
    assert projected["queues"] == {b.data["conversation_id"]: [{"type": b.type, "data": b.data}]}
    assert projected["client_cancelled"] == {}
    assert json.loads(first_path.read_text(encoding="utf-8"))["client_cancelled"] == {"local-id-1": record}
    second_projected = json.loads(second_path.read_text(encoding="utf-8"))
    assert second_projected["client_pending"] == second_original and second_projected["client_cancelled"] == {}
    assert archive.read_bytes() == archived_bytes
    assert rollback_module.prepare_rollback(tmp_path)["rewritten_count"] == 0


def test_two_cancelled_producers_with_same_id_have_separate_r6_maps(tmp_path):
    first_path, first_archive, first_record = cancelled(tmp_path, "first-window", "same-local-id")
    second_path, second_archive, second_record = cancelled(tmp_path, "second-window", "same-local-id")
    originals = {path: path.read_bytes() for path in (first_archive, second_archive)}
    outcome = rollback_module.prepare_rollback(tmp_path)
    assert outcome["cancellation_count"] == 2
    assert json.loads(first_path.read_text(encoding="utf-8"))["client_cancelled"] == {"same-local-id": first_record}
    assert json.loads(second_path.read_text(encoding="utf-8"))["client_cancelled"] == {"same-local-id": second_record}
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_legacy_shared_input_with_two_possible_origins_preserves_every_file(tmp_path):
    first_path, archive, _record = cancelled(tmp_path, "first-window", "same-local-id")
    second = queue(tmp_path, "second-window")
    raw = command("same-local-id")
    second.persist_client_command(raw)
    second.close()
    shared = tmp_path / "data/user-message-queue/conversations/shared.json"
    shared.parent.mkdir(parents=True)
    payload = rollback_module._read_queue(shared)
    payload["queues"] = {raw.data["conversation_id"]: [{"type": raw.type, "data": raw.data}]}
    shared.write_text(json.dumps(payload), encoding="utf-8")
    paths = (first_path, archive, second.path, shared)
    originals = {path: path.read_bytes() for path in paths}
    with pytest.raises(rollback_module.RollbackPreparationError, match="unique cancellation producer"):
        rollback_module.prepare_rollback(tmp_path)
    assert all(path.read_bytes() == data for path, data in originals.items())
