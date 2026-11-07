from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Response, HTTPException
import pytest

from backend import config
from backend.api import routes_skills, _state
from backend.agent.run_context import RunContext
from backend.feature_flags import FeatureFlags
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.plugins.install_suggestions import discover_install_candidates, installed_plugin
from backend.plugins.manager import PluginManager
from backend.tools.plugin_install import ListAvailablePluginsToInstallTool, RequestPluginInstallTool, bind_plugin_install_tools
from backend.tools.registry import ToolRegistry
from backend.tests.test_plugin_catalog_scope_chain import catalog_home, write_marketplace


@pytest.fixture
def install_scope(catalog_home, monkeypatch):
    env = catalog_home
    monkeypatch.setattr("backend.skills.loader.STATE_ROOT", env.state)
    workspace = env.root / "workspace"
    workspace.mkdir()
    plugin = write_marketplace(workspace)
    # This package supplies one local Skill; no external account or process.
    (plugin / ".mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    document = json.loads(env.settings.read_text(encoding="utf-8"))
    document["tool_suggest"] = {"discoverables": [{"type": "plugin", "id": "review@project-market"}]}
    env.settings.write_text(json.dumps(document), encoding="utf-8")
    stack = config.load_config_layer_stack(cwd=workspace)
    candidates = discover_install_candidates(stack, workspace)
    candidate = next(entry for entry in candidates if entry.id == "review@project-market")
    env.workspace, env.plugin, env.stack, env.candidate = workspace, plugin, stack, candidate
    monkeypatch.setattr(config, "SETTINGS_FILE", env.settings)
    return env


def tool_context(env, *, response=None, subagent=False):
    events = []
    async def emit(kind, data):
        events.append((kind, data))
    approval = AsyncMock(return_value=response or {"action": "decline"})
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=env.workspace,
        conversation_id="owner", turn_id="turn", tool_call_id="install-call", session_id="window",
        run_context=RunContext(is_subagent=subagent), emit_event=emit, approval_handler=approval)
    return context, events


def request_args(env):
    return {"tool_type": "plugin", "action_type": "install", "tool_id": env.candidate.id, "suggest_reason": "Use the requested review plugin."}


@pytest.mark.asyncio
async def test_actual_candidate_listing_decline_and_accept_without_install_are_distinct(install_scope):
    env = install_scope
    listing = await ListAvailablePluginsToInstallTool((env.candidate,)).execute({})
    assert json.loads(listing.content)["tools"] == [env.candidate.entry()]
    for action in ("decline", "cancel", "accept"):
        context, events = tool_context(env, response={"action": action})
        tool = RequestPluginInstallTool((env.candidate,))
        result = json.loads((await tool.execute(request_args(env), context)).content)
        assert result["user_confirmed"] is (action == "accept")
        assert not result["completed"] and not context.run_context.plugin_capabilities_changed
        assert len(events) == 1 and events[0][0] == "ask_user"
        assert events[0][1]["_meta"]["suggestion_id"] == "request_plugin_install_install-call"
        assert events[0][1]["_meta"]["tool_id"] == env.candidate.id
        assert not tool.is_concurrency_safe(request_args(env)) and not tool.is_idempotent()


def test_install_request_has_one_client_prompt_in_default_permission_mode(install_scope):
    from backend.permissions.checker import PermissionChecker
    from backend.tools.base import PermissionLevel
    tool = RequestPluginInstallTool((install_scope.candidate,))
    permission = PermissionContext(mode="confirm")
    checker = PermissionChecker(config.permission_settings_from_config({}), install_scope.workspace)
    assert checker.check(tool.name, request_args(install_scope), context=permission, tool=tool) == PermissionLevel.AUTO


@pytest.mark.asyncio
async def test_pending_client_install_and_backend_receipt_verify_the_same_real_plugin(install_scope, monkeypatch):
    env = install_scope
    context, events = tool_context(env)
    session = SimpleNamespace(turn_wait_state=SimpleNamespace(pending_approval_payloads={}))
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda key: session if key == "window" else None))
    async def install_from_client(request_id):
        emitted = events[-1][1]
        session.turn_wait_state.pending_approval_payloads[request_id] = {"request_id": request_id,
            "conversation_id": "owner", "turn_id": "turn", "message_id": "answer", "workspace_root": str(env.workspace),
            "request": {"subtype": "elicitation", "_meta": emitted["_meta"]}}
        request = routes_skills.PluginInstallPromptRequest(session_id="window", request_id=request_id,
            conversation_id="owner", turn_id="turn", message_id="answer")
        receipt = await routes_skills.install_requested_plugin_api(request, Response())
        assert receipt["installed"] and receipt["plugin_id"] == env.candidate.id
        # Retry after the install committed but before the client ACK arrived.
        repeated = await routes_skills.install_requested_plugin_api(request, Response())
        assert repeated["installed"]
        return {"action": "accept"}
    context.approval_handler = install_from_client
    result = json.loads((await RequestPluginInstallTool((env.candidate,)).execute(request_args(env), context)).content)
    assert result["completed"] and result["user_confirmed"]
    assert installed_plugin(env.candidate, env.workspace)[1]
    assert context.run_context.plugin_capabilities_changed
    assert env.candidate.id not in {entry.id for entry in discover_install_candidates(context.run_context.plugin_capability_config, env.workspace)}
    assert PluginManager(config_stack=context.run_context.plugin_capability_config).resolve(env.candidate.id)["skill_count"] == 1


@pytest.mark.asyncio
async def test_install_never_uses_a_stale_prompt_owner_or_changed_source(install_scope, monkeypatch):
    env = install_scope
    metadata = {"codex_approval_kind": "tool_suggestion", "tool_type": "plugin", "suggest_type": "install",
        "tool_id": env.candidate.id, "source": env.candidate.source}
    pending = {"conversation_id": "owner", "turn_id": "turn", "message_id": "answer", "workspace_root": str(env.workspace),
        "request": {"subtype": "elicitation", "_meta": metadata}}
    session = SimpleNamespace(turn_wait_state=SimpleNamespace(pending_approval_payloads={"request": pending}))
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda _: session))
    for changes in ({"turn_id": "other"}, {"message_id": "other"}, {"conversation_id": "other"}, {"request_id": "cancelled"}):
        request = routes_skills.PluginInstallPromptRequest(**{"session_id": "window", "request_id": "request",
            "conversation_id": "owner", "turn_id": "turn", "message_id": "answer", **changes})
        with pytest.raises(HTTPException, match="no longer pending"):
            await routes_skills.install_requested_plugin_api(request, Response())
    metadata["source"] = {"source": "git", "url": "https://changed.invalid/plugin"}
    request = routes_skills.PluginInstallPromptRequest(session_id="window", request_id="request", conversation_id="owner", turn_id="turn", message_id="answer")
    with pytest.raises(HTTPException, match="source has changed"):
        await routes_skills.install_requested_plugin_api(request, Response())
    assert not installed_plugin(env.candidate, env.workspace)[1]


@pytest.mark.asyncio
async def test_permanent_decline_updates_discovery_without_removing_unrelated_settings(install_scope):
    env = install_scope
    context, _ = tool_context(env, response={"action": "decline", "_meta": {"persist": "always"}})
    result = json.loads((await RequestPluginInstallTool((env.candidate,)).execute(request_args(env), context)).content)
    assert not result["completed"] and not result["user_confirmed"]
    settings = json.loads(env.settings.read_text(encoding="utf-8"))
    assert settings["unrelated"] == "keep"
    assert settings["tool_suggest"]["disabled_tools"] == [{"type": "plugin", "id": env.candidate.id}]
    stack = config.load_config_layer_stack(cwd=env.workspace)
    assert env.candidate.id not in {entry.id for entry in discover_install_candidates(stack, env.workspace)}


@pytest.mark.asyncio
async def test_root_only_and_exact_candidate_validation_do_not_emit_a_prompt(install_scope):
    env = install_scope
    context, events = tool_context(env, subagent=True)
    tool = RequestPluginInstallTool((env.candidate,))
    assert (await tool.execute(request_args(env), context)).is_error
    context.run_context.is_subagent = False
    for change in ({"tool_id": "other@market"}, {"suggest_reason": " "}, {"action_type": "remove"}, {"tool_type": "connector"}):
        assert (await tool.execute({**request_args(env), **change}, context)).is_error
    assert not events


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", ["apps", "plugins", "tool_suggest", "subagent", None])
async def test_only_root_steps_with_all_source_gates_capture_the_catalog(install_scope, disabled):
    env = install_scope
    context, _ = tool_context(env, subagent=disabled == "subagent")
    context.model_execution = SimpleNamespace(config=SimpleNamespace(config_layer_stack=env.stack,
        feature_flags=FeatureFlags({disabled: False}) if disabled in {"apps", "plugins", "tool_suggest"} else FeatureFlags()))
    registry = ToolRegistry()
    await bind_plugin_install_tools(registry, context)
    assert ("request_plugin_install" in registry.list_tools()) is (disabled is None)
    if disabled is None:
        next_registry = ToolRegistry()
        await bind_plugin_install_tools(next_registry, context)
        assert registry.get_tool("request_plugin_install") is next_registry.get_tool("request_plugin_install")


@pytest.mark.asyncio
async def test_real_query_installs_then_uses_the_new_skill_and_mcp_in_its_next_steps(install_scope):
    import sys
    from backend.sdk import create_session
    from backend.artifact.store import ArtifactStore
    from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
    from backend.mcp.manager import MCPServerManager
    from backend.skills.loader import SkillLoader
    from backend.skills.manager import SkillManager
    from backend.tools.tool_search import ToolSearchTool

    env = install_scope
    from backend.tests.test_hooks_contract import _python_hook_command
    hook_log = env.workspace / "installed-hook.txt"
    hook = env.plugin / "hook.py"
    hook.write_text(f"from pathlib import Path\nPath({str(hook_log)!r}).write_text('REAL_PLUGIN_HOOK')\nprint('{{}}')\n", encoding="utf-8")
    command, shell = _python_hook_command(hook)
    manifest_path = env.plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hooks"] = {"PreToolUse": [{"matcher": "tool_search", "hooks": [{"type": "command", "command": command, "shell": shell}]}]}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    peer = env.plugin / "peer.py"
    peer.write_text("import json,sys\nfor line in sys.stdin:\n"
        " m=json.loads(line)\n if 'id' not in m: continue\n"
        " method=m['method']\n"
        " if method=='initialize': r={'protocolVersion':m['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'installed-review','version':'1'}}\n"
        " elif method=='tools/list': r={'tools':[{'name':'echo','description':'installed review echo','inputSchema':{'type':'object','properties':{}},'annotations':{'readOnlyHint':True}}]}\n"
        " elif method=='tools/call': r={'content':[{'type':'text','text':'INSTALLED_ECHO_RESULT'}]}\n"
        " else: r={}\n print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r}),flush=True)\n", encoding="utf-8")
    (env.plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"reviewer": {"command": sys.executable, "args": [str(peer)]}}}), encoding="utf-8")
    cfg = config.load_config(cwd=env.workspace)
    cfg = replace(cfg, agent=replace(cfg.agent, max_iterations=6, code_mode_only=False), llm=replace(cfg.llm, supports_search_tool=True))
    artifacts = ArtifactStore(storage_dir=env.root / "artifacts")
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    loader = SkillLoader(env.workspace)
    loader._config_stack = cfg.config_layer_stack
    skills = SkillManager(loader)
    skills.discover()
    mcp = MCPServerManager(config_path=env.root / "empty-mcp.json", workspace_root=env.workspace)
    run = RunContext(environment_host_captured=True, environment_host_skill_manager=skills,
        environment_host_mcp_manager=mcp, skill_manager=skills, mcp_manager=mcp)
    calls = []
    class Model(LLMAdapter):
        async def stream_chat(self, messages, tools=None, metadata=None):
            index = len(calls)
            calls.append(messages)
            if index == 0:
                name, arguments = "list_available_plugins_to_install", {}
            elif index == 1:
                assert any(env.candidate.id in str(message.content) for message in messages if message.role == "tool")
                name, arguments = "request_plugin_install", request_args(env)
            elif index == 2:
                assert any('"completed": true' in str(message.content) for message in messages if message.role == "tool")
                assert any(meta.description == "Review real changes" for meta in run.context_builder.skill_manager.list_metas())
                from backend.hooks.manager import HookEvent
                assert run.hook_manager.has_hooks(HookEvent.PRE_TOOL_USE), run.hook_manager.list_hooks()
                name, arguments = "tool_search", {"query": "installed review echo"}
            elif index == 3:
                assert hook_log.read_text() == "REAL_PLUGIN_HOOK"
                name = next(name for name in run.active_tool_registry.list_tools() if name.endswith("__echo"))
                arguments = {}
            else:
                assert any("INSTALLED_ECHO_RESULT" in str(message.content) for message in messages if message.role == "tool")
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Installed plugin used successfully.", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
                return
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(f"plugin-step-{index}", name, arguments)])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        async def simple_chat(self, messages):
            return ""
    async def user_installs(request_id):
        assert request_id == "plugin-step-1"
        response = await asyncio.to_thread(env.client.post, "/api/plugins/install", json={"marketplace": env.candidate.marketplace,
            "plugin_name": env.candidate.plugin_name, "refresh_marketplace": False, "workspace_root": str(env.workspace)})
        assert response.status_code == 200, response.text
        return {"action": "accept"}
    session = create_session(session_id="actual-plugin-query", workspace_root=env.workspace, config=cfg,
        llm=Model(), artifact_store=artifacts, tool_registry=registry, run_context=run,
        permission_context=PermissionContext(mode="bypass"), approval_handler=user_installs)
    try:
        events = [event async for event in session.query("Use the review plugin, install it if needed.")]
        assert any(event.type == "done" and event.data.get("status") == "completed" for event in events), [event.data for event in events if event.type == "error"]
        assert len(calls) == 5
    finally:
        await session.aclose()
        await mcp.stop_all()
        artifacts.shutdown()


@pytest.mark.asyncio
async def test_hook_publication_during_an_existing_call_keeps_its_original_post_hook(install_scope):
    from backend.agent.tool_execution import run_tool
    from backend.hooks.manager import HookResult
    from backend.llm.base import ToolCallEvent
    from backend.tools.base import BaseTool, ToolResult, ToolSchema

    context, _ = tool_context(install_scope)
    calls = []
    new = SimpleNamespace(run_pre_tool=AsyncMock(return_value=HookResult()), run_post_tool=AsyncMock(return_value=HookResult()))
    async def original_pre(*_, **__):
        context.run_context.hook_manager = new
        calls.append("old-pre")
        return HookResult()
    async def original_post(*_, **__):
        calls.append("old-post")
        return HookResult()
    context.run_context.hook_manager = SimpleNamespace(run_pre_tool=original_pre, run_post_tool=original_post)
    class Echo(BaseTool):
        name, description, read_only = "echo", "echo", True
        def get_schema(self):
            return ToolSchema(self.name, self.description, {"type": "object", "properties": {}})
        async def execute(self, args, context=None):
            return ToolResult("real echo")
    registry = ToolRegistry()
    registry.register(Echo())
    result = await run_tool(ToolCallEvent("owned-call", "echo", {}), registry, context)
    assert result.content == "real echo"
    assert calls == ["old-pre", "old-post"]
    new.run_post_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_install_wait_cannot_publish_a_success_or_next_step_refresh(install_scope):
    context, events = tool_context(install_scope)
    entered = asyncio.Event()
    async def wait(_):
        entered.set()
        await asyncio.Event().wait()
    context.approval_handler = wait
    task = asyncio.create_task(RequestPluginInstallTool((install_scope.candidate,)).execute(request_args(install_scope), context))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not context.run_context.plugin_capabilities_changed
    assert not installed_plugin(install_scope.candidate, install_scope.workspace)[1]
