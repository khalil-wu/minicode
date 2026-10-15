"""Producer-scoped request identity through the real WS/admission stores."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from backend import main
from backend.agent.execution_journal import execution_journal_owner, turn_input_consumption_id, turn_user_prompt_id
from backend.agent.runtime import AgentRuntime, default_runtime
from backend.conversations.repository import ConversationRepository
from backend.tests.test_unconfigured_session_protocol import ProtocolSocket, protocol_environment
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.run_manager import SessionRunManager


@pytest.mark.asyncio
async def test_two_verified_windows_same_conversation_same_request_id_execute_both_tasks(monkeypatch, protocol_environment):
    from backend.config_helpers import SETTINGS_FILE
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fixture")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://identity.protocol.invalid/v1")
    SETTINGS_FILE.write_text(json.dumps({"llm": {"provider": "openai"}}), encoding="utf-8")
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        output = [{"type": "message", "id": f"answer-{len(requests)}", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Task complete.", "annotations": []}]}]
        event = {"type": "response.completed", "response": {"id": f"response-{len(requests)}", "status": "completed",
            "end_turn": True, "output": output, "usage": {"input_tokens": 20, "output_tokens": 4}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(event) + "\n\n")

    def build(config, *, model_override=None, provider_override=None, model_runtime=None):
        from backend.services.llm_adapter_factory import build_provider_adapter
        adapter = build_provider_adapter(provider_override, model_override, model_runtime=model_runtime)
        adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        return adapter

    async def verified(_socket, instance, owner):
        assert instance == owner + "-instance"
        return owner + "-controlled-proof"

    monkeypatch.setattr(main, "_create_session_llm", build)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", build)
    monkeypatch.setattr("backend.ws.manager._verified_window_binding", verified)
    sockets = [ProtocolSocket("identity-window-a"), ProtocolSocket("identity-window-b")]
    for index, socket in enumerate(sockets):
        owner = f"identity-owner-{index}"
        socket.query_params.update(window_owner_id=owner, window_instance_id=owner + "-instance")
    async with main.lifespan(main.app):
        tasks = [asyncio.create_task(main.websocket_endpoint(socket)) for socket in sockets]
        try:
            for socket in sockets:
                await socket.until("llm.model.updated")
            await sockets[0].command("conversation.create", activate=True, title="Shared request identity")
            conversation_id = (await sockets[0].until("conversation.switched"))["conversation_id"]
            await sockets[1].command("conversation.switch", conversation_id=conversation_id)
            await sockets[1].until("conversation.switched", conversation_id=conversation_id, is_hydrating=False)
            receipts = []
            for socket in sockets:
                # No message IDs are sent; two independent producers legally
                # reuse one request ID and even submit identical content.
                await socket.command("user_message", content="Complete this task", conversation_id=conversation_id,
                    client_command_id="same-request")
                receipt = await socket.until("command.result", command="user_message", client_command_id="same-request")
                assert receipt["data"]["admitted"] is True
                done = await socket.until("done", conversation_id=conversation_id, message_id=receipt["data"]["assistant_message_id"])
                assert done["status"] == "completed"
                receipts.append(receipt["data"])
            assert len(requests) == 2
            assert len({item["user_message_id"] for item in receipts}) == 2
            assert len({item["assistant_message_id"] for item in receipts}) == 2
            session = main._state.ws_manager.get_session(sockets[0].query_params["session_id"])
            saved = session.conversation_repo.get_conversation(conversation_id)
            assert [item["id"] for item in saved.transcript if item["role"] == "user"] == [item["user_message_id"] for item in receipts]
            assert {item["id"] for item in saved.transcript if item["role"] == "assistant"} == {item["assistant_message_id"] for item in receipts}
            journal = default_runtime().execution_journal(execution_journal_owner("conversation", session.conversation_repo.store_instance_id(), conversation_id))
            for socket, receipt in zip(sockets, receipts):
                origin = DurableUserMessageQueue.queue_key_for_session(socket.query_params["session_id"])
                boundary = saved.context_snapshot["turn_admissions"][receipt["user_message_id"]]
                assert boundary["client_queue_key"] == origin
                assert boundary["assistant_message_id"] == receipt["assistant_message_id"]
                consumed = journal.get_event(turn_input_consumption_id(receipt["user_message_id"], "same-request"))
                assert consumed.payload["client_queue_key"] == origin
        finally:
            for socket, task in zip(sockets, tasks):
                await socket.finish(task)


@pytest.fixture
def recovered_session(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(conversation_id="identity-conversation", memory_mode="disabled")
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    events, starts = [], []

    async def send_event(event):
        events.append(event)

    async def start(content, **kwargs):
        starts.append((content, kwargs))

    session = SimpleNamespace(session_id="identity-source", active_conversation_id=conversation.id,
        _extension_shutdown_requested=False, ws_manager=None, connection_generation=1,
        conversation_repo=repository, running_agent_task_for=lambda cid: None,
        conversation_lifecycle_lock=lambda: asyncio.Lock(),
        event_outbox=SimpleNamespace(bind_connection_generation=lambda *_: nullcontext(), bind_client_command=lambda *_, **__: nullcontext()),
        session_lifecycle=SimpleNamespace(schedule_task_runtime_update=lambda: None, workspace_root_for_conversation=lambda *_: ""),
        send_event=send_event, start_agent_run=start)
    queue_path = tmp_path / "user-message-queue/identity-source.json"
    queue_path.parent.mkdir(parents=True)
    packet = {"type": "user_message", "data": {"conversation_id": conversation.id, "client_command_id": "legacy-request",
        "content": "Original submitted content", "display_content": "Original submitted content", "attachments": [{
            "id": "attachment-original", "kind": "document", "file_name": "原文附件.txt", "media_type": "text/plain",
            "artifact_id": "artifact-original", "doc_id": "", "size_bytes": 6, "title": "", "summary": "",
            "input_source": "", "source_char_count": 6}]}}
    queue_path.write_text(json.dumps({"version": 6, "client_pending": [packet]}), encoding="utf-8")
    manager = session.run_manager = SessionRunManager(session)
    manager._notification_wakes_closed = True
    session.command_dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "completion-log")
    journal = runtime.execution_journal(execution_journal_owner("conversation", repository.store_instance_id(), conversation.id))
    yield session, runtime, journal, packet, events, starts
    manager._unsubscribe_parent_notifications()
    manager._unsubscribe_subagent_activity()
    manager.close_durable_queue()
    runtime.close(release_lease=True)


def _admit_legacy(session, runtime, journal, packet, *, steered=False, consumed=True):
    run = runtime.start_run(conversation_id=session.active_conversation_id,
        session_id="another-window" if steered else session.session_id)
    user_id = "user_legacy-request"
    boundary = {"history_start": 0, "history_end": 1, "run_id": run.run_id, "client_command_id": "legacy-request"}
    user = {"id": user_id, "role": "user", "content": packet["data"]["content"],
        "display_content": packet["data"]["display_content"], "attachments": packet["data"]["attachments"], "steered": steered}
    snapshot = {"history": [{"role": "user", "content": user["content"], "is_user_input": True}], "turn_admissions": {user_id: boundary}}
    session.conversation_repo.commit_turn_admission(session.active_conversation_id,
        user_message=user, context_snapshot=snapshot,
        expected_revision=session.conversation_repo.get_conversation(session.active_conversation_id).revision)
    journal.append_once("user_prompt", {**user, "conversation_id": session.active_conversation_id,
        "user_message_id": user_id, "client_command_id": "legacy-request", "context_snapshot": snapshot},
        event_id=turn_user_prompt_id(session.conversation_repo.store_instance_id(), session.active_conversation_id, user_id))
    if consumed:
        journal.append_once("system", {"lifecycle": "turn_input_consumed", "conversation_id": session.active_conversation_id,
            "user_message_id": user_id, "client_command_id": "legacy-request", "run_id": run.run_id},
            event_id=turn_input_consumption_id(user_id, "legacy-request"))
    runtime.commit_terminal(run.run_id, "interrupted", summary="Old process ended", terminal_reason="runtime_interrupted")
    return user_id


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_r6_pending_without_message_ids_restores_proven_original_admission(recovered_session, consumed):
    session, runtime, journal, packet, events, starts = recovered_session
    user_id = _admit_legacy(session, runtime, journal, packet, consumed=consumed)
    await session.command_dispatcher._run_durable_client_command("legacy-request", 1)
    if consumed:
        assert starts == []
        receipt = next(event.data for event in events if event.type == "command.result")
        assert receipt["data"]["consumed"] and receipt["data"]["replayed"]
        assert receipt["data"]["user_message_id"] == user_id
        assert receipt["data"]["assistant_message_id"] == "assistant_legacy-request"
    else:
        assert len(starts) == 1
        metadata = starts[0][1]["metadata"]
        assert metadata["_turn_admission_restored"] is True
        assert metadata["user_message_id"] == user_id
        assert metadata["assistant_message_id"] == "assistant_legacy-request"
    assert len(session.conversation_repo.get_conversation(session.active_conversation_id).transcript) == 1


@pytest.mark.asyncio
async def test_r6_unprovable_steer_origin_reports_failure_and_keeps_original_pending_input(recovered_session):
    session, runtime, journal, packet, events, starts = recovered_session
    _admit_legacy(session, runtime, journal, packet, steered=True)
    await session.command_dispatcher._run_durable_client_command("legacy-request", 1)
    assert starts == []
    failure = next(event.data for event in events if event.type == "command.result")
    assert failure["level"] == "error" and "no provable submitting window" in failure["message"]
    pending = session.run_manager.durable_client_commands.pending_client_commands()
    assert len(pending) == 1
    for key, value in packet["data"].items():
        assert pending[0].data[key] == value
    assert "user_message_id" not in pending[0].data and "assistant_message_id" not in pending[0].data
    assert session.command_dispatcher.recent_client_command_ids == []


@pytest.mark.asyncio
async def test_consumed_journal_projection_is_authoritative_when_live_transcript_no_longer_contains_input(recovered_session):
    session, runtime, journal, packet, events, starts = recovered_session
    origin = session.run_manager.durable_client_commands.path.stem
    suffix = hashlib.sha256(f"{origin}\0legacy-request".encode("utf-8")).hexdigest()
    user_id, assistant_id = "user_" + suffix, "assistant_" + suffix
    boundary = {"history_start": 0, "history_end": 1, "run_id": "original-run", "client_command_id": "legacy-request",
        "client_queue_key": origin, "assistant_message_id": assistant_id}
    journal.append_once("user_prompt", {**packet["data"], "user_message_id": user_id, "client_queue_key": origin,
        "assistant_message_id": assistant_id, "context_snapshot": {"turn_admissions": {user_id: boundary}}},
        event_id=turn_user_prompt_id(session.conversation_repo.store_instance_id(), session.active_conversation_id, user_id))
    journal.append_once("system", {"lifecycle": "turn_input_consumed", "run_id": "original-run"},
        event_id=turn_input_consumption_id(user_id, "legacy-request"))
    await session.command_dispatcher._run_durable_client_command("legacy-request", 1)
    assert starts == []
    receipt = next(event.data for event in events if event.type == "command.result")
    assert receipt["data"]["user_message_id"] == user_id and receipt["data"]["assistant_message_id"] == assistant_id
    assert receipt["data"]["consumed"] is True
    assert session.conversation_repo.get_conversation(session.active_conversation_id).transcript == []
