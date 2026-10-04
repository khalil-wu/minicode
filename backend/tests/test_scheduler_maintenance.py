import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from backend.services.scheduler_service import SchedulerServiceError, scheduled_run_history, update_scheduled_task
from backend.tasks import scheduler as scheduler_module
from backend.tasks.scheduler import ScheduledTaskRun, TaskScheduler


def scheduler_case(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "state" / "scheduled_tasks.json")
    root = str(tmp_path / "project")
    return TaskScheduler(), root


def test_edit_preserves_id_history_and_recomputes_next_run(monkeypatch, tmp_path):
    scheduler, root = scheduler_case(monkeypatch, tmp_path)
    task = scheduler.add_task("Old", "Old prompt", "0 9 * * *", workspace_root=root, timezone="UTC")
    run = ScheduledTaskRun(task_id=task.id, workspace_root=task.workspace_root, status="completed")
    scheduler._runs[run.id] = run
    update_scheduled_task(scheduler, {"task_id": task.id, "name": "Edited", "prompt": "New prompt", "schedule": "30 10 * * 1-5", "timezone": "Asia/Shanghai", "isolation": "workspace", "permission_mode": "confirm", "conversation_id": "original-conversation"}, workspace_root=root)
    restored = TaskScheduler()
    row = restored.list_tasks(workspace_root=root)[0]
    assert row["id"] == task.id
    assert row["name"] == "Edited"
    assert row["prompt"] == "New prompt"
    assert row["next_run_at"] is not None
    assert restored.list_runs(workspace_root=root)[0]["id"] == run.id
    with pytest.raises(SchedulerServiceError, match="not found"):
        update_scheduled_task(scheduler, {"task_id": task.id, "name": "Wrong owner", "prompt": "New", "schedule": "0 * * * *", "permission_mode": "auto"}, workspace_root=str(tmp_path / "other"))
    assert task.name == "Edited"


def test_history_pages_reach_all_runs_and_remain_scoped(monkeypatch, tmp_path):
    scheduler, root = scheduler_case(monkeypatch, tmp_path)
    task = scheduler.add_task("History", "Inspect", "0 * * * *", workspace_root=root)
    for index in range(121):
        run = ScheduledTaskRun(task_id=task.id, workspace_root=task.workspace_root, scheduled_at=(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(), status="completed")
        scheduler._runs[run.id] = run
    other = ScheduledTaskRun(task_id="other", workspace_root=str(tmp_path / "other"))
    scheduler._runs[other.id] = other
    pages = [scheduled_run_history(scheduler, {"task_id": task.id, "limit": 50, "offset": offset}, workspace_root=root) for offset in (0, 50, 100)]
    assert [len(page["runs"]) for page in pages] == [50, 50, 21]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert len({run["id"] for page in pages for run in page["runs"]}) == 121
    assert all(run["task_id"] == task.id for page in pages for run in page["runs"])


def test_edit_during_execution_changes_only_future_input(monkeypatch, tmp_path):
    scheduler, root = scheduler_case(monkeypatch, tmp_path)

    async def exercise():
        release = asyncio.Event()
        started = asyncio.Event()
        seen = []

        async def on_fire(task, run):
            started.set()
            await release.wait()
            seen.append((task.name, task.prompt, task.schedule))
            return {"status": "completed"}

        scheduler._on_fire = on_fire
        task = scheduler.add_task("Original", "Original prompt", "0 * * * *", workspace_root=root)
        run = scheduler.run_now(task.id, workspace_root=root)
        await started.wait()
        update_scheduled_task(scheduler, {"task_id": task.id, "name": "Future", "prompt": "Future prompt", "schedule": "0 10 * * *", "permission_mode": "auto"}, workspace_root=root)
        release.set()
        await scheduler._run_tasks[run.id]
        assert seen == [("Original", "Original prompt", "0 * * * *")]
        assert scheduler.list_tasks(workspace_root=root)[0]["prompt"] == "Future prompt"
        await scheduler.stop()

    asyncio.run(exercise())
