"""External checks for the historical scheduled-work state failures."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo


workspace = Path.cwd().resolve()
sys.path.insert(0, str(workspace))

import backend
import backend.tasks.scheduler as scheduler_module
from backend.tasks.scheduler import ScheduledTask, ScheduledTaskRun, TaskScheduler, next_run_after


if not Path(backend.__file__).resolve().is_relative_to(workspace):
    raise RuntimeError("oracle imported backend outside the task checkout")


class NewYorkLocalClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 15, 12, tzinfo=UTC)

    def astimezone(self, tz=None):
        converted = super().astimezone(ZoneInfo("America/New_York") if tz is None else tz)
        return converted.replace(tzinfo=timezone(converted.utcoffset())) if tz is None else converted


class SchedulerOracle(unittest.TestCase):
    def test_last_workspace_task_stays_removed_after_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(scheduler_module, "SCHEDULE_FILE", root / "state/tasks.json"):
                scheduler = TaskScheduler()
                project = root / "project"
                other = root / "other"
                removed = scheduler.add_task("audit", "audit", "0 * * * *", workspace_root=str(project))
                retained = scheduler.add_task("other", "other", "0 * * * *", workspace_root=str(other))
                self.assertTrue(scheduler.remove_task(removed.id, workspace_root=str(project)))
                state_file = project / ".minicode/scheduled_tasks.json"
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    self.assertEqual(state["tasks"], [])
                    self.assertEqual(state["runs"], [])
                self.assertEqual([task["id"] for task in TaskScheduler().list_tasks()], [retained.id])

    def test_pending_and_cleanup_runs_survive_history_limit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(scheduler_module, "SCHEDULE_FILE", root / "state/tasks.json"):
                scheduler = TaskScheduler()
                task = scheduler.add_task("audit", "audit", "0 * * * *", workspace_root=str(root / "project"))
                now = datetime.now(UTC)
                for name, status, cleanup in (
                    ("pending", "pending", False),
                    ("running", "running", False),
                    ("cleanup", "cancelled", True),
                ):
                    scheduler._runs[name] = ScheduledTaskRun(
                        id=name, task_id=task.id, status=status,
                        cleanup_pending=cleanup, workspace_root=task.workspace_root,
                        started_at=(now - timedelta(days=1)).isoformat(),
                    )
                for number in range(501):
                    run = ScheduledTaskRun(
                        id=f"done-{number}", task_id=task.id, status="completed",
                        workspace_root=task.workspace_root,
                        started_at=(now + timedelta(seconds=number)).isoformat(),
                    )
                    scheduler._runs[run.id] = run
                scheduler._save()
                restored = TaskScheduler()
                for instance in (scheduler, restored):
                    self.assertTrue(
                        {"pending", "running", "cleanup"} <= instance._runs.keys(),
                        "an unfinished or cleanup-owned run was pruned",
                    )
                    self.assertTrue("done-0" not in instance._runs, "old completed history was not trimmed")
                self.assertTrue(restored._runs["cleanup"].cleanup_pending)

    def test_expired_and_invalid_persisted_tasks_never_fire(self):
        for case in ("expired", "bad_timezone", "bad_schedule"):
            with self.subTest(case=case), TemporaryDirectory() as directory:
                root = Path(directory)
                state_file = root / "tasks.json"
                now = datetime.now(UTC)
                task = ScheduledTask(
                    id="audit", prompt="audit", schedule="* * * * *",
                    next_run_at=(now - timedelta(minutes=1)).isoformat(),
                )
                if case == "expired":
                    task.created_at = (now - timedelta(days=8)).isoformat()
                else:
                    task.recurring = False
                    if case == "bad_timezone":
                        task.timezone = "Mars/Phobos"
                    else:
                        task.schedule = "invalid cron"
                state_file.write_text(json.dumps({"tasks": [task.to_dict()], "runs": []}), encoding="utf-8")
                fired = []

                async def on_fire(task, run):
                    fired.append(task.id)
                    return {"status": "completed"}

                async def check():
                    scheduler = TaskScheduler(on_fire=on_fire)
                    try:
                        scheduler._tick(now)
                        await asyncio.sleep(0)
                        self.assertEqual(fired, [])
                        self.assertFalse(scheduler._run_tasks)
                        if case == "expired":
                            self.assertIsNotNone(scheduler._tasks[task.id].deleted_at)
                        else:
                            self.assertIn(task.id, scheduler._unusable_schedule_ids)
                    finally:
                        await scheduler.stop()

                with patch.object(scheduler_module, "SCHEDULE_FILE", state_file):
                    asyncio.run(check())

    def test_local_time_uses_future_dst_offset(self):
        examples = (
            ((2027, 1, 15), "0 9 * * *", datetime(2027, 1, 15, 14, tzinfo=UTC)),
            ((2027, 7, 15), "0 9 * * *", datetime(2027, 7, 15, 13, tzinfo=UTC)),
        )
        with patch.object(scheduler_module, "datetime", NewYorkLocalClock):
            for start, expression, expected in examples:
                with self.subTest(start=start):
                    result = next_run_after(expression, NewYorkLocalClock(*start, tzinfo=UTC), timezone="")
                    self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
