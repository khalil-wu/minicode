"""Public Source realtime protocol over actual loopback WS and thread owners."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.asyncio.server import serve

from backend.agent.message import UserCommand
from backend.config import AppConfig, LLMSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.llm.provider_contracts import ProviderAdapterSpec
from backend.realtime.protocol import AudioChunk, Start
from backend.realtime.runtime import Delegation, RealtimeRuntime
from backend.realtime.session import capture_config, start_runtime, thread_runtime
from backend.realtime.transport import RealtimeConfig, RealtimeConnection, session_config, websocket_url
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.ws.manager import WebSocketManager
from backend.ws.payload_contracts import is_non_replayable_event_type


async def until(predicate, seconds=10):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def peer(version="v2"):
    state = SimpleNamespace(request=None, messages=[], socket=None, connected=asyncio.Event())
    async def receive(socket):
        state.socket, state.request = socket, socket.request
        state.connected.set()
        if version == "v3":
            initial = json.loads(await socket.recv())
            state.messages.append(initial)
            await socket.send(json.dumps({"type": "session.started", "session": {"id": "actual-upstream-session"}}))
        async for message in socket:
            value = json.loads(message)
            state.messages.append(value)
            if value["type"] == "session.close":
                await socket.close()
    async with serve(receive, "127.0.0.1", 0) as server:
        state.base_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
        yield state


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
async def test_real_transport_keeps_version_headers_session_and_utf8_wire(version):
    async with peer(version) as remote:
        params = Start(threadId="thread-original", outputModality="audio", version=version,
            includeStartupContext=False, prompt="explicit startup")
        connection = RealtimeConnection(RealtimeConfig(remote.base_url, {"Authorization": "Bearer isolated-key"}, version=version), params, "instructions")
        await connection.open()
        try:
            await connection.audio(AudioChunk(data="AAAA", sampleRate=24000, numChannels=1, samplesPerChannel=1))
            await connection.text("中文" * 300, "user")
            await until(lambda: len(remote.messages) >= (6 if version == "v3" else 3))
            parsed = urlsplit(remote.request.path)
            assert parsed.path == ("/v1/live" if version == "v3" else "/v1/realtime")
            assert parse_qs(parsed.query)["model"] == (["gpt-live-1-codex"] if version == "v3" else ["gpt-realtime-1.5"])
            assert remote.request.headers["authorization"] == "Bearer isolated-key"
            assert remote.request.headers["x-session-id"] == "thread-original"
            if version in {"v1", "v3"}:
                assert remote.request.headers["openai-alpha"] == ("quicksilver=v2" if version == "v3" else "quicksilver=v1")
            assert remote.messages[0]["type"] == "session.update"
            assert remote.messages[1] == {"type": "input_audio.append" if version == "v3" else "input_audio_buffer.append", "audio": "AAAA"}
            if version == "v3":
                chunks = [value["content"][0]["text"] for value in remote.messages[2:]]
                assert "".join(chunks) == "中文" * 300
                assert all(len(text.encode()) <= 500 for text in chunks)
            else:
                assert remote.messages[2]["item"]["content"][0]["text"] == ("[USER] " if version == "v2" else "") + "中文" * 300
                assert not any(value["type"] == "response.create" for value in remote.messages)
        finally:
            await connection.close()


def test_source_option_gates_prompt_presence_and_transport_capability():
    config = RealtimeConfig("http://127.0.0.1:1/v1", {}, voice="marin", prompt="configured prompt")
    assert config.capabilities()["transports"] == ["websocket"]
    assert replace(config, version="v3").capabilities()["transports"] == ["websocket", "webrtc", "existingCall"]
    assert replace(config, version="v3", session_type="transcription").capabilities()["transports"] == ["websocket", "existingCall"]
    assert "headers" not in config.capabilities()
    for version, transport in (("v2", {"type": "webrtc", "sdp": "offer"}), ("v2", {"type": "existingCall", "callId": "rtc_owned"})):
        with pytest.raises(ValueError, match="v1 or v3"):
            RealtimeConnection(config, Start(threadId="t", outputModality="audio", version=version, transport=transport, includeStartupContext=False), "")
    with pytest.raises(ValueError, match="configuration options"):
        RealtimeConnection(config, Start(threadId="t", outputModality="audio", version="v1",
            transport={"type": "existingCall", "callId": "rtc_owned"}, includeStartupContext=False, prompt=None), "")
    accepted = RealtimeConnection(config, Start(threadId="t", outputModality="audio",
        transport={"type": "webrtc", "sdp": "offer"}), "")
    assert accepted.version == "v1" and accepted.session["audio"]["output"]["voice"] == "cove"
    from backend.realtime.prompts import backend_prompt
    assert backend_prompt(Start(threadId="t", outputModality="audio", prompt=None), config) == "configured prompt"
    assert backend_prompt(Start(threadId="t", outputModality="audio", prompt=None), replace(config, prompt="")) == ""
    assert backend_prompt(Start(threadId="t", outputModality="audio"), replace(config, prompt="")).startswith("## Identity, tone, and role")
    normalized = Start.model_validate({"threadId": "t", "outputModality": "audio", "transport": None, "clientManagedHandoffs": None})
    assert normalized.transport.type == "websocket" and normalized.client_managed_handoffs is False


def test_provider_capture_uses_original_selection_auth_proxy_and_never_publishes_secrets(monkeypatch):
    from backend.realtime import session as module
    config = AppConfig(llm=LLMSettings(api_key="base-provider-secret", provider="base", base_url="https://base.example/v1", wire_api="anthropic"))
    spec = ProviderAdapterSpec(provider_id="original-provider", model_id="original-model", api="openai-responses",
        api_key="original-provider-secret", base_url="https://original.example/v1", headers={"x-runtime-auth": "actual-header-secret"},
        auth_header=True, proxy_mode="disabled", max_tokens=0)
    resolved = []
    def resolve(provider, model): resolved.append((provider, model)); return spec
    provider_reads = []
    runtime = SimpleNamespace(get_provider=lambda provider: provider_reads.append(provider) or SimpleNamespace(configured=True),
        get_available_snapshot=lambda: (SimpleNamespace(provider="original-provider"),),
        get_model=lambda provider, model: SimpleNamespace(api="openai-responses", base_url="https://original.example/v1"), resolve_adapter_spec=resolve)
    session = SimpleNamespace(conversation_repo=SimpleNamespace(get_conversation_summary=lambda _owner: SimpleNamespace(
        model_selection={"provider": "original-provider", "model": "original-model"})),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _conversation: "original-workspace"),
        _model_runtime_for_conversation=lambda _owner: runtime)
    monkeypatch.setattr(module, "load_config", lambda **_kwargs: config)
    public = capture_config(session, "original-thread", credentials=False).capabilities()
    assert public["configured"] and not resolved and not provider_reads
    assert not any(secret in json.dumps(public) for secret in ("base-provider-secret", "original-provider-secret", "actual-header-secret"))
    captured = capture_config(session, "original-thread", credentials=True)
    assert provider_reads == ["original-provider"]
    assert resolved == [("original-provider", "original-model")]
    assert captured.base_url == "https://original.example/v1" and captured.proxy_mode == "disabled"
    assert captured.headers == {"x-runtime-auth": "actual-header-secret", "Authorization": "Bearer original-provider-secret"}


@pytest.mark.asyncio
async def test_existing_call_joins_original_sideband_without_changing_session_configuration():
    async with peer() as remote:
        connection = RealtimeConnection(RealtimeConfig(remote.base_url, {}, version="v3", voice="marin", session_type="transcription"),
            Start(threadId="original", outputModality="audio", version="v3", includeStartupContext=False,
                transport={"type": "existingCall", "callId": "rtc_original"}), "unused")
        await connection.open()
        try:
            assert urlsplit(remote.request.path).path == "/v1/live/rtc_original"
            assert not remote.messages
            await connection.text("original thread context", "developer")
            await until(lambda: len(remote.messages) == 1)
            assert remote.messages[0] == {"type": "session.context.append", "content": [{"type": "input_text", "text": "original thread context"}]}
        finally:
            await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["v1", "v3"])
async def test_actual_webrtc_http_offer_and_original_call_sideband(version):
    requests = []
    async def http_peer(reader, writer):
        raw_headers = await reader.readuntil(b"\r\n\r\n")
        header_lines = raw_headers.decode().split("\r\n")
        headers = {name.lower(): value.strip() for name, value in (line.split(":", 1) for line in header_lines[1:] if ":" in line)}
        body = await reader.readexactly(int(headers["content-length"]))
        requests.append((header_lines[0], headers, body))
        answer = b"v=0\r\nactual-loopback-answer"
        writer.write(b"HTTP/1.1 201 Created\r\nLocation: /v1/realtime/calls/rtc_actual\r\nContent-Type: application/sdp\r\nContent-Length: "
            + str(len(answer)).encode() + b"\r\nConnection: close\r\n\r\n" + answer)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    async with peer() as remote:
        server = await asyncio.start_server(http_peer, "127.0.0.1", 0)
        async with server:
            base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
            params = Start(threadId="original-thread", outputModality="audio", version=version,
                transport={"type": "webrtc", "sdp": "v=0\r\nactual-offer"})
            config = RealtimeConfig(remote.base_url, {"Authorization": "Bearer original-call-auth"}, version=version, call_base_url=base)
            connection = RealtimeConnection(config, params, "actual session instructions")
            await connection.open()
            try:
                assert connection.sdp == "v=0\r\nactual-loopback-answer"
                method, headers, body = requests[0]
                assert method == f"POST /v1/{'live' if version == 'v3' else 'realtime/calls'} HTTP/1.1"
                assert headers["authorization"] == "Bearer original-call-auth"
                assert b'actual-offer' in body and b'actual session instructions' in body
                assert b'name="sdp"' in body and b'name="session"' in body
                parsed = urlsplit(remote.request.path)
                if version == "v3":
                    assert parsed.path == "/v1/live/rtc_actual" and not remote.messages
                else:
                    assert parse_qs(parsed.query)["call_id"] == ["rtc_actual"]
                    await until(lambda: bool(remote.messages))
                    assert remote.messages[0]["type"] == "session.update"
            finally:
                await connection.close()


@pytest.mark.asyncio
async def test_v3_response_item_prefix_bem_channel_and_speakable_chunk_bounds():
    async with peer("v3") as remote:
        params = Start(threadId="A", outputModality="audio", version="v3", codexResponsesAsItems=True,
            codexResponseItemPrefix="Original executor", codexResponseHandoffMode="bemTags",
            codexResponseHandoffChannelPrefixes={"final": ["<final>"]})
        connection = RealtimeConnection(RealtimeConfig(remote.base_url, {}, version="v3"), params, "")
        await connection.open()
        try:
            await connection.handoff("delegation-A", "<final>" + "中文" * 900, final=True)
            await until(lambda: len(remote.messages) > 1)
            values = remote.messages[1:]
            assert all(value["type"] == "session.context.append" and value["channel"] == "speakable" for value in values)
            texts = [value["content"][0]["text"] for value in values]
            assert "".join(texts).startswith("Original executor\n\n<final>")
            assert all(len(text.encode()) <= 500 for text in texts)
            assert len("".join(texts).encode()) <= 4000
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_v2_real_function_item_admission_output_and_response_queue():
    async with peer() as remote:
        committed, emitted, admissions = [], [], []
        async def emit(payload): emitted.append(payload)
        async def commit(item): committed.append(item)
        async def delegate(prompt, record): admissions.append((prompt, record))
        async def record_tail(text, identity): committed.append({"tail": text, "identity": identity})
        service = RealtimeRuntime(RealtimeConfig(remote.base_url, {}), Start(threadId="A", outputModality="audio"),
            emit=emit, commit=commit, delegate=delegate, record_tail=record_tail, track_admission=lambda _task: None,
            startup_context="", active_changed=lambda: None)
        service.launch()
        await until(lambda: service.started)
        try:
            await remote.socket.send(json.dumps({"type": "conversation.item.done", "item": {"type": "function_call", "id": "call-item",
                "call_id": "call-real", "name": "background_agent", "arguments": '{"prompt":"Read original file"}'}}))
            await until(lambda: len(admissions) == 1)
            prompt, record = admissions[0]
            assert prompt == "Read original file" and record.identity == "call-real"
            await service.observe_backend_event("item.completed", {"conversation_id": "A", "message_id": record.assistant_message_id, "turn_id": "real-turn",
                "item": {"id": "process", "type": "agent_message", "source": "commentary", "text": "reading"}})
            assert not record.final_sent
            await service._receive({"type": "response.created", "response": {"id": "voice-answer"}})
            await service.observe_backend_event("item.completed", {"conversation_id": "A", "message_id": record.assistant_message_id, "turn_id": "real-turn",
                "item": {"id": "actual-final", "type": "agent_message", "source": "model_final", "text": "File actually read"}})
            assert not record.final_sent
            await service.observe_backend_event("done", {"conversation_id": "A", "message_id": record.assistant_message_id, "turn_id": "real-turn", "status": "completed"})
            await until(lambda: any(value.get("item", {}).get("type") == "function_call_output" for value in remote.messages))
            assert not any(value["type"] == "response.create" for value in remote.messages)
            await service._receive({"type": "response.done", "response": {"id": "voice-answer"}})
            await until(lambda: any(value["type"] == "response.create" for value in remote.messages))
            assert any(value.get("item", {}).get("content", [{}])[0].get("text") == "[BACKEND] File actually read" for value in remote.messages)
            promoted = next(item for item in committed if item["type"] == "bemItemPromoted")
            assert (promoted["turnId"], promoted["itemId"]) == ("real-turn", "actual-final")
            assert emitted[-1]["type"] == "thread/realtime/item/completed"
            assert not any("audio" in item for item in committed)
        finally:
            await service.aclose()


class FinalModel(LLMAdapter):
    model = "isolated-script-model"
    def __init__(self):
        self.inputs = []
        self.requests = []
        self.owner = None
        self.gate = None
    async def stream_chat(self, messages, tools=None, metadata=None):
        self.requests.append((metadata, messages))
        # The same provider also serves independently owned memory work. Only
        # this actual thread's requests belong to the voice query gate/count.
        if metadata["session_id"] == self.owner:
            self.inputs.append(messages)
            if self.gate is not None:
                await self.gate.wait()
        yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Actual backing query completed.", phase="final_answer")
        yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)
    async def simple_chat(self, messages, *, max_tokens=None): return "unused"


@pytest.mark.asyncio
async def test_actual_thread_owner_query_timeline_cold_read_and_stop_after_switch(tmp_path, monkeypatch):
    from backend.realtime import session as module
    from backend.ws import agent_runner
    from backend.conversations.repository import ConversationRepository
    async with peer() as remote:
        config = AppConfig(llm=LLMSettings(api_key="", provider="custom", base_url=remote.base_url,
            model="isolated-script-model", wire_api="chat"))
        model = FinalModel()
        monkeypatch.setattr(module, "load_config", lambda **_: config)
        monkeypatch.setattr(agent_runner, "load_config", lambda **_: config)
        monkeypatch.setattr(agent_runner, "_get_or_create_session_llm", lambda *_args, **_kwargs: model)
        connection = _connection(tmp_path, "realtime-real-owner")
        connection.update(config=config, llm=model)
        manager = WebSocketManager()
        session, _ = await manager.connect(**connection)
        monkeypatch.setattr(session, "_model_runtime_for_conversation", lambda _owner: None)
        monkeypatch.setattr(session, "_resolve_available_models", lambda *_args: [])
        monkeypatch.setattr(session, "_resolve_llm_provider", lambda *_args: "custom")
        monkeypatch.setattr(session, "_conversation_tool_registry", lambda *_args, **_kwargs: session.tool_registry)
        first = session.conversation_repo.create_conversation(workspace_root=str(tmp_path), permission_mode="bypass", memory_mode="disabled")
        second = session.conversation_repo.create_conversation(workspace_root=str(tmp_path), permission_mode="bypass", memory_mode="disabled")
        model.owner = first.id
        session.active_conversation_id = first.id
        captured = RealtimeConfig(remote.base_url, {"Authorization": "Bearer isolated-realtime"}, configured=True)
        monkeypatch.setattr(module, "capture_config", lambda *_args, **_kwargs: captured)
        try:
            await session.command_dispatcher._dispatch_client_command(UserCommand(type="thread/realtime/start", data={
                "threadId": first.id, "conversation_id": first.id, "outputModality": "audio", "flushTranscriptTailOnSessionEnd": True,
                "client_command_id": "live-start"}), session.connection_generation)
            owner = thread_runtime(session, first.id)
            await until(lambda: owner.realtime_runtime.started)
            session.active_conversation_id = second.id
            await remote.socket.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Run the original task"}))
            await remote.socket.send(json.dumps({"type": "conversation.item.done", "item": {"type": "function_call", "id": "original-call",
                "call_id": "original-handoff", "name": "background_agent", "arguments": '{"prompt":"Run the original task"}'}}))
            await until(lambda: any(value.get("item", {}).get("type") == "function_call_output" for value in remote.messages), seconds=30)
            assert len(model.inputs) == 1
            assert any(message.role == "developer" and "Realtime conversation started." in message.content for message in model.inputs[0])
            assert any("Run the original task" in message.content for message in model.inputs[0])
            assert session.active_conversation_id == second.id
            assert session.conversation_repo.get_conversation(second.id).transcript == []
            original_service = owner.realtime_runtime
            manager.disconnect(session.session_id, connection_generation=session.connection_generation)
            await remote.socket.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "speech while the renderer was disconnected"}))
            await until(lambda: original_service.last_user_item_id != original_service.last_handoff_user_item_id)
            reconnect = _connection(tmp_path / "reconnect", "realtime-real-owner")
            restored, _ = await manager.connect(**reconnect)
            assert restored is session and owner.realtime_runtime is original_service and original_service.active
            await until(lambda: session.running_agent_task_for(first.id) is None)
            model.gate = asyncio.Event()
            await remote.socket.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Second voice request"}))
            await remote.socket.send(json.dumps({"type": "conversation.item.done", "item": {"type": "function_call", "id": "second-call",
                "call_id": "second-handoff", "name": "background_agent", "arguments": '{"prompt":"Second voice request"}'}}))
            await until(lambda: len(model.inputs) == 2, seconds=30)
            await remote.socket.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Additional unsubmitted user speech"}))
            await until(lambda: original_service.last_user_item_id != original_service.last_handoff_user_item_id)
            await session.command_dispatcher._dispatch_client_command(UserCommand(type="thread/realtime/appendAudio", data={
                "threadId": first.id, "conversation_id": first.id, "audio": {"data": "AAAA", "sampleRate": 24000, "numChannels": 1}}), session.connection_generation)
            async with asyncio.timeout(5):
                await session.command_dispatcher._dispatch_client_command(UserCommand(type="thread/realtime/stop", data={
                    "threadId": first.id, "conversation_id": first.id, "client_command_id": "live-stop"}), session.connection_generation)
            assert not owner.realtime_runtime.active and owner.realtime_runtime.task.done()
            assert session.running_agent_task_for(first.id) is not None
            model.gate.set()
            await until(lambda: session.running_agent_task_for(first.id) is None, seconds=30)
            assert len(model.inputs) == 2
            cold = ConversationRepository(session.conversation_repo._base_dir).get_conversation(first.id)
            facts = [block["item"] for message in cold.transcript for block in message.get("blocks", []) if block["type"] == "realtime"]
            assert any(item["type"] == "bemItemPromoted" for item in facts)
            assert facts[-1]["type"] == "realtimeSessionClosed"
            assert sum(message["content"] == "Actual backing query completed." for message in cold.transcript) == 2, cold.transcript
            tail = next(message for message in cold.context_snapshot["history"] if message.get("content_kind") == "realtime_conversation.delegation")
            assert "Additional unsubmitted user speech" in tail["content"]
            assert not tail.get("is_user_input", False) and tail["private_model_only"]
            assert not any("<realtime_delegation>" in message["content"] for message in cold.transcript)
            await session.event_outbox.drain_delivery()
            assert not any(event.get("command") == "thread/realtime/appendAudio" for event in connection["websocket"].sent)
            assert is_non_replayable_event_type("thread/realtime/outputAudio/delta")
            assert not any(command.type.startswith("thread/realtime/") for command in session.run_manager.durable_client_commands.pending_client_commands())
        finally:
            if model.gate is not None:
                model.gate.set()
            await _release_session(session)


def test_late_setup_reuses_durable_private_tail_once_after_cold_history_restore():
    from backend.agent.context import ContextBuilder
    from backend.agent.run_context import RunContext
    from backend.realtime.protocol import transcript_tail_fragment
    identity = "019a17a0-0203-7000-8000-000000000001"
    text = transcript_tail_fragment("assistant: no additional user action\nuser: real final words")
    first = ContextBuilder()
    first.append_realtime_transcript_tail(text, identity)
    cold = ContextBuilder()
    cold.load_snapshot(first.export_snapshot())
    run = RunContext(realtime_history_fragments=[(identity, text)])
    cold._tool_execution_context = SimpleNamespace(run_context=run)
    cold._sync_realtime_world_state()
    messages = [message for message in cold.export_snapshot()["history"] if message.get("content_kind") == "realtime_conversation.delegation"]
    assert len(messages) == 1 and run.realtime_history_fragments == []
    assert messages[0]["role"] == "user" and messages[0]["private_model_only"]
    assert not messages[0].get("is_user_input", False)


@pytest.mark.asyncio
async def test_realtime_end_context_waits_for_terminal_speech_commit_and_never_infers():
    async with peer() as remote:
        entered, persisted = asyncio.Event(), asyncio.Event()
        committed, emitted, admissions = [], [], []
        async def commit(item): committed.append(item)
        async def emit(event): emitted.append(event)
        async def record_tail(text, identity):
            entered.set()
            await persisted.wait()
            committed.append({"tail": text, "identity": identity})
        async def delegate(_prompt, _record): raise AssertionError("A terminal tail must not start inference")
        service = RealtimeRuntime(RealtimeConfig(remote.base_url, {}),
            Start(threadId="original", outputModality="audio", flushTranscriptTailOnSessionEnd=True),
            emit=emit, commit=commit, delegate=delegate, record_tail=record_tail,
            track_admission=admissions.append, startup_context="", active_changed=lambda: None)
        service.launch()
        await until(lambda: service.started)
        await remote.socket.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "actual terminal speech"}))
        await until(lambda: bool(service.transcript_tail))
        closing = asyncio.create_task(service.aclose())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert not service.active and service.context_active
            assert not any(event["type"] == "thread/realtime/closed" for event in emitted)
            persisted.set()
            await closing
            assert not service.context_active and admissions == []
            assert any("actual terminal speech" in item.get("tail", "") for item in committed)
            assert emitted[-1]["type"] == "thread/realtime/closed" and emitted[-1]["reason"] == "requested"
        finally:
            persisted.set()
            await closing
