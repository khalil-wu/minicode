from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.scheduler_service import SchedulerServiceError, scheduled_run_history, toggle_scheduled_task
from backend.ws.handlers import mcp as handlers
from backend.tasks import scheduler as scheduler_module
from backend.tasks.scheduler import TaskScheduler


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


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["scheduler.add", "scheduler.update"])
async def test_task_mutation_ack_keeps_its_real_task_when_the_reader_switches_after_snapshot(tmp_path, monkeypatch, command):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    root_a, root_b = tmp_path / "A", tmp_path / "B"
    root_a.mkdir()
    root_b.mkdir()
    scheduler = TaskScheduler()
    task_id = None
    if command == "scheduler.update":
        task_id = scheduler.add_task("Before", "Original A", "0 9 * * *", workspace_root=str(root_a), conversation_id="A").id
        scheduler.toggle_task(task_id, False, workspace_root=str(root_a))
    events, snapshots = [], []

    async def send_event(event):
        events.append(event)

    async def send_payload(payload, **kwargs):
        snapshots.append(payload)
        session.active_conversation_id = "B"
        session.session_lifecycle.workspace_root = root_b

    session = SimpleNamespace(
        session_id="scheduler-mutation-receipt", active_conversation_id="A", send_event=send_event, send_payload=send_payload,
        conversation_repo=SimpleNamespace(get_conversation=lambda cid: SimpleNamespace(id=cid, workspace_root=str(root_a if cid == "A" else root_b), worktree_path="")),
        session_lifecycle=SimpleNamespace(workspace_root=root_a, current_workspace_root=lambda: session.session_lifecycle.workspace_root),
        resolve_requested_workspace=lambda requested: root_a if requested == str(root_a) else root_b,
    )
    monkeypatch.setattr(handlers, "_get_scheduler", lambda session: scheduler)
    payload = {"owner_conversation_id": "A", "workspace_root": str(root_a), "request_id": "captured-request",
        "name": "Captured A", "prompt": "Only change A", "schedule": "20 10 * * 1-5", "permission_mode": "auto",
        "conversation_id": "A", "isolation": "workspace"}
    if task_id:
        payload["task_id"] = task_id
    handler = handlers.handle_scheduler_add if command == "scheduler.add" else handlers.handle_scheduler_update
    assert await handler(session, payload)
    assert session.active_conversation_id == "B"
    receipt = events[-1].data["data"]
    task = receipt["task"]
    assert receipt["conversation_id"] == "A"
    assert receipt["workspace_root"] == str(root_a)
    assert receipt["request_id"] == "captured-request"
    assert task["id"] == snapshots[0]["tasks"][0]["id"]
    assert task["conversation_id"] == "A" and task["workspace_root"] == str(root_a)
    assert task["prompt"] == "Only change A"
    assert scheduler.list_tasks(workspace_root=str(root_b)) == []
    assert len(scheduler.list_tasks(workspace_root=str(root_a))) == 1
    if task_id:
        assert receipt["task_id"] == task_id and task["id"] == task_id
        assert task["enabled"] is False
    else:
        assert receipt["name"] == task["name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_reader", [False, True])
async def test_list_ack_keeps_the_same_execution_snapshot_when_the_view_differs(tmp_path, monkeypatch, switch_reader):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    view_root, execution_root, root_b = tmp_path / "view-A", tmp_path / "execution-A", tmp_path / "B"
    for root in (view_root, execution_root, root_b):
        root.mkdir()
    scheduler = TaskScheduler()
    task = scheduler.add_task("Captured A", "Original execution prompt", "0 9 * * *", workspace_root=str(execution_root), conversation_id="A")
    scheduler.add_task("B only", "Other owner", "0 9 * * *", workspace_root=str(root_b), conversation_id="B")
    events, snapshots = [], []

    async def send_event(event):
        events.append(event)

    async def send_payload(payload, **kwargs):
        snapshots.append(payload)
        # The ACK must describe the list already sent, including when another
        # mutation or reader switch happens while that snapshot is delivered.
        task.prompt = "Changed after the captured snapshot"
        if switch_reader:
            session.active_conversation_id = "B"
            session.session_lifecycle.workspace_root = root_b

    session = SimpleNamespace(
        session_id="scheduler-list-receipt", active_conversation_id="A", send_event=send_event, send_payload=send_payload,
        conversation_repo=SimpleNamespace(get_conversation=lambda cid: SimpleNamespace(id=cid,
            workspace_root=str(view_root if cid == "A" else root_b), execution_cwd=str(execution_root) if cid == "A" else "", worktree_path="")),
        session_lifecycle=SimpleNamespace(workspace_root=view_root, current_workspace_root=lambda: session.session_lifecycle.workspace_root),
        resolve_requested_workspace=lambda requested: Path(requested or session.session_lifecycle.workspace_root),
    )
    monkeypatch.setattr(handlers, "_get_scheduler", lambda session: scheduler)
    assert await handlers.handle_scheduler_list(session, {"owner_conversation_id": "A", "workspace_root": str(execution_root), "request_id": "captured-list"})
    receipt = events[-1].data["data"]
    assert receipt["conversation_id"] == "A"
    assert receipt["workspace_root"] == receipt["execution_cwd"] == str(execution_root)
    assert receipt["request_id"] == "captured-list"
    assert receipt["tasks"] is snapshots[0]["tasks"]
    assert receipt["runs"] is snapshots[0]["runs"]
    assert [row["id"] for row in receipt["tasks"]] == [task.id]
    assert receipt["tasks"][0]["conversation_id"] == "A"
    assert receipt["tasks"][0]["workspace_root"] == str(execution_root)
    assert receipt["tasks"][0]["prompt"] == "Original execution prompt"
    assert task.prompt == "Changed after the captured snapshot"
    assert session.active_conversation_id == ("B" if switch_reader else "A")
    assert session.session_lifecycle.workspace_root == (root_b if switch_reader else view_root)
