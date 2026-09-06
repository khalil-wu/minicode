from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import backend.tasks.scheduler as scheduler_module
from backend.tasks.scheduler import ScheduledTask, ScheduledTaskRun, TaskScheduler, next_run_after


@pytest.mark.parametrize("operation", ["remove", "destroy_conversation"])
def test_removing_the_last_project_task_clears_its_actual_store(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "state" / "tasks.json")
    project = tmp_path / "project"
    other = tmp_path / "other"
    scheduler = TaskScheduler()
    task = scheduler.add_task("audit", "audit", "0 * * * *", workspace_root=str(project), conversation_id="owner")
    retained = scheduler.add_task("other", "other", "0 * * * *", workspace_root=str(other))
    if operation == "remove":
        assert scheduler.remove_task(task.id, workspace_root=str(project))
    else:
        assert asyncio.run(scheduler.destroy_for_conversation("owner")) == 1

    state = json.loads((project / ".minicode" / "scheduled_tasks.json").read_text(encoding="utf-8"))
    assert state["tasks"] == []
    assert state["runs"] == []
    restored = TaskScheduler()
    assert [item["id"] for item in restored.list_tasks()] == [retained.id]


def test_history_retention_preserves_unfinished_runs_in_memory_and_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "state" / "tasks.json")
    scheduler = TaskScheduler()
    task = scheduler.add_task("audit", "audit", "0 * * * *", workspace_root=str(tmp_path / "project"))
    now = datetime.now(UTC)
    for name, status, cleanup in [("pending", "pending", False), ("running", "running", False), ("cleanup", "cancelled", True)]:
        scheduler._runs[name] = ScheduledTaskRun(
            id=name, task_id=task.id, status=status, cleanup_pending=cleanup,
            workspace_root=task.workspace_root, started_at=(now - timedelta(days=1)).isoformat(),
        )
    for number in range(501):
        run = ScheduledTaskRun(
            id=f"done-{number}", task_id=task.id, status="completed",
            workspace_root=task.workspace_root, started_at=(now + timedelta(seconds=number)).isoformat(),
        )
        scheduler._runs[run.id] = run
    scheduler._save()
    restored = TaskScheduler()

    for instance in (scheduler, restored):
        assert len(instance._runs) == 503
        assert {"pending", "running", "cleanup"} <= instance._runs.keys()
        assert "done-0" not in instance._runs
    pending = restored._reconcile_orphaned_runs()
    assert [run.id for _, run in pending] == ["pending"]
    assert restored._runs["cleanup"].cleanup_pending


@pytest.mark.parametrize("case", ["expired", "bad_timezone", "bad_schedule"])
def test_scheduler_tick_does_not_fire_expired_or_invalid_persisted_tasks(tmp_path, monkeypatch, case):
    path = tmp_path / "tasks.json"
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", path)
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
    path.write_text(json.dumps({"tasks": [task.to_dict()], "runs": []}), encoding="utf-8")
    fired = []

    async def on_fire(task, run):
        fired.append(task.id)
        return {"status": "completed"}

    async def run():
        scheduler = TaskScheduler(on_fire=on_fire)
        try:
            # Do not list tasks first: expiry must run in the scheduler itself.
            scheduler._tick(now)
            await asyncio.sleep(0)
            assert fired == []
            assert not scheduler._run_tasks
            if case == "expired":
                assert scheduler._tasks[task.id].deleted_at is not None
                assert TaskScheduler()._tasks[task.id].deleted_at is not None
            else:
                assert task.id in scheduler._unusable_schedule_ids
                assert scheduler._tasks[task.id].deleted_at is None
        finally:
            await scheduler.stop()

    asyncio.run(run())


class _NewYorkLocalClock(datetime):
    """Model the standard library's fixed-offset local datetime result."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 15, 12, tzinfo=UTC)

    def astimezone(self, tz=None):
        converted = super().astimezone(ZoneInfo("America/New_York") if tz is None else tz)
        return converted.replace(tzinfo=timezone(converted.utcoffset())) if tz is None else converted


@pytest.mark.parametrize("start,expression,expected", [
    ((2027, 1, 15), "0 9 * * *", datetime(2027, 1, 15, 14, tzinfo=UTC)),
    ((2027, 7, 15), "0 9 * * *", datetime(2027, 7, 15, 13, tzinfo=UTC)),
    ((2027, 3, 14), "30 2 * * *", datetime(2027, 3, 15, 6, 30, tzinfo=UTC)),
])
def test_local_schedule_uses_the_offset_at_the_future_instant(monkeypatch, start, expression, expected):
    monkeypatch.setattr(scheduler_module, "datetime", _NewYorkLocalClock)
    result = next_run_after(expression, _NewYorkLocalClock(*start, tzinfo=UTC), timezone="")
    assert result == expected
