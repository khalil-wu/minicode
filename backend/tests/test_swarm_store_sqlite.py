from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.agent.swarm_migrations import SchemaMigration, SchemaMigrationRunner
from backend.agent.swarm_store import FileSwarmStore


def test_store_uses_wal_and_persists_records(tmp_path) -> None:
    root = tmp_path / "swarm"
    store = FileSwarmStore(root)
    message = store.append_message({
        "conversation_id": "conversation-a",
        "sender_id": "main",
        "recipient_id": "worker",
        "content": "persist me",
    })

    restored = FileSwarmStore(root)

    assert restored.list_messages(conversation_id="conversation-a") == [message]
    with sqlite3.connect(root / "swarm.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "7"
    with store._connect() as connection:  # type: ignore[attr-defined]
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_schema_checksum_is_recorded_and_verified_on_reopen(tmp_path) -> None:
    root = tmp_path / "swarm"
    FileSwarmStore(root)

    with sqlite3.connect(root / "swarm.sqlite3") as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_checksum_v7'"
        ).fetchone()
        assert row is not None
        assert len(row[0]) == 64
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_checksum_v7'",
            ("0" * 64,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="schema checksum mismatch"):
        FileSwarmStore(root)


def test_ordered_migration_manifest_detects_historical_definition_tampering(tmp_path) -> None:
    path = tmp_path / "migrations.sqlite3"
    migrations = (
        SchemaMigration(1, "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"),
        SchemaMigration(2, "CREATE TABLE sample (id TEXT PRIMARY KEY);"),
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        SchemaMigrationRunner(migrations).run(connection)

    changed_history = (
        migrations[0],
        SchemaMigration(2, "CREATE TABLE sample (id INTEGER PRIMARY KEY);"),
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(RuntimeError, match="migration checksum mismatch for v2"):
            SchemaMigrationRunner(changed_history).run(connection)


def test_store_records_each_ordered_schema_migration_checksum(tmp_path) -> None:
    root = tmp_path / "swarm"
    FileSwarmStore(root)

    with sqlite3.connect(root / "swarm.sqlite3") as connection:
        keys = connection.execute(
            "SELECT key FROM metadata WHERE key LIKE 'migration_checksum_v%' ORDER BY key"
        ).fetchall()

    assert [row[0] for row in keys] == [
        "migration_checksum_v1",
        "migration_checksum_v2",
        "migration_checksum_v3",
        "migration_checksum_v4",
        "migration_checksum_v5",
        "migration_checksum_v6",
        "migration_checksum_v7",
    ]


def test_runtime_lease_cannot_be_stolen_before_expiry(tmp_path) -> None:
    store = FileSwarmStore(tmp_path / "swarm")
    first = store.claim_runtime_lease(
        runtime_instance_id="runtime-a",
        requested_owner_token="owner-a",
        process_id=101,
        process_start_identity="birth-a",
        now_ms=1_000,
        ttl_ms=30_000,
    )
    conflicting = store.claim_runtime_lease(
        runtime_instance_id="runtime-a",
        requested_owner_token="owner-b",
        process_id=101,
        process_start_identity="birth-b",
        now_ms=2_000,
        ttl_ms=30_000,
    )

    assert first["acquired"] is True
    assert conflicting["acquired"] is False
    assert conflicting["owner_token"] == "owner-a"


def test_runtime_lease_can_be_fenced_and_reclaimed_after_expiry(tmp_path) -> None:
    store = FileSwarmStore(tmp_path / "swarm")
    store.claim_runtime_lease(
        runtime_instance_id="runtime-a",
        requested_owner_token="owner-a",
        process_id=101,
        process_start_identity="birth-a",
        now_ms=1_000,
        ttl_ms=1_000,
    )
    reclaimed = store.claim_runtime_lease(
        runtime_instance_id="runtime-a",
        requested_owner_token="owner-b",
        process_id=202,
        process_start_identity="birth-b",
        now_ms=2_001,
        ttl_ms=1_000,
    )

    assert reclaimed["acquired"] is True
    assert reclaimed["owner_token"] == "owner-b"
    assert store.heartbeat_runtime_lease(
        runtime_instance_id="runtime-a",
        owner_token="owner-a",
        process_id=101,
        process_start_identity="birth-a",
        now_ms=2_002,
        ttl_ms=1_000,
    ) is False


def test_sequences_are_per_conversation_and_output_advances_task_twice(tmp_path) -> None:
    store = FileSwarmStore(tmp_path / "swarm")

    assert store.append_message({"conversation_id": "a"})["seq"] == 1
    assert store.append_message({"conversation_id": "b"})["seq"] == 1
    task = store.create_task({"conversation_id": "a", "title": "work"})
    updated = store.append_output(task["task_id"], {"content": "done"})

    assert task["seq"] == 2
    assert updated is not None
    assert updated["outputs"][0]["seq"] == 3
    assert updated["seq"] == 4


def test_dependencies_remain_reciprocal_when_one_side_is_patched(tmp_path) -> None:
    store = FileSwarmStore(tmp_path / "swarm")
    first = store.create_task({"conversation_id": "a", "task_id": "first", "title": "first"})
    store.create_task({
        "conversation_id": "a",
        "task_id": "second",
        "title": "second",
        "blocked_by": [first["task_id"]],
    })

    updated = store.update_task("second", {"blocks": ["third"]})

    assert updated is not None
    assert updated["blocked_by"] == ["first"]
    assert updated["blocks"] == ["third"]
    assert store.get_task("first")["blocks"] == ["second"]  # type: ignore[index]


def test_team_replace_and_delete_use_new_sequence_numbers(tmp_path) -> None:
    store = FileSwarmStore(tmp_path / "swarm")
    first = store.create_team({"conversation_id": "a", "team_name": "review"})
    second = store.create_team({"conversation_id": "a", "team_name": "review"})
    deleted = store.delete_team(conversation_id="a", team_name="review")

    assert first["team_id"] != second["team_id"]
    assert second["seq"] == first["seq"] + 1
    assert deleted is not None
    assert deleted["team_id"] == second["team_id"]
    assert deleted["deleted_seq"] == second["seq"] + 1
    assert store.list_teams(conversation_id="a") == []


def test_legacy_json_is_imported_once_and_repairs_dependencies(tmp_path) -> None:
    root = tmp_path / "swarm"
    root.mkdir()
    (root / "conversation-a.json").write_text(json.dumps({
        "version": 1,
        "high_water": 7,
        "messages": [{
            "message_id": "msg-1",
            "conversation_id": "conversation-a",
            "content": "legacy",
            "seq": 2,
        }],
        "tasks": [
            {
                "task_id": "first",
                "conversation_id": "conversation-a",
                "title": "first",
                "blocks": ["second"],
                "seq": 4,
            },
            {
                "task_id": "second",
                "conversation_id": "conversation-a",
                "title": "second",
                "seq": 5,
            },
        ],
        "teams": [],
    }), encoding="utf-8")

    store = FileSwarmStore(root)
    FileSwarmStore(root)

    assert store.list_messages(conversation_id="conversation-a")[0]["content"] == "legacy"
    assert store.get_task("second")["blocked_by"] == ["first"]  # type: ignore[index]
    assert store.append_message({"conversation_id": "conversation-a"})["seq"] == 8


def test_legacy_import_records_verified_source_counts_and_checksum(tmp_path) -> None:
    root = tmp_path / "swarm"
    root.mkdir()
    source = root / "conversation-a.json"
    source.write_text(json.dumps({
        "messages": [{"message_id": "msg-1", "content": "legacy"}],
        "tasks": [{
            "task_id": "task-1",
            "title": "legacy task",
            "outputs": [{"output_id": "output-1", "content": "legacy output"}],
        }],
        "teams": [{"team_id": "team-1", "team_name": "legacy team"}],
    }), encoding="utf-8")

    store = FileSwarmStore(root)
    report = store.get_legacy_migration_report()

    assert report is not None
    assert report["verified"] is True
    assert report["totals"] == {
        "messages": 1,
        "tasks": 1,
        "task_outputs": 1,
        "teams": 1,
    }
    assert report["sources"] == [{
        "path": "conversation-a.json",
        "sha256": sha256(source.read_bytes()).hexdigest(),
        "canonical_sha256": report["sources"][0]["canonical_sha256"],
        "counts": {
            "messages": 1,
            "tasks": 1,
            "task_outputs": 1,
            "teams": 1,
        },
    }]
    assert len(report["canonical_sha256"]) == 64
    assert len(report["sources"][0]["canonical_sha256"]) == 64
    assert FileSwarmStore(root).get_legacy_migration_report() == report


def test_existing_migration_marker_backfills_missing_report(tmp_path) -> None:
    root = tmp_path / "swarm"
    store = FileSwarmStore(root)
    with sqlite3.connect(root / "swarm.sqlite3") as connection:
        connection.execute(
            "DELETE FROM metadata WHERE key = 'legacy_json_import_report_v1'"
        )
        connection.commit()

    restored = FileSwarmStore(root)

    assert restored.get_legacy_migration_report() == {
        "canonical_sha256": sha256(b"[]").hexdigest(),
        "completed_at": restored.get_legacy_migration_report()["completed_at"],
        "sources": [],
        "totals": {
            "messages": 0,
            "tasks": 0,
            "task_outputs": 0,
            "teams": 0,
        },
        "verified": True,
        "version": 1,
    }


def test_legacy_duplicate_task_keeps_outputs_from_each_source(tmp_path) -> None:
    root = tmp_path / "swarm"
    root.mkdir()
    shared_task = {
        "task_id": "task-1",
        "title": "shared task",
        "conversation_id": "conversation-1",
        "created_at": 100,
        "updated_at": 100,
        "seq": 1,
    }
    (root / "a.json").write_text(json.dumps({
        "tasks": [{
            **shared_task,
            "outputs": [{"output_id": "output-a", "content": "first", "created_at": 101}],
        }],
    }), encoding="utf-8")
    (root / "b.json").write_text(json.dumps({
        "tasks": [{
            **shared_task,
            "outputs": [{"output_id": "output-b", "content": "second", "created_at": 102}],
        }],
    }), encoding="utf-8")

    store = FileSwarmStore(root)
    task = store.get_task("task-1")

    assert task is not None
    assert [output["output_id"] for output in task["outputs"]] == ["output-a", "output-b"]
    assert store.get_legacy_migration_report()["totals"]["task_outputs"] == 2


def test_malformed_legacy_json_rolls_back_without_migration_marker(tmp_path) -> None:
    root = tmp_path / "swarm"
    root.mkdir()
    path = root / "broken.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(RuntimeError, match="broken.json"):
        FileSwarmStore(root)

    with sqlite3.connect(root / "swarm.sqlite3") as connection:
        marker = connection.execute(
            "SELECT value FROM metadata WHERE key = 'legacy_json_import_v1'"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert marker is None
    assert count == 0


def test_two_store_instances_do_not_lose_concurrent_writes(tmp_path) -> None:
    root = tmp_path / "swarm"
    stores = [FileSwarmStore(root), FileSwarmStore(root)]

    def write(index: int) -> None:
        stores[index % 2].append_message({
            "conversation_id": "shared",
            "content": str(index),
        })

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    messages = stores[0].list_messages(conversation_id="shared", limit=100)
    assert len(messages) == 40
    assert [message["seq"] for message in messages] == list(range(1, 41))
    with sqlite3.connect(root / "swarm.sqlite3") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_wal_diagnostics_includes_file_sizes_and_telemetry(tmp_path) -> None:
    root = tmp_path / "swarm"
    store = FileSwarmStore(root)
    store.append_message({
        "conversation_id": "conv-diagnostics",
        "content": "diagnostic test",
    })
    store.create_task({
        "conversation_id": "conv-diagnostics",
        "title": "diag task",
        "assignee": "worker",
    })

    diag = store.wal_diagnostics()

    assert diag["journal_mode"] == "wal"
    assert diag["busy_timeout_ms"] == 5000
    assert "file_sizes" in diag
    assert diag["file_sizes"]["db_bytes"] > 0
    # WAL file may or may not exist depending on checkpoint timing, but the
    # key must be present.
    assert "wal_bytes" in diag["file_sizes"]
    assert "shm_bytes" in diag["file_sizes"]
    assert "freelist_pages" in diag
    assert isinstance(diag["freelist_pages"], int)
    assert "telemetry" in diag
    assert diag["telemetry"]["write_count"] >= 2  # at least append_message + create_task
    assert diag["telemetry"]["busy_retries"] >= 0
    assert diag["telemetry"]["migration_duration_ms"] >= 0.0
    assert diag["table_row_counts"]["messages"] == 1
    assert diag["table_row_counts"]["tasks"] == 1

