from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.runtime import AgentRuntime
from backend.tasks.manager import TaskManager
from backend.ws.run_manager import SessionRunManager
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.handler import WebSocketSession


def _manager() -> SessionRunManager:
    session = SimpleNamespace(
        cleanup_tasks=set(),
        cancel_pending_approvals=AsyncMock(),
        session_lifecycle=SimpleNamespace(schedule_task_runtime_update=lambda: None),
    )
    manager = SessionRunManager(session)
    manager.watch_conversation_notifications = lambda _id: None
    manager._cancel_run_tree = AsyncMock()
    return manager


@pytest.mark.parametrize("all_runs", [False, True])
def test_stop_does_not_cancel_approvals_registered_after_the_stop_request(all_runs):
    class Session(SessionApprovalRuntimeMixin):
        cancel_pending_approvals = WebSocketSession.cancel_pending_approvals

        def __init__(self):
            self.cleanup_tasks = set()
            self.approval_diff_cache = {}
            self.session_lifecycle = SimpleNamespace(schedule_task_runtime_update=lambda: None)
            self.send_event = AsyncMock()

    async def scenario():
        session = Session()
        manager = SessionRunManager(session)
        manager.watch_conversation_notifications = lambda _id: None
        manager._cancel_run_tree = AsyncMock()
        loop = asyncio.get_running_loop()
        old_approval = loop.create_future()
        new_approval = loop.create_future()
        session.turn_wait_state.register_waiter("old-approval", old_approval)
        session.turn_wait_state.pending_approval_payloads["old-approval"] = {"conversation_id": "owner"}

        async def old_run():
            try:
                await asyncio.Event().wait()
            finally:
                session.turn_wait_state.register_waiter("new-approval", new_approval)
                session.turn_wait_state.pending_approval_payloads["new-approval"] = {"conversation_id": "owner"}

        task = asyncio.create_task(old_run())
        manager.register(conversation_id="owner", task=task, task_id="old-run", cancel_event=asyncio.Event(), active_conversation_id="owner")
        await asyncio.sleep(0)
        try:
            await manager.cancel(conversation_id=None if all_runs else "owner")
            assert old_approval.cancelled()
            assert not new_approval.done()
            assert session.turn_wait_state.pending_approvals == {"new-approval": new_approval}
            notices = [call.args[0] for call in session.send_event.call_args_list]
            assert len(notices) == 1
            assert notices[0].data["request_ids"] == ["old-approval"]
            assert notices[0].data["conversation_id"] == "owner"
        finally:
            new_approval.cancel()
            manager.stop_notification_wake_intake()

    asyncio.run(scenario())


@pytest.mark.parametrize("all_runs", [False, True])
def test_cancel_keeps_the_captured_run_when_a_new_run_replaces_it(all_runs: bool) -> None:
    async def scenario():
        manager = _manager()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stop_children(task_id, **kwargs):
            assert task_id == "old-run"
            entered.set()
            await release.wait()

        manager._cancel_run_tree = stop_children
        old = asyncio.create_task(asyncio.Event().wait())
        old_cancel = asyncio.Event()
        manager.register(conversation_id="owner", task=old, task_id="old-run", cancel_event=old_cancel, active_conversation_id="owner")
        stopping = asyncio.create_task(manager.cancel(conversation_id=None if all_runs else "owner"))
        new = None
        try:
            await entered.wait()
            assert old_cancel.is_set()
            with suppress(asyncio.CancelledError):
                await old
            new = asyncio.create_task(asyncio.Event().wait())
            new_cancel = asyncio.Event()
            manager.register(conversation_id="owner", task=new, task_id="new-run", cancel_event=new_cancel, active_conversation_id="owner")
            release.set()
            assert await stopping
            assert not new_cancel.is_set()
            assert not new.done()
        finally:
            release.set()
            for task in (old, new):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (old, new, stopping) if task is not None), return_exceptions=True)
            manager.stop_notification_wake_intake()

    asyncio.run(scenario())


def test_repeated_stop_does_not_interrupt_the_run_cleanup() -> None:
    async def scenario():
        manager = _manager()
        cleanup_entered = asyncio.Event()
        release = asyncio.Event()
        second_stop = asyncio.Event()
        cleaned = []
        approvals = 0

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_entered.set()
                await release.wait()
                cleaned.append(True)

        async def cancel_approvals(**kwargs):
            nonlocal approvals
            approvals += 1
            if approvals == 2:
                second_stop.set()

        manager._session.cancel_pending_approvals = cancel_approvals
        task = asyncio.create_task(worker())
        manager.register(conversation_id="owner", task=task, task_id="run", cancel_event=asyncio.Event(), active_conversation_id="owner")
        await asyncio.sleep(0)
        first = asyncio.create_task(manager.cancel(conversation_id="owner"))
        await cleanup_entered.wait()
        second = asyncio.create_task(manager.cancel(conversation_id="owner"))
        try:
            await second_stop.wait()
            assert task.cancelling() == 1
        finally:
            release.set()
            await asyncio.gather(first, second)
            with suppress(asyncio.CancelledError):
                await task
            manager.stop_notification_wake_intake()
        assert cleaned == [True]

    asyncio.run(scenario())


def test_repeated_subagent_stop_preserves_its_cleanup(tmp_path) -> None:
    async def scenario():
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
        cleanup_entered = asyncio.Event()
        release = asyncio.Event()
        cleaned = []

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_entered.set()
                await release.wait()
                cleaned.append(True)

        task = asyncio.create_task(worker())
        runtime.register_subagent_task("child", task, owner_task_id="parent", cancel_event=asyncio.Event())
        await asyncio.sleep(0)
        try:
            assert runtime.cancel_subagent_task("child") == "cancelled"
            await cleanup_entered.wait()
            assert runtime.cancel_subagent_task("child") == "cancelled"
            assert task.cancelling() == 1
        finally:
            release.set()
            with suppress(asyncio.CancelledError):
                await task
            runtime.close()
        assert cleaned == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("shutdown", [False, True])
def test_task_manager_waits_for_already_requested_cleanup(shutdown: bool) -> None:
    async def scenario():
        manager = TaskManager()
        entered = asyncio.Event()
        release = asyncio.Event()
        cleaned = []

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                entered.set()
                await release.wait()
                cleaned.append(True)

        managed = manager.create("test", worker())
        await asyncio.sleep(0)
        manager.cancel(managed.id)
        await entered.wait()
        draining = None
        try:
            if shutdown:
                draining = asyncio.create_task(manager.cancel_all_and_wait())
                await asyncio.sleep(0)
            else:
                manager.cancel(managed.id)
            assert managed.task.cancelling() == 1
        finally:
            release.set()
            if draining is not None:
                await draining
            with suppress(asyncio.CancelledError):
                await managed.task
        assert cleaned == [True]
        assert managed.status == "cancelled"

    asyncio.run(scenario())


def test_task_manager_can_cancel_a_registered_future() -> None:
    async def scenario():
        manager = TaskManager()
        future = asyncio.get_running_loop().create_future()
        managed = manager.create("future", future)
        assert manager.cancel(managed.id)
        await asyncio.sleep(0)
        assert future.cancelled()
        assert managed.status == "cancelled"

    asyncio.run(scenario())


def test_session_and_task_manager_share_one_parent_cancellation(monkeypatch) -> None:
    async def scenario():
        manager = _manager()
        tasks = TaskManager()
        manager._session.task_manager = tasks
        del manager._cancel_run_tree
        cleanup_entered = asyncio.Event()
        children_stopped = asyncio.Event()
        release = asyncio.Event()
        cleaned = []

        async def stop_children(task_id, **kwargs):
            await cleanup_entered.wait()
            children_stopped.set()

        monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: SimpleNamespace(stop_subagent_tasks_for_task=stop_children))

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_entered.set()
                await release.wait()
                cleaned.append(True)

        managed = tasks.create("agent.run", worker())
        manager.register(conversation_id="owner", task=managed.task, task_id=managed.id, cancel_event=asyncio.Event(), active_conversation_id="owner")
        await asyncio.sleep(0)
        stopping = asyncio.create_task(manager.cancel(conversation_id="owner"))
        try:
            await children_stopped.wait()
            assert managed.task.cancelling() == 1
        finally:
            release.set()
            await stopping
            manager.stop_notification_wake_intake()
        assert cleaned == [True]
        assert managed.status == "cancelled"

    asyncio.run(scenario())
