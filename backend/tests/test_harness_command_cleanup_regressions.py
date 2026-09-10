from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxPolicy, SandboxResult
from backend.sandbox import runner as runner_module
from backend.terminal import manager as manager_module
from backend.terminal import task_persistence
from backend.tools import command_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["nonzero", "io"])
async def test_container_cleanup_retains_identity_and_host_exit_is_insufficient(tmp_path, monkeypatch, failure):
    runner = runner_module.SandboxRunner(SandboxPolicy.bypass())
    cid = tmp_path / "container.cid"
    cid.write_text("owned-cid", encoding="utf-8")
    runner._container_cidfile = cid
    runner._container_engine = "docker"
    runner._container_name = "minicode-owned"
    host = SimpleNamespace(pid=123456, returncode=0)
    runner.process = host
    monkeypatch.setattr(runner_module, "terminate_process_tree", AsyncMock(return_value=True))
    spawn = Mock(return_value=SimpleNamespace(returncode=1, stderr=b"permission denied"))
    if failure == "io":
        spawn.side_effect = OSError("engine unavailable")
    monkeypatch.setattr(runner_module.subprocess, "run", spawn)
    assert not await runner.cleanup()
    assert runner.process is host
    assert runner._container_name == "minicode-owned"
    assert runner._container_engine == "docker"
    assert cid.read_text(encoding="utf-8") == "owned-cid"
    spawn.side_effect = None
    spawn.return_value.returncode = 0
    assert await runner.cleanup()
    assert runner.process is None
    assert runner._container_name == ""
    assert not cid.exists()


@pytest.mark.asyncio
async def test_normal_command_projects_failed_container_cleanup(monkeypatch):
    runner = runner_module.SandboxRunner(SandboxPolicy.bypass())
    host = SimpleNamespace(pid=123456, returncode=0)
    runner.process = host
    monkeypatch.setattr(runner, "_run", AsyncMock(return_value=SandboxResult("done", "", 0)))
    monkeypatch.setattr(runner, "_cleanup_container", AsyncMock(return_value=False))
    result = await runner.run("fixture")
    assert result.cleanup_pending
    assert result.cleanup_reason == "container_cleanup_pending"
    assert runner.process is host


class PendingRunner:
    def __init__(self, policy):
        self.process = SimpleNamespace(pid=123456, returncode=None)
        self._container_name = ""
        self.container_ownership = {}
        self.cleanup = AsyncMock(side_effect=[False, True])

    async def run(self, *args, **kwargs):
        if "process_ready_callback" in kwargs:
            await kwargs["process_ready_callback"](self.process)
        return SandboxResult("partial", "I/O failed", 1, cleanup_pending=True, cleanup_reason="process_tree_survived_execution_error_kill")


@pytest.mark.asyncio
async def test_background_io_failure_retains_runner_and_cancel_retries(monkeypatch):
    monkeypatch.setattr(manager_module, "SandboxRunner", PendingRunner)
    manager = manager_module.BackgroundCommandManager()
    command = manager_module.BackgroundCommand(command_id="fixture", command="fake", conversation_id="conv", sandbox_policy=SandboxPolicy.bypass())
    manager._commands[command.command_id] = command
    task = asyncio.create_task(manager._execute(command))
    manager._tasks[command.command_id] = task
    await task
    runner = command.runner
    assert command.status == "failed" and command.cleanup_pending
    assert manager._processes[command.command_id] is runner.process
    assert await manager.cancel(command.command_id, conversation_id="conv")
    assert command.cleanup_pending and command.runner is runner
    assert await manager.cancel(command.command_id, conversation_id="conv")
    assert not command.cleanup_pending
    assert command.runner is None
    assert command.command_id not in manager._processes
    assert runner.cleanup.await_count == 2


@pytest.mark.asyncio
async def test_foreground_pending_cleanup_moves_to_existing_background_owner(tmp_path, monkeypatch):
    manager = manager_module.BackgroundCommandManager()
    monkeypatch.setattr(command_tool, "SandboxRunner", PendingRunner)
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path, conversation_id="conv", background_manager=manager)
    result = await command_tool.RunCommandTool(artifact_store=object())._execute_foreground("fixture", str(tmp_path), 10, context)
    assert result.cleanup_receipt["pending"] == 1
    command_id = result.cleanup_receipt["resource_id"]
    retained = manager.get_status(command_id, conversation_id="conv")
    assert retained.cleanup_pending and retained.runner is not None
    assert command_id in result.content
    assert await manager.cancel(command_id, conversation_id="conv")
    assert retained.cleanup_pending
    assert await manager.cancel(command_id, conversation_id="conv")
    assert not retained.cleanup_pending


def test_orphan_reaper_preserves_container_evidence_after_host_exit(tmp_path, monkeypatch):
    cidfile = tmp_path / "owned.cid"
    cidfile.write_text("owned-cid", encoding="utf-8")
    task_persistence.save_task(
        session_id="fixture-session", task_id="fixture-task", command="fake",
        description="fixture", cwd=str(tmp_path), pid=123456, started_at=1,
        timeout_ms=0, owner_pid=789012, owner_start_time=1, process_start_time=1,
        container_engine="docker", container_ref="minicode-owned", container_cidfile=str(cidfile),
        cleanup_pending=True, base_dir=tmp_path,
    )
    monkeypatch.setattr(task_persistence, "process_identity_matches", lambda *args: False)
    remove = Mock(return_value=SimpleNamespace(returncode=1, stderr=b"engine unavailable"))
    monkeypatch.setattr(runner_module.subprocess, "run", remove)
    first = task_persistence.cleanup_orphaned_tasks("fixture-session", base_dir=tmp_path)[0]
    assert first.cleanup_pending
    assert first.container_ref == "minicode-owned"
    assert first.container_cidfile == str(cidfile)
    assert cidfile.exists()
    assert remove.call_args.args[0] == ["docker", "rm", "--force", "minicode-owned"]
    remove.return_value.returncode = 0
    second = task_persistence.cleanup_orphaned_tasks("fixture-session", base_dir=tmp_path)[0]
    assert not second.cleanup_pending
    assert not cidfile.exists()
