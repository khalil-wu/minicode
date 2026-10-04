from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.scheduler_service import SchedulerServiceError, scheduled_run_history, toggle_scheduled_task
from backend.ws.handlers import mcp as handlers


class Scheduler:
    def __init__(self):
        self.calls = []

    def toggle_task(self, task_id, enabled, **kwargs):
        self.calls.append(("toggle", task_id, enabled, kwargs))
        return True

    def list_tasks(self, **kwargs):
        return []

    def list_runs(self, **kwargs):
        self.calls.append(("history", kwargs))
        return []


@pytest.mark.parametrize("value", ["false", "true", None, 0, 1, []])
def test_toggle_rejects_nonboolean_before_scheduler_mutation(value):
    scheduler = Scheduler()
    with pytest.raises(SchedulerServiceError, match="boolean"):
        toggle_scheduled_task(scheduler, {"task_id": "owned", "enabled": value}, workspace_root="owned-root")
    assert scheduler.calls == []


@pytest.mark.parametrize("field", ["offset", "limit"])
@pytest.mark.parametrize("value", [True, False, 1.75, "2", None])
def test_history_does_not_coerce_or_truncate_invalid_page_values(field, value):
    scheduler = Scheduler()
    with pytest.raises(SchedulerServiceError):
        scheduled_run_history(scheduler, {field: value}, workspace_root="owned-root")
    assert scheduler.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command,payload", [
    ("scheduler.toggle", {"task_id": "owned", "enabled": "false"}),
    ("scheduler.history", {"offset": 1.5}),
])
async def test_invalid_scheduler_requests_settle_as_ws_errors_without_dispatching_scheduler(tmp_path, monkeypatch, command, payload):
    scheduler = Scheduler()
    events = []
    root = str(tmp_path)

    async def send_event(event):
        events.append(event)

    session = SimpleNamespace(
        active_conversation_id="owned", send_event=send_event,
        conversation_repo=SimpleNamespace(get_conversation=lambda cid: SimpleNamespace(id=cid, workspace_root=root, worktree_path="")),
        session_lifecycle=SimpleNamespace(workspace_root=tmp_path, current_workspace_root=lambda: tmp_path),
        resolve_requested_workspace=lambda requested: tmp_path,
    )
    monkeypatch.setattr(handlers, "_get_scheduler", lambda session: scheduler)
    handler = handlers.handle_scheduler_toggle if command == "scheduler.toggle" else handlers.handle_scheduler_history
    assert await handler(session, {**payload, "owner_conversation_id": "owned", "workspace_root": root})
    assert scheduler.calls == []
    assert len(events) == 1
    assert events[0].type == "command.result" and events[0].data["level"] == "error"


@pytest.mark.asyncio
async def test_toggle_default_receipt_matches_committed_true_value(tmp_path, monkeypatch):
    scheduler = Scheduler()
    events = []

    async def send_event(event):
        events.append(event)

    async def send_payload(payload, **kwargs):
        pass

    session = SimpleNamespace(
        active_conversation_id="owned", send_event=send_event, send_payload=send_payload,
        session_lifecycle=SimpleNamespace(workspace_root=tmp_path, current_workspace_root=lambda: tmp_path),
        resolve_requested_workspace=lambda requested: tmp_path,
    )
    monkeypatch.setattr(handlers, "_get_scheduler", lambda session: scheduler)
    assert await handlers.handle_scheduler_toggle(session, {"task_id": "owned"})
    assert scheduler.calls[0][2] is True
    assert events[0].data["data"]["enabled"] is True
