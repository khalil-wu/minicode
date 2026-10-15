from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import types

from backend.agent.conversation_query_guard import ConversationQueryGuardRegistry
from backend.agent.first_byte_waiter import wait_for_provider_event
from backend.agent.turn_context_runtime import _coerce_extension_context_messages
from backend.attachments.store import AttachmentStore
from backend.bootstrap.app import AppBootstrap
from backend.config_helpers import LLMSettings
from backend.conversations.import_export import import_conversation_tree
from backend.conversations.models import ConversationRecord
from backend.conversations.public_projection import project_public_conversation
from backend.conversations.repository import ConversationRepository
from backend.extensions.loader import ExtensionLoader
from backend.extensions.trust import ExtensionTrustPolicy
from backend.hooks.discovery import discover_hook_snapshot
from backend.hooks.http_executor import _proxy_for_url
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.errors import retry_after_seconds
from backend.llm.openai_adapter import OpenAIAdapter
from backend.mcp.client import MCPClient
from backend.services.chat_api_service import attachment_preview_payload
from backend.ws.event_outbox import EventOutbox
from backend.ws.turn_wait_state import TurnWaitState


@pytest.mark.parametrize("layout", ["monolithic", "split"])
def test_legacy_utf8_survives_migration_current_fallback_and_original_bytes(tmp_path, layout):
    literal = "Keep the literal token \u00c3\u00a9 exactly."
    record = ConversationRecord(
        id="conv_legacy_literal", title=literal,
        transcript=[{"id": "user-old", "role": "user", "content": literal}],
        context_snapshot={"history": [{"role": "user", "content": literal}]},
    )
    if layout == "monolithic":
        payload = record.to_dict()
        payload.pop("encoding_version", None)
        sources = {tmp_path / f"{record.id}.json": json.dumps(payload, ensure_ascii=False)}
    else:
        metadata = record.to_meta_dict()
        metadata.pop("encoding_version", None)
        sources = {
            tmp_path / f"{record.id}.meta.json": json.dumps(metadata, ensure_ascii=False),
            tmp_path / f"{record.id}.transcript.jsonl": json.dumps(record.transcript[0], ensure_ascii=False) + "\n",
            tmp_path / f"{record.id}.snapshot.json": json.dumps(record.context_snapshot, ensure_ascii=False),
        }
    for path, body in sources.items():
        path.write_text(body, encoding="utf-8")
    original = {path: path.read_bytes() for path in sources}
    repo = ConversationRepository(tmp_path)
    loaded = repo.get_conversation(record.id)
    assert loaded.title == loaded.transcript[0]["content"] == literal
    assert loaded.context_snapshot["history"][0]["content"] == literal
    loaded.title = "Updated title"
    repo.save_conversation(loaded)
    cold = ConversationRepository(tmp_path).get_conversation(record.id)
    assert cold.transcript[0]["content"] == cold.context_snapshot["history"][0]["content"] == literal
    assert {path: path.read_bytes() for path in sources} == original
    manifest = json.loads((tmp_path / f"{record.id}.manifest.json").read_text(encoding="utf-8"))
    (tmp_path / f"{record.id}.g{manifest['current_generation']}.transcript.jsonl").write_text("{broken", encoding="utf-8")
    fallback = ConversationRepository(tmp_path).get_conversation(record.id)
    assert fallback.title == fallback.transcript[0]["content"] == literal
    assert fallback.context_snapshot["history"][0]["content"] == literal
    assert {path: path.read_bytes() for path in sources} == original
    assert ConversationRepository(tmp_path).delete_conversation(record.id)
    assert all(not path.exists() for path in sources)


@pytest.mark.parametrize("source,size", [("workspace_snapshot", 1), ("tool_patch", 1), ("tool_patch", 300_000)])
def test_export_import_keeps_complete_owned_diff_without_private_history(tmp_path, source, size):
    repo = ConversationRepository(tmp_path / "source")
    patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+" + "n" * size + "\n"
    created = repo.create_conversation(conversation_id="conv_export_source", context_snapshot={"history": [{"role": "system", "content": "PRIVATE_PROVIDER_MARKER"}]})
    created.transcript = [{"id": "answer", "role": "assistant", "content": "Changed a.txt", "metadata": {"turn_diff": {
        "thread_id": created.id, "conversation_id": created.id, "message_id": "answer", "turn_id": "turn", "revision": 3,
        "source": source, "diff": patch,
    }}}]
    repo.save_conversation(created)
    exported = repo.export_conversation_tree(created.id)
    assert exported["conversations"][0]["transcript"][0]["metadata"]["turn_diff"]["diff"] == patch
    assert "PRIVATE_PROVIDER_MARKER" not in json.dumps(exported)
    target = tmp_path / "target"
    result = import_conversation_tree(ConversationRepository(target), exported)
    cold = ConversationRepository(target)
    diff = cold.get_message_turn_diff(result["conversation_id"], "answer", turn_id="turn", revision=3)
    assert diff["diff"] == patch
    assert diff["thread_id"] == diff["conversation_id"] == result["conversation_id"]
    imported = cold.get_conversation(result["conversation_id"])
    public = project_public_conversation(imported)
    wire_diff = public["transcript"][0]["metadata"]["turn_diff"]
    assert wire_diff["thread_id"] == wire_diff["conversation_id"] == imported.id
    if source == "workspace_snapshot" or size > 262_144:
        assert wire_diff["diff"] is None and wire_diff["deferred"] is True


@pytest.mark.asyncio
async def test_mcp_delayed_request_cleanup_signals_lifecycle_before_join(monkeypatch):
    monkeypatch.setattr("backend.mcp.client.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.02)
    client = MCPClient("audit-cleanup", command="not-launched")
    release, entered, close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def request():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    request_task = asyncio.create_task(request())
    client._active_request_tasks.add(request_task)
    client._close_event = close
    client._lifecycle_task = asyncio.create_task(close.wait())
    await entered.wait()
    assert await client.close() is False
    reaper = asyncio.create_task(client.finish_pending_cleanup())
    release.set()
    assert await asyncio.wait_for(reaper, timeout=1)
    assert close.is_set() and client._lifecycle_task is None
    assert not client.cleanup_status["pending"]


class _ElicitationSession:
    def __init__(self, repository, response):
        self.session_id = "audit-form-session"
        self.is_connected = True
        self.conversation_repo = repository
        self.session_lifecycle = SimpleNamespace(workspace_root_for_conversation=lambda conversation: conversation.workspace_root)
        self.turn_wait_state = TurnWaitState()
        self.response = response
        self.prompts = []
        self.events = []
        self.terminal = []

    async def send_payload(self, payload, *, log_context):
        self.prompts.append(payload)
        self.turn_wait_state.pending_elicitations[payload["request_id"]].set_result(self.response)
        return True

    async def send_event(self, event):
        self.events.append(event.to_ws_message())

    async def emit_approval_cancelled_once(self, request_ids, *, reason, conversation_id):
        self.terminal.append((request_ids, reason, conversation_id))


def _owned_elicitation_client(tmp_path, monkeypatch, response):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(workspace_root=str(tmp_path))
    session = _ElicitationSession(repository, response)
    registry = ConversationQueryGuardRegistry()
    claim = registry.try_start(conversation.id, owner_id="audit-form-run")
    monkeypatch.setattr("backend.agent.conversation_query_guard.conversation_query_guards", lambda: registry)
    manager = object()
    owner = {"session_id": session.session_id, "conversation_id": conversation.id, "mcp_manager": manager,
             "conversation_run_generation": claim.generation, "message_id": "assistant", "turn_id": "turn", "task_id": "task"}
    bootstrap = object.__new__(AppBootstrap)
    bootstrap.ws_manager = SimpleNamespace(iter_sessions=lambda: [session])
    bootstrap.get_mcp_manager_for_workspace = lambda workspace: manager
    client = MCPClient("audit-server", elicitation_handler=bootstrap._handle_mcp_elicitation)
    client._active_tool_request_owners = {1: owner}
    monkeypatch.setattr("backend.mcp.client.feature_enabled", lambda name: name == "mcp_elicitation")
    return client, session, registry, claim


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["object", "field", "option", "items"])
@pytest.mark.parametrize("scheme", ["file", "http"])
async def test_standard_mcp_form_rejects_refs_before_prompt_and_host_io(tmp_path, monkeypatch, location, scheme):
    outside = tmp_path / "outside.json"
    outside.write_text('{"enum":["PRIVATE_AUDIT_MARKER"]}', encoding="utf-8")
    uri = outside.as_uri() if scheme == "file" else "http://127.0.0.1:1/schema.json"
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    if location == "object":
        schema["$ref"] = uri
    elif location == "field":
        schema["properties"]["answer"]["$ref"] = uri
    elif location == "option":
        schema["properties"]["answer"] = {"type": "string", "oneOf": [{"const": "yes", "title": "Yes", "$ref": uri}]}
    else:
        schema["properties"]["answer"] = {"type": "array", "items": {"type": "string", "enum": ["yes"], "$ref": uri}}
    calls = []

    def forbidden_io(*args, **kwargs):
        calls.append(args)
        raise AssertionError("form schema must never open a resource")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_io)
    client, session, registry, claim = _owned_elicitation_client(tmp_path, monkeypatch, {"action": "accept", "content": {"answer": "WRONG"}})
    params = types.ElicitRequestFormParams(message="Choose", requestedSchema=schema)
    try:
        result = await client._sdk_elicitation_callback(SimpleNamespace(request_id=1), params)
        assert isinstance(result, types.ElicitResult) and result.action == "cancel"
        assert not calls and not session.prompts
        assert session.events[-1]["type"] == "command.result"
        assert session.events[-1]["level"] == "error"
        assert session.events[-1]["conversation_id"] == claim.conversation_id
        assert session.events[-1]["data"]["schema_errors"]
        serialized = result.model_dump(by_alias=True, exclude_none=True)
        assert set(serialized) <= {"action", "content", "_meta"}
        assert "PRIVATE_AUDIT_MARKER" not in json.dumps(serialized)
        assert session.turn_wait_state.waiter_ids() == set()
    finally:
        registry.end(claim)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_mcp_accepted_form_keeps_typed_reply_or_visible_validation_failure(tmp_path, monkeypatch, valid):
    content = {"answer": "yes" if valid else "WRONG", "count": 2, "enabled": True, "tags": ["a"]}
    schema = {"type": "object", "required": ["answer"], "properties": {
        "answer": {"type": "string", "oneOf": [{"const": "yes", "title": "Yes"}]},
        "count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
        "enabled": {"type": "boolean", "default": True},
        "tags": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"anyOf": [{"const": "a", "title": "A"}]}},
    }}
    client, session, registry, claim = _owned_elicitation_client(tmp_path, monkeypatch, {"action": "accept", "content": content})
    try:
        result = await client._sdk_elicitation_callback(SimpleNamespace(request_id=2), types.ElicitRequestFormParams(message="Choose", requestedSchema=schema))
        assert isinstance(result, types.ElicitResult)
        assert result.action == ("accept" if valid else "cancel")
        assert len(session.prompts) == 1 and session.terminal
        assert session.turn_wait_state.waiter_ids() == set() and not session.turn_wait_state.pending_approval_payloads
        if valid:
            assert result.content == content and not session.events
        else:
            assert result.content is None
            assert session.events[-1]["level"] == "error"
            assert "failed validation" in session.events[-1]["message"]
            assert "error" not in result.model_dump(by_alias=True)
    finally:
        registry.end(claim)


def _save_attachment(store, identifier, conversation_id="conv_attachment"):
    store.save(artifact_id=identifier, content="Uploaded data", metadata={"conversation_id": conversation_id, "workspace_root": "",
        "attachment": {"artifact_id": identifier, "file_name": identifier + ".txt", "type": "text"}})


def test_existing_attachment_store_inventory_observes_another_store_upload(tmp_path):
    first = AttachmentStore(tmp_path)
    _save_attachment(first, "att_first")
    second = AttachmentStore(tmp_path)
    _save_attachment(second, "att_second")
    _save_attachment(second, "att_other", "conv_other")
    assert {identifier for identifier, _ in first.list_metadata(conversation_id="conv_attachment", workspace_root="")} == {"att_first", "att_second"}


@pytest.mark.parametrize("failure", ["json", "eacces"])
def test_attachment_read_api_and_delete_preserve_actual_storage_error(tmp_path, monkeypatch, failure):
    store = AttachmentStore(tmp_path / "attachments")
    _save_attachment(store, "att_bad")
    path = tmp_path / "attachments" / "att_bad.json"
    if failure == "json":
        path.write_text("{broken", encoding="utf-8")
        expected = json.JSONDecodeError
    else:
        original_read = Path.read_text

        def read(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError(errno.EACCES, "permission denied", str(candidate))
            return original_read(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
        expected = PermissionError
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(conversation_id="conv_attachment")
    session = SimpleNamespace(conversation_repo=repo, session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _: ""))
    manager = SimpleNamespace(get_session=lambda _: session)
    with pytest.raises(expected):
        attachment_preview_payload(session_id="audit", conversation_id=conversation.id, artifact_id="att_bad", ws_manager=manager, attachment_store=store)
    with pytest.raises(expected):
        store.delete_for_conversation(conversation.id)
    assert path.exists()


def test_replay_torn_utf8_tail_uses_degraded_optional_cache_read(tmp_path):
    path = tmp_path / "audit-replay.jsonl"
    first = {"type": "command.result", "event_seq": 4, "conversation_id": "conv_replay", "command": "audit", "message": "kept", "level": "info"}
    raw = (json.dumps(first) + "\n").encode() + b'{"message":"\xe4\xb8'
    path.write_bytes(raw)
    store, events = EventOutbox.load_replay_state(session_id="audit-replay", replay_root=tmp_path, replay_limit=10)
    assert events[0]["message"] == "kept" and events[0]["event_seq"] == 4
    assert store.read_status.degraded and store.read_status.dropped_lines == 1
    assert path.read_bytes() == raw


@pytest.mark.parametrize("url,bypass", [("http://[::1]/", "::1"), ("http://[::1]/", "[::1]"), ("https://example.test/", "example.test:443"), ("https://[::1]/", "[::1]:443")])
def test_http_hook_bypasses_explicit_origin_in_no_proxy(monkeypatch, url, bypass):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("NO_PROXY", bypass)
    assert _proxy_for_url(url) is None
    assert _proxy_for_url("https://unmatched.test/") == "http://proxy.test:3128"


def test_optional_hook_utf16_returns_warning_snapshot_and_preserves_file(tmp_path):
    folder = tmp_path / ".minicode"
    folder.mkdir()
    path = folder / "hooks.json"
    raw = '{"hooks":{}}'.encode("utf-16")
    path.write_bytes(raw)
    requirements = SimpleNamespace(disable_all_hooks=None, allow_managed_hooks_only=None, allowed_http_hook_urls=None,
        http_hook_allowed_env_vars=None, restricts_customization_to_plugins=lambda _: False, value_for=lambda _: None, source_for=lambda _: None)
    layer = SimpleNamespace(config={}, source=SimpleNamespace(kind="user", file=str(folder / "config.toml"), project_config_folder="", source_id="audit"))
    stack = SimpleNamespace(startup_warnings=(), requirements=requirements, effective_config=lambda: {}, get_layers=lambda: [layer], fingerprint="audit")
    result = discover_hook_snapshot(config_stack=stack, workspace_root=tmp_path, workspace_trusted=True, plugin_sources=[])
    assert any("Failed to read hook settings" in warning and "hooks.json" in warning for warning in result.warnings)
    assert path.read_bytes() == raw
    path.write_text('{"hooks":{}}', encoding="utf-8")
    control = discover_hook_snapshot(config_stack=stack, workspace_root=tmp_path, workspace_trusted=True, plugin_sources=[])
    assert not control.warnings


def _terminal_response(event_type="response.completed"):
    response = {"id": "resp-audit", "status": "completed", "end_turn": True, "usage": {"input_tokens": 1, "output_tokens": 1}, "output": [
        {"type": "message", "id": "msg-audit", "role": "assistant", "status": "completed", "phase": "final_answer",
         "content": [{"type": "output_text", "text": "answer", "annotations": []}]},
    ]}
    if event_type == "response.incomplete":
        response.update(status="incomplete", incomplete_details={"reason": "interrupted"})
    return {"type": event_type, "response": response}


def _sse_response(frames):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_interrupted_responses_keeps_native_output_and_continuation_boundary():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _sse_response([_terminal_response("response.incomplete")]))) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", base_url="https://fixture.invalid/v1", wire_api="responses", proxy_mode="direct"), http_client=client)
        try:
            events = [event async for event in adapter.stream_chat([LLMMessage("user", "Continue")])]
        finally:
            await adapter.aclose()
    assert not any(event.type is StreamEventType.ERROR for event in events)
    done = events[-1]
    assert done.type is StreamEventType.DONE and done.end_turn is False
    assert done.raw["event_type"] == "response.incomplete"
    assert done.provider_items[0]["type"] == "message"
    assert "answer" in json.dumps(done.provider_items)


@pytest.mark.asyncio
async def test_registered_developer_update_reaches_all_provider_wires_and_chat_cache(tmp_path):
    marker = "REGISTERED_DEVELOPER_UNITS"

    def factory(api):
        def context(event, _context):
            event["messages"].append({"role": "developer", "content": marker})
            return {"messages": event["messages"]}
        api.on("context", context)

    loader = ExtensionLoader(cwd=tmp_path, trust_policy=ExtensionTrustPolicy(cwd=tmp_path, project_root=tmp_path), use_cache=False)
    loaded = await loader.load_factory(factory, extension_path="<audit-known-inline-factory>")
    assert not loaded.errors
    messages = _coerce_extension_context_messages(await loaded.runner.emit_context([
        LLMMessage("system", "Stable instructions " * 1000), LLMMessage("user", "Work", is_user_input=True),
    ]))
    try:
        for api in ("responses", "chat"):
            captured = []

            def reply(request):
                captured.append(json.loads(request.content))
                return _sse_response([_terminal_response()]) if api == "responses" else _sse_response([{"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}])

            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                adapter = OpenAIAdapter(LLMSettings(provider="openai", api_key="fixture", model="gpt-5.6", base_url="https://api.openai.com/v1", wire_api=api, proxy_mode="direct"), http_client=client)
                try:
                    events = [event async for event in adapter.stream_chat(messages, metadata={"session_id": "audit-context"})]
                finally:
                    await adapter.aclose()
            assert events[-1].type is StreamEventType.DONE
            wire = captured[0]["input" if api == "responses" else "messages"]
            assert wire[-1]["role"] == "developer" and marker in json.dumps(wire[-1])
        system, _ = AnthropicAdapter._convert_messages(messages)
        assert marker in system
    finally:
        await loaded.runner.shutdown("audit-complete")
        loader.clear_cache()


@pytest.mark.asyncio
async def test_chat_auxiliary_error_retains_authoritative_retry_after():
    reply = httpx.Response(429, headers={"Retry-After": "37"}, json={"error": {"message": "Rate limit", "type": "rate_limit_error"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", base_url="https://fixture.invalid/v1", wire_api="chat", proxy_mode="direct"), http_client=client)
        try:
            with pytest.raises(RuntimeError) as error:
                await adapter._simple_chat_completions([LLMMessage("user", "Query")])
        finally:
            await adapter.aclose()
    assert retry_after_seconds(error.value, default=None) == 37
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_anthropic_ping_advances_actual_provider_event_waiter():
    class PingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"message_start","message":{"id":"msg-audit","role":"assistant","content":[],"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
            for _ in range(4):
                await asyncio.sleep(0.02)
                yield b'data: {"type":"ping"}\n\n'
            yield b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\ndata: {"type":"message_stop"}\n\n'

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=PingStream()))) as client:
        adapter = AnthropicAdapter(api_key="fixture", model="fixture", base_url="https://fixture.invalid", proxy_mode="direct")
        adapter._http_client = client
        stream = adapter.stream_chat([LLMMessage("user", "Work")])
        owners = set()
        try:
            first = await wait_for_provider_event(stream, timeout_seconds=0.15, cancel_event=None, owner=owners)
            assert first.type is StreamEventType.USAGE
            pings = [await wait_for_provider_event(stream, timeout_seconds=0.06, cancel_event=None, owner=owners) for _ in range(4)]
            assert all(event.type is StreamEventType.TRANSPORT_ACTIVITY for event in pings)
            remaining = [event async for event in stream]
            assert remaining[-1].type is StreamEventType.DONE and not owners
        finally:
            await stream.aclose()
            await adapter.aclose()
