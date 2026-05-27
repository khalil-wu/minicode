"""Cron-like scheduled task runner.

Stores task definitions in `.minicode/scheduled_tasks.json` and fires them
on schedule by creating new agent sessions with the configured prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from backend.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SCHEDULE_FILE = PROJECT_ROOT / ".minicode" / "scheduled_tasks.json"


@dataclass
class ScheduledTask:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    prompt: str = ""
    schedule: str = "0 * * * *"  # cron expression (min hour dom month dow)
    permission_mode: str = "auto_approve"
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_run_at: str | None = None
    next_run_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of valid values."""
    values: set[int] = set()
    for part in field_str.split(","):
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if base == "*":
                start = min_val
            else:
                start = int(base)
            for v in range(start, max_val + 1, step):
                values.add(v)
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        elif "-" in part:
            lo, hi = part.split("-", 1)
            values.update(range(int(lo), int(hi) + 1))
        else:
            values.add(int(part))
    return values


def cron_matches(expression: str, dt: datetime) -> bool:
    """Check if a datetime matches a 5-field cron expression."""
    parts = expression.strip().split()
    if len(parts) != 5:
        return False
    try:
        minutes = _parse_cron_field(parts[0], 0, 59)
        hours = _parse_cron_field(parts[1], 0, 23)
        doms = _parse_cron_field(parts[2], 1, 31)
        months = _parse_cron_field(parts[3], 1, 12)
        dows = _parse_cron_field(parts[4], 0, 6)
    except (ValueError, IndexError):
        return False
    # Python weekday: Mon=0..Sun=6; cron: Sun=0, Mon=1..Sat=6
    cron_dow = (dt.weekday() + 1) % 7
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and cron_dow in dows
    )


# Callback type: async fn(task) that creates a session and runs the prompt
TaskFireCallback = Callable[["ScheduledTask"], Coroutine[Any, Any, None]]


class TaskScheduler:
    """In-memory scheduler that checks tasks every 60s."""

    def __init__(self, on_fire: TaskFireCallback | None = None) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._on_fire = on_fire
        self._loop_task: asyncio.Task[None] | None = None
        self._load()

    def _load(self) -> None:
        if not SCHEDULE_FILE.exists():
            return
        try:
            data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
            for raw in data.get("tasks", []):
                task = ScheduledTask.from_dict(raw)
                self._tasks[task.id] = task
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("scheduler load failed: %s", exc)

    def _save(self) -> None:
        SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "tasks": [t.to_dict() for t in self._tasks.values()]}
        SCHEDULE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_tasks(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._tasks.values()]

    def add_task(self, name: str, prompt: str, schedule: str, permission_mode: str = "auto_approve") -> ScheduledTask:
        task = ScheduledTask(name=name, prompt=prompt, schedule=schedule, permission_mode=permission_mode)
        self._tasks[task.id] = task
        self._save()
        return task

    def remove_task(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._save()
        return True

    def toggle_task(self, task_id: str, enabled: bool) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.enabled = enabled
        self._save()
        return True

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.ensure_future(self._run_loop())

    def stop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = datetime.now(UTC)
            for task in list(self._tasks.values()):
                if not task.enabled:
                    continue
                if cron_matches(task.schedule, now):
                    task.last_run_at = now.isoformat()
                    self._save()
                    if self._on_fire:
                        try:
                            await self._on_fire(task)
                        except Exception as exc:
                            logger.error("scheduled task %s fire failed: %s", task.id, exc)
