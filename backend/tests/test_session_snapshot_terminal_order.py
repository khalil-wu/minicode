"""The real owner snapshot must remain ordered with its terminal publication."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import threading
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import AgentEvent, UserCommand
from backend.tests.test_conversation_window_follower import text, windows
from backend.ws.handlers.conversation import handle_conversation_switch
from backend.ws.handlers.session import handle_session_restore, handle_session_sync


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["sync", "restore", "list", "switch"])
async def test_own_snapshot_cannot_replace_a_terminal_answer_with_a_pre_terminal_page(windows, monkeypatch, operation):
    manager, owner, _, conversation, _, start, drain = windows
    start()
    original_message = {"id": "actual-assistant", "role": "assistant", "content": "partial", "terminal_status": "running"}
    owner.conversation_repo.upsert_transcript_message(conversation.id, original_message)
    await text(owner, conversation, "partial")
    await drain()
    owner.ws.sent.clear()
    hydration_publication = False
    if operation in {"restore", "switch"}:
        # Workspace preparation/model selection is independent of the actual
        # repository hydration callback and WebSocket ordering exercised here.
        monkeypatch.setattr(owner, "switch_workspace_for_conversation", AsyncMock(return_value=True))
        monkeypatch.setattr(owner, "refresh_llm_selection", lambda: None)
        monkeypatch.setattr(owner.session_lifecycle, "schedule_runtime_capabilities", lambda **_kwargs: None)
        reconcile = owner.reconcile_persisted_ui_agent_state

        async def hydration_ready(conversation_id):
            nonlocal hydration_publication
            record = await reconcile(conversation_id)
            hydration_publication = True
            return record

        monkeypatch.setattr(owner, "reconcile_persisted_ui_agent_state", hydration_ready)

    loop = asyncio.get_running_loop()
    read_started, release_read = asyncio.Event(), threading.Event()
    read_page = owner.conversation_repo.get_conversation_view
    held = False

    def hold_stale_page(conversation_id, *args, **kwargs):
        nonlocal held
        page = read_page(conversation_id, *args, **kwargs)
        if conversation_id == conversation.id and not held and (operation not in {"restore", "switch"} or hydration_publication):
            held = True
            loop.call_soon_threadsafe(read_started.set)
            assert release_read.wait(5), "snapshot read was never released"
        return page

    monkeypatch.setattr(owner.conversation_repo, "get_conversation_view", hold_stale_page)
    data = {"last_seq": 0}
    if operation == "restore":
        data["last_conversation_id"] = conversation.id
    if operation == "switch":
        data["conversation_id"] = conversation.id
    handlers = {"sync": handle_session_sync, "restore": handle_session_restore, "switch": handle_conversation_switch}
    dispatch = asyncio.create_task(owner.send_conversation_list() if operation == "list" else handlers[operation](owner, data))
    publishing = None
    try:
        await asyncio.wait_for(read_started.wait(), 5)
        snapshot_owns_boundary = manager.conversation_stream_lock(conversation.id).locked()

        async def complete_turn():
            await asyncio.to_thread(owner.conversation_repo.upsert_transcript_message, conversation.id,
                {**original_message, "content": "partial completed", "terminal_status": "completed"})
            await text(owner, conversation, " completed")
            await owner.send_event(AgentEvent(type="done", data={"conversation_id": conversation.id,
                "message_id": "actual-assistant", "turn_id": "actual-turn", "status": "completed"}))
            owner.run_manager.mark_delivery_complete(conversation.id, "actual-task")

        publishing = asyncio.create_task(complete_turn())
        if not snapshot_owns_boundary:
            # Reproduce completion during the unlocked page read. With the
            # existing stream boundary held, publication instead waits for it.
            await asyncio.wait_for(publishing, 5)
        release_read.set()
        await asyncio.wait_for(asyncio.gather(dispatch, publishing), 5)
        if operation in {"restore", "switch"}:
            await asyncio.wait_for(owner.conversation_runtime.wait_for_hydration(conversation.id), 5)
        await drain()
        visible = "partial"
        snapshots = 0
        for payload in deepcopy(owner.ws.sent):
            if payload["type"] in {"session.synced", "session.restored", "conversation.switched", "conversation.list"}:
                page = payload.get("active_conversation") or payload.get("conversation")
                if page and page["id"] == conversation.id:
                    saved = next((message for message in page["transcript"] if message["id"] == "actual-assistant"), None)
                    if saved is not None:
                        visible = saved["content"]
                        snapshots += 1
            elif payload["type"] == "agent_message.delta" and payload.get("message_id") == "actual-assistant":
                visible += payload["delta"]
            elif payload["type"] == "stream_resume" and payload.get("message_id") == "actual-assistant":
                visible = "".join(block["content"] for block in payload["content_blocks"] if block["type"] == "text")
        assert snapshots > 0
        assert owner.conversation_repo.get_conversation(conversation.id).transcript[-1]["content"] == "partial completed"
        assert visible == "partial completed", owner.ws.sent
    finally:
        release_read.set()
        await asyncio.gather(dispatch, *(task for task in [publishing] if task is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["sync", "list"])
async def test_snapshot_and_frozen_replay_release_stream_ownership_before_network_delivery(windows, monkeypatch, operation):
    manager, owner, _, conversation, _, start, drain = windows
    start()
    for number in range(2):
        await owner.send_event(AgentEvent(type="agent.item", data={"conversation_id": conversation.id,
            "item_id": f"process-{number}", "kind": "process_text", "content": str(number), "status": "completed"}))
        if number == 0:
            cursor = owner.event_outbox.current_replay_seq
    await drain()
    entered, release = asyncio.Event(), asyncio.Event()
    send = owner.ws.send_json

    snapshot_type = "session.synced" if operation == "sync" else "conversation.list"

    async def held_network(payload):
        if payload["type"] == snapshot_type:
            entered.set()
            await release.wait()
        await send(payload)

    monkeypatch.setattr(owner.ws, "send_json", held_network)
    syncing = asyncio.create_task(handle_session_sync(owner, {"last_seq": cursor}) if operation == "sync"
        else owner.send_conversation_list())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(asyncio.shield(syncing), 5)
        assert not manager.conversation_stream_lock(conversation.id).locked()
        await asyncio.wait_for(text(owner, conversation, "progress while delivery is held"), 5)
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.gather(syncing, return_exceptions=True)
        await drain()
    types = [payload["type"] for payload in owner.ws.sent]
    if operation == "sync":
        assert types.index(snapshot_type) < types.index("session.replay") < types.index("agent_message.delta")
    else:
        assert types.index(snapshot_type) < types.index("agent_message.delta")


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_read", ["page", "sidebar"])
@pytest.mark.parametrize("stale_archived", [False, True])
async def test_conversation_list_keeps_the_current_owner_across_repository_awaits(windows, monkeypatch, blocked_read, stale_archived):
    _, owner, _, conversation, other, _, drain = windows
    owner.conversation_repo.append_transcript_message(conversation.id, {"role": "user", "content": "owner A history"})
    owner.conversation_repo.append_transcript_message(other.id, {"role": "user", "content": "owner B history"})
    if stale_archived:
        owner.conversation_repo.set_archived(conversation.id, True)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    read_page = owner.conversation_repo.get_conversation_view
    read_sidebar = owner.conversation_repo.get_sidebar_order
    pages = []

    def page(owner_id, *args, **kwargs):
        result = read_page(owner_id, *args, **kwargs)
        pages.append(owner_id)
        if blocked_read == "page" and len(pages) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "page read was never released"
        return result

    def sidebar(*args, **kwargs):
        result = read_sidebar(*args, **kwargs)
        if blocked_read == "sidebar":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "sidebar read was never released"
        return result

    monkeypatch.setattr(owner.conversation_repo, "get_conversation_view", page)
    monkeypatch.setattr(owner.conversation_repo, "get_sidebar_order", sidebar)
    listing = asyncio.create_task(owner.send_conversation_list())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        owner.active_conversation_id = other.id
    finally:
        release.set()
        await asyncio.wait_for(listing, 5)
        await drain()
    assert owner.active_conversation_id == other.id
    payloads = [payload for payload in owner.ws.sent if payload["type"] == "conversation.list"]
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["active_conversation_id"] == payload["conversation_id"] == other.id
    assert payload["session"]["active_conversation_id"] == payload["active_conversation"]["id"] == other.id
    assert payload["active_conversation"]["transcript"][-1]["content"] == "owner B history"
    assert pages == ([conversation.id, other.id] if blocked_read == "page" else [other.id])


@pytest.mark.asyncio
async def test_superseded_conversation_list_does_not_clear_the_new_navigation(windows, monkeypatch):
    _, owner, _, conversation, other, _, drain = windows
    lifecycle = owner.session_lifecycle
    old_navigation = lifecycle.admit_foreground_navigation(UserCommand(type="conversation.switch", data={"conversation_id": conversation.id}))
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    read = owner.conversation_repo.get_conversation_view

    def page(owner_id):
        result = read(owner_id)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "navigation page was never released"
        return result

    async def list_old_navigation():
        with lifecycle.bind_foreground_navigation(old_navigation):
            return await owner.send_conversation_list()

    monkeypatch.setattr(owner.conversation_repo, "get_conversation_view", page)
    listing = asyncio.create_task(list_old_navigation())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        lifecycle.admit_foreground_navigation(UserCommand(type="conversation.switch", data={"conversation_id": other.id}))
        owner.active_conversation_id = other.id
    finally:
        release.set()
        published = await asyncio.wait_for(listing, 5)
        await drain()
    assert published is False
    assert owner.active_conversation_id == other.id
    assert not any(payload["type"] == "conversation.list" for payload in owner.ws.sent)
