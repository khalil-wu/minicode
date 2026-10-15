from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import AgentSettings, AppConfig, LLMSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.context import PermissionContext
from backend.ws.agent_runner import _clear_session_llm_cache, _get_or_create_session_llm
from backend.ws.handlers.conversation import handle_context_compact
from backend.agent.conversation_query_guard import conversation_query_guards


def compaction_states(session):
    events = [call.args[0].data for call in session.send_event.await_args_list if call.args[0].type == "conversation.compaction.updated"]
    assert all(event["summary"].strip() for event in events)
    return [event["state"] for event in events]


def assert_original_transcript_and_real_compactions(after, before, *, count=1):
    assert after.transcript[:len(before.transcript)] == before.transcript
    operations = after.transcript[len(before.transcript):]
    assert len(operations) == count
    for operation in operations:
        assert operation["role"] == "system" and operation["content"] == ""
        block = operation["blocks"][0]
        assert block["itemKind"] == "context_compaction" and block["source"] == "manual"
        assert block["status"] == "completed"
        assert block["startedAt"] == block["timestamp"] <= block["finishedAt"]


class CompactEndpoint:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.hold = False
        self.entered = threading.Event()
        self.release = threading.Event()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                endpoint.requests.append((self.path, payload))
                endpoint.entered.set()
                if endpoint.hold:
                    endpoint.release.wait(5)
                body = {"id": "compact-response", "output": [{"type": "compaction", "encrypted_content": "retained-provider-window"}],
                    "usage": {"input_tokens": 100, "output_tokens": 10}}
                if not self.path.endswith("/compact"):
                    body = {"id": "summary-response", "status": "completed", "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Retained actual user constraints and completed work."}]}],
                        "usage": {"input_tokens": 100, "output_tokens": 10}}
                if endpoint.status != 200:
                    body = {"error": {"message": "actual compact provider rejection"}}
                encoded = json.dumps(body).encode()
                content_type = "application/json"
                if payload.get("stream") and endpoint.status == 200:
                    content_type = "text/event-stream"
                    encoded = ("data: " + json.dumps({"type": "response.output_text.delta", "item_id": "summary",
                        "delta": "Retained actual user constraints and completed work."}) + "\n\n"
                        + "data: " + json.dumps({"type": "response.completed", "response": body}) + "\n\n").encode()
                self.send_response(endpoint.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    # The cancellation case deliberately closes its HTTP request.
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"


async def prepare(monkeypatch, tmp_path, endpoint, *, active=True, native=True):
    config = AppConfig(llm=LLMSettings(provider="openai", api_key="fixture-key", base_url=endpoint.url,
        model="alpha", wire_api="responses", native_compaction=native, responses_websocket=False),
        agent=AgentSettings(compaction_keep_recent_tokens=0 if native else 32))
    adapters = []

    def factory(selected, **_kwargs):
        adapter = OpenAIAdapter(selected.llm)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr("backend.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", factory)
    monkeypatch.setattr("backend.ws.handlers.conversation._conversation_has_active_run", lambda *_args: False)
    registry = SimpleNamespace(get_schemas=lambda **_kwargs: [])
    repo = ConversationRepository(tmp_path / "conversations")
    target = "conv_compact_owner"
    session = SimpleNamespace(session_id="manual-compact-session", config=config, cleanup_tasks=set(), _llm_close_tasks=set(), _llm_adapter_cache={},
        _llm_adapter_leases={}, _retired_llm_adapters={}, active_conversation_id=target if active else "other-owner",
        _extension_runtime_states={}, _model_runtime_for_conversation=lambda _id: None,
        _conversation_tool_registry=lambda _id: registry, tool_registry=registry,
        permission_context=PermissionContext(), permission_checker=None,
        permission_context_for_conversation=lambda *_args, **_kwargs: PermissionContext(),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _record: tmp_path),
        conversation_repo=repo, last_agent_state=AgentState(user_message="Keep user constraints"), send_event=AsyncMock(),
        _conversation_projection_lock=lambda _id: lock,
    )
    lock = asyncio.Lock()

    async def previous_turn():
        return _get_or_create_session_llm(session, config=config, provider="openai", model="alpha")

    retired = await asyncio.create_task(previous_turn())
    await asyncio.sleep(0)
    builder = ContextBuilder(llm=retired, conversation_id=target, workspace_root=tmp_path)
    await builder.start_turn("Keep user constraints", session.last_agent_state)
    builder.append_assistant("Actual completed task")
    await builder.start_turn("Keep the newest instruction", AgentState(user_message="Keep the newest instruction"))
    snapshot = builder.export_snapshot()
    snapshot["ui_agent_state"] = {"subagents": [{"id": "real-child", "status": "done"}]}
    record = repo.create_conversation(conversation_id=target, context_snapshot=snapshot, workspace_root=str(tmp_path),
        model_selection={"provider": "openai", "model": "alpha"}, transcript=[
            {"id": "user", "role": "user", "content": "Keep user constraints", "timestamp": 1},
            {"id": "answer", "role": "assistant", "content": "Actual completed task", "timestamp": 2},
        ])
    session.context_builder = builder if active else ContextBuilder(llm=retired, conversation_id="other-owner")
    session.llm = retired
    session.conversation_runtime = SimpleNamespace(_context_builder=session.context_builder,
        active_conversation_id=session.active_conversation_id, wait_for_hydration=AsyncMock(), defer_repository_hydration=Mock())
    session._on_conversation_hydration_complete = lambda *_args: None
    _clear_session_llm_cache(session)
    await asyncio.gather(*session._llm_close_tasks)
    assert retired._closed and retired._http_client is None
    return session, adapters, record


@pytest.mark.parametrize("active", [False, True])
def test_manual_compact_captures_a_live_model_after_the_previous_turn_adapter_was_retired(monkeypatch, tmp_path, active):
    async def scenario(endpoint):
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint, active=active)
        other_snapshot = session.context_builder.export_snapshot()
        await handle_context_compact(session, {"conversation_id": before.id})
        after = session.conversation_repo.get_conversation(before.id)
        assert [path for path, _payload in endpoint.requests] == ["/v1/responses/compact"]
        assert endpoint.requests[0][1]["model"] == "alpha"
        assert after.compaction_state == "compacted"
        assert_original_transcript_and_real_compactions(after, before)
        assert after.context_snapshot["ui_agent_state"] == before.context_snapshot["ui_agent_state"]
        assert adapters[0]._closed and adapters[0]._http_client is None
        assert adapters[1]._closed and adapters[1]._http_client is None
        if active:
            assert session.context_builder.export_snapshot()["history"] == after.context_snapshot["history"]
        else:
            assert session.context_builder.export_snapshot() == other_snapshot
        assert not session._llm_adapter_leases
        assert any(call.args[0].type == "context_compacted" for call in session.send_event.await_args_list)
        assert compaction_states(session) == ["compacting", "compacted"]

    with CompactEndpoint() as endpoint:
        asyncio.run(scenario(endpoint))


def test_repeated_compaction_uses_the_current_model_and_rejects_foreign_native_origin_without_losing_history(monkeypatch, tmp_path):
    async def scenario(endpoint, foreign):
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint)
        await handle_context_compact(session, {"conversation_id": before.id})
        session.conversation_repo.update_model_selection(before.id, provider="openai", model="beta")
        await handle_context_compact(session, {"conversation_id": before.id})
        assert [payload["model"] for _path, payload in endpoint.requests] == ["alpha", "beta"]
        assert endpoint.requests[1][1]["input"] == [{"type": "compaction", "encrypted_content": "retained-provider-window"}]
        saved = session.conversation_repo.get_conversation(before.id)
        live = deepcopy(session.context_builder.export_snapshot())
        config = replace(session.config, llm=replace(session.config.llm, base_url=foreign.url))
        monkeypatch.setattr("backend.config.load_config", lambda **_kwargs: config)
        await handle_context_compact(session, {"conversation_id": before.id})
        assert not foreign.requests
        assert session.conversation_repo.get_conversation(before.id).context_snapshot == saved.context_snapshot
        assert session.context_builder.export_snapshot() == live
        assert session.conversation_repo.get_conversation(before.id).transcript == saved.transcript
        assert [call.args[0].data for call in session.send_event.await_args_list if call.args[0].type == "error"][-1]["error_code"] == "context.compact_failed"
        assert all(adapter._closed for adapter in adapters)

    with CompactEndpoint() as endpoint, CompactEndpoint() as foreign:
        asyncio.run(scenario(endpoint, foreign))


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_provider_failure_or_operation_cancellation_preserves_the_original_context(monkeypatch, tmp_path, cancel):
    async def scenario(endpoint):
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint)
        live = deepcopy(session.context_builder.export_snapshot())
        if cancel:
            endpoint.hold = True
            waiter = asyncio.create_task(handle_context_compact(session, {"conversation_id": before.id}))
            assert await asyncio.to_thread(endpoint.entered.wait, 10)
            operation = next(iter(session.cleanup_tasks))
            assert adapters[-1]._http_client is not None
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            endpoint.release.set()
        else:
            endpoint.status = 400
            await handle_context_compact(session, {"conversation_id": before.id})
            error = [call.args[0].data for call in session.send_event.await_args_list if call.args[0].type == "error"][-1]
            assert error["error_code"] == "context.compact_failed"
            assert "400 Bad Request" in error["message"]
        assert session.conversation_repo.get_conversation(before.id).context_snapshot == before.context_snapshot
        assert session.conversation_repo.get_conversation(before.id).transcript == before.transcript
        assert session.context_builder.export_snapshot() == live
        assert adapters[-1]._closed and adapters[-1]._http_client is None
        assert not session._llm_adapter_leases
        assert compaction_states(session) == ["compacting", "failed"]

    with CompactEndpoint() as endpoint:
        asyncio.run(scenario(endpoint))


def test_manual_local_summary_compaction_uses_a_fresh_client_and_retains_admitted_user_instructions(monkeypatch, tmp_path):
    async def scenario(endpoint):
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint, native=False)
        await handle_context_compact(session, {"conversation_id": before.id})
        after = session.conversation_repo.get_conversation(before.id)
        assert [path for path, _payload in endpoint.requests] == ["/v1/responses"]
        assert after.compaction_state == "compacted"
        assert "Retained actual user constraints" in str(after.context_snapshot["history"])
        assert "Keep the newest instruction" in str(after.context_snapshot["history"])
        assert_original_transcript_and_real_compactions(after, before)
        assert after.context_snapshot["ui_agent_state"] == before.context_snapshot["ui_agent_state"]
        assert all(adapter._closed and adapter._http_client is None for adapter in adapters)

    with CompactEndpoint() as endpoint:
        asyncio.run(scenario(endpoint))


def test_cancelling_only_the_waiter_does_not_close_the_adapter_before_the_actual_commit(monkeypatch, tmp_path):
    async def scenario(endpoint):
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint)
        endpoint.hold = True
        waiter = asyncio.create_task(handle_context_compact(session, {"conversation_id": before.id}))
        assert await asyncio.to_thread(endpoint.entered.wait, 10)
        operation = next(iter(session.cleanup_tasks))
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not operation.done()
        assert compaction_states(session) == ["compacting"]
        assert not adapters[-1]._closed and adapters[-1]._http_client is not None
        assert session.conversation_repo.get_conversation(before.id).context_snapshot == before.context_snapshot
        endpoint.release.set()
        await operation
        await asyncio.sleep(0)
        await asyncio.gather(*session._llm_close_tasks)
        assert session.conversation_repo.get_conversation(before.id).compaction_state == "compacted"
        assert adapters[-1]._closed and adapters[-1]._http_client is None
        assert not session._llm_adapter_leases
        assert compaction_states(session) == ["compacting", "compacted"]

    with CompactEndpoint() as endpoint:
        asyncio.run(scenario(endpoint))


def test_query_claim_rejection_does_not_publish_a_fake_compaction_start(monkeypatch, tmp_path):
    async def scenario(endpoint):
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint)
        claim = conversation_query_guards().try_start(before.id, owner_id="actual-other-query")
        assert claim is not None
        try:
            await handle_context_compact(session, {"conversation_id": before.id})
            assert compaction_states(session) == []
            assert not endpoint.requests
            assert [call.args[0].data for call in session.send_event.await_args_list if call.args[0].type == "error"][-1]["error_code"] == "conversation.active_run"
            assert adapters[-1]._closed
        finally:
            conversation_query_guards().end(claim)

    with CompactEndpoint() as endpoint:
        asyncio.run(scenario(endpoint))


def test_switching_conversation_during_compaction_does_not_replace_the_new_owners_context(monkeypatch, tmp_path):
    async def scenario(endpoint):
        session, adapters, before = await prepare(monkeypatch, tmp_path, endpoint)
        endpoint.hold = True
        waiter = asyncio.create_task(handle_context_compact(session, {"conversation_id": before.id}))
        assert await asyncio.to_thread(endpoint.entered.wait, 10)
        session.active_conversation_id = "conv_other_owner"
        session.conversation_runtime.active_conversation_id = "conv_other_owner"
        session.context_builder.clear()
        session.context_builder.append_user("Other owner's unsaved task context")
        session.last_agent_state = AgentState(user_message="Other owner's new prompt", conversation_id="conv_other_owner")
        other = deepcopy(session.context_builder.export_snapshot())
        endpoint.release.set()
        await waiter
        assert session.context_builder.export_snapshot() == other
        after = session.conversation_repo.get_conversation(before.id)
        assert after.compaction_state == "compacted"
        assert_original_transcript_and_real_compactions(after, before)
        assert after.context_snapshot["ui_agent_state"] == before.context_snapshot["ui_agent_state"]
        for call in session.send_event.await_args_list:
            event = call.args[0]
            payload = event.data["data"] if event.type == "command.result" else event.data
            assert payload["conversation_id"] == before.id
        assert adapters[-1]._closed

    with CompactEndpoint() as endpoint:
        asyncio.run(scenario(endpoint))
