"""Persistent MiniCode state machine for two-phase memory generation."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from filelock import FileLock, Timeout as FileLockTimeout


STAGE1_JOB_KIND = "memory_stage1"
PHASE2_JOB_KIND = "memory_consolidate_global"
PHASE2_JOB_KEY = "global"
MEMORY_DB_NAME = "memories_1.sqlite3"


@dataclass(frozen=True)
class JobClaim:
    kind: str
    job_key: str
    ownership_token: str
    input_watermark: int


@dataclass(frozen=True)
class Stage1Output:
    thread_id: str
    source_updated_at: int
    raw_memory: str
    rollout_summary: str
    rollout_slug: str | None
    generated_at: int
    usage_count: int | None = None
    last_usage: int | None = None
    selected_for_phase2: bool = False
    selected_for_phase2_source_updated_at: int | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS stage1_outputs (
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

CREATE TABLE IF NOT EXISTS jobs (
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
"""


class MemoryJobStore:
    """SQLite job store with atomic claims and ownership-token commits.

    Connections are intentionally short lived. ``memory.reset`` swaps the whole
    memory directory; a worker holding an old token then opens the new database
    and cannot commit into the reset generation.
    """

    def __init__(self, path: Path | str, *, reset_lock: FileLock | None = None) -> None:
        self.path = Path(path)
        self._reset_lock = reset_lock or FileLock(
            self.path.parent / f".{self.path.name}.reset.lock"
        )

    @contextmanager
    def _connect(self):
        """Open the DB inside the same reset lock as phase2/file writes."""

        try:
            with self._reset_lock.acquire(timeout=5.0):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("PRAGMA busy_timeout = 5000")
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(_SCHEMA)
                    yield connection
                finally:
                    connection.close()
        except FileLockTimeout as exc:
            raise TimeoutError(f"Timed out waiting for memory reset lock: {self._reset_lock.lock_file}") from exc

    @staticmethod
    def _now(now: int | None) -> int:
        return int(time.time()) if now is None else int(now)

    def claim_stage1(
        self,
        *,
        thread_id: str,
        source_updated_at: int,
        worker_id: str,
        lease_seconds: int,
        retry_limit: int,
        max_running_jobs: int,
        now: int | None = None,
    ) -> JobClaim | None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            output = connection.execute(
                "SELECT source_updated_at FROM stage1_outputs WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT * FROM jobs WHERE kind = ? AND job_key = ?",
                (STAGE1_JOB_KIND, thread_id),
            ).fetchone()

            if output is not None and int(output["source_updated_at"]) >= source_updated_at:
                connection.commit()
                return None
            if job is not None and int(job["last_success_watermark"] or 0) >= source_updated_at:
                connection.commit()
                return None

            previous_watermark = int(job["input_watermark"] or 0) if job is not None else 0
            source_advanced = source_updated_at > previous_watermark
            if job is not None and not source_advanced:
                if str(job["status"]) == "running" and int(job["lease_until"] or 0) > timestamp:
                    connection.commit()
                    return None
                if str(job["status"]) == "error":
                    if int(job["retry_remaining"] or 0) <= 0:
                        connection.commit()
                        return None
                    if int(job["retry_at"] or 0) > timestamp:
                        connection.commit()
                        return None

            running = connection.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE kind = ? AND status = 'running' AND lease_until > ?
                """,
                (STAGE1_JOB_KIND, timestamp),
            ).fetchone()[0]
            if int(running) >= max(1, int(max_running_jobs)):
                connection.commit()
                return None

            token = uuid.uuid4().hex
            retry_remaining = (
                max(1, int(retry_limit))
                if source_advanced or job is None
                else int(job["retry_remaining"] or 0)
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    kind, job_key, status, worker_id, ownership_token,
                    started_at, finished_at, lease_until, retry_at,
                    retry_remaining, last_error, input_watermark,
                    last_success_watermark
                ) VALUES (?, ?, 'running', ?, ?, ?, NULL, ?, NULL, ?, NULL, ?, ?)
                ON CONFLICT(kind, job_key) DO UPDATE SET
                    status = 'running',
                    worker_id = excluded.worker_id,
                    ownership_token = excluded.ownership_token,
                    started_at = excluded.started_at,
                    finished_at = NULL,
                    lease_until = excluded.lease_until,
                    retry_at = NULL,
                    retry_remaining = excluded.retry_remaining,
                    last_error = NULL,
                    input_watermark = excluded.input_watermark
                """,
                (
                    STAGE1_JOB_KIND,
                    thread_id,
                    worker_id,
                    token,
                    timestamp,
                    timestamp + max(1, int(lease_seconds)),
                    retry_remaining,
                    source_updated_at,
                    int(job["last_success_watermark"] or 0) if job is not None else None,
                ),
            )
            connection.commit()
            return JobClaim(STAGE1_JOB_KIND, thread_id, token, source_updated_at)

    def complete_stage1(
        self,
        claim: JobClaim,
        *,
        raw_memory: str,
        rollout_summary: str,
        rollout_slug: str | None,
        now: int | None = None,
    ) -> bool:
        timestamp = self._now(now)
        raw_memory = raw_memory.strip()
        rollout_summary = rollout_summary.strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._owns(connection, claim):
                connection.commit()
                return False

            if raw_memory and rollout_summary:
                connection.execute(
                    """
                    INSERT INTO stage1_outputs (
                        thread_id, source_updated_at, raw_memory, rollout_summary,
                        rollout_slug, generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        source_updated_at = excluded.source_updated_at,
                        raw_memory = excluded.raw_memory,
                        rollout_summary = excluded.rollout_summary,
                        rollout_slug = excluded.rollout_slug,
                        generated_at = excluded.generated_at
                    WHERE excluded.source_updated_at >= stage1_outputs.source_updated_at
                    """,
                    (
                        claim.job_key,
                        claim.input_watermark,
                        raw_memory,
                        rollout_summary,
                        rollout_slug,
                        timestamp,
                    ),
                )
                self._enqueue_phase2_tx(connection, claim.input_watermark, timestamp)
            else:
                previous = connection.execute(
                    "SELECT selected_for_phase2 FROM stage1_outputs WHERE thread_id = ?",
                    (claim.job_key,),
                ).fetchone()
                connection.execute(
                    "DELETE FROM stage1_outputs WHERE thread_id = ?",
                    (claim.job_key,),
                )
                if previous is not None and bool(previous["selected_for_phase2"]):
                    self._enqueue_phase2_tx(connection, claim.input_watermark, timestamp)

            connection.execute(
                """
                UPDATE jobs SET
                    status = 'succeeded', worker_id = NULL, ownership_token = NULL,
                    finished_at = ?, lease_until = NULL, retry_at = NULL,
                    last_error = NULL, last_success_watermark = input_watermark
                WHERE kind = ? AND job_key = ? AND ownership_token = ?
                """,
                (timestamp, claim.kind, claim.job_key, claim.ownership_token),
            )
            connection.commit()
            return True

    def fail_stage1(
        self,
        claim: JobClaim,
        error: str,
        *,
        retry_delay_seconds: int,
        now: int | None = None,
    ) -> bool:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._owns(connection, claim):
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE jobs SET
                    status = 'error', worker_id = NULL, ownership_token = NULL,
                    finished_at = ?, lease_until = NULL, retry_at = ?,
                    retry_remaining = MAX(retry_remaining - 1, 0), last_error = ?
                WHERE kind = ? AND job_key = ? AND ownership_token = ?
                """,
                (
                    timestamp,
                    timestamp + max(1, int(retry_delay_seconds)),
                    str(error)[:1000],
                    claim.kind,
                    claim.job_key,
                    claim.ownership_token,
                ),
            )
            connection.commit()
            return True

    def remove_thread_output(
        self,
        thread_id: str,
        *,
        watermark: int | None = None,
        now: int | None = None,
    ) -> bool:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT source_updated_at, selected_for_phase2 FROM stage1_outputs WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            connection.execute(
                "DELETE FROM stage1_outputs WHERE thread_id = ?",
                (thread_id,),
            )
            connection.execute(
                "DELETE FROM jobs WHERE kind = ? AND job_key = ?",
                (STAGE1_JOB_KIND, thread_id),
            )
            if previous is not None:
                next_watermark = max(
                    int(previous["source_updated_at"] or 0) + 1,
                    int(watermark or 0),
                    timestamp,
                )
                self._enqueue_phase2_tx(connection, next_watermark, timestamp)
            connection.commit()
            return previous is not None

    def prune_unselected_outputs(
        self,
        *,
        older_than: int,
        limit: int | None,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT thread_id FROM stage1_outputs
                WHERE selected_for_phase2 = 0
                  AND COALESCE(last_usage, source_updated_at) < ?
                ORDER BY COALESCE(last_usage, source_updated_at) ASC
                LIMIT ?
                """,
                (int(older_than), max(0, int(limit))),
            ).fetchall()
            ids = [str(row["thread_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM stage1_outputs WHERE thread_id IN ({placeholders})",
                    ids,
                )
                connection.execute(
                    f"DELETE FROM jobs WHERE kind = ? AND job_key IN ({placeholders})",
                    [STAGE1_JOB_KIND, *ids],
                )
            connection.commit()
            return len(ids)

    def enqueue_phase2(self, input_watermark: int, *, now: int | None = None) -> None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enqueue_phase2_tx(connection, int(input_watermark), timestamp)
            connection.commit()

    def claim_phase2(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        retry_limit: int,
        success_cooldown_seconds: int,
        now: int | None = None,
    ) -> JobClaim | None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM jobs WHERE kind = ? AND job_key = ?",
                (PHASE2_JOB_KIND, PHASE2_JOB_KEY),
            ).fetchone()
            if job is None:
                token = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO jobs (
                        kind, job_key, status, worker_id, ownership_token,
                        started_at, finished_at, lease_until, retry_at,
                        retry_remaining, last_error, input_watermark,
                        last_success_watermark
                    ) VALUES (?, ?, 'running', ?, ?, ?, NULL, ?, NULL, ?, NULL, 0, 0)
                    """,
                    (
                        PHASE2_JOB_KIND,
                        PHASE2_JOB_KEY,
                        worker_id,
                        token,
                        timestamp,
                        timestamp + max(1, int(lease_seconds)),
                        max(1, int(retry_limit)),
                    ),
                )
                connection.commit()
                return JobClaim(PHASE2_JOB_KIND, PHASE2_JOB_KEY, token, 0)
            input_watermark = int(job["input_watermark"] or 0)
            if str(job["status"]) == "running" and int(job["lease_until"] or 0) > timestamp:
                connection.commit()
                return None
            if int(job["retry_at"] or 0) > timestamp:
                connection.commit()
                return None
            finished_at = int(job["finished_at"] or 0)
            if (
                not job["last_error"]
                and finished_at + max(0, int(success_cooldown_seconds)) > timestamp
            ):
                connection.commit()
                return None

            token = uuid.uuid4().hex
            connection.execute(
                """
                UPDATE jobs SET
                    status = 'running', worker_id = ?, ownership_token = ?,
                    started_at = ?, finished_at = NULL, lease_until = ?,
                    retry_at = NULL,
                    retry_remaining = CASE
                        WHEN retry_remaining <= 0 THEN ? ELSE retry_remaining END,
                    last_error = NULL
                WHERE kind = ? AND job_key = ?
                """,
                (
                    worker_id,
                    token,
                    timestamp,
                    timestamp + max(1, int(lease_seconds)),
                    max(1, int(retry_limit)),
                    PHASE2_JOB_KIND,
                    PHASE2_JOB_KEY,
                ),
            )
            connection.commit()
            return JobClaim(PHASE2_JOB_KIND, PHASE2_JOB_KEY, token, input_watermark)

    def owns_phase2(self, claim: JobClaim) -> bool:
        with self._connect() as connection:
            return self._owns(connection, claim)

    def heartbeat_phase2(
        self,
        claim: JobClaim,
        *,
        lease_seconds: int,
        now: int | None = None,
    ) -> bool:
        timestamp = self._now(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET lease_until = ?
                WHERE kind = ? AND job_key = ? AND status = 'running'
                  AND ownership_token = ?
                """,
                (
                    timestamp + max(1, int(lease_seconds)),
                    claim.kind,
                    claim.job_key,
                    claim.ownership_token,
                ),
            )
            return cursor.rowcount == 1

    def list_stage1_outputs(
        self,
        *,
        limit: int,
        max_unused_days: int,
        now: int | None = None,
    ) -> list[Stage1Output]:
        timestamp = self._now(now)
        cutoff = timestamp - max(0, int(max_unused_days)) * 86_400
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM stage1_outputs
                WHERE raw_memory != '' AND rollout_summary != ''
                  AND (
                    (usage_count IS NULL AND source_updated_at >= ?)
                    OR (usage_count IS NOT NULL AND COALESCE(last_usage, 0) >= ?)
                  )
                ORDER BY COALESCE(usage_count, 0) DESC,
                         COALESCE(last_usage, source_updated_at) DESC,
                         source_updated_at DESC,
                         thread_id DESC
                LIMIT ?
                """,
                (cutoff, cutoff, -1 if limit is None else max(0, int(limit))),
            ).fetchall()
        return [self._stage1_from_row(row) for row in rows]

    def complete_phase2(
        self,
        claim: JobClaim,
        selected_outputs: Iterable[Stage1Output],
        *,
        completion_watermark: int,
        now: int | None = None,
    ) -> bool:
        timestamp = self._now(now)
        selected = list(selected_outputs)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._owns(connection, claim):
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE stage1_outputs SET
                    selected_for_phase2 = 0,
                    selected_for_phase2_source_updated_at = NULL
                WHERE selected_for_phase2 != 0
                   OR selected_for_phase2_source_updated_at IS NOT NULL
                """
            )
            for output in selected:
                connection.execute(
                    """
                    UPDATE stage1_outputs SET
                        selected_for_phase2 = 1,
                        selected_for_phase2_source_updated_at = ?
                    WHERE thread_id = ? AND source_updated_at = ?
                    """,
                    (
                        output.source_updated_at,
                        output.thread_id,
                        output.source_updated_at,
                    ),
                )
            connection.execute(
                """
                UPDATE jobs SET
                    status = 'succeeded', worker_id = NULL, ownership_token = NULL,
                    finished_at = ?, lease_until = NULL, retry_at = NULL,
                    last_error = NULL, last_success_watermark = ?
                WHERE kind = ? AND job_key = ? AND ownership_token = ?
                """,
                (
                    timestamp,
                    int(completion_watermark),
                    claim.kind,
                    claim.job_key,
                    claim.ownership_token,
                ),
            )
            connection.commit()
            return True

    def fail_phase2(
        self,
        claim: JobClaim,
        error: str,
        *,
        retry_delay_seconds: int,
        now: int | None = None,
    ) -> bool:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Phase-2 failure is a state transition just like completion.  A
            # stale worker must match the claimed input watermark as well as
            # its token; otherwise it can consume the retry budget for a
            # newer phase-2 generation after the row was advanced in place.
            if not self._owns(connection, claim):
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE jobs SET
                    status = 'error', worker_id = NULL, ownership_token = NULL,
                    finished_at = ?, lease_until = NULL, retry_at = ?,
                    retry_remaining = MAX(retry_remaining - 1, 0), last_error = ?
                WHERE kind = ? AND job_key = ? AND ownership_token = ?
                """,
                (
                    timestamp,
                    timestamp + max(1, int(retry_delay_seconds)),
                    str(error)[:1000],
                    claim.kind,
                    claim.job_key,
                    claim.ownership_token,
                ),
            )
            connection.commit()
            return True

    def record_stage1_output_usage(
        self,
        thread_ids: Iterable[str],
        *,
        now: int | None = None,
    ) -> int:
        timestamp = self._now(now)
        unique = list(dict.fromkeys(str(item) for item in thread_ids if str(item)))
        if not unique:
            return 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = 0
            for thread_id in unique:
                cursor = connection.execute(
                    """
                    UPDATE stage1_outputs SET
                        usage_count = COALESCE(usage_count, 0) + 1,
                        last_usage = ?
                    WHERE thread_id = ?
                    """,
                    (timestamp, thread_id),
                )
                updated += cursor.rowcount
            connection.commit()
            return updated

    @staticmethod
    def _owns(connection: sqlite3.Connection, claim: JobClaim) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM jobs
            WHERE kind = ? AND job_key = ? AND status = 'running'
              AND ownership_token = ? AND input_watermark = ?
            """,
            (
                claim.kind,
                claim.job_key,
                claim.ownership_token,
                claim.input_watermark,
            ),
        ).fetchone()
        return row is not None

    @staticmethod
    def _owns_token(connection: sqlite3.Connection, claim: JobClaim) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM jobs
            WHERE kind = ? AND job_key = ? AND status = 'running'
              AND ownership_token = ?
            """,
            (claim.kind, claim.job_key, claim.ownership_token),
        ).fetchone()
        return row is not None

    @staticmethod
    def _enqueue_phase2_tx(
        connection: sqlite3.Connection,
        input_watermark: int,
        timestamp: int,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM jobs WHERE kind = ? AND job_key = ?",
            (PHASE2_JOB_KIND, PHASE2_JOB_KEY),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO jobs (
                    kind, job_key, status, retry_remaining, input_watermark
                ) VALUES (?, ?, 'pending', 3, ?)
                """,
                (PHASE2_JOB_KIND, PHASE2_JOB_KEY, int(input_watermark)),
            )
            return
        current_watermark = int(row["input_watermark"] or 0)
        next_watermark = (
            int(input_watermark)
            if int(input_watermark) > current_watermark
            else current_watermark + 1
        )
        if str(row["status"]) == "running":
            connection.execute(
                """
                UPDATE jobs SET input_watermark = ?
                WHERE kind = ? AND job_key = ?
                """,
                (next_watermark, PHASE2_JOB_KIND, PHASE2_JOB_KEY),
            )
        else:
            connection.execute(
                """
                UPDATE jobs SET status = 'pending', input_watermark = ?,
                    retry_at = CASE WHEN retry_at > ? THEN retry_at ELSE NULL END
                WHERE kind = ? AND job_key = ?
                """,
                (next_watermark, timestamp, PHASE2_JOB_KIND, PHASE2_JOB_KEY),
            )

    @staticmethod
    def _stage1_from_row(row: sqlite3.Row) -> Stage1Output:
        return Stage1Output(
            thread_id=str(row["thread_id"]),
            source_updated_at=int(row["source_updated_at"]),
            raw_memory=str(row["raw_memory"]),
            rollout_summary=str(row["rollout_summary"]),
            rollout_slug=str(row["rollout_slug"]) if row["rollout_slug"] is not None else None,
            generated_at=int(row["generated_at"]),
            usage_count=int(row["usage_count"]) if row["usage_count"] is not None else None,
            last_usage=int(row["last_usage"]) if row["last_usage"] is not None else None,
            selected_for_phase2=bool(row["selected_for_phase2"]),
            selected_for_phase2_source_updated_at=(
                int(row["selected_for_phase2_source_updated_at"])
                if row["selected_for_phase2_source_updated_at"] is not None
                else None
            ),
        )
