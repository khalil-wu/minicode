from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import WebSocketDisconnect

from backend import main
from backend.agent.runtime import default_runtime
from backend.config import LLMSettings
from backend.llm.capabilities import capabilities_for_adapter, capabilities_from_settings
from backend.tests.test_ws_cold_connection import RecordingSocket


class ProtocolSocket(RecordingSocket):
    def __init__(self, session_id):
        super().__init__(session_id)
        self.query_params["protocol"] = "control_v1"
        self.commands = asyncio.Queue()
        self.events = asyncio.Queue()

    async def receive_text(self):
        command = await self.commands.get()
        if command is None:
            raise WebSocketDisconnect(code=1000)
        return json.dumps(command)

    async def send_json(self, payload):
        await super().send_json(payload)
        await self.events.put(payload)

    async def until(self, event_type, **fields):
        for _ in range(100):
            payload = await asyncio.wait_for(self.events.get(), 5)
            if payload.get("type") == event_type and all(payload.get(key) == value for key, value in fields.items()):
                return payload
        raise AssertionError(f"No {event_type} event with {fields}; actual wire: {self.sent}")

    async def command(self, command_type, **data):
        await self.commands.put({"type": command_type, **data})

    async def finish(self, task):
        await self.commands.put(None)
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", ["chat", "responses", "anthropic"])
async def test_declared_capabilities_match_actual_wire_adapter_before_sampling(wire_api):
    from backend.services.llm_adapter_factory import build_wire_adapter
    settings = LLMSettings(api_key="offline", provider="custom", model="declared-model", wire_api=wire_api,
        base_url="https://protocol.invalid/v1", reasoning_effort="medium", reasoning_effort_levels=("low", "medium", "high"),
        input_modalities=("text", "image"), parallel_tool_calls=False, native_compaction=False,
        supports_hosted_web_search=True, context_window=64000, max_context_window=128000, max_output_tokens=8000)
    declared = capabilities_from_settings(settings, provider="custom").to_dict()
    adapter = build_wire_adapter(settings)
    if wire_api == "anthropic":
        from backend.llm.provider_contracts import ProviderAdapterSpec
        adapter.provider_adapter_spec = ProviderAdapterSpec(provider_id="custom", model_id=settings.model,
            api="anthropic-messages", api_key="offline", base_url=settings.base_url, headers={}, auth_header=False,
            max_tokens=8000, max_output_tokens=settings.max_output_tokens, context_window=settings.context_window,
            max_context_window=settings.max_context_window, supports_hosted_web_search=True, parallel_tool_calls=False)
    try:
        actual = capabilities_for_adapter(adapter).to_dict()
        for key in ("model", "wire_api", "reasoning_effort_supported", "reasoning_effort_levels", "configured_reasoning_effort",
                    "effective_reasoning_effort", "vision", "parallel_tool_calls", "native_compaction", "supports_hosted_web_search",
                    "context_window", "max_context_window", "max_output_tokens", "streaming", "tool_calling"):
            assert declared[key] == actual[key], key
    finally:
        await adapter.aclose()


@pytest.fixture
def protocol_environment(monkeypatch):
    monkeypatch.delenv("MINICODE_RUNTIME_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_AVAILABLE_MODELS", raising=False)
    monkeypatch.setattr(main, "_is_websocket_authorized", lambda _: True)
    monkeypatch.setattr(main, "_websocket_origin_allowed", lambda _: True)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_model", ["", "gpt-6.1-sol"])
async def test_protocol_start_restore_catalogs_and_files_do_not_bind_a_model(
    tmp_path, monkeypatch, protocol_environment, selected_model,
):
    if selected_model:
        import backend.config_helpers as config_helpers
        config_helpers.SETTINGS_FILE.write_text(json.dumps({"llm": {
            "provider": "custom", "custom": {"model": selected_model,
                "base_url": "https://protocol.invalid/v1", "wire_api": "responses",
                "reasoning_effort": "medium", "available_models": [selected_model],
                "models_source": "live", "model_metadata": {selected_model: {
                    "source": "provider", "supports_hosted_web_search": True,
                    "parallel_tool_calls": False, "input": ["text", "image"], "native_compaction": False,
                }}}}}), encoding="utf-8")
    adapter_calls = []

    def model_forbidden(*args, **kwargs):
        adapter_calls.append(kwargs)
        raise AssertionError("This operation does not sample or bind a model")

    monkeypatch.setattr(main, "_create_session_llm", model_forbidden)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", model_forbidden)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Preserved file content", encoding="utf-8")
    trust_file = tmp_path / "trusted_workspaces.json"
    trust_file.write_text(json.dumps({"version": 1, "roots": [str(workspace)]}), encoding="utf-8")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    socket = ProtocolSocket("session_unbound_start")

    async with main.lifespan(main.app):
        task = asyncio.create_task(main.websocket_endpoint(socket))
        try:
            model_state = await socket.until("llm.model.updated")
            assert model_state["current_model"] == selected_model
            session = main._state.ws_manager.get_session(socket.query_params["session_id"])
            assert session.is_connected
            assert session.llm is None and session.context_builder._llm is None
            assert session._llm_adapter_cache == {}
            if not selected_model:
                capabilities = session._provider_capabilities_payload()
                assert capabilities["model"] == "" and capabilities["reasoning_effort_supported"] is False
                assert capabilities["reasoning_effort_levels"] == []
            saved = session.conversation_repo.create_conversation(
                title="Existing task",
                model_selection={"provider": model_state["provider"], "model": selected_model,
                    "reasoning_effort": "medium"} if selected_model else {},
            )
            await socket.command("session.restore", last_seq=0, last_conversation_id=saved.id)
            restored = await socket.until("session.restored")
            assert restored["current_model"] == selected_model
            assert session.active_conversation_id == saved.id
            if selected_model:
                assert model_state["configured_reasoning_effort"] == model_state["effective_reasoning_effort"] == "medium"
                assert model_state["reasoning_effort_levels"] == ["low", "medium", "high", "xhigh", "max", "ultra"]
                capabilities = session.runtime_capability_snapshot()["provider_capabilities"]
                assert capabilities["model"] == selected_model and capabilities["reasoning_effort_supported"] is True
                assert capabilities["configured_reasoning_effort"] == capabilities["effective_reasoning_effort"] == "medium"
                assert capabilities["reasoning_effort_levels"] == model_state["reasoning_effort_levels"]
                assert capabilities["vision"] is True and capabilities["parallel_tool_calls"] is False
                assert capabilities["native_compaction"] is False and capabilities["supports_hosted_web_search"] is True
            await socket.command("commands.list")
            catalog = await socket.until("commands.list")
            assert any(entry["command"] == "review" for entry in catalog["commands"])
            extension = session._extension_runtime_states[saved.id]
            refresh_task = extension.get("model_refresh_task")
            if refresh_task is not None:
                await asyncio.wait_for(asyncio.shield(refresh_task), 5)
            await socket.command("skills.list")
            skills = await socket.until("skills.list")
            assert {entry["name"] for entry in skills["skills"]} >= {"browser", "code-review", "verify"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                settings = await client.get("/api/llm/settings")
                assert settings.status_code == 200
                file = await client.get("/api/workspace/file", params={"workspace_root": str(workspace), "path": "README.md"})
                assert file.status_code == 200 and file.json()["content"] == "Preserved file content"
            await socket.command("ping")
            await socket.until("pong")
            assert session.llm is None and session._llm_adapter_cache == {}
            if selected_model:
                capabilities = session._provider_capabilities_payload()
                assert capabilities["model"] == selected_model and capabilities["effective_reasoning_effort"] == "medium"
                assert capabilities["reasoning_effort_supported"] is True
                assert capabilities["vision"] is True and capabilities["parallel_tool_calls"] is False
            assert adapter_calls == []
            assert not any(event["type"] == "error" for event in socket.sent)
        finally:
            await socket.finish(task)
        reconnect = ProtocolSocket(socket.query_params["session_id"])
        task = asyncio.create_task(main.websocket_endpoint(reconnect))
        try:
            await reconnect.until("llm.model.updated")
            assert main._state.ws_manager.get_session(reconnect.query_params["session_id"]) is session
            assert session.connection_generation == 2 and session.llm is None
            assert adapter_calls == []
        finally:
            await reconnect.finish(task)


@pytest.mark.asyncio
async def test_empty_model_settings_sync_and_real_query_rejection_keep_protocol_connected(
    monkeypatch, protocol_environment,
):
    adapter_calls = []

    def model_forbidden(*args, **kwargs):
        adapter_calls.append(kwargs)
        raise AssertionError("An empty model selection must never construct an adapter")

    monkeypatch.setattr(main, "_create_session_llm", model_forbidden)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", model_forbidden)
    socket = ProtocolSocket("session_empty_query")
    async with main.lifespan(main.app):
        task = asyncio.create_task(main.websocket_endpoint(socket))
        try:
            await socket.until("llm.model.updated")
            await socket.command("llm.config.set", provider="openai", model="")
            await socket.until("runtime.capabilities", source="llm.config.set")
            await socket.command("conversation.create", activate=True, title="Unconfigured task")
            switched = await socket.until("conversation.switched")
            conversation_id = switched["conversation_id"]
            await socket.command("user_message", content="Inspect the project", conversation_id=conversation_id,
                client_command_id="empty-model-query", user_message_id="empty-user", assistant_message_id="empty-assistant")
            failure = await socket.until("error", conversation_id=conversation_id, provider_error_type="model")
            assert "provider=model" in failure["message"]
            done = await socket.until("done", conversation_id=conversation_id)
            assert done["status"] == "failed" and done["reason"] == "llm_initialization_failed"
            session = main._state.ws_manager.get_session(socket.query_params["session_id"])
            assert session.llm is None and session._llm_adapter_cache == {}
            terminal = default_runtime().latest_main_run(conversation_id)
            assert terminal.status == "failed" and terminal.terminal_reason == "llm_initialization_failed"
            assert terminal.error == failure["message"]
            await socket.command("ping")
            await socket.until("pong")
            assert session.is_connected and socket.closed == [] and adapter_calls == []
        finally:
            await socket.finish(task)


@pytest.mark.asyncio
async def test_first_real_query_creates_the_selected_adapter_and_runs_native_response(
    monkeypatch, protocol_environment,
):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fixture")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://protocol.invalid/v1")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    adapters, requests = [], []

    async def respond(request):
        requests.append(json.loads(request.content))
        output = [{"type": "message", "id": "native-answer", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "The requested result is ready.", "annotations": []}]}]
        event = {"type": "response.completed", "response": {"id": "native-response", "status": "completed",
            "end_turn": True, "output": output, "usage": {"input_tokens": 20, "output_tokens": 6}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(event) + "\n\n")

    def build(config, *, model_override=None, provider_override=None, model_runtime=None):
        from backend.services.llm_adapter_factory import build_provider_adapter
        adapter = build_provider_adapter(provider_override, model_override, model_runtime=model_runtime)
        adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(main, "_create_session_llm", build)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", build)
    socket = ProtocolSocket("session_first_query")
    async with main.lifespan(main.app):
        task = asyncio.create_task(main.websocket_endpoint(socket))
        try:
            await socket.until("llm.model.updated")
            assert adapters == []
            await socket.command("conversation.create", activate=True, title="First selected query")
            switched = await socket.until("conversation.switched")
            conversation_id = switched["conversation_id"]
            assert adapters == []
            await socket.command("commands.list")
            await socket.until("commands.list")
            session = main._state.ws_manager.get_session(socket.query_params["session_id"])
            refresh_task = session._extension_runtime_states[conversation_id].get("model_refresh_task")
            if refresh_task is not None:
                await asyncio.wait_for(asyncio.shield(refresh_task), 5)
            before = session._provider_capabilities_payload()
            assert before["model"] == "gpt-6.1-sol" and before["effective_reasoning_effort"] == "medium"
            assert before["reasoning_effort_supported"] is True and adapters == []
            await socket.command("user_message", content="Return the requested result", conversation_id=conversation_id,
                client_command_id="first-selected-query", user_message_id="first-user", assistant_message_id="first-assistant")
            done = await socket.until("done", conversation_id=conversation_id)
            assert done["status"] == "completed", [event for event in socket.sent if event["type"] == "error"]
            session = main._state.ws_manager.get_session(socket.query_params["session_id"])
            assert len(adapters) == 1 and session.llm is adapters[0]
            assert len(requests) == 1 and requests[0]["model"] == "gpt-6.1-sol"
            after = session._provider_capabilities_payload()
            for key in ("model", "wire_api", "reasoning_effort_supported", "reasoning_effort_levels", "reasoning_effort_wire_map",
                        "configured_reasoning_effort", "effective_reasoning_effort", "vision", "parallel_tool_calls", "native_compaction",
                        "supports_hosted_web_search", "context_window"):
                assert before[key] == after[key], key
            assert session.conversation_repo.get_conversation(conversation_id).transcript[-1]["content"] == "The requested result is ready."
            await socket.command("ping")
            await socket.until("pong")
            assert session.is_connected and not any(event["type"] == "error" for event in socket.sent)
        finally:
            await socket.finish(task)
