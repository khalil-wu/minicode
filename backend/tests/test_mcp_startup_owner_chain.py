"""Controlled MCP transport gates through the real connection/view owners.

These tests do not open browsers, contact providers, or control the desktop.
Only the SDK transport peer is replaced; manager, cache, environment leases,
readiness, reconnect and final permission/dispatch paths are the product code.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.run_context import RunContext
from backend.agent.tool_execution import _authorize_final_tool_request, _bind_final_tool_request
from backend.config import PermissionSettings
from backend.environments import EnvironmentManager
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.mcp.connections import MCPConnection, MCPConnectionPool, MCPToolCatalogCache
from backend.mcp.environment_runtime import MCPEnvironmentRuntime
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.mcp.registry import MCPToolProxy
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import PermissionLevel
from backend.tools.registry import ToolRegistry
from backend.workspace.executor_local import LocalExecutionEnvironmentBackend


def definition(*, read=True, required=()):
    return MCPToolDef("inspect", "Current server operation", {
        "type": "object", "properties": {"value": {"type": "string"}}, "required": list(required)},
        annotations={"readOnlyHint": read}, meta={"anthropic/alwaysLoad": True})


class GatePeer:
    server_capabilities = {}
    server_info = {}
    has_valid_token = False

    def __init__(self, *, fail=None, cacheable=True, tools=None, ready=False):
        self.entered, self.release, self.listed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.list_release = None
        if ready:
            self.release.set()
        self.connected = False
        self.fail = fail
        self.tool_catalog_cacheable = cacheable
        self.instructions = "Captured real server instructions"
        self.tools = [definition()] if tools is None else tools
        self.starts = self.lists = self.closes = 0
        self.calls = []

    async def connect(self):
        self.starts += 1
        self.entered.set()
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        self.connected = True

    async def list_tools(self):
        self.lists += 1
        self.listed.set()
        if self.list_release is not None:
            await self.list_release.wait()
        return deepcopy(self.tools)

    async def close(self):
        self.closes += 1
        self.connected = False
        return True

    async def finish_pending_cleanup(self):
        return True

    def pending_requests(self):
        return ()

    async def subscribe_resource(self, uri):
        return True

    async def call_tool(self, name, args, *, request_owner=None, request_meta=None):
        self.calls.append((name, deepcopy(args), request_owner))
        return MCPCallResult(content=[{"type": "text", "text": "Exact owned observation"}])


@asynccontextmanager
async def owners(tmp_path, monkeypatch):
    import backend.mcp.connections as connections_module
    import backend.mcp.manager as manager_module

    cache = MCPToolCatalogCache()
    monkeypatch.setattr(connections_module, "TOOL_CATALOG_CACHE", cache)
    monkeypatch.setattr(manager_module, "TOOL_CATALOG_CACHE", cache)
    environments = EnvironmentManager(tmp_path, local_backend=LocalExecutionEnvironmentBackend())
    pool = MCPConnectionPool()
    plans, constructed = defaultdict(deque), []
    managers, runtimes = [], []

    def create(manager, config, **kwargs):
        peer = plans[config.name].popleft()
        constructed.append((config.name, peer, kwargs))
        return peer

    monkeypatch.setattr(MCPServerManager, "_create_client", create)

    def manager(*configs, grace=10, startup_policy="Eager"):
        runtime = MCPEnvironmentRuntime(environments, None, local_cwd=tmp_path.as_uri(), connection_pool=pool)
        runtimes.append(runtime)
        result = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path,
            environment_runtime=runtime, runtime_configs=configs, optional_startup_grace_ms=grace,
            startup_policy=startup_policy, feature_flags=FeatureFlags({"mcp_roots": False, "mcp_elicitation": False}))
        managers.append(result)
        return result

    try:
        yield SimpleNamespace(manager=manager, plans=plans, constructed=constructed,
            pool=pool, cache=cache, runtimes=runtimes, environments=environments)
    finally:
        await asyncio.gather(*(manager.stop_all() for manager in managers))
        await pool.aclose()
        await asyncio.gather(*(runtime.aclose() for runtime in runtimes))
        await environments.close()


def server(tmp_path, name="gate", **kwargs):
    return MCPServerConfig(name, command="controlled-peer", cwd=str(tmp_path), **kwargs)


def seed(owner, config, tools=None):
    context = owner._catalog_contexts.get(config.name)
    if context is None:
        from backend.mcp.connections import TOOL_CATALOG_CACHE
        context = TOOL_CATALOG_CACHE.context(owner._catalog_identity(config))
    context.publish(context.begin_fetch(), tools or [definition()], "Captured real server instructions")
    return context


@pytest.mark.asyncio
async def test_bootstrap_to_step_keeps_one_initialize_and_original_grace_after_view_retirement(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config, peer = server(tmp_path), GatePeer()
        host.plans[config.name].append(peer)
        first = host.manager(config)
        await first.start_all()
        await peer.entered.wait()
        view = first._connection_view(config.name)
        deadline = view.connection._startup_deadline
        assert first.get_all_tools() == {} and peer.starts == 1
        await first.stop_all()
        await first.environment_runtime.aclose()
        assert not view.connection.task.done() and peer.closes == 0
        second = host.manager(config)
        await asyncio.wait_for(second.start_all(), 1)
        assert second._connection_view(config.name).connection is view.connection
        assert view.connection._startup_deadline == deadline and len(host.constructed) == 1
        peer.release.set()
        await second.wait_for_server_startup(config.name)
        assert second.get_client(config.name) is peer and peer.lists == 1


@pytest.mark.asyncio
async def test_concurrent_startup_admission_shares_one_initialize_attempt(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config = server(tmp_path)
        peer = GatePeer()
        host.plans[config.name].append(peer)
        manager = host.manager(config)

        await asyncio.gather(
            manager.ensure_server_startup(config),
            manager.ensure_server_startup(config),
        )
        await peer.entered.wait()
        await asyncio.sleep(0)
        assert peer.starts == 1

        peer.release.set()
        await manager.wait_for_server_startup(config.name)
        assert manager.get_client(config.name) is peer


@pytest.mark.asyncio
@pytest.mark.parametrize("required,grace", [(True, 10), (False, 0)])
async def test_required_or_zero_grace_waits_original_attempt_without_changing_sdk_timeout(tmp_path, monkeypatch, required, grace):
    async with owners(tmp_path, monkeypatch) as host:
        config, peer = server(tmp_path, required=required), GatePeer()
        host.plans[config.name].append(peer)
        manager = host.manager(config, grace=grace)
        starting = asyncio.create_task(manager.start_all())
        await peer.entered.wait()
        await asyncio.sleep(0)
        assert not starting.done()
        assert MCPClient("unstarted")._startup_timeout == 30.0
        peer.release.set()
        await asyncio.wait_for(starting, 1)
        assert manager.get_client(config.name) is peer


@pytest.mark.asyncio
@pytest.mark.parametrize("readiness,exposed_after_failure", [("connection", False), ("catalog", True)])
async def test_cached_connection_failure_and_catalog_readiness_are_distinct(tmp_path, monkeypatch, readiness, exposed_after_failure):
    async with owners(tmp_path, monkeypatch) as host:
        config = server(tmp_path, startup_readiness=readiness)
        peer = GatePeer(fail=ConnectionError("original initialize failure"))
        host.plans[config.name].append(peer)
        manager = host.manager(config)
        context = seed(manager, config)
        assert context.get().tools[0].annotations == {}
        await manager.start_all()
        assert config.name in manager.get_all_tools()
        peer.release.set()
        await manager.wait_for_server_startup(config.name)
        await manager.capture_startup_readiness()
        assert (config.name in manager.get_all_tools()) is exposed_after_failure
        assert manager.get_client(config.name) is None
        assert "original initialize failure" in manager._servers[config.name].last_error


@pytest.mark.asyncio
async def test_opt_out_during_other_required_startup_rechecks_required_catalog_before_projection(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        cached = server(tmp_path, "cached", required=True, startup_readiness="catalog")
        blocking = server(tmp_path, "required", required=True)
        peer, required = GatePeer(cacheable=False), GatePeer()
        peer.list_release = asyncio.Event()
        host.plans[cached.name].append(peer)
        host.plans[blocking.name].append(required)
        manager = host.manager(cached, blocking)
        context = seed(manager, cached)
        starting = asyncio.create_task(manager.start_all())
        await asyncio.gather(peer.entered.wait(), required.entered.wait())
        peer.release.set()
        await peer.listed.wait()
        assert context.get() is None
        required.release.set()
        await manager.wait_for_server_startup(blocking.name)
        await asyncio.sleep(0)
        assert not starting.done(), "Required Catalog cannot use a server's rejected cache"
        peer.list_release.set()
        await asyncio.wait_for(starting, 1)
        assert manager.get_client(cached.name) is peer


@pytest.mark.asyncio
async def test_optional_opt_out_does_not_reintroduce_cached_tools_while_other_server_waits(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config, required_config = server(tmp_path), server(tmp_path, "required", required=True)
        peer, required = GatePeer(cacheable=False, fail=ConnectionError("opted-out failure")), GatePeer()
        host.plans[config.name].append(peer)
        host.plans[required_config.name].append(required)
        manager = host.manager(config, required_config)
        context = seed(manager, config)
        starting = asyncio.create_task(manager.start_all())
        await asyncio.gather(peer.entered.wait(), required.entered.wait())
        # Real initialize capability negotiation opts out before tools/list.
        peer.fail = None
        peer.list_release = asyncio.Event()
        peer.release.set()
        await peer.listed.wait()
        assert context.get() is None
        required.release.set()
        await asyncio.wait_for(starting, 1)
        assert config.name not in manager.get_all_tools()
        peer.list_release.set()


@pytest.mark.asyncio
async def test_idle_startup_cancel_reaches_producer_but_dormant_cache_is_not_started(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config, peer = server(tmp_path), GatePeer()
        host.plans[config.name].append(peer)
        dormant = host.manager(config, startup_policy="LazyWhenCached")
        seed(dormant, config)
        await dormant.start_all()
        view = dormant._connection_view(config.name)
        await host.pool.cancel_startup()
        assert view.connection.task is None and not view.connection.closed and not host.constructed
        call = asyncio.create_task(dormant.prepare_server_call(config.name))
        await peer.entered.wait()
        await host.pool.cancel_startup()
        result = await asyncio.gather(call, return_exceptions=True)
        assert isinstance(result[0], (asyncio.CancelledError, ConnectionError))
        assert view.connection.closed and peer.closes == 1
        assert dormant._servers[config.name].status is not ServerStatus.STARTING


@pytest.mark.asyncio
async def test_retired_generations_are_all_drained_at_thread_shutdown(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        first_config = server(tmp_path)
        second_config = replace(first_config, args=["changed-transport"])
        first_peer, second_peer = GatePeer(), GatePeer()
        host.plans[first_config.name].extend((first_peer, second_peer))
        first, second = host.manager(first_config), host.manager(second_config)
        await first.start_all()
        await second.start_all()
        old = first._connection_view(first_config.name).connection
        new = second._connection_view(first_config.name).connection
        assert old is not new and old.retired and not old.closed
        await host.pool.aclose()
        assert old.closed and new.closed and first_peer.closes == second_peer.closes == 1


@pytest.mark.asyncio
async def test_two_remote_observers_join_one_fresh_reconnect_generation(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config = MCPServerConfig("http-gate", transport="http", url="http://controlled.invalid/mcp")
        first_peer, replacement = GatePeer(ready=True), GatePeer(ready=True)
        host.plans[config.name].extend((first_peer, replacement))
        first, second = host.manager(config, grace=0), host.manager(config, grace=0)
        await first.start_all()
        await second.start_all()
        original = first._connection_view(config.name).connection
        first_peer.connected = False
        await original.notify_disconnect(config.name)
        await asyncio.gather(*tuple(first._reconnect_tasks.values()), *tuple(second._reconnect_tasks.values()))
        assert first.get_client(config.name) is second.get_client(config.name) is replacement
        assert replacement.starts == 1 and len(host.constructed) == 2 and original.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["restart", "oauth"])
async def test_explicit_restart_or_oauth_replaces_failed_attempt_owner(tmp_path, monkeypatch, operation):
    async with owners(tmp_path, monkeypatch) as host:
        config = MCPServerConfig("remote", transport="http", url="http://controlled.invalid/mcp")
        failed, replacement = GatePeer(ready=True, fail=ConnectionError("failed attempt")), GatePeer(ready=True)
        host.plans[config.name].extend((failed, replacement))
        manager = host.manager(config, grace=0)
        await manager.start_all()
        original = manager._connection_view(config.name).connection
        if operation == "restart":
            await manager.restart_server(config.name)
        else:
            await manager.oauth_login(config.name)
        assert original.closed and manager.get_client(config.name) is replacement
        assert len(host.constructed) == 2 and replacement.starts == 1
        assert host.constructed[-1][2]["interactive_oauth"] is (operation == "oauth")


@pytest.mark.asyncio
async def test_connection_shutdown_cancels_and_drains_shared_catalog_refresh():
    peer = GatePeer(ready=True)
    async def factory(connection):
        return peer
    connection = MCPConnection(factory=factory, close=lambda client: client.close(), identity="refresh")
    await connection.ready()
    peer.listed.clear()
    peer.list_release = asyncio.Event()
    await connection.notify_tools_changed("refresh")
    refresh = asyncio.create_task(connection.refresh_catalog())
    await peer.listed.wait()
    await connection.aclose()
    result = await asyncio.gather(refresh, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert connection._catalog_refresh.done() and peer.closes == 1


def test_catalog_ttl_generation_revision_opt_out_and_lru_context_ownership(monkeypatch):
    import backend.mcp.connections as module
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    cache = MCPToolCatalogCache()
    context = cache.context("original")
    older, newer = context.begin_fetch(), context.begin_fetch()
    context.publish(newer, [definition(read=True)], "new")
    captured = context.get()
    context.publish(older, [definition(read=False)], "old")
    assert context.get().instructions == "new" and captured.tools[0].annotations == {}
    for index in range(40):
        cache.context(index)
    assert len(cache.entries) == 32 and context.get() is captured
    now[0] += 30 * 60 + 1
    assert context.get() is None and context.get(captured) is captured
    context.invalidate()
    assert context.get(captured) is None
    context.publish(context.begin_fetch(), [definition()], "after-notification")
    assert context.get().instructions == "after-notification"
    context.disable()
    assert context.get(captured) is None


@pytest.mark.asyncio
async def test_cached_live_schema_and_hints_authorize_and_execute_the_same_exact_client(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config, peer = server(tmp_path), GatePeer(tools=[definition(read=False)])
        host.plans[config.name].append(peer)
        manager = host.manager(config)
        seed(manager, config, [definition(read=True, required=("value",))])
        await manager.start_all()
        advertised = MCPToolProxy(config.name, manager.get_all_tools()[config.name][0], manager,
            manager=manager, prepare_call=manager.prepare_server_call)
        # Callable normalization survives live reconstruction.
        advertised.name = "mcp__gate__inspect_allocated"
        registry = ToolRegistry()
        registry.register(advertised)
        context = ToolExecutionContext(PermissionContext(workspace_root=tmp_path), workspace_root=tmp_path,
            run_context=RunContext(), tool_registry=registry)
        call = ToolCallEvent(id="cached-live", name=advertised.name, arguments={})
        authorizing = asyncio.create_task(_authorize_final_tool_request(call, tool_registry=registry,
            permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[], auto_allow=[]), tmp_path),
            permission_context=context.permission, tool_ctx=context))
        await peer.entered.wait()
        assert not authorizing.done()
        peer.release.set()
        authorization = await asyncio.wait_for(authorizing, 1)
        assert not authorization.error
        assert authorization.permission_decision.permission_level is PermissionLevel.CONFIRM
        prepared = authorization.request.prepared_tool
        assert prepared._tool_def.input_schema["required"] == [] and prepared.name == advertised.name
        assert prepared._client is peer and not prepared.read_only
        _bind_final_tool_request(call, authorization.request)
        # A registry publication cannot substitute another client's approved action.
        registry._tools[advertised.name] = SimpleNamespace(name=advertised.name, policy_identity="replacement")
        result = await registry.execute(call.name, {}, context=context,
            expected_policy_identity=authorization.request.policy_identity, prepared_tool=prepared)
        assert not result.is_error and len(peer.calls) == 1


@pytest.mark.asyncio
async def test_execution_binding_lease_lives_until_real_client_close(tmp_path):
    environments = EnvironmentManager(tmp_path, local_backend=LocalExecutionEnvironmentBackend())
    runtime = MCPEnvironmentRuntime(environments, None, local_cwd=tmp_path.as_uri())
    environment = environments.get_environment("local")
    try:
        binding = await runtime.bind_server(server(tmp_path))
        client = MCPClient("never-started", execution_binding=binding)
        assert environment.borrowers == 2
        await runtime.aclose()
        assert environment.borrowers == 1
        assert await client.close()
        assert environment.borrowers == 0
    finally:
        await runtime.aclose()
        await environments.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["node", "cua"])
async def test_node_and_cua_views_keep_one_thread_owned_startup_until_shutdown(tmp_path, monkeypatch, kind):
    from backend.mcp.node_repl import NodeReplCompanions, _Companion, SOURCE_PREFIX as NODE_SOURCE
    from backend.mcp.cua_repl import CuaReplCompanions, SOURCE_PREFIX as CUA_SOURCE

    environments = EnvironmentManager(tmp_path, local_backend=LocalExecutionEnvironmentBackend())
    attachment = SimpleNamespace(lease=environments.get_environment("local").borrow())
    owner = NodeReplCompanions() if kind == "node" else CuaReplCompanions()
    prefix, name = (NODE_SOURCE, "node_repl") if kind == "node" else (CUA_SOURCE, "cua_repl")
    config = server(tmp_path, name, source=prefix + "captured")
    clients = []

    class CompanionPeer(GatePeer):
        def __init__(self, config, attachment, network, **kwargs):
            super().__init__()
            self.lease = attachment.lease.clone()
            clients.append(self)

        async def close_thread(self):
            await super().close()
            await self.lease.aclose()

    owner.client_type = CompanionPeer
    owner.instances["captured"] = _Companion("captured", config, attachment, None)
    owner.active_key = "captured"
    runtime = MCPEnvironmentRuntime(environments, None, local_cwd=tmp_path.as_uri())
    runtime.node_repl_companions = owner if kind == "node" else None
    runtime.cua_repl_companions = owner if kind == "cua" else None
    managers = [MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path,
        environment_runtime=runtime, runtime_configs=(config,), optional_startup_grace_ms=10) for _ in range(2)]
    try:
        await managers[0].start_all()
        original = managers[0]._connection_view(name).connection
        await managers[0].stop_all()
        await managers[1].start_all()
        assert managers[1]._connection_view(name).connection is original
        assert len(clients) == 1 and clients[0].starts == 1 and clients[0].closes == 0
        clients[0].release.set()
        await managers[1].wait_for_server_startup(name)
        assert managers[1].get_client(name) is clients[0] and clients[0].lists == 1
        await managers[1].stop_all()
        assert clients[0].closes == 0
        await owner.aclose()
        assert original.closed and clients[0].closes == 1
    finally:
        await asyncio.gather(*(manager.stop_all() for manager in managers))
        await owner.aclose()
        await runtime.aclose()
        await attachment.lease.aclose()
        await environments.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["node", "cua"])
async def test_companion_reuses_startup_grace_before_a_new_manager_view_attaches(tmp_path, monkeypatch, kind):
    from backend.mcp.node_repl import NodeReplCompanions, _Companion, SOURCE_PREFIX as NODE_SOURCE
    from backend.mcp.cua_repl import CuaReplCompanions, SOURCE_PREFIX as CUA_SOURCE

    environments = EnvironmentManager(tmp_path, local_backend=LocalExecutionEnvironmentBackend())
    attachment = SimpleNamespace(lease=environments.get_environment("local").borrow())
    owner = NodeReplCompanions() if kind == "node" else CuaReplCompanions()
    prefix, name = (NODE_SOURCE, "node_repl") if kind == "node" else (CUA_SOURCE, "cua_repl")
    config = server(tmp_path, name, source=prefix + "captured")
    clients = []

    class CompanionPeer(GatePeer):
        def __init__(self, config, attachment, network, **kwargs):
            super().__init__()
            self.lease = attachment.lease.clone()
            clients.append(self)

        async def close_thread(self):
            await super().close()
            await self.lease.aclose()

    owner.client_type = CompanionPeer
    owner.instances["captured"] = _Companion("captured", config, attachment, None)
    owner.active_key = "captured"
    runtime = MCPEnvironmentRuntime(environments, None, local_cwd=tmp_path.as_uri())
    runtime.node_repl_companions = owner if kind == "node" else None
    runtime.cua_repl_companions = owner if kind == "cua" else None
    managers = [MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path,
        environment_runtime=runtime, runtime_configs=(config,), optional_startup_grace_ms=200,
        requires_read_only_tools=True) for _ in range(2)]
    attached, release_view = asyncio.Event(), asyncio.Event()
    prepare_view = managers[1]._prepare_connection_view

    async def delayed_view(*args, **kwargs):
        attached.set()
        await release_view.wait()
        return await prepare_view(*args, **kwargs)

    monkeypatch.setattr(managers[1], "_prepare_connection_view", delayed_view)
    try:
        await managers[0].start_all()
        assert clients and clients[0].starts == 1 and not clients[0].connected
        second = asyncio.create_task(managers[1].start_all())
        await asyncio.wait_for(attached.wait(), timeout=1)
        await asyncio.wait_for(second, timeout=0.1)
        release_view.set()
        clients[0].release.set()
        await managers[1].wait_for_server_startup(name)
        assert len(clients) == 1 and managers[1].get_client(name) is clients[0]
    finally:
        release_view.set()
        if clients:
            clients[0].release.set()
        await asyncio.gather(*(manager.stop_all() for manager in managers))
        await owner.aclose()
        await runtime.aclose()
        await attachment.lease.aclose()
        await environments.close()


@pytest.mark.asyncio
async def test_connection_and_discovery_identity_keep_authority_but_exclude_view_budgets(tmp_path, monkeypatch):
    from backend.mcp.catalog_identity import catalog_identity, connection_identity

    environments = EnvironmentManager(tmp_path, local_backend=LocalExecutionEnvironmentBackend())
    runtime = MCPEnvironmentRuntime(environments, None, local_cwd=tmp_path.as_uri())
    config = server(tmp_path, env_vars=(("B5_CATALOG_VALUE", "local"),))
    capabilities = (MCPClient.PROTOCOL_VERSION, True, False, ())
    monkeypatch.setenv("B5_CATALOG_VALUE", "first-private-value")
    try:
        live = connection_identity(config, runtime, capabilities, None, requires_read_only_tools=False)
        catalog = catalog_identity(config, runtime, capabilities)
        changed_view = replace(config, required=True, startup_timeout_sec=99, tool_timeout_sec=17,
            startup_readiness="catalog", enabled_tools=["inspect"])
        assert live == connection_identity(changed_view, runtime, capabilities, None, requires_read_only_tools=False)
        assert catalog == catalog_identity(changed_view, runtime, capabilities)
        monkeypatch.setenv("B5_CATALOG_VALUE", "second-private-value")
        assert live != connection_identity(config, runtime, capabilities, None, requires_read_only_tools=False)
        assert catalog != catalog_identity(config, runtime, capabilities)
        assert catalog_identity(replace(config, environment_id="remote", env_vars=(("REMOTE_VALUE", "remote"),)), runtime, capabilities) is None
        assert catalog_identity(config, runtime, capabilities, requires_read_only_tools=True) is None
        remote = MCPServerConfig("http", transport="http", url="https://controlled.invalid/mcp", headers_helper="helper")
        assert catalog_identity(remote, runtime, capabilities) is None
        assert catalog_identity(replace(remote, headers_helper=""), runtime, capabilities, has_oauth_credentials=True) is None
    finally:
        await runtime.aclose()
        await environments.close()


@pytest.mark.asyncio
async def test_call_timeout_comes_from_each_view_including_return_to_default(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config, peer = server(tmp_path, tool_timeout_sec=7), GatePeer(ready=True)
        host.plans[config.name].append(peer)
        first = host.manager(config, grace=0)
        await first.start_all()
        first_proxy = MCPToolProxy(config.name, peer.tools[0], first, manager=first)
        assert not (await first_proxy.execute({})).is_error
        second = host.manager(replace(config, tool_timeout_sec=None), grace=0)
        await second.start_all()
        second_proxy = MCPToolProxy(config.name, peer.tools[0], second, manager=second)
        assert not (await second_proxy.execute({})).is_error
        assert [request[2]["mcp_tool_timeout_sec"] for request in peer.calls] == [7, 300.0]
        assert peer.starts == 1


@pytest.mark.asyncio
async def test_prepared_client_is_not_substituted_after_permission_while_remote_reconnects(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        config = MCPServerConfig("remote", transport="http", url="http://controlled.invalid/mcp")
        original, replacement = GatePeer(ready=True), GatePeer(ready=True)
        host.plans[config.name].extend((original, replacement))
        manager = host.manager(config, grace=0)
        await manager.start_all()
        advertised = MCPToolProxy(config.name, original.tools[0], manager, manager=manager)
        prepared = await advertised.prepare_for_execution()
        await manager.restart_server(config.name)
        assert manager.get_client(config.name) is replacement
        result = await prepared.execute({})
        assert result.is_error and not original.calls and not replacement.calls


@pytest.mark.asyncio
async def test_environment_bind_gate_uses_original_thread_producer_grace_and_required_waiter(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        entered, release = asyncio.Event(), asyncio.Event()
        bound = []
        original_bind = MCPEnvironmentRuntime.bind_server
        async def bind(runtime, config):
            bound.append(config.name)
            entered.set()
            await release.wait()
            return await original_bind(runtime, config)
        monkeypatch.setattr(MCPEnvironmentRuntime, "bind_server", bind)
        config, peer = server(tmp_path), GatePeer(ready=True)
        host.plans[config.name].append(peer)
        first = host.manager(config)
        starting = asyncio.create_task(first.start_all())
        await entered.wait()
        await asyncio.wait_for(starting, 1)
        connection = first._connection_view(config.name).connection
        deadline = connection._startup_deadline
        assert bound == [config.name] and not host.constructed and connection.client is None
        await first.stop_all()
        await first.environment_runtime.aclose()
        second = host.manager(config)
        await asyncio.wait_for(second.start_all(), 1)
        assert second._connection_view(config.name).connection is connection
        assert connection._startup_deadline == deadline and bound == [config.name]
        required = host.manager(replace(config, required=True))
        required_start = asyncio.create_task(required.start_all())
        await asyncio.sleep(0)
        assert not required_start.done() and not host.constructed
        release.set()
        await asyncio.wait_for(required_start, 1)
        assert required.get_client(config.name) is peer and peer.starts == 1
        assert bound == [config.name] and len(host.constructed) == 1


@pytest.mark.asyncio
async def test_pending_bind_keeps_real_startup_slot_after_all_views_retire(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        entered, release = asyncio.Event(), asyncio.Event()
        bound = []
        original_bind = MCPEnvironmentRuntime.bind_server
        async def bind(runtime, config):
            bound.append(config.name)
            if config.name == "first":
                entered.set()
                await release.wait()
            return await original_bind(runtime, config)
        monkeypatch.setattr(MCPEnvironmentRuntime, "bind_server", bind)
        configs = (server(tmp_path, "first"), server(tmp_path, "second"))
        peers = (GatePeer(ready=True), GatePeer(ready=True))
        for config, peer in zip(configs, peers):
            host.plans[config.name].append(peer)
        first = host.manager(*configs)
        first._local_startup_slots = asyncio.Semaphore(1)
        await first.start_all()
        await entered.wait()
        await first.stop_all()
        second = host.manager(*configs)
        await second.start_all()
        assert bound == ["first"] and not host.constructed
        assert all(not second._connection_view(config.name).connection.task.done() for config in configs)
        release.set()
        await asyncio.gather(*(second.wait_for_server_startup(config.name) for config in configs))
        assert bound == ["first", "second"] and len(host.constructed) == 2
        assert [peer.starts for peer in peers] == [1, 1]


@pytest.mark.asyncio
async def test_standalone_environment_bind_is_inside_optional_attempt_budget(tmp_path, monkeypatch):
    async with owners(tmp_path, monkeypatch) as host:
        entered, release = asyncio.Event(), asyncio.Event()
        original_bind = MCPEnvironmentRuntime.bind_server
        async def bind(runtime, config):
            entered.set()
            await release.wait()
            return await original_bind(runtime, config)
        monkeypatch.setattr(MCPEnvironmentRuntime, "bind_server", bind)
        config, peer = server(tmp_path), GatePeer(ready=True)
        host.plans[config.name].append(peer)
        manager = host.manager(config)
        manager.environment_runtime.connection_pool = None
        starting = asyncio.create_task(manager.start_all())
        await entered.wait()
        await asyncio.wait_for(starting, 1)
        assert manager.get_client(config.name) is None and not host.constructed
        release.set()
        await manager.wait_for_server_startup(config.name)
        assert manager.get_client(config.name) is peer and peer.starts == 1
