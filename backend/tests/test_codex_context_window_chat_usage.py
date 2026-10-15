from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.agent.message import UserCommand
from backend.config import LLMSettings
from backend.feature_flags import FeatureFlags
from backend.llm.openai_adapter import OpenAIAdapter
from backend.tests.test_codex_context_window import run_window
from backend.tests.test_codex_context_window_ws import release_window_session
from backend.tests.test_context_fork_activation_chain import _connected_fork_session
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.context_window_tools import GetContextRemainingTool, NewContextTool
from backend.tools.file_tools import ReadFileTool
from backend.tools.registry import ToolRegistry


@contextmanager
def chat_provider(usage_kind):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            body = json.dumps({"data": [{"id": "fixture-chat", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            index = len(requests)
            requests.append(body)
            if index == 0:
                calls = [{"index": 0, "id": "real-chat-vm", "type": "function", "function": {"name": "tool_exec", "arguments": json.dumps({"code": "text(await tools.get_context_remaining({}));", "yield_time_ms": 10000})}}]
                delta, finish = {"tool_calls": calls}, "tool_calls"
            elif index == 1:
                calls = [{"index": 0, "id": "real-chat-window", "type": "function", "function": {"name": "new_context", "arguments": "{}"}}]
                delta, finish = {"tool_calls": calls}, "tool_calls"
            else:
                delta, finish = {"content": "ACTUAL_CHAT_WINDOW_DONE"}, "stop"
            events = [
                {"id": f"real-chat-{index}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": f"real-chat-{index}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            if usage_kind != "absent":
                usage = {"prompt_tokens": 0, "completion_tokens": 0} if usage_kind == "zero" else {"prompt_tokens": 2000, "completion_tokens": 500}
                events.append({"choices": [], "usage": usage})
            encoded = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def adapter_for(url):
    return OpenAIAdapter(LLMSettings(api_key="local-test-key", provider="custom", model="fixture-chat", base_url=url,
        wire_api="chat", context_window=100_000, context_window_source="declared-fixture", context_window_verified=True))


@pytest.mark.parametrize("usage_kind", ["absent", "zero", "normal"])
def test_real_chat_adapter_vm_loop_distinguishes_missing_usage_from_reported_zero(tmp_path, usage_kind):
    async def scenario(url, requests):
        adapter = adapter_for(url)
        state, builder, _, events = await run_window(tmp_path, adapter, code_only=True)
        assert state.terminal_status == "completed", state.stopped_reason
        assert len(requests) == 3
        assert state.reply == "ACTUAL_CHAT_WINDOW_DONE"
        window = builder.export_snapshot()["context_window_state"]
        assert window["prefill_server_observed"] is (usage_kind != "absent")
        if usage_kind == "absent":
            assert window["last_response_total_tokens"] > 0
            assert builder.context_window_status()["tokens_left"] < 90_000
        else:
            assert window["last_response_total_tokens"] == (0 if usage_kind == "zero" else 2_500)
        assert sum(event.type == "item.completed" and event.data["item"]["type"] == "context_compaction" for event in events) == 1
    with chat_provider(usage_kind) as (url, requests):
        asyncio.run(scenario(url, requests))


@pytest.mark.asyncio
async def test_real_chat_nested_vm_ws_has_compaction_and_followup_terminal(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    session.conversation_repo.update_memory_mode(parent.id, "disabled")
    with chat_provider("absent") as (url, requests):
        adapter = adapter_for(url)
        registry = ToolRegistry()
        for tool in (NewContextTool(), GetContextRemainingTool(), ToolExecTool(), ToolWaitTool(), ReadFileTool(session.artifact_store)):
            registry.register(tool)
        session.config = replace(session.config, llm=adapter._settings,
            agent=replace(session.config.agent, code_mode_only=True), feature_flags=FeatureFlags({"token_budget": True}))
        session.conversation_repo.update_model_selection(parent.id, provider="custom", model="fixture-chat")
        monkeypatch.setenv("CUSTOM_AVAILABLE_MODELS", "fixture-chat")
        monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **_kwargs: session.config)
        monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *_args, **_kwargs: adapter)
        monkeypatch.setattr(session, "_build_conversation_tool_registry", lambda *_args, **_kwargs: registry)
        try:
            assert await session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
                "conversation_id": parent.id, "workspace_root": parent.workspace_root,
                "permission_mode": "bypass", "client_command_id": "real-chat-window-command",
                "user_message_id": "real-chat-user", "assistant_message_id": "real-chat-answer",
                "content": "Start and finish the real Chat window fixture.",
            }), session.connection_generation) is True
            running = session.run_manager.running_task_for(parent.id)
            if running is not None:
                await asyncio.wait_for(asyncio.shield(running), 10)
            await session.event_outbox.drain_delivery()
            assert len(requests) == 3
            events = connection["websocket"].sent
            assert not any(event.get("type") == "error" for event in events), json.dumps([event for event in events if event.get("type") == "error"])
            assert any(event.get("type") == "item.completed" and event["item"]["type"] == "context_compaction" for event in events)
            assert any(event.get("type") == "item.completed" and event["item"].get("text") == "ACTUAL_CHAT_WINDOW_DONE" for event in events)
            assert any(event.get("type") == "done" for event in events)
            assert session.conversation_repo.get_conversation(parent.id).transcript[-1]["content"] == "ACTUAL_CHAT_WINDOW_DONE"
        finally:
            await release_window_session(session, parent.id)
