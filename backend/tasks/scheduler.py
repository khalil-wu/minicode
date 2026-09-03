"""Cron-like scheduled task runner.

Stores task definitions in `.minicode/scheduled_tasks.json` and fires them
on schedule by creating new agent sessions with the configured prompt.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.async_cleanup import cancel_and_drain, cancel_and_drain_receipt
from backend.atomic_io import atomic_write_text
from backend.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SCHEDULE_FILE = PROJECT_ROOT / ".minicode" / "scheduled_tasks.json"
SCHEDULE_REGISTRY_FILE = PROJECT_ROOT / ".minicode" / "scheduled_task_projects.json"
PROJECT_SCHEDULE_RELATIVE_PATH = Path(".minicode") / "scheduled_tasks.json"


def _normalize_workspace_root(value: str | None) -> str:
    """Return a stable workspace key without requiring the folder to exist."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return str(Path(text).expanduser())


def _project_schedule_file(workspace_root: str) -> Path:
    return Path(workspace_root) / PROJECT_SCHEDULE_RELATIVE_PATH


def _registry_file() -> Path:
    # Deriving this from SCHEDULE_FILE keeps existing tests and embedders that
    # monkeypatch the legacy store fully isolated.
    return SCHEDULE_FILE.with_name(SCHEDULE_REGISTRY_FILE.name)


# cc cron contract (CronCreateTool.ts / cronTasks.ts): at most 50 jobs,
# recurring tasks auto-expire after 7 days, one-shot jobs auto-delete after
# their first fire.
MAX_SCHEDULED_JOBS = 50
SCHEDULED_TASK_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


@dataclass
class ScheduledTask:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    prompt: str = ""
    schedule: str = "0 * * * *"  # cron expression (min hour dom month dow)
    permission_mode: str = "confirm"
    # cc schedules cron in the process-local timezone by default (cron.ts).
    timezone: str = ""
    isolation: str = "worktree"
    # cc recurring flag: false = fire once at the next match, then auto-delete.
    recurring: bool = True
    deleted_at: str | None = None
    # Scheduled work is deliberately bound to one workspace.  Keeping the
    # binding on the task makes the process-wide scheduler safe when a user
    # switches projects between two ticks.
    workspace_root: str = ""
    conversation_id: str = ""
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_run_id: str | None = None
    last_run_status: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        values = {k: v for k, v in data.items() if k in known}
        # Existing scheduled tasks historically ran in the configured
        # workspace. Preserve that behavior during migration; new tasks opt
        # into the safer Worktree default through ``add_task``.
        if "isolation" not in data:
            values["isolation"] = "workspace"
        return cls(**values)


@dataclass
class ScheduledTaskRun:
    """Durable result for one scheduled task execution."""

    id: str = field(default_factory=lambda: f"schedule_run_{uuid.uuid4().hex[:16]}")
    task_id: str = ""
    scheduled_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str = ""
    finished_at: str | None = None
    status: str = "pending"
    conversation_id: str = ""
    workspace_root: str = ""
    result_summary: str = ""
    error: str = ""
    cleanup_pending: bool = False
    cleanup_reason: str = ""
    cleanup_requested_at: str | None = None
    cleanup_completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTaskRun":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class CronFields:
    minutes: set[int]
    hours: set[int]
    doms: set[int]
    months: set[int]
    dows: set[int]


def _parse_cron_field(field_str: str, min_val: int, max_val: int, *, dow: bool = False) -> set[int]:
    """Parse the same bounded 5-field subset used by MiniCode."""
    values: set[int] = set()
    for part in field_str.split(","):
        wildcard = re.fullmatch(r"\*(?:/(\d+))?", part)
        if wildcard:
            step = int(wildcard.group(1) or "1")
            if step < 1:
                raise ValueError("cron step must be positive")
            values.update(range(min_val, max_val + 1, step))
            continue
        ranged = re.fullmatch(r"(\d+)-(\d+)(?:/(\d+))?", part)
        if ranged:
            lo, hi = int(ranged.group(1)), int(ranged.group(2))
            step = int(ranged.group(3) or "1")
            effective_max = 7 if dow else max_val
            if lo < min_val or hi > effective_max or lo > hi or step < 1:
                raise ValueError("cron range is out of bounds")
            values.update(0 if dow and value == 7 else value for value in range(lo, hi + 1, step))
            continue
        if not re.fullmatch(r"\d+", part):
            raise ValueError("unsupported cron field")
        value = int(part)
        if dow and value == 7:
            value = 0
        if value < min_val or value > max_val:
            raise ValueError("cron value is out of bounds")
        values.add(value)
    if not values:
        raise ValueError("empty cron field")
    return values


def _parse_cron_expression(expression: str) -> CronFields | None:
    parts = expression.strip().split()
    if len(parts) != 5:
        return None
    try:
        return CronFields(
            _parse_cron_field(parts[0], 0, 59),
            _parse_cron_field(parts[1], 0, 23),
            _parse_cron_field(parts[2], 1, 31),
            _parse_cron_field(parts[3], 1, 12),
            _parse_cron_field(parts[4], 0, 6, dow=True),
        )
    except (ValueError, IndexError):
        return None


def _cron_parts_match(parts: CronFields, dt: datetime) -> bool:
    # Python weekday: Mon=0..Sun=6; cron: Sun=0, Mon=1..Sat=6
    cron_dow = (dt.weekday() + 1) % 7
    dom_wild = len(parts.doms) == 31
    dow_wild = len(parts.dows) == 7
    dom_match = dt.day in parts.doms
    dow_match = cron_dow in parts.dows
    day_match = (
        True if dom_wild and dow_wild
        else dow_match if dom_wild
        else dom_match if dow_wild
        else dom_match or dow_match
    )
    return dt.minute in parts.minutes and dt.hour in parts.hours and dt.month in parts.months and day_match


def _schedule_timezone(name: str | None) -> ZoneInfo:
    # Empty means the process-local timezone (cc cron.ts default).
    value = str(name or "").strip()
    if not value:
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    # An unknown timezone is a configuration error and must fail explicitly;
    # silently shifting every fire time to UTC hid the mistake from the user.
    return ZoneInfo(value)


def is_valid_timezone(name: str | None) -> bool:
    value = str(name or "").strip()
    if not value:
        return False
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True


def cron_matches(expression: str, dt: datetime, *, timezone: str = "UTC") -> bool:
    """Check if a datetime matches a 5-field cron expression."""
    parts = _parse_cron_expression(expression)
    if not parts:
        return False
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return _cron_parts_match(parts, aware.astimezone(_schedule_timezone(timezone)))


def next_run_after(
    expression: str,
    dt: datetime,
    *,
    timezone: str = "UTC",
    max_days: int = 366,
) -> datetime | None:
    """Return the next minute matching a cron expression after dt."""
    if max_days <= 0:
        return None
    parts = _parse_cron_expression(expression)
    if not parts:
        return None
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    current = aware.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = current + timedelta(days=max_days)
    while current <= deadline:
        if _cron_parts_match(parts, current.astimezone(_schedule_timezone(timezone))):
            return current
        current += timedelta(minutes=1)
    return None


def _parse_last_run_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _stored_next_run(task: ScheduledTask) -> datetime | None:
    if task.deleted_at is not None:
        return None
    persisted = _parse_last_run_at(task.next_run_at)
    if persisted is not None:
        return persisted
    anchor = _parse_last_run_at(task.last_run_at) or _parse_last_run_at(task.created_at)
    if anchor is None:
        return None
    return next_run_after(task.schedule, anchor, timezone=task.timezone)


def _missed_schedule_points(task: ScheduledTask, now: datetime) -> list[datetime]:
    """Return at most one durable, coalesced due point.

    MiniCode advances recurring schedules from the time they are actually
    claimed instead of replaying every wall-clock minute missed while the
    process was asleep.  ``next_run_at`` is the durable equivalent here: an
    overdue value fires once on startup, then the scheduler advances it from
    the claim time.
    """
    due_at = _stored_next_run(task)
    if due_at is None:
        return []
    return [due_at] if due_at <= now.astimezone(UTC) else []


# Callback type: async fn(task) that creates a session and runs the prompt
TaskFireCallback = Callable[..., Coroutine[Any, Any, dict[str, Any] | None]]


class TaskScheduler:
    """In-memory scheduler that checks tasks every 60s."""

    def __init__(
        self,
        on_fire: TaskFireCallback | None = None,
        on_change: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._runs: dict[str, ScheduledTaskRun] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._on_fire = on_fire
        self._on_change = on_change
        self._loop_task: asyncio.Task[None] | None = None
        self._running = False
        # Structured evidence for the most recent corrupt state-file read and
        # for tasks whose stored schedule could not be recomputed.
        self.last_load_error: dict[str, str] | None = None
        self._unusable_schedule_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        # Older MiniCode builds stored every project's tasks in one process
        # file.  Keep that file as a read-compatible migration source, while
        # current tasks live alongside their workspace in `.minicode`.
        self._load_file(SCHEDULE_FILE)
        registry_file = _registry_file()
        try:
            registry = json.loads(registry_file.read_text(encoding="utf-8")) if registry_file.exists() else {}
            workspace_roots = registry.get("workspace_roots", []) if isinstance(registry, dict) else []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("scheduler registry load failed: %s", exc)
            workspace_roots = []
        for raw_root in workspace_roots:
            workspace_root = _normalize_workspace_root(str(raw_root))
            if not workspace_root:
                continue
            self._load_file(_project_schedule_file(workspace_root), workspace_root=workspace_root)

    def _load_file(self, path: Path, *, workspace_root: str = "") -> None:
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("scheduler state must be an object")
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            # A corrupt state file must not silently masquerade as "no tasks".
            # Quarantine the file so the next _save cannot destroy the only
            # copy, and keep structured evidence on the scheduler.
            self.last_load_error = {
                "path": str(path),
                "reason": type(exc).__name__,
                "detail": str(exc),
            }
            logger.error(
                "Scheduled task state %s is unreadable (%s: %s); quarantining it. "
                "Tasks in this file are preserved but NOT loaded.",
                path,
                type(exc).__name__,
                exc,
            )
            try:
                quarantine = path.with_name(
                    f"{path.name}.corrupt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
                )
                path.replace(quarantine)
                self.last_load_error["quarantined_to"] = str(quarantine)
            except OSError as quarantine_exc:
                self.last_load_error["quarantine_error"] = str(quarantine_exc)
                logger.error("Failed to quarantine corrupt scheduler state %s: %s", path, quarantine_exc)
            return
        for raw in data.get("tasks", []):
            if not isinstance(raw, dict):
                continue
            task = ScheduledTask.from_dict(raw)
            task.workspace_root = _normalize_workspace_root(task.workspace_root or workspace_root)
            if workspace_root and task.workspace_root != workspace_root:
                logger.warning("Ignoring task %s with mismatched workspace store", task.id)
                continue
            if task.deleted_at is not None:
                task.next_run_at = None
            elif task.enabled:
                try:
                    next_run = _stored_next_run(task)
                except (ZoneInfoNotFoundError, ValueError) as exc:
                    # A stored task whose schedule can no longer be computed
                    # must be visible evidence, never a silent UTC shift.
                    logger.error(
                        "Task %s has an unusable schedule/timezone (%s); it will not fire until fixed.",
                        task.id,
                        exc,
                    )
                    self._unusable_schedule_ids.add(task.id)
                    task.next_run_at = None
                else:
                    task.next_run_at = next_run.isoformat() if next_run else None
            else:
                task.next_run_at = None
            self._tasks[task.id] = task
        for raw in data.get("runs", []):
            if not isinstance(raw, dict):
                continue
            run = ScheduledTaskRun.from_dict(raw)
            run.workspace_root = _normalize_workspace_root(run.workspace_root or workspace_root)
            if workspace_root and run.workspace_root != workspace_root:
                logger.warning("Ignoring scheduled run %s with mismatched workspace store", run.id)
                continue
            if run.task_id:
                self._runs[run.id] = run

    def _save(self) -> None:
        # Keep recent history bounded.  It is user-facing diagnostics, not an
        # unbounded execution log.
        history = sorted(
            self._runs.values(),
            key=lambda run: (run.started_at or run.scheduled_at, run.id),
            reverse=True,
        )[:500]
        tasks_by_workspace: dict[str, list[ScheduledTask]] = {}
        runs_by_workspace: dict[str, list[ScheduledTaskRun]] = {}
        for task in self._tasks.values():
            task.workspace_root = _normalize_workspace_root(task.workspace_root)
            tasks_by_workspace.setdefault(task.workspace_root, []).append(task)
        for run in history:
            run.workspace_root = _normalize_workspace_root(run.workspace_root)
            runs_by_workspace.setdefault(run.workspace_root, []).append(run)

        # Keep only unbound legacy/embedded tasks in the process-level file.
        self._write_state_file(
            SCHEDULE_FILE,
            tasks_by_workspace.get("", []),
            runs_by_workspace.get("", []),
        )
        workspace_roots = sorted(root for root in set(tasks_by_workspace) | set(runs_by_workspace) if root)
        for workspace_root in workspace_roots:
            self._write_state_file(
                _project_schedule_file(workspace_root),
                tasks_by_workspace.get(workspace_root, []),
                runs_by_workspace.get(workspace_root, []),
            )
        registry_file = _registry_file()
        atomic_write_text(
            registry_file,
            json.dumps({"version": 1, "workspace_roots": workspace_roots}, indent=2),
            encoding="utf-8",
        )
        self._notify_changed()

    @staticmethod
    def _write_state_file(path: Path, tasks: list[ScheduledTask], runs: list[ScheduledTaskRun]) -> None:
        atomic_write_text(
            path,
            json.dumps(
                {
                    "version": 4,
                    "tasks": [task.to_dict() for task in tasks],
                    "runs": [run.to_dict() for run in runs],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def set_on_change(self, callback: Callable[[], Awaitable[None] | None] | None) -> None:
        self._on_change = callback

    def _notify_changed(self) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            outcome = callback()
            if inspect.isawaitable(outcome):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    close = getattr(outcome, "close", None)
                    if callable(close):
                        close()
                    return
                loop.create_task(outcome)
        except Exception:
            logger.debug("Failed to notify scheduled task observers", exc_info=True)

    def _sweep_expired(self) -> None:
        """MiniCode auto-expires recurring schedules after 7 days."""
        now = time.time()
        changed = False
        for task in list(self._tasks.values()):
            if task.deleted_at is not None or not task.recurring:
                continue
            try:
                created = datetime.fromisoformat(task.created_at).timestamp()
            except ValueError:
                continue
            if now - created > SCHEDULED_TASK_MAX_AGE_SECONDS:
                task.deleted_at = datetime.now(UTC).isoformat()
                task.next_run_at = None
                changed = True
        if changed:
            self._save()

    def list_tasks(self, *, workspace_root: str | None = None) -> list[dict[str, Any]]:
        self._sweep_expired()
        requested_workspace = _normalize_workspace_root(workspace_root) if workspace_root is not None else None
        rows: list[dict[str, Any]] = []
        for task in self._tasks.values():
            if task.deleted_at is not None:
                continue
            if requested_workspace is not None and task.workspace_root != requested_workspace:
                continue
            row = task.to_dict()
            if task.enabled:
                try:
                    next_run = _stored_next_run(task)
                except (ZoneInfoNotFoundError, ValueError) as exc:
                    # Keep a malformed persisted task visible while preventing
                    # one bad timezone/schedule from breaking the whole list.
                    logger.error(
                        "Task %s has an unusable schedule/timezone (%s); it will not fire until fixed.",
                        task.id,
                        exc,
                    )
                    self._unusable_schedule_ids.add(task.id)
                    next_run = None
            else:
                next_run = None
            row["next_run_at"] = next_run.isoformat() if next_run else None
            rows.append(row)
        return sorted(rows, key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")), reverse=True)

    def list_runs(self, *, task_id: str | None = None, workspace_root: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        requested_workspace = _normalize_workspace_root(workspace_root) if workspace_root is not None else None
        rows = [
            run.to_dict()
            for run in self._runs.values()
            if (not task_id or run.task_id == task_id)
            and (requested_workspace is None or run.workspace_root == requested_workspace)
        ]
        rows.sort(key=lambda row: (str(row.get("started_at") or row.get("scheduled_at") or ""), str(row.get("id") or "")), reverse=True)
        return rows[:max(1, int(limit))]

    def add_task(
        self,
        name: str,
        prompt: str,
        schedule: str,
        permission_mode: str = "confirm",
        *,
        workspace_root: str = "",
        conversation_id: str = "",
        timezone: str = "",
        isolation: str = "worktree",
        recurring: bool = True,
    ) -> ScheduledTask:
        existing = self.list_tasks()
        if len([t for t in existing if t.get("status") != "deleted"]) >= MAX_SCHEDULED_JOBS:
            raise RuntimeError(
                f"Too many scheduled jobs (max {MAX_SCHEDULED_JOBS}). Cancel one first."
            )
        clean_timezone = str(timezone or "").strip()
        if clean_timezone and not is_valid_timezone(clean_timezone):
            raise ValueError(
                f"Unknown timezone '{clean_timezone}'. Use an IANA zone name (e.g. Asia/Shanghai) or leave it empty."
            )
        task = ScheduledTask(
            name=name,
            prompt=prompt,
            schedule=schedule,
            permission_mode=permission_mode,
            workspace_root=_normalize_workspace_root(workspace_root),
            conversation_id=conversation_id,
            timezone=str(timezone or "").strip(),
            isolation="workspace" if isolation == "workspace" else "worktree",
            recurring=bool(recurring),
        )
        next_run = next_run_after(task.schedule, datetime.now(UTC), timezone=task.timezone)
        task.next_run_at = next_run.isoformat() if next_run else None
        self._tasks[task.id] = task
        self._save()
        return task

    def remove_task(self, task_id: str, *, workspace_root: str | None = None) -> bool:
        task = self._tasks.get(task_id)
        if task is None or not self._workspace_matches(task.workspace_root, workspace_root):
            return False
        del self._tasks[task_id]
        self._save()
        return True

    def toggle_task(self, task_id: str, enabled: bool, *, workspace_root: str | None = None) -> bool:
        task = self._tasks.get(task_id)
        if (
            task is None
            or task.deleted_at is not None
            or not self._workspace_matches(task.workspace_root, workspace_root)
        ):
            return False
        if enabled and task.timezone and not is_valid_timezone(task.timezone):
            raise ValueError(
                f"Task '{task.id}' has unknown timezone '{task.timezone}'; fix the timezone before enabling."
            )
        self._unusable_schedule_ids.discard(task_id)
        task.enabled = enabled
        if enabled:
            next_run = next_run_after(task.schedule, datetime.now(UTC), timezone=task.timezone)
            task.next_run_at = next_run.isoformat() if next_run else None
        else:
            task.next_run_at = None
        self._save()
        return True

    def run_now(self, task_id: str, *, workspace_root: str | None = None) -> ScheduledTaskRun | None:
        task = self._tasks.get(task_id)
        if (
            task is None
            or task.deleted_at is not None
            or not self._workspace_matches(task.workspace_root, workspace_root)
        ):
            return None
        for run_id, worker in self._run_tasks.items():
            active_run = self._runs.get(run_id)
            if active_run is not None and active_run.task_id == task_id and not worker.done():
                return active_run
        return self._schedule_fire(task, datetime.now(UTC), consume_schedule=False)

    def retry_run(self, run_id: str, *, workspace_root: str | None = None) -> ScheduledTaskRun | None:
        previous = self._runs.get(run_id)
        if previous is None or not self._workspace_matches(previous.workspace_root, workspace_root):
            return None
        task = self._tasks.get(previous.task_id)
        if task is None or task.deleted_at is not None:
            return None
        return self.run_now(previous.task_id, workspace_root=workspace_root)

    def cancel_run(self, run_id: str, *, workspace_root: str | None = None) -> bool:
        active = self._run_tasks.get(run_id)
        run = self._runs.get(run_id)
        if active is None or active.done() or run is None or not self._workspace_matches(run.workspace_root, workspace_root):
            return False
        active.cancel()
        run.status = "cancelled"
        run.finished_at = datetime.now(UTC).isoformat()
        run.cleanup_pending = True
        run.cleanup_reason = "cancel_requested"
        run.cleanup_requested_at = run.finished_at
        run.cleanup_completed_at = None
        self._save()
        return True

    async def destroy_for_conversation(self, conversation_id: str) -> int:
        """Cancel and remove schedules/runs that can still write to a deleted chat."""

        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        task_ids = {
            task_id
            for task_id, task in self._tasks.items()
            if str(task.conversation_id or "").strip() == owner
        }
        run_ids = {
            run_id
            for run_id, run in self._runs.items()
            if run.task_id in task_ids or str(run.conversation_id or "").strip() == owner
        }
        workers = [
            worker
            for run_id, worker in self._run_tasks.items()
            if run_id in run_ids and not worker.done()
        ]
        still_pending = await cancel_and_drain(
            workers,
            timeout=None,
            label=f"scheduled runs for conversation {owner}",
        )
        if still_pending:
            raise RuntimeError("a scheduled run did not stop within the lifecycle deadline")
        for run_id in run_ids:
            self._run_tasks.pop(run_id, None)
            self._runs.pop(run_id, None)
        for task_id in task_ids:
            self._tasks.pop(task_id, None)
        if task_ids or run_ids:
            self._save()
        return len(task_ids) + len(run_ids)

    @staticmethod
    def _workspace_matches(stored: str, requested: str | None) -> bool:
        return requested is None or _normalize_workspace_root(stored) == _normalize_workspace_root(requested)

    async def start(self) -> None:
        """Start the scheduler loop."""
        if self._running:
            return
        self._running = True
        try:
            pending = self._reconcile_orphaned_runs()
            for task, run in pending:
                self._start_worker(task, run)
            self._loop_task = asyncio.create_task(self._run_loop())
        except Exception:
            self._running = False
            raise
        logger.info("TaskScheduler started")

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        active_workers = [worker for worker in self._run_tasks.values() if not worker.done()]
        if active_workers:
            receipt = await cancel_and_drain_receipt(
                active_workers,
                timeout=None,
                label="scheduled task workers",
            )
            still_pending = {
                worker for worker in active_workers if not worker.done()
            }
            if receipt.pending:
                finished_at = datetime.now(UTC).isoformat()
                for run_id, worker in list(self._run_tasks.items()):
                    if worker not in still_pending:
                        continue
                    run = self._runs.get(run_id)
                    if run is None:
                        continue
                    run.status = "cancelled"
                    run.error = "cancelled during scheduler shutdown"
                    run.finished_at = finished_at
                    run.cleanup_pending = True
                    run.cleanup_reason = "scheduler_shutdown_pending"
                    run.cleanup_requested_at = run.cleanup_requested_at or finished_at
                    run.cleanup_completed_at = None
                self._save()
        self._run_tasks = {
            run_id: worker
            for run_id, worker in self._run_tasks.items()
            if not worker.done()
        }
        logger.info("TaskScheduler stopped")

    async def _run_loop(self) -> None:
        """Main scheduler loop - checks tasks every 60 seconds."""
        try:
            while self._running:
                self._tick(datetime.now(UTC))
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    def _tick(self, now: datetime) -> None:
        for task in list(self._tasks.values()):
            if (
                task.deleted_at is not None
                or not task.enabled
                or self._active_run_for_task(task.id) is not None
            ):
                continue
            if task.id in self._unusable_schedule_ids:
                continue
            try:
                due_points = _missed_schedule_points(task, now)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                # A broken stored schedule must not kill the scheduler loop;
                # record evidence and stop firing this task until it is fixed.
                logger.error(
                    "Task %s has an unusable schedule/timezone (%s); it will not fire until fixed.",
                    task.id,
                    exc,
                )
                self._unusable_schedule_ids.add(task.id)
                continue
            if not due_points:
                continue
            try:
                self._schedule_fire(task, due_points[-1], consume_schedule=True)
            except OSError:
                logger.error("Failed to durably claim scheduled task %s", task.id, exc_info=True)

    def _active_run_for_task(self, task_id: str) -> ScheduledTaskRun | None:
        for run_id, worker in self._run_tasks.items():
            run = self._runs.get(run_id)
            if run is not None and run.task_id == task_id and not worker.done():
                return run
        return None

    def _reconcile_orphaned_runs(self) -> list[tuple[ScheduledTask, ScheduledTaskRun]]:
        """Resolve persisted runs left behind by a prior process."""
        pending: list[tuple[ScheduledTask, ScheduledTaskRun]] = []
        replaying_task_ids: set[str] = set()
        changed = False
        interrupted_at = datetime.now(UTC).isoformat()
        for run in sorted(self._runs.values(), key=lambda item: (item.scheduled_at, item.id)):
            task = self._tasks.get(run.task_id)
            if run.status == "running":
                run.status = "failed"
                run.finished_at = interrupted_at
                run.error = "interrupted by scheduler restart"
                changed = True
            elif run.status == "pending":
                if task is None:
                    run.status = "failed"
                    run.finished_at = interrupted_at
                    run.error = "scheduled task definition is missing"
                    changed = True
                elif task.id in replaying_task_ids:
                    run.status = "failed"
                    run.finished_at = interrupted_at
                    run.error = "duplicate pending run suppressed during scheduler restart"
                    changed = True
                else:
                    replaying_task_ids.add(task.id)
                    pending.append((task, run))
            if task is not None and run.status == "failed" and task.last_run_id == run.id:
                task.last_run_status = "failed"
                task.last_error = run.error
        if changed:
            self._save()
        return pending

    def _start_worker(
        self,
        task: ScheduledTask,
        run: ScheduledTaskRun,
        *,
        start_gate: asyncio.Event | None = None,
    ) -> asyncio.Task[None]:
        async def execute() -> None:
            if start_gate is not None:
                await start_gate.wait()
            await self._fire_one(task, run)

        worker = asyncio.create_task(execute())
        self._run_tasks[run.id] = worker
        return worker

    def _schedule_fire(
        self,
        task: ScheduledTask,
        due_at: datetime,
        *,
        consume_schedule: bool,
    ) -> ScheduledTaskRun:
        if task.deleted_at is not None:
            raise ValueError(f"Cannot schedule deleted task '{task.id}'")
        run = ScheduledTaskRun(
            task_id=task.id,
            scheduled_at=due_at.astimezone(UTC).isoformat(),
            workspace_root=task.workspace_root,
            conversation_id=task.conversation_id,
        )
        previous = (
            task.last_run_at,
            task.next_run_at,
            task.deleted_at,
            task.last_run_id,
            task.last_run_status,
            task.last_error,
        )
        start_gate = asyncio.Event()
        worker = self._start_worker(task, run, start_gate=start_gate)
        self._runs[run.id] = run
        claimed_at = datetime.now(UTC)
        task.last_run_at = claimed_at.isoformat()
        if not task.recurring:
            # cc one-shot (recurring=false): fire once, then auto-delete.
            task.next_run_at = None
            task.deleted_at = claimed_at.isoformat()
        elif consume_schedule:
            next_run = next_run_after(task.schedule, claimed_at, timezone=task.timezone)
            task.next_run_at = next_run.isoformat() if next_run else None
        task.last_run_id = run.id
        task.last_run_status = "pending"
        task.last_error = None
        try:
            # The worker already exists but is gated.  Once this commit
            # succeeds, a crash leaves a replayable pending run; before it
            # succeeds, the durable schedule cursor remains unconsumed.
            self._save()
        except Exception:
            (
                task.last_run_at,
                task.next_run_at,
                task.deleted_at,
                task.last_run_id,
                task.last_run_status,
                task.last_error,
            ) = previous
            self._runs.pop(run.id, None)
            self._run_tasks.pop(run.id, None)
            worker.cancel()
            raise
        start_gate.set()
        return run

    async def _fire_one(self, task: ScheduledTask, run: ScheduledTaskRun) -> None:
        try:
            run.status = "running"
            run.started_at = datetime.now(UTC).isoformat()
            task.last_run_status = "running"
            self._save()
            if self._on_fire is None:
                raise RuntimeError("Scheduled task runner is not configured")
            result = await self._invoke_callback(task, run)
            result = result if isinstance(result, dict) else {}
            run.status = str(result.get("status") or "completed")
            run.conversation_id = str(result.get("conversation_id") or run.conversation_id)
            reported_workspace = _normalize_workspace_root(result.get("workspace_root"))
            # The worker may report its resolved path, but it must never move
            # a run into another project's history.  The task binding is the
            # authority for both persistence and notification routing.
            if reported_workspace and reported_workspace != task.workspace_root:
                logger.warning(
                    "scheduled task %s reported a different workspace; keeping bound workspace",
                    task.id,
                )
            elif reported_workspace:
                run.workspace_root = reported_workspace
            run.result_summary = str(result.get("summary") or result.get("reply") or "")[:8000]
            run.error = str(result.get("error") or "")[:4000]
            if run.status not in {"completed", "partial", "failed", "cancelled"}:
                run.status = "completed" if not run.error else "failed"
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.error = "cancelled"
            raise
        except Exception as exc:
            logger.error("scheduled task %s fire failed: %s", task.id, exc)
            run.status = "failed"
            run.error = str(exc)[:4000]
        finally:
            run.finished_at = datetime.now(UTC).isoformat()
            if run.cleanup_requested_at is not None:
                run.cleanup_pending = False
                run.cleanup_completed_at = run.finished_at
            task.last_run_id = run.id
            task.last_run_status = run.status
            task.last_error = run.error or None
            self._run_tasks.pop(run.id, None)
            try:
                self._save()
            except OSError:
                logger.error("Failed to persist terminal state for scheduled run %s", run.id, exc_info=True)

    async def _invoke_callback(self, task: ScheduledTask, run: ScheduledTaskRun) -> dict[str, Any] | None:
        callback = self._on_fire
        if callback is None:
            return None
        try:
            parameters = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            result = callback(task, run)
        else:
            result = callback(task, run) if len(parameters) >= 2 else callback(task)
        return await result


_GLOBAL_SCHEDULER: TaskScheduler | None = None


def get_global_scheduler(on_fire: TaskFireCallback | None = None) -> TaskScheduler:
    """Return the process-wide scheduler used by websocket handlers and bootstrap."""
    global _GLOBAL_SCHEDULER
    if _GLOBAL_SCHEDULER is None:
        _GLOBAL_SCHEDULER = TaskScheduler(on_fire=on_fire)
    elif on_fire is not None:
        _GLOBAL_SCHEDULER._on_fire = on_fire
    return _GLOBAL_SCHEDULER


def reset_global_scheduler_for_tests() -> None:
    global _GLOBAL_SCHEDULER
    _GLOBAL_SCHEDULER = None
