from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

import backend.terminal.manager as terminal_manager_module

from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxCapability, SandboxPolicy, SandboxRunner
from backend.terminal.manager import BackgroundCommand, BackgroundCommandManager
from backend.terminal.task_persistence import (
    cleanup_orphaned_tasks,
    get_process_start_time,
    get_tasks_dir,
    load_task,
    save_task,
)
from backend.tools.command_tool import RunCommandTool
from backend.tools.monitor_tool import MonitorTool
from backend.tools.read_file import ReadFileTool


@pytest.mark.asyncio
async def test_background_command_emits_started_before_completed_with_owner() -> None:
    events: list[tuple[str, BackgroundCommand]] = []
    completed = asyncio.Event()

    async def on_started(command: BackgroundCommand) -> None:
        events.append(("started", command))

    async def on_completed(command: BackgroundCommand) -> None:
        events.append(("completed", command))
        completed.set()

    manager = BackgroundCommandManager(
        on_started=on_started,
        on_completed=on_completed,
        session_id="session-background-ready",
    )
    command = await manager.run_background(
        "echo minicode-background-ready",
        conversation_id="conv-owned",
        timeout_ms=10_000,
        sandbox_policy=SandboxPolicy.bypass(timeout=10),
    )

    await asyncio.wait_for(completed.wait(), timeout=10)

    assert [event for event, _ in events] == ["started", "completed"]
    assert command.conversation_id == "conv-owned"
    assert command.status == "completed"
    assert command.exit_code == 0
    lifecycle = command.to_dict()
    assert lifecycle["run_id"] == command.command_id
    assert lifecycle["task_id"] == command.command_id
    assert lifecycle["kind"] == "background_command"
    assert lifecycle["phase"] == "completed"
    assert lifecycle["seq"] >= 2
    assert lifecycle["completed_at_ms"] is not None
    assert lifecycle["result"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_background_command_completion_invalidates_workspace_read_cache(tmp_path) -> None:
    target = tmp_path / "generated.txt"
    target.write_text("before\n", encoding="utf-8")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path,
        conversation_id="conv-background-cache",
        metadata={"_read_file_hashes": {}},
    )
    reader = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    cached = await reader.execute({"file_path": "generated.txt"}, context)
    assert "before" in cached.content

    manager = BackgroundCommandManager(session_id="session-background-cache")
    script = (
        "from pathlib import Path; "
        f"Path(r'{target}').write_text('after\\n', encoding='utf-8')"
    )
    command_text = subprocess.list2cmdline([sys.executable, "-c", script])
    completed = asyncio.Event()

    async def on_completed(_command: BackgroundCommand) -> None:
        completed.set()

    manager._on_completed = on_completed
    await manager.run_background(
        command_text,
        conversation_id="conv-background-cache",
        cwd=str(tmp_path),
        sandbox_policy=SandboxPolicy.bypass(timeout=10),
    )
    await asyncio.wait_for(completed.wait(), timeout=10)

    fresh = await reader.execute({"file_path": "generated.txt"}, context)
    assert "after" in fresh.content
    assert "before" not in fresh.content


@pytest.mark.asyncio
async def test_background_command_cancellation_emits_terminal_update() -> None:
    started = asyncio.Event()
    completed = asyncio.Event()
    terminal_statuses: list[str] = []

    async def on_started(_command: BackgroundCommand) -> None:
        started.set()

    async def on_completed(command: BackgroundCommand) -> None:
        terminal_statuses.append(command.status)
        completed.set()

    manager = BackgroundCommandManager(
        on_started=on_started,
        on_completed=on_completed,
        session_id="session-background-cancelled",
    )
    long_command = "ping 127.0.0.1 -n 20 > nul" if os.name == "nt" else "sleep 20"
    command = await manager.run_background(
        long_command,
        conversation_id="conv-cancelled",
        timeout_ms=30_000,
        sandbox_policy=SandboxPolicy.bypass(timeout=30),
    )
    await asyncio.wait_for(started.wait(), timeout=5)

    assert await manager.cancel(command.command_id, conversation_id="conv-cancelled") is True
    await asyncio.wait_for(completed.wait(), timeout=10)

    assert terminal_statuses == ["cancelled"]


@pytest.mark.asyncio
async def test_background_command_stdin_uses_owned_sandbox_process() -> None:
    started = asyncio.Event()
    completed = asyncio.Event()

    async def on_started(_command: BackgroundCommand) -> None:
        started.set()

    async def on_completed(_command: BackgroundCommand) -> None:
        completed.set()

    manager = BackgroundCommandManager(
        on_started=on_started,
        on_completed=on_completed,
        session_id="session-background-stdin",
    )
    script = "import sys; value = sys.stdin.readline(); print('received:' + value, flush=True)"
    command = await manager.run_background(
        subprocess.list2cmdline([sys.executable, "-u", "-c", script]),
        conversation_id="conv-background-stdin",
        timeout_ms=10_000,
        sandbox_policy=SandboxPolicy.bypass(timeout=10),
    )
    await asyncio.wait_for(started.wait(), timeout=5)

    written = await manager.write_stdin(
        command.command_id,
        "hello\n",
        conversation_id="conv-background-stdin",
        close_stdin=True,
    )
    await asyncio.wait_for(completed.wait(), timeout=10)

    assert written == 6
    assert command.status == "completed"
    assert command.exit_code == 0
    assert "received:hello" in command.output
    with pytest.raises(KeyError):
        await manager.write_stdin(
            command.command_id,
            "late",
            conversation_id="conv-other",
        )


@pytest.mark.asyncio
async def test_background_start_fails_before_task_when_owner_persistence_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BackgroundCommandManager(session_id="session-owner-admission")
    executed = False

    async def should_not_execute(_command: BackgroundCommand) -> None:
        nonlocal executed
        executed = True

    def reject_owner(*_args, **_kwargs):
        raise OSError("durable owner unavailable")

    monkeypatch.setattr(manager, "_execute", should_not_execute)
    monkeypatch.setattr("backend.terminal.task_persistence.save_task", reject_owner)

    with pytest.raises(RuntimeError, match="owner could not be persisted"):
        await manager.run_background(
            "echo must-not-start",
            cwd=str(tmp_path),
            conversation_id="conv-owner-admission",
            sandbox_policy=SandboxPolicy.bypass(timeout=10),
        )

    await asyncio.sleep(0)
    assert executed is False
    assert manager._tasks == {}
    assert manager._commands == {}


@pytest.mark.asyncio
async def test_cancellation_timeout_retains_owner_across_destroy_and_shutdown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BackgroundCommandManager(session_id="session-pending-cleanup")
    release = asyncio.Event()
    task_started = asyncio.Event()
    cancellation_observed = asyncio.Event()

    async def cancellation_resistant() -> None:
        task_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observed.set()
            await release.wait()

    command = BackgroundCommand(
        command_id="bg_pending_cleanup",
        command="long-running command",
        cwd=str(tmp_path),
        status="running",
        conversation_id="conv-pending-cleanup",
    )
    task = asyncio.create_task(cancellation_resistant())
    manager._commands[command.command_id] = command
    manager._tasks[command.command_id] = task
    monkeypatch.setattr(
        terminal_manager_module,
        "CANCELLATION_DRAIN_TIMEOUT_SECONDS",
        0.01,
    )
    await asyncio.wait_for(task_started.wait(), timeout=1)

    try:
        assert await manager.cancel(
            command.command_id,
            conversation_id=command.conversation_id,
        ) is True
        await asyncio.wait_for(cancellation_observed.wait(), timeout=1)
        assert command.status == "running"
        assert command.cleanup_pending is True
        assert command.command_id in manager._tasks
        assert command.command_id in manager._commands

        # Unproven cleanup is signalled by raising, matching the sibling
        # teardown APIs (destroy_sessions_for_conversation,
        # _stop_preview_processes). A short return count was invisible to
        # conversation.delete, which discards the count, so it deleted the
        # conversation and worktree past a process that may still be alive.
        with pytest.raises(RuntimeError, match="could not be proven stopped"):
            await manager.destroy_for_conversation(command.conversation_id)
        assert command.command_id in manager._tasks
        assert command.command_id in manager._commands

        await manager.shutdown()
        assert command.command_id in manager._tasks
        assert command.command_id in manager._commands
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_background_command_exposes_live_output_until_cancelled(tmp_path) -> None:
    started = asyncio.Event()

    async def on_started(_command: BackgroundCommand) -> None:
        started.set()

    manager = BackgroundCommandManager(
        on_started=on_started,
        session_id="session-background-output",
    )
    script = "import time; print('server-ready', flush=True); time.sleep(60)"
    command_text = subprocess.list2cmdline([sys.executable, "-u", "-c", script])
    command = await manager.run_background(
        command_text,
        conversation_id="conv-live-output",
        cwd=str(tmp_path),
        timeout_ms=0,
        sandbox_policy=SandboxPolicy.bypass(timeout=0),
    )

    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        deadline = time.monotonic() + 5
        while "server-ready" not in command.output and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        snapshot = await MonitorTool().execute(
            {"command_id": command.command_id},
            context=ToolExecutionContext(
                permission=PermissionContext(mode="bypass"),
                background_manager=manager,
                conversation_id="conv-live-output",
            ),
        )

        assert command.status == "running"
        assert "server-ready" in command.output
        assert f"Background command {command.command_id} (running)" in snapshot.content
        assert "server-ready" in snapshot.content
    finally:
        await manager.cancel(command.command_id, conversation_id="conv-live-output")


@pytest.mark.asyncio
async def test_run_command_background_handoff_ignores_foreground_timeout(tmp_path) -> None:
    captured: dict[str, object] = {}

    class _Manager:
        async def run_background(self, **kwargs):
            captured.update(kwargs)
            return BackgroundCommand(
                command_id="bg_test",
                command=str(kwargs["command"]),
                cwd=str(kwargs["cwd"]),
            )

    manager = _Manager()
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path,
        background_manager=manager,
        conversation_id="conv-background-handoff",
    )

    result = await tool.execute(
        {
            "command": "npm run dev",
            "cwd": str(tmp_path),
            "timeout": 1,
            "run_in_background": True,
        },
        context=context,
    )

    assert tool.resolve_timeout({"timeout": 1, "run_in_background": True}) is None
    assert captured["timeout_ms"] == 0
    assert captured["sandbox_policy"].timeout == 0
    assert captured["conversation_id"] == "conv-background-handoff"
    assert result.status == "success"
    assert result.display_summary == "Started in background: Run"
    assert "monitor(action='status', command_id='bg_test')" in result.content
    assert "monitor(action='cancel', command_id='bg_test')" in result.content
    assert "process-name matching" in result.content


@pytest.mark.asyncio
async def test_agent_background_command_fails_closed_when_sandbox_is_unavailable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BackgroundCommandManager(session_id="session-sandbox-unavailable")
    monkeypatch.setattr(
        SandboxRunner,
        "capability",
        lambda self: SandboxCapability(
            available=False,
            backend="unavailable",
            filesystem_isolated=False,
            network_isolated=False,
            reason="test sandbox unavailable",
        ),
    )

    with pytest.raises(RuntimeError, match="Sandbox unavailable"):
        await manager.run_background(
            "echo must-not-run",
            cwd=str(tmp_path),
            conversation_id="conv-sandbox-unavailable",
            sandbox_policy=SandboxPolicy.workspace_default(tmp_path),
        )

    assert manager.list_commands(
        include_completed=True,
        conversation_id="conv-sandbox-unavailable",
    ) == []


@pytest.mark.asyncio
async def test_background_command_requires_explicit_sandbox_policy() -> None:
    manager = BackgroundCommandManager(session_id="session-explicit-sandbox")

    with pytest.raises(RuntimeError, match="explicit sandbox policy"):
        await manager.run_background(
            "echo must-not-run",
            conversation_id="conv-explicit-sandbox",
        )

    assert manager.list_commands(
        include_completed=True,
        conversation_id="conv-explicit-sandbox",
    ) == []


@pytest.mark.asyncio
async def test_background_command_requires_conversation_owner() -> None:
    manager = BackgroundCommandManager()

    with pytest.raises(RuntimeError, match="conversation owner"):
        await manager.run_background(
            "echo must-not-run",
        sandbox_policy=SandboxPolicy.bypass(timeout=10),
        )


def test_background_command_queries_are_isolated_by_conversation_owner() -> None:
    manager = BackgroundCommandManager()
    first = BackgroundCommand(
        command_id="bg_first",
        command="npm run dev",
        conversation_id="conv-first",
    )
    second = BackgroundCommand(
        command_id="bg_second",
        command="pytest",
        conversation_id="conv-second",
    )
    manager._commands = {first.command_id: first, second.command_id: second}

    assert manager.get_status(first.command_id, conversation_id="conv-first") is first
    assert manager.get_status(first.command_id, conversation_id="conv-second") is None
    assert [item["command_id"] for item in manager.list_commands(
        include_completed=True,
        conversation_id="conv-second",
    )] == [second.command_id]


def test_background_recovery_fences_dead_owner_and_terminates_detached_child(tmp_path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        save_task(
            session_id="owner-crash",
            task_id="long-task",
            command="long task",
            description="long task",
            cwd=str(tmp_path),
            pid=child.pid,
            process_start_time=get_process_start_time(child.pid),
            owner_pid=2_147_483_647,
            owner_start_time=1.0,
            started_at=time.time(),
            timeout_ms=60_000,
            base_dir=tmp_path,
        )

        orphaned = cleanup_orphaned_tasks("owner-crash", base_dir=tmp_path)

        assert [task.task_id for task in orphaned] == ["long-task"]
        child.wait(timeout=10)
        persisted = load_task("owner-crash", "long-task", base_dir=tmp_path)
        assert persisted is not None
        assert persisted.status == "interrupted"
        assert persisted.cleanup_pending is False
        assert persisted.cleanup_reason == "background_owner_exited"
        assert persisted.cleanup_requested_at is not None
        assert persisted.cleanup_completed_at is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_background_recovery_marks_legacy_live_child_uncertain_instead_of_running(tmp_path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        task_dir = get_tasks_dir("legacy-owner", base_dir=tmp_path)
        (task_dir / "legacy-live.json").write_text(
            json.dumps({
                "task_id": "legacy-live",
                "command": "legacy task",
                "description": "legacy task",
                "cwd": str(tmp_path),
                "pid": child.pid,
                "started_at": time.time(),
                "timeout_ms": 60_000,
                "status": "running",
            }),
            encoding="utf-8",
        )

        orphaned = cleanup_orphaned_tasks("legacy-owner", base_dir=tmp_path)

        assert [task.task_id for task in orphaned] == ["legacy-live"]
        assert orphaned[0].status == "interrupted"
        assert orphaned[0].cleanup_pending is True
        assert orphaned[0].cleanup_reason == "legacy_process_identity_unavailable"
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_background_recovery_does_not_kill_reused_pid_identity(tmp_path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        actual_start = get_process_start_time(child.pid)
        save_task(
            session_id="pid-reuse",
            task_id="stale-task",
            command="stale task",
            description="stale task",
            cwd=str(tmp_path),
            pid=child.pid,
            process_start_time=(actual_start or 0.0) - 10.0,
            owner_pid=2_147_483_647,
            owner_start_time=1.0,
            started_at=time.time(),
            timeout_ms=60_000,
            base_dir=tmp_path,
        )

        orphaned = cleanup_orphaned_tasks("pid-reuse", base_dir=tmp_path)

        assert [task.task_id for task in orphaned] == ["stale-task"]
        assert child.poll() is None
        persisted = load_task("pid-reuse", "stale-task", base_dir=tmp_path)
        assert persisted is not None
        assert persisted.cleanup_pending is False
    finally:
        child.kill()
        child.wait(timeout=10)


def test_background_recovery_returns_the_committed_terminal_record(tmp_path) -> None:
    save_task(
        session_id="committed-recovery",
        task_id="dead-task",
        command="python worker.py",
        description="worker",
        cwd=str(tmp_path),
        pid=2_147_483_647,
        process_start_time=1.0,
        owner_pid=2_147_483_646,
        owner_start_time=1.0,
        started_at=10.0,
        timeout_ms=60_000,
        status="running",
        conversation_id="conversation-1",
        owner_task_id="turn-1",
        parent_run_id="run-1",
        base_dir=tmp_path,
    )

    recovered = cleanup_orphaned_tasks("committed-recovery", base_dir=tmp_path)

    assert len(recovered) == 1
    record = recovered[0]
    assert record.status == "interrupted"
    assert record.cleanup_pending is False
    assert record.cleanup_reason == "background_owner_exited"
    assert record.cleanup_completed_at is not None
    assert record.conversation_id == "conversation-1"


def test_background_task_directory_honors_runtime_state_root(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path))

    tasks_dir = get_tasks_dir("desktop-session")

    assert tasks_dir == tmp_path / "data" / "agent-runtime" / "background_tasks" / "desktop-session"


@pytest.mark.parametrize("unsafe_id", ["../escape", "..\\escape", "C:\\escape", ".."])
def test_background_task_storage_rejects_path_like_ids(tmp_path, unsafe_id: str) -> None:
    with pytest.raises(ValueError):
        get_tasks_dir(unsafe_id, base_dir=tmp_path)
    with pytest.raises(ValueError):
        load_task("safe-session", unsafe_id, base_dir=tmp_path)
