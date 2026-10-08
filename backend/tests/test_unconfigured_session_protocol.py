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
async def test_archive_publishes_fallback_without_waiting_for_provider_history(
    monkeypatch, protocol_environment,
):
    import threading

    socket = ProtocolSocket("session_archive_paged_fallback")
    release_history = threading.Event()
    async with main.lifespan(main.app):
        task = asyncio.create_task(main.websocket_endpoint(socket))
        try:
            await socket.until("llm.model.updated")
            session = main._state.ws_manager.get_session(socket.query_params["session_id"])
            archived = session.conversation_repo.create_conversation(title="Archive me")
            fallback = session.conversation_repo.create_conversation(title="Fallback",
                transcript=[{"role": "user", "content": f"History {index}"} for index in range(120)],
                context_snapshot={"history": [{"role": "user", "content": f"History {index}"} for index in range(120)]})
            await socket.command("conversation.switch", conversation_id=archived.id)
            await socket.until("conversation.switched", conversation_id=archived.id, is_hydrating=False)
            read_history = session.conversation_repo.get_conversation

            def blocked_history(identity):
                if identity == fallback.id:
                    assert release_history.wait(5)
                return read_history(identity)

            monkeypatch.setattr(session.conversation_repo, "get_conversation", blocked_history)
            read_inventory = session.conversation_repo.list_conversations_with_revision
            inventory_reads = []

            def counted_inventory():
                inventory = read_inventory()
                inventory_reads.append(inventory)
                return inventory

            monkeypatch.setattr(session.conversation_repo, "list_conversations_with_revision", counted_inventory)
            await socket.command("conversation.archive", conversation_id=archived.id)
            result = await socket.until("command.result", command="conversation.archive")
            assert result["level"] == "success"
            assert len(inventory_reads) == 1
            assert session.active_conversation_id == fallback.id
            assert session.conversation_repo.get_conversation_summary(archived.id).archived
            switched = next(event for event in socket.sent if event.get("type") == "conversation.switched"
                and event.get("conversation_id") == fallback.id)
            assert switched["is_hydrating"] and switched["conversation"]["transcript_page"]["total_messages"] == 120
            release_history.set()
            await socket.until("conversation.switched", conversation_id=fallback.id, is_hydrating=False)
        finally:
            release_history.set()
            await socket.finish(task)


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
            if selected_model:
                await socket.command("llm.config.set", provider="custom", source="frontend.footer", reasoning_effort="high",
                    client_command_id="blank-footer-effort")
                blank_state = await socket.until("llm.model.updated", client_command_id="blank-footer-effort")
                assert blank_state["effective_reasoning_effort"] == "high"
                blank_capabilities = await socket.until("runtime.capabilities", client_command_id="blank-footer-effort")
                assert blank_capabilities["conversation_id"] == ""
                assert "skills" not in blank_capabilities["capabilities"]
                assert session.llm is session.context_builder._llm is None and adapter_calls == []
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
@pytest.mark.parametrize("failure", ["missing", "untrusted", "history_read", "workspace_parse", "owner_metadata"])
async def test_warm_switch_failures_reply_to_target_and_preserve_source(
    tmp_path, monkeypatch, protocol_environment, failure,
):
    source_root = tmp_path / "source-project"
    source_root.mkdir()
    target_root = tmp_path / "target-project"
    if failure != "missing":
        target_root.mkdir()
    trust_file = tmp_path / "trusted-workspaces.json"
    trust_file.write_text(json.dumps({"version": 1, "roots": [str(source_root)]}), encoding="utf-8")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    socket = ProtocolSocket(f"session_failed_switch_{failure}")
    fault_message = f"Target {failure} failed before switching"

    async with main.lifespan(main.app):
        task = asyncio.create_task(main.websocket_endpoint(socket))
        try:
            await socket.until("llm.model.updated")
            session = main._state.ws_manager.get_session(socket.query_params["session_id"])
            source = session.conversation_repo.create_conversation(title="Source task", workspace_root=str(source_root),
                transcript=[{"role": "user", "content": "Original history"}],
                context_snapshot={"history": [{"role": "user", "content": "Original history"}]})
            target = session.conversation_repo.create_conversation(title="Target task", workspace_root=str(target_root),
                transcript=[{"role": "user", "content": "Target history"}])
            await socket.command("conversation.switch", conversation_id=source.id)
            await socket.until("conversation.switched", conversation_id=source.id, is_hydrating=False)
            source_history = [message.content for message in session.context_builder._history]
            source_transcript = session.conversation_repo.get_conversation(source.id).transcript

            if failure == "history_read":
                read_view = session.conversation_repo.get_conversation_view

                def failing_view(identity, **kwargs):
                    if identity == target.id:
                        raise RuntimeError(fault_message)
                    return read_view(identity, **kwargs)

                monkeypatch.setattr(session.conversation_repo, "get_conversation_view", failing_view)
            elif failure == "workspace_parse":
                import backend.services.workspace_service as workspace_service
                parse_workspace = workspace_service.parse_workspace_activation_request

                def failing_parse(path):
                    if path == str(target_root):
                        raise RuntimeError(fault_message)
                    return parse_workspace(path)

                monkeypatch.setattr(workspace_service, "parse_workspace_activation_request", failing_parse)
            elif failure == "owner_metadata":
                read_summary = session.conversation_repo.get_conversation_summary

                def failing_summary(identity):
                    if identity == target.id:
                        raise RuntimeError(fault_message)
                    return read_summary(identity)

                monkeypatch.setattr(session.conversation_repo, "get_conversation_summary", failing_summary)

            command_id = f"switch-failure-{failure}"
            await socket.command("conversation.switch", conversation_id=target.id, client_command_id=command_id)
            result = await socket.until("command.result", command="conversation.switch", client_command_id=command_id)
            assert result["level"] == "error"
            assert result["conversation_id"] == result["data"]["conversation_id"] == target.id
            assert result["data"]["client_command_id"] == command_id
            if failure in {"missing", "untrusted"}:
                assert result["data"]["error_code"] == f"workspace_{failure}"
                assert str(target_root) in result["message"]
            else:
                assert result["message"] == fault_message
            if failure == "owner_metadata":
                assert result["workspace_root"] == result["data"]["workspace_root"] == ""
            else:
                assert result["workspace_root"] == result["data"]["workspace_root"] == str(target_root.resolve())
            assert session.active_conversation_id == source.id
            assert session.session_lifecycle.current_workspace_root() == source_root.resolve()
            assert session.session_lifecycle.workspace_context.root_path == source_root.resolve()
            assert [message.content for message in session.context_builder._history] == source_history
            assert session.conversation_repo.get_conversation(source.id).transcript == source_transcript
            assert not any(event.get("type") == "conversation.switched" and event.get("conversation_id") == target.id for event in socket.sent)
            await socket.command("ping")
            await socket.until("pong")
            assert session.is_connected and socket.closed == []
        finally:
            await socket.finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_location", ["top", "data"])
async def test_dispatched_result_preserves_explicit_global_owner_and_empty_workspace(
    tmp_path, monkeypatch, protocol_environment, owner_location,
):
    from backend.agent.message import AgentEvent

    old_root = tmp_path / "previous-workspace"
    old_root.mkdir()
    trust_file = tmp_path / "trusted-workspaces.json"
    trust_file.write_text(json.dumps({"version": 1, "roots": [str(old_root)]}), encoding="utf-8")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    socket = ProtocolSocket(f"session_explicit_result_owner_{owner_location}")
    async with main.lifespan(main.app):
        task = asyncio.create_task(main.websocket_endpoint(socket))
        try:
            await socket.until("llm.model.updated")
            session = main._state.ws_manager.get_session(socket.query_params["session_id"])
            previous = session.conversation_repo.create_conversation(title="Project task", workspace_root=str(old_root),
                transcript=[{"role": "user", "content": "Original history"}])
            global_task = session.conversation_repo.create_conversation(title="Global task")
            await socket.command("conversation.switch", conversation_id=previous.id)
            await socket.until("conversation.switched", conversation_id=previous.id, is_hydrating=False)

            async def producer(_data):
                owner = {"conversation_id": global_task.id, "workspace_root": ""}
                event = AgentEvent.command_result("conversation.export", "Explicit owner failure", level="error",
                    data=owner if owner_location == "data" else None)
                if owner_location == "top":
                    event.data.update(owner)
                await session.send_event(event)
                return True

            session.command_registry.register("conversation.export", producer)
            command_id = f"explicit-owner-{owner_location}"
            await socket.command("conversation.export", conversation_id=previous.id, client_command_id=command_id)
            result = await socket.until("command.result", command="conversation.export", client_command_id=command_id)
            assert result["conversation_id"] == result["data"]["conversation_id"] == global_task.id
            assert result["workspace_root"] == result["data"]["workspace_root"] == ""
            assert session.active_conversation_id == previous.id
            assert session.session_lifecycle.current_workspace_root() == old_root.resolve()
            assert session.conversation_repo.get_conversation(previous.id).transcript[0]["content"] == "Original history"
        finally:
            await socket.finish(task)


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
async def test_real_queries_and_warm_history_switches_bind_only_at_execution_boundaries(
    monkeypatch, protocol_environment,
):
    from backend.config_helpers import SETTINGS_FILE
    from backend.agent.model_execution import ModelExecutionSnapshot

    monkeypatch.setenv("OPENAI_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fixture")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://protocol.invalid/v1")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    SETTINGS_FILE.write_text(json.dumps({"llm": {"provider": "openai", "custom": {
        "api_key": "offline-fixture", "model": "catalog-model", "available_models": ["catalog-model"],
        "base_url": "https://custom.protocol.invalid/v1", "wire_api": "responses", "tool_mode": "direct",
        "model_metadata": {"catalog-model": {"source": "provider", "context_window": 32000,
            "reasoning_effort_levels": ["low", "high"], "input": ["text"], "tool_mode": "direct",
            "parallel_tool_calls": False, "native_compaction": False, "supports_hosted_web_search": False}}}}}), encoding="utf-8")
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
            captured = ModelExecutionSnapshot.capture(session.config, adapters[0])
            history = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"Saved entry {index}"}
                for index in range(24)]
            saved = session.conversation_repo.create_conversation(title="Other provider task", transcript=history,
                context_snapshot={"history": history}, model_selection={"provider": "custom", "model": "catalog-model", "reasoning_effort": "high"})
            unavailable = session.conversation_repo.create_conversation(title="Retired model task", transcript=history,
                context_snapshot={"history": history}, model_selection={"provider": "custom", "model": "retired-model", "reasoning_effort": "low"})
            removed = session.conversation_repo.create_conversation(title="Removed provider task", transcript=history,
                context_snapshot={"history": history}, model_selection={"provider": "Removed-Provider", "model": "removed-model", "reasoning_effort": "low"})

            summarize_capabilities = session.runtime_capability_summary

            def summary_after_public_history(**kwargs):
                if session.active_conversation_id == saved.id:
                    assert any(event["type"] == "conversation.switched" and event.get("conversation_id") == saved.id
                        and event.get("is_hydrating") is True for event in socket.sent)
                return summarize_capabilities(**kwargs)

            monkeypatch.setattr(session, "runtime_capability_summary", summary_after_public_history)

            await socket.command("conversation.switch", conversation_id=saved.id)
            pending = await socket.until("conversation.switched", conversation_id=saved.id, is_hydrating=True)
            complete = await socket.until("conversation.switched", conversation_id=saved.id, is_hydrating=False)
            assert pending["conversation"]["transcript"][-1]["content"] == complete["conversation"]["transcript"][-1]["content"] == "Saved entry 23"
            current = complete["session"]["capabilities"]["provider_capabilities"]
            assert current["provider"] == "custom" and current["model"] == "catalog-model"
            assert current["context_window"] == 32000 and current["vision"] is False
            assert current["effective_reasoning_effort"] == "high" and current["parallel_tool_calls"] is False
            assert current["native_compaction"] is False and current["supports_hosted_web_search"] is False
            assert len(session.context_builder._history) == 24
            assert len(adapters) == len(requests) == 1
            assert session.llm is captured.llm and captured.provider == "openai"
            assert captured.model == captured.llm.model_id() == "gpt-6.1-sol"
            assert captured.config.llm.reasoning_effort == "medium"

            await socket.command("commands.list")
            await socket.until("commands.list", conversation_id=saved.id)
            refresh_task = session._extension_runtime_states[saved.id].get("model_refresh_task")
            if refresh_task is not None:
                await asyncio.wait_for(asyncio.shield(refresh_task), 5)
            assert len(adapters) == 1 and session.llm is captured.llm
            assert session.runtime_toolset_policy(session.tool_registry).code_mode_enabled is False
            await socket.command("user_message", content="Use the saved provider", conversation_id=saved.id,
                client_command_id="saved-provider-query", user_message_id="saved-user", assistant_message_id="saved-assistant")
            second_done = await socket.until("done", conversation_id=saved.id)
            assert second_done["status"] == "completed"
            assert len(adapters) == len(requests) == 2 and session.llm is adapters[1]
            assert requests[1]["model"] == "catalog-model" and requests[1]["reasoning"]["effort"] == "high"
            assert "Saved entry 0" in json.dumps(requests[1])

            for old_task in (unavailable, removed):
                await socket.command("conversation.switch", conversation_id=old_task.id)
                await socket.until("conversation.switched", conversation_id=old_task.id, is_hydrating=True)
                restored = await socket.until("conversation.switched", conversation_id=old_task.id, is_hydrating=False)
                assert restored["conversation"]["transcript"][-1]["content"] == "Saved entry 23"
                current = restored["session"]["capabilities"]["provider_capabilities"]
                assert current["provider"] == old_task.model_selection["provider"]
                assert current["model"] == old_task.model_selection["model"]
                if old_task.id == removed.id:
                    assert session.models_source == "unavailable" and session.available_models == []
                    assert current["confidence"] == "unavailable" and current["limitations"] == ["provider_catalog_unavailable"]
                    assert current["wire_api"] == current["base_url"] == ""
                    assert current["streaming"] is False and current["tool_calling"] is False
                    assert current["context_window"] == 0 and current["vision"] is None
                await socket.command("commands.list")
                await socket.until("commands.list", conversation_id=old_task.id)
                refresh_task = session._extension_runtime_states[old_task.id].get("model_refresh_task")
                if refresh_task is not None:
                    await asyncio.wait_for(asyncio.shield(refresh_task), 5)
                assert session.provider == old_task.model_selection["provider"]
                assert session.selected_model == old_task.model_selection["model"]
                if old_task.id == removed.id:
                    selection = session._llm_selection_payload()
                    assert selection["models_source"] == "unavailable" and selection["provider"] == "Removed-Provider"
                    assert selection["available_models"] == [] and selection["wire_api"] == ""
                assert len(adapters) == len(requests) == 2
            await socket.command("user_message", content="Resume the retired provider", conversation_id=removed.id,
                client_command_id="removed-provider-query", user_message_id="removed-user", assistant_message_id="removed-assistant")
            unavailable_error = await socket.until("error", conversation_id=removed.id)
            unavailable_done = await socket.until("done", conversation_id=removed.id)
            assert unavailable_error["error_type"] == unavailable_error["provider_error_type"] == "model"
            assert unavailable_error["recoverable"] is False
            assert unavailable_done["status"] == "failed" and unavailable_done["reason"] == "llm_initialization_failed"
            assert unavailable_done["failure_recoverable"] is False
            terminal = default_runtime().latest_main_run(removed.id)
            assert terminal.status == "failed" and terminal.terminal_reason == "llm_initialization_failed"
            assert terminal.error == unavailable_error["message"]
            assert session.conversation_repo.get_conversation(removed.id).transcript[0]["content"] == "Saved entry 0"
            assert len(adapters) == len(requests) == 2
            await socket.command("session.restore", last_seq=0, last_conversation_id=conversation_id)
            restored = await socket.until("session.restored")
            assert restored["conversation"]["transcript"][-1]["content"] == "The requested result is ready."
            assert session.provider == "openai" and session.selected_model == "gpt-6.1-sol"
            assert session._provider_capabilities_payload()["model"] == "gpt-6.1-sol"
            assert session.runtime_toolset_policy(session.tool_registry).code_mode_enabled is True
            assert len(adapters) == len(requests) == 2 and session.llm is adapters[1]
            skill_snapshot = session.skill_manager.snapshot
            skill_reads = []

            def counted_skill_snapshot(*args, **kwargs):
                skill_reads.append(args)
                return skill_snapshot(*args, **kwargs)

            monkeypatch.setattr(session.skill_manager, "snapshot", counted_skill_snapshot)
            await socket.command("llm.model.set", model="gpt-6.1-sol", conversation_id=conversation_id, client_command_id="explicit-model-change")
            model_changed = await socket.until("llm.model.updated", conversation_id=conversation_id, client_command_id="explicit-model-change")
            assert model_changed["model"] == "gpt-6.1-sol"
            assert session.llm is adapters[1] and len(adapters) == 2
            assert session.context_builder._llm is session.llm
            assert len(requests) == 2
            model_capabilities = await socket.until("runtime.capabilities", client_command_id="explicit-model-change")
            assert model_capabilities["capabilities"]["provider_capabilities"]["model"] == "gpt-6.1-sol"
            assert "skills" not in model_capabilities["capabilities"] and "composer_commands" not in model_capabilities["capabilities"]
            assert model_capabilities["capabilities"]["tool_views"]
            assert skill_reads == []
            await socket.command("llm.config.set", source="frontend.footer", reasoning_effort="high", conversation_id=conversation_id,
                client_command_id="explicit-effort-change")
            effort_changed = await socket.until("llm.model.updated", conversation_id=conversation_id,
                client_command_id="explicit-effort-change", effective_reasoning_effort="high")
            assert effort_changed["model"] == "gpt-6.1-sol"
            assert session.context_builder._llm is session.llm
            assert session.llm is adapters[1] and len(adapters) == len(requests) == 2
            effort_capabilities = await socket.until("runtime.capabilities", client_command_id="explicit-effort-change")
            assert effort_capabilities["capabilities"]["provider_capabilities"]["effective_reasoning_effort"] == "high"
            assert skill_reads == []
            monkeypatch.setattr(session.skill_manager, "snapshot", skill_snapshot)
            await socket.command("user_message", content="Use the new task selection", conversation_id=conversation_id,
                client_command_id="selected-next-query", user_message_id="selected-user", assistant_message_id="selected-assistant")
            selected_done = await socket.until("done", conversation_id=conversation_id)
            assert selected_done["status"] == "completed"
            assert len(adapters) == len(requests) == 3 and session.llm is adapters[2]
            assert requests[2]["model"] == "gpt-6.1-sol" and requests[2]["reasoning"]["effort"] == "high"
            await socket.command("ping")
            await socket.until("pong")
            assert session.is_connected
            assert [event for event in socket.sent if event["type"] == "error"] == [unavailable_error]
        finally:
            await socket.finish(task)
