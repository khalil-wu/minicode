from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.guardian.host import GuardianHostState
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.tool_execution import _apply_pre_tool_hook, store_result
from backend.config import AppConfig, LLMSettings
from backend.hooks.manager import HookResult
from backend.llm.base import ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry


@contextmanager
def classification_endpoint(payloads):
    records = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            records.append({"path": self.path, "headers": dict(self.headers),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            body = "".join(f"data: {json.dumps(payload)}\n\n" for payload in payloads).encode() + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", records
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.asyncio
async def test_actual_classifier_http_retains_the_toolless_source_body_headers_and_exact_first_delta():
    events = [
        {"type": "response.created", "response": {"id": "classification"}},
        {"type": "response.output_text.delta", "delta": " LOW"},
        {"type": "response.output_text.delta", "delta": "low"},
        {"type": "response.completed", "response": {"id": "classification", "usage": {"input_tokens": 17, "output_tokens": 3}}},
    ]
    request = {"model": "gpt-5.6-luna", "input": [
        {"type": "additional_tools", "role": "developer", "tools": []},
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "classifier Source policy"}]},
        {"type": "compaction", "encrypted_content": "opaque-parent-fixture"},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "actual admitted input"}]},
    ], "tool_choice": "none", "parallel_tool_calls": False, "reasoning": {"effort": "low", "context": "all_turns"},
        "store": False, "stream": True, "include": [], "prompt_cache_key": "guardian-v2:owner",
        "client_metadata": {"parent_turn_id": "parent-turn", "root_turn_id": "root-turn", "turn_id": "classifier-turn"}}
    original = deepcopy(request)
    with classification_endpoint(events) as (base_url, records):
        adapter = OpenAIAdapter(LLMSettings(provider="custom", base_url=base_url, api_key="controlled-fixture", model="main-model",
            wire_api="chat", max_tokens=123))
        try:
            actual = [event async for event in adapter.guardian_classification_events(request, headers={
                "session_id": "rollout", "thread-id": "classifier", "x-openai-subagent": "guardian",
                "x-codex-window-id": "classifier:0", "x-openai-internal-codex-responses-lite": "true"})]
        finally:
            await adapter.aclose()
    assert actual == events
    assert actual[1]["delta"] == " LOW"
    assert request == original
    assert records[0]["path"] == "/v1/responses"
    assert records[0]["body"] == original
    assert "max_output_tokens" not in records[0]["body"]
    assert "instructions" not in records[0]["body"]
    assert "text" not in records[0]["body"]
    received_headers = {name.lower(): value for name, value in records[0]["headers"].items()}
    assert received_headers["authorization"] == "Bearer controlled-fixture"
    assert received_headers["x-codex-window-id"] == "classifier:0"
    assert "x-codex-guardian" not in received_headers


@pytest.mark.asyncio
async def test_actual_raw_classifier_preserves_provider_failure_and_cancellation_instead_of_classifying_them():
    failure = {"type": "response.failed", "response": {"error": {"code": "real_error", "message": "provider failure"}}}
    with classification_endpoint([failure]) as (base_url, records):
        adapter = OpenAIAdapter(LLMSettings(provider="custom", base_url=base_url, api_key="", model="parent"))
        try:
            assert [event async for event in adapter.guardian_classification_events({"model": "gpt-5.6-luna", "stream": True}, headers={})] == [failure]
            cancel = asyncio.Event()
            cancel.set()
            with pytest.raises(asyncio.CancelledError):
                async for _ in adapter.guardian_classification_events({"model": "gpt-5.6-luna", "stream": True}, headers={}, cancel_event=cancel):
                    pytest.fail("A cancelled raw request cannot yield classification evidence")
        finally:
            await adapter.aclose()
    assert len(records) == 1


@pytest.mark.asyncio
async def test_async_observer_sees_actual_post_hook_arguments_and_canonical_failed_result_finishes_that_call(tmp_path):
    observer = SimpleNamespace(on_tool_start=AsyncMock(), on_tool_finish=Mock())
    hook = SimpleNamespace(run_pre_tool=AsyncMock(return_value=HookResult(updated_input={"command": "echo changed"})))
    owner = RunContext(hook_manager=hook, guardian_async_service=observer)
    registry = ToolRegistry()
    tool_context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path,
        run_context=owner, tool_registry=registry)
    call = ToolCallEvent(id="source-observed", name="run_command", arguments={"command": "echo original", "tty": True})
    assert await _apply_pre_tool_hook(call, tool_context) is None
    observer.on_tool_start.assert_awaited_once_with(call, tool_context, registry)
    assert call.arguments == {"command": "echo changed", "tty": True}
    state = AgentState(user_message="requested command")
    builder = ContextBuilder()
    result = store_result(call, ToolResult("actual refusal", is_error=True, status="blocked"), builder, state,
        tool_ctx=tool_context, tool_registry=registry)
    observer.on_tool_finish.assert_called_once_with(call.id)
    assert result.type == "tool_result"
    assert result.data["status"] == "blocked"
    assert "actual refusal" in result.data["summary"]


@pytest.mark.asyncio
async def test_hook_refusal_does_not_create_an_async_start_for_a_tool_that_was_not_started(tmp_path):
    observer = SimpleNamespace(on_tool_start=AsyncMock(), on_tool_finish=Mock())
    hook = SimpleNamespace(run_pre_tool=AsyncMock(return_value=HookResult(blocked=True, message="real hook refusal")))
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path,
        run_context=RunContext(hook_manager=hook, guardian_async_service=observer))
    result = await _apply_pre_tool_hook(ToolCallEvent(id="not-started", name="run_command", arguments={"command": "echo original"}), context)
    assert result.is_error
    observer.on_tool_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_async_source_tracks_actual_rebound_model_and_live_permission_and_closes_owned_tasks(tmp_path):
    adapter = OpenAIAdapter(LLMSettings(provider="custom", base_url="http://127.0.0.1:1/v1", api_key="", model="parent"))
    host = GuardianHostState("owner")
    first = ModelExecutionSnapshot.capture(AppConfig(llm=adapter._settings), adapter)
    permission = PermissionContext(mode="confirm", workspace_root=tmp_path)
    owner = RunContext(model_execution=first, permission_context_provider=lambda: permission)
    context = ToolExecutionContext(permission, workspace_root=tmp_path, model_execution=first, run_context=owner)
    builder = ContextBuilder(llm=adapter)
    builder.bind_tool_context(context)
    host.bind(builder)
    observer_close = AsyncMock()
    pool_close = AsyncMock()
    host.async_service.aclose = observer_close
    host.service.pool.aclose = pool_close
    try:
        assert host.current_tool_context().model_execution is first
        second = replace(first, model="different-next-model")
        owner.model_execution = second
        permission = replace(permission, mode="bypass")
        current = host.current_tool_context()
        assert current.model_execution is second
        assert current.permission.mode == "bypass"
        assert context.permission.mode == "confirm"
        assert context.model_execution is first
        await host.aclose()
        observer_close.assert_awaited_once()
        pool_close.assert_awaited_once()
    finally:
        await adapter.aclose()
