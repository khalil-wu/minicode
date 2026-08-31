from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.memory.job_store import MemoryJobStore


def _store(tmp_path: Path) -> MemoryJobStore:
    return MemoryJobStore(tmp_path / "memory" / "memories_1.sqlite3")


def _claim_stage1(
    store: MemoryJobStore,
    thread_id: str,
    source_revision: int,
    *,
    now: int,
):
    return store.claim_stage1(
        thread_id=thread_id,
        source_revision=source_revision,
        worker_id=f"worker-{thread_id}",
        lease_seconds=60,
        retry_limit=3,
        max_running_jobs=4,
        now=now,
    )


def _complete_stage1(
    store: MemoryJobStore,
    claim,
    *,
    source_updated_at: int,
    now: int,
) -> None:
    assert store.complete_stage1(
        claim,
        raw_memory=f"memory-{claim.job_key}-{claim.input_revision}",
        rollout_summary=f"summary-{claim.job_key}-{claim.input_revision}",
        rollout_slug=None,
        source_updated_at=source_updated_at,
        now=now,
    )


def test_stage1_uses_durable_revision_when_updates_share_one_second(tmp_path: Path) -> None:
    store = _store(tmp_path)
    updated_at = 1_800_000_000

    first = _claim_stage1(store, "thread-a", 1, now=100)
    assert first is not None
    _complete_stage1(store, first, source_updated_at=updated_at, now=101)

    second = _claim_stage1(store, "thread-a", 2, now=102)
    assert second is not None
    _complete_stage1(store, second, source_updated_at=updated_at, now=103)

    outputs = store.list_stage1_outputs(
        limit=10,
        max_unused_days=30,
        now=updated_at + 1,
    )
    assert len(outputs) == 1
    assert outputs[0].source_revision == 2
    assert outputs[0].source_updated_at == updated_at
    assert _claim_stage1(store, "thread-a", 2, now=104) is None


def test_phase2_catalog_revision_revokes_running_owner_on_new_output(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    first = _claim_stage1(store, "thread-a", 1, now=100)
    assert first is not None
    _complete_stage1(store, first, source_updated_at=1_800_000_000, now=101)

    phase2 = store.claim_phase2(
        worker_id="phase2-a",
        lease_seconds=60,
        retry_limit=3,
        success_cooldown_seconds=0,
        now=102,
    )
    assert phase2 is not None
    assert phase2.input_revision == 1

    second = _claim_stage1(store, "thread-b", 1, now=103)
    assert second is not None
    _complete_stage1(store, second, source_updated_at=1_800_000_000, now=104)

    assert not store.owns_phase2(phase2)
    assert not store.complete_phase2(phase2, [], now=105)

    replacement = store.claim_phase2(
        worker_id="phase2-b",
        lease_seconds=60,
        retry_limit=3,
        success_cooldown_seconds=0,
        now=106,
    )
    assert replacement is not None
    assert replacement.input_revision == 2
    assert store.complete_phase2(replacement, [], now=107)


def test_v1_timestamp_watermarks_migrate_without_blocking_revisions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "memory" / "memories_1.sqlite3"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE stage1_outputs (
            thread_id TEXT PRIMARY KEY,
            source_updated_at INTEGER NOT NULL,
            raw_memory TEXT NOT NULL,
            rollout_summary TEXT NOT NULL,
            rollout_slug TEXT,
            generated_at INTEGER NOT NULL,
            usage_count INTEGER,
            last_usage INTEGER,
            selected_for_phase2 INTEGER NOT NULL DEFAULT 0,
            selected_for_phase2_source_updated_at INTEGER
        );
        CREATE TABLE jobs (
            kind TEXT NOT NULL,
            job_key TEXT NOT NULL,
            status TEXT NOT NULL,
            worker_id TEXT,
            ownership_token TEXT,
            started_at INTEGER,
            finished_at INTEGER,
            lease_until INTEGER,
            retry_at INTEGER,
            retry_remaining INTEGER NOT NULL,
            last_error TEXT,
            input_watermark INTEGER,
            last_success_watermark INTEGER,
            PRIMARY KEY (kind, job_key)
        );
        INSERT INTO stage1_outputs VALUES (
            'thread-a', 1800000000, 'old-memory', 'old-summary', NULL,
            100, NULL, NULL, 1, 1800000000
        );
        INSERT INTO jobs VALUES (
            'memory_stage1', 'thread-a', 'succeeded', NULL, NULL,
            90, 100, NULL, NULL, 3, NULL, 1800000000, 1800000000
        );
        """
    )
    connection.close()

    store = MemoryJobStore(db_path)
    outputs = store.list_stage1_outputs(
        limit=10,
        max_unused_days=30,
        now=1_800_000_001,
    )
    assert len(outputs) == 1
    assert outputs[0].source_revision == -1
    assert outputs[0].source_updated_at == 1_800_000_000
    assert outputs[0].selected_for_phase2_source_revision == -1

    claim = _claim_stage1(store, "thread-a", 0, now=101)
    assert claim is not None
    _complete_stage1(
        store,
        claim,
        source_updated_at=1_800_000_000,
        now=102,
    )

    connection = sqlite3.connect(db_path)
    output_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(stage1_outputs)")
    }
    job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()

    assert "source_revision" in output_columns
    assert "selected_for_phase2_source_revision" in output_columns
    assert "input_revision" in job_columns
    assert "input_watermark" not in job_columns
    assert schema_version == 2
