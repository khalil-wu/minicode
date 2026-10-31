"""Thread-owned Node companion, admitted through the normal MCP tool registry.

Codex 822e58c defines the MCP identity and accepted Guardian evidence. Its
public checkout does not contain the Node executor; node_repl_server.mjs is
MiniCode's implementation of the observed js/reset/module-directory contract.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from backend.mcp.client import MCPClient, MCPTransport
from backend.mcp.executor_stdio import executor_stdio_client
from backend.mcp.connections import MCPConnection, MCPConnectionView

SOURCE_PREFIX = "builtin:node_repl:"
_SERVER = Path(__file__).with_name("node_repl_server.mjs").read_text(encoding="utf-8")


def _env_policy(attachment):
    policy = attachment.config.shell_environment_policy
    return {"inherit": policy.inherit, "ignoreDefaultExcludes": policy.ignore_default_excludes,
        "exclude": list(policy.exclude), "set": dict(policy.set_values), "includeOnly": list(policy.include_only)}


class NodeReplMCPClient(MCPClient):
    def __init__(self, config, attachment, network, *, on_disconnect, on_tools_changed, feature_flags=None,
            requires_read_only_tools=False):
        self.attachment = attachment
        self.thread_lease = attachment.lease.clone()
        self.scope = attachment.permission_scope
        self.network = network
        self._thread_closing = False
        self.process_handle = None
        super().__init__(config.name, command=config.command, args=config.args,
            transport=MCPTransport.STDIO, env=config.env,
            on_disconnect=on_disconnect, on_tools_changed=on_tools_changed,
            startup_timeout=config.startup_timeout_sec, tool_timeout=config.tool_timeout_sec,
            feature_flags=feature_flags, requires_read_only_tools=requires_read_only_tools)

    def _tool_request_timeout_seconds(self, params):
        timeout = params.get("arguments", {}).get("timeout_ms", 30000) if params["name"] == "js" else 30000
        return max(super()._tool_request_timeout_seconds(params), timeout / 1000 + self._request_timeout)

    async def _sdk_transport_context(self):
        return self._executor_transport()

    @asynccontextmanager
    async def _executor_transport(self):
        options = {"process_observer": self._capture_process}
        network_lease = None
        if self.network is not None:
            if self.attachment.is_remote:
                network_lease = await self.network.prepare_remote(environment_id=self.scope.environment_id,
                    execution_id=uuid4().hex, attribution_token=uuid4().hex)
                options.update(enforce_managed_network=True, network_proxy=network_lease.remote_launch,
                    network_decider=network_lease.decide)
            else:
                options["managed_network_policy"] = self.network
        try:
            async with executor_stdio_client(self.thread_lease, [self._command, *self._args],
                    self.attachment.selection.cwd, env=self._env,
                    env_policy=_env_policy(self.attachment),
                    sandbox=self.scope.process_sandbox_context(enforce_managed_network=self.network is not None),
                    process_options=options) as streams:
                yield streams
        finally:
            if network_lease is not None:
                await network_lease.aclose()

    def _capture_process(self, process):
        self.process_handle = process

    async def close(self) -> bool:
        """The manager relinquishes its view; the receiving thread owns the kernel."""
        return await super().close() if self._thread_closing else True

    async def close_thread(self) -> None:
        self._thread_closing = True
        if not await self.close():
            if not await super().finish_pending_cleanup():
                raise ConnectionError("Node companion process cleanup is still pending")
        if self.process_handle is not None:
            receipt = await self.process_handle.aclose()
            if not receipt["closed"]:
                raise ConnectionError("Original Node companion process close was not confirmed")
        await self.thread_lease.aclose()


@dataclass
class _Companion:
    key: str
    config: object
    attachment: object
    network: object
    client: NodeReplMCPClient | None = None
    connection: MCPConnection | None = None
    views: int = 0
    retired: bool = False
    closed: bool = False
    feature_flags: object | None = None
    requires_read_only_tools: bool = False
    startup_window: tuple[int, float] | None = None

    def startup_deadline(self, grace_ms: int, deadline: float) -> float:
        if self.startup_window is None or self.startup_window[0] != grace_ms:
            self.startup_window = (grace_ms, deadline)
        return self.startup_window[1]


@dataclass
class NodeCompanionView:
    owner: object
    companion: _Companion
    connection_view: MCPConnectionView
    released: bool = False

    @property
    def client(self):
        return self.companion.client

    async def wait_for_startup(self):
        return await self.connection_view.wait_for_startup()

    @property
    def connection(self):
        return self.connection_view.connection

    async def aclose(self) -> bool:
        async with self.owner.lock:
            if not self.released:
                self.companion.views -= 1
                self.released = True
                await self.connection_view.aclose()
            if self.companion.retired and self.companion.views == 0:
                await self.owner._close(self.companion)
        return True


class NodeReplCompanions:
    source_prefix = SOURCE_PREFIX
    client_type = NodeReplMCPClient

    def __init__(self):
        self.instances: dict[str, _Companion] = {}
        self.active_key = None
        self.lock = asyncio.Lock()

    def startup_deadline(self, config, grace_ms: int, deadline: float) -> float:
        companion = self.instances[config.source.removeprefix(self.source_prefix)]
        return companion.startup_deadline(grace_ms, deadline)

    async def admit(self, attachment, network, *, feature_flags=None, requires_read_only_tools=False):
        from backend.mcp.manager import MCPServerConfig

        command = (os.environ.get("MINICODE_EDITOR_NODE") or shutil.which("node")) if not attachment.is_remote else "node"
        if command is None:
            return None
        identity = json.dumps([id(attachment.environment), attachment.permission_scope.identity(),
            _env_policy(attachment), attachment.config.mcp_policy, network.identity() if network is not None else None,
            feature_flags.enabled("mcp_roots") if feature_flags is not None else None, requires_read_only_tools],
            sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        # CreateProcess limits the complete Windows command line to 32,767
        # UTF-16 units. Transport the embedded module compressed, retaining the
        # same stdin MCP stream and executor-owned process on every platform.
        module = _SERVER + "\n//# sourceURL=minicode-node-repl.mjs\n"
        source = base64.b64encode(gzip.compress(module.encode("utf-8"), mtime=0)).decode("ascii")
        bootstrap = ("await import('data:text/javascript;base64,' + "
            "(await import('node:zlib')).gunzipSync(Buffer.from('" + source
            + "', 'base64')).toString('base64'))")
        config = MCPServerConfig("node_repl", command=command,
            args=["--expose-internals", "--input-type=module", "--eval", bootstrap],
            env={"ELECTRON_RUN_AS_NODE": "1"} if not attachment.is_remote else {},
            cwd=attachment.selection.cwd, source=SOURCE_PREFIX + key,
            environment_id=attachment.selection.environment_id)
        async with self.lock:
            if self.active_key is not None and self.active_key != key:
                previous = self.instances.get(self.active_key)
                if previous is not None:
                    previous.retired = True
                    if previous.views == 0:
                        await self._close(previous)
            self.active_key = key
            returning = self.instances.get(key)
            if returning is not None and returning.connection is not None and returning.connection.closed:
                returning.retired = True
                if returning.views == 0:
                    await self._close(returning)
                if self.instances.get(key) is returning:
                    self.instances.pop(key)
            if returning is not None and returning.retired and returning.views == 0:
                await self._close(returning)
            if key not in self.instances:
                self.instances[key] = _Companion(key, config, attachment, network, feature_flags=feature_flags,
                    requires_read_only_tools=requires_read_only_tools)
            self.instances[key].retired = False
        return config

    async def retire(self, config=None):
        async with self.lock:
            key = config.source.removeprefix(self.source_prefix) if config is not None else self.active_key
            if key is None:
                return
            companion = self.instances[key]
            companion.retired = True
            self.active_key = None
            if companion.views == 0:
                await self._close(companion)

    def client_for(self, config, *, on_disconnect, on_tools_changed):
        companion = self.instances[config.source.removeprefix(self.source_prefix)]
        if companion.client is None:
            companion.client = self.client_type(config, companion.attachment, companion.network,
                on_disconnect=None, on_tools_changed=None, feature_flags=companion.feature_flags,
                requires_read_only_tools=companion.requires_read_only_tools)

            async def create(connection):
                companion.client._on_disconnect = connection.notify_disconnect
                companion.client._on_tools_changed = connection.notify_tools_changed
                return companion.client

            async def close(client):
                await client.close_thread()

            companion.connection = MCPConnection(factory=create, close=close, identity=companion.key)
            companion.connection.client = companion.client
        view = companion.connection.acquire(on_disconnect=on_disconnect, on_tools_changed=on_tools_changed)
        companion.views += 1
        return companion.client, NodeCompanionView(self, companion, view)

    async def _close(self, companion):
        if companion.closed:
            return
        if companion.connection is not None:
            await companion.connection.aclose()
        companion.closed = True
        if self.instances.get(companion.key) is companion:
            self.instances.pop(companion.key)

    async def aclose(self) -> None:
        async with self.lock:
            for companion in tuple(self.instances.values()):
                companion.retired = True
                await self._close(companion)
            self.active_key = None

    async def cancel_startup(self) -> None:
        await asyncio.gather(*(companion.connection.cancel_startup()
            for companion in tuple(self.instances.values()) if companion.connection is not None))

    async def restart(self, config) -> None:
        async with self.lock:
            key = config.source.removeprefix(self.source_prefix)
            original = self.instances[key]
            await self._close(original)
            self.instances[key] = _Companion(key, config, original.attachment, original.network,
                feature_flags=original.feature_flags, requires_read_only_tools=original.requires_read_only_tools)
            self.active_key = key


async def register_node_companion(run_context, registry, agent_session, *, execution) -> None:
    """Register only after the canonical step has captured its network policy."""
    snapshot = run_context.turn_environments
    prepared = run_context.environment_capabilities
    if snapshot is None or prepared is None:
        return
    attachment = snapshot.primary()
    if attachment is None:
        return
    manager = prepared.mcp_manager
    thread = run_context.environment_thread_runtime
    owner_host = thread if thread is not None else run_context.node_repl_owner or agent_session
    existing = manager.get_server_config("node_repl")
    if existing is not None and not existing.source.startswith(SOURCE_PREFIX):
        if owner_host is not None and owner_host.node_repl_companions is not None:
            await owner_host.node_repl_companions.retire()
        return
    if owner_host is None:
        return
    if owner_host.node_repl_companions is None:
        owner_host.node_repl_companions = NodeReplCompanions()
    owner = owner_host.node_repl_companions
    config = await owner.admit(attachment, run_context.network_step.policy_for(attachment),
        feature_flags=execution.config.feature_flags, requires_read_only_tools=manager._requires_read_only_tools)
    if config is None:
        return
    from backend.mcp.policy import load_mcp_policy

    policy = load_mcp_policy(managed_settings_dir=manager._managed_settings_dir,
        config_stack=execution.config.config_layer_stack)
    [config] = manager._apply_mcp_policies([config], policy=policy)
    config = manager.environment_runtime.apply_authority(config)
    if execution.model_info is not None and execution.model_info.extra.get("node_repl_disabled", False):
        config.enabled = False
        config.disabled_reason = "The selected model disables node_repl"
    if not config.enabled:
        await owner.retire(config)
    manager.environment_runtime.node_repl_companions = owner
    manager._runtime_configs = tuple(item for item in manager._runtime_configs if item.name != "node_repl") + (config,)
    await manager.ensure_server_startup(config)
    await manager.capture_startup_readiness(names=(config.name,))
    manager.validate_required_startups(names=(config.name,))
    registry.mcp_tool_registry.sync()
