from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.services.terminal_service import parse_terminal_exec_command
from backend.tasks import manager as task_module
from backend.terminal import task_output
from backend.terminal.manager import BackgroundCommand, BackgroundCommandManager
from backend.terminal import task_persistence


def test_durable_output_fault_keeps_written_counters_at_the_actual_file(tmp_path):
    output = task_output.DurableTaskOutput(session_id="fixture", conversation_id="conv", task_id="fault", base_dir=tmp_path)
    output._file.close()

    class FaultFile:
        def write(self, data):
            raise OSError("fixture write failed")

        def flush(self):
            pass

        def close(self):
            pass

    output._file = FaultFile()
    with pytest.raises(OSError, match="fixture write failed"):
        output.append("unwritten")
    output.close()
    assert output.characters_written == output.bytes_written == output.path.stat().st_size == 0


def test_durable_output_disk_cap_counts_retained_text_and_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(task_output, "MAX_TASK_OUTPUT_BYTES", 3)
    output = task_output.DurableTaskOutput(session_id="fixture", conversation_id="conv", task_id="cap", base_dir=tmp_path)
    output.append("123")
    output.append("not retained")
    output.append("also not retained")
    output.close()
    body = output.path.read_bytes()
    assert output.capped
    assert output.characters_written == len(body.decode("utf-8"))
    assert output.bytes_written == len(body)


def test_unreadable_background_output_retains_ownership_and_marks_the_live_tail(tmp_path):
    manager = BackgroundCommandManager(session_id="fixture")
    command = BackgroundCommand(command_id="fixture", command="fixture", conversation_id="conv", output="tail", output_chars=100, output_path=str(tmp_path / "missing.output"))
    manager._commands[command.command_id] = command
    assert manager.get_output_snapshot("fixture", conversation_id="other", max_chars=50) is None
    assert manager.get_output_snapshot("fixture", conversation_id="conv", max_chars=50) == ("tail", True, "")


@pytest.mark.asyncio
async def test_cancelling_a_waiter_does_not_request_cancellation_of_its_owned_work():
    release = asyncio.Event()
    manager = task_module.TaskManager()
    managed = manager.create("owned", release.wait())
    waiter = asyncio.create_task(manager.wait(managed.id))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    try:
        assert not managed.task.done() and managed.task.cancelling() == 0
        assert managed.status == "running"
    finally:
        release.set()
        await managed.task
        await manager.cancel_all_and_wait()


@pytest.mark.asyncio
async def test_task_timeout_uses_existing_drain_and_keeps_a_resistant_source_owned(monkeypatch):
    monkeypatch.setattr(task_module, "CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.005)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def source():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return "late cleanup result"

    manager = task_module.TaskManager(max_tasks=1)
    managed = manager.create("timeout", source(), timeout=0.005)
    await entered.wait()
    try:
        await cancelled.wait()
        with pytest.raises(TimeoutError, match="Task timed out"):
            await asyncio.wait_for(manager.wait(managed.id), timeout=0.2)
        assert managed.cleanup_pending and not managed.source_task.done()
        manager.prune()
        assert manager.get(managed.id) is managed
        manager.cancel(managed.id)
        assert managed.source_task.cancelling() == 1
    finally:
        release.set()
        await asyncio.gather(managed.task, managed.source_task, return_exceptions=True)
        await asyncio.sleep(0)
        await manager.cancel_all_and_wait()
    assert managed.status == "failed" and "Task timed out" in managed.error
    assert not managed.cleanup_pending and managed.cleanup_completed_at


@pytest.mark.asyncio
async def test_inner_timeout_error_preserves_its_actual_error_and_cancellation_state():
    async def source():
        raise TimeoutError("inner provider timeout")

    manager = task_module.TaskManager()
    managed = manager.create("inner-timeout", source(), timeout=1)
    with pytest.raises(TimeoutError, match="inner provider timeout"):
        await manager.wait(managed.id)
    assert managed.source_task.cancelling() == 0
    assert managed.status == "failed" and managed.error == "inner provider timeout"


@pytest.mark.parametrize("command", [None, 7, {"command": "anything"}])
def test_terminal_command_parser_does_not_turn_non_strings_into_shell_commands(command):
    request = parse_terminal_exec_command({"command": command})
    assert request.command == "" and request.error_event.data["message"] == "Command must be a string"


def test_unobservable_persisted_owner_keeps_cleanup_pending_without_touching_a_process(tmp_path, monkeypatch):
    task_persistence.save_task(
        session_id="fixture", task_id="owned", command="fixture", description="fixture", cwd=str(tmp_path),
        pid=None, started_at=1, timeout_ms=0, base_dir=tmp_path, conversation_id="conv",
        owner_pid=7, owner_start_time=1,
    )
    monkeypatch.setattr(task_persistence, "process_identity_matches", lambda *args: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("No process action is authorized for an unobservable owner")

    monkeypatch.setattr(task_persistence, "_terminate_owned_process", forbidden)
    recovered = task_persistence.cleanup_orphaned_tasks("fixture", base_dir=tmp_path)
    assert len(recovered) == 1
    assert recovered[0].status == "interrupted" and recovered[0].cleanup_pending
    assert recovered[0].cleanup_reason == "process_identity_unavailable"
    assert task_persistence.load_task("fixture", "owned", tmp_path) == recovered[0]


def test_unobservable_host_identity_does_not_authorize_container_cleanup(tmp_path, monkeypatch):
    task_persistence.save_task(
        session_id="fixture", task_id="container-uncertain", command="fixture", description="fixture", cwd=str(tmp_path),
        pid=None, started_at=1, timeout_ms=0, base_dir=tmp_path, owner_pid=7, owner_start_time=1,
        container_engine="fixture-engine", container_ref="fixture-container",
    )
    monkeypatch.setattr(task_persistence, "process_identity_matches", lambda *args: None)
    attempted = []
    monkeypatch.setattr("backend.sandbox.runner.cleanup_owned_container", lambda *args: attempted.append(args) or True)
    recovered = task_persistence.cleanup_orphaned_tasks("fixture", tmp_path)[0]
    assert attempted == []
    assert recovered.cleanup_pending and recovered.cleanup_reason == "process_identity_unavailable"
    assert recovered.container_ref == "fixture-container"
