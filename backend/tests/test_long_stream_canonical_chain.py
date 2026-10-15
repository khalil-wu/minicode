"""Canonical text through the actual SDK, turn reducer, WS projection and disk."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.loop_process_events import model_process_text_event
from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.provider_text_projection import project_provider_text_chunk
from backend.agent.state import AgentState
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.stream_sanitizer import ThinkingStreamSanitizer
from backend.agent.turn_state import AgentTurnState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, TokenBudget
from backend.config_helpers import LLMSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.hooks.manager import HookEvent, HookManager, HookResult
from backend.permissions.context import PermissionContext
from backend.sdk import query
from backend.tools.file_tools import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import _project_agent_message_event
from backend.ws.event_outbox import EventOutbox
from backend.ws.stream_state import apply_stream_event, create_stream_state, get_stream_content_blocks


CHUNK = "Canonical chunk with real text.\n"
FIRST = "FIRST_VISIBLE."
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6H8sAAAAASUVORK5CYII="


class LongStreamModel(LLMAdapter):
    def __init__(self, settings, count: int, tail: str, image_only: bool):
        self._settings = settings
        self._provider_id = "openai"
        self.count, self.tail, self.image_only = count, tail, image_only
        self.calls = 0
        self.read_result_seen = False

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        if self.calls == 1:
            for _ in range(self.count):
                yield StreamEvent(StreamEventType.TEXT_CHUNK, content=CHUNK, phase="commentary", item_id="process-item")
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="read-marker", name="read_file", arguments={"file_path": "marker.txt"},
            )])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
            return
        self.read_result_seen = any("OWNED_READ_MARKER" in message.content for message in messages)
        if self.image_only:
            yield StreamEvent(StreamEventType.IMAGE_CHUNK, image_data=PNG, image_media_type="image/png", item_id="image-result")
        else:
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content=FIRST, phase="final_answer", item_id="first-answer")
            for index in range(self.count):
                yield StreamEvent(StreamEventType.TEXT_CHUNK, content=CHUNK, phase="final_answer", item_id="last-answer", content_index=index % 2)
                if index % 31 == 0:
                    yield StreamEvent(StreamEventType.TEXT_CHUNK, content="", phase="final_answer", item_id="last-answer", lifecycle="end")
            if self.tail:
                yield StreamEvent(StreamEventType.TEXT_CHUNK, content=self.tail, phase="final_answer", item_id="tail-answer")
        yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True,
            usage=UsageInfo(input_tokens=100, output_tokens=self.count * 8 + 4))

    async def simple_chat(self, messages, *, max_tokens=None):
        return "Controlled transport does not need a side query."


class CapturedSocket:
    def __init__(self, *, record_payloads=False):
        self.messages = 0
        self.wire_bytes = 0
        self.max_payload_bytes = 0
        self.saw_private_chunks = False
        self.payloads = [] if record_payloads else None

    async def send_json(self, payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.messages += 1
        self.wire_bytes += len(encoded)
        self.max_payload_bytes = max(self.max_payload_bytes, len(encoded))
        self.saw_private_chunks |= b'"_text_parts"' in encoded
        if self.payloads is not None:
            self.payloads.append(payload)


class CapturedStop(HookManager):
    """Observe the actual harness Stop call; no user hook or command executes."""
    def __init__(self, workspace):
        super().__init__(workspace_root=workspace)
        self.answers = []

    def has_hooks(self, event):
        return event == HookEvent.STOP or super().has_hooks(event)

    async def run_stop(self, user_message, draft_reply, **kwargs):
        self.answers.append(draft_reply)
        return HookResult()


async def _run_image_query_through_main(directory, workspace, config, model, stop, artifacts, registry):
    """Use the real main runner for SDK image conversion and durable transcript.

    The SDK intentionally emits image_chunk. Artifact creation and the preview
    event belong to _run_agent_locked, so copying only text reducers is not an
    image harness. This controlled host borrows the existing main-run fixture;
    its query submission seam calls the actual SDK once, with main admission.
    """
    from backend.tests.test_agent_runner_done_fallback import _Session
    from backend.ws.command_handlers import SessionCommandHandlersMixin

    events = []
    session = _Session(workspace, events)
    session.config = config
    session.llm = model
    session.provider = config.llm.provider
    session.selected_model = config.llm.model
    session.available_models = [config.llm.model]
    session.artifact_store = artifacts
    session.tool_registry = registry
    # Unlike the text reducer-only probe, the real query refreshes its live
    # policy through the main host. Give that host the same admitted policy.
    session.permission_context_for_conversation = lambda conversation, source="agent.run": session.permission_checker.build_context(
        mode="bypass", sandbox_mode="danger-full-access", source=source,
    )
    session.permission_context = session.permission_context_for_conversation(session.active_conversation)
    session._provider_override_active = False
    session._resolve_models_source = lambda provider, settings=None: "configured"
    session.refresh_llm_selection = SessionCommandHandlersMixin.refresh_llm_selection.__get__(session)
    session.conversation_repo.update_memory_mode(session.active_conversation_id, "disabled")
    socket = CapturedSocket(record_payloads=True)
    outbox = EventOutbox(session_id="controlled-image-main", websocket=socket, replay_root=directory / "image-replay",
        replay_limit=1000, cleanup_tasks=session.cleanup_tasks, has_active_run=lambda: False,
        requires_conversation_owner=lambda kind, payload: False, workspace_scoped_event_types=set())
    session.event_outbox = outbox
    artifacts_seen = []
    sdk_events = []
    captured = {}
    wire_streams = {session.active_conversation_id: create_stream_state(session.active_conversation_id, "assistant_image_main")}

    async def submit(submission):
        captured["state"] = submission.state
        captured["context"] = submission.session.context_builder
        runtime = submission.runtime
        runtime.run_context.cancel_event = runtime.cancel_event
        async for event in query(submission.user_message, llm=model, state=submission.state,
                config=config, tool_registry=registry, artifact_store=artifacts, workspace_root=runtime.workspace_root,
                context_builder=submission.session.context_builder, run_context=runtime.run_context,
                permission_context=runtime.permission_context, max_iterations=3, metadata=runtime.metadata,
                session_id=runtime.session_id, code_store=submission.session.code_store):
            sdk_events.append(event)
            yield event

    async def send_payload(payload, *, log_context="", **kwargs):
        events.append(dict(payload))
        if payload.get("type") == "artifact.preview":
            artifacts_seen.append(dict(payload))
        apply_stream_event(session._conversation_streams, session.active_conversation_id, payload["type"], payload)
        apply_stream_event(wire_streams, session.active_conversation_id, payload["type"], payload)
        await outbox.send_payload(payload, log_context=log_context, wait_for_delivery=False)

    async def send_event(event):
        await send_payload(event.to_ws_message(), log_context=event.type)

    session.query_engine = SimpleNamespace(submit=submit)
    session.send_event = send_event
    session.send_payload = send_payload
    run_context = RunContext(hook_manager=stop)
    wall, cpu = time.perf_counter(), time.process_time()
    with pytest.MonkeyPatch.context() as host_patch:
        host_patch.setattr("backend.config.load_config", lambda cwd=None, **kwargs: config)
        # This controlled adapter belongs to the foreground image query only.
        host_patch.setattr("backend.memory.generation.schedule_memory_startup", lambda **kwargs: None)
        host_patch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None, **kwargs: config)
        host_patch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda host, **kwargs: model)
        try:
            await session._run_agent_locked(
                "Read marker.txt and return the generated image without answer text.",
                conversation_id=session.active_conversation_id,
                metadata={"assistant_message_id": "assistant_image_main", "user_message_id": "user_image_main"},
                run_context=run_context,
            )
            await outbox.drain_delivery()
            await outbox.drain_persistence()
        finally:
            session.run_manager.close_durable_queue()
            await artifacts.flush()
    cold = ConversationRepository(session.conversation_repo._base_dir).get_conversation(session.active_conversation_id)
    assert cold is not None
    assistant = next(message for message in reversed(cold.transcript) if message["role"] == "assistant")
    snapshot = SimpleNamespace(content=assistant["content"], blocks=assistant["blocks"])
    return {"state": captured["state"], "model": model, "context": captured["context"], "stop": stop,
        "snapshot": snapshot, "ws_blocks": get_stream_content_blocks(wire_streams[session.active_conversation_id]),
        "cold": cold, "artifacts": artifacts_seen, "artifact_store": artifacts, "socket": socket,
        "sdk_events": sdk_events, "main_events": events,
        "measurements": {"wall_seconds": time.perf_counter() - wall, "process_cpu_seconds": time.process_time() - cpu,
            "upstream": "controlled adapter, no real provider/token billing",
            "surface": "actual SDK query -> main runner image validation/store/preview/transcript -> WS/outbox -> cold store"}}


async def run_long_query(directory: Path, *, count: int, tail: str = "", image_only: bool = False, live: bool = True):
    """Synthetic upstream, real harness/IO/reducers/outbox; no real provider or GUI."""
    directory.mkdir(parents=True, exist_ok=True)
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "marker.txt").write_text("OWNED_READ_MARKER", encoding="utf-8")
    repo = ConversationRepository(directory / "conversations")
    conversation = repo.create_conversation(workspace_root=str(workspace))
    state = AgentState(user_message="Read marker.txt and return the complete text output.")
    state.conversation_id = conversation.id
    settings = LLMSettings(api_key="offline-fixture", provider="openai", model="controlled-long-stream", base_url="https://fixture.invalid/v1", auth_header=False)
    agent = AgentSettings(live_text_streaming=live)
    config = AppConfig(llm=settings, agent=agent, token_budget=TokenBudget(total=1_000_000))
    model = LongStreamModel(settings, count, tail, image_only)
    context = ContextBuilder(token_budget=config.token_budget, agent_settings=agent, llm=model,
        workspace_root=workspace, conversation_id=conversation.id)
    stop = CapturedStop(workspace)
    run_context = RunContext(hook_manager=stop)
    artifacts = ArtifactStore(storage_dir=directory / "artifacts")
    registry = ToolRegistry()
    registry.register(ReadFileTool(artifacts))
    if image_only:
        return await _run_image_query_through_main(directory, workspace, config, model, stop, artifacts, registry)
    main = AgentTurnState(now_ms=lambda: int(time.time() * 1000))
    message_id, turn_id = "assistant_probe", "turn_probe"
    streams = {conversation.id: create_stream_state(conversation.id, message_id, turn_id)}
    socket = CapturedSocket()
    outbox = EventOutbox(session_id="controlled-long-stream", websocket=socket, replay_root=directory / "replay",
        replay_limit=1000, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda kind, payload: False, workspace_scoped_event_types=set())
    terminal = "failed"
    snapshots = 0
    artifacts_seen = []
    heartbeat_delays = []
    stop_heartbeat = asyncio.Event()
    async def heartbeat():
        target = time.perf_counter() + 0.01
        while not stop_heartbeat.is_set():
            await asyncio.sleep(max(0, target - time.perf_counter()))
            now = time.perf_counter()
            heartbeat_delays.append(max(0, now - target))
            target = now + 0.01
    ticker = asyncio.create_task(heartbeat())
    wall, cpu = time.perf_counter(), time.process_time()
    last_snapshot = wall
    first_byte_ms = None
    try:
        async for event in query(state.user_message, llm=model, state=state, config=config,
                tool_registry=registry, artifact_store=artifacts, workspace_root=workspace,
                context_builder=context, run_context=run_context,
                permission_context=PermissionContext(mode="bypass", approval_policy="never", sandbox_mode="danger-full-access", workspace_root=workspace),
                max_iterations=3, metadata={"conversation_id": conversation.id, "assistant_message_id": message_id, "turn_id": turn_id}):
            # Match the real handler boundary: SDK-only provider frames never
            # enter UI or its reconnect snapshot.
            if event.type == "stream_event" and event.data.get("sdk_only", True):
                continue
            data = {**event.data, "conversation_id": conversation.id, "message_id": message_id, "turn_id": turn_id}
            if event.type in {"item.started", "agent_message.delta", "item.completed"}:
                _project_agent_message_event(main, event.type, data)
            elif event.type in {"thinking", "thinking_delta"}:
                main.append_thinking(str(data.get("content") or ""), data)
            elif event.type == "agent.item":
                main.record_process_item(data)
            elif event.type == "agent.item.delta":
                main.record_process_delta(data)
            elif event.type == "tool_call":
                main.record_tool_call(data)
            elif event.type == "tool_result":
                main.record_tool_result(data)
            elif event.type == "artifact.preview":
                artifacts_seen.append(data)
            elif event.type == "done":
                terminal = data.get("status", "failed")
                main.record_done(data)
            elif event.type == "error":
                main.record_error(data)
            if first_byte_ms is None and event.type in {"agent_message.delta", "agent.item", "artifact.preview"}:
                first_byte_ms = (time.perf_counter() - wall) * 1000
            apply_stream_event(streams, conversation.id, event.type, data)
            await outbox.send_payload({"type": event.type, **data}, log_context="controlled-long-stream", wait_for_delivery=False)
            if time.perf_counter() - last_snapshot >= 0.25:
                json.dumps(get_stream_content_blocks(streams[conversation.id]))
                main.finalize(terminal_status="partial")
                snapshots += 1
                last_snapshot = time.perf_counter()
        await outbox.drain_delivery()
        await outbox.drain_persistence()
    finally:
        stop_heartbeat.set()
        await ticker
    elapsed, cpu_seconds = time.perf_counter() - wall, time.process_time() - cpu
    snapshot = main.finalize(terminal_status=terminal)
    ws_blocks = get_stream_content_blocks(streams[conversation.id])
    conversation.transcript = [{"id": message_id, "role": "assistant", "content": snapshot.content,
        "blocks": snapshot.blocks, "terminal_status": terminal}]
    conversation.context_snapshot = context.export_snapshot()
    repo.save_conversation(conversation)
    cold = ConversationRepository(directory / "conversations").get_conversation(conversation.id)
    return {"state": state, "model": model, "context": context, "stop": stop,
        "snapshot": snapshot, "ws_blocks": ws_blocks, "cold": cold,
        "artifacts": artifacts_seen, "artifact_store": artifacts, "socket": socket,
        "measurements": {"chunks_per_phase": count, "chunk_chars": len(CHUNK), "wall_seconds": elapsed,
            "process_cpu_seconds": cpu_seconds, "first_byte_ms": first_byte_ms, "periodic_snapshots": snapshots,
            "wire_bytes": socket.wire_bytes, "wire_messages": socket.messages, "max_payload_bytes": socket.max_payload_bytes,
            "event_loop_max_delay_ms": max(heartbeat_delays, default=0) * 1000,
            "upstream": "controlled adapter, no real provider/token billing", "surface": "source SDK+IO+turn+WS+outbox, no packaged GUI"}}


@pytest.fixture
def isolated_policy(monkeypatch):
    from backend import managed_settings
    monkeypatch.setattr(managed_settings, "_load_admin_platform_settings", lambda: managed_settings.ManagedSettingsResult({}))
    monkeypatch.setattr(managed_settings, "_load_windows_registry_settings", lambda hive: managed_settings.ManagedSettingsResult({}))


@pytest.mark.parametrize("tail", ["", " \n\t", "<thinking>HIDDEN_ONLY_ITEM</thinking>"])
@pytest.mark.parametrize("live", [True, False])
def test_real_query_last_visible_item_matches_stop_main_ws_and_cold_history(tmp_path, isolated_policy, tail, live):
    result = asyncio.run(run_long_query(tmp_path / "case", count=64, tail=tail, live=live))
    expected = CHUNK * 64
    assert result["model"].calls == 2 and result["model"].read_result_seen
    assert result["state"].reply == expected
    assert result["stop"].answers == [expected]
    assert result["context"]._history[-1].content == expected
    assert result["snapshot"].content == result["cold"].transcript[0]["content"] == expected
    visible = [block for block in result["snapshot"].blocks if block.get("type") == "text" and block.get("content", "").strip()]
    assert [block["content"] for block in visible] == [FIRST, expected]
    process = next(block for block in result["snapshot"].blocks if block["type"] == "process")
    assert process["content"] == (CHUNK * 64).strip()
    assert not result["socket"].saw_private_chunks
    assert "_text_parts" not in json.dumps(result["ws_blocks"])
    assert "HIDDEN_ONLY_ITEM" not in json.dumps(result["cold"].transcript)


def test_real_image_only_result_keeps_artifact_and_has_no_fabricated_text(tmp_path, isolated_policy):
    result = asyncio.run(run_long_query(tmp_path / "case", count=4, image_only=True))
    assert result["model"].read_result_seen and result["artifacts"]
    assert sum(event.type == "image_chunk" for event in result["sdk_events"]) == 1
    assert not any(payload["type"] == "image_chunk" for payload in result["socket"].payloads)
    assert [event["status"] for event in result["main_events"] if event["type"] == "done"] == ["completed"]
    assistant = next(message for message in reversed(result["cold"].transcript) if message["role"] == "assistant")
    assert result["state"].reply == result["snapshot"].content == assistant["content"] == ""
    assert [message["role"] for message in result["cold"].transcript] == ["user", "assistant"]
    assert result["stop"].answers == [""]
    identifier = result["artifacts"][0]["artifact_id"]
    assert result["artifact_store"].get(identifier, conversation_id=result["cold"].id, workspace_root=result["cold"].workspace_root)
    assert [artifact["artifactId"] for artifact in assistant["artifacts"]] == [identifier]
    restored = ArtifactStore(storage_dir=result["artifact_store"]._storage_dir)
    assert restored.get(identifier, conversation_id=result["cold"].id, workspace_root=result["cold"].workspace_root) == PNG


@pytest.mark.parametrize("phase", ["final_answer", ""])
def test_cancel_keeps_exact_streamed_item_bytes_and_retry_discards_old_view(phase):
    async def run():
        text, stream, sanitizer = StreamTextState(iteration_id="cancel"), StreamAttemptState(), ThinkingStreamSanitizer()
        main = AgentTurnState(now_ms=lambda: 1)
        streams = {"conv_cancel_case": create_stream_state("conv_cancel_case", "message", "turn")}
        for index in range(1000):
            event = StreamEvent(StreamEventType.TEXT_CHUNK, content=CHUNK, phase=phase, item_id="actual-item")
            async for projected in project_provider_text_chunk(event, stream_state=stream, stream_text=text,
                    visible_text_sanitizer=sanitizer, provider_raw_final_text={}, live_text_streaming=True,
                    awaiting_trailing_done=False, process_event_factory=model_process_text_event):
                if hasattr(projected, "type"):
                    _project_agent_message_event(main, projected.type, projected.data)
                    apply_stream_event(streams, "conv_cancel_case", projected.type, projected.data)
        cancelled = text.cancel_active_agent_message()
        _project_agent_message_event(main, cancelled.type, cancelled.data)
        apply_stream_event(streams, "conv_cancel_case", cancelled.type, cancelled.data)
        assert cancelled.data["item"]["text"] == CHUNK * 1000
        assert main.finalize(terminal_status="cancelled").blocks[0]["content"] == CHUNK * 1000
        assert get_stream_content_blocks(streams["conv_cancel_case"])[0]["content"] == CHUNK * 1000
        text.reset_for_provider_retry()
        assert not text.full_text and not text.final_candidate_text and not text.pending_unphased_text
        assert not text.emitted_agent_messages
    asyncio.run(run())


def test_process_cursor_preserves_split_whitespace_without_resending_prefix():
    state = StreamTextState(iteration_id="spaces")
    output = []
    for piece in [" ", "first", " ", "\n", "second ", " "]:
        state.append_process_text(piece)
        event = state.maybe_stream_process_text(source="commentary", event_factory=model_process_text_event)
        if event is not None:
            output.append(event.data.get("delta", event.data.get("content", "")))
    assert "".join(output) == "first \nsecond"
    complete = state.flush_pending_process_text(None, source=None, event_factory=model_process_text_event)
    assert complete.data["content"] == "first \nsecond"


def test_main_and_reconnect_complete_snapshot_replaces_buffer_authoritatively():
    state = AgentTurnState(now_ms=lambda: 1)
    streams = {"conv_process_case": create_stream_state("conv_process_case", "message")}
    start = {"id": "process", "item_id": "process", "kind": "process_text", "content": "first", "status": "running"}
    state.record_process_item(start)
    apply_stream_event(streams, "conv_process_case", "agent.item", start)
    for _ in range(1000):
        delta = {"item_id": "process", "delta": " more"}
        state.record_process_delta(delta)
        apply_stream_event(streams, "conv_process_case", "agent.item.delta", delta)
    assert get_stream_content_blocks(streams["conv_process_case"])[0]["content"] == "first" + " more" * 1000
    complete = {**start, "content": "authoritative replacement", "status": "completed"}
    state.record_process_item(complete)
    apply_stream_event(streams, "conv_process_case", "agent.item", complete)
    assert state.finalize(terminal_status="completed").blocks[0]["content"] == "authoritative replacement"
    assert get_stream_content_blocks(streams["conv_process_case"])[0]["content"] == "authoritative replacement"
