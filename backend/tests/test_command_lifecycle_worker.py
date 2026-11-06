from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from backend.tests.test_durable_command_completion import clone_dispatcher
from backend.agent.message import UserCommand


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim_client_command", "complete_client_command", "release_client_command"])
async def test_disk_command_lifecycle_does_not_run_on_event_loop(clone_dispatcher, monkeypatch, stage):
    case = clone_dispatcher
    loop_thread = threading.get_ident()
    method = getattr(case.queue, stage)
    calls = []
    def observe(*args, **kwargs):
        calls.append(threading.get_ident())
        assert threading.get_ident() != loop_thread, "Disk work blocked the websocket event loop"
        return method(*args, **kwargs)
    monkeypatch.setattr(case.queue, stage, observe)
    case.dispatcher._dispatch_client_command = AsyncMock(return_value=stage != "release_client_command")
    await case.dispatcher._run_durable_client_command("clone-one", 1)
    assert calls


@pytest.mark.asyncio
async def test_other_events_progress_while_claim_is_waiting_and_cancel_releases_it(clone_dispatcher, monkeypatch):
    case = clone_dispatcher
    entered, release = threading.Event(), threading.Event()
    method = case.queue.claim_client_command
    def held(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        return method(*args)
    loop_thread = threading.get_ident()
    monkeypatch.setattr(case.queue, "claim_client_command", held)
    case.dispatcher._dispatch_client_command = AsyncMock(return_value=True)
    task = asyncio.create_task(case.dispatcher._run_durable_client_command("clone-one", 1))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        tick = []
        asyncio.get_running_loop().call_soon(tick.append, "stream tick")
        await asyncio.sleep(0)
        assert tick == ["stream tick"]
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert case.queue.has_client_command("clone-one")
        assert not case.queue.is_client_command_completed("clone-one")
        case.dispatcher._dispatch_client_command.assert_not_awaited()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_key", ["conversation_id", "owner_conversation_id"])
async def test_slow_claim_cannot_reorder_the_latest_navigation_intent(clone_dispatcher, monkeypatch, owner_key):
    case = clone_dispatcher
    old_target = case.repo.create_conversation(title="old")
    new_target = case.repo.create_conversation(title="new")
    old = UserCommand("conversation.switch", {"client_command_id": "old-selection", owner_key: old_target.id})
    new = UserCommand("conversation.switch", {"client_command_id": "new-selection", owner_key: new_target.id})
    for command in (old, new):
        assert case.queue.persist_client_command(command)
    entered, release = threading.Event(), threading.Event()
    claim = case.queue.claim_client_command
    def held(identity):
        if identity == "old-selection":
            entered.set()
            assert release.wait(5)
        return claim(identity)
    monkeypatch.setattr(case.queue, "claim_client_command", held)
    accepted = []
    lifecycle = case.session.session_lifecycle
    async def dispatch(command, generation):
        from backend.ws.command_dispatcher import _normalize_command_owner
        _normalize_command_owner(command)
        receipt = lifecycle.admit_foreground_navigation(command)
        with lifecycle.bind_foreground_navigation(receipt):
            if lifecycle.foreground_navigation_is_current():
                case.session.active_conversation_id = command.data["conversation_id"]
                accepted.append(command.data["client_command_id"])
        return True
    case.dispatcher._dispatch_client_command = dispatch
    try:
        case.dispatcher._schedule_durable_client_command("old-selection", 1, command_type=old.type, command=old)
        assert await asyncio.to_thread(entered.wait, 2)
        case.dispatcher._schedule_durable_client_command("new-selection", 1, command_type=new.type, command=new)
        for _ in range(200):
            if "new-selection" in accepted:
                break
            await asyncio.sleep(.01)
        assert accepted == ["new-selection"]
        release.set()
        await asyncio.gather(*tuple(case.dispatcher._command_tasks))
        assert accepted == ["new-selection"]
        assert case.session.active_conversation_id == new_target.id
    finally:
        release.set()
        await asyncio.gather(*tuple(case.dispatcher._command_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_navigation_before_claim_retires_its_pending_selection(clone_dispatcher):
    case = clone_dispatcher
    target = case.repo.create_conversation(title="cancelled selection")
    command = UserCommand("conversation.switch", {"client_command_id": "cancelled-selection", "conversation_id": target.id})
    assert case.queue.persist_client_command(command)
    case.dispatcher._schedule_durable_client_command("cancelled-selection", 1, command_type=command.type, command=command)
    tasks = tuple(case.dispatcher.command_tasks)
    assert case.session.session_lifecycle._foreground_navigation_pending
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not case.session.session_lifecycle._foreground_navigation_pending
    assert case.session.active_conversation_id == case.source.id
    assert case.queue.has_client_command("cancelled-selection")


@pytest.mark.asyncio
async def test_cancellation_during_completion_settles_the_executed_command(clone_dispatcher, monkeypatch):
    case = clone_dispatcher
    entered, release = threading.Event(), threading.Event()
    complete = case.queue.complete_client_command
    def held(identity):
        entered.set()
        assert release.wait(5)
        return complete(identity)
    monkeypatch.setattr(case.queue, "complete_client_command", held)
    task = asyncio.create_task(case.dispatcher._run_durable_client_command("clone-one", 1))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await task
        assert case.queue.is_client_command_completed("clone-one")
        assert "clone-one" not in case.dispatcher._pending_client_completions
        await case.dispatcher._run_durable_client_command("clone-one", 2)
        assert case.calls == ["clone-one"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recent_id_compaction_cannot_overwrite_a_later_completion(clone_dispatcher, monkeypatch):
    case = clone_dispatcher
    monkeypatch.setattr("backend.ws.command_dispatcher.RECENT_CLIENT_COMMAND_IDS_MAX", 1)
    await case.dispatcher._mark_client_command_seen(UserCommand("ping", {"client_command_id": "initial"}))
    entered, release = threading.Event(), threading.Event()
    rewrite = case.dispatcher.client_command_store.rewrite_ids
    append = case.dispatcher.client_command_store.append
    appended = []
    def held(ids):
        if ids == ["first"]:
            entered.set()
            assert release.wait(5)
        rewrite(ids)
    def observe(identity, **kwargs):
        appended.append(identity)
        append(identity, **kwargs)
    monkeypatch.setattr(case.dispatcher.client_command_store, "rewrite_ids", held)
    monkeypatch.setattr(case.dispatcher.client_command_store, "append", observe)
    first = asyncio.create_task(case.dispatcher._mark_client_command_seen(UserCommand("ping", {"client_command_id": "first"})))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(case.dispatcher._mark_client_command_seen(UserCommand("ping", {"client_command_id": "second"})))
        await asyncio.sleep(0.05)
        assert appended == ["first"]
        release.set()
        await asyncio.gather(first, second)
        assert case.dispatcher.client_command_store.load_ids(limit=10) == ["second"]
        assert case.dispatcher.recent_client_command_ids == ["second"]
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user_message", "conversation.delete"])
async def test_scheduled_navigation_keeps_its_receipt_after_the_selected_owner_changes(clone_dispatcher, monkeypatch, kind):
    case = clone_dispatcher
    lifecycle = case.session.session_lifecycle
    case.session.active_conversation_id = None if kind == "user_message" else case.source.id
    data = {"client_command_id": "held-navigation", "conversation_id": case.source.id}
    if kind == "user_message":
        data["_implicit_new_conversation_id"] = case.source.id
    command = UserCommand(kind, data)
    assert case.queue.persist_client_command(command)
    entered, release = threading.Event(), threading.Event()
    claim = case.queue.claim_client_command
    def held(identity):
        entered.set()
        assert release.wait(5)
        return claim(identity)
    monkeypatch.setattr(case.queue, "claim_client_command", held)
    observed = []
    async def dispatch(value, generation):
        receipt = lifecycle.admit_foreground_navigation(value)
        with lifecycle.bind_foreground_navigation(receipt):
            observed.append(lifecycle.foreground_navigation_is_current())
        return True
    case.dispatcher._dispatch_client_command = dispatch
    case.dispatcher._schedule_durable_client_command("held-navigation", 1, command_type=kind, command=command)
    tasks = tuple(case.dispatcher.command_tasks | case.dispatcher.execution_command_tasks)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        latest = lifecycle.admit_foreground_navigation(UserCommand("conversation.switch", {
            "conversation_id": "new-owner", "client_command_id": "latest-selection"}))
        case.session.active_conversation_id = "new-owner"
        release.set()
        await asyncio.gather(*tasks)
        assert observed == [False]
        assert lifecycle._foreground_navigation_receipt is latest
        assert lifecycle._foreground_navigation_pending
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
