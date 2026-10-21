"""The voice model tool shares the live thread owner, discovery, and stop path."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json

import pytest

from backend.agent.message import UserCommand
from backend.agent.run_context import RunContext
from backend.agent.tool_execution import run_tool, toolset_policy_guard_reason
from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.config import AppConfig, LLMSettings
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.context import PermissionContext, ToolCallSource, ToolExecutionContext
from backend.realtime.protocol import Start
from backend.realtime.runtime import RealtimeRuntime
from backend.realtime.session import thread_runtime
from backend.realtime.transport import RealtimeConfig
from backend.tests.test_realtime_source_chain import peer, until
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.tools.realtime_tools import EndRealtimeVoiceCallTool, active_realtime_service
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import ToolSearchTool
from backend.tools.toolsets import ToolsetPolicy
from backend.ws.execution_context import ConversationThreadRuntime
from backend.ws.manager import WebSocketManager


def names(schemas):
    return {item["function"]["name"] for item in schemas}


@asynccontextmanager
async def owned_call():
    async with peer() as remote:
        committed, emitted = [], []
        async def commit(item): committed.append(item)
        async def emit(event): emitted.append(event)
        async def no_delegation(*_args): raise AssertionError("No model admission requested")
        service = RealtimeRuntime(RealtimeConfig(remote.base_url, {}), Start(threadId="original", outputModality="audio"),
            emit=emit, commit=commit, delegate=no_delegation, record_tail=no_delegation,
            track_admission=lambda _task: None, startup_context="", active_changed=lambda: None)
        owner = ConversationThreadRuntime(store_identity="original-store", conversation_id="original", realtime_runtime=service)
        registry = ToolRegistry()
        registry.register(EndRealtimeVoiceCallTool())
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), conversation_id="original",
            run_context=RunContext(environment_thread_runtime=owner), tool_registry=registry,
            metadata={"conversation_store_id": "original-store"})
        service.launch()
        try:
            await until(lambda: service.started)
            context.metadata["_toolset_policy"] = replace(ToolsetPolicy.default(), realtime_service=service)
            yield service, context, committed, emitted
        finally:
            await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["no_run", "no_owner", "foreign_thread", "foreign_store", "subagent", "not_started", "closing"])
async def test_voice_tool_rejects_nonowning_or_inactive_context_before_side_effects(boundary):
    async with owned_call() as (service, context, _committed, emitted):
        if boundary == "no_run": context.run_context = None
        elif boundary == "no_owner": context.run_context.environment_thread_runtime = None
        elif boundary == "foreign_thread": context.conversation_id = "other-thread"
        elif boundary == "foreign_store": context.metadata["conversation_store_id"] = "other-store"
        elif boundary == "subagent": context.run_context.is_subagent = True
        elif boundary == "not_started": service.started = False
        elif boundary == "closing": service.closing = True
        call = ToolCallEvent(id="not-owned", name="end_realtime_voice_call", arguments={})
        assert active_realtime_service(context) is None
        assert toolset_policy_guard_reason(call, context.tool_registry, context)
        result = await EndRealtimeVoiceCallTool().execute({}, context)
        assert result.is_error and service.active
        assert not any(event["type"] == "thread/realtime/closed" for event in emitted)


@pytest.mark.asyncio
async def test_same_discovery_instance_expires_after_real_close_and_never_enters_nested_surface():
    async with owned_call() as (service, context, committed, emitted):
        registry = context.tool_registry
        search = ToolSearchTool(registry)
        registry.register(search)
        policy = effective_toolset_policy(base_policy=None, tool_registry=registry, disabled_tools=set(),
            requires_explicit_workspace=False, workspace_root=None, permission_mode="bypass",
            supports_search_tool=True, realtime_service=active_realtime_service(context))
        context.metadata["_toolset_policy"] = policy
        assert names(registry.get_schemas(toolset_policy=policy)) == {"tool_search"}
        found = await search.execute({"query": "end voice chat"}, context)
        assert [tool["name"] for namespace in found.tool_search_output for tool in namespace["tools"]] == ["end_realtime_voice_call"]
        before = search._catalog
        context.tool_call_id = "nested"
        context.call_sources["nested"] = ToolCallSource(kind="code_mode")
        assert json.loads((await search.execute({"query": "end voice chat"}, context)).content) == {"tools": []}
        assert not names(registry.get_schemas(toolset_policy=replace(policy, nested_surface=True))) & {"end_realtime_voice_call"}
        assert "realtime_service" not in policy.to_mapping()
        child = effective_toolset_policy(base_policy=policy, tool_registry=registry, disabled_tools=set(),
            requires_explicit_workspace=False, workspace_root=None, permission_mode="bypass",
            realtime_service=service, is_subagent=True)
        assert not child.is_available(registry.get_tool_spec("end_realtime_voice_call"))
        context.tool_call_id = "direct"
        old_key = policy.cache_key()
        await service.aclose()
        assert policy.cache_key() != old_key
        assert (await search.execute({"query": "end voice chat"}, context)).tool_search_output == []
        assert search._catalog is not before
        assert "end_realtime_voice_call" not in names(registry.get_schemas(toolset_policy=policy))
        assert committed[-1]["type"] == "realtimeSessionClosed"
        assert emitted[-1]["type"] == "thread/realtime/closed"
        assert (await EndRealtimeVoiceCallTool().execute({})).is_error


@pytest.mark.asyncio
async def test_captured_tool_request_cannot_close_replacement_voice_call():
    async with owned_call() as (old, context, _committed, _emitted):
        async with owned_call() as (replacement, _new_context, _new_committed, new_emitted):
            context.run_context.environment_thread_runtime.realtime_runtime = replacement
            assert active_realtime_service(context) is replacement
            result = await run_tool(ToolCallEvent(id="stale-sampled", name="end_realtime_voice_call", arguments={}),
                context.tool_registry, context)
            assert result.is_error and old.active and replacement.active
            assert "original task" in result.content
            assert not any(event["type"] == "thread/realtime/closed" for event in new_emitted)


class VoiceToolModel(LLMAdapter):
    model = "isolated-voice-tool-model"

    def __init__(self, settings):
        self._settings = settings
        self.requests = []
        self.received, self.release = asyncio.Event(), asyncio.Event()

    async def stream_chat(self, messages, tools=None, metadata=None):
        current = names(tools or [])
        self.requests.append((messages, current))
        if len(self.requests) == 1:
            self.received.set()
            await self.release.wait()
        if self._settings.supports_search_tool and len(self.requests) == 1:
            call = ToolCallEvent(id="discover-end", name="tool_search", arguments={"query": "end voice chat"})
        elif len(self.requests) == (2 if self._settings.supports_search_tool else 1):
            call = ToolCallEvent(id="actual-end", name="end_realtime_voice_call", arguments={})
        else:
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="The voice chat ended.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)
            return
        yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[call])
        yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")

    async def simple_chat(self, messages, *, max_tokens=None): return "unused"


@pytest.mark.asyncio
@pytest.mark.parametrize("search_enabled", [False, True])
async def test_actual_model_tool_roundtrip_closes_original_voice_and_preserves_tail_and_backing_work(tmp_path, monkeypatch, search_enabled):
    from backend.realtime import session as module
    from backend.ws import agent_runner

    async with peer() as remote:
        config = AppConfig(llm=LLMSettings(api_key="", provider="custom", base_url=remote.base_url,
            model=VoiceToolModel.model, wire_api="chat", tool_mode="direct", supports_search_tool=search_enabled))
        model = VoiceToolModel(config.llm)
        monkeypatch.setattr(module, "load_config", lambda **_: config)
        monkeypatch.setattr(agent_runner, "load_config", lambda **_: config)
        monkeypatch.setattr(agent_runner, "_get_or_create_session_llm", lambda *_args, **_kwargs: model)
        connection = _connection(tmp_path, "voice-tool-model")
        connection.update(config=config, llm=model)
        connection["tool_registry"].register(EndRealtimeVoiceCallTool())
        connection["tool_registry"].register(ToolSearchTool(connection["tool_registry"]))
        manager = WebSocketManager()
        session, _ = await manager.connect(**connection)
        monkeypatch.setattr(session, "_model_runtime_for_conversation", lambda _owner: None)
        monkeypatch.setattr(session, "_resolve_available_models", lambda *_args: [])
        monkeypatch.setattr(session, "_resolve_llm_provider", lambda *_args: "custom")
        monkeypatch.setattr(session, "_conversation_tool_registry", lambda *_args, **_kwargs: session.tool_registry)
        first = session.conversation_repo.create_conversation(workspace_root=str(tmp_path), permission_mode="bypass", memory_mode="disabled")
        second = session.conversation_repo.create_conversation(workspace_root=str(tmp_path), permission_mode="bypass")
        session.active_conversation_id = first.id
        monkeypatch.setattr(module, "capture_config", lambda *_args, **_kwargs:
            RealtimeConfig(remote.base_url, {"Authorization": "Bearer isolated-realtime"}, configured=True))
        try:
            await session.command_dispatcher._dispatch_client_command(UserCommand(type="thread/realtime/start", data={
                "threadId": first.id, "conversation_id": first.id, "outputModality": "audio", "flushTranscriptTailOnSessionEnd": True}), session.connection_generation)
            owner = thread_runtime(session, first.id)
            await until(lambda: owner.realtime_runtime.started)
            session.active_conversation_id = second.id
            await remote.socket.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "End this voice chat"}))
            await remote.socket.send(json.dumps({"type": "conversation.item.done", "item": {"type": "function_call", "id": "end-call", "call_id": "end-handoff",
                "name": "background_agent", "arguments": '{"prompt":"End this voice chat"}'}}))
            await asyncio.wait_for(model.received.wait(), 30)
            await remote.socket.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "End this voice chat now"}))
            await until(lambda: any(item["text"] == "End this voice chat now" for item in owner.realtime_runtime.transcript_tail))
            model.release.set()
            await until(lambda: session.running_agent_task_for(first.id) is None, seconds=30)
            assert len(model.requests) == (3 if search_enabled else 2)
            if search_enabled:
                assert "tool_search" in model.requests[0][1] and "end_realtime_voice_call" not in model.requests[0][1]
            end_request = model.requests[1 if search_enabled else 0]
            assert "end_realtime_voice_call" in end_request[1]
            assert any("End this voice chat" in message.content for message in end_request[0])
            final_messages, final_tools = model.requests[-1]
            assert "end_realtime_voice_call" not in final_tools
            assert any('call_id="actual-end"' in message.content and '"ended": true' in message.content for message in final_messages)
            assert any(message.role == "developer" and "Realtime conversation ended." in message.content for message in final_messages)
            assert not owner.realtime_runtime.active and not owner.realtime_runtime.context_active
            assert owner.realtime_runtime.task.done()
            assert session.active_conversation_id == second.id
            assert session.conversation_repo.get_conversation(second.id).transcript == []
            cold = ConversationRepository(session.conversation_repo._base_dir).get_conversation(first.id)
            assert any(message["content"] == "The voice chat ended." for message in cold.transcript)
            tail = next(message for message in cold.context_snapshot["history"] if message.get("content_kind") == "realtime_conversation.delegation")
            assert "End this voice chat now" in tail["content"] and tail["private_model_only"]
            assert not any("<realtime_delegation>" in message["content"] for message in cold.transcript)
            assert any(block["item"]["type"] == "realtimeSessionClosed" for message in cold.transcript for block in message.get("blocks", []) if block["type"] == "realtime")
        finally:
            model.release.set()
            await _release_session(session)
