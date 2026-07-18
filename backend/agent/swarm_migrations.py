"""Schema and legacy-data migration orchestration for the swarm store."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Sequence

MIGRATION_KEY = "legacy_json_import_v1"
MIGRATION_REPORT_KEY = "legacy_json_import_report_v1"


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class LegacySwarmMigrator:
    """One-shot importer for pre-SQLite swarm JSON snapshots.

    Record normalization and insertion remain store-owned so the migrator does
    not duplicate canonical persistence rules. This class owns discovery,
    verification, reporting, and the one-shot migration marker.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def run(self) -> None:
        with self.store._write() as connection:
            if connection.execute(
                "SELECT 1 FROM metadata WHERE key = ?",
                (MIGRATION_KEY,),
            ).fetchone():
                if not connection.execute(
                    "SELECT 1 FROM metadata WHERE key = ?",
                    (MIGRATION_REPORT_KEY,),
                ).fetchone():
                    empty_report = {
                        "version": 1,
                        "completed_at": _epoch_ms(),
                        "verified": not any(self.store.root.glob("*.json")),
                        "totals": {
                            "messages": 0,
                            "tasks": 0,
                            "task_outputs": 0,
                            "teams": 0,
                        },
                        "canonical_sha256": sha256(b"[]").hexdigest(),
                        "sources": [],
                    }
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)",
                        (MIGRATION_REPORT_KEY, _json(empty_report)),
                    )
                return

            sources: list[dict[str, Any]] = []
            totals = {
                "messages": 0,
                "tasks": 0,
                "task_outputs": 0,
                "teams": 0,
            }
            for path in sorted(self.store.root.glob("*.json")):
                try:
                    source_bytes = path.read_bytes()
                    state = json.loads(source_bytes.decode("utf-8"))
                except Exception as error:
                    raise RuntimeError(f"Failed to import legacy swarm store {path}: {error}") from error
                if not isinstance(state, dict):
                    raise RuntimeError(f"Failed to import legacy swarm store {path}: root must be an object")
                fallback_scope = "" if path.stem == "global" else path.stem
                imported = self.store._import_legacy_state(
                    connection,
                    state,
                    fallback_scope,
                    path,
                )
                canonical_sha256 = self.store._verify_legacy_records(
                    connection,
                    imported["record_ids"],
                    path,
                )
                counts = imported["counts"]
                for key in totals:
                    totals[key] += int(counts[key])
                sources.append({
                    "path": path.name,
                    "sha256": sha256(source_bytes).hexdigest(),
                    "canonical_sha256": canonical_sha256,
                    "counts": counts,
                })

            canonical_manifest = [
                {
                    "path": source["path"],
                    "canonical_sha256": source["canonical_sha256"],
                }
                for source in sources
            ]
            report = {
                "version": 1,
                "completed_at": _epoch_ms(),
                "verified": True,
                "totals": totals,
                "canonical_sha256": sha256(_json(canonical_manifest).encode("utf-8")).hexdigest(),
                "sources": sources,
            }
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (MIGRATION_KEY, str(_epoch_ms())),
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (MIGRATION_REPORT_KEY, _json(report)),
            )


class SchemaMigrationRunner:
    """Applies an ordered schema manifest and verifies immutable history."""

    def __init__(self, migrations: Sequence["SchemaMigration"]) -> None:
        ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
        versions = [migration.version for migration in ordered]
        if versions != list(range(1, len(ordered) + 1)):
            raise ValueError("schema migrations must be contiguous and start at version 1")
        self.migrations = ordered

    def run(self, connection: Any) -> int:
        has_metadata = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
        ).fetchone() is not None
        current_version = 0
        if has_metadata:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            current_version = int(row["value"]) if row is not None else 0

        for migration in self.migrations:
            checksum = migration.checksum
            key = f"migration_checksum_v{migration.version}"
            existing = None
            if has_metadata:
                existing = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    (key,),
                ).fetchone()
            if existing is not None and str(existing["value"]) != checksum:
                raise RuntimeError(
                    f"migration checksum mismatch for v{migration.version}: "
                    f"expected {existing['value']}, got {checksum}"
                )
            if migration.version > current_version:
                connection.executescript(migration.sql)
                has_metadata = True
                current_version = migration.version
            if existing is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    (key, checksum),
                )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(current_version),),
            )
        return current_version

    @staticmethod
    def record_or_verify(connection: Any, version: int) -> str:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
            ORDER BY type, name
            """
        ).fetchall()
        manifest = [
            {
                "type": str(row["type"]),
                "name": str(row["name"]),
                "table": str(row["tbl_name"]),
                "sql": " ".join(str(row["sql"]).split()),
            }
            for row in rows
        ]
        checksum = sha256(_json(manifest).encode("utf-8")).hexdigest()
        key = f"schema_checksum_v{int(version)}"
        existing = connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (key,),
        ).fetchone()
        if existing is not None and str(existing["value"]) != checksum:
            raise RuntimeError(
                f"schema checksum mismatch for v{version}: "
                f"expected {existing['value']}, got {checksum}"
            )
        if existing is None:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (key, checksum),
            )
        return checksum


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    sql: str

    @property
    def checksum(self) -> str:
        manifest = {
            "version": self.version,
            "sql": " ".join(self.sql.split()),
        }
        return sha256(_json(manifest).encode("utf-8")).hexdigest()
