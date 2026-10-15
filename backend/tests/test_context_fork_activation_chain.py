from __future__ import annotations

import asyncio
import copy
import threading

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import UserCommand
from backend.config import AgentSettings, TokenBudget
from backend.tests.test_context_fork_owner_chain import _dispatching_fork_session, _drain, _fork_command
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.workspace.context import WorkspaceContext
from backend.ws.manager import WebSocketManager


async def _connected_fork_session(tmp_path, monkeypatch, history_length=2):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: None)
    from backend.api import _state
    monkeypatch.setattr(_state, "bootstrap", None)
    connection = _connection(tmp_path, session_id="fork-activation")
    session, _ = await WebSocketManager().connect(**connection)
    monkeypatch.setattr(session, "refresh_llm_selection", lambda **_kwargs: None)
    monkeypatch.setattr(session.session_lifecycle, "schedule_runtime_capabilities", lambda **_kwargs: None)
    workspace = tmp_path / "source-workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("Preserved source content", encoding="utf-8")
    context = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
    transcript = []
    for index in range(history_length):
        role = "user" if index % 2 == 0 else "assistant"
        content = f"Recorded {role} {index}"
        transcript.append({"id": f"message-{index}", "role": role, "content": content})
        (context.append_user if role == "user" else context.append_assistant)(content)
    parent = session.conversation_repo.create_conversation(
        title="Actual source", workspace_root=str(workspace), git_branch="existing-source",
        transcript=transcript, context_snapshot=context.export_snapshot(),
        model_selection={"provider": "openai", "model": "fixture-model", "reasoning_effort": "high"},
    )
    session.active_conversation_id = parent.id
    session.load_active_conversation_snapshot(parent.id, parent.context_snapshot)
    await session.conversation_runtime.wait_for_hydration(parent.id)
    session.session_lifecycle.workspace_context = WorkspaceContext(workspace)
    session.session_lifecycle.workspace_root = workspace
    await session.event_outbox.drain_delivery()
    connection["websocket"].sent.clear()
    return session, connection, parent


@pytest.mark.asyncio
@pytest.mark.parametrize("history_length", [2, 30])
async def test_actual_fork_activation_publishes_canonical_owner_before_inventory_and_receipt(tmp_path, monkeypatch, history_length):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch, history_length)
    original = copy.deepcopy(parent.context_snapshot)
    start_hydration = session.start_active_conversation_hydration

    def start_after_projection(conversation_id):
        projection = next(event for event in connection["websocket"].sent
                          if event.get("type") == "conversation.switched" and event.get("conversation_id") == conversation_id)
        assert projection["is_hydrating"] is True
        return start_hydration(conversation_id)

    monkeypatch.setattr(session, "start_active_conversation_hydration", start_after_projection)
    try:
        command = _fork_command(parent, message_id=f"message-{history_length - 1}")
        assert await session.command_dispatcher._dispatch_client_command(command, session.connection_generation) is True
        events = connection["websocket"].sent
        forked = next(event for event in events if event.get("type") == "context_forked")
        branch_id = forked["branch_conversation_id"]
        await session.conversation_runtime.wait_for_hydration(branch_id)
        await session.event_outbox.drain_delivery()
        projection = next(event for event in events if event.get("type") == "conversation.switched")
        result = next(event for event in events if event.get("type") == "command.result" and event.get("command") == "context.fork")
        inventory = next(event for event in events if event.get("type") == "conversation.list")
        assert projection["conversation_id"] == projection["conversation"]["id"] == branch_id
        assert projection["session"]["active_conversation_id"] == session.active_conversation_id == branch_id
        assert projection["conversation"]["transcript"] == parent.transcript
        assert projection["conversation"]["parent_conversation_id"] == parent.id
        assert projection["client_command_id"] == "fork-semantic-command"
        assert projection["client_command_type"] == "context.fork"
        assert projection["is_hydrating"] is (history_length == 30)
        assert projection["seq"] < inventory["seq"] < forked["seq"] < result["seq"]
        assert result["level"] == "success"
        assert result["conversation_id"] == parent.id and result["data"]["branch_activated"] is True
        assert session.conversation_repo.get_conversation(parent.id).context_snapshot == original
        assert len(session.context_builder.export_snapshot()["history"]) == history_length
        assert (tmp_path / "source-workspace" / "source.txt").read_text(encoding="utf-8") == "Preserved source content"
        if history_length == 30:
            assert any(event.get("type") == "conversation.hydration.updated" and event.get("conversation_id") == branch_id
                       and event.get("is_hydrating") is False for event in events)
    finally:
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("held_stage", ["source_hydration", "payload_preparation", "workspace_activation"])
async def test_later_navigation_supersedes_waiting_fork_without_publishing_or_activating_child(tmp_path, monkeypatch, held_stage):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    selected_workspace = tmp_path / "selected-workspace"
    selected_workspace.mkdir()
    selected = session.conversation_repo.create_conversation(title="Later selection", workspace_root=str(selected_workspace))
    entered, release = asyncio.Event(), asyncio.Event()
    thread_entered, thread_release = threading.Event(), threading.Event()
    if held_stage == "source_hydration":
        hydrate = session.conversation_runtime.wait_for_hydration

        async def held_hydration(conversation_id):
            if conversation_id == parent.id:
                entered.set()
                await release.wait()
            return await hydrate(conversation_id)
        monkeypatch.setattr(session.conversation_runtime, "wait_for_hydration", held_hydration)
    elif held_stage == "payload_preparation":
        from backend.services import conversation_payload_service
        build_payload = conversation_payload_service.build_conversation_switched_payload

        def held_payload(conversation, **kwargs):
            if conversation.branch_kind == "context_fork":
                thread_entered.set()
                assert thread_release.wait(5)
            return build_payload(conversation, **kwargs)
        monkeypatch.setattr(conversation_payload_service, "build_conversation_switched_payload", held_payload)
    else:
        switch_workspace = session.switch_workspace_for_conversation

        async def held_workspace(conversation, **kwargs):
            if conversation.branch_kind == "context_fork":
                entered.set()
                await release.wait()
            return await switch_workspace(conversation, **kwargs)
        monkeypatch.setattr(session, "switch_workspace_for_conversation", held_workspace)
    first = asyncio.create_task(session.command_dispatcher._dispatch_client_command(
        _fork_command(parent, message_id="message-1"), session.connection_generation,
    ))
    second = None
    try:
        if held_stage == "payload_preparation":
            assert await asyncio.to_thread(thread_entered.wait, 5)
        else:
            await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch", {
            "conversation_id": selected.id, "client_command_id": "later-user-navigation",
        }), session.connection_generation))
        await asyncio.sleep(0)
        release.set()
        thread_release.set()
        assert all(await asyncio.gather(first, second))
        await session.conversation_runtime.wait_for_hydration(selected.id)
        await session.event_outbox.drain_delivery()
        events = connection["websocket"].sent
        forked = next(event for event in events if event.get("type") == "context_forked")
        result = next(event for event in events if event.get("type") == "command.result" and event.get("command") == "context.fork")
        pages = [event for event in events if event.get("type") == "conversation.switched"]
        assert pages and all(event["client_command_id"] == "later-user-navigation" for event in pages)
        assert pages[-1]["conversation_id"] == selected.id
        assert session.active_conversation_id == selected.id
        assert session.session_lifecycle.workspace_root == selected_workspace
        assert forked["branch_created"] is True and forked["branch_activated"] is False
        assert forked["superseded"] is True
        assert result["level"] == "success" and result["data"]["superseded"] is True
        assert result["conversation_id"] == parent.id
        branch = session.conversation_repo.get_conversation(forked["branch_conversation_id"])
        assert branch.parent_conversation_id == parent.id and branch.context_snapshot["history"]
        assert (tmp_path / "source-workspace" / "source.txt").read_text(encoding="utf-8") == "Preserved source content"
    finally:
        release.set()
        thread_release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "rejected"])
async def test_fork_switched_projection_failure_returns_error_and_restores_source(tmp_path, failure):
    session, dispatcher, socket, parent = _dispatching_fork_session(tmp_path)
    original = session.context_builder.export_snapshot()
    send = session.send_payload

    async def failed_projection(payload, **kwargs):
        if payload["type"] == "conversation.switched":
            if failure == "exception":
                raise OSError("Actual switched projection publication failed")
            return False
        return await send(payload, **kwargs)

    session.send_payload = failed_projection
    assert await dispatcher._dispatch_client_command(_fork_command(parent), 1) is True
    assert not any(event["type"] == "conversation.switched" for event in socket.messages)
    forked = next(event for event in socket.messages if event["type"] == "context_forked")
    result = next(event for event in socket.messages if event["type"] == "command.result")
    assert forked["branch_created"] is True and forked["branch_activated"] is False
    assert result["level"] == "error" and result["data"]["reason"] == "activation_failed"
    assert result["data"]["branch_conversation_id"] == forked["branch_conversation_id"]
    assert session.active_conversation_id == parent.id
    restored = session.context_builder.export_snapshot()
    assert restored["guardian_evidence"]["review_context_revision"] != original["guardian_evidence"]["review_context_revision"]
    restored["guardian_evidence"]["review_context_revision"] = original["guardian_evidence"]["review_context_revision"]
    assert restored == original
    assert session.switched == [forked["branch_conversation_id"], parent.id]
    await _drain(session)


def test_only_activating_materialized_fork_admits_foreground_navigation(tmp_path):
    session, _, _, parent = _dispatching_fork_session(tmp_path)
    lifecycle = session.session_lifecycle
    older = lifecycle.admit_foreground_navigation(UserCommand("conversation.switch", {
        "conversation_id": parent.id, "client_command_id": "older-navigation",
    }))
    for overrides in ({"activate": False}, {"activate": True, "create_branch": False}):
        assert lifecycle.admit_foreground_navigation(_fork_command(parent, **overrides)) is None
        with lifecycle.bind_foreground_navigation(older):
            assert lifecycle.foreground_navigation_is_current()
    fork = lifecycle.admit_foreground_navigation(_fork_command(parent))
    assert fork.client_command_id == "fork-semantic-command" and fork.target_conversation_id is None
    with lifecycle.bind_foreground_navigation(older):
        assert lifecycle.foreground_navigation_is_current() is False
    with lifecycle.bind_foreground_navigation(fork):
        assert lifecycle.foreground_navigation_is_current()
