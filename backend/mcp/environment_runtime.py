"""Source per-server placement and catalog authority from captured selections."""
from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from pathlib import PurePosixPath, PureWindowsPath
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from backend.mcp.policy import mcp_policy_from_requirements
from backend.workspace.executor_paths import ExecutorPath

if TYPE_CHECKING:
    from backend.agent.environment_selection import TurnEnvironmentSnapshot
    from backend.mcp.manager import MCPServerConfig

_MCP_UNIX_CORE = ("HOME", "LOGNAME", "PATH", "SHELL", "USER", "__CF_USER_TEXT_ENCODING", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ")
_MCP_WINDOWS_CORE = ("PATH", "PATHEXT", "SHELL", "COMSPEC", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "USERNAME", "USERDOMAIN",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
    "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR", "POWERSHELL", "PWSH")


@dataclass(frozen=True)
class MCPExecutionBinding:
    environment_id: str
    lease: object
    cwd_uri: str
    workspace_root_uris: tuple[str, ...]
    remote_env_policy: dict
    bearer_token_env_var: str | None


def remote_mcp_environment_policy(names: tuple[str, ...]) -> dict:
    core = _MCP_WINDOWS_CORE if os.name == "nt" else _MCP_UNIX_CORE
    return {"inherit": "all" if names else "core", "ignoreDefaultExcludes": True,
        "exclude": [], "set": {}, "includeOnly": [*core, "SYSTEMROOT", "TEMP", "TMP", *names] if names else []}


@dataclass
class MCPAppEnvironmentLease:
    runtime: object
    borrow_released: bool = False

    async def aclose(self) -> None:
        runtime = self.runtime
        if runtime is not None:
            if not self.borrow_released:
                runtime._app_borrowers -= 1
                self.borrow_released = True
            if runtime._app_borrowers == 0 and runtime._app_close_requested:
                await runtime.aclose()
            self.runtime = None


class MCPEnvironmentRuntime:
    def __init__(self, environment_manager, turn_environments: TurnEnvironmentSnapshot | None, *, local_cwd: str):
        self.environment_manager = environment_manager
        self.snapshot = turn_environments.clone() if turn_environments is not None else None
        self.local_cwd = local_cwd
        self.borrowed = {}
        self.node_repl_companions = None
        self.cua_repl_companions = None
        self.cua_repl_config = None
        self._app_borrowers = 0
        self._app_close_requested = False
        self._close_lock = asyncio.Lock()

    def acquire_app_lease(self) -> MCPAppEnvironmentLease:
        self._app_borrowers += 1
        return MCPAppEnvironmentLease(self)

    def permission_scope(self, environment_id: str):
        if self.snapshot is None:
            return None
        ready = next((entry for entry in self.snapshot.turn_environments()
            if entry.selection.environment_id == environment_id), None)
        return ready.permission_scope if ready is not None else None

    def apply_authority(self, config: MCPServerConfig) -> MCPServerConfig:
        if not config.enabled or self.snapshot is None:
            return config
        from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_COMPANION_SOURCE

        declaration = self.cua_repl_config if config.source.startswith(CUA_COMPANION_SOURCE) else config
        selection = next((selection for selection in self.snapshot.all_selections()
            if selection.environment_id == config.environment_id), None)
        if selection is None:
            allowed = config.environment_id == "local" or config.selected_root_id is not None
        elif selection.config.kind == "FromThread":
            allowed = True
        elif selection.config.kind in {"Pending", "Failed"}:
            allowed = False
        else:
            policy = selection.config.value.mcp_policy
            if policy is None:
                allowed = True
            else:
                parsed = mcp_policy_from_requirements({"mcp_servers": policy.get("servers"), "plugins": policy.get("plugins")},
                    source=f"environment {config.environment_id}")
                if declaration.plugin_id is None:
                    requirement = parsed.requirements.get(declaration.name) if parsed.requirements is not None else None
                    allowed = parsed.requirements is None or requirement is not None and requirement.matches(declaration)
                else:
                    plugin_rules = parsed.plugin_requirements
                    has_plugin_servers = bool(plugin_rules)
                    rules = plugin_rules.get(declaration.plugin_id) if plugin_rules is not None else None
                    requirement = rules.get(declaration.plugin_server_name) if rules is not None else None
                    allowed = parsed.requirements != {} and (not has_plugin_servers or requirement is not None and requirement.matches(declaration))
        return config if allowed else replace(config, enabled=False,
            disabled_reason=f"MCP server is unavailable under environment `{config.environment_id}` authority")

    async def bind_server(self, config: MCPServerConfig) -> MCPExecutionBinding | None:
        if config.credential_policy == "ExecutorOnly" and (config.environment_id == "local" or config.transport != "http"
            or config.env_http_headers or config.headers_helper):
            raise ValueError(f"executor-discovered MCP server '{config.name}' requires a remote HTTP transport without host environment headers or helpers")
        lease = None
        cwd, roots = self.local_cwd, ()
        if self.snapshot is not None:
            ready = next((entry for entry in self.snapshot.turn_environments()
                if entry.selection.environment_id == config.environment_id), None)
            if ready is not None:
                lease, cwd, roots = ready.lease, ready.selection.cwd, ready.config.workspace_roots
            else:
                starting = next((entry for entry in self.snapshot.starting()
                    if entry.selection.environment_id == config.environment_id), None)
                if starting is not None:
                    lease, cwd = starting.lease, starting.selection.cwd
                elif config.environment_id in self.snapshot.captured_environments():
                    if config.environment_id == "local" and config.transport == "http":
                        return None
                    raise ValueError(f"MCP server `{config.name}` references unavailable environment `{config.environment_id}`")
        if lease is None:
            environment = self.environment_manager.get_environment(config.environment_id)
            if environment is not None:
                if config.environment_id not in self.borrowed:
                    self.borrowed[config.environment_id] = environment.borrow()
                lease = self.borrowed[config.environment_id]
        if lease is None:
            if config.environment_id == "local" and config.transport == "http":
                return None
            if config.environment_id == "local":
                raise ValueError(f"local stdio MCP server `{config.name}` requires a local environment")
            raise ValueError(f"MCP server `{config.name}` references unknown environment id `{config.environment_id}`")
        if config.environment_id != "local" and config.transport not in {"stdio", "http"}:
            raise ValueError("executor MCP servers require stdio or Streamable HTTP transport")
        if config.environment_id != "local" and config.transport == "stdio" and not config.cwd:
            raise ValueError("executor stdio server requires an explicit cwd")
        if config.environment_id != "local" and config.transport == "stdio":
            info = await lease.environment.info()
            platform = info.get("platformOs")
            native = PureWindowsPath(config.cwd) if platform == "windows" else PurePosixPath(config.cwd)
            if not config.cwd.startswith("file:") and not native.is_absolute():
                raise ValueError("executor stdio server requires an absolute cwd")
            cwd = ExecutorPath.resolve(config.cwd, cwd_uri=cwd, platform_os=platform).uri
        bearer = None
        if config.environment_id != "local" and config.bearer_token_env_var is not None:
            info = await lease.environment.info()
            if info["capabilities"].get("httpHeaderEnvVars", False):
                bearer = config.bearer_token_env_var
            elif config.credential_policy == "ExecutorOnly":
                raise ValueError(f"MCP server '{config.name}' requires executor-side environment credential resolution; "
                    "update the executor to a version that supports it (host fallback is disabled)")
        remote_names = tuple(name for name, source in config.env_vars if source == "remote")
        return MCPExecutionBinding(config.environment_id, lease, cwd, tuple(roots),
            remote_mcp_environment_policy(remote_names), bearer)

    async def aclose(self) -> None:
        self._app_close_requested = True
        if self._app_borrowers:
            return
        async with self._close_lock:
            for lease in self.borrowed.values():
                await lease.aclose()
            self.borrowed.clear()
            if self.snapshot is not None:
                await self.snapshot.aclose()
                self.snapshot = None


@dataclass
class PreparedEnvironmentCapabilities:
    mcp_manager: object
    skill_manager: object
    warnings: tuple[str, ...]
    discoveries: tuple
    runtime: MCPEnvironmentRuntime | None
    owns_mcp_manager: bool
    thread_id: str
    turn_id: str

    def validate_required_skills(self) -> None:
        if self.runtime is not None and self.runtime.snapshot is not None:
            self.skill_manager.validate_required_skills(self.runtime.snapshot.required_skills())

    def bind_registry(self, registry, artifact_store) -> None:
        from backend.services.tool_registry_factory import register_mcp_tools
        from backend.tools.mcp_tools import (ListMcpResourcesTool, ReadMcpResourceTool, ListMcpResourceTemplatesTool,
            SubscribeMcpResourceTool, UnsubscribeMcpResourceTool, ListMcpResourceNotificationsTool, ListMcpPromptsTool, GetMcpPromptTool)
        from backend.tools.skill_tools import bind_skill_tools

        previous = registry.mcp_tool_registry
        if previous is not None:
            for server_name in tuple(previous._server_tools):
                previous.unregister_server_tools(server_name)
        registry.mcp_tool_registry = None
        for tool in (ListMcpResourcesTool(self.mcp_manager), ReadMcpResourceTool(self.mcp_manager, artifact_store),
            ListMcpResourceTemplatesTool(self.mcp_manager), SubscribeMcpResourceTool(self.mcp_manager),
            UnsubscribeMcpResourceTool(self.mcp_manager), ListMcpResourceNotificationsTool(self.mcp_manager),
            ListMcpPromptsTool(self.mcp_manager), GetMcpPromptTool(self.mcp_manager)):
            if registry.get_tool(tool.name) is not None:
                registry.unregister(tool.name)
            tool.prepare_call = self.mcp_manager.prepare_server_call if self.mcp_manager is not None else None
            registry.register(tool)
        if self.mcp_manager is not None:
            register_mcp_tools(registry, self.mcp_manager, artifact_store, prepare_call=self.mcp_manager.prepare_server_call)
        bind_skill_tools(registry, self.skill_manager)

    def retire(self, run_context) -> asyncio.Task:
        context = run_context.context_builder
        tool_context = context._tool_execution_context if context is not None else None
        call_tasks = tool_context.cleanup_tasks_by_call if tool_context is not None else None
        code = run_context.code_execution
        cells = tuple(cell.task for cell in code.cells.values()
            if cell.task is not None and cell.registry.mcp_tool_registry is not None
            and cell.registry.mcp_tool_registry._mcp_manager is self.mcp_manager) if code is not None else ()
        consumers = tuple(task for task in (*cells, *run_context.lifecycle_cleanup_tasks,
            *self.mcp_manager.pending_requests()) if not task.done())

        async def close_after_consumers():
            # Cells retain their original registry even while waiting on a
            # timer. Timed-out tools retain their actual execution tasks in
            # the existing cleanup owner. Freeze those borrowers before this
            # retirement can itself be handed to that owner at turn end.
            if consumers:
                await asyncio.wait(consumers)
            # The registry can finish a cancelled wrapper after handing its
            # still-running child to this original call owner. Follow that
            # handoff without discovering retirement tasks in the shared
            # lifecycle set, or a later turn's replacement tool context.
            while call_tasks is not None and (pending := {task for task in call_tasks.values() if not task.done()}):
                await asyncio.wait(pending)
            # A retained cell can start its final RPC after retirement began;
            # the original client still owns any cancellation-resistant call.
            while pending := set(self.mcp_manager.pending_requests()):
                await asyncio.wait(pending)
            await self.aclose()

        return asyncio.create_task(close_after_consumers(), name="mcp-capabilities-retire")

    async def aclose(self) -> None:
        try:
            if self.owns_mcp_manager:
                await self.mcp_manager.stop_all()
        finally:
            if self.runtime is not None:
                await self.runtime.aclose()


async def prepare_environment_capabilities(*, turn_environments, host_skill_manager, host_mcp_manager,
    feature_flags, thread_id: str, turn_id: str, session_source: str, isolated: bool,
    environment_manager, local_cwd: str, is_subagent: bool = False,
    previous: PreparedEnvironmentCapabilities | None = None, disabled_plugin_ids: tuple[str, ...] = (),
    disabled_skill_paths=None, restriction_product: str | None = "codex",
    selected_capability_roots: tuple[dict, ...] = ()) -> PreparedEnvironmentCapabilities:
    from backend.mcp.environment_discovery import discover_environment_http, selected_plugin_configs
    from backend.mcp.manager import MCPServerManager
    from backend.mcp.node_repl import SOURCE_PREFIX as NODE_COMPANION_SOURCE
    from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_COMPANION_SOURCE, is_configured_cua
    from backend.skills.environment_catalog import ExecutorCapabilityRoot, ExecutorSkillCatalog, discover_executor_roots
    from backend.skills.manager import SkillManager

    del session_source  # The owner supplies explicit isolation/subagent/product facts.
    skills = host_skill_manager.clone_host_catalog() if host_skill_manager is not None else SkillManager()
    if turn_environments is None:
        return PreparedEnvironmentCapabilities(host_mcp_manager, skills, (), (), None, False, thread_id, turn_id)
    runtime = MCPEnvironmentRuntime(environment_manager, turn_environments, local_cwd=local_cwd)
    prepared = PreparedEnvironmentCapabilities(None, skills, (), (), runtime, False, thread_id, turn_id)
    try:
        ready = tuple(runtime.snapshot.turn_environments())
        restricted = any(not environment.permission_scope.has_full_disk_read_access() for environment in ready)
        high_level = restricted or feature_flags.enabled("executor_capability_discovery")
        roots = {}
        resolution_warnings = []
        attachments = [(environment.config_origin, selected)
            for environment in ready for selected in environment.config.selected_capability_roots]
        combined = [next((root for origin, root in attachments if origin == "Thread" and root["id"] == thread_root["id"]
            and root["location"]["environmentId"] == thread_root["location"]["environmentId"]), thread_root)
            for thread_root in selected_capability_roots]
        combined.extend(root for _origin, root in attachments)
        attachment_count = 0
        captured = runtime.snapshot.captured_environments()
        for index, selected in enumerate(combined):
            location = selected["location"]
            environment_id = location["environmentId"]
            if environment_id not in captured:
                resolution_warnings.append(f"ignoring capability root {selected['id']} without a captured turn environment")
                continue
            if selected["id"] in roots:
                kept = roots[selected["id"]]
                if (kept.environment_id, kept.path) != (environment_id, location["path"]):
                    resolution_warnings.append(f"ignoring selected capability root {selected['id']} with conflicting location")
                continue
            if index >= len(selected_capability_roots):
                if attachment_count == 256:
                    resolution_warnings.append("ignoring excess selected capability roots from ready environments")
                    break
                attachment_count += 1
            environment = next((entry for entry in ready if entry.selection.environment_id == environment_id), None)
            if environment is None:
                resolution_warnings.append(f"selected capability root {selected['id']} has no ready turn environment")
                continue
            roots[selected["id"]] = ExecutorCapabilityRoot(selected["id"], environment_id, location["path"],
                environment.environment, environment.permission_scope.sandbox_context() if restricted else None,
                environment.peer_info.get("platformOs"), high_level, restricted)
        cache = {entry.root.id: entry for entry in previous.discoveries} if previous is not None and previous.thread_id == thread_id else {}
        selected = tuple(roots.values())
        missing = tuple(root for root in selected if root.id not in cache or cache[root.id].root != root
            or cache[root.id].retryable or cache[root.id].bundle.get("error") is not None
            or cache[root.id].bundle.get("warnings") or not high_level)
        discovered = await discover_executor_roots(missing)
        cache.update((entry.root.id, entry) for entry in discovered)
        discoveries = tuple(cache[root.id] for root in selected)
        catalog = ExecutorSkillCatalog(discoveries, disabled_paths=disabled_skill_paths, restriction_product=restriction_product)
        skills.attach_executor_catalog(catalog)
        prepared.discoveries = discoveries
        source = host_mcp_manager
        manager = MCPServerManager(config_path=source._config_path if source is not None else None,
            workspace_root=source.workspace_root if source is not None else None,
            elicitation_handler=source._elicitation_handler if source is not None else None,
            on_status_change=source._on_status_change if source is not None else None,
            managed_settings_dir=source._managed_settings_dir if source is not None else None,
            requirements_path=source._requirements_path if source is not None else None,
            environment_runtime=runtime, runtime_configs=(),
            startup_policy="LazyWhenCached" if is_subagent else "Eager",
            cached_catalog=tuple(entry for entry in source.catalog_snapshot()
                if not entry[0].source.startswith((NODE_COMPANION_SOURCE, CUA_COMPANION_SOURCE))) if source is not None else ())
        prepared.mcp_manager = manager
        prepared.owns_mcp_manager = True
        # Dynamic kernels are admitted only after this step captures its
        # model/network scope and receiving owner; they are not host config.
        configs = {config.name: deepcopy(config) for config in source.load_config()
            if not config.source.startswith((NODE_COMPANION_SOURCE, CUA_COMPANION_SOURCE))} if source is not None else {}
        warnings = [*resolution_warnings, *catalog.warnings]
        plugins, disabled_roots = selected_plugin_configs(manager, discoveries,
            enabled=feature_flags.enabled("plugins", True), disabled_plugin_ids=disabled_plugin_ids, warnings=warnings)
        if disabled_roots:
            catalog.entries = [entry for entry in catalog.entries if entry.authority_id not in disabled_roots]
        for config in reversed(plugins):
            current = configs.get(config.name)
            if current is None or current.source.startswith("plugin:"):
                configs[config.name] = config
        registered = set()
        if not isolated:
            for environment in ready:
                for config in await discover_environment_http(manager, environment, warnings):
                    current = configs.get(config.name)
                    if config.name in registered or current is not None and (current.environment_id != config.environment_id or not current.source.startswith("plugin:")):
                        continue
                    registered.add(config.name)
                    configs[config.name] = config
        if source is not None:
            from backend.config import load_config_layer_stack
            from backend.mcp.policy import load_mcp_policy

            stack = load_config_layer_stack(cwd=source.workspace_root, requirements_path=source._requirements_path,
                managed_settings_dir=source._managed_settings_dir)
            policy = load_mcp_policy(managed_settings_dir=source._managed_settings_dir, config_stack=stack)
            configs = {config.name: config for config in manager._apply_mcp_policies(list(configs.values()), policy=policy)}
        manager._runtime_configs = tuple(configs.values())
        cua_configs = [config for config in configs.values() if is_configured_cua(config)]
        if len(cua_configs) > 1:
            raise ValueError("A receiving thread must select exactly one CUA computer runtime")
        if cua_configs:
            runtime.cua_repl_config = cua_configs[0]
            manager._runtime_configs = tuple(config for config in configs.values() if config is not cua_configs[0])
        await manager.start_all()
        prepared.warnings = tuple(warnings)
        return prepared
    except BaseException:
        await prepared.aclose()
        raise
