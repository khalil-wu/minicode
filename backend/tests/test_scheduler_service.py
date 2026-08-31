from __future__ import annotations

import pytest

from backend.services.scheduler_service import (
    SchedulerServiceError,
    add_scheduled_task,
    scheduled_permission_mode,
)


class _Scheduler:
    def __init__(self) -> None:
        self.added: list[dict] = []

    def add_task(self, **kwargs):
        self.added.append(kwargs)

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
