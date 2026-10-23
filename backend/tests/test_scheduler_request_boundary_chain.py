from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.scheduler_service import SchedulerServiceError, scheduled_run_history, toggle_scheduled_task
from backend.ws.handlers import mcp as handlers
from backend.tasks import scheduler as scheduler_module
from backend.tasks.scheduler import ScheduledTaskRun, TaskScheduler


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
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    scheduler = TaskScheduler()
    task = scheduler.add_task("Default toggle", "Keep owned task", "0 9 * * *", workspace_root=str(tmp_path), conversation_id="owned")
    scheduler.toggle_task(task.id, False, workspace_root=str(tmp_path))
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
    assert await handlers.handle_scheduler_toggle(session, {"task_id": task.id})
    receipt = events[0].data["data"]
    assert receipt["enabled"] is receipt["task"]["enabled"] is True
    assert receipt["task_id"] == receipt["task"]["id"] == task.id
    assert TaskScheduler().list_tasks(workspace_root=str(tmp_path))[0]["enabled"] is True


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


@pytest.mark.asyncio
@pytest.mark.parametrize("command,enabled", [("scheduler.toggle", False), ("scheduler.toggle", True), ("scheduler.remove", None)])
async def test_toggle_and_remove_ack_keep_the_original_execution_owner_after_snapshot(tmp_path, monkeypatch, command, enabled):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    view_root, execution_root, root_b = tmp_path / "view-A", tmp_path / "execution-A", tmp_path / "B"
    for root in (view_root, execution_root, root_b):
        root.mkdir()
    scheduler = TaskScheduler()
    task = scheduler.add_task("Captured A", "Only mutate A", "0 9 * * *", workspace_root=str(execution_root), conversation_id="A")
    other = scheduler.add_task("B only", "Keep B", "0 9 * * *", workspace_root=str(root_b), conversation_id="B")
    if command == "scheduler.toggle":
        scheduler.toggle_task(task.id, not enabled, workspace_root=str(execution_root))
    events, snapshots = [], []

    async def send_event(event):
        events.append(event)

    async def send_payload(payload, **kwargs):
        snapshots.append(payload)
        session.active_conversation_id = "B"
        session.session_lifecycle.workspace_root = root_b
        if command == "scheduler.toggle":
            scheduler.toggle_task(task.id, not enabled, workspace_root=str(execution_root))

    session = SimpleNamespace(
        session_id="scheduler-controls-receipt", active_conversation_id="A", send_event=send_event, send_payload=send_payload,
        conversation_repo=SimpleNamespace(get_conversation=lambda cid: SimpleNamespace(id=cid,
            workspace_root=str(view_root if cid == "A" else root_b), execution_cwd=str(execution_root) if cid == "A" else "", worktree_path="")),
        session_lifecycle=SimpleNamespace(workspace_root=view_root, current_workspace_root=lambda: session.session_lifecycle.workspace_root),
        resolve_requested_workspace=lambda requested: Path(requested or session.session_lifecycle.workspace_root),
    )
    monkeypatch.setattr(handlers, "_get_scheduler", lambda session: scheduler)
    payload = {"owner_conversation_id": "A", "workspace_root": str(execution_root), "request_id": "captured-control", "task_id": task.id}
    if command == "scheduler.toggle":
        payload["enabled"] = enabled
    handler = handlers.handle_scheduler_toggle if command == "scheduler.toggle" else handlers.handle_scheduler_remove
    assert await handler(session, payload)
    receipt = events[-1].data["data"]
    assert receipt["conversation_id"] == "A"
    assert receipt["workspace_root"] == receipt["execution_cwd"] == str(execution_root)
    assert receipt["request_id"] == "captured-control" and receipt["task_id"] == task.id
    assert snapshots[0]["conversation_id"] == "A" and snapshots[0]["workspace_root"] == str(execution_root)
    assert session.active_conversation_id == "B" and session.session_lifecycle.workspace_root == root_b
    assert scheduler.list_tasks(workspace_root=str(root_b))[0]["id"] == other.id
    assert scheduler.list_tasks(workspace_root=str(root_b))[0]["enabled"] is True
    if command == "scheduler.toggle":
        assert receipt["enabled"] is receipt["task"]["enabled"] is enabled
        assert receipt["task"] == snapshots[0]["tasks"][0]
        assert receipt["task"]["id"] == task.id and receipt["task"]["conversation_id"] == "A"
        assert receipt["task"]["workspace_root"] == str(execution_root)
        assert scheduler.list_tasks(workspace_root=str(execution_root))[0]["enabled"] is not enabled
    else:
        assert snapshots[0]["tasks"] == []
        assert scheduler.list_tasks(workspace_root=str(execution_root)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["scheduler.toggle", "scheduler.remove"])
async def test_task_controls_preserve_the_original_not_found_error_for_another_workspace(tmp_path, monkeypatch, command):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    root_a, root_b = tmp_path / "A", tmp_path / "B"
    root_a.mkdir()
    root_b.mkdir()
    scheduler = TaskScheduler()
    other = scheduler.add_task("B only", "Keep B", "0 9 * * *", workspace_root=str(root_b), conversation_id="B")
    events, snapshots = [], []

    async def send_event(event):
        events.append(event)

    async def send_payload(payload, **kwargs):
        snapshots.append(payload)

    session = SimpleNamespace(
        active_conversation_id="A", send_event=send_event, send_payload=send_payload,
        session_lifecycle=SimpleNamespace(workspace_root=root_a, current_workspace_root=lambda: root_a),
        resolve_requested_workspace=lambda requested: root_a,
    )
    monkeypatch.setattr(handlers, "_get_scheduler", lambda session: scheduler)
    handler = handlers.handle_scheduler_toggle if command == "scheduler.toggle" else handlers.handle_scheduler_remove
    assert await handler(session, {"task_id": other.id, "enabled": False, "owner_conversation_id": "A", "workspace_root": str(root_a)})
    assert snapshots == [] and len(events) == 1
    assert events[0].data["level"] == "error"
    assert events[0].data["message"] == f"Task '{other.id}' not found"
    assert scheduler.list_tasks(workspace_root=str(root_b))[0]["enabled"] is True


@pytest.mark.asyncio
async def test_target_thread_discovers_and_operates_the_explicit_management_root_without_switching_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "global.json")
    root_a, root_b, root_c = (tmp_path / name for name in ("A", "B", "C"))
    for root in (root_a, root_b, root_c):
        root.mkdir()
    hold = {"run": False}
    release = asyncio.Event()

    async def fire(task, run):
        run.start_receipt.set_result({"status": "started", "turn_id": f"turn:{run.id}"})
        if hold["run"]:
            await release.wait()
        return {"status": "completed", "workspace_root": task.workspace_root, "execution_workspace_root": str(root_b), "conversation_id": "B"}

    scheduler = TaskScheduler(on_fire=fire)
    scheduler.set_heartbeat_gate(lambda *_args, **_kwargs: None)
    owned = scheduler.add_task("Managed by A", "Continue B", "0 9 * * *", workspace_root=str(root_a), conversation_id="B")
    unrelated = scheduler.add_task("Unrelated A", "Do not expose", "0 9 * * *", workspace_root=str(root_a), conversation_id="unrelated-A")
    local = scheduler.add_task("Local B", "Keep B", "0 9 * * *", workspace_root=str(root_b))
    history = ScheduledTaskRun(id="owned-history", task_id=owned.id, workspace_root=str(root_a), conversation_id="B", status="completed")
    hidden = ScheduledTaskRun(id="unrelated-history", task_id=unrelated.id, workspace_root=str(root_a), status="completed")
    scheduler._runs.update({history.id: history, hidden.id: hidden})
    scheduler._save()
    events, snapshots = [], []

    async def send_event(event):
        events.append(event)

    async def send_payload(payload, **kwargs):
        snapshots.append(payload)

    session = SimpleNamespace(
        session_id="scheduler-target-route", active_conversation_id="B", draft="Keep B draft", send_event=send_event, send_payload=send_payload,
        conversation_repo=SimpleNamespace(get_conversation=lambda cid: SimpleNamespace(id=cid, workspace_root=str(root_b), execution_cwd=str(root_b), worktree_path="")),
        session_lifecycle=SimpleNamespace(workspace_root=root_b, current_workspace_root=lambda: root_b),
        resolve_requested_workspace=lambda requested: Path(requested or root_b),
    )
    monkeypatch.setattr(handlers, "_get_scheduler", lambda session: scheduler)

    async def command(handler, **fields):
        assert await handler(session, {"owner_conversation_id": "B", "workspace_root": str(root_b), "request_id": "B-request", **fields})
        assert session.active_conversation_id == "B" and session.draft == "Keep B draft"
        return events[-1].data

    listed = await command(handlers.handle_scheduler_list)
    assert {task["id"] for task in listed["data"]["tasks"]} == {owned.id, local.id}
    assert {run["id"] for run in listed["data"]["runs"]} == {history.id}
    assert snapshots[-1]["workspace_root"] == str(root_b)
    directed = await command(handlers.handle_scheduler_list, task_workspace_root=str(root_a))
    assert directed["data"]["workspace_root"] == str(root_b)
    assert {task["id"] for task in directed["data"]["tasks"]} == {owned.id, unrelated.id}
    assert snapshots[-1]["workspace_root"] == str(root_a) and snapshots[-1]["conversation_id"] == "B"

    configuration = {"task_id": owned.id, "task_workspace_root": str(root_a), "name": "Edited in B", "prompt": "B's new prompt",
        "schedule": "RRULE:FREQ=MINUTELY;INTERVAL=30", "permission_mode": "auto", "timezone": "UTC", "isolation": "workspace", "kind": "heartbeat", "conversation_id": "B"}
    updated = await command(handlers.handle_scheduler_update, **configuration)
    assert updated["data"]["workspace_root"] == str(root_b)
    assert updated["data"]["task"]["workspace_root"] == str(root_a) and updated["data"]["task"]["id"] == owned.id
    assert {task["id"] for task in snapshots[-1]["tasks"]} == {owned.id, local.id}
    for enabled in (False, True):
        toggled = await command(handlers.handle_scheduler_toggle, task_id=owned.id, task_workspace_root=str(root_a), enabled=enabled)
        assert toggled["data"]["enabled"] is toggled["data"]["task"]["enabled"] is enabled
        assert toggled["data"]["conversation_id"] == "B" and toggled["data"]["task"]["workspace_root"] == str(root_a)
    page = await command(handlers.handle_scheduler_history, task_id=owned.id, task_workspace_root=str(root_a))
    assert [run["id"] for run in page["data"]["runs"]] == [history.id]

    refused = await command(handlers.handle_scheduler_run_now, task_id=owned.id)
    assert refused["level"] == "error" and refused["message"] == f"Task '{owned.id}' not found"
    started = await command(handlers.handle_scheduler_run_now, task_id=owned.id, task_workspace_root=str(root_a))
    assert started["data"]["workspace_root"] == str(root_b) and started["data"]["task_id"] == owned.id
    await asyncio.gather(*tuple(scheduler._run_tasks.values()))
    await asyncio.sleep(0)
    first_run = scheduler._tasks[owned.id].last_run_id
    hold["run"] = True
    retried = await command(handlers.handle_scheduler_retry, run_id=first_run, task_workspace_root=str(root_a))
    assert retried["data"]["workspace_root"] == str(root_b)
    retry_run = scheduler._tasks[owned.id].last_run_id
    cancelled = await command(handlers.handle_scheduler_cancel, run_id=retry_run, task_workspace_root=str(root_a))
    assert cancelled["data"]["workspace_root"] == str(root_b) and cancelled["data"]["run_id"] == retry_run
    await asyncio.gather(*tuple(scheduler._run_tasks.values()), return_exceptions=True)
    await asyncio.sleep(0)
    assert scheduler._runs[retry_run].status == "cancelled"

    moved_target = await command(handlers.handle_scheduler_update, **{**configuration, "conversation_id": "C"})
    assert moved_target["data"]["task"]["conversation_id"] == "C" and moved_target["data"]["task"]["workspace_root"] == str(root_a)
    assert {task["id"] for task in snapshots[-1]["tasks"]} == {local.id}
    directed = await command(handlers.handle_scheduler_list, task_workspace_root=str(root_a))
    assert next(task for task in directed["data"]["tasks"] if task["id"] == owned.id)["conversation_id"] == "C"
    removed = await command(handlers.handle_scheduler_remove, task_id=owned.id, task_workspace_root=str(root_a))
    assert removed["data"]["task_id"] == owned.id and removed["data"]["workspace_root"] == str(root_b)
    assert [task["id"] for task in scheduler.list_tasks(workspace_root=str(root_a))] == [unrelated.id]
    assert [task["id"] for task in scheduler.list_tasks(workspace_root=str(root_b))] == [local.id]
