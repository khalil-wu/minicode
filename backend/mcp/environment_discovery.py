"""MCP registrations discovered in an executor's captured URI domain."""
from __future__ import annotations

import asyncio
import json
import tomllib
from copy import deepcopy
from pathlib import Path

from backend.config_requirements import RequirementsLayerEntry, RequirementSource, compose_requirements
from backend.environments import ExecutorError
from backend.mcp.policy import mcp_policy_from_requirements
from backend.workspace.executor_paths import ExecutorPath


def _merge(target: dict, incoming: dict) -> None:
    for name, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(name), dict):
            _merge(target[name], value)
        else:
            target[name] = deepcopy(value)


async def discover_environment_http(manager, environment, warnings) -> list:
    if not environment.is_remote:
        return []
    try:
        async with asyncio.timeout(10):
            info = await environment.environment.info()
            capabilities = info["capabilities"]
            if not capabilities.get("environmentConfigRead", False):
                return []
            client = await environment.environment.get_client()
            response = await client.request("environment/config/read", {"cwd": environment.selection.cwd,
                "configPaths": [["mcp_servers"]], "requirementsPaths": [["mcp_servers"]]})
        requirements = compose_requirements(RequirementsLayerEntry(RequirementSource("unknown", layer["source"]),
            tomllib.loads(layer["toml"])) for layer in response["requirements"]["layers"])
        policy = mcp_policy_from_requirements(requirements.raw, source=f"executor {environment.selection.environment_id}")
        merged = {}
        for layer in response["config"]["layers"]:
            _merge(merged, tomllib.loads(layer["toml"]))
        configs = []
        for name, raw in merged.get("mcp_servers", {}).items():
            if "url" not in raw or not capabilities.get("httpHeaderEnvVars", False) and "bearer_token_env_var" in raw:
                continue
            if raw.get("environment_id", "local") != "local" or name == "codex_apps":
                continue
            if raw.get("http_headers_helper") or raw.get("env_http_headers"):
                warnings.append(f"executor-local HTTP header helpers are not supported for {name}")
                continue
            raw = {**raw, "environment_id": environment.selection.environment_id, "transport": "http"}
            config = manager._config_from_mapping(name, raw, source="executor", priority=0, base_dir=Path.cwd(),
                credential_policy="ExecutorOnly", executor_source=True)
            if policy.requirements is not None:
                requirement = policy.requirements.get(name)
                if requirement is None or not requirement.matches(config):
                    config.enabled, config.disabled_reason = False, "Disabled by executor-local MCP requirements"
            configs.append(config)
        return configs
    except (ExecutorError, OSError, TimeoutError, ValueError) as error:
        warnings.append(f"failed to discover executor-local MCP servers in {environment.selection.environment_id}: {error}")
        return []


def selected_plugin_configs(manager, discoveries, *, enabled, disabled_plugin_ids, warnings):
    configs, disabled_roots = [], set()
    for discovery in discoveries:
        root, bundle = discovery.root, discovery.bundle
        plugin = bundle.get("plugin")
        if bundle.get("error") is not None or plugin is None:
            continue
        try:
            manifest = json.loads(plugin["manifest"]["contents"])
            if not isinstance(manifest, dict) or not isinstance(manifest.get("name", ""), str):
                raise ValueError("plugin manifest must be an object with a string name")
            if not enabled or root.id in disabled_plugin_ids:
                disabled_roots.add(root.id)
                continue
            declaration = manifest.get("mcpServers")
            file = plugin.get("mcpConfig")
            if isinstance(declaration, dict):
                servers = declaration
            elif declaration is not None:
                if not isinstance(declaration, str) or not declaration.startswith("./"):
                    raise ValueError("plugin MCP path must use ./ syntax")
                expected = root.uri(root.path).joinpath(declaration).uri
                if file is None or file["path"] != expected:
                    warnings.append(f"exec-server capability bundle omitted declared MCP config for {root.id}")
                    continue
                servers = json.loads(file["contents"])
            elif file is not None:
                servers = json.loads(file["contents"])
            else:
                continue
            if not isinstance(servers, dict):
                raise ValueError("plugin MCP config must be an object")
            servers = servers.get("mcpServers", servers)
            for name, raw in servers.items():
                try:
                    if not isinstance(raw, dict):
                        raise ValueError("plugin MCP server must be an object")
                    raw = deepcopy(raw)
                    raw.pop("type", None)
                    raw["environment_id"] = root.environment_id
                    if "command" in raw:
                        raw["transport"] = "stdio"
                        cwd = ExecutorPath.resolve(raw.get("cwd") or root.path, cwd_uri=root.path, platform_os=root.platform_os)
                        if not cwd.is_relative_to(root.uri(root.path)):
                            raise ValueError("executor plugin MCP cwd must remain within plugin root")
                        raw["cwd"] = cwd.uri
                    if "url" in raw:
                        raw["transport"] = "http"
                    config = manager._config_from_mapping(name, raw, source=f"plugin:{root.id}", priority=0,
                        base_dir=Path.cwd(), executor_source=True, selected_root_id=root.id,
                        plugin_id=root.id, plugin_server_name=name,
                        credential_policy="ExecutorOnly" if root.environment_id != "local" and "url" in raw else "HostFallbackAllowed")
                    configs.append(config)
                except ValueError as error:
                    warnings.append(f"ignoring invalid exec-server-discovered MCP server {root.id}/{name}: {error}")
        except ValueError as error:
            warnings.append(f"failed to parse exec-server-discovered plugin manifest {root.id}: {error}")
    return configs, disabled_roots
