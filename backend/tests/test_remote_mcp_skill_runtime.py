"""Source executor discovery through runtime admission and native provider bodies."""
from __future__ import annotations

import json
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import base64

import httpx
from websockets.asyncio.server import serve
import os
import sys
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.environment_selection import (EnvironmentConfig, EnvironmentConfigState, ThreadEnvironmentDefaults,
    ThreadEnvironments, TurnEnvironmentRequest, TurnEnvironmentSelection)
from backend.agent.execution_journal import ExecutionJournal
from backend.artifact.store import ArtifactStore
from backend.environments import EnvironmentManager, ExecutorRpcError
from backend.feature_flags import FeatureFlags
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, ToolCallEvent, estimate_llm_message_tokens
from backend.llm.openai_adapter import _openai_chat_messages, _responses_function_call_output_item
from backend.mcp.environment_runtime import MCPEnvironmentRuntime, prepare_environment_capabilities
from backend.mcp.manager import MCPServerConfig, MCPServerManager
from backend.mcp.config_file import read_mcp_config, write_mcp_config
from backend.services import mcp_service
from backend.sandbox.policy import PermissionProfile
from backend.tests.test_code_execution import ScriptModel, run_model
from backend.tests.test_environments_http import _config, _reply, _delta, _environment
from backend.tests.test_remote_mcp_skill_consumers import _catalog
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry
from backend.tools.skill_tools import ListSkillsTool, ReadSkillTool
from backend.tools.mcp_tools import ListMcpResourcesTool, ListMcpResourceTemplatesTool, ReadMcpResourceTool
from backend.tools.toolsets import ToolsetPolicy


async def _resolve(selection, config, environment, peer_info):
    return config


def _configure_source(tmp_path, *, required=()):
    home = tmp_path / "owned-source-home"
    home.mkdir()
    _config(tmp_path, {"program": os.environ["MINICODE_SOURCE_EXECUTOR"],
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "8"],
        "cwd": str(tmp_path), "env": {"CODEX_HOME": str(home)}, "skills": {"required": list(required)}})
    return home, EnvironmentManager(tmp_path)


async def _capture(manager, cwd, roots=()):
    config = EnvironmentConfig(PermissionProfile.disabled(), (cwd,), selected_capability_roots=tuple(roots))
    owner = ThreadEnvironments(manager, ThreadEnvironmentDefaults(config, "disabled"), resolve_config=_resolve)
    await owner.update_selections((TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("remote", cwd, (cwd,),
        EnvironmentConfigState.ready(config))),))
    return owner, await owner.snapshot()


def _assert_provider_outputs(message, output):
    assert _responses_function_call_output_item(message)["output"] == output
    assert _openai_chat_messages([message])[0]["content"] == output
    _, messages = AnthropicAdapter._convert_messages([message])
    block = next(block for row in messages for block in row["content"] if block["type"] == "tool_result")
    assert block["content"] == output


def test_host_native_json_retains_cold_checkpoint_journal_and_all_three_provider_bodies(tmp_path):
    body = json.dumps({"resources": [{"server": "docs", "uri": "docs://actual", "title": "实际资源",
        "annotations": {"audience": ["assistant"]}, "_meta": {"source": "original"}}], "nextCursor": "原游标"}, ensure_ascii=False)
    builder = ContextBuilder(workspace_root=tmp_path)
    builder.append_assistant_tool_calls([ToolCallEvent("native-call", "list_mcp_resources", {})])
    builder.append_tool_result("native-call", "list_mcp_resources", ToolResult("display", native_tool_output=body))
    message = builder._history[-1]
    assert message.content == body and message.mcp_output is None and message.code_mode_output is None
    assert estimate_llm_message_tokens(message) == (len(body.encode("utf-8")) + 3) // 4
    _assert_provider_outputs(message, body)
    snapshot = builder.export_snapshot()
    restored = ContextBuilder(workspace_root=tmp_path)
    restored.load_snapshot(snapshot)
    _assert_provider_outputs(restored._history[-1], body)
    journal = ExecutionJournal("native-json", base_dir=tmp_path / "journal")
    journal.append_tool_use({"id": "native-call", "name": "list_mcp_resources", "args": {}})
    journal.append_tool_result({"id": "native-call", "content": "display", "model_content": body, "native_tool_output": body},
        tool_name="list_mcp_resources")
    replay = journal.reconstruct_history()
    tool_message = next(row for row in replay if row["role"] == "tool")
    assert tool_message["content"] == body and tool_message["native_tool_output"] == body


@pytest.mark.parametrize("tool_type", [ListMcpResourcesTool, ListMcpResourceTemplatesTool, ReadMcpResourceTool])
def test_captured_model_resource_messages_reach_registry_and_change_cache_without_rewriting_runtime(tool_type):
    tool = tool_type(None)
    registry = ToolRegistry()
    registry.register(tool)
    parameters = {"type": "object", "properties": {"server": {"type": "string", "description": "Model server", "format": "ignored"}},
        "additionalProperties": False, "required": ["server"], "unevaluatedProperties": False}
    def policy(description, raw):
        return ToolsetPolicy(model_messages={"tools": {"mcp_resources": {tool.name: {"description": description, "parameters": raw}}}})

    first = registry.get_schemas(toolset_policy=policy("Original model catalog", json.dumps(parameters)))[0]["function"]
    second = registry.get_schemas(toolset_policy=policy("Replacement model catalog", json.dumps(parameters)))[0]["function"]
    assert first["description"] == "Original model catalog" and second["description"] == "Replacement model catalog"
    assert first["parameters"]["required"] == ["server"]
    assert "unevaluatedProperties" not in first["parameters"] and "format" not in first["parameters"]["properties"]["server"]
    assert tool.get_schema().description == tool.description and "Model server" not in repr(tool.get_schema().parameters)
    empty = registry.get_schemas(toolset_policy=policy("", json.dumps(parameters)))[0]["function"]
    assert empty["description"] == ""
    for raw in ('[]', '{"type":"array"}', '{"type":"object","properties":{"server":true}}', '{"type":"object","required":"server"}', '{invalid'):
        invalid = registry.get_schemas(toolset_policy=policy("Valid independent description", raw))[0]["function"]
        assert invalid["description"] == "Valid independent description" and invalid["parameters"] == tool.get_schema().parameters


@pytest.mark.parametrize("remote_full,main_full,has_scope", [(False, True, True), (True, False, True), (False, True, False)])
def test_final_mcp_approval_uses_captured_server_scope_even_when_primary_has_other_permissions(tmp_path, remote_full, main_full, has_scope):
    from backend.mcp.registry import MCPToolProxy
    from backend.permissions.context import PermissionContext
    from backend.permissions.environment_scope import EnvironmentPermissionScope
    from backend.sandbox.policy import FileSystemSandboxPolicy, NetworkSandboxPolicy
    from backend.tests.test_codex_mcp_approval_producer import authorize, policy, proxy

    _tool, client, manager = proxy({"destructiveHint": True, "openWorldHint": True}, "prompt")
    config = manager.get_server_config("source-server")
    config.environment_id = "remote-server"
    scope = EnvironmentPermissionScope("remote-server", PermissionProfile.disabled() if remote_full else
        PermissionProfile.managed(FileSystemSandboxPolicy.restricted(()), network=NetworkSandboxPolicy.RESTRICTED),
        "file:///executor", ("file:///executor",), "linux") if has_scope else None
    manager.environment_runtime = SimpleNamespace(permission_scope=lambda environment_id: scope)
    tool = MCPToolProxy("source-server", _tool._tool_def, client, manager=manager)
    permission = PermissionContext(mode="bypass", approval_policy="never", workspace_root=tmp_path)
    result, _, _ = authorize(tool, tmp_path, policy(tmp_path, "disabled" if main_full else "managed-workspace"), permission)
    assert (result.permission_decision.decision == "allow") is (remote_full and has_scope)
    manager.environment_runtime.permission_scope = lambda environment_id: None
    assert tool.permission_scope is scope and tool.environment_id == "remote-server"


@pytest.mark.asyncio
async def test_original_mcp_callback_owner_keeps_server_scope_and_manager_after_next_step_rebind(tmp_path):
    from backend.agent.run_context import RunContext
    from backend.mcp.registry import MCPToolProxy
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.permissions.environment_scope import EnvironmentPermissionScope
    from backend.tests.test_codex_mcp_approval_producer import policy, proxy

    old_tool, transport, manager = proxy({"readOnlyHint": True})
    manager.get_server_config("source-server").environment_id = "original-remote"
    scope = EnvironmentPermissionScope("original-remote", PermissionProfile.disabled(), "file:///original", (), "linux")
    manager.environment_runtime = SimpleNamespace(permission_scope=lambda environment_id: scope)
    tool = MCPToolProxy("source-server", old_tool._tool_def, transport, manager=manager)
    context = ToolExecutionContext(PermissionContext(), workspace_root=tmp_path,
        sandbox_policy=policy(tmp_path, "disabled"), run_context=RunContext(mcp_manager=object()))
    result = await tool.execute({"value": "original"}, context=context)
    assert not result.is_error
    owner = transport.calls[0][2]
    assert owner["mcp_manager"] is manager and owner["mcp_permission_scope"] is scope and owner["mcp_permission_profile"] is None


@pytest.mark.asyncio
async def test_real_provider_step_uses_captured_model_resource_messages_in_code_mode_catalog(tmp_path):
    from backend.config import LLMSettings
    from backend.llm.provider_contracts import ModelDefinition

    manager = SimpleNamespace(iter_connected_clients=lambda: ())
    messages = {"tools": {"mcp_resources": {"list_mcp_resources": {"description": "CAPTURED_RESOURCE_CATALOG"}}}}
    info = ModelDefinition("custom", "owned-fixture", "Owned fixture", "openai-completions", "https://fixture.invalid/v1",
        extra={"model_messages": messages})
    model = ScriptModel("const result = await tools.list_mcp_resources({}); text(JSON.parse(result.content));")
    model._settings = LLMSettings(api_key="", model="owned-fixture", tool_mode="code_mode_only")
    state, _, _, _, _ = await run_model(tmp_path, model, [ListMcpResourcesTool(manager)], code_mode_only=True, model_info=info)
    assert state.terminal_status == "completed", model.reports
    assert "CAPTURED_RESOURCE_CATALOG" in model.visible_tool_descriptions[0]["tool_exec"]
    assert model.reports[-1]["output"] == ['{"resources":[]}']


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_capability", [False, True])
async def test_restricted_selected_root_uses_real_sandbox_rpc_and_failed_partial_catalogs_are_not_success_cache(tmp_path, sandbox_capability):
    from backend.sandbox.policy import FileSystemSandboxPolicy, NetworkSandboxPolicy

    discoveries = []
    async def peer(socket):
        async for raw in socket:
            message = json.loads(raw)
            method = message["method"]
            if method == "initialized":
                continue
            info = {"providerId": "owned-sandbox-peer", "platformOs": "linux",
                "capabilities": {"capabilityDiscoverySandbox": sandbox_capability}}
            if method == "initialize":
                result = {"sessionId": "owned-sandbox", "environmentInfo": info}
            elif method == "environment/info":
                result = info
            elif method == "capabilityRoots/discoverV1":
                discoveries.append(message["params"])
                [root] = message["params"]["roots"]
                assert root["sandbox"] is not None and root["sandbox"]["permissions"]["type"] == "managed"
                result = {"roots": [{"id": root["id"], "path": root["path"], "skills": [],
                    "warnings": ["actual partial scan"] if len(discoveries) == 2 else [],
                    "error": "actual first failure" if len(discoveries) == 1 else None}]}
            else:
                raise AssertionError(f"Unexpected executor method {method}")
            await socket.send(json.dumps({"id": message["id"], "result": result}))

    async with serve(peer, "127.0.0.1", 0) as server:
        _config(tmp_path, {"url": f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"})
        manager = EnvironmentManager(tmp_path)
        root = {"id": "owner-selected", "location": {"type": "environment", "environmentId": "remote", "path": "file:///executor/skills"}}
        profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted(()), network=NetworkSandboxPolicy.RESTRICTED)
        config = EnvironmentConfig(profile, (), selected_capability_roots=(root,))
        owner = ThreadEnvironments(manager, ThreadEnvironmentDefaults(config, "disabled"), resolve_config=_resolve)
        await owner.update_selections((TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("remote", "file:///executor", (),
            EnvironmentConfigState.ready(config))),))
        snapshot = await owner.snapshot()
        prepared = []
        try:
            for _ in range(4 if sandbox_capability else 1):
                current = await prepare_environment_capabilities(turn_environments=snapshot, host_skill_manager=None, host_mcp_manager=None,
                    feature_flags=FeatureFlags({"executor_capability_discovery": False}), thread_id="sandbox-owner", turn_id="sandbox-turn",
                    session_source="sdk", isolated=True, environment_manager=manager, local_cwd=tmp_path.as_uri(),
                    previous=prepared[-1] if prepared else None)
                prepared.append(current)
            assert len(discoveries) == (3 if sandbox_capability else 0)
            if sandbox_capability:
                assert "actual first failure" in str(prepared[0].warnings)
                assert "actual partial scan" in str(prepared[1].warnings)
                assert not prepared[-1].warnings
            else:
                assert "does not support sandboxed capability discovery" in str(prepared[0].warnings)
        finally:
            for current in prepared:
                await current.aclose()
            await snapshot.aclose()
            await owner.aclose()
            await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("config_state,expected_state", [("Failed", "failed"), ("Pending", "starting")])
async def test_required_skill_failed_or_starting_attachment_blocks_real_query_engine_before_any_provider_request(tmp_path, monkeypatch, caplog, config_state, expected_state):
    from contextlib import aclosing
    from backend.agent.loop_session import AgentLoopSessionContext
    from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
    from backend.agent.run_context import RunContext
    from backend.agent.runtime import AgentRuntime
    from backend.agent.state import AgentState
    from backend.config import AgentSettings, PermissionSettings, TokenBudget
    from backend.mcp.environment_runtime import PreparedEnvironmentCapabilities
    from backend.permissions.context import PermissionContext
    from backend.permissions.checker import PermissionChecker

    async def peer(socket):
        async for raw in socket:
            message = json.loads(raw)
            if message["method"] == "initialized":
                continue
            assert message["method"] in {"initialize", "environment/info"}
            info = {"providerId": "required-skill-owner", "platformOs": "linux", "capabilities": {}}
            result = {"sessionId": "required-skill-session", "environmentInfo": info} if message["method"] == "initialize" else info
            await socket.send(json.dumps({"id": message["id"], "result": result}))

    states = []
    original_gate = PreparedEnvironmentCapabilities.validate_required_skills
    def observe_actual_gate(prepared):
        snapshot = prepared.runtime.snapshot
        states.append((len(snapshot.starting()), type(snapshot.environments[0]).__name__, snapshot.required_skills()))
        original_gate(prepared)
    monkeypatch.setattr(PreparedEnvironmentCapabilities, "validate_required_skills", observe_actual_gate)

    async with serve(peer, "127.0.0.1", 0) as server:
        _config(tmp_path, {"url": f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "skills": {"required": ["exact"]}})
        manager = EnvironmentManager(tmp_path)
        model = ScriptModel("")
        budget = TokenBudget(total=96000)
        artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
        context = ContextBuilder(llm=model, token_budget=budget, workspace_root=tmp_path)
        session = AgentSession(llm=model, tool_registry=ToolRegistry(), artifact_store=artifacts, context_builder=context,
            permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), tmp_path),
            agent_settings=AgentSettings(max_iterations=2, stream_max_attempts=0), token_budget=budget)
        runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
        state = AgentState(user_message="Use the required environment", workspace_root=tmp_path, conversation_id="required-skill-thread")
        owner_state = EnvironmentConfigState.failed("Actual owner failure") if config_state == "Failed" else EnvironmentConfigState.pending()
        run = RunContext(agent_runtime=runtime, environment_manager=manager,
            environment_requests=(TurnEnvironmentRequest("remote", "file:///executor", (), owner_state),))
        try:
            async with asyncio.timeout(10), aclosing(QueryEngine().submit(QuerySubmission(user_message=state.user_message,
                session=session, state=state, runtime=AgentLoopSessionContext(session_id="required-skill-session", workspace_root=tmp_path,
                    permission_context=PermissionContext(mode="bypass"), run_context=run)))) as stream:
                events = [event async for event in stream]
            assert model.calls == 0 and states
            assert states[0][0] == (1 if expected_state == "starting" else 0)
            assert states[0][1] == ("StartingTurnEnvironment" if expected_state == "starting" else "FailedTurnEnvironment")
            assert states[0][2][0].skill_names == ("exact",)
            assert any(event.type == "done" and event.data["status"] == "failed" for event in events)
            assert any(event.type == "error" and event.data["error_type"] == "required_skill_unavailable"
                and event.data["message"] == 'Required skill "exact" from environment "remote" is unavailable' for event in events)
            assert 'Required skill "exact" from environment "remote" is unavailable' in caplog.text
        finally:
            await session.aclose()
            artifacts.shutdown()
            runtime.close(release_lease=True)
            await manager.close()


def test_mcp_authority_and_credential_sources_do_not_follow_server_name_or_host_secrets(tmp_path, monkeypatch):
    selection = SimpleNamespace(environment_id="remote", config=SimpleNamespace(kind="Ready", value=SimpleNamespace(mcp_policy={"servers": {}})))
    snapshot = SimpleNamespace(clone=lambda: snapshot, all_selections=lambda: (selection,))
    runtime = MCPEnvironmentRuntime(None, snapshot, local_cwd=tmp_path.as_uri())
    config = MCPServerConfig("actual", transport="http", url="https://executor.invalid/mcp", environment_id="remote")
    assert not runtime.apply_authority(config).enabled
    selection.config.kind = "FromThread"
    assert runtime.apply_authority(config).enabled
    selection.config.kind = "Failed"
    assert not runtime.apply_authority(replace(config, selected_root_id="selected")).enabled
    snapshot.all_selections = lambda: ()
    assert not runtime.apply_authority(config).enabled
    assert runtime.apply_authority(replace(config, selected_root_id="selected")).enabled
    monkeypatch.setenv("HOST_SHOULD_NOT_RESOLVE", "not-executor-value")
    manager = MCPServerManager(config_path=tmp_path / "unused.json", workspace_root=None)
    remote = manager._config_from_mapping("actual", {"environment_id": "remote", "transport": "http", "url": "https://executor.invalid/mcp",
        "bearer_token_env_var": "HOST_SHOULD_NOT_RESOLVE", "http_headers": {"x-original": "${HOST_SHOULD_NOT_RESOLVE}"}},
        source="executor", priority=0, base_dir=tmp_path, executor_source=True, credential_policy="ExecutorOnly")
    assert remote.headers["x-original"] == "${HOST_SHOULD_NOT_RESOLVE}"
    assert remote.bearer_token_env_var == "HOST_SHOULD_NOT_RESOLVE"
    assert remote.oauth_credential_name() == "executor:cmVtb3Rl:YWN0dWFs"
    assert MCPServerConfig("executor:reserved").oauth_credential_name() == "local:executor:reserved"
    with pytest.raises(ValueError, match="executor-side environment resolution"):
        manager._config_from_mapping("invalid", {"environment_id": "remote", "transport": "http", "url": "https://executor.invalid/mcp",
            "env_http_headers": {"authorization": "HOST_SHOULD_NOT_RESOLVE"}}, source="executor", priority=0,
            base_dir=tmp_path, executor_source=True, credential_policy="ExecutorOnly")


@pytest.mark.asyncio
async def test_missing_environment_ambient_local_http_and_old_peer_host_credential_policy_are_distinct(tmp_path, monkeypatch):
    runtime = MCPEnvironmentRuntime(SimpleNamespace(get_environment=lambda environment_id: None), None, local_cwd=tmp_path.as_uri())
    assert await runtime.bind_server(MCPServerConfig("ambient", transport="http", url="https://local.example/mcp")) is None
    with pytest.raises(ValueError, match="requires a local environment"):
        await runtime.bind_server(MCPServerConfig("stdio", command="python"))
    with pytest.raises(ValueError, match="unknown environment"):
        await runtime.bind_server(MCPServerConfig("missing", transport="http", url="https://remote.example/mcp", environment_id="unknown"))

    async def no_http(socket, message):
        raise AssertionError("Credential admission must complete before sending HTTP")

    monkeypatch.setenv("OWNED_HOST_BEARER", "host-owned-token")
    async with _environment(tmp_path, no_http, env_capability=False) as (environments, _original, _requests):
        runtime = MCPEnvironmentRuntime(environments, None, local_cwd=tmp_path.as_uri())
        config = MCPServerConfig("host-owned", transport="http", url="https://remote.example/mcp", environment_id="remote",
            bearer_token_env_var="OWNED_HOST_BEARER", credential_policy="HostFallbackAllowed")
        try:
            binding = await runtime.bind_server(config)
            assert binding.bearer_token_env_var is None
            owner = MCPServerManager(config_path=tmp_path / "unused.json", workspace_root=None)
            client = owner._create_client(config, execution_binding=binding)
            assert (await client._resolved_http_headers())["Authorization"] == "Bearer host-owned-token"
            with pytest.raises(ValueError, match="host fallback is disabled"):
                await runtime.bind_server(replace(config, credential_policy="ExecutorOnly"))
        finally:
            await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_editable_mcp_native_environment_fields_survive_status_save_and_reload_without_aliases(tmp_path, monkeypatch, transport):
    path = tmp_path / ".mcp.json"
    original = {"transport": transport, "environment_id": "remote-owner", "enabled": False, "auto_start": False}
    if transport == "http":
        original.update(url="https://executor.example/mcp", bearer_token_env_var="EXECUTOR_TOKEN", env_http_headers={"x-tenant": "HOST_TENANT"})
    else:
        original.update(command="python", args=["server.py"], cwd="/workspace/plugin",
            env_vars=[{"name": "REMOTE_KEY", "source": "remote"}, {"name": "LOCAL_KEY", "source": "local"}])
    write_mcp_config(json.dumps({"servers": {"actual": original}}), path)
    manager = MCPServerManager(config_path=path, workspace_root=None)
    manager.load_config = manager._load_local_configs
    monkeypatch.setattr(mcp_service, "MCP_CONFIG_FILE", path)
    monkeypatch.setattr(mcp_service.config_file_mod, "read_mcp_config", lambda config_path=path: read_mcp_config(config_path))
    monkeypatch.setattr(mcp_service.config_file_mod, "write_mcp_config", lambda content, config_path=path: write_mcp_config(content, config_path))
    await manager.start_all()

    async def hook(**kwargs):
        return None

    try:
        [status] = mcp_service.get_mcp_status(manager)
        assert status["environment_id"] == "remote-owner" and status["enabled"] is False
        payload = {"name": "actual", "original_name": "actual", **original}
        await mcp_service.update_mcp_server(manager, payload, config_change_hook=hook)
        saved = json.loads(path.read_text(encoding="utf-8"))["servers"]["actual"]
        for name, value in original.items():
            assert saved[name] == value
        [config] = manager._load_local_configs()
        assert config.environment_id == "remote-owner" and config.enabled is False
        if transport == "stdio":
            assert config.env_vars == (("REMOTE_KEY", "remote"), ("LOCAL_KEY", "local"))
        else:
            assert config.bearer_token_env_var == "EXECUTOR_TOKEN" and config.env_http_headers == {"x-tenant": "HOST_TENANT"}
        with pytest.raises(mcp_service.MCPServiceError, match="local or remote"):
            mcp_service._manual_config_from_payload({"name": "invalid", "transport": "stdio", "command": "python",
                "env_vars": [{"name": "ALIAS", "source": "USERPROFILE"}]})
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_skill_namespace_source_budgets_full_utf8_and_code_mode_receives_typed_json(tmp_path):
    catalog, document = _catalog(body="原内容😀\n" * 6000)
    listing = await ListSkillsTool(catalog).execute({"authority": {"kind": "executor"}})
    listed = json.loads(listing.native_tool_output)
    entry = listed["skills"][0]
    assert listed["next_cursor"] is None and entry["authority"] == {"kind": "executor", "id": "selected"}
    reader = ReadSkillTool(catalog)
    page = await reader.execute({"package": entry["package"]})
    response = json.loads(page.native_tool_output)
    assert response["next_cursor"] and len(page.native_tool_output.encode("utf-8")) <= 12000
    parts = [response["contents"]]
    while response["next_cursor"] is not None:
        page = await reader.execute({"package": entry["package"], "cursor": response["next_cursor"]})
        response = json.loads(page.native_tool_output)
        parts.append(response["contents"])
    assert "".join(parts) == document
    model = ScriptModel('const r = await tools.skills__list({authority:{kind:"executor"}}); text({name:r.skills[0].name,hidden:r.native_tool_output === undefined,kind:r.skills[0].authority.kind});')
    state, builder, journal, events, _ = await run_model(tmp_path, model, [ListSkillsTool(catalog), reader])
    assert state.terminal_status == "completed", model.reports
    assert json.loads(model.reports[-1]["output"][0]) == {"name": "exact", "hidden": True, "kind": "executor"}
    leaf = next(record for record in state.tool_calls if record.call_source.get("kind") == "code_mode")
    assert json.loads(leaf.result_payload["native_tool_output"])["skills"][0]["name"] == "exact"
    assert all("native_tool_output" not in event.data for event in events)
    assert [row.name for row in builder._history if row.role == "tool"] == ["tool_exec"]
    assert [row.get("name") for row in journal.reconstruct_history() if row["role"] == "tool"] == ["tool_exec"]


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires explicitly owned Source executor")
async def test_source_selected_plugin_skill_mcp_child_lazy_start_and_original_lease(tmp_path, monkeypatch):
    plugin = tmp_path / "selected-plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills/exact").mkdir(parents=True)
    (plugin / ".codex-plugin/plugin.json").write_text('{"name":"original-plugin"}', encoding="utf-8")
    instructions = "---\nname: exact\ndescription: Original environment instructions\n---\n真正的原环境Skill😀\n"
    (plugin / "skills/exact/SKILL.md").write_bytes(instructions.encode("utf-8"))
    server = plugin / "server.py"
    server.write_text('''import json,sys,time
from pathlib import Path
for line in sys.stdin.buffer:
    request=json.loads(line)
    if "id" not in request: continue
    method=request["method"]
    if method=="initialize": result={"protocolVersion":request["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"original-plugin","version":"1"},"instructions":"Original bound server"}
    elif method=="tools/list": result={"tools":[{"name":"echo","description":"Echo from actual executor","inputSchema":{"type":"object","properties":{"wait_file":{"type":"string"}}},"annotations":{"readOnlyHint":True}}]}
    else:
        wait_file=request["params"].get("arguments",{}).get("wait_file")
        if wait_file:
            Path(wait_file+".started").write_text("actual MCP request entered")
            while not Path(wait_file).exists(): time.sleep(.01)
        result={"content":[{"type":"text","text":"原环境真实执行😀"}]}
    sys.stdout.buffer.write(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result},ensure_ascii=False).encode("utf-8")+b"\\n");sys.stdout.buffer.flush()
''', encoding="utf-8")
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"bound": {"command": sys.executable, "args": [str(server)],
        "required": True}}}), encoding="utf-8")
    _, manager = _configure_source(tmp_path, required=("original-plugin:exact",))
    root = {"id": "selected-original", "location": {"type": "environment", "environmentId": "remote", "path": plugin.as_uri()}}
    owner, snapshot = await _capture(manager, tmp_path.as_uri(), (root,))
    prepared = child = None
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    try:
        prepared = await prepare_environment_capabilities(turn_environments=snapshot, host_skill_manager=None, host_mcp_manager=None,
            feature_flags=FeatureFlags({"executor_capability_discovery": True}), thread_id="parent", turn_id="turn-1",
            session_source="cli", isolated=True, environment_manager=manager, local_cwd=tmp_path.as_uri())
        assert not prepared.warnings
        prepared.validate_required_skills()
        registry = ToolRegistry()
        prepared.bind_registry(registry, store)
        entry = prepared.skill_manager.executor_catalog.entries[0]
        assert entry.name == "original-plugin:exact"
        assert json.loads((await registry.get_tool("skills__read").execute({"package": entry.package})).native_tool_output)["contents"] == instructions
        parent_proxy = registry.get_tool("mcp__bound__echo")
        assert parent_proxy is not None and prepared.mcp_manager.get_client("bound") is not None
        original = snapshot.primary().environment
        child = await prepare_environment_capabilities(turn_environments=snapshot, host_skill_manager=prepared.skill_manager,
            host_mcp_manager=prepared.mcp_manager, feature_flags=FeatureFlags({"executor_capability_discovery": True}),
            thread_id="child", turn_id="child-turn", session_source="subagent", is_subagent=True, isolated=True,
            environment_manager=manager, local_cwd=tmp_path.as_uri())
        child.validate_required_skills()
        assert child.mcp_manager.get_client("bound") is None and child.mcp_manager.get_all_tools()["bound"]
        child_registry = registry.fork()
        child.bind_registry(child_registry, store)
        manager.upsert({"id": "remote", "program": "replacement-must-never-start"})
        assert not original.closed and parent_proxy._manager is prepared.mcp_manager
        result = await child_registry.get_tool("mcp__bound__echo").execute({})
        assert not result.is_error, result.content
        assert result.mcp_result["content"][0]["text"] == "原环境真实执行😀"
        assert child.mcp_manager.get_client("bound") is not None
        assert not (await parent_proxy.execute({})).is_error

        from backend.agent.context import ContextBuilder
        from backend.agent.query_engine import AgentSession
        from backend.config import AgentSettings, PermissionSettings, TokenBudget
        from backend.permissions.checker import PermissionChecker
        from backend.tools.code_execution import ToolExecTool, ToolWaitTool
        from backend.tools.tool_search import ToolSearchTool

        # This is a real yielded Code Mode cell. Its second MCP request has
        # not started when the next model step replaces the active catalog.
        held = {}
        original_execute = parent_proxy.execute

        async def capture_run(args, *, context=None):
            held["run"] = context.run_context
            return await original_execute(args, context=context)

        monkeypatch.setattr(parent_proxy, "execute", capture_run)
        for tool in (ToolExecTool(), ToolWaitTool(), ToolSearchTool(registry)):
            registry.register(tool)

        async def rebind_after_yield(report):
            assert report["status"] == "running" and report["output"] == ["original cell retained"]
            run = held["run"]
            child.bind_registry(registry, store)
            run.mcp_manager = child.mcp_manager
            held["retirement"] = prepared.retire(run)
            run.environment_retirement_tasks.add(held["retirement"])
            assert prepared.mcp_manager.get_client("bound") is not None
            return ToolCallEvent("wait-original-cell", "tool_wait", {"cell_id": report["cell_id"], "yield_time_ms": 10000})

        model = ScriptModel('await tools.mcp__bound__echo({}); text("original cell retained"); await yield_control(); '
            'await new Promise(resolve => setTimeout(resolve, 1200)); '
            'const result = await tools.mcp__bound__echo({}); text(result.content[0].text);', after_first=rebind_after_yield)
        budget = TokenBudget(total=96000)
        context = ContextBuilder(llm=model, token_budget=budget, workspace_root=tmp_path)
        session = AgentSession(llm=model, tool_registry=registry, artifact_store=store,
            permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), tmp_path),
            context_builder=context, agent_settings=AgentSettings(code_mode_only=True), token_budget=budget)
        state, _, _, _, _ = await run_model(tmp_path, model, session=session, context=context)
        assert state.terminal_status == "completed", model.reports
        assert model.reports[-1]["output"] == ["原环境真实执行😀"] and held["retirement"].done()
        assert prepared.mcp_manager.get_client("bound") is None

        from backend.agent.run_context import RunContext

        release = tmp_path / "release-actual-mcp"
        pending_call = asyncio.create_task(child_registry.get_tool("mcp__bound__echo").execute({"wait_file": str(release)}))
        async with asyncio.timeout(5):
            while not Path(str(release) + ".started").exists():
                await asyncio.sleep(.01)
        cleanup_owner = RunContext()
        retired = child.retire(cleanup_owner)
        # Terminal may hand the retirement to the existing cleanup owner.
        # It must not discover itself by dynamically rereading that set.
        cleanup_owner.lifecycle_cleanup_tasks.add(retired)
        release.write_text("continue actual RPC", encoding="utf-8")
        assert not (await pending_call).is_error
        async with asyncio.timeout(5):
            await asyncio.shield(retired)
        assert child.mcp_manager.get_client("bound") is None
    finally:
        if child is not None:
            await child.aclose()
        if prepared is not None:
            await prepared.aclose()
        store.shutdown()
        await snapshot.aclose()
        await owner.aclose()
        await manager.close()


@pytest.mark.asyncio
async def test_source_wire_config_read_http_mcp_resources_keep_original_json_metadata_and_cursors(tmp_path):
    calls = []

    def response(data):
        calls.append(data)
        if "id" not in data:
            return 202, None
        method = data["method"]
        if method == "initialize":
            result = {"protocolVersion": data["params"]["protocolVersion"], "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "source-http-resource", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "resources/list":
            second = data.get("params", {}).get("cursor") == "original-next"
            result = {"resources": [{"uri": "docs://second" if second else "docs://first", "name": "实际资源", "title": "原标题",
                "annotations": {"audience": ["assistant"]}, "_meta": {"owned": "resource"}}], **({} if second else {"nextCursor": "original-next"})}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": [{"uriTemplate": "docs://{name}", "name": "Original template", "title": "模板",
                "_meta": {"owned": "template"}}]}
        elif method == "resources/read":
            result = {"contents": [{"uri": data["params"]["uri"], "text": "真实原始内容😀", "_meta": {"part": "kept"}}], "_meta": {"request": "kept"}}
        else:
            result = {"content": [{"type": "text", "text": "actual executor HTTP"}]}
        return 200, {"jsonrpc": "2.0", "id": data["id"], "result": result}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def reply(self, status, value=None):
            encoded = json.dumps(value, ensure_ascii=False).encode("utf-8") if value is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            self.reply(405)

        def do_DELETE(self):
            self.reply(204)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.reply(*response(data))

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/mcp"
    wire_calls = []
    config_text = '[mcp_servers.remote_http]\nurl = ' + json.dumps(url) + '\n'

    async def peer(socket):
        async with httpx.AsyncClient(trust_env=False) as http:
            async for raw in socket:
                message = json.loads(raw)
                wire_calls.append(message)
                method = message["method"]
                info = {"providerId": "source-contract-peer", "platformOs": "windows" if os.name == "nt" else "linux",
                    "capabilities": {"environmentConfigRead": True, "httpHeaderEnvVars": True}}
                if method == "initialized":
                    continue
                if method == "initialize":
                    result = {"sessionId": "source-contract-session", "environmentInfo": info}
                elif method == "environment/info":
                    result = info
                elif method == "environment/config/read":
                    assert message["params"] == {"cwd": tmp_path.as_uri(), "configPaths": [["mcp_servers"]], "requirementsPaths": [["mcp_servers"]]}
                    result = {"userHomeDir": tmp_path.as_uri(), "codexHomeDir": tmp_path.as_uri(), "hostname": "executor.example.test",
                        "config": {"layers": [{"source": "owned-test", "baseDir": tmp_path.as_uri(), "toml": config_text}], "cloudInsertionIndex": 0},
                        "requirements": {"layers": [], "cloudInsertionIndex": 0}}
                elif method == "http/request":
                    params = message["params"]
                    response = await http.request(params["method"], params["url"],
                        headers=[(row["name"], row["value"]) for row in params["headers"]],
                        content=base64.b64decode(params["bodyBase64"]) if params.get("bodyBase64") is not None else None)
                    await _reply(socket, message, status=response.status_code, headers=[{"name": name, "value": value} for name, value in response.headers.multi_items()])
                    await _delta(socket, params["requestId"], 1, response.content, done=True)
                    continue
                else:
                    raise AssertionError(f"Unexpected executor method {method}")
                await socket.send(json.dumps({"id": message["id"], "result": result}))

    executor_server = await serve(peer, "127.0.0.1", 0)
    _config(tmp_path, {"url": f"ws://127.0.0.1:{executor_server.sockets[0].getsockname()[1]}"})
    manager = EnvironmentManager(tmp_path)
    owner, snapshot = await _capture(manager, tmp_path.as_uri())
    prepared = None
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    try:
        prepared = await prepare_environment_capabilities(turn_environments=snapshot, host_skill_manager=None, host_mcp_manager=None,
            feature_flags=FeatureFlags(), thread_id="http-owner", turn_id="http-turn", session_source="cli", isolated=False,
            environment_manager=manager, local_cwd=tmp_path.as_uri())
        assert not prepared.warnings
        config = prepared.mcp_manager.get_server_config("remote_http")
        assert config.environment_id == "remote" and config.credential_policy == "ExecutorOnly"
        assert prepared.mcp_manager.get_client("remote_http") is not None
        registry = ToolRegistry()
        prepared.bind_registry(registry, store)
        listed = await registry.get_tool("list_mcp_resources").execute({"server": "remote_http"})
        payload = json.loads(listed.native_tool_output)
        assert payload["nextCursor"] == "original-next" and payload["resources"][0]["title"] == "原标题"
        assert payload["resources"][0]["_meta"] == {"owned": "resource"}
        _assert_provider_outputs(LLMMessage("tool", "display", name="list_mcp_resources", tool_call_id="resources", native_tool_output=listed.native_tool_output), listed.native_tool_output)
        second = json.loads((await registry.get_tool("list_mcp_resources").execute({"server": "remote_http", "cursor": payload["nextCursor"]})).native_tool_output)
        assert "nextCursor" not in second and second["resources"][0]["uri"] == "docs://second"
        all_resources = json.loads((await registry.get_tool("list_mcp_resources").execute({})).native_tool_output)
        assert "nextCursor" not in all_resources and len(all_resources["resources"]) == 2
        templates = json.loads((await registry.get_tool("list_mcp_resource_templates").execute({})).native_tool_output)
        assert templates["resourceTemplates"][0]["_meta"] == {"owned": "template"}
        read = await registry.get_tool("read_mcp_resource").execute({"server": "remote_http", "uri": "docs://first"})
        assert json.loads(read.native_tool_output) == {"server": "remote_http", "uri": "docs://first", "contents": [
            {"uri": "docs://first", "text": "真实原始内容😀", "_meta": {"part": "kept"}}], "_meta": {"request": "kept"}}
        assert {"initialize", "tools/list", "resources/list", "resources/read", "resources/templates/list"} <= {call["method"] for call in calls}
        assert {"environment/config/read", "http/request"} <= {call["method"] for call in wire_calls}
    finally:
        if prepared is not None:
            await prepared.aclose()
        store.shutdown()
        await snapshot.aclose()
        await owner.aclose()
        await manager.close()
        executor_server.close()
        await executor_server.wait_closed()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires explicitly owned installed Source executor")
async def test_installed_source_config_read_native_interoperability_probe(tmp_path):
    _, manager = _configure_source(tmp_path)
    lease = manager.borrow("remote")
    try:
        info = await lease.environment.info()
        if not info["capabilities"].get("environmentConfigRead", False):
            pytest.skip("Installed Codex executor does not advertise environmentConfigRead")
        try:
            result = await (await lease.environment.get_client()).request("environment/config/read", {"cwd": tmp_path.as_uri(),
                "configPaths": [["mcp_servers"]], "requirementsPaths": [["mcp_servers"]]})
        except ExecutorRpcError as error:
            if "stub does not implement `environment/config/read` yet" in str(error):
                pytest.skip("Installed Codex 26.1007 advertises environmentConfigRead but implements only a stub; checkout-pin wire consumption is tested separately")
            raise
        assert "config" in result and "requirements" in result
    finally:
        await lease.aclose()
        await manager.close()
