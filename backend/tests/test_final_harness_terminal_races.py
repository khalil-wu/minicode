from __future__ import annotations

import asyncio
import sys

import pytest

from backend.agent.message import AgentEvent
from backend.agent.query_engine import QueryEngine
from backend.sandbox import SandboxPolicy
from backend.terminal.manager import BackgroundCommandManager
from backend.tests.test_subagent_startup_lifecycle import _launch, startup_case


@pytest.mark.asyncio
async def test_command_cancel_after_real_exit_keeps_terminal_fact_and_pending_notification(tmp_path):
    notifying, release = asyncio.Event(), asyncio.Event()
    notifications = []

    async def completed(command):
        notifying.set()
        await release.wait()
        notifications.append((command.status, command.exit_code))

    manager = BackgroundCommandManager(session_id="final-command-race", on_completed=completed)
    command = await manager.run_background(
        "python -c print('command completed')",
        argv=[sys.executable, "-u", "-c", "print('command completed', flush=True)"],
        conversation_id="owner", cwd=str(tmp_path), sandbox_policy=SandboxPolicy.bypass(timeout=10),
    )
    try:
        await asyncio.wait_for(notifying.wait(), timeout=10)
        assert command.status == "completed" and command.exit_code == 0
        assert not manager._tasks[command.command_id].done()
        cancelled = await manager.cancel(command.command_id, conversation_id="owner")
        assert (command.status, command.exit_code, command.cleanup_pending) == ("completed", 0, False)
        assert (command.lifecycle.phase, command.lifecycle.status) == ("completed", "completed")
        assert cancelled is False
        release.set()
        await asyncio.wait_for(manager._tasks[command.command_id], timeout=5)
        assert notifications == [("completed", 0)]
    finally:
        release.set()
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_source", ["runtime", "caller"])
async def test_sealed_child_completion_survives_cancel_during_done_delivery(startup_case, monkeypatch, cancel_source):
    case = startup_case
    notifying, release = asyncio.Event(), asyncio.Event()
    emit = case.context.emit_event

    async def submit(engine, submission):
        submission.state.reply = "The child completed its actual task."
        yield AgentEvent.done(status="completed", reason="success")

    async def gated_emit(event_type, payload):
        if event_type == "subagent.done":
            notifying.set()
            await release.wait()
        await emit(event_type, payload)

    monkeypatch.setattr(QueryEngine, "submit", submit)
    case.context.emit_event = gated_emit
    child_id, _ = await _launch(case, "background")
    try:
        await asyncio.wait_for(notifying.wait(), timeout=5)
        assert case.runtime.get_subagent(child_id).status == "completed"
        worker = case.workers[0]
        assert not worker.done()
        if cancel_source == "runtime":
            stop = case.runtime.cancel_subagent_task(child_id, reason="late user interrupt")
        else:
            worker.cancel()
            stop = None
        release.set()
        outcomes = await asyncio.gather(*case.workers, return_exceptions=True)
        snapshot = case.runtime.get_subagent_snapshot(child_id)
        assert (snapshot["status"], snapshot["result"]["status"]) == ("completed", "completed")
        assert snapshot["result"]["content"] == "The child completed its actual task."
        assert [payload["status"] for kind, payload in case.events if kind == "subagent.done"] == ["completed"]
        assert all(not isinstance(outcome, BaseException) for outcome in outcomes)
        if cancel_source == "runtime":
            assert stop == "done"
        record = case.runtime.get_subagent(child_id)
        retained = case.runtime.store_subagent_result(child_id, status="cancelled", content="late cancellation",
            agent_path=record.agent_path, mailbox_epoch=record.mailbox_epoch)
        assert (retained.status, retained.content) == ("completed", "The child completed its actual task.")
    finally:
        release.set()
        await asyncio.gather(*case.workers, return_exceptions=True)
