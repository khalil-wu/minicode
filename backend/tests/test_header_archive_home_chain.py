from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import UserCommand
from backend.services.conversation_payload_service import parse_conversation_archive_request
from backend.tests.test_context_fork_activation_chain import _connected_fork_session
from backend.tests.test_ws_cold_connection import _release_session
from backend.ws.payload_contracts import validate_session_projection_payload


def _archive(parent, **overrides):
    return UserCommand("conversation.archive", {
        "conversation_id": parent.id, "client_command_id": "header-archive-home",
        "archive_navigation": "home", **overrides,
    })


def _isolate_archive_work(monkeypatch):
    from backend.tasks import scheduler
    from backend.ws.handlers import conversation
    pause = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler, "get_global_scheduler", lambda: type("Scheduler", (), {"pause_for_conversation": pause})())
    monkeypatch.setattr(conversation, "_schedule_long_term_memory_forgetting", lambda *_args: None)
    return pause


@pytest.mark.asyncio
async def test_actual_source_header_archive_publishes_blank_owner_instead_of_selecting_another_chat(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    _isolate_archive_work(monkeypatch)
    other = session.conversation_repo.create_conversation(title="Existing other chat", workspace_root=parent.workspace_root)
    original = copy.deepcopy(parent.context_snapshot)
    session.attachment_store.save(artifact_id="archived-upload", content="Original uploaded content",
        metadata={"conversation_id": parent.id, "workspace_root": parent.workspace_root})
    artifact = session.artifact_store.save("Original tool result", "read_file", conversation_id=parent.id, workspace_root=parent.workspace_root)
    try:
        assert await session.command_dispatcher._dispatch_client_command(_archive(parent), session.connection_generation) is True
        events = connection["websocket"].sent
        projections = [event for event in events if event.get("type") == "conversation.switched"]
        assert len(projections) == 1
        blank = projections[0]
        assert blank["conversation_id"] is None and blank["conversation"] is None
        assert blank["session"]["active_conversation_id"] is None
        assert blank["archive_navigation"] == "home" and blank["client_command_id"] == "header-archive-home"
        assert session.active_conversation_id is None and session.session_lifecycle.workspace_root is None
        assert session.context_builder.history_length == 0
        result = next(event for event in events if event.get("type") == "command.result" and event.get("command") == "conversation.archive")
        inventory = next(event for event in events if event.get("type") == "conversation.list")
        assert inventory["archive_navigation"] == "home" and inventory["client_command_id"] == "header-archive-home"
        assert inventory["active_conversation_id"] is None
        assert blank["seq"] < inventory["seq"] < result["seq"]
        assert result["level"] == "success" and result["conversation_id"] == parent.id
        assert result["data"]["archived"] is True and result["data"]["home_activated"] is True
        assert result["data"]["superseded"] is False
        archived = session.conversation_repo.get_conversation(parent.id)
        assert archived.archived is True and archived.context_snapshot == original and archived.transcript == parent.transcript
        assert session.conversation_repo.get_conversation(other.id).archived is False
        assert session.attachment_store.get("archived-upload", conversation_id=parent.id, workspace_root=parent.workspace_root) == "Original uploaded content"
        assert session.artifact_store.get(artifact, conversation_id=parent.id, workspace_root=parent.workspace_root) == "Original tool result"
        assert (tmp_path / "source-workspace" / "source.txt").read_text(encoding="utf-8") == "Preserved source content"
    finally:
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("later_kind", ["conversation.switch", "conversation.create"])
async def test_later_navigation_wins_while_original_header_archive_finishes(tmp_path, monkeypatch, later_kind):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    pause = _isolate_archive_work(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_pause(conversation_id):
        assert conversation_id == parent.id
        entered.set()
        await release.wait()
        return True

    pause.side_effect = held_pause
    selected = session.conversation_repo.create_conversation(title="Later existing selection", workspace_root=parent.workspace_root)
    first = asyncio.create_task(session.command_dispatcher._dispatch_client_command(_archive(parent), session.connection_generation))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        later_data = {"client_command_id": "later-header-navigation", "conversation_id": selected.id}
        if later_kind == "conversation.create":
            later_data.update(conversation_id="conv_later_header", workspace_root=parent.workspace_root, activate=True, title="Later created task")
        second = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand(later_kind, later_data), session.connection_generation))
        await asyncio.sleep(0)
        release.set()
        assert all(await asyncio.gather(first, second))
        await session.conversation_runtime.wait_for_hydration(later_data["conversation_id"])
        await session.event_outbox.drain_delivery()
        events = connection["websocket"].sent
        pages = [event for event in events if event.get("type") == "conversation.switched"]
        assert pages and all(event.get("client_command_id") == "later-header-navigation" for event in pages)
        assert pages[-1]["conversation_id"] == session.active_conversation_id == later_data["conversation_id"]
        result = next(event for event in events if event.get("type") == "command.result" and event.get("client_command_id") == "header-archive-home")
        assert result["level"] == "success" and result["data"]["archived"] is True
        assert result["data"]["superseded"] is True and result["data"]["home_activated"] is False
        assert session.conversation_repo.get_conversation(parent.id).archived is True
        assert not any(event.get("archive_navigation") == "home" for event in pages)
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
        await _release_session(session)


@pytest.mark.asyncio
async def test_sidebar_archive_without_header_flag_keeps_existing_fallback_behavior(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    _isolate_archive_work(monkeypatch)
    fallback = session.conversation_repo.create_conversation(title="Existing fallback", workspace_root=parent.workspace_root)
    try:
        command = _archive(parent)
        command.data.pop("archive_navigation")
        assert await session.command_dispatcher._dispatch_client_command(command, session.connection_generation) is True
        await session.conversation_runtime.wait_for_hydration(fallback.id)
        await session.event_outbox.drain_delivery()
        assert session.active_conversation_id == fallback.id
        assert session.conversation_repo.get_conversation(parent.id).archived is True
        assert all("archive_navigation" not in event for event in connection["websocket"].sent)
    finally:
        await _release_session(session)


@pytest.mark.asyncio
async def test_header_archive_cleanup_refusal_does_not_publish_home_or_claim_success(tmp_path, monkeypatch):
    from backend.ws.handlers import conversation
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    _isolate_archive_work(monkeypatch)
    monkeypatch.setattr(conversation, "_stop_conversation_run", AsyncMock(return_value=False))
    try:
        assert await session.command_dispatcher._dispatch_client_command(_archive(parent), session.connection_generation) is True
        events = connection["websocket"].sent
        result = next(event for event in events if event.get("type") == "command.result")
        assert result["level"] == "error" and result["data"]["reason"] == "run_still_active"
        assert session.conversation_repo.get_conversation(parent.id).archived is False
        assert session.active_conversation_id == parent.id
        assert not any(event.get("type") == "conversation.switched" for event in events)
    finally:
        await _release_session(session)


def test_explicit_canonical_blank_is_valid_but_missing_switch_owner_stays_invalid():
    validate_session_projection_payload({"type": "conversation.switched", "conversation_id": None, "conversation": None, "is_hydrating": False})
    with pytest.raises(ValueError, match="requires an owner"):
        validate_session_projection_payload({"type": "conversation.switched"})


def test_only_header_archive_home_admits_foreground_navigation_receipt(tmp_path):
    from types import SimpleNamespace
    from backend.ws.session_lifecycle import SessionLifecycle
    lifecycle = SessionLifecycle(SimpleNamespace(active_conversation_id="original"))
    previous = lifecycle.admit_foreground_navigation(UserCommand("conversation.switch", {"conversation_id": "target", "client_command_id": "previous"}))
    assert lifecycle.admit_foreground_navigation(UserCommand("conversation.archive", {"conversation_id": "original"})) is None
    with lifecycle.bind_foreground_navigation(previous):
        assert lifecycle.foreground_navigation_is_current()
    header = lifecycle.admit_foreground_navigation(UserCommand("conversation.archive", {"conversation_id": "original", "archive_navigation": "home", "client_command_id": "header"}))
    assert header.target_conversation_id is None
    with lifecycle.bind_foreground_navigation(previous):
        assert lifecycle.foreground_navigation_is_current() is False
    assert parse_conversation_archive_request({"conversation_id": " original ", "archive_navigation": "home"}).conversation_id == "original"
    with pytest.raises(ValueError, match="archive_navigation"):
        parse_conversation_archive_request({"archive_navigation": "unrelated"})
