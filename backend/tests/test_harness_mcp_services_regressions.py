from __future__ import annotations

import asyncio
import json
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import threading

import httpx
import pytest

from backend.agent.tool_schema_derivation import derive_turn_tool_schema_state
from backend.agent.prompting import build_tool_runtime_guidance
from backend.agent.message import AgentEvent
from backend.config import AgentSettings, TokenBudget
from backend.commands import catalog, slash_commands
from backend.commands.registry import CommandRegistry
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.mcp.manager import MCPAuthStatus, MCPServerConfig, MCPServerManager, ServerStatus
from backend.mcp.oauth import CredentialOAuthProvider, SDKTokenStorage, TokenStore
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.plugins import package as plugin_package
from backend.plugins import store as plugin_store
from backend.plugins.manager import MarketplaceRegistry
from backend.plugins.policy import ManagedPluginPolicy
from backend.services import conversation_worktree_handoff_service as handoff
from backend.services import llm_provider_helpers, mcp_service, plugin_settings_service, workspace_service
from backend.skills.marketplace import registry_server_to_marketplace_mcp
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import ToolSearchTool
from backend.ws.handlers import conversation as conversation_handlers


class MCPPeer:
    def __init__(self, endpoint: str, tools: list[MCPToolDef] | None = None):
        self.endpoint = endpoint
        self.instructions = f"instructions for {endpoint}"
        self.tools = tools or [MCPToolDef("act", "act", {"type": "object"})]
        self.connected = False
        self.calls = []
        self.subscriptions = []
        self.list_entered = asyncio.Event()
        self.list_release = None
        self.closed = 0
        self.has_valid_token = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed += 1
        self.connected = False
        return True

    async def list_tools(self):
        self.list_entered.set()
        if self.list_release is not None:
            await self.list_release.wait()
        return self.tools

    async def subscribe_resource(self, uri):
        self.subscriptions.append(uri)
        return True

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        return MCPCallResult([{"type": "text", "text": self.endpoint}])


def manager_fixture(tmp_path):
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path)
    manager._stored_auth_status = lambda config: MCPAuthStatus.UNSUPPORTED
    return manager


@pytest.mark.asyncio
async def test_prepared_mcp_call_cannot_move_endpoint_and_subscriptions_do_not_cross(tmp_path):
    manager = manager_fixture(tmp_path)
    clients = []
    def create(config, **kwargs):
        peer = MCPPeer(config.url)
        clients.append(peer)
        return peer
    manager._create_client = create
    config = MCPServerConfig("server", transport="http", url="https://old.invalid/mcp")
    await manager.start_server(config)
    registry = ToolRegistry()
    adapter = MCPToolRegistry(registry, mcp_manager=manager)
    adapter.register_server_tools(config.name, clients[0].tools, clients[0])
    fork = registry.fork()
    prepared = fork.get_tool("mcp__server__act")
    await manager.subscribe_resource(config.name, "private://old-account")
    manager.load_config = lambda: [replace(config, url="https://new.invalid/mcp")]
    await manager.reload_config()
    result = await prepared.execute({"prepared": "old"})
    assert result.is_error
    assert not clients[1].calls
    assert not clients[1].subscriptions
    fork.mcp_tool_registry.sync()
    refreshed = fork.get_tool(prepared.name)
    assert refreshed is not prepared
    assert registry.get_tool(prepared.name) is prepared
    assert not (await refreshed.execute({"prepared": "new"})).is_error
    await manager.stop_all()


@pytest.mark.asyncio
async def test_mcp_catalog_notification_invalidates_before_list_completes(tmp_path):
    manager = manager_fixture(tmp_path)
    peer = MCPPeer("same")
    manager._create_client = lambda config, **kwargs: peer
    await manager.start_server(MCPServerConfig("server", command="python"))
    prepared = MCPToolProxy("server", peer.tools[0], peer, manager=manager)
    peer.list_entered.clear()
    peer.list_release = asyncio.Event()
    manager._schedule_tool_refresh("server", peer)
    await peer.list_entered.wait()
    assert (await prepared.execute({})).is_error
    assert not peer.calls
    await manager.stop_all()


@pytest.mark.asyncio
async def test_cancel_mcp_initialization_after_connect_closes_the_peer(tmp_path):
    manager = manager_fixture(tmp_path)
    peer = MCPPeer("cancel")
    peer.list_release = asyncio.Event()
    manager._create_client = lambda config, **kwargs: peer
    task = asyncio.create_task(manager.start_server(MCPServerConfig("server", command="python")))
    await peer.list_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peer.closed == 1
    assert not peer.connected
    assert manager._servers["server"].status is ServerStatus.OFFLINE


@pytest.mark.asyncio
async def test_structured_mcp_result_survives_sdk_and_tool_projection():
    client = MCPClient("structured")
    client._connected = True
    client._request = AsyncMock(return_value={"content": [], "structuredContent": {"answer": 42}, "_meta": {"ui/resourceUri": "ui://result"}})
    proxy = MCPToolProxy("structured", MCPToolDef("act", "act"), client)
    result = await proxy.execute({})
    assert json.loads(result.content) == {"answer": 42}
    assert result.runtime_metadata["mcp"]["_meta"] == {"ui/resourceUri": "ui://result"}


def test_instructions_and_deferred_catalog_are_rederived():
    schema = {"type": "function", "function": {"name": "mcp__demo__act", "description": "act", "parameters": {"type": "object"}}}
    first = derive_turn_tool_schema_state(base_tool_schemas=[schema], mcp_instructions={"demo": "old instructions"})
    second = derive_turn_tool_schema_state(base_tool_schemas=[schema], mcp_instructions={"demo": "new instructions"}, previous=first)
    assert "new instructions" in second.runtime_guidance
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    old = MCPToolProxy("demo", MCPToolDef("old", "old"), None)
    registry.register(old)
    first = derive_turn_tool_schema_state(base_tool_schemas=registry.get_schemas(), mcp_instructions={}, tool_registry=registry)
    registry.unregister(old.name)
    new = MCPToolProxy("demo", MCPToolDef("new", "new"), None)
    registry.register(new)
    second = derive_turn_tool_schema_state(base_tool_schemas=registry.get_schemas(), mcp_instructions={}, tool_registry=registry, previous=first)
    assert new.name in second.deferred_tools_prompt_block
    assert old.name not in second.deferred_tools_prompt_block


def test_plugin_server_identity_is_shared_by_instructions_and_mentions(monkeypatch):
    name = "plugin:demo@market:worker"
    proxy = MCPToolProxy(name, MCPToolDef("act", "act"), None)
    assert "server instructions" in build_tool_runtime_guidance([proxy.get_schema().to_openai_tool()], {name: "server instructions"})
    monkeypatch.setattr(plugin_settings_service, "get_plugin_snapshot", lambda **kwargs: {"plugins": [
        {"id": "demo@market", "name": "demo", "marketplace": "market", "enabled": True, "mcp_server_names": ["worker"]}
    ]})
    result = plugin_settings_service.resolve_enabled_plugin_mentions([{"path": "plugin://demo@market"}], connected_mcp_servers=[name])
    assert result[0]["mcp_server_names"] == [name]


@pytest.mark.asyncio
async def test_oauth_restart_restores_expiry_issuer_and_serializes_rotating_refresh(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken
    memory = {}
    async def read(storage, suffix): return memory.get((storage._service, storage._server, suffix))
    async def write(storage, suffix, value): memory[(storage._service, storage._server, suffix)] = value
    monkeypatch.setattr(SDKTokenStorage, "_get", read)
    monkeypatch.setattr(SDKTokenStorage, "_set", write)
    url = "https://resource.invalid/mcp"
    store = TokenStore(tmp_path / "tokens.json").for_server(url, "client")
    metadata = OAuthClientMetadata(redirect_uris=["http://127.0.0.1/callback"], token_endpoint_auth_method="none")
    info = OAuthClientInformationFull(**metadata.model_dump(), client_id="client")
    seed = store.sdk_storage("server")
    seed.context = SimpleNamespace(
        oauth_metadata=OAuthMetadata(issuer="https://issuer.invalid", authorization_endpoint="https://issuer.invalid/authorize", token_endpoint="https://issuer.invalid/token", response_types_supported=["code"]),
        protected_resource_metadata=None,
        auth_server_url="https://issuer.invalid",
    )
    await seed.set_client_info(info)
    with monkeypatch.context() as clock:
        clock.setattr("backend.mcp.oauth.time.time", lambda: 1.0)
        await seed.set_tokens(OAuthToken(access_token="expired", refresh_token="R0", token_type="Bearer", expires_in=1))
    providers = [CredentialOAuthProvider(url, metadata, store.sdk_storage("server")) for _ in range(2)]
    entered = asyncio.Event()
    release = asyncio.Event()
    refreshes = []
    authorizations = []
    async def respond(request):
        if str(request.url) == "https://issuer.invalid/token":
            refreshes.append(request.content.decode())
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "R1", "token_type": "Bearer", "expires_in": 3600})
        authorizations.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})
    async def request(provider):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), auth=provider) as client:
            await client.post(url)
    first = asyncio.create_task(request(providers[0]))
    await entered.wait()
    second = asyncio.create_task(request(providers[1]))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert len(refreshes) == 1
    assert "refresh_token=R0" in refreshes[0]
    assert authorizations == ["Bearer fresh", "Bearer fresh"]


def make_plugin(root, name="demo", version="1.0.0"):
    (root / ".minicode-plugin").mkdir(parents=True)
    (root / ".minicode-plugin/plugin.json").write_text(json.dumps({"name": name, "version": version, "mcp_servers": {"worker": {"transport": "stdio", "command": "node", "args": ["dist/index.js"]}}}), encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist/index.js").write_text("console.log('runtime')", encoding="utf-8")


def test_packaged_plugin_contains_its_runtime_entrypoint(tmp_path, monkeypatch):
    source = tmp_path / "source"
    make_plugin(source)
    monkeypatch.setattr(plugin_package, "feature_enabled", lambda *args: True)
    result = plugin_package.package_plugin_directory(source, tmp_path / "packages")
    with zipfile.ZipFile(result["package"]["path"]) as archive:
        assert "dist/index.js" in archive.namelist()


@pytest.mark.asyncio
async def test_canonical_plugin_activation_rolls_back_when_enablement_fails(tmp_path, monkeypatch):
    policy = ManagedPluginPolicy({}, None, (), {})
    old = tmp_path / "old"
    new = tmp_path / "new"
    make_plugin(old, version="1.0.0")
    make_plugin(new, version="2.0.0")
    store = plugin_store.PluginStore(tmp_path / "store")
    store.materialize(old, name="demo", version="1.0.0")
    monkeypatch.setattr(plugin_settings_service, "plugin_install_root", lambda: tmp_path / "installed")
    original_init = plugin_store.PluginStore.__init__
    monkeypatch.setattr(plugin_store.PluginStore, "__init__", lambda self, root=None, **kwargs: original_init(self, root or tmp_path / "store", **kwargs))
    def fail(_mutator): raise OSError("settings publication failed")
    monkeypatch.setattr(plugin_settings_service, "_update_settings_json", fail)
    with pytest.raises(OSError, match="settings publication failed"):
        await plugin_settings_service.import_plugin_from_path(new, overwrite=True, settings_file=tmp_path / "settings.json", config_change_hook=AsyncMock(return_value=None), _policy=policy)
    assert store.active("demo@local").version == "1.0.0"
    assert not store.version_path("local", "demo", "2.0.0").exists()
    assert not (tmp_path / "installed/demo").exists()


@pytest.mark.asyncio
async def test_marketplace_manifest_identity_is_checked_before_mutation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    make_plugin(source, "actual")
    hook = AsyncMock()
    with pytest.raises(ValueError, match="does not match requested"):
        await plugin_settings_service.import_plugin_from_path(source, settings_file=tmp_path / "settings.json", config_change_hook=hook,
            _policy=ManagedPluginPolicy({}, None, (), {}), _expected_plugin_id="requested@local")
    hook.assert_not_awaited()


def test_concurrent_marketplace_reconcile_cannot_destroy_activation_rollback(tmp_path, monkeypatch):
    registry = MarketplaceRegistry(tmp_path / "marketplaces.json")
    observer = MarketplaceRegistry(registry.path)
    archive = tmp_path / "market.zip"
    def bundle(value):
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(".minicode-plugin/marketplace.json", json.dumps({"name": "market", "plugins": []}))
            output.writestr("value", value)
    policy = ManagedPluginPolicy({}, None, (), {})
    bundle("old")
    registry.add("market", {"source": "file", "path": str(archive)}, policy=policy)
    registry.refresh("market", policy=policy)
    bundle("new")
    entered = threading.Event()
    release = threading.Event()
    def fail_commit(records):
        entered.set()
        assert release.wait(5)
        raise OSError("registry commit failed")
    monkeypatch.setattr(registry, "_write", fail_commit)
    with ThreadPoolExecutor(2) as workers:
        refresh = workers.submit(registry.refresh, "market", policy=policy)
        assert entered.wait(5)
        reconcile = workers.submit(observer.reconcile, policy=policy)
        try:
            with pytest.raises(FutureTimeout):
                reconcile.result(timeout=0.2)
        finally:
            release.set()
        with pytest.raises(OSError, match="registry commit failed"):
            refresh.result(timeout=5)
        assert reconcile.result(timeout=5)["ok"]
    assert (registry._materialized_root("market") / "value").read_text() == "old"


@pytest.mark.asyncio
async def test_permission_rule_hook_can_prevent_the_write(monkeypatch):
    from backend.hooks.manager import HookResult
    from unittest.mock import Mock
    writer = Mock()
    monkeypatch.setattr("backend.services.permission_content_service.add_permission_content_rule", writer)
    monkeypatch.setattr("backend.hooks.runtime.run_config_change_hook", AsyncMock(return_value=HookResult(blocked=True, feedback="veto")))
    emit = AsyncMock()
    session = SimpleNamespace(emit_command_result=emit)
    await conversation_handlers.handle_permissions_content_rule_add(session, {"rule": "run_command(*)"})
    writer.assert_not_called()
    assert emit.await_args.kwargs["level"] == "error"


@pytest.mark.asyncio
async def test_heartbeat_reads_and_persists_its_existing_model_context(tmp_path, monkeypatch):
    from backend.services import chat_api_service as chat, scheduled_task_runner as scheduled
    conversation = SimpleNamespace(id="heartbeat-regression", workspace_root=str(tmp_path), archived=False, git_isolated=False,
        transcript=[{"role": "user", "content": "prior task context"}], context_snapshot={})
    class Repository:
        def get_conversation(self, identity): return conversation
        def append_transcript_message(self, identity, message): conversation.transcript.append(message)
        def patch_context_snapshot(self, identity, snapshot): conversation.context_snapshot.update(snapshot)
    class Engine:
        def submit(self, submission):
            context = submission.session.context_builder
            assert context is not None
            assert "prior task context" in json.dumps(context.export_snapshot())
            context.append_user_context("new heartbeat checkpoint")
            async def events():
                yield AgentEvent(type="done", data={"status": "completed"})
            return events()
    original_run = chat.run_owned_rest_chat
    async def run(**kwargs):
        kwargs["query_engine"] = Engine()
        return await original_run(**kwargs)
    monkeypatch.setattr(scheduled, "ConversationRepository", Repository)
    monkeypatch.setattr(scheduled, "main_worktree_root", lambda root: Path(root).resolve())
    monkeypatch.setattr(scheduled, "run_owned_rest_chat", run)
    monkeypatch.setattr(chat, "load_config", lambda **kwargs: SimpleNamespace(agent=AgentSettings(), token_budget=TokenBudget()))
    monkeypatch.setattr(chat, "ArtifactStore", object)
    monkeypatch.setattr(chat, "default_runtime", lambda: SimpleNamespace(execution_journal=lambda owner: object()))
    bootstrap = SimpleNamespace(create_tool_registry=lambda *args, **kwargs: object(),
        create_permission_checker=lambda **kwargs: object(), create_llm=lambda **kwargs: object(), mcp_manager=None)
    task = SimpleNamespace(id="schedule", name="heartbeat", prompt="Continue", conversation_id=conversation.id,
        workspace_root=str(tmp_path), permission_mode="confirm", isolation="workspace")
    result = await scheduled.run_scheduled_task(task, SimpleNamespace(id="heartbeat-run"), bootstrap=bootstrap)
    assert result["status"] == "completed"
    assert "new heartbeat checkpoint" in json.dumps(conversation.context_snapshot)


@pytest.mark.asyncio
async def test_handoff_reports_stash_restore_failure_during_rollback(tmp_path, monkeypatch):
    conversation = SimpleNamespace(id="handoff", worktree_path=str(tmp_path / "worktree"), workspace_root=str(tmp_path), git_branch="feature")
    emit = AsyncMock()
    session = SimpleNamespace(main_worktree_root=lambda path: tmp_path, emit_command_result=emit)
    monkeypatch.setattr(handoff, "stash_workspace_changes", lambda *args, **kwargs: (True, "a" * 40))
    monkeypatch.setattr(handoff, "switch_main_checkout", lambda *args: (False, "switch failed"))
    monkeypatch.setattr(handoff, "restore_workspace_stash", lambda *args: (False, "restore failed"))
    monkeypatch.setattr("backend.workspace.worktree.WorktreeManager", lambda root: SimpleNamespace(
        remove_worktree=lambda *args, **kwargs: True, create_worktree=lambda *args, **kwargs: True))
    monkeypatch.setattr(conversation_handlers, "_release_active_sessions_from_conversation_workspace", AsyncMock(return_value=[]))
    monkeypatch.setattr(conversation_handlers, "_switch_active_sessions_to_conversation_workspace", AsyncMock(return_value=[]))
    await conversation_handlers._handle_conversation_worktree_handoff_claimed(session, conversation=conversation,
        conversation_id=conversation.id, target_kind="local", dirty_action="stash", preflight={"main_checkout": {"branch": "main", "head": "h"}})
    assert "restore_workspace_stash:restore failed" in emit.await_args.kwargs["data"]["rollback_errors"]


def test_mcp_cwd_removal_and_sse_registry_protocol():
    data = {"name": "demo", "transport": "stdio", "command": "python", "cwd": ""}
    entry = mcp_service._server_entry_from_payload(mcp_service._manual_config_from_payload(data), data, existing={"cwd": "old"})
    assert "cwd" not in entry
    entry = registry_server_to_marketplace_mcp({"name": "sse", "remotes": [{"type": "sse", "url": "https://server.invalid/sse"}]})
    assert entry["config_snippet"]["server"]["transport"] == "sse"


@pytest.mark.asyncio
async def test_text_provider_generation_rejects_a_proxy_html_success(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs): return httpx.Response(200, text="<html>login</html>", request=httpx.Request("POST", url))
    monkeypatch.setattr(llm_provider_helpers.httpx, "AsyncClient", Client)
    with pytest.raises(ValueError):
        await llm_provider_helpers._check_openai_compatible_generation("https://server.invalid/v1", "", "model", "chat", headers={}, auth_header=False)
    with pytest.raises(ValueError):
        await llm_provider_helpers._check_anthropic_generation("https://server.invalid/v1", "", "model", headers={}, auth_header=False)


@pytest.mark.asyncio
async def test_disabling_auto_merge_updates_the_exact_remote_pr_before_local_state(tmp_path, monkeypatch):
    workspace_service.write_pr_automation(tmp_path, {"auto_merge": True})
    view = AsyncMock(return_value=(0, json.dumps({"number": 7, "state": "OPEN", "headRefName": "feature", "statusCheckRollup": []})))
    mutation = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(workspace_service.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(workspace_service, "_run_gh_pr_view", view)
    monkeypatch.setattr(workspace_service, "_run_gh_pr_merge_auto", mutation)
    result = await workspace_service.set_git_pr_automation_payload(tmp_path, {"auto_merge": False})
    mutation.assert_awaited_once_with("gh", cwd=str(tmp_path), pr_number=7, enabled=False)
    assert result["automation"]["auto_merge"] is False


def test_handoff_stash_keeps_its_identity_when_another_stash_is_created(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q")
    (tmp_path / "file").write_text("base\n")
    git("add", "file")
    git("-c", "user.name=Audit", "-c", "user.email=audit@example.invalid", "commit", "-qm", "base")
    (tmp_path / "file").write_text("owned\n")
    ok, revision = handoff.stash_workspace_changes(tmp_path, label="owned")
    assert ok and len(revision) == 40
    (tmp_path / "file").write_text("other\n")
    git("stash", "push", "-m", "other")
    assert handoff.restore_workspace_stash(tmp_path, revision)[0]
    assert (tmp_path / "file").read_text() == "owned\n"


@pytest.mark.asyncio
async def test_slash_archive_does_not_announce_success_after_real_handler_refuses(monkeypatch):
    outcomes = []
    async def emit(command, message, **kwargs): outcomes.append((command, message, kwargs))
    ws = SimpleNamespace(active_conversation_id="conv", command_registry=CommandRegistry(), emit_command_result=emit,
                         conversation_repo=SimpleNamespace(get_conversation=lambda _: SimpleNamespace(id="conv")))
    ws.command_registry.register("conversation.archive", lambda payload: conversation_handlers.handle_conversation_archive(ws, payload))
    monkeypatch.setattr(conversation_handlers, "_conversation_activity_blockers", lambda *args: {"terminal_sessions": 1})
    await slash_commands._handle_archive(ws, "", None)
    assert len(outcomes) == 1 and outcomes[0][2]["level"] == "error"
    memory = next(entry for entry in catalog._COMPOSER_COMMAND_CATALOG if entry["command"] == "memory")
    assert slash_commands._normalize_memory_mode(memory["template"].split()[1]) == "enabled"
    assert "args" not in next(entry for entry in catalog._COMPOSER_COMMAND_CATALOG if entry["command"] == "new")


@pytest.mark.asyncio
async def test_cancelled_memory_reset_acquisition_releases_the_existing_barrier(monkeypatch):
    from backend.memory import generation
    entered = asyncio.Event()
    async def drain(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(generation, "cancel_and_drain", drain)
    generation.end_memory_reset()
    task = asyncio.create_task(generation.begin_memory_reset())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not generation.memory_reset_in_progress()
