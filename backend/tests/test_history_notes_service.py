from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.agent.context_window import ContextWindowConfig
from backend.agent.history_notes import (
    ENCRYPTED_TOOL_ARGUMENTS_HEADER,
    HISTORY_NOTES_BACKEND_TIMEOUT,
    TOOL_OUTPUT_TRUNCATION_POLICY_HEADER,
    HistoryNotesBackend,
    HistoryNotesError,
    contribute_history_notes_thread_context,
    contribute_legacy_notes_thread_context,
    register_history_notes_tools,
)
from backend.llm.model_runtime import ModelRuntime
from backend.llm.provider_models import ProviderModelsStorage
from backend.mcp.manager import MCPServerConfig
from backend.permissions.context import PermissionContext, ToolCallSource, ToolExecutionContext
from backend.permissions.checker import PermissionChecker, evaluate_permission_decision
from backend.config import PermissionSettings
from backend.tests.test_model_runtime import _FakeAuthStorage
from backend.tests.test_harness_mcp_services_regressions import manager_fixture
from backend.tools.base import validate_tool_input
from backend.tools.history_notes import history_notes_actions, history_notes_model_output
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ToolsetPolicy


@contextmanager
def notes_server(*, result=None, status=200, raw=None, delay=0, redirect=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "body": body, "headers": dict(self.headers), "received_at": time.monotonic()})
            if redirect is not None and self.path != "/redirected-service":
                self.send_response(307)
                self.send_header("Location", redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if delay:
                time.sleep(delay)
            payload = raw if raw is not None else json.dumps(result).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # The timeout case intentionally closes its actual HTTP socket.
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def notes_runtime(tmp_path, url, *, auth_mode="chatgpt", provider_name="OpenAI"):
    runtime = ModelRuntime(
        provider_configs={}, models_path=tmp_path / "models.json",
        models_store=ProviderModelsStorage(tmp_path / "models-store.json"),
        settings_snapshot={},
    )
    storage = _FakeAuthStorage()
    runtime._auth_storage = storage
    runtime.register_provider("service-fixture", {
        "name": provider_name,
        "api": "openai-responses",
        "base_url": "https://configured-model-only.invalid/v1",
        "models": [{"id": "fixture", "input": ["text", "image"],
            "truncation_policy": {"mode": "tokens", "limit": 17}}],
        "oauth": {
            "login": lambda _callbacks: storage.get("service-fixture"),
            "refresh": lambda credential: credential,
            "to_auth": lambda credential: {
                "api_key": credential["access"],
                "base_url": url + "/backend-api/codex/",
                "headers": {"ChatGPT-Account-ID": "explicit-fixture-account", "x-openai-actor-authorization": "explicit-fixture-actor"},
            },
        },
    })
    storage.set("service-fixture", {
        "type": "oauth", "access": "explicit-fixture-access",
        "refresh": "explicit-fixture-refresh", "expires": time.time() * 1000 + 600_000,
        **({"auth_mode": auth_mode} if auth_mode is not None else {}),
    })
    return runtime, runtime.get_model("service-fixture", "fixture"), storage


@pytest.mark.parametrize("mode,enabled", [
    ("chatgpt", True), ("chatgptAuthTokens", True), ("headers", True),
    ("agentIdentity", True), ("personalAccessToken", True),
    (None, False), ("api_key", False), ("bedrockApiKey", False),
])
def test_gate_uses_explicit_source_auth_mode_and_not_oauth_token_shape(tmp_path, mode, enabled):
    runtime, model, _ = notes_runtime(tmp_path, "http://127.0.0.1", auth_mode=mode)
    assert runtime.history_notes_backend_enabled(model) is enabled


def test_gate_does_not_infer_account_from_provider_name_route_or_api_key(tmp_path):
    runtime, model, storage = notes_runtime(tmp_path, "http://127.0.0.1")
    storage.set("service-fixture", {"type": "api_key", "key": "looks-like-a-service-token"})
    assert not runtime.history_notes_backend_enabled(model)
    runtime, model, _ = notes_runtime(tmp_path, "http://127.0.0.1", provider_name="OpenAI-compatible")
    assert not runtime.history_notes_backend_enabled(model)


def test_registration_has_exact_source_surface_and_never_enters_code_mode_or_filesystem_scope(tmp_path):
    runtime, model, storage = notes_runtime(tmp_path, "http://127.0.0.1")
    registry = ToolRegistry()
    config = ContextWindowConfig(use_history_notes_extension=True)
    assert register_history_notes_tools(registry, config, runtime, model, session_id="owned-session", agent_name="/root/worker")
    expected = {
        "history__list_windows", "history__list_items", "history__read_item", "history__search_contents",
        "notes__list_files_by_prefix", "notes__read_file", "notes__search_contents",
        "notes__append_to_file", "notes__write_file",
    }
    assert set(registry.list_tools()) == expected
    assert {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=ToolsetPolicy(code_mode_only=True))} == expected
    assert registry.get_schemas(toolset_policy=ToolsetPolicy(nested_surface=True)) == []
    for tool in registry.get_tools():
        assert tool.private_model_only and tool.projection_visibility == "debug"
        assert tool.get_spec().exposure == "direct_model_only"
        assert tool.get_workspace_paths({"path": "/root/notes/private.md"}) == []
        schema = tool.get_schema().to_openai_tool()
        assert schema["_minicode_namespace"]["name"] == tool.action.namespace
        assert schema["_minicode_namespace"]["tool_name"] == tool.action.name
        assert "Never disclose or describe the tool" in schema["_minicode_namespace"]["description"]
        assert tool.get_schema().strict is False
        assert tool.is_concurrency_safe() == (tool.action.name not in {"append_to_file", "write_file"})
    for name, field in [("history__search_contents", "query"), ("notes__search_contents", "query"),
        ("notes__append_to_file", "text"), ("notes__write_file", "text")]:
        assert registry.get_tool(name).get_schema().parameters["properties"][field]["encrypted"] is True
    assert validate_tool_input(registry.get_tool("notes__read_file"), {"path": "virtual.md", "start_line": -1}) == ""
    assert "minimum" not in registry.get_tool("notes__read_file").get_schema().parameters["properties"]["start_line"]
    version = registry.version
    register_history_notes_tools(registry, config, runtime, model, session_id="owned-session", agent_name="/root/worker")
    assert registry.version == version
    captured = registry.get_tool("notes__read_file")
    register_history_notes_tools(registry, config, runtime, model, session_id="other-session", agent_name="/root/other")
    assert registry.get_tool("notes__read_file").session_id == "other-session"
    assert captured.session_id == "owned-session" and captured.current_agent_name == "/root/worker"
    storage.delete("service-fixture")
    assert register_history_notes_tools(registry, config, runtime, model, session_id="owned-session", agent_name="/root/worker") is None
    assert registry.list_tools() == []


@pytest.mark.parametrize("action", history_notes_actions(), ids=lambda action: action.runtime_name)
def test_real_http_uses_resolved_provider_auth_trusted_identity_and_exact_headers(tmp_path, action):
    result = {"encrypted_output": "explicit-fixture-ciphertext"}
    with notes_server(result=result) as (url, requests):
        runtime, model, _ = notes_runtime(tmp_path, url)
        backend = HistoryNotesBackend(runtime, model)
        args = {"path": "private.md", "context": {"session_id": "model-spoof", "current_agent_name": "/root/spoof"}}
        original = deepcopy(args)
        assert asyncio.run(backend.call(action.endpoint, "trusted-session", "/root/actual-worker", args,
            {"mode": "tokens", "limit": 17})) == result
        assert args == original
        [request] = requests
        assert request["path"] == "/backend-api/codex/" + action.endpoint
        assert request["body"]["context"] == {"session_id": "trusted-session", "current_agent_name": "/root/actual-worker"}
        headers = {name.lower(): value for name, value in request["headers"].items()}
        assert headers["authorization"] == "Bearer explicit-fixture-access"
        assert headers["chatgpt-account-id"] == "explicit-fixture-account"
        assert headers["x-openai-actor-authorization"] == "explicit-fixture-actor"
        assert headers[TOOL_OUTPUT_TRUNCATION_POLICY_HEADER] == '{"mode":"tokens","limit":17}'
        assert (headers.get(ENCRYPTED_TOOL_ARGUMENTS_HEADER) == "true") is (action.name in {"search_contents", "append_to_file", "write_file"})


def test_nonobject_arguments_and_logout_do_not_reach_service(tmp_path):
    with notes_server(result={"text": "unused"}) as (url, requests):
        runtime, model, storage = notes_runtime(tmp_path, url)
        backend = HistoryNotesBackend(runtime, model)
        with pytest.raises(HistoryNotesError, match="^History tool arguments must be a JSON object$"):
            asyncio.run(backend.call("alpha/history/v2/list_windows", "session", "/root", [], {"mode": "bytes", "limit": 1000}))
        storage.delete("service-fixture")
        with pytest.raises(HistoryNotesError, match="Could not resolve backend authentication"):
            asyncio.run(backend.call("alpha/notes/v2/read_file", "session", "/root", {}, {"mode": "bytes", "limit": 1000}))
        assert requests == []


def test_actual_service_redirect_follows_source_default_policy(tmp_path):
    with notes_server(result={"text": "redirected result"}, redirect="/redirected-service") as (url, requests):
        runtime, model, _ = notes_runtime(tmp_path, url)
        result = asyncio.run(HistoryNotesBackend(runtime, model).call("alpha/notes/v2/read_file", "session", "/root", {"path": "a.md"}, {"mode": "bytes", "limit": 1000}))
        assert result == {"text": "redirected result"}
        assert [request["path"] for request in requests] == ["/backend-api/codex/alpha/notes/v2/read_file", "/redirected-service"]
        assert requests[0]["body"] == requests[1]["body"]
        for request in requests:
            assert request["headers"]["Authorization"] == "Bearer explicit-fixture-access"


@pytest.mark.parametrize("status,raw,message", [
    (503, b'{"text":"private failure"}', "The backend request failed"),
    (200, b"invalid private JSON", "The backend returned invalid JSON"),
])
def test_source_service_errors_return_nonsecret_model_error(tmp_path, status, raw, message):
    with notes_server(status=status, raw=raw) as (url, _):
        runtime, model, _ = notes_runtime(tmp_path, url)
        with pytest.raises(HistoryNotesError, match=message) as caught:
            asyncio.run(HistoryNotesBackend(runtime, model).call("alpha/notes/v2/read_file", "session", "/root", {}, {"mode": "bytes", "limit": 1000}))
        assert "private" not in str(caught.value)


def test_source_backend_timeout_is_whole_http_request(tmp_path, monkeypatch):
    assert HISTORY_NOTES_BACKEND_TIMEOUT == 35
    monkeypatch.setattr("backend.agent.history_notes.HISTORY_NOTES_BACKEND_TIMEOUT", .05)
    with notes_server(result={"text": "late result"}, delay=.3) as (url, requests):
        runtime, model, _ = notes_runtime(tmp_path, url)
        with pytest.raises(HistoryNotesError, match="The backend request failed"):
            asyncio.run(HistoryNotesBackend(runtime, model).call("alpha/notes/v2/read_file", "session", "/root", {}, {"mode": "bytes", "limit": 1000}))
        assert len(requests) == 1
        # Source resolves auth and builds its transport before execute's
        # 35-second request clock; measure the actual HTTP request here.
        assert time.monotonic() - requests[0]["received_at"] < .25


@pytest.mark.parametrize("hint,status,expected", [
    ({"text": "Recent notes: /root/notes/a.md"}, 200, "Recent notes: /root/notes/a.md"),
    ({"text": ""}, 200, None), ({"text": "字" * 1334}, 200, None),
    ({"text": "a" * 4000}, 200, "a" * 4000),
    ({"text": "a" * 4001}, 200, None), ({"text": 123}, 200, None),
    ({"text": "unavailable"}, 503, None),
], ids=["native", "empty", "multibyte-overflow", "byte-boundary", "ascii-overflow", "invalid-text", "http-error"])
def test_thread_hint_uses_source_byte_budget_and_does_not_inject_failed_hints(tmp_path, hint, status, expected):
    with notes_server(result=hint, status=status) as (url, requests):
        runtime, model, _ = notes_runtime(tmp_path, url)
        text = asyncio.run(contribute_history_notes_thread_context(HistoryNotesBackend(runtime, model), session_id="trusted-session", agent_name="/root/worker"))
        assert text == expected
        [request] = requests
        assert request["path"] == "/backend-api/codex/alpha/notes/v2/thread_hint"
        assert request["body"] == {"context": {"session_id": "trusted-session", "current_agent_name": "/root/worker"}}
        headers = {name.lower(): value for name, value in request["headers"].items()}
        assert headers[TOOL_OUTPUT_TRUNCATION_POLICY_HEADER] == '{"mode":"bytes","limit":4000}'
        assert ENCRYPTED_TOOL_ARGUMENTS_HEADER not in headers


def test_native_encrypted_output_and_images_stay_separate_from_hooks_and_plain_log():
    result = {"encrypted_output": "explicit-fixture-ciphertext", "images": [
        {"data": "cG5n", "mime_type": "image/png", "detail": "original"},
        {"data": "anBlZw==", "mime_type": "image/jpeg", "detail": "low"},
        {"data": "Z2lm", "mime_type": "image/gif"},
        {"data": "d2VicA==", "mime_type": "image/webp", "detail": None},
    ]}
    log_result, native = history_notes_model_output(result)
    assert log_result == {"encrypted_output": "explicit-fixture-ciphertext"}
    assert native == [
        {"type": "encrypted_content", "encrypted_content": "explicit-fixture-ciphertext"},
        {"type": "input_image", "image_url": "data:image/png;base64,cG5n", "detail": "original"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,anBlZw==", "detail": "low"},
        {"type": "input_image", "image_url": "data:image/gif;base64,Z2lm"},
        {"type": "input_image", "image_url": "data:image/webp;base64,d2VicA=="},
    ]
    assert "images" in result
    assert history_notes_model_output({"encrypted_output": "", "images": []})[1] == [{"type": "encrypted_content", "encrypted_content": ""}]
    assert history_notes_model_output({"text": "actual plaintext"})[1] == '{"text":"actual plaintext"}'


@pytest.mark.parametrize("images", [None, {}, [None], [{"mime_type": "image/png"}],
    [{"data": "private-image"}], [{"data": "private-image", "mime_type": "image/png", "detail": "invalid"}],
    [{"data": "private-image", "mime_type": "image/png", "detail": []}],
])
def test_malformed_source_attachments_produce_an_explicit_error(images):
    with pytest.raises(HistoryNotesError, match="^History backend returned invalid image content.$"):
        history_notes_model_output({"encrypted_output": "explicit-fixture-ciphertext", "images": images})


def test_tool_output_preserves_server_budget_and_is_unavailable_in_code_mode(tmp_path):
    result = {"encrypted_output": "cipher" * 5000, "images": [{"data": "cG5n", "mime_type": "image/png", "detail": "original"}]}
    with notes_server(result=result) as (url, requests):
        runtime, model, _ = notes_runtime(tmp_path, url)
        registry = ToolRegistry()
        register_history_notes_tools(registry, ContextWindowConfig(use_history_notes_extension=True), runtime, model,
            session_id="trusted-session", agent_name="/root/worker")
        tool = registry.get_tool("notes__read_file")
        output = asyncio.run(tool.execute({"path": "private.md"}))
        assert not output.is_error
        assert output.history_notes_output[0]["encrypted_content"] == result["encrypted_output"]
        assert output.history_notes_output[1]["detail"] == "original"
        assert output.images == [] and output.artifact_id is None
        assert output.runtime_metadata["hook_output"] == {"encrypted_output": result["encrypted_output"]}
        assert "cG5n" not in output.content
        context = ToolExecutionContext(PermissionContext(), tool_call_id="nested",
            call_sources={"nested": ToolCallSource(kind="code_mode")})
        rejected = asyncio.run(tool.execute({"path": "private.md"}, context))
        assert rejected.is_error and rejected.content == "History tools are unavailable in code mode."
        assert len(requests) == 1


def test_undeclared_policy_uses_source_unknown_model_bytes_default(tmp_path):
    with notes_server(result={"text": "result"}) as (url, requests):
        runtime, model, _ = notes_runtime(tmp_path, url)
        registry = ToolRegistry()
        register_history_notes_tools(registry, ContextWindowConfig(use_history_notes_extension=True), runtime, replace(model, truncation_policy=None),
            session_id="session", agent_name="/root")
        assert not asyncio.run(registry.get_tool("notes__read_file").execute({"path": "a.md"})).is_error
        headers = {name.lower(): value for name, value in requests[0]["headers"].items()}
        assert headers[TOOL_OUTPUT_TRUNCATION_POLICY_HEADER] == '{"mode":"bytes","limit":10000}'


def legacy_notes_config(tmp_path, *, mode="text", tool_name="thread_hint", init_delay=0):
    script = tmp_path / "legacy_notes_mcp.py"
    receipt = tmp_path / "legacy-calls.jsonl"
    script.write_text('''import json, sys, time
from pathlib import Path
mode, tool_name, receipt, init_delay = sys.argv[1:]
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        time.sleep(float(init_delay))
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "source-legacy-notes-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": tool_name, "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}, "_meta": {"ui": {"visibility": []}}}]}
    elif method == "tools/call":
        with Path(receipt).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(request, ensure_ascii=False) + "\\n")
        if mode == "rpc-error":
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": "private-rpc-error"}}), flush=True)
            continue
        content = [] if mode == "empty" else [{"type": "text", "text": ""}, {"type": "text", "text": "legacy-first-line"}, {"type": "private-fixture-block", "text": "legacy-second-line"}, {"type": "text", "text": " "}, {"type": "text", "text": "x" * 5000}]
        result = {"content": content, "isError": mode == "server-error", "structuredContent": {"not-a-hint": "ignored"}}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''', encoding="utf-8")
    config = MCPServerConfig("notes", command=sys.executable,
        args=["-u", str(script), mode, tool_name, str(receipt), str(init_delay)])
    return config, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "server-error", "empty", "rpc-error"])
async def test_legacy_bridge_uses_real_registered_mcp_rpc_metadata_and_source_text_join(tmp_path, mode):
    manager = manager_fixture(tmp_path)
    config, receipt = legacy_notes_config(tmp_path, mode=mode)
    await manager.start_server(config)
    client = manager.get_client("notes")
    owners = []
    original_call = client.call_tool

    async def capture_call(*args, **kwargs):
        owners.append(kwargs["request_owner"])
        return await original_call(*args, **kwargs)

    client.call_tool = capture_call
    try:
        hint = await contribute_legacy_notes_thread_context(manager, thread_id="trusted-thread",
            owner_session_id="trusted-websocket", conversation_id="trusted-conversation")
        if mode in {"text", "server-error"}:
            assert hint == "legacy-first-line\nlegacy-second-line\n \n" + "x" * 5000
            assert len(hint.encode("utf-8")) > 4000
        else:
            assert hint is None
        [request] = [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()]
        assert request["method"] == "tools/call"
        assert request["params"]["name"] == "thread_hint"
        assert request["params"]["_meta"] == {"threadId": "trusted-thread"}
        assert "arguments" not in request["params"]
        assert owners == [{"session_id": "trusted-websocket", "conversation_id": "trusted-conversation", "mcp_manager": manager}]
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_legacy_bridge_waits_only_for_existing_startup_and_does_not_create_servers(tmp_path):
    manager = manager_fixture(tmp_path)
    assert await contribute_legacy_notes_thread_context(manager, thread_id="thread", owner_session_id="ws", conversation_id="conv") is None
    assert manager.get_all_status() == []
    config, receipt = legacy_notes_config(tmp_path, init_delay=.15)
    pending_start = asyncio.create_task(manager.start_server(config))
    while manager.get_server_lifecycle("notes") is None:
        await asyncio.sleep(0)
    try:
        hint = await contribute_legacy_notes_thread_context(manager, thread_id="thread", owner_session_id="ws", conversation_id="conv")
        await pending_start
        assert hint.startswith("legacy-first-line")
        assert len(receipt.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_legacy_bridge_requires_registered_actual_tool(tmp_path):
    manager = manager_fixture(tmp_path)
    config, receipt = legacy_notes_config(tmp_path, tool_name="ordinary-tool")
    await manager.start_server(config)
    try:
        assert await contribute_legacy_notes_thread_context(manager, thread_id="thread", owner_session_id="ws", conversation_id="conv") is None
        assert not receipt.exists()
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_legacy_bridge_does_not_misclassify_programming_errors_as_empty_hint(tmp_path):
    manager = manager_fixture(tmp_path)
    config, receipt = legacy_notes_config(tmp_path)
    await manager.start_server(config)

    async def broken_consumer(*args, **kwargs):
        raise AttributeError("Actual consumer wiring bug")

    manager.get_client("notes").call_tool = broken_consumer
    try:
        with pytest.raises(AttributeError, match="Actual consumer wiring bug"):
            await contribute_legacy_notes_thread_context(manager, thread_id="thread", owner_session_id="ws", conversation_id="conv")
        assert not receipt.exists()
    finally:
        await manager.stop_all()


@pytest.mark.parametrize("action", ["write_file", "append_to_file"])
@pytest.mark.parametrize("rule,expected", [("default", "auto"), ("ask", "confirm"), ("deny", "deny")])
def test_source_private_notes_plan_retains_authored_approval_and_deny_rules(tmp_path, action, rule, expected):
    runtime, model, _ = notes_runtime(tmp_path, "http://127.0.0.1")
    registry = ToolRegistry()
    register_history_notes_tools(registry, ContextWindowConfig(use_history_notes_extension=True), runtime, model,
        session_id="session", agent_name="/root")
    name = "notes__" + action
    tool = registry.get_tool(name)
    settings = PermissionSettings(require_confirm=[name] if rule == "ask" else [], require_diff_review=[],
        always_deny=[name] if rule == "deny" else [])
    checker = PermissionChecker(settings, tmp_path)
    decision = evaluate_permission_decision(checker, name,
        args={"path": "/root/notes/private.md", "text": "explicit-private-note"},
        context=PermissionContext(mode="plan"), tool=tool)
    assert decision.permission_level.value == expected
    assert decision.decision == {"auto": "allow", "confirm": "ask", "deny": "deny"}[expected]


@pytest.mark.parametrize("action", ["write_file", "append_to_file"])
def test_actual_plan_query_writes_private_notes_with_readonly_agent_scope(tmp_path, action):
    from backend.tests.test_codex_history_notes_chain import (
        call, final, service_chain, sdk_fixture, streamed_calls, submit,
    )

    item = call(action, {"path": "/root/notes/private.md", "text": "explicit-fixture-note"}, namespace="notes")
    with service_chain([streamed_calls([item]), [final()]], note_result={"text": "updated"}) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                checker = PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), tmp_path)
                state, events = await submit(fixture, permission_context=PermissionContext(mode="plan"),
                    permission_checker=checker, metadata={"read_only": True})
                writes = [request for request in server.note_requests if request["path"].endswith("/" + action)]
                assert len(writes) == 1
                assert writes[0]["body"]["text"] == "explicit-fixture-note"
                record = next(record for record in state.tool_calls if record.tool_name == "notes__" + action)
                assert record.status == "success"
                assert record.private_model_only
                assert not any(event.type == "approval_request" for event in events)
                tool = fixture.registry.get_tool("notes__" + action)
                assert tool.is_read_only({"path": "a.md", "text": "note"})
                assert not tool.is_concurrency_safe()
                assert tool.get_side_effect_kind() == "none" and not tool.is_idempotent()
        asyncio.run(scenario())


def test_actual_plan_notes_writes_remain_serial_despite_readonly_scope(tmp_path, monkeypatch):
    from backend.tests.test_codex_history_notes_chain import (
        call, final, service_chain, sdk_fixture, streamed_calls, submit,
    )

    active, max_active, order = 0, 0, []
    original = HistoryNotesBackend.call

    async def observe_real_call(self, path, *args, **kwargs):
        nonlocal active, max_active
        if not path.endswith(("/write_file", "/append_to_file")):
            return await original(self, path, *args, **kwargs)
        active += 1
        max_active = max(max_active, active)
        order.append(path)
        try:
            # Keep the first real service call in flight while the provider
            # commits the second; their Source parallel flag owns ordering.
            await asyncio.sleep(.03)
            return await original(self, path, *args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(HistoryNotesBackend, "call", observe_real_call)
    calls = [call("write_file", {"path": "a.md", "text": "one"}, namespace="notes", call_id="note-one"),
        call("append_to_file", {"path": "a.md", "text": "two"}, namespace="notes", call_id="note-two")]
    with service_chain([streamed_calls(calls), [final()]], note_result={"text": "updated"}) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                checker = PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), tmp_path)
                state, _ = await submit(fixture, permission_context=PermissionContext(mode="plan"),
                    permission_checker=checker, metadata={"read_only": True})
                assert order == ["alpha/notes/v2/write_file", "alpha/notes/v2/append_to_file"]
                assert max_active == 1 and active == 0
                assert len([record for record in state.tool_calls if record.tool_name in {"notes__write_file", "notes__append_to_file"}
                    and record.status == "success"]) == 2
        asyncio.run(scenario())


@pytest.mark.parametrize("action", ["write_file", "append_to_file"])
@pytest.mark.parametrize("approve", [True, False], ids=["approved", "declined"])
def test_actual_plan_notes_authored_ask_reaches_control_request_and_real_callback(tmp_path, action, approve):
    from contextlib import aclosing

    from backend.agent.loop_session import AgentLoopSessionContext
    from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
    from backend.agent.state import AgentState
    from backend.tests.test_codex_history_notes_chain import (
        call, final, service_chain, sdk_fixture, streamed_calls,
    )

    name = "notes__" + action
    item = call(action, {"path": "/root/notes/private.md", "text": "explicit-reviewed-note"}, namespace="notes")
    with service_chain([streamed_calls([item]), [final()]], note_result={"text": "updated"}) as server:
        async def scenario():
            async with sdk_fixture(tmp_path, server) as fixture:
                callbacks = []

                async def approval(call_id):
                    callbacks.append(call_id)
                    return {"action": "approve" if approve else "reject"}

                checker = PermissionChecker(PermissionSettings(require_confirm=[name], require_diff_review=[]), tmp_path)
                session = AgentSession(llm=fixture.adapter, tool_registry=fixture.registry,
                    artifact_store=fixture.artifacts, context_builder=fixture.builder,
                    permission_checker=checker, agent_settings=fixture.config.agent,
                    token_budget=fixture.config.token_budget, approval_handler=approval)
                state = AgentState(user_message="Perform the fixture", workspace_root=tmp_path,
                    conversation_id="notes-source-conversation")
                try:
                    async with aclosing(QueryEngine().submit(QuerySubmission(
                        user_message=state.user_message, session=session, state=state,
                        runtime=AgentLoopSessionContext(session_id="notes-plan-ask", workspace_root=tmp_path,
                            permission_context=PermissionContext(mode="plan"), run_context=fixture.owner,
                            metadata={"read_only": True, "conversation_id": state.conversation_id}),
                    ))) as stream:
                        events = [event async for event in stream]
                    assert state.terminal_status == "completed", state.stopped_reason
                    approvals = [event for event in events if event.type == "approval_request"]
                    assert len(approvals) == 1
                    assert approvals[0].data["tool_name"] == name
                    assert approvals[0].data["args"] == {"path": "/root/notes/private.md", "text": "explicit-reviewed-note"}
                    assert approvals[0].data["request_digest"]
                    assert callbacks == ["private-notes-call"]
                    calls = [request for request in server.note_requests if request["path"].endswith("/" + action)]
                    assert len(calls) == int(approve)
                    record = next(record for record in state.tool_calls if record.tool_name == name)
                    assert record.request_digest == approvals[0].data["request_digest"]
                    assert (record.status == "success") is approve
                    assert record.private_model_only
                    assert not any(event.type in {"tool_call", "tool_result"} and event.data.get("name") == name for event in events)
                finally:
                    await session.aclose()
        asyncio.run(scenario())
