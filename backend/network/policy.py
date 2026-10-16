"""Captured thread config translated once to Source network-policy inputs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from backend.permissions.environment_scope import executor_permission_profile


@dataclass(frozen=True)
class ThreadNetworkPolicyDraft:
    controller: dict[str, Any] | None
    requirements: dict[str, Any] | None

    def __post_init__(self):
        object.__setattr__(self, "controller", deepcopy(self.controller))
        object.__setattr__(self, "requirements", deepcopy(self.requirements))


def capture_thread_network_policy(stack) -> ThreadNetworkPolicyDraft:
    effective = stack.effective_config()
    permissions = effective.get("permissions", {})
    requirements = deepcopy(dict(stack.requirements.network_constraints)) or None
    if "network" in permissions:
        return ThreadNetworkPolicyDraft(permissions["network"], requirements)
    sandbox = effective.get("sandbox", {})
    legacy = sandbox.get("network")
    if not sandbox.get("enabled", False) or not legacy:
        return ThreadNetworkPolicyDraft(None, requirements)
    # MiniCode's existing settings names are translated into Source's typed
    # schema. Host normalization, policy ceilings and matching stay native.
    aliases = {"allowUnixSockets": "unix_sockets", "allowAllUnixSockets": "dangerously_allow_all_unix_sockets",
        "allowLocalBinding": "allow_local_binding", "allowUpstreamProxy": "allow_upstream_proxy",
        "enableSocks5": "enable_socks5", "enableSocks5Udp": "enable_socks5_udp"}
    controller = {aliases.get(key, key): deepcopy(value) for key, value in legacy.items()
        if key not in {"allowedDomains", "deniedDomains", "strictAllowlist", "allowManagedDomainsOnly", "httpProxyPort", "socksProxyPort"}}
    if "allowedDomains" in legacy or "deniedDomains" in legacy:
        domains = {name: "allow" for name in legacy.get("allowedDomains", ())}
        domains.update({name: "deny" for name in legacy.get("deniedDomains", ())})
        controller["domains"] = domains
    if "allowUnixSockets" in legacy:
        controller["unix_sockets"] = {name: "allow" for name in legacy["allowUnixSockets"]}
    for field_name, prefix, target in (("httpProxyPort", "http", "proxy_url"), ("socksProxyPort", "socks5", "socks_url")):
        if field_name in legacy:
            controller[target] = f"{prefix}://127.0.0.1:{legacy[field_name]}"
    if "strictAllowlist" in legacy or "allowManagedDomainsOnly" in legacy:
        requirements = dict(requirements or {})
        requirements["managed_allowed_domains_only"] = legacy.get("allowManagedDomainsOnly", legacy.get("strictAllowlist"))
    controller.setdefault("enabled", True)
    return ThreadNetworkPolicyDraft(controller, requirements)


@dataclass(frozen=True)
class NetworkExecutionPolicy:
    environment_id: str
    runtime: Any = field(repr=False, compare=False)
    source_input: dict[str, Any]
    effective_policy: dict[str, Any]
    remote_launch: dict[str, Any]

    def __post_init__(self):
        for name in ("source_input", "effective_policy", "remote_launch"):
            object.__setattr__(self, name, deepcopy(getattr(self, name)))

    def identity(self) -> dict[str, Any]:
        from backend.network.native import SOURCE_COMMIT

        return {"source_commit": SOURCE_COMMIT, "environment_id": self.environment_id, "source_input": deepcopy(self.source_input),
            "effective_policy": deepcopy(self.effective_policy), "proxy": deepcopy(self.remote_launch["proxy"])}

    def remote_config(self, *, environment_id: str, execution_id: str) -> dict[str, Any]:
        result = deepcopy(self.remote_launch)
        result.update(environmentId=environment_id, executionId=execution_id)
        return result

    def for_scope(self, scope) -> NetworkExecutionPolicy | None:
        captured = {**deepcopy(self.source_input), "permissionProfile": executor_permission_profile(scope.permission_profile),
            "platformOs": scope.platform_os, "windowsSandbox": scope.windows_sandbox_level}
        if captured == self.source_input:
            return self
        result = self.runtime.inspect_sync(captured)
        return NetworkExecutionPolicy(self.environment_id, self.runtime, captured, result["effectivePolicy"], result["remoteLaunch"]) if result["enabled"] else None

    async def prepare_local(self, env: dict[str, str], *, environment_id: str, execution_id: str,
            attribution_token: str, decider=None, observer=None):
        return await self.runtime.prepare_local(self.source_input, env, environment_id=environment_id,
            execution_id=execution_id, attribution_token=attribution_token, decider=decider, observer=observer)

    async def prepare_remote(self, *, environment_id: str, execution_id: str, attribution_token: str, decider=None,
            observer=None):
        return await self.runtime.prepare_remote(self.source_input, environment_id=environment_id,
            execution_id=execution_id, attribution_token=attribution_token, decider=decider, observer=observer)


def policy_input(draft, environment, *, exec_policy_sources=()) -> dict[str, Any]:
    return {"controller": deepcopy(draft.controller), "requirements": deepcopy(draft.requirements),
        "environmentPolicy": deepcopy(environment.config.network_policy),
        "permissionProfile": executor_permission_profile(environment.config.permission_profile),
        "platformOs": environment.peer_info.get("platformOs"),
        "windowsSandbox": environment.permission_scope.windows_sandbox_level,
        "execPolicySources": [source.payload() for source in exec_policy_sources]}
