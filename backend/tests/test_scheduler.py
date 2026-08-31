import asyncio
import json
from datetime import UTC, datetime, timedelta

from backend.tasks import scheduler as scheduler_module
from backend.tasks.scheduler import (
    ScheduledTask,
    ScheduledTaskRun,
    TaskScheduler,
    _missed_schedule_points,
    cron_matches,
    next_run_after,
)


def test_cron_next_run_after_matches_next_minute() -> None:
    now = datetime(2026, 6, 22, 8, 58, 20, tzinfo=UTC)

    next_run = next_run_after("0 9 * * 1-5", now)

    assert next_run == datetime(2026, 6, 22, 9, 0, tzinfo=UTC)
    assert cron_matches("0 9 * * 1-5", next_run)


def test_invalid_cron_has_no_next_run() -> None:
    now = datetime(2026, 6, 22, 8, 58, tzinfo=UTC)

    assert next_run_after("not a cron", now) is None
    assert cron_matches("not a cron", now) is False


def test_cron_supports_ranges_steps_sunday_seven_and_dom_dow_or() -> None:
    sunday = datetime(2026, 6, 28, 9, 10, tzinfo=UTC)
    monday = datetime(2026, 6, 29, 9, 10, tzinfo=UTC)

    assert cron_matches("*/5 9 * * 7", sunday)
    assert not cron_matches("*/5 9 * * 7", monday)
    # When both day fields are restricted, standard cron uses OR: June 29 is
    # not day 1, but it is Monday.
    assert cron_matches("10 9 1 6 1", monday)
    assert not cron_matches("*/0 9 * * *", monday)


def test_next_run_respects_project_timezone() -> None:
    now = datetime(2026, 6, 22, 0, 58, 20, tzinfo=UTC)

    next_run = next_run_after("0 9 * * 1-5", now, timezone="Asia/Shanghai")

    assert next_run == datetime(2026, 6, 22, 1, 0, tzinfo=UTC)
    assert cron_matches("0 9 * * 1-5", next_run, timezone="Asia/Shanghai")


def test_scheduler_list_tasks_includes_computed_next_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")
    scheduler = TaskScheduler()
    task = scheduler.add_task("Morning check", "Check build", "0 9 * * 1-5")

    rows = scheduler.list_tasks()

    assert rows[0]["id"] == task.id
    assert rows[0]["next_run_at"]

    scheduler.toggle_task(task.id, False)
    rows = scheduler.list_tasks()
    assert rows[0]["next_run_at"] is None


def test_missed_schedule_points_catches_up_without_repeating_same_minute() -> None:
    task = ScheduledTask(
        name="Every minute",
        prompt="check",
        schedule="* * * * *",
        last_run_at=datetime(2026, 6, 22, 9, 0, 30, tzinfo=UTC).isoformat(),
    )
    now = datetime(2026, 6, 22, 9, 3, 45, tzinfo=UTC)

    due = _missed_schedule_points(task, now)

    assert due == [datetime(2026, 6, 22, 9, 1, tzinfo=UTC)]


def test_missed_schedule_points_does_not_duplicate_current_minute() -> None:
    task = ScheduledTask(
        name="Every minute",
        prompt="check",
        schedule="* * * * *",
        last_run_at=datetime(2026, 6, 22, 9, 3, 5, tzinfo=UTC).isoformat(),
    )
    now = datetime(2026, 6, 22, 9, 3, 45, tzinfo=UTC)

    assert _missed_schedule_points(task, now) == []


def test_run_now_invokes_runner_and_persists_project_scoped_history(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")
    project_one = tmp_path / "project-one"
    project_two = tmp_path / "project-two"
    project_one.mkdir()
    project_two.mkdir()
    observed: list[tuple[str, str]] = []

    async def on_fire(task, run):
        observed.append((task.id, run.id))
        return {
            "status": "completed",
            "conversation_id": "conv_schedule_run",
            "workspace_root": "C:/projects/one",
            "summary": "Build is green",
        }

    async def scenario() -> None:
        scheduler = TaskScheduler(on_fire=on_fire)
        task = scheduler.add_task(
            "Build",
            "Run tests",
            "0 * * * *",
            workspace_root=str(project_one),
        )
        scheduler.add_task(
            "Other",
            "Ignore",
            "0 * * * *",
            workspace_root=str(project_two),
        )
        run = scheduler.run_now(task.id)
        assert run is not None
        active = scheduler._run_tasks[run.id]
        await active

        assert observed == [(task.id, run.id)]
        assert scheduler.list_tasks(workspace_root=str(project_one))[0]["last_run_status"] == "completed"
        history = scheduler.list_runs(workspace_root=str(project_one))
        assert history[0]["conversation_id"] == "conv_schedule_run"
        assert history[0]["result_summary"] == "Build is green"
        assert scheduler.list_tasks(workspace_root=str(project_two))[0]["name"] == "Other"

    asyncio.run(scenario())


def test_retry_and_cancel_preserve_run_history(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")
    project = tmp_path / "project"
    project.mkdir()

    async def on_fire(_task, _run):
        await asyncio.sleep(5)
        return {"status": "completed"}

    async def scenario() -> None:
        scheduler = TaskScheduler(on_fire=on_fire)
        task = scheduler.add_task("Build", "Run tests", "0 * * * *", workspace_root=str(project))
        run = scheduler.run_now(task.id)
        assert run is not None
        assert scheduler.cancel_run(run.id) is True
        await asyncio.sleep(0)
        history = scheduler.list_runs(workspace_root=str(project))
        assert history[0]["status"] == "cancelled"
        retry = scheduler.retry_run(run.id)
        assert retry is not None
        assert retry.id != run.id
        assert scheduler.cancel_run(retry.id) is True
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_scheduler_persists_each_workspace_in_its_own_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduler" / "legacy.json")
    project_one = tmp_path / "project-one"
    project_two = tmp_path / "project-two"
    project_one.mkdir()
    project_two.mkdir()

    scheduler = TaskScheduler()
    first = scheduler.add_task("One", "Check one", "0 * * * *", workspace_root=str(project_one))
    second = scheduler.add_task("Two", "Check two", "0 * * * *", workspace_root=str(project_two))

    first_file = project_one / ".minicode" / "scheduled_tasks.json"
    second_file = project_two / ".minicode" / "scheduled_tasks.json"
    assert first_file.exists()
    assert second_file.exists()
    assert {item["id"] for item in json.loads(first_file.read_text(encoding="utf-8"))["tasks"]} == {first.id}
    assert {item["id"] for item in json.loads(second_file.read_text(encoding="utf-8"))["tasks"]} == {second.id}

    restored = TaskScheduler()
    assert [item["id"] for item in restored.list_tasks(workspace_root=str(project_one))] == [first.id]
    assert [item["id"] for item in restored.list_tasks(workspace_root=str(project_two))] == [second.id]


def test_scheduler_mutations_cannot_cross_workspace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduler" / "legacy.json")
    project_one = tmp_path / "project-one"
    project_two = tmp_path / "project-two"
    project_one.mkdir()
    project_two.mkdir()
    scheduler = TaskScheduler()
    task = scheduler.add_task("One", "Check one", "0 * * * *", workspace_root=str(project_one))

    assert scheduler.toggle_task(task.id, False, workspace_root=str(project_two)) is False
    assert scheduler.remove_task(task.id, workspace_root=str(project_two)) is False
    assert scheduler.run_now(task.id, workspace_root=str(project_two)) is None
    assert scheduler.list_tasks(workspace_root=str(project_one))[0]["enabled"] is True


def test_legacy_tasks_migrate_to_workspace_isolation() -> None:
    task = ScheduledTask.from_dict({"name": "Legacy", "prompt": "check", "schedule": "0 * * * *"})

    assert task.isolation == "workspace"


def test_run_now_reuses_active_run_and_stop_cancels_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")
    project = tmp_path / "project"
    project.mkdir()

    async def on_fire(_task, _run):
        await asyncio.sleep(30)
        return {"status": "completed"}

    async def scenario() -> None:
        scheduler = TaskScheduler(on_fire=on_fire)
        task = scheduler.add_task("Long task", "wait", "0 * * * *", workspace_root=str(project))
        first = scheduler.run_now(task.id)
        second = scheduler.run_now(task.id)
        assert first is not None
        assert second is first

        await asyncio.sleep(0)
        await scheduler.stop()

        history = scheduler.list_runs(workspace_root=str(project))
        assert history[0]["status"] == "cancelled"
        assert scheduler._run_tasks == {}

    asyncio.run(scenario())


def test_scheduler_start_ticks_immediately_and_catches_up_persisted_next_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")

    async def scenario() -> None:
        seed = TaskScheduler()
        task = seed.add_task("Catch up", "run now", "* * * * *")
        due_at = datetime.now(UTC) - timedelta(minutes=3)
        task.next_run_at = due_at.isoformat()
        seed._save()

        fired = asyncio.Event()

        async def on_fire(_task, _run):
            fired.set()
            return {"status": "completed"}

        restored = TaskScheduler(on_fire=on_fire)
        assert restored._tasks[task.id].next_run_at == due_at.isoformat()
        await restored.start()
        await asyncio.wait_for(fired.wait(), timeout=1.0)
        await asyncio.sleep(0)
        await restored.stop()

        history = restored.list_runs(task_id=task.id)
        assert len(history) == 1
        assert history[0]["scheduled_at"] == due_at.isoformat()

    asyncio.run(scenario())


def test_scheduler_replays_pending_orphan_with_original_run_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")

    async def scenario() -> None:
        seed = TaskScheduler()
        task = seed.add_task("Replay", "resume pending", "0 0 1 1 *")
        task.next_run_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        orphan = ScheduledTaskRun(
            id="schedule_run_pending_orphan",
            task_id=task.id,
            scheduled_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            status="pending",
        )
        seed._runs[orphan.id] = orphan
        task.last_run_id = orphan.id
        task.last_run_status = "pending"
        seed._save()

        observed: list[str] = []
        completed = asyncio.Event()

        async def on_fire(_task, run):
            observed.append(run.id)
            completed.set()
            return {"status": "completed"}

        restored = TaskScheduler(on_fire=on_fire)
        await restored.start()
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        worker = restored._run_tasks.get(orphan.id)
        if worker is not None:
            await worker
        await restored.stop()

        assert observed == [orphan.id]
        assert restored._runs[orphan.id].status == "completed"

    asyncio.run(scenario())


def test_scheduler_marks_running_orphan_failed_on_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")

    async def scenario() -> None:
        seed = TaskScheduler()
        task = seed.add_task("Interrupted", "do work", "0 0 1 1 *")
        task.next_run_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        orphan = ScheduledTaskRun(
            id="schedule_run_running_orphan",
            task_id=task.id,
            scheduled_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
            started_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            status="running",
        )
        seed._runs[orphan.id] = orphan
        task.last_run_id = orphan.id
        task.last_run_status = "running"
        seed._save()

        restored = TaskScheduler(on_fire=lambda *_args: None)
        await restored.start()
        assert restored._runs[orphan.id].status == "failed"
        assert restored._runs[orphan.id].error == "interrupted by scheduler restart"
        assert restored._tasks[task.id].last_run_status == "failed"
        await restored.stop()

    asyncio.run(scenario())


def test_scheduler_claim_persistence_failure_never_releases_worker(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")

    async def scenario() -> None:
        fired = False

        async def on_fire(_task, _run):
            nonlocal fired
            fired = True
            return {"status": "completed"}

        scheduler = TaskScheduler(on_fire=on_fire)
        task = scheduler.add_task("Durable claim", "run", "* * * * *")
        due_at = datetime.now(UTC) - timedelta(minutes=1)
        task.next_run_at = due_at.isoformat()
        monkeypatch.setattr(scheduler, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))

        scheduler._tick(datetime.now(UTC))
        await asyncio.sleep(0)

        assert fired is False
        assert scheduler._runs == {}
        assert scheduler._run_tasks == {}
        assert task.next_run_at == due_at.isoformat()
        assert task.last_run_id is None

    asyncio.run(scenario())


def test_scheduler_does_not_double_fire_while_task_worker_is_active(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_fire(_task, _run):
            started.set()
            await release.wait()
            return {"status": "completed"}

        scheduler = TaskScheduler(on_fire=on_fire)
        task = scheduler.add_task("No duplicate", "run", "* * * * *")
        task.next_run_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

        scheduler._tick(datetime.now(UTC))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        scheduler._tick(datetime.now(UTC) + timedelta(minutes=2))

        assert len([run for run in scheduler._runs.values() if run.task_id == task.id]) == 1
        release.set()
        worker = next(iter(scheduler._run_tasks.values()))
        await worker

    asyncio.run(scenario())


def test_scheduler_does_not_retry_callback_body_type_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "scheduled_tasks.json")
    calls = 0

    def on_fire(_task, _run):
        nonlocal calls
        calls += 1
        raise TypeError("callback body failed")

    async def scenario() -> None:
        scheduler = TaskScheduler(on_fire=on_fire)
        task = scheduler.add_task("Single call", "run", "0 * * * *")
        run = scheduler.run_now(task.id)
        assert run is not None
        await scheduler._run_tasks[run.id]

        assert calls == 1
        assert run.status == "failed"
        assert run.error == "callback body failed"

    asyncio.run(scenario())
