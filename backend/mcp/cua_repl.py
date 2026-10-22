"""Explicit installed Source or independently implemented MiniCode CUA runtime.

The configured cua_repl MCP server explicitly selects the installed Source
runtime or MiniCode's independent receiving helper. No Source package or native
binary is redistributed. In installed mode the Source package owns native
helper protocol/launch; MiniCode owns its pipe, real turn and approval lifecycle.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from mcp.client.stdio import get_default_environment

from backend.mcp.node_repl import NodeReplCompanions, NodeReplMCPClient, _Companion, _env_policy
from backend.runtime_paths import agent_runtime_root

SOURCE_PREFIX = "builtin:cua_repl:"
_HOST_SCRIPT = Path(__file__).with_name("computer_use_host.mjs")


def is_configured_cua(config) -> bool:
    configured = config.name == "cua_repl" and (
        "computer" in config.env.get("CUA_REPL_ENABLED_SURFACES", "").split(","))
    if configured and config.env.get("MINICODE_CUA_PROVIDER", "installed-source") not in {"installed-source", "minicode-win32"}:
        raise ValueError("Unknown MINICODE_CUA_PROVIDER; select installed-source or minicode-win32 explicitly")
    return configured


@dataclass(frozen=True)
class CuaRuntime:
    root: Path
    node: Path
    node_repl: Path
    modules: Path
    launcher: Path
    transport: Path
    helper: Path
    codex_cli: Path

    @classmethod
    def selected(cls, config):
        selected = Path(config.env["CUA_REPL_NODE_REPL_PATH"])
        if not selected.is_absolute():
            raise ValueError("CUA_REPL_NODE_REPL_PATH must select an absolute installed runtime")
        node_repl = selected.resolve(strict=True)
        root = node_repl.parent
        modules = root / "node_modules"
        sky = modules / "@oai/sky"
        paths = cls(root, root / "node.exe", node_repl, modules,
            modules / "@oai/cua-repl/bin/cua-repl.mjs",
            sky / "dist/project/cua/sky_js/src/targets/windows/internal/helper_transport.js",
            sky / "bin/windows/codex-computer-use.exe", root.parent.parent / "codex.exe")
        for path in (paths.node, paths.node_repl, paths.launcher, paths.transport, paths.helper, paths.codex_cli):
            if not path.is_file():
                raise FileNotFoundError(f"Selected CUA runtime file is unavailable: {path}")
        if (Path(config.command).resolve(strict=True) != paths.node or len(config.args) != 1
                or Path(config.args[0]).resolve(strict=True) != paths.launcher):
            raise ValueError("cua_repl command/args must select this runtime's Node and @oai/cua-repl launcher")
        if config.env["CUA_REPL_ENABLED_SURFACES"] != "computer":
            raise ValueError("MiniCode's installed CUA companion currently admits only the computer surface")
        package = json.loads((sky / "package.json").read_text(encoding="utf-8"))
        if package["name"] != "@oai/sky":
            raise ValueError("Selected CUA runtime has a different Sky package identity")
        return paths


class ComputerUseHost:
    def __init__(self, runtime: CuaRuntime, home: Path):
        self.runtime, self.home = runtime, home
        self.process = None
        self.pipe_path = "\\\\.\\pipe\\minicode-computer-use-" + uuid4().hex
        self.stderr = ""
        self.pending = {}
        self.sequence = 0
        self.ready = asyncio.get_running_loop().create_future()
        self.readers = ()
        self.references = 0

    async def start(self):
        self.home.mkdir(parents=True, exist_ok=True)
        env = {**get_default_environment(), "CODEX_HOME": str(self.home), "CODEX_CLI_PATH": str(self.runtime.codex_cli)}
        options = {"pipePath": self.pipe_path, "transportModulePath": str(self.runtime.transport),
            "helperPath": str(self.runtime.helper), "codexCliPath": str(self.runtime.codex_cli), "codexHome": str(self.home)}
        self.process = await asyncio.create_subprocess_exec(str(self.runtime.node), str(_HOST_SCRIPT), json.dumps(options),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, creationflags=0x08000000 if os.name == "nt" else 0)
        self.readers = (asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr()))
        await self.ready

    async def _read_stderr(self):
        while chunk := await self.process.stderr.read(65536):
            self.stderr += chunk.decode("utf-8", errors="replace")

    async def _read_stdout(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if message.get("type") == "ready":
                    if message["pipePath"] != self.pipe_path:
                        raise ConnectionError("Computer Use host published another pipe identity")
                    self.ready.set_result(None)
                else:
                    future = self.pending.pop(message["id"])
                    if "error" in message:
                        future.set_exception(ConnectionError(message["error"]))
                    else:
                        future.set_result(message.get("result"))
            code = await self.process.wait()
            await self.readers[1]
            error = ConnectionError(f"Original Computer Use host exited ({code}): {self.stderr.strip()}")
        except Exception as failure:
            error = failure
        if not self.ready.done():
            self.ready.set_exception(error)
        for future in self.pending.values():
            future.set_exception(error)
        self.pending.clear()

    async def request(self, method, params=None):
        self.sequence += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.sequence] = future
        self.process.stdin.write((json.dumps({"id": self.sequence, "method": method, "params": params}) + "\n").encode())
        await self.process.stdin.drain()
        return await future

    async def aclose(self):
        await self.request("shutdown")
        code = await self.process.wait()
        await asyncio.gather(*self.readers)
        if code != 0:
            raise ConnectionError(f"Original Computer Use host did not close cleanly ({code}): {self.stderr.strip()}")


_HOSTS = {}
_HOST_LOCK = asyncio.Lock()


async def _acquire_host(runtime, home):
    key = (runtime, home)
    async with _HOST_LOCK:
        host = _HOSTS.get(key)
        if host is None:
            host = ComputerUseHost(runtime, home)
            await host.start()
            _HOSTS[key] = host
        elif host.process.returncode is not None:
            raise ConnectionError(f"Original Computer Use host exited ({host.process.returncode}): {host.stderr.strip()}")
        host.references += 1
        return host


async def _release_host(host):
    async with _HOST_LOCK:
        host.references -= 1
        if host.references == 0:
            await host.aclose()
            del _HOSTS[host.runtime, host.home]


class CuaReplMCPClient(NodeReplMCPClient):
    def __init__(self, config, attachment, network, *, on_disconnect, on_tools_changed):
        from backend.mcp.native_cua_runtime import is_native_cua
        self.native = is_native_cua(config)
        self.runtime = None if self.native else CuaRuntime.selected(config)
        from backend.mcp.cua_approval_store import native_approval_home
        self.home = native_approval_home() if self.native else agent_runtime_root() / "computer-use"
        self.host = None
        super().__init__(config, attachment, network, on_disconnect=on_disconnect, on_tools_changed=on_tools_changed)
        self._startup_timeout = 120

    def _tool_request_timeout_seconds(self, params):
        return super()._tool_request_timeout_seconds(params) + 300

    async def close_thread(self):
        try:
            if self.native and self.host is not None:
                await self.host.request("closeThread", {})
        finally:
            await super().close_thread()

    @asynccontextmanager
    async def _executor_transport(self):
        if self.native:
            from backend.mcp.native_cua_runtime import NativeCuaTurnControl
            self.host = NativeCuaTurnControl(self)
            try:
                async with super()._executor_transport() as streams:
                    yield streams
            finally:
                self.host = None
            return
        host = await _acquire_host(self.runtime, self.home)
        self.host = host
        self._env = {**self._env, "CUA_REPL_NODE_REPL_PATH": str(self.runtime.node_repl),
            "NODE_REPL_NODE_PATH": str(self.runtime.node), "NODE_REPL_NODE_MODULE_DIRS": str(self.runtime.modules),
            "NODE_REPL_TRUSTED_CODE_PATHS": os.pathsep.join((str(self.home), str(self.runtime.modules))),
            "NODE_REPL_TRUSTED_SERVICES": json.dumps({"sky": "@oai/sky/service"}),
            "CODEX_CLI_PATH": str(self.runtime.codex_cli), "CODEX_HOME": str(self.home),
            "SKY_CUA_NATIVE_PIPE": "1", "SKY_CUA_NATIVE_PIPE_DIRECTORY": host.pipe_path}
        try:
            async with super()._executor_transport() as streams:
                yield streams
        finally:
            await _release_host(host)
            self.host = None

    async def call_tool(self, tool_name, arguments=None, *, request_owner=None, request_meta=None):
        owner = request_owner or {}
        if tool_name == "js" and (not owner.get("conversation_id") or not owner.get("turn_id")):
            raise ValueError("CUA execution requires its real conversation and turn owner")
        meta = dict(request_meta or {})
        if owner.get("conversation_id") and owner.get("turn_id"):
            meta["x-codex-turn-metadata"] = {"session_id": owner["conversation_id"],
                "thread_id": owner["conversation_id"], "turn_id": owner["turn_id"]}
        return await super().call_tool(tool_name, arguments, request_owner=request_owner, request_meta=meta)


class CuaReplCompanions(NodeReplCompanions):
    source_prefix = SOURCE_PREFIX
    client_type = CuaReplMCPClient

    async def admit(self, attachment, network, selected, *, app_access=None):
        from backend.mcp.native_cua_runtime import is_native_cua, native_cua_config
        native = is_native_cua(selected)
        if native:
            selected = native_cua_config(selected, attachment, app_access or {})
        elif attachment.is_remote or os.name != "nt":
            raise ValueError("The selected CUA runtime requires a local Windows receiving environment")
        identity = json.dumps([id(attachment.environment), attachment.permission_scope.identity(), _env_policy(attachment),
            attachment.config.mcp_policy, network.identity() if network is not None else None,
            selected.command, selected.args, selected.env], sort_keys=True)
        key = hashlib.sha256(identity.encode()).hexdigest()
        config = replace(selected, source=SOURCE_PREFIX + key, cwd=attachment.selection.cwd,
            environment_id=attachment.selection.environment_id if native else "local")
        async with self.lock:
            if self.active_key is not None and self.active_key != key:
                old = self.instances[self.active_key]
                old.retired = True
                if old.views == 0:
                    await self._close(old)
            self.active_key = key
            if key not in self.instances:
                self.instances[key] = _Companion(key, config, attachment, network)
            self.instances[key].retired = False
        return config


@dataclass(frozen=True)
class CuaTurn:
    companion: object
    thread_id: str
    turn_id: str

    async def finish_after(self, consumers):
        try:
            await asyncio.gather(*(asyncio.shield(task) for task in consumers))
        finally:
            client = self.companion.client
            if client is not None and client.host is not None:
                await client.host.request("closeActiveTurn", {"sessionId": self.thread_id, "turnId": self.turn_id})


async def register_cua_companion(run_context, agent_session, *, execution):
    prepared = run_context.environment_capabilities
    if prepared is None or prepared.runtime is None:
        return
    manager = prepared.mcp_manager
    selected = prepared.runtime.cua_repl_config
    thread = run_context.environment_thread_runtime
    owner_host = thread if thread is not None else run_context.node_repl_owner or agent_session
    if selected is None or not selected.enabled:
        if owner_host is not None and owner_host.cua_repl_companions is not None:
            await owner_host.cua_repl_companions.retire()
        return
    attachment = run_context.turn_environments.primary()
    if owner_host.cua_repl_companions is None:
        owner_host.cua_repl_companions = CuaReplCompanions()
    owner = owner_host.cua_repl_companions
    from backend.mcp.native_cua_runtime import is_native_cua
    app_access = None
    if is_native_cua(selected):
        stack = execution.config.config_layer_stack
        app_access = stack.effective_config().get("computer_use", {}) if stack is not None else {}
    config = await owner.admit(attachment, run_context.network_step.policy_for(attachment), selected,
        app_access=app_access)
    from backend.mcp.policy import load_mcp_policy

    policy = load_mcp_policy(managed_settings_dir=manager._managed_settings_dir,
        config_stack=execution.config.config_layer_stack)
    [config] = manager._apply_mcp_policies([config], policy=policy)
    config = manager.environment_runtime.apply_authority(config)
    if execution.model_info is not None and execution.model_info.extra.get("node_repl_disabled", False):
        config = replace(config, enabled=False, disabled_reason="The selected model disables node_repl")
    manager.environment_runtime.cua_repl_companions = owner
    manager._runtime_configs = tuple(item for item in manager._runtime_configs if item.name != config.name) + (config,)
    if not config.enabled:
        await owner.retire(config)
        await manager.register_config(config)
        return
    await manager.start_server(config)
    if config.required and manager.get_client(config.name) is None:
        raise RuntimeError("Required CUA runtime failed to initialize: " + manager._servers[config.name].last_error)
    run_context.cua_repl_turn = CuaTurn(owner.instances[config.source.removeprefix(SOURCE_PREFIX)], prepared.thread_id, prepared.turn_id)
