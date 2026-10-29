"""Discovery identities for reusable MCP definitions, separate from live authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import weakref


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def environment_object(runtime, environment_id: str):
    if runtime is None:
        return None
    snapshot = runtime.snapshot
    if snapshot is not None:
        captured = snapshot.captured_environments().get(environment_id)
        if captured is not None:
            return captured
        starting = next((item for item in snapshot.starting() if item.selection.environment_id == environment_id), None)
        if starting is not None:
            return starting.lease.environment
    return runtime.environment_manager.get_environment(environment_id)


def catalog_identity(config, runtime, capabilities, *, has_oauth_credentials: bool = False,
        requires_read_only_tools: bool = False):
    if requires_read_only_tools:
        return None
    environment = environment_object(runtime, config.environment_id)
    reference = weakref.ref(environment) if environment is not None else None
    if config.transport == "stdio":
        if any(source == "remote" for _name, source in config.env_vars):
            return None
        values = [(name, os.environ.get(name)) for name, _source in config.env_vars]
        transport = ("stdio", _fingerprint((config.command, config.args, config.env, config.env_vars,
            config.cwd, config.environment_id, capabilities, values)))
    elif config.transport == "http":
        explicit_auth = bool(config.bearer_token_env_var or any(
            name.lower() == "authorization" and value for name, value in config.headers.items())
            or any(name.lower() == "authorization" for name in config.env_http_headers))
        if config.headers_helper or config.oauth_client_id or config.oauth_callback_port is not None:
            return None
        if not explicit_auth and has_oauth_credentials:
            return None
        referenced = sorted(set(([config.bearer_token_env_var] if config.bearer_token_env_var else [])
            + list(config.env_http_headers.values())))
        values = [(name, os.environ.get(name)) for name in referenced] if config.credential_policy == "HostFallbackAllowed" else []
        transport = ("http", _fingerprint((config.url, config.bearer_token_env_var, config.headers,
            config.env_http_headers, config.environment_id, config.plugin_id is not None,
            config.credential_policy, capabilities, values)))
    else:
        return None
    fallback = (runtime.local_cwd if runtime is not None else str(Path.cwd())) if config.transport == "stdio" and config.cwd is None else None
    return config.name, transport, reference, fallback


def connection_identity(config, runtime, capabilities, credential_identity, *, requires_read_only_tools: bool):
    environment = environment_object(runtime, config.environment_id)
    reference = weakref.ref(environment) if environment is not None else None
    scope = runtime.permission_scope(config.environment_id) if runtime is not None else None
    fallback = (runtime.local_cwd if runtime is not None else str(Path.cwd())) if config.transport == "stdio" and config.cwd is None else None
    local_variables = {name for name, source in config.env_vars if source == "local"}
    if config.credential_policy == "HostFallbackAllowed":
        local_variables.update(config.env_http_headers.values())
        if config.bearer_token_env_var is not None:
            local_variables.add(config.bearer_token_env_var)
    value = (config.name, config.environment_id, config.transport, config.command, config.args, config.env,
        config.env_vars, config.cwd, config.url, config.headers, config.headers_helper, config.oauth_client_id,
        config.oauth_callback_port, config.bearer_token_env_var, config.env_http_headers, config.credential_policy,
        config.source, config.config_path, config.project_workspace, config.plugin_id, config.plugin_server_name,
        config.selected_root_id, capabilities, credential_identity, requires_read_only_tools,
        scope.identity() if scope is not None else None,
        [(name, os.environ.get(name)) for name in sorted(local_variables)])
    return reference, fallback, _fingerprint(value)
