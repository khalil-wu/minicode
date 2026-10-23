from __future__ import annotations

import pytest

from backend.services.scheduler_service import (
    SchedulerServiceError,
    add_scheduled_task,
    scheduled_permission_mode,
    update_scheduled_task,
)
from backend.tasks import scheduler as scheduler_module
from backend.tasks.scheduler import ScheduledTask, TaskScheduler


class _Scheduler:
    def __init__(self) -> None:
        self.added: list[dict] = []

    def add_task(self, **kwargs):
        task = ScheduledTask(**kwargs)
        self.added.append(task.to_dict())
        return task

    def list_tasks(self, *, workspace_root=None):
        return list(self.added)

    def list_runs(self, *, workspace_root=None):
        return []


def test_scheduled_permission_mode_rejects_unattended_bypass() -> None:
    scheduler = _Scheduler()
    with pytest.raises(SchedulerServiceError, match="confirm.*auto"):
        add_scheduled_task(
            scheduler,
            {
                "name": "unsafe",
                "prompt": "run anything",
                "schedule": "0 9 * * *",
                "permission_mode": "bypass",
            },
            workspace_root="C:/repo",
        )
    assert scheduler.added == []
    assert scheduled_permission_mode("bypass") == "confirm"


def test_scheduled_permission_mode_allows_normal_auto_policy() -> None:
    scheduler = _Scheduler()
    add_scheduled_task(
        scheduler,
        {
            "name": "daily",
            "prompt": "inspect the project",
            "schedule": "0 9 * * *",
            "permission_mode": "auto",
        },
        workspace_root="C:/repo",
    )
    assert scheduler.added[0]["permission_mode"] == "auto"


def test_task_mutations_return_the_durable_record_with_a_stable_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    root = tmp_path / "project"
    root.mkdir()
    scheduler = TaskScheduler()
    added = add_scheduled_task(scheduler, {
        "name": "Captured task", "prompt": "Continue A", "schedule": "0 9 * * *",
        "permission_mode": "auto", "conversation_id": "owner-A", "isolation": "workspace",
    }, workspace_root=str(root))
    task_id = added.task["id"]
    assert added.task["conversation_id"] == "owner-A"
    assert added.task["kind"] == "heartbeat"
    assert added.tasks[0]["id"] == task_id
    scheduler.toggle_task(task_id, False, workspace_root=str(root))
    updated = update_scheduled_task(scheduler, {
        "task_id": task_id, "name": "Edited task", "prompt": "Keep A's identity", "schedule": "20 10 * * 1-5",
        "permission_mode": "confirm", "conversation_id": "owner-A", "isolation": "workspace",
    }, workspace_root=str(root))
    assert updated.task["id"] == task_id
    assert updated.task["prompt"] == "Keep A's identity"
    assert updated.task["enabled"] is False
    assert TaskScheduler().list_tasks(workspace_root=str(root))[0] == updated.task


def test_task_configuration_preserves_raw_rrule_execution_model_and_notification_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    managed, execution = tmp_path / "managed", tmp_path / "execution"
    managed.mkdir()
    execution.mkdir()
    scheduler = TaskScheduler()
    rule = "DTSTART:20270101T000000Z\nRRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0;BYSECOND=0"
    payload = {"name": "Raw rule", "prompt": "Independent run", "schedule": rule, "permission_mode": "auto", "kind": "cron",
        "conversation_id": "", "execution_workspace_root": str(execution), "provider": "openai", "model": "gpt-5",
        "reasoning_effort": "high", "notification_policy": "failed_runs_only"}
    added = add_scheduled_task(scheduler, payload, workspace_root=str(managed))
    task_id = added.task["id"]
    assert added.task["schedule"] == rule
    assert added.task["next_run_at"] == "2027-01-01T00:00:00+00:00"
    updated = update_scheduled_task(scheduler, {"task_id": task_id, "name": "Edited", "prompt": "Keep choices", "schedule": rule,
        "permission_mode": "auto", "conversation_id": ""}, workspace_root=str(managed))
    assert updated.task["notification_policy"] == "failed_runs_only" and updated.task["model"] == "gpt-5"
    assert updated.task["execution_workspace_root"] == str(execution)
    cleared = update_scheduled_task(scheduler, {**payload, "task_id": task_id, "execution_workspace_root": "",
        "projectless_base": str(tmp_path / "actual-default-directory"), "provider": None, "model": None,
        "reasoning_effort": None, "notification_policy": None}, workspace_root=str(managed))
    assert cleared.task["workspace_root"] == str(managed) and cleared.task["execution_workspace_root"] == ""
    assert cleared.task["notification_policy"] is cleared.task["model"] is cleared.task["provider"] is cleared.task["reasoning_effort"] is None
    assert TaskScheduler().list_tasks(workspace_root=str(managed))[0] == cleared.task
