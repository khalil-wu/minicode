"""Focused verification for the eight business-source review repairs."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from mcp import types


def test_mcp_schema_keeps_wire_literals_and_only_projects_schema_prose():
    from backend.mcp.client import MCPClient

    schema = {
        "type": "object",
        "description": "\u212a prose\u202e",
        "$defs": {"choice": {"title": "\u212a", "enum": ["\u212a"]}},
        "properties": {
            "choice": {"type": "string", "enum": ["\u212a"], "const": "\u212a", "default": "\u212a", "pattern": "^\u212a$"},
            "description": {"type": "object", "default": {"description": "\u212a", "\u202e": "\u212a"}},
        },
        "required": ["choice"],
        "allOf": [{"description": "\u212a", "properties": {"choice": {"$ref": "#/$defs/choice"}}}],
    }
    original = copy.deepcopy(schema)

    class Session:
        async def list_tools(self, cursor=None):
            return types.ListToolsResult(tools=[types.Tool(name="wire_values", description="description", inputSchema=schema)])

        async def call_tool(self, name, arguments, **kwargs):
            invalid = not Draft202012Validator(original).is_valid(arguments)
            return types.CallToolResult(content=[types.TextContent(type="text", text="rejected" if invalid else "accepted")], isError=invalid)

    async def run():
        client = MCPClient("schema-verification")
        client._connected = True
        client._server_capabilities.tools = True
        client._session = Session()
        projected = (await client.list_tools())[0].input_schema
        assert projected["description"] == "K prose"
        assert projected["$defs"]["choice"]["title"] == "K"
        assert projected["allOf"][0]["description"] == "K"
        assert projected["properties"] == original["properties"]
        assert projected["$defs"]["choice"]["enum"] == ["\u212a"]
        assert projected["allOf"][0]["properties"] == original["allOf"][0]["properties"]
        assert Draft202012Validator(projected).is_valid({"choice": "\u212a"})
        assert not Draft202012Validator(projected).is_valid({"choice": "K"})
        result = await client.call_tool("wire_values", {"choice": "\u212a"})
        assert result.is_error is False
        assert result.text == "accepted"
        assert schema == original

    asyncio.run(run())


def test_mcp_schema_still_rejects_unsafe_schema_identifiers():
    from backend.mcp.client import MCPClient

    class Session:
        async def list_tools(self, cursor=None):
            return types.ListToolsResult(tools=[types.Tool(name="safe_name", inputSchema={"type": "object", "properties": {"unsafe\u202e": {"type": "string"}}})])

    async def run():
        client = MCPClient("unsafe-schema")
        client._connected = True
        client._server_capabilities.tools = True
        client._session = Session()
        with pytest.raises(ConnectionError, match="unsafe schema metadata"):
            await client.list_tools()

    asyncio.run(run())


@pytest.mark.parametrize("drain", [False, True])
def test_dynamic_command_early_cancel_reaps_real_child(drain):
    from backend.hooks.manager import HookEvent, HookManager, _HookEntry
    from backend.hooks.runners import HookRuntimeBindings, PendingAsyncCommand, _drain_started_hook_process, _terminate_hook_operation
    from backend.hooks.task_output import HookTaskOutput
    from backend.subprocesses import spawn_exec

    async def run():
        process = await spawn_exec(sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        capture = HookTaskOutput(scope_id="review-early-cancel", task_id="real-child")
        operation = asyncio.create_task(_drain_started_hook_process(process, b"", capture=capture))
        manager = HookManager()
        command = PendingAsyncCommand(process=process, capture=capture, operation=operation, timeout_seconds=30)
        entry = _HookEntry(matcher=re.compile(".*"), entry_id="early-cancel", command="bounded child")
        try:
            manager._schedule_dynamic_async_command(command, entry, HookEvent.PRE_TOOL_USE, runtime=HookRuntimeBindings())
            if drain:
                next(iter(manager._async_tasks)).cancel()
                await manager.drain_async_hooks()
            else:
                await manager.finalize_async_hooks()
            assert process.returncode is not None
            assert operation.done()
            assert capture._finished
            assert manager.pending_async_hooks == 0
            assert manager._session_runtime.async_commands == {}
            await manager.finalize_async_hooks()
        finally:
            if process.returncode is None or not operation.done():
                await _terminate_hook_operation(process, operation, capture)

    asyncio.run(run())


def test_failed_http_hook_does_not_grant_or_rewrite_real_permission_result(monkeypatch):
    from backend.hooks.manager import HookManager

    async def run():
        body = json.dumps({"event": "permission_request", "permission_decision": "allow", "updated_input": {"path": "changed", "content": "changed"}, "continue": False, "system_message": "not successful"}).encode()

        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        try:
            monkeypatch.setenv("NO_PROXY", "127.0.0.1")
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            manager = HookManager.from_settings({"hooks": {"permission_request": [{"matcher": "write_file", "hooks": [{"type": "http", "url": url}]}]}})
            result = await manager.run_permission_request("write_file", {"path": "original", "content": "original"})
            assert result.errors and "HTTP 500" in result.errors[0]
            assert result.permission_decision == ""
            assert result.updated_input is None
            assert result.prevent_continuation is False
            assert result.system_message == ""
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_command_hook_controls_require_success_but_exit_two_still_denies(exit_code):
    from backend.hooks.dispatcher import HookExecution
    from backend.hooks.manager import HookEvent, _HookEntry
    from backend.hooks.reducer import reduce_hook_executions

    entry = _HookEntry(matcher=re.compile(".*"), entry_id="decision", command="decision")
    body = json.dumps({"event": "pre_tool_use", "permission_decision": "allow", "updated_input": {"path": "changed"}})
    execution = HookExecution(entry, body, "blocked feedback", exit_code, 0, 0, 1)
    result = reduce_hook_executions(HookEvent.PRE_TOOL_USE, [execution], expected_event_name="pre_tool_use")
    assert result.permission_decision == {0: "allow", 1: "", 2: "deny"}[exit_code]
    assert result.updated_input == ({"path": "changed"} if exit_code == 0 else None)
    assert result.blocked is (exit_code == 2)


def test_project_approval_uses_original_server_identity(tmp_path, monkeypatch):
    import backend.mcp.project_settings as settings
    from backend.mcp.manager import MCPServerManager

    monkeypatch.setattr(settings, "is_workspace_trusted", lambda root: True)
    settings.approve_project_mcp_server("safe.name", tmp_path)
    assert settings.project_mcp_server_status("safe.name", tmp_path) == "approved"
    assert settings.project_mcp_server_status("safe/name", tmp_path) == "pending"
    assert settings.project_mcp_server_status("safe_name", tmp_path) == "pending"
    settings.approve_project_mcp_server("safe/name", tmp_path)
    settings.reject_project_mcp_server("safe.name", tmp_path)
    assert settings.project_mcp_server_status("safe.name", tmp_path) == "rejected"
    assert settings.project_mcp_server_status("safe/name", tmp_path) == "approved"

    async def run():
        manager = MCPServerManager(workspace_root=tmp_path)
        starts = []

        async def start(config):
            starts.append(config.name)

        async def register(config):
            pass

        manager.start_server = start
        manager.register_config = register
        status = settings.project_mcp_server_status("safe_name", tmp_path)
        config = manager._config_from_mapping("safe_name", {"transport": "stdio", "command": sys.executable}, source="project", priority=0, base_dir=tmp_path, approval_status=status)
        await manager._start_or_register_config(config)
        assert starts == []

    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://github.com/acme/forbidden.git", "git@github.com:acme/forbidden.git", "ssh://git@github.com/acme/forbidden.git", "https://github.com/ACME/FORBIDDEN.git", "https://github.com/acme/%66orbidden.git"])
def test_github_deny_is_repository_identity_not_transport_text(url):
    from backend.plugins.policy import ManagedPluginPolicy, PluginSettingsError

    policy = ManagedPluginPolicy({}, None, ({"source": "github", "repo": "acme/forbidden"},), {})
    with pytest.raises(PluginSettingsError, match="blocked"):
        policy.assert_source_allowed({"source": "git", "url": url})
    policy.assert_source_allowed({"source": "git", "url": "ssh://git@github.com/acme/other.git"})


def test_repository_policy_keeps_ref_path_and_non_github_boundaries():
    from backend.plugins.policy import ManagedPluginPolicy, PluginSettingsError

    source = {"source": "github", "repo": "acme/repo", "ref": "approved", "path": "plugin"}
    allowed = ManagedPluginPolicy({}, (source,), (), {})
    allowed.assert_source_allowed({"source": "git", "url": "git@github.com:acme/repo.git", "ref": "approved", "path": "plugin"})
    for changed in [{"ref": "other", "path": "plugin"}, {"ref": "approved", "path": "other"}, {}]:
        with pytest.raises(PluginSettingsError):
            allowed.assert_source_allowed({"source": "git", "url": "git@github.com:acme/repo.git", **changed})
    blocked = ManagedPluginPolicy({}, None, ({"source": "git", "url": "https://git.example.invalid/acme/repo.git"},), {})
    blocked.assert_source_allowed({"source": "git", "url": "ssh://git@git.example.invalid/acme/repo.git"})


@pytest.mark.parametrize("borrowed", [False, True])
def test_cancelled_load_revokes_only_its_own_generation_and_registration_owners(tmp_path, borrowed):
    from backend.extensions import ExtensionLoader, ExtensionStaleError

    async def run():
        first_path = tmp_path / "first.py"
        second_path = tmp_path / "second.py"
        first_path.write_text("# factory boundary supplied by test\n", encoding="utf-8")
        second_path.write_text("# factory boundary supplied by test\n", encoding="utf-8")
        apis = []
        old_events = []
        actions = []
        shared_loader = ExtensionLoader(cwd=tmp_path, cache_namespace="borrowed-generation")
        shared = None
        if borrowed:
            def old_factory(api):
                apis.append(api)
                api.events.on("probe", lambda event: old_events.append("old"))
                api.register_provider("old-provider", {"models": []})

            old = await shared_loader.load_factory(old_factory, extension_path=str(first_path))
            shared = old.runtime
        captured = []

        def first_factory(api):
            captured.append(api)
            api.events.on("probe", lambda event: api.send_message("cancelled callback"))
            api.register_provider("new-provider", {"models": []})

        async def second_factory(api):
            captured.append(api)
            raise asyncio.CancelledError()

        class Loader(ExtensionLoader):
            def _load_factory(self, path):
                return (first_factory if path == first_path else second_factory), None

        loader = Loader(cwd=tmp_path, cache_namespace="borrowed-generation" if borrowed else "owned-generation", event_bus=old.runner.event_bus if borrowed else None, runtime_actions={"send_message": lambda **values: actions.append(values["message"])})
        try:
            with pytest.raises(asyncio.CancelledError):
                await loader.load([first_path, second_path], runtime=shared, project_trusted=True)
            for api in captured:
                with pytest.raises(ExtensionStaleError):
                    api.send_message("must not run")
            assert actions == []
            assert captured[0]._runner.active is False
            if borrowed:
                assert old.runner.active
                await old.runner.event_bus.emit("probe")
                assert old_events == ["old"]
                assert [item.name for item in shared.pending_provider_registrations] == ["old-provider"]
            else:
                assert captured[0]._runner.runtime.active is False
                assert loader.event_bus._handlers == {}
        finally:
            if borrowed:
                old.runner.invalidate()

    asyncio.run(run())


def test_cancelled_inline_factory_api_is_stale(tmp_path):
    from backend.extensions import ExtensionLoader, ExtensionStaleError

    async def run():
        captured = []

        async def factory(api):
            captured.append(api)
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await ExtensionLoader(cwd=tmp_path).load_factory(factory)
        with pytest.raises(ExtensionStaleError):
            captured[0].register_flag("late", {"type": "boolean", "default": True})

    asyncio.run(run())


def test_callback_binding_uses_positions_aliases_and_one_invocation(tmp_path):
    from backend.extensions import ExtensionLoader
    from backend.extensions.runtime import _call_with_signature

    def alias(args, *, context, **extra):
        return args, context, extra

    result = _call_with_signature(alias, {"params": {"x": 1}, "ctx": "context", "other": 2}, ("call-id", {"x": 1}))
    assert result == ({"x": 1}, "context", {"other": 2})

    def positional(event, unlabelled, /):
        return event, unlabelled

    assert _call_with_signature(positional, {"event": "event", "ctx": "context"}, ("event", "context")) == ("event", "context")
    calls = []

    def execute(tool_call_id, payload, signal, on_update, ctx):
        calls.append((tool_call_id, payload, signal, on_update, ctx))
        return "OK"

    async def run():
        result = await ExtensionLoader(cwd=tmp_path).load_factory(lambda api: api.register_tool({"name": "mixed", "description": "mixed callback", "parameters": {"type": "object"}, "execute": execute}))
        try:
            output = await result.runner.invoke_tool(tool_call_id="call-id", tool_name="mixed", params={"x": 1})
            assert output.content == "OK"
            assert calls[0][:4] == ("call-id", {"x": 1}, None, None)
            assert len(calls) == 1
        finally:
            result.runner.invalidate()

    asyncio.run(run())
    side_effects = []

    def failing(params, **extra):
        side_effects.append(params)
        raise TypeError("callback failure, not binding failure")

    with pytest.raises(TypeError, match="callback failure"):
        _call_with_signature(failing, {"params": {"x": 1}}, ("call-id", {"x": 1}))
    assert side_effects == [{"x": 1}]


def test_registration_owner_is_internal_not_provider_config(tmp_path):
    from backend.extensions import ExtensionLoader

    async def run():
        result = await ExtensionLoader(cwd=tmp_path).load_factory(lambda api: api.register_provider("provider", {"models": []}))
        received = []
        sink = SimpleNamespace(register_provider=lambda name, config: received.append((name, config)))
        result.runner.bind_provider_sink(sink)
        assert received == [("provider", {"models": []})]
        result.runner.invalidate()

    asyncio.run(run())


def test_disconnect_stop_waits_for_real_oauth_peer_and_keeps_old_owner(tmp_path, monkeypatch):
    import backend.mcp.client as client_module
    from backend.mcp.client import MCPClient, MCPTransport
    from backend.mcp.manager import MCPServerConfig, MCPServerManager, MCPServerState, ServerStatus
    from backend.mcp.oauth import create_loopback_callback

    async def run():
        callback = await create_loopback_callback(interactive=False, server_url="http://127.0.0.1:12345/mcp")
        close_entered = asyncio.Event()
        close = callback.close

        async def observed_close():
            close_entered.set()
            await close()

        callback.close = observed_close
        transport_closed = []

        class Session:
            def __init__(self, *args, transport_closed: asyncio.Event, **kwargs):
                self.closed = transport_closed

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def initialize(self):
                transport_closed.append(self.closed)
                return types.InitializeResult(protocolVersion=types.LATEST_PROTOCOL_VERSION, capabilities=types.ServerCapabilities(), serverInfo=types.Implementation(name="bounded", version="1"))

        @asynccontextmanager
        async def transport():
            yield (None, None)

        monkeypatch.setattr(client_module, "_LifecycleClientSession", Session)
        client = MCPClient("lifecycle", transport=MCPTransport.HTTP, url="http://127.0.0.1:12345/mcp")
        client._sdk_transport_context = lambda: asyncio.sleep(0, result=transport())
        client._oauth_callback = callback
        manager = MCPServerManager(workspace_root=tmp_path)
        state = MCPServerState(config=MCPServerConfig(name="lifecycle", transport="http", url="http://127.0.0.1:12345/mcp"), client=client, status=ServerStatus.CONNECTED)
        manager._servers["lifecycle"] = state

        async def disconnected(name):
            await manager._handle_client_disconnect(name, client)

        client._on_disconnect = disconnected
        replacement_attempts = []

        async def attempt(*args, **kwargs):
            replacement_attempts.append("must wait for retirement")

        manager._attempt_connection = attempt
        writer = None
        stop = None
        try:
            await client.connect()
            reader, writer = await asyncio.open_connection("127.0.0.1", callback.server.sockets[0].getsockname()[1])
            await asyncio.sleep(0.01)
            transport_closed[0].set()
            await asyncio.wait_for(close_entered.wait(), 2)
            stop = asyncio.create_task(manager.stop_server("lifecycle"))
            await asyncio.sleep(0.03)
            assert not stop.done()
            assert state.client is client
            assert callback.server._active_count == 1
            assert not client._lifecycle_task.done()
            assert replacement_attempts == []
            writer.write(("GET " + callback.callback_path + "?code=bounded&state=bounded HTTP/1.1\r\nHost: localhost\r\n\r\n").encode())
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
            writer = None
            assert await asyncio.wait_for(stop, 2)
            assert state.status == ServerStatus.OFFLINE
            assert state.client is None
            assert callback.server._active_count == 0
            assert client._lifecycle_task is None
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await manager._cancel_reconnect_task("lifecycle")
            await close()
            if stop is not None:
                await asyncio.gather(stop, return_exceptions=True)
            if client._lifecycle_task is not None:
                await asyncio.gather(client._lifecycle_task, return_exceptions=True)

    asyncio.run(run())


def test_explicit_start_cannot_reuse_a_retiring_client(tmp_path, monkeypatch):
    from backend.mcp.manager import MCPAuthStatus, MCPServerConfig, MCPServerManager, MCPServerState, ServerStatus

    async def run():
        manager = MCPServerManager(workspace_root=tmp_path)
        monkeypatch.setattr(manager, "_stored_auth_status", lambda config: MCPAuthStatus.UNSUPPORTED)
        config = MCPServerConfig(name="retiring", transport="http", url="https://example.invalid/mcp")
        closed = asyncio.Event()
        release = asyncio.Event()

        class Client:
            connected = False

            async def close(self):
                closed.set()
                await release.wait()
                return True

        old = Client()
        state = MCPServerState(config=config, client=old, status=ServerStatus.ERROR)
        manager._servers[config.name] = state
        attempts = []

        async def attempt(name, state, **kwargs):
            attempts.append(state.client)

        manager._attempt_connection = attempt
        starting = asyncio.create_task(manager.start_server(config, force=True))
        await closed.wait()
        assert state.client is old
        assert attempts == []
        release.set()
        await starting
        assert attempts == [None]

    asyncio.run(run())
