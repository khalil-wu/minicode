"""Durable SQLite store for the shared multi-agent task board."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
from backend.agent.swarm_migrations import (
    MIGRATION_REPORT_KEY,
    LegacySwarmMigrator,
    SchemaMigration,
    SchemaMigrationRunner,
)

SCHEMA_VERSION = 5
SCHEMA_MIGRATIONS = (
    SchemaMigration(1, """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scope_counters (
            conversation_id TEXT PRIMARY KEY,
            high_water INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_scope_seq ON messages(conversation_id, seq);
        CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
        CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_id);
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            assignee TEXT NOT NULL,
            status TEXT NOT NULL,
            team_name TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_scope_updated ON tasks(conversation_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_team ON tasks(team_name);
        CREATE TABLE IF NOT EXISTS task_outputs (
            output_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_outputs_task_seq ON task_outputs(task_id, seq);
        CREATE TABLE IF NOT EXISTS task_dependencies (
            blocker_task_id TEXT NOT NULL,
            blocked_task_id TEXT NOT NULL,
            PRIMARY KEY (blocker_task_id, blocked_task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_task_dependencies_blocked ON task_dependencies(blocked_task_id);
        CREATE TABLE IF NOT EXISTS teams (
            team_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            team_name TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (conversation_id, team_name)
        );
        CREATE INDEX IF NOT EXISTS idx_teams_scope_updated ON teams(conversation_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_teams_name ON teams(team_name);
    """),
    SchemaMigration(2, """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            parent_run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation ON agent_runs(conversation_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);
    """),
    SchemaMigration(3, """
        CREATE TABLE IF NOT EXISTS subagent_runs (
            subagent_id TEXT PRIMARY KEY,
            parent_run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent ON subagent_runs(parent_run_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_task ON subagent_runs(task_id);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_workflow ON subagent_runs(workflow_id);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_status ON subagent_runs(status);
        CREATE TABLE IF NOT EXISTS subagent_results (
            subagent_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            completed_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_subagent_results_status ON subagent_results(status);
    """),
    SchemaMigration(4, """
        ALTER TABLE agent_runs ADD COLUMN owner_token TEXT NOT NULL DEFAULT '';
        ALTER TABLE subagent_runs ADD COLUMN owner_token TEXT NOT NULL DEFAULT '';
        ALTER TABLE subagent_runs ADD COLUMN agent_path TEXT NOT NULL DEFAULT '';
        ALTER TABLE subagent_runs ADD COLUMN mailbox_epoch INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE subagent_results ADD COLUMN owner_token TEXT NOT NULL DEFAULT '';
        ALTER TABLE subagent_results ADD COLUMN agent_path TEXT NOT NULL DEFAULT '';
        ALTER TABLE subagent_results ADD COLUMN mailbox_epoch INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_agent_runs_owner ON agent_runs(owner_token);
        CREATE INDEX IF NOT EXISTS idx_subagent_runs_owner ON subagent_runs(owner_token);
        CREATE TABLE IF NOT EXISTS runtime_leases (
            runtime_instance_id TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL UNIQUE,
            process_id INTEGER NOT NULL,
            process_start_identity TEXT NOT NULL,
            heartbeat_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runtime_leases_expiry ON runtime_leases(expires_at);
    """),
    SchemaMigration(5, """
        CREATE TABLE IF NOT EXISTS mailbox_deliveries (
            message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
            participant_id TEXT NOT NULL,
            mailbox_epoch INTEGER NOT NULL,
            status TEXT NOT NULL,
            claim_owner TEXT NOT NULL DEFAULT '',
            claim_token TEXT NOT NULL DEFAULT '',
            claimed_at INTEGER NOT NULL DEFAULT 0,
            lease_expires_at INTEGER NOT NULL DEFAULT 0,
            acked_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (message_id, participant_id, mailbox_epoch)
        );
        CREATE INDEX IF NOT EXISTS idx_mailbox_deliveries_claim
            ON mailbox_deliveries(participant_id, mailbox_epoch, status, lease_expires_at);
        CREATE INDEX IF NOT EXISTS idx_mailbox_deliveries_owner
            ON mailbox_deliveries(claim_owner, claim_token);
    """),
)


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


logger = logging.getLogger(__name__)


class FileSwarmStore:
    """SQLite/WAL task, message, and team store.

    The historical class name remains the public API so callers do not need a
    migration layer. Each operation uses a short connection and writes acquire
    SQLite's transaction lock with ``BEGIN IMMEDIATE``.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "swarm.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        # Telemetry counters (process-lifetime, not persisted).
        self._busy_retries: int = 0
        self._write_count: int = 0
        self._migration_duration_ms: float = 0.0
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        # synchronous is connection-scoped. Set it on every short-lived
        # connection instead of relying on SQLite's compile-time default.
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        self._write_count += 1
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                connection.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if attempt < max_retries and "locked" in str(exc).lower():
                    self._busy_retries += 1
                    logger.debug("SQLite busy retry %d: %s", attempt + 1, exc)
                    time.sleep(0.05 * (attempt + 1))
                    continue
                connection.rollback()
                raise
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            version = SchemaMigrationRunner(SCHEMA_MIGRATIONS).run(connection)
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"schema migration stopped at v{version}, expected v{SCHEMA_VERSION}")
            SchemaMigrationRunner.record_or_verify(connection, SCHEMA_VERSION)
            connection.commit()
        finally:
            connection.close()
        migration_start = time.monotonic()
        self._import_legacy_json()
        self._migration_duration_ms = (time.monotonic() - migration_start) * 1000.0

    def wal_diagnostics(self) -> dict[str, Any]:
        """Return WAL mode, checkpoint, file-size, and telemetry diagnostics.

        Useful when multi-agent coordination produces lock contention or
        stale-read symptoms.  Safe to call at any time; opens a read-only
        connection so it never blocks writers.
        """
        conn = self._connect()
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
            wal_checkpoint = conn.execute("PRAGMA wal_checkpoint").fetchone()
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()
            synchronous = conn.execute("PRAGMA synchronous").fetchone()
            page_size = conn.execute("PRAGMA page_size").fetchone()
            # Table row counts for a quick health snapshot
            tables = ["tasks", "agent_runs", "subagent_runs", "messages", "mailbox_deliveries", "teams"]
            counts: dict[str, int] = {}
            for t in tables:
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
                    counts[t] = int(row[0]) if row else 0
                except Exception:
                    counts[t] = -1
            # File sizes on disk
            db_size = self._file_size(self.path)
            wal_size = self._file_size(Path(str(self.path) + "-wal"))
            shm_size = self._file_size(Path(str(self.path) + "-shm"))
            # Freelist pages (wasted space that VACUUM would reclaim)
            freelist = conn.execute("PRAGMA freelist_count").fetchone()
            return {
                "journal_mode": str(journal_mode[0]) if journal_mode else "unknown",
                "wal_checkpoint": {
                    "busy": int(wal_checkpoint[0]) if wal_checkpoint else -1,
                    "log_frames": int(wal_checkpoint[1]) if wal_checkpoint else -1,
                    "checkpointed_frames": int(wal_checkpoint[2]) if wal_checkpoint else -1,
                },
                "busy_timeout_ms": int(busy_timeout[0]) if busy_timeout else -1,
                "synchronous": str(synchronous[0]) if synchronous else "unknown",
                "page_size": int(page_size[0]) if page_size else -1,
                "freelist_pages": int(freelist[0]) if freelist else -1,
                "file_sizes": {
                    "db_bytes": db_size,
                    "wal_bytes": wal_size,
                    "shm_bytes": shm_size,
                },
                "table_row_counts": counts,
                "telemetry": {
                    "write_count": self._write_count,
                    "busy_retries": self._busy_retries,
                    "migration_duration_ms": round(self._migration_duration_ms, 2),
                },
                "db_path": str(self.path),
            }
        finally:
            conn.close()

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def claim_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        requested_owner_token: str,
        process_id: int,
        process_start_identity: str,
        now_ms: int,
        ttl_ms: int,
    ) -> dict[str, Any]:
        """Claim an expiring runtime lease or reuse this process's lease.

        ``runtime_instance_id`` is stable within one MiniCode process. The
        opaque owner token is the fencing value stored on every mutable run.
        A live lease owned by a different process cannot be stolen.
        """
        instance_id = str(runtime_instance_id or "").strip()
        requested = str(requested_owner_token or "").strip()
        start_identity = str(process_start_identity or "").strip()
        if not instance_id or not requested:
            raise ValueError("runtime lease requires instance id and owner token")
        expires_at = int(now_ms) + max(1_000, int(ttl_ms))
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE runtime_instance_id = ?",
                (instance_id,),
            ).fetchone()
            if row is not None:
                same_process = bool(
                    start_identity
                    and int(row["process_id"]) == int(process_id)
                    and str(row["process_start_identity"] or "") == start_identity
                )
                if same_process:
                    owner_token = str(row["owner_token"])
                    connection.execute(
                        """
                        UPDATE runtime_leases
                        SET heartbeat_at = ?, expires_at = ?
                        WHERE runtime_instance_id = ? AND owner_token = ?
                        """,
                        (int(now_ms), expires_at, instance_id, owner_token),
                    )
                    return {
                        "acquired": True,
                        "reused": True,
                        "runtime_instance_id": instance_id,
                        "owner_token": owner_token,
                        "process_id": int(process_id),
                        "process_start_identity": start_identity,
                        "heartbeat_at": int(now_ms),
                        "expires_at": expires_at,
                    }
                if int(row["expires_at"]) > int(now_ms):
                    return {
                        "acquired": False,
                        "reused": False,
                        **dict(row),
                    }
            connection.execute(
                """
                INSERT INTO runtime_leases(
                    runtime_instance_id, owner_token, process_id,
                    process_start_identity, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(runtime_instance_id) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    process_id = excluded.process_id,
                    process_start_identity = excluded.process_start_identity,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                """,
                (
                    instance_id,
                    requested,
                    int(process_id),
                    start_identity,
                    int(now_ms),
                    expires_at,
                ),
            )
        return {
            "acquired": True,
            "reused": False,
            "runtime_instance_id": instance_id,
            "owner_token": requested,
            "process_id": int(process_id),
            "process_start_identity": start_identity,
            "heartbeat_at": int(now_ms),
            "expires_at": expires_at,
        }

    def heartbeat_runtime_lease(
        self,
        *,
        runtime_instance_id: str,
        owner_token: str,
        process_id: int,
        process_start_identity: str,
        now_ms: int,
        ttl_ms: int,
    ) -> bool:
        expires_at = int(now_ms) + max(1_000, int(ttl_ms))
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE runtime_instance_id = ? AND owner_token = ?
                  AND process_id = ? AND process_start_identity = ?
                """,
                (
                    int(now_ms),
                    expires_at,
                    str(runtime_instance_id or "").strip(),
                    str(owner_token or "").strip(),
                    int(process_id),
                    str(process_start_identity or "").strip(),
                ),
            )
            return cursor.rowcount == 1

    def list_runtime_leases(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_leases ORDER BY runtime_instance_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def release_runtime_lease(self, *, runtime_instance_id: str, owner_token: str) -> bool:
        with self._write() as connection:
            cursor = connection.execute(
                """
                DELETE FROM runtime_leases
                WHERE runtime_instance_id = ? AND owner_token = ?
                """,
                (
                    str(runtime_instance_id or "").strip(),
                    str(owner_token or "").strip(),
                ),
            )
            return cursor.rowcount == 1

    def upsert_agent_run(
        self,
        payload: dict[str, Any],
        *,
        expected_owner_token: str | None = None,
        allow_takeover_terminal: bool = False,
    ) -> dict[str, Any] | None:
        record = dict(payload)
        run_id = str(record.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("agent run requires run_id")
        owner_token = str(record.get("runtime_owner_token") or "").strip()
        with self._write() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_runs(
                    run_id, conversation_id, parent_run_id, task_id,
                    status, updated_at, payload_json, owner_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    parent_run_id = excluded.parent_run_id,
                    task_id = excluded.task_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json,
                    owner_token = excluded.owner_token
                WHERE ? IS NULL
                   OR agent_runs.owner_token = ?
                   OR (? = 1 AND agent_runs.status != 'running')
                """,
                (
                    run_id,
                    str(record.get("conversation_id") or ""),
                    str(record.get("parent_run_id") or ""),
                    str(record.get("task_id") or ""),
                    str(record.get("status") or "running"),
                    _epoch_ms(),
                    _json(record),
                    owner_token,
                    expected_owner_token,
                    str(expected_owner_token or ""),
                    1 if allow_takeover_terminal else 0,
                ),
            )
        return record if cursor.rowcount == 1 else None

    def list_agent_runs(
        self,
        *,
        conversation_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id = ?")
            values.append(conversation_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM agent_runs
                {where}
                ORDER BY updated_at DESC, run_id
                """,
                values,
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def upsert_subagent(
        self,
        payload: dict[str, Any],
        *,
        expected_owner_token: str | None = None,
        allow_takeover_terminal: bool = False,
    ) -> dict[str, Any] | None:
        record = dict(payload)
        subagent_id = str(record.get("subagent_id") or "").strip()
        if not subagent_id:
            raise ValueError("subagent run requires subagent_id")
        owner_token = str(record.get("runtime_owner_token") or "").strip()
        agent_path = str(record.get("agent_path") or "").strip()
        mailbox_epoch = max(0, int(record.get("mailbox_epoch") or 0))
        with self._write() as connection:
            cursor = connection.execute(
                """
                INSERT INTO subagent_runs(
                    subagent_id, parent_run_id, task_id, workflow_id,
                    status, updated_at, payload_json, owner_token,
                    agent_path, mailbox_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subagent_id) DO UPDATE SET
                    parent_run_id = excluded.parent_run_id,
                    task_id = excluded.task_id,
                    workflow_id = excluded.workflow_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json,
                    owner_token = excluded.owner_token,
                    agent_path = excluded.agent_path,
                    mailbox_epoch = excluded.mailbox_epoch
                WHERE ? IS NULL
                   OR subagent_runs.owner_token = ?
                   OR (? = 1 AND subagent_runs.status != 'running')
                """,
                (
                    subagent_id,
                    str(record.get("parent_run_id") or ""),
                    str(record.get("task_id") or ""),
                    str(record.get("workflow_id") or ""),
                    str(record.get("status") or "running"),
                    _epoch_ms(),
                    _json(record),
                    owner_token,
                    agent_path,
                    mailbox_epoch,
                    expected_owner_token,
                    str(expected_owner_token or ""),
                    1 if allow_takeover_terminal else 0,
                ),
            )
        return record if cursor.rowcount == 1 else None

    def list_subagents(
        self,
        *,
        parent_run_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if parent_run_id:
            clauses.append("parent_run_id = ?")
            values.append(parent_run_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM subagent_runs
                {where}
                ORDER BY updated_at DESC, subagent_id
                """,
                values,
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def upsert_subagent_result(
        self,
        payload: dict[str, Any],
        *,
        expected_owner_token: str | None = None,
        agent_path: str = "",
        mailbox_epoch: int = 0,
    ) -> dict[str, Any] | None:
        record = dict(payload)
        subagent_id = str(record.get("subagent_id") or "").strip()
        if not subagent_id:
            raise ValueError("subagent result requires subagent_id")
        owner_token = str(expected_owner_token or record.get("runtime_owner_token") or "").strip()
        expected_path = str(agent_path or record.get("agent_path") or "").strip()
        expected_epoch = max(0, int(mailbox_epoch or record.get("mailbox_epoch") or 0))
        with self._write() as connection:
            if expected_owner_token is not None:
                current = connection.execute(
                    """
                    SELECT 1 FROM subagent_runs
                    WHERE subagent_id = ? AND owner_token = ?
                      AND agent_path = ? AND mailbox_epoch = ?
                    """,
                    (subagent_id, owner_token, expected_path, expected_epoch),
                ).fetchone()
                if current is None:
                    return None
            cursor = connection.execute(
                """
                INSERT INTO subagent_results(
                    subagent_id, status, completed_at, payload_json,
                    owner_token, agent_path, mailbox_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subagent_id) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    payload_json = excluded.payload_json,
                    owner_token = excluded.owner_token,
                    agent_path = excluded.agent_path,
                    mailbox_epoch = excluded.mailbox_epoch
                """,
                (
                    subagent_id,
                    str(record.get("status") or ""),
                    int(record.get("completed_at") or _epoch_ms()),
                    _json(record),
                    owner_token,
                    expected_path,
                    expected_epoch,
                ),
            )
        return record if cursor.rowcount == 1 else None

    def get_subagent_result(self, subagent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM subagent_results WHERE subagent_id = ?",
                (subagent_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def delete_subagent_result(
        self,
        subagent_id: str,
        *,
        expected_owner_token: str | None = None,
    ) -> bool:
        with self._write() as connection:
            if expected_owner_token is not None:
                owner = connection.execute(
                    "SELECT owner_token FROM subagent_runs WHERE subagent_id = ?",
                    (subagent_id,),
                ).fetchone()
                if owner is None or str(owner["owner_token"] or "") != str(expected_owner_token):
                    return False
            cursor = connection.execute(
                "DELETE FROM subagent_results WHERE subagent_id = ?",
                (subagent_id,),
            )
            return cursor.rowcount > 0

    def get_legacy_migration_report(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (MIGRATION_REPORT_KEY,),
            ).fetchone()
        return json.loads(row["value"]) if row is not None else None

    def recover_runtime_state(
        self,
        *,
        interrupted_at: int,
        summary: str,
        current_instance_id: str,
        current_owner_token: str,
        current_process_id: int,
        current_process_start_identity: str,
        active_owner_tokens: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        with self._write() as connection:
            run_rows = connection.execute(
                "SELECT run_id, owner_token, payload_json FROM agent_runs"
            ).fetchall()
            runs: list[dict[str, Any]] = []
            for row in run_rows:
                payload = json.loads(row["payload_json"])
                previous_owner_token = str(row["owner_token"] or "")
                owner_is_active = bool(
                    previous_owner_token and previous_owner_token in active_owner_tokens
                )
                if payload.get("status") == "running" and not owner_is_active:
                    payload.update({
                        "status": "interrupted",
                        "phase": "final",
                        "completed_at": interrupted_at,
                        "summary": summary,
                        "runtime_instance_id": current_instance_id,
                        "runtime_process_id": int(current_process_id),
                        "runtime_process_start_identity": current_process_start_identity,
                        "runtime_owner_token": current_owner_token,
                    })
                    cursor = connection.execute(
                        """
                        UPDATE agent_runs
                        SET status = 'interrupted', updated_at = ?, payload_json = ?,
                            owner_token = ?
                        WHERE run_id = ? AND status = 'running' AND owner_token = ?
                        """,
                        (
                            interrupted_at,
                            _json(payload),
                            current_owner_token,
                            row["run_id"],
                            previous_owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        latest = connection.execute(
                            "SELECT payload_json FROM agent_runs WHERE run_id = ?",
                            (row["run_id"],),
                        ).fetchone()
                        payload = json.loads(latest["payload_json"])
                runs.append(payload)

            subagent_rows = connection.execute(
                """
                SELECT subagent_id, owner_token, agent_path, mailbox_epoch, payload_json
                FROM subagent_runs
                """
            ).fetchall()
            subagents: list[dict[str, Any]] = []
            for row in subagent_rows:
                payload = json.loads(row["payload_json"])
                previous_owner_token = str(row["owner_token"] or "")
                owner_is_active = bool(
                    previous_owner_token and previous_owner_token in active_owner_tokens
                )
                if payload.get("status") == "running" and not owner_is_active:
                    payload.update({
                        "status": "interrupted",
                        "completed_at": interrupted_at,
                        "result_summary": summary,
                        "runtime_instance_id": current_instance_id,
                        "runtime_process_id": int(current_process_id),
                        "runtime_process_start_identity": current_process_start_identity,
                        "runtime_owner_token": current_owner_token,
                    })
                    agent_path = str(payload.get("agent_path") or row["agent_path"] or "")
                    mailbox_epoch = max(
                        0,
                        int(payload.get("mailbox_epoch") or row["mailbox_epoch"] or 0),
                    )
                    cursor = connection.execute(
                        """
                        UPDATE subagent_runs
                        SET status = 'interrupted', updated_at = ?, payload_json = ?,
                            owner_token = ?, agent_path = ?, mailbox_epoch = ?
                        WHERE subagent_id = ? AND status = 'running' AND owner_token = ?
                        """,
                        (
                            interrupted_at,
                            _json(payload),
                            current_owner_token,
                            agent_path,
                            mailbox_epoch,
                            row["subagent_id"],
                            previous_owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        latest = connection.execute(
                            "SELECT payload_json FROM subagent_runs WHERE subagent_id = ?",
                            (row["subagent_id"],),
                        ).fetchone()
                        payload = json.loads(latest["payload_json"])
                subagents.append(payload)

            result_rows = connection.execute(
                "SELECT payload_json FROM subagent_results"
            ).fetchall()
            results = [json.loads(row["payload_json"]) for row in result_rows]
        return {"runs": runs, "subagents": subagents, "results": results}

    @staticmethod
    def _next_seq(connection: sqlite3.Connection, conversation_id: str) -> int:
        connection.execute(
            """
            INSERT INTO scope_counters(conversation_id, high_water)
            VALUES (?, 0)
            ON CONFLICT(conversation_id) DO NOTHING
            """,
            (conversation_id,),
        )
        connection.execute(
            "UPDATE scope_counters SET high_water = high_water + 1 WHERE conversation_id = ?",
            (conversation_id,),
        )
        row = connection.execute(
            "SELECT high_water FROM scope_counters WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return int(row["high_water"])

    @staticmethod
    def _set_high_water(connection: sqlite3.Connection, conversation_id: str, value: int) -> None:
        connection.execute(
            """
            INSERT INTO scope_counters(conversation_id, high_water)
            VALUES (?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                high_water = MAX(scope_counters.high_water, excluded.high_water)
            """,
            (conversation_id, max(0, int(value))),
        )

    def append_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        with self._write() as connection:
            message = {
                "message_id": str(payload.get("message_id") or _new_id("msg")),
                "sender_id": str(payload.get("sender_id") or ""),
                "recipient_id": str(payload.get("recipient_id") or ""),
                "content": str(payload.get("content") or ""),
                "conversation_id": conversation_id,
                "team_name": str(payload.get("team_name") or ""),
                "task_id": str(payload.get("task_id") or ""),
                "sender_mailbox_epoch": max(0, int(payload.get("sender_mailbox_epoch") or 0)),
                "recipient_mailbox_epoch": max(0, int(payload.get("recipient_mailbox_epoch") or 0)),
                "recipient_mailbox_epochs": {
                    str(key): max(0, int(value or 0))
                    for key, value in (payload.get("recipient_mailbox_epochs") or {}).items()
                    if str(key).strip()
                } if isinstance(payload.get("recipient_mailbox_epochs"), dict) else {},
                "created_at": int(payload.get("created_at") or _epoch_ms()),
                "seq": self._next_seq(connection, conversation_id),
            }
            self._insert_message(connection, message)
            return dict(message)

    @staticmethod
    def _insert_message(connection: sqlite3.Connection, message: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO messages(
                message_id, conversation_id, sender_id, recipient_id,
                created_at, seq, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message["message_id"],
                message["conversation_id"],
                message["sender_id"],
                message["recipient_id"],
                message["created_at"],
                message["seq"],
                _json(message),
            ),
        )

    def list_messages(
        self,
        *,
        participant_id: str = "",
        conversation_id: str = "",
        since_seq: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id = ?")
            values.append(conversation_id)
        if participant_id:
            clauses.append("(sender_id = ? OR recipient_id = ? OR recipient_id IN ('all', '*'))")
            values.extend((participant_id, participant_id))
        if since_seq > 0:
            clauses.append("seq > ?")
            values.append(since_seq)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM messages
                {where}
                ORDER BY seq DESC, created_at DESC, message_id DESC
                LIMIT ?
                """,
                (*values, bounded_limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in reversed(rows)]

    @staticmethod
    def _message_targets_incarnation(
        message: dict[str, Any],
        *,
        participant_id: str,
        mailbox_epoch: int,
    ) -> bool:
        recipient = str(message.get("recipient_id") or "")
        if recipient in {"all", "*"}:
            epochs = message.get("recipient_mailbox_epochs")
            if isinstance(epochs, dict) and participant_id in epochs:
                return int(epochs.get(participant_id) or 0) == mailbox_epoch
            return mailbox_epoch <= 1
        if recipient != participant_id:
            return False
        target_epoch = int(message.get("recipient_mailbox_epoch") or 0)
        return target_epoch == mailbox_epoch or (target_epoch == 0 and mailbox_epoch <= 1)

    def claim_messages(
        self,
        *,
        participant_id: str,
        mailbox_epoch: int,
        claim_owner: str,
        conversation_id: str = "",
        since_seq: int = 0,
        limit: int = 100,
        now_ms: int | None = None,
        lease_ms: int = 30_000,
    ) -> list[dict[str, Any]]:
        """Atomically claim incoming messages for one agent incarnation.

        Claims are exclusive until acknowledged or their lease expires. The
        returned delivery metadata is intentionally separate from the message
        payload so mailbox history remains immutable.
        """
        participant = str(participant_id or "").strip()
        owner = str(claim_owner or "").strip()
        if not participant or not owner:
            raise ValueError("mailbox claim requires participant_id and claim_owner")
        epoch = max(0, int(mailbox_epoch or 0))
        now = int(now_ms if now_ms is not None else _epoch_ms())
        expires_at = now + max(1_000, int(lease_ms))
        bounded_limit = max(1, min(int(limit or 100), 100))
        clauses = ["(recipient_id = ? OR recipient_id IN ('all', '*'))"]
        values: list[Any] = [participant]
        if conversation_id:
            clauses.append("conversation_id = ?")
            values.append(str(conversation_id))
        if since_seq > 0:
            clauses.append("seq > ?")
            values.append(max(0, int(since_seq)))
        where = " AND ".join(clauses)

        claimed: list[dict[str, Any]] = []
        with self._write() as connection:
            rows = connection.execute(
                f"""
                SELECT message_id, payload_json
                FROM messages
                WHERE {where}
                ORDER BY seq ASC, created_at ASC, message_id ASC
                LIMIT 1000
                """,
                tuple(values),
            ).fetchall()
            for row in rows:
                message = json.loads(row["payload_json"])
                if not self._message_targets_incarnation(
                    message,
                    participant_id=participant,
                    mailbox_epoch=epoch,
                ):
                    continue
                delivery = connection.execute(
                    """
                    SELECT status, claim_owner, claim_token, lease_expires_at
                    FROM mailbox_deliveries
                    WHERE message_id = ? AND participant_id = ? AND mailbox_epoch = ?
                    """,
                    (row["message_id"], participant, epoch),
                ).fetchone()
                if delivery is not None:
                    status = str(delivery["status"] or "")
                    if status == "acked":
                        continue
                    if status == "claimed" and int(delivery["lease_expires_at"] or 0) > now:
                        continue

                claim_token = _new_id("claim")
                connection.execute(
                    """
                    INSERT INTO mailbox_deliveries(
                        message_id, participant_id, mailbox_epoch, status,
                        claim_owner, claim_token, claimed_at, lease_expires_at, acked_at
                    ) VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?, 0)
                    ON CONFLICT(message_id, participant_id, mailbox_epoch) DO UPDATE SET
                        status = 'claimed',
                        claim_owner = excluded.claim_owner,
                        claim_token = excluded.claim_token,
                        claimed_at = excluded.claimed_at,
                        lease_expires_at = excluded.lease_expires_at,
                        acked_at = 0
                    """,
                    (
                        row["message_id"], participant, epoch, owner,
                        claim_token, now, expires_at,
                    ),
                )
                claimed.append({
                    "message": message,
                    "participant_id": participant,
                    "mailbox_epoch": epoch,
                    "claim_owner": owner,
                    "claim_token": claim_token,
                    "lease_expires_at": expires_at,
                })
                if len(claimed) >= bounded_limit:
                    break
        return claimed

    def ack_message_claims(
        self,
        claims: list[dict[str, Any]],
        *,
        claim_owner: str,
        acked_at: int | None = None,
    ) -> int:
        owner = str(claim_owner or "").strip()
        if not owner or not claims:
            return 0
        now = int(acked_at if acked_at is not None else _epoch_ms())
        count = 0
        with self._write() as connection:
            for claim in claims:
                cursor = connection.execute(
                    """
                    UPDATE mailbox_deliveries
                    SET status = 'acked', acked_at = ?, lease_expires_at = 0
                    WHERE message_id = ? AND participant_id = ? AND mailbox_epoch = ?
                      AND status = 'claimed' AND claim_owner = ? AND claim_token = ?
                    """,
                    (
                        now,
                        str(claim.get("message_id") or ""),
                        str(claim.get("participant_id") or ""),
                        max(0, int(claim.get("mailbox_epoch") or 0)),
                        owner,
                        str(claim.get("claim_token") or ""),
                    ),
                )
                count += max(0, int(cursor.rowcount or 0))
        return count

    def release_message_claims(
        self,
        claims: list[dict[str, Any]],
        *,
        claim_owner: str,
    ) -> int:
        owner = str(claim_owner or "").strip()
        if not owner or not claims:
            return 0
        count = 0
        with self._write() as connection:
            for claim in claims:
                cursor = connection.execute(
                    """
                    DELETE FROM mailbox_deliveries
                    WHERE message_id = ? AND participant_id = ? AND mailbox_epoch = ?
                      AND status = 'claimed' AND claim_owner = ? AND claim_token = ?
                    """,
                    (
                        str(claim.get("message_id") or ""),
                        str(claim.get("participant_id") or ""),
                        max(0, int(claim.get("mailbox_epoch") or 0)),
                        owner,
                        str(claim.get("claim_token") or ""),
                    ),
                )
                count += max(0, int(cursor.rowcount or 0))
        return count

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        with self._write() as connection:
            task = self._new_task(payload, self._next_seq(connection, conversation_id))
            self._insert_task(connection, task)
            self._replace_dependencies(
                connection,
                task["task_id"],
                blocks=task["blocks"],
                blocked_by=task["blocked_by"],
            )
            return self._get_task(connection, task["task_id"]) or dict(task)

    @staticmethod
    def _new_task(payload: dict[str, Any], seq: int) -> dict[str, Any]:
        now = _epoch_ms()
        return {
            "task_id": str(payload.get("task_id") or _new_id("swarm_task")),
            "title": str(payload.get("title") or ""),
            "description": str(payload.get("description") or ""),
            "assignee": str(payload.get("assignee") or ""),
            "conversation_id": str(payload.get("conversation_id") or "").strip(),
            "agent_type": str(payload.get("agent_type") or "general-purpose").strip() or "general-purpose",
            "role": str(payload.get("role") or ""),
            "objective": str(payload.get("objective") or ""),
            "read_only": bool(payload.get("read_only", False)),
            "write_scope": _string_list(payload.get("write_scope")),
            "status": str(payload.get("status") or "pending"),
            "priority": str(payload.get("priority") or "normal"),
            "team_name": str(payload.get("team_name") or ""),
            "created_by": str(payload.get("created_by") or ""),
            "created_at": int(payload.get("created_at") or now),
            "updated_at": int(payload.get("updated_at") or now),
            "completed_at": payload.get("completed_at"),
            "blocks": _string_list(payload.get("blocks")),
            "blocked_by": _string_list(payload.get("blocked_by")),
            "outputs": list(payload.get("outputs") or []),
            "seq": int(seq),
        }

    @staticmethod
    def _task_storage_payload(task: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in task.items()
            if key not in {"blocks", "blocked_by", "outputs"}
        }

    def _insert_task(self, connection: sqlite3.Connection, task: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO tasks(
                task_id, conversation_id, assignee, status, team_name,
                updated_at, seq, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task["task_id"],
                task["conversation_id"],
                task["assignee"],
                task["status"],
                task["team_name"],
                task["updated_at"],
                task["seq"],
                _json(self._task_storage_payload(task)),
            ),
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._get_task(connection, task_id)

    def _get_task(self, connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        task = json.loads(row["payload_json"])
        task["outputs"] = [
            json.loads(output["payload_json"])
            for output in connection.execute(
                "SELECT payload_json FROM task_outputs WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        ]
        task["blocks"] = [
            row["blocked_task_id"]
            for row in connection.execute(
                """
                SELECT blocked_task_id FROM task_dependencies
                WHERE blocker_task_id = ? ORDER BY blocked_task_id
                """,
                (task_id,),
            ).fetchall()
        ]
        task["blocked_by"] = [
            row["blocker_task_id"]
            for row in connection.execute(
                """
                SELECT blocker_task_id FROM task_dependencies
                WHERE blocked_task_id = ? ORDER BY blocker_task_id
                """,
                (task_id,),
            ).fetchall()
        ]
        return task

    def list_tasks(
        self,
        *,
        assignee: str = "",
        status: str = "",
        team_name: str = "",
        conversation_id: str = "",
        since_seq: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("assignee", assignee),
            ("status", status),
            ("team_name", team_name),
            ("conversation_id", conversation_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        if since_seq > 0:
            clauses.append("seq > ?")
            values.append(since_seq)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT task_id FROM tasks
                {where}
                ORDER BY updated_at DESC, task_id
                LIMIT ?
                """,
                (*values, max(1, min(limit, 100))),
            ).fetchall()
            return [
                task
                for row in rows
                if (task := self._get_task(connection, row["task_id"])) is not None
            ]

    def update_task(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._write() as connection:
            task = self._get_task(connection, task_id)
            if task is None:
                return None
            for key in (
                "title",
                "description",
                "assignee",
                "priority",
                "team_name",
                "agent_type",
                "role",
                "objective",
            ):
                if key in patch:
                    task[key] = str(patch.get(key) or "").strip()
            if "read_only" in patch:
                task["read_only"] = bool(patch.get("read_only"))
            if "write_scope" in patch:
                task["write_scope"] = _string_list(patch.get("write_scope"))
            if "blocks" in patch:
                task["blocks"] = _string_list(patch.get("blocks"))
            if "blocked_by" in patch:
                task["blocked_by"] = _string_list(patch.get("blocked_by"))
            if "status" in patch:
                status = str(patch.get("status") or "").strip()
                if status:
                    task["status"] = status
                    task["completed_at"] = _epoch_ms() if status in {"completed", "cancelled"} else None
            task["updated_at"] = _epoch_ms()
            task["seq"] = self._next_seq(connection, task["conversation_id"])
            connection.execute(
                """
                UPDATE tasks SET
                    assignee = ?, status = ?, team_name = ?,
                    updated_at = ?, seq = ?, payload_json = ?
                WHERE task_id = ?
                """,
                (
                    task["assignee"],
                    task["status"],
                    task["team_name"],
                    task["updated_at"],
                    task["seq"],
                    _json(self._task_storage_payload(task)),
                    task_id,
                ),
            )
            self._replace_dependencies(
                connection,
                task_id,
                blocks=task["blocks"],
                blocked_by=task["blocked_by"],
            )
            return self._get_task(connection, task_id)

    @staticmethod
    def _replace_dependencies(
        connection: sqlite3.Connection,
        task_id: str,
        *,
        blocks: list[str],
        blocked_by: list[str],
    ) -> None:
        connection.execute(
            """
            DELETE FROM task_dependencies
            WHERE blocker_task_id = ? OR blocked_task_id = ?
            """,
            (task_id, task_id),
        )
        edges = {
            (task_id, blocked_id)
            for blocked_id in _string_list(blocks)
            if blocked_id != task_id
        }
        edges.update(
            (blocker_id, task_id)
            for blocker_id in _string_list(blocked_by)
            if blocker_id != task_id
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO task_dependencies(blocker_task_id, blocked_task_id)
            VALUES (?, ?)
            """,
            sorted(edges),
        )

    def append_output(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._write() as connection:
            task = self._get_task(connection, task_id)
            if task is None:
                return None
            output = {
                "output_id": str(payload.get("output_id") or _new_id("task_output")),
                "author_id": str(payload.get("author_id") or ""),
                "content": str(payload.get("content") or ""),
                "created_at": int(payload.get("created_at") or _epoch_ms()),
                "seq": self._next_seq(connection, task["conversation_id"]),
            }
            connection.execute(
                """
                INSERT INTO task_outputs(output_id, task_id, created_at, seq, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    output["output_id"],
                    task_id,
                    output["created_at"],
                    output["seq"],
                    _json(output),
                ),
            )
            task["updated_at"] = _epoch_ms()
            task["seq"] = self._next_seq(connection, task["conversation_id"])
            connection.execute(
                """
                UPDATE tasks SET updated_at = ?, seq = ?, payload_json = ?
                WHERE task_id = ?
                """,
                (
                    task["updated_at"],
                    task["seq"],
                    _json(self._task_storage_payload(task)),
                    task_id,
                ),
            )
            return self._get_task(connection, task_id)

    def create_team(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        team_name = str(payload.get("team_name") or "").strip()
        with self._write() as connection:
            connection.execute(
                "DELETE FROM teams WHERE conversation_id = ? AND team_name = ?",
                (conversation_id, team_name),
            )
            now = _epoch_ms()
            team = {
                "team_id": str(payload.get("team_id") or _new_id("team")),
                "team_name": team_name,
                "description": str(payload.get("description") or ""),
                "conversation_id": conversation_id,
                "created_by": str(payload.get("created_by") or ""),
                "created_at": int(payload.get("created_at") or now),
                "updated_at": int(payload.get("updated_at") or now),
                "members": self._normalize_members(payload.get("members")),
                "seq": self._next_seq(connection, conversation_id),
            }
            self._insert_team(connection, team)
            return dict(team)

    @staticmethod
    def _insert_team(connection: sqlite3.Connection, team: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO teams(
                team_id, conversation_id, team_name, updated_at, seq, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                team["team_id"],
                team["conversation_id"],
                team["team_name"],
                team["updated_at"],
                team["seq"],
                _json(team),
            ),
        )

    def list_teams(
        self,
        *,
        conversation_id: str = "",
        team_name: str = "",
        since_seq: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id = ?")
            values.append(conversation_id)
        if team_name:
            clauses.append("team_name = ?")
            values.append(team_name)
        if since_seq > 0:
            clauses.append("seq > ?")
            values.append(since_seq)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM teams
                {where}
                ORDER BY updated_at DESC, team_id
                LIMIT ?
                """,
                (*values, max(1, min(limit, 100))),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def delete_team(self, *, conversation_id: str = "", team_name: str = "") -> dict[str, Any] | None:
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT team_id, payload_json FROM teams
                WHERE conversation_id = ? AND team_name = ?
                """,
                (conversation_id, team_name),
            ).fetchone()
            if row is None:
                return None
            removed = json.loads(row["payload_json"])
            removed["deleted_seq"] = self._next_seq(connection, conversation_id)
            removed["deleted_at"] = _epoch_ms()
            connection.execute("DELETE FROM teams WHERE team_id = ?", (row["team_id"],))
            return removed

    @staticmethod
    def _normalize_members(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        members: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            member_id = str(item.get("id") or item.get("member_id") or item.get("name") or "").strip()
            if not member_id or member_id in seen:
                continue
            seen.add(member_id)
            members.append({
                "id": member_id,
                "role": str(item.get("role") or "").strip(),
                "agent_type": str(item.get("agent_type") or "general-purpose").strip() or "general-purpose",
                "description": str(item.get("description") or "").strip(),
            })
        return members

    def _import_legacy_json(self) -> None:
        LegacySwarmMigrator(self).run()

    def _import_legacy_state(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
        fallback_scope: str,
        source: Path,
    ) -> dict[str, Any]:
        counts = {
            "messages": 0,
            "tasks": 0,
            "task_outputs": 0,
            "teams": 0,
        }
        record_ids: dict[str, list[str]] = {
            "messages": [],
            "tasks": [],
            "task_outputs": [],
            "teams": [],
        }
        scopes: set[str] = {fallback_scope}
        for raw in state.get("messages") or []:
            if not isinstance(raw, dict):
                continue
            payload = dict(raw)
            payload["conversation_id"] = str(payload.get("conversation_id") or fallback_scope)
            scopes.add(payload["conversation_id"])
            message_id = str(payload.get("message_id") or _new_id("msg"))
            self._insert_legacy_unique(
                connection,
                table="messages",
                id_column="message_id",
                record_id=message_id,
                payload=payload,
                source=source,
                insert=lambda record: self._insert_message(connection, record),
                normalize=lambda record: {
                    "message_id": str(record.get("message_id") or _new_id("msg")),
                    "sender_id": str(record.get("sender_id") or ""),
                    "recipient_id": str(record.get("recipient_id") or ""),
                    "content": str(record.get("content") or ""),
                    "conversation_id": str(record.get("conversation_id") or fallback_scope),
                    "team_name": str(record.get("team_name") or ""),
                    "task_id": str(record.get("task_id") or ""),
                    "created_at": int(record.get("created_at") or _epoch_ms()),
                    "seq": int(record.get("seq") or 0),
                },
            )
            counts["messages"] += 1
            record_ids["messages"].append(message_id)

        dependency_edges: set[tuple[str, str]] = set()
        for raw in state.get("tasks") or []:
            if not isinstance(raw, dict):
                continue
            payload = dict(raw)
            payload["conversation_id"] = str(payload.get("conversation_id") or fallback_scope)
            scopes.add(payload["conversation_id"])
            task = self._new_task(payload, int(payload.get("seq") or 0))
            counts["tasks"] += 1
            record_ids["tasks"].append(task["task_id"])
            outputs = list(task.pop("outputs", []))
            blocks = list(task.get("blocks", []))
            blocked_by = list(task.get("blocked_by", []))
            existing = connection.execute(
                "SELECT payload_json FROM tasks WHERE task_id = ?",
                (task["task_id"],),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload_json"]) != self._task_storage_payload(task):
                    raise RuntimeError(
                        f"Conflicting legacy task id {task['task_id']} while importing {source}"
                    )
            else:
                self._insert_task(connection, task)
            for output_raw in outputs:
                if not isinstance(output_raw, dict):
                    continue
                output = {
                    "output_id": str(output_raw.get("output_id") or _new_id("task_output")),
                    "author_id": str(output_raw.get("author_id") or ""),
                    "content": str(output_raw.get("content") or ""),
                    "created_at": int(output_raw.get("created_at") or _epoch_ms()),
                    "seq": int(output_raw.get("seq") or 0),
                }
                existing_output = connection.execute(
                    """
                    SELECT task_id, payload_json FROM task_outputs
                    WHERE output_id = ?
                    """,
                    (output["output_id"],),
                ).fetchone()
                if existing_output is None:
                    connection.execute(
                        """
                        INSERT INTO task_outputs(output_id, task_id, created_at, seq, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            output["output_id"],
                            task["task_id"],
                            output["created_at"],
                            output["seq"],
                            _json(output),
                        ),
                    )
                elif (
                    existing_output["task_id"] != task["task_id"]
                    or json.loads(existing_output["payload_json"]) != output
                ):
                    raise RuntimeError(
                        f"Conflicting legacy output id {output['output_id']} while importing {source}"
                    )
                counts["task_outputs"] += 1
                record_ids["task_outputs"].append(output["output_id"])
            dependency_edges.update((task["task_id"], blocked) for blocked in blocks if blocked != task["task_id"])
            dependency_edges.update((blocker, task["task_id"]) for blocker in blocked_by if blocker != task["task_id"])

        connection.executemany(
            """
            INSERT OR IGNORE INTO task_dependencies(blocker_task_id, blocked_task_id)
            VALUES (?, ?)
            """,
            sorted(dependency_edges),
        )

        for raw in state.get("teams") or []:
            if not isinstance(raw, dict):
                continue
            payload = dict(raw)
            payload["conversation_id"] = str(payload.get("conversation_id") or fallback_scope)
            scopes.add(payload["conversation_id"])
            team = {
                "team_id": str(payload.get("team_id") or _new_id("team")),
                "team_name": str(payload.get("team_name") or ""),
                "description": str(payload.get("description") or ""),
                "conversation_id": payload["conversation_id"],
                "created_by": str(payload.get("created_by") or ""),
                "created_at": int(payload.get("created_at") or _epoch_ms()),
                "updated_at": int(payload.get("updated_at") or _epoch_ms()),
                "members": self._normalize_members(payload.get("members")),
                "seq": int(payload.get("seq") or 0),
            }
            counts["teams"] += 1
            record_ids["teams"].append(team["team_id"])
            existing = connection.execute(
                "SELECT payload_json FROM teams WHERE team_id = ?",
                (team["team_id"],),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload_json"]) != team:
                    raise RuntimeError(
                        f"Conflicting legacy team id {team['team_id']} while importing {source}"
                    )
            else:
                try:
                    self._insert_team(connection, team)
                except sqlite3.IntegrityError as error:
                    raise RuntimeError(
                        f"Conflicting legacy team {team['team_name']} while importing {source}"
                    ) from error

        high_water = int(state.get("high_water") or 0)
        for scope in scopes:
            record_max = connection.execute(
                """
                SELECT MAX(seq) FROM (
                    SELECT seq FROM messages WHERE conversation_id = ?
                    UNION ALL SELECT seq FROM tasks WHERE conversation_id = ?
                    UNION ALL SELECT seq FROM teams WHERE conversation_id = ?
                )
                """,
                (scope, scope, scope),
            ).fetchone()[0]
            output_max = connection.execute(
                """
                SELECT MAX(task_outputs.seq)
                FROM task_outputs
                JOIN tasks ON tasks.task_id = task_outputs.task_id
                WHERE tasks.conversation_id = ?
                """,
                (scope,),
            ).fetchone()[0]
            self._set_high_water(
                connection,
                scope,
                max(high_water if scope == fallback_scope else 0, int(record_max or 0), int(output_max or 0)),
            )
        return {"counts": counts, "record_ids": record_ids}

    @staticmethod
    def _verify_legacy_records(
        connection: sqlite3.Connection,
        record_ids: dict[str, list[str]],
        source: Path,
    ) -> str:
        identifiers = {
            "messages": "message_id",
            "tasks": "task_id",
            "task_outputs": "output_id",
            "teams": "team_id",
        }
        canonical_records: list[dict[str, Any]] = []
        for table, ids in record_ids.items():
            id_column = identifiers[table]
            for record_id in ids:
                found = connection.execute(
                    f"SELECT payload_json FROM {table} WHERE {id_column} = ?",
                    (record_id,),
                ).fetchone()
                if found is None:
                    raise RuntimeError(
                        f"Legacy import verification failed for {source}: "
                        f"missing {table}.{id_column}={record_id}"
                    )
                canonical_records.append({
                    "table": table,
                    "id": record_id,
                    "payload": json.loads(found["payload_json"]),
                })
        canonical_records.sort(key=lambda item: (item["table"], item["id"]))
        return sha256(_json(canonical_records).encode("utf-8")).hexdigest()

    @staticmethod
    def _insert_legacy_unique(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        payload: dict[str, Any],
        source: Path,
        insert: Any,
        normalize: Any,
    ) -> None:
        record = normalize({**payload, id_column: record_id})
        existing = connection.execute(
            f"SELECT payload_json FROM {table} WHERE {id_column} = ?",
            (record_id,),
        ).fetchone()
        if existing is None:
            insert(record)
            return
        if json.loads(existing["payload_json"]) != record:
            raise RuntimeError(
                f"Conflicting legacy {id_column} {record_id} while importing {source}"
            )
