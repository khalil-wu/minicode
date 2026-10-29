from __future__ import annotations

import os
import asyncio
from contextlib import suppress
from pathlib import Path

import pytest

from backend.agent.message import AgentEvent, UserCommand
from backend.agent.query_engine import QueryEngine
from backend.agent.runtime import default_runtime
from backend.config import AppConfig, LLMSettings
from backend.conversations.models import ConversationRecord
from backend.conversations.public_projection import project_public_conversation, project_public_conversation_summary
from backend.conversations.repository import ConversationRepository
from backend.services.conversation_payload_service import build_conversation_switched_payload, parse_conversation_create_request
from backend.tests.test_agent_runner_done_fallback import _Session, _NoopLLM, _admit_runner_turn, _run_session_scenario
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.ws.manager import WebSocketManager
from backend.ws.run_manager import SessionRunManager
from backend.ws.stream_state import create_stream_state


def test_mode_survives_repository_reopen_inventory_and_public_switch_without_changing_permissions(tmp_path):
    storage = tmp_path / "conversations"
    first = ConversationRepository(storage)
    plan = first.create_conversation(title="Plan task", agent_mode="plan", permission_mode="bypass")
    normal = first.create_conversation(title="Ordinary task")
    assert normal.agent_mode == "build"
    updated = first.update_agent_mode(plan.id, "review")
    assert updated.permission_mode == "bypass"
    assert updated.agent_mode_revision == 1
    reopened = ConversationRepository(storage)
    record = reopened.get_conversation(plan.id)
    assert record.agent_mode == "review" and record.permission_mode == "bypass"
    assert record.agent_mode_revision == updated.agent_mode_revision
    summary = next(item for item in reopened.list_conversations() if item.id == plan.id)
    assert project_public_conversation_summary(summary)["agent_mode"] == "review"
    payload = build_conversation_switched_payload(record, is_hydrating=False, runtime_snapshot={})
    assert payload["conversation"]["agent_mode"] == "review"
    assert payload["conversation"]["permission_mode"] == "bypass"
    assert payload["conversation"]["revision"] == updated.revision
    assert reopened.get_conversation(normal.id).agent_mode == "build"


def test_new_request_default_is_build_and_explicit_adopted_mode_is_independent_of_permission():
    default = parse_conversation_create_request({"permission_mode": "plan"})
    assert default.agent_mode == "build" and default.permission_mode == "plan"
    adopted = parse_conversation_create_request({"agent_mode": "plan", "permission_mode": "bypass"})
    assert adopted.agent_mode == "plan" and adopted.permission_mode == "bypass"
    old_record = ConversationRecord.from_dict({"id": "conv_legacy_mode", "title": "Legacy", "permission_mode": "plan"})
    assert old_record.agent_mode == "build"
    assert project_public_conversation(old_record)["agent_mode"] == "build"


@pytest.mark.asyncio
async def test_mode_command_updates_exact_hidden_owner_and_real_receipt_without_activating_it(tmp_path, monkeypatch):
    manager = WebSocketManager()
    connection = _connection(tmp_path, session_id="mode-command-chain")
    session, _ = await manager.connect(**connection)
    monkeypatch.setattr(session, "refresh_llm_selection", lambda **_: None)
    monkeypatch.setattr(session.session_lifecycle, "schedule_runtime_capabilities", lambda **_: None)
    active = session.conversation_repo.create_conversation(title="Foreground", agent_mode="build", permission_mode="confirm")
    hidden = session.conversation_repo.create_conversation(title="Hidden", agent_mode="build", permission_mode="bypass")
    session.active_conversation_id = active.id
    try:
        handled = await session.command_dispatcher._dispatch_client_command(UserCommand("conversation.agent_mode.set", {
            "conversation_id": hidden.id, "agent_mode": "plan", "client_command_id": "hidden-mode-receipt",
        }), session.connection_generation)
        assert handled
        await session.event_outbox.drain_delivery()
        record = session.conversation_repo.get_conversation(hidden.id)
        assert record.agent_mode == "plan" and record.permission_mode == "bypass"
        assert session.active_conversation_id == active.id
        result = next(event for event in connection["websocket"].sent if event.get("type") == "command.result"
                      and event.get("client_command_id") == "hidden-mode-receipt")
        assert result["command"] == "conversation.agent_mode.set"
        assert result["level"] == "success"
        assert result["data"]["conversation_id"] == hidden.id
        assert result["data"]["agent_mode"] == "plan"
        assert result["data"]["revision"] == record.revision
        inventories = [event for event in connection["websocket"].sent if event.get("type") == "conversation.list"]
        assert any(next(item for item in event["conversations"] if item["id"] == hidden.id)["agent_mode"] == "plan"
                   for event in inventories if any(item["id"] == hidden.id for item in event["conversations"]))
        snapshot = session.runtime_snapshot(include_capabilities=False)
        assert snapshot["agent_mode"] == "build"
        assert snapshot["active_conversation_id"] == active.id
        session.active_conversation_id = hidden.id
        snapshot = session.runtime_snapshot(include_capabilities=False)
        assert snapshot["agent_mode"] == "plan"
        assert snapshot["agent_mode_revision"] == record.revision
    finally:
        await _release_session(session)


@pytest.mark.parametrize("selected,explicit,role,expected_durable", [
    ("plan", None, "main", "plan"),
    ("plan", "build", "main", "build"),
    ("build", "review", "main", "review"),
    ("plan", "explore", "subagent:worker", "plan"),
])
def test_turn_and_recovery_use_owned_durable_mode_while_explicit_child_modes_do_not_rewrite_parent(
    tmp_path, monkeypatch, isolate_runtime_data_dirs, selected, explicit, role, expected_durable,
):
    memory_state = Path(os.environ["MINICODE_STATE_ROOT"]) / "mode-chain-memory"
    monkeypatch.setattr("backend.memory.file_memory.DATA_ROOT", memory_state)
    monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", memory_state / "memory")
    events = []
    session = _Session(tmp_path, events)
    session.conversation_repo.update_agent_mode("conv_runnerdone", selected)
    foreign = session.conversation_repo.create_conversation(title="Unrelated active task", agent_mode="build")
    session.active_conversation_id = foreign.id
    captured = []

    async def runner(**kwargs):
        captured.append(kwargs["metadata"]["agent_mode"])
        await _admit_runner_turn(kwargs)
        yield AgentEvent.agent_message_completed("Actual completed answer", source="model_final")
        yield AgentEvent.done()

    session.query_engine = QueryEngine(runner=runner)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None, **_kwargs: AppConfig(llm=LLMSettings(provider="openai", model="gpt-test", api_key="test-key")))
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", lambda config, model_override=None, **_kwargs: _NoopLLM())
    metadata = {"agent_runtime": default_runtime(), "assistant_message_id": "assistant-mode-turn", "user_message_id": "user-mode-turn", "agent_role": role}
    if explicit is not None:
        metadata["agent_mode"] = explicit
        metadata["_agent_mode_selection_revision"] = session.conversation_repo.get_conversation("conv_runnerdone").agent_mode_revision
    _run_session_scenario(session, session._run_agent_locked("Continue owned work", conversation_id="conv_runnerdone", metadata=metadata))
    assert captured == [explicit or selected], events
    assert session.conversation_repo.get_conversation("conv_runnerdone").agent_mode == expected_durable
    assert session.conversation_repo.get_conversation(foreign.id).agent_mode == "build"
    reopened = ConversationRepository(session.conversation_repo.storage_root)
    assert reopened.get_conversation("conv_runnerdone").agent_mode == expected_durable


def _owned_admission_session(tmp_path, monkeypatch):
    memory_state = Path(os.environ["MINICODE_STATE_ROOT"]) / "mode-admission-memory"
    monkeypatch.setattr("backend.memory.file_memory.DATA_ROOT", memory_state)
    monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", memory_state / "memory")
    events = []
    session = _Session(tmp_path, events)
    session._extension_shutdown_requested = False
    session.running_agent_task_for = session.run_manager.running_task_for
    session.run_manager.stop_notification_wake_intake()

    async def start(content, *, attachments, conversation_id, metadata):
        await session._run_agent_locked(content, attachments=attachments, conversation_id=conversation_id, metadata=metadata)

    session.start_agent_run = start
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None, **_kwargs: AppConfig(llm=LLMSettings(api_key="test-key")))
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", lambda config, model_override=None, **_kwargs: _NoopLLM())
    return session, events


@pytest.mark.parametrize("requested_behavior", ["follow_up", "steer"])
def test_accepted_queued_build_keeps_its_turn_mode_after_new_plan_selection_and_durable_queue_reopen(
    tmp_path, monkeypatch, isolate_runtime_data_dirs, requested_behavior,
):
    session, events = _owned_admission_session(tmp_path, monkeypatch)
    owner = "conv_runnerdone"
    session.conversation_repo.update_agent_mode(owner, "plan")
    captured = []

    async def runner(**kwargs):
        captured.append(kwargs["metadata"]["agent_mode"])
        await _admit_runner_turn(kwargs)
        yield AgentEvent.agent_message_completed("Queued build actually ran", source="model_final")
        yield AgentEvent.done()

    session.query_engine = QueryEngine(runner=runner)

    async def scenario():
        running = asyncio.create_task(asyncio.Event().wait())
        session.run_manager.register(conversation_id=owner, task=running, task_id="busy-plan-turn",
                                     cancel_event=asyncio.Event(), active_conversation_id=owner)
        session._conversation_streams[owner] = {**create_stream_state(owner, "running-plan", "busy-plan-turn"), "agent_mode": "plan"}
        session.run_manager.turn_input_queue(owner).begin_turn("busy-plan-turn")
        try:
            command = UserCommand("user_message", {"content": "Implement approved plan", "conversation_id": owner,
                "agent_mode": "build", "streaming_behavior": requested_behavior,
                "user_message_id": "queued-build-user", "assistant_message_id": "queued-build-answer", "client_command_id": "queued-build-command",
                **({"expected_turn_id": "busy-plan-turn"} if requested_behavior == "steer" else {})})
            await session.command_dispatcher._handle_command_inner(command)
            accepted = next(event for event in events if event.get("type") == "command.result" and event.get("command") == "user_message")
            if requested_behavior == "steer":
                assert accepted["level"] == "error"
                assert accepted["data"]["error_code"] == "steer_configuration_mismatch"
                assert accepted["data"]["field"] == "agent_mode"
                assert session.conversation_repo.get_conversation(owner).agent_mode == "plan"
                assert session.run_manager.queued_user_messages(owner) == []
                assert session.run_manager.turn_input_queue(owner).pending_count() == 0
                assert session.run_manager.running_task_for(owner) is running
                return
            assert accepted["data"]["queued"] is True
            assert session.conversation_repo.get_conversation(owner).agent_mode == "build"
            session.conversation_repo.update_agent_mode(owner, "plan")
            selected_revision = session.conversation_repo.get_conversation(owner).agent_mode_revision
            # Rebuild both durable stores; no in-memory command or record is
            # reused to establish the queued mode or the future selection.
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            session.run_manager.run_tasks.pop(owner)
            storage = session.conversation_repo.storage_root
            session.conversation_repo = ConversationRepository(storage)
            session.conversation_runtime._conversation_repo = session.conversation_repo
            session.run_manager.close_durable_queue()
            session.run_manager = SessionRunManager(session)
            session.run_manager.stop_notification_wake_intake()
            session.running_agent_task_for = session.run_manager.running_task_for
            pending = session.run_manager.queued_user_messages(owner)
            assert len(pending) == 1 and pending[0].data["agent_mode"] == "build"
            restored = session.conversation_repo.get_conversation(owner)
            assert restored.agent_mode == "plan" and restored.agent_mode_revision == selected_revision
            queued = session.run_manager.dequeue_user_message(owner)
            queued.data["_queued_user_message_dispatch"] = True
            await session.command_dispatcher._handle_command_inner(queued)
            assert captured == ["build"]
            final = ConversationRepository(storage).get_conversation(owner)
            assert final.agent_mode == "plan" and final.agent_mode_revision == selected_revision
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            session.run_manager.close_durable_queue()

    _run_session_scenario(session, scenario())


def test_queue_configuration_write_failure_reopens_original_selection_revision_and_applies_only_at_real_admission(
    tmp_path, monkeypatch, isolate_runtime_data_dirs,
):
    session, _ = _owned_admission_session(tmp_path, monkeypatch)
    owner = "conv_runnerdone"
    session.conversation_repo.update_agent_mode(owner, "plan")
    captured = []

    async def runner(**kwargs):
        captured.append(kwargs["metadata"]["agent_mode"])
        await _admit_runner_turn(kwargs)
        yield AgentEvent.agent_message_completed("Recovered accepted queue", source="model_final")
        yield AgentEvent.done()

    session.query_engine = QueryEngine(runner=runner)

    async def scenario():
        running = asyncio.create_task(asyncio.Event().wait())
        session.run_manager.register(conversation_id=owner, task=running, task_id="busy-before-mode-write",
                                     cancel_event=asyncio.Event(), active_conversation_id=owner)
        update_mode = session.conversation_repo.update_agent_mode
        def failed_configuration_write(*args, **kwargs):
            raise OSError("Actual configuration write failure")
        monkeypatch.setattr(session.conversation_repo, "update_agent_mode", failed_configuration_write)
        try:
            with pytest.raises(OSError, match="configuration write failure"):
                await session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
                    "content": "Durable accepted build", "conversation_id": owner, "agent_mode": "build",
                    "user_message_id": "recover-mode-user", "assistant_message_id": "recover-mode-answer", "client_command_id": "recover-mode-command",
                }))
            assert session.conversation_repo.get_conversation(owner).agent_mode == "plan"
            monkeypatch.setattr(session.conversation_repo, "update_agent_mode", update_mode)
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            storage = session.conversation_repo.storage_root
            session.run_manager.close_durable_queue()
            session.conversation_repo = ConversationRepository(storage)
            session.conversation_runtime._conversation_repo = session.conversation_repo
            session.run_manager = SessionRunManager(session)
            session.run_manager.stop_notification_wake_intake()
            session.running_agent_task_for = session.run_manager.running_task_for
            pending = session.run_manager.dequeue_user_message(owner)
            assert pending is not None and pending.data["_agent_mode_selection_revision"] == 1
            pending.data["_queued_user_message_dispatch"] = True
            await session.command_dispatcher._handle_command_inner(pending)
            assert captured == ["build"]
            restored = ConversationRepository(storage).get_conversation(owner)
            assert restored.agent_mode == "build" and restored.agent_mode_revision == 2
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            session.run_manager.close_durable_queue()

    _run_session_scenario(session, scenario())


@pytest.mark.parametrize("new_selection", [None, "plan"])
def test_fresh_turn_updates_default_only_at_actual_admission_and_keeps_a_newer_same_value_choice(
    tmp_path, monkeypatch, isolate_runtime_data_dirs, new_selection,
):
    session, _ = _owned_admission_session(tmp_path, monkeypatch)
    owner = "conv_runnerdone"
    session.conversation_repo.update_agent_mode(owner, "plan")
    captured = []

    async def scenario():
        reached, release = asyncio.Event(), asyncio.Event()

        async def runner(**kwargs):
            captured.append(kwargs["metadata"]["agent_mode"])
            reached.set()
            await release.wait()
            await _admit_runner_turn(kwargs)
            yield AgentEvent.agent_message_completed("Actually admitted", source="model_final")
            yield AgentEvent.done()

        session.query_engine = QueryEngine(runner=runner)
        pending = asyncio.create_task(session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
            "content": "Implement", "conversation_id": owner, "agent_mode": "build",
            "user_message_id": "fresh-mode-user", "assistant_message_id": "fresh-mode-answer", "client_command_id": "fresh-mode-command",
        })))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            # Startup progress did not change the default before admission.
            assert session.conversation_repo.get_conversation(owner).agent_mode == "plan"
            if new_selection is not None:
                previous = session.conversation_repo.get_conversation(owner).agent_mode_revision
                session.conversation_repo.update_agent_mode(owner, new_selection)
                assert session.conversation_repo.get_conversation(owner).agent_mode_revision > previous
            release.set()
            await pending
            assert captured == ["build"]
            reopened = ConversationRepository(session.conversation_repo.storage_root).get_conversation(owner)
            assert reopened.agent_mode == (new_selection or "build")
        finally:
            release.set()
            await pending
            session.run_manager.close_durable_queue()

    _run_session_scenario(session, scenario())


def test_unadmitted_startup_refusal_and_failed_queue_publish_leave_the_real_selection_unchanged(
    tmp_path, monkeypatch, isolate_runtime_data_dirs,
):
    session, _ = _owned_admission_session(tmp_path, monkeypatch)
    owner = "conv_runnerdone"
    session.conversation_repo.update_agent_mode(owner, "plan")
    selected_revision = session.conversation_repo.get_conversation(owner).agent_mode_revision

    async def refused(**kwargs):
        yield AgentEvent.error("Actual startup refusal", error_type="startup")
        yield AgentEvent.done(status="failed", reason="startup_failed")

    session.query_engine = QueryEngine(runner=refused)

    async def scenario():
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
            "content": "Not admitted", "conversation_id": owner, "agent_mode": "build", "client_command_id": "refused-mode-command",
        }))
        assert session.conversation_repo.get_conversation(owner).agent_mode == "plan"
        running = asyncio.create_task(asyncio.Event().wait())
        session.run_manager.register(conversation_id=owner, task=running, task_id="busy-queue-refusal",
                                     cancel_event=asyncio.Event(), active_conversation_id=owner)
        def fail_publish(*args, **kwargs):
            raise OSError("Actual queue publish failure")
        monkeypatch.setattr(session.run_manager, "enqueue_user_message", fail_publish)
        try:
            with pytest.raises(OSError, match="queue publish failure"):
                await session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
                    "content": "Queue failed", "conversation_id": owner, "agent_mode": "build", "client_command_id": "failed-queue-mode-command",
                }))
            saved = ConversationRepository(session.conversation_repo.storage_root).get_conversation(owner)
            assert saved.agent_mode == "plan" and saved.agent_mode_revision == selected_revision
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            session.run_manager.close_durable_queue()

    _run_session_scenario(session, scenario())
