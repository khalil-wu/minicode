"""Canonical lifecycle payloads shared by long-running runtime work."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def epoch_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ExecutionLifecycle:
    """Transport-neutral identity and transition state for asynchronous work."""

    run_id: str
    task_id: str = ""
    parent_run_id: str = ""
    incarnation: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = "task"
    phase: str = "queued"
    status: str = "running"
    seq: int = 0
    started_at: int = field(default_factory=epoch_ms)
    updated_at: int = field(default_factory=epoch_ms)
    completed_at: int | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)

    def transition(
        self,
        *,
        phase: str,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self.seq += 1
        self.phase = str(phase or self.phase)
        if status:
            self.status = str(status)
        self.updated_at = epoch_ms()
        if result is not None:
            self.result = dict(result)
        if error is not None:
            self.error = dict(error)
        if self.status in {"completed", "failed", "cancelled", "interrupted", "partial"}:
            self.completed_at = self.updated_at

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id or self.run_id,
            "parent_run_id": self.parent_run_id,
            "incarnation": self.incarnation,
            "seq": self.seq,
            "kind": self.kind,
            "phase": self.phase,
            "status": self.status,
            "started_at_ms": self.started_at,
            "updated_at": self.updated_at,
            "completed_at_ms": self.completed_at,
            "result": dict(self.result),
            "error": dict(self.error),
        }
