from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.async_cleanup import cancel_and_drain_receipt, retain_cleanup_task
from backend.tests.test_agent_runner_done_fallback import _Session


def test_live_callback_keeps_process_claim_across_websocket_hosts(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = _Session(tmp_path / "first", [])
        second_events: list[dict] = []
        second = _Session(tmp_path / "second", second_events)
        released = asyncio.Event()
        callback_started = asyncio.Event()
        late_file = tmp_path / "late-effect.txt"
        second_admitted: list[bool] = []
        queue_wakes: list[tuple[str, bool]] = []
        first.schedule_next_queued_user_message = lambda cid: queue_wakes.append((cid, late_file.exists()))

        async def callback() -> None:
            callback_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await released.wait()
                late_file.write_text("settled", encoding="utf-8")

        async def first_run(_message, *, run_context, metadata, **_kwargs) -> None:
            metadata["run_id"] = "owned-first"
            pending = asyncio.create_task(callback())
            await callback_started.wait()
            receipt = await cancel_and_drain_receipt(
                [pending], timeout=0.01, label="audit callback", owner=run_context.lifecycle_cleanup_tasks
            )
            assert receipt.pending == 1

        async def second_run(_message, *, metadata, **_kwargs) -> None:
            metadata["run_id"] = "owned-second"
            second_admitted.append(True)

        first._run_agent_locked = first_run
        second._run_agent_locked = second_run
        guards = conversation_query_guards()
        conversation_id = "conv_runnerdone"
        try:
            await first._run_agent("first", metadata={"_run_task_id": "old"})
            assert guards.active_claim(conversation_id) is not None
            assert first.run_manager.has_active_run()
            assert first.run_manager.has_pending_lifecycle_cleanup(conversation_id)
            assert queue_wakes == []
            assert not late_file.exists()
            # Even cancellation before the fence starts cannot release the
            # claim. This is cancellation of the owner waiter, not the child.
            for fence in tuple(first.run_manager._lifecycle_release_tasks):
                fence.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await second._run_agent("second", metadata={"_run_task_id": "new"})
            assert second_admitted == []
            assert second_events[-1]["reason"] == "conversation_busy"
            released.set()
            await asyncio.gather(*first.run_manager.lifecycle_cleanup_tasks_for(conversation_id))
            await asyncio.gather(*first.run_manager._lifecycle_release_tasks)
            await asyncio.sleep(0)
            assert guards.active_claim(conversation_id) is None
            assert not first.run_manager.has_pending_lifecycle_cleanup(conversation_id)
            assert queue_wakes == [(conversation_id, True)]
            assert late_file.read_text(encoding="utf-8") == "settled"
            await second._run_agent("second", metadata={"_run_task_id": "new"})
            assert second_admitted == [True]
        finally:
            released.set()
            await asyncio.gather(*first.run_manager.lifecycle_cleanup_tasks_for(conversation_id))
            claim = guards.active_claim(conversation_id)
            if claim is not None:
                guards.end(claim)
            first.run_manager.close_durable_queue()
            second.run_manager.close_durable_queue()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_generation_retirement_waits_for_actual_callback(tmp_path: Path, cancel_waiter: bool) -> None:
    async def scenario() -> None:
        host = _Session(tmp_path, [])
        released = asyncio.Event()
        shut_down = asyncio.Event()
        effects: list[str] = []
        pending = asyncio.create_task(released.wait())
        owner = host.run_manager.lifecycle_cleanup_tasks_for("conv_runnerdone")
        retain_cleanup_task(pending, owner)

        async def shutdown(reason: str) -> None:
            assert pending.done()
            effects.append(reason)
            shut_down.set()

        runtime = SimpleNamespace(shutdown=shutdown)
        await host._retire_lifecycle_runtime(
            "conv_runnerdone", runtime, None, None,
            reason="reload", clear_loader_cache=False, defer_until=None,
        )
        state = host._extension_runtime_state("conv_runnerdone")
        if cancel_waiter:
            state["retired_generations"][0]["defer_until"].cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        await host._drain_retired_lifecycle_runtimes("conv_runnerdone", force=True)
        assert effects == []
        assert state["retired_generations"]
        released.set()
        await asyncio.wait_for(shut_down.wait(), 1)
        assert effects == ["reload"]
        host.run_manager.close_durable_queue()

    asyncio.run(scenario())


def test_cancelled_shutdown_waiter_does_not_dispose_borrowed_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = _Session(tmp_path, [])
        released = asyncio.Event()
        shut_down = asyncio.Event()
        effects: list[str] = []
        pending = asyncio.create_task(released.wait())
        retain_cleanup_task(pending, host.run_manager.lifecycle_cleanup_tasks_for("conv_runnerdone"))

        async def shutdown(reason: str) -> None:
            assert pending.done()
            effects.append(reason)
            shut_down.set()

        state = host._extension_runtime_state("conv_runnerdone")
        state["runtime"] = SimpleNamespace(shutdown=shutdown)
        teardown = asyncio.create_task(host._shutdown_lifecycle_runtimes("disconnect"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        teardown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await teardown
        assert state["runtime"] is not None
        assert effects == []
        assert host.cleanup_tasks
        released.set()
        await asyncio.wait_for(shut_down.wait(), 1)
        await asyncio.gather(*host.cleanup_tasks)
        assert effects == ["disconnect"]
        assert host._extension_runtime_states == {}
        host.run_manager.close_durable_queue()

    asyncio.run(scenario())
