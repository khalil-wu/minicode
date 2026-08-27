from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from backend.config import DATA_ROOT, PROJECT_ROOT
from backend.mcp.client import MCPClient, MCPToolDef, MCPTransport
from backend.mcp.project_settings import (
    PROJECT_MCP_APPROVED,
    PROJECT_MCP_PENDING,
    project_mcp_config_paths,
    project_mcp_server_status,
)
from backend.mcp.policy import (
    MCPPolicy,
    load_enterprise_mcp_payload,
    load_mcp_policy,
)
from backend.mcp.transport import mcp_transport_from_mapping, normalize_mcp_transport
from backend.plugins.layout import plugin_manifest_path
from backend.workspace.state import get_explicit_active_workspace_root

logger = logging.getLogger(__name__)

_DYNAMIC_WORKSPACE = object()

MCP_CONFIG_FILE = DATA_ROOT / ".mcp.json"

# Remote reconnect is bounded. The first attempt is immediate; delay is only
# applied between failed attempts.
MAX_RECONNECT_ATTEMPTS = 3
INITIAL_RECONNECT_BACKOFF_SECONDS = 1.0
MAX_RECONNECT_BACKOFF_SECONDS = 30.0
_REMOTE_RECONNECT_TRANSPORTS = frozenset({"sse", "http", "ws"})
MCP_REQUEST_TIMEOUT_SECONDS = 60.0
MCP_DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
MCP_DEFAULT_TOOL_TIMEOUT_SECONDS = 100_000.0
_MCP_SERVER_FIELDS = frozenset({
    "transport", "command", "args", "env", "env_vars", "cwd", "url",
    "headers", "headers_helper", "oauth", "auto_start", "startup_timeout_sec",
    "tool_timeout_sec", "required", "supports_parallel_tool_calls",
    "enabled_tools", "disabled_tools", "default_tools_approval_mode", "tools",
    "requires_user_action", "setup_hint", "docs_url",
})


class ServerStatus(Enum):
    OFFLINE = "offline"
    STARTING = "starting"
    CONNECTED = "connected"
    ERROR = "error"
    RECONNECTING = "reconnecting"


class MCPAuthStatus(Enum):
    """Authentication state reported independently from transport lifecycle.

    A server can be configured or reachable without that proving an
    authenticated external account.
    """

    UNSUPPORTED = "unsupported"
    NOT_LOGGED_IN = "not_logged_in"
    OAUTH = "oauth"


# Lifecycle phase classification prefers the typed attributes carried by
# ``MCPAuthenticationError``. Some older external MCP servers only return a
# string, so keep a deliberately narrow fallback.  A bare word such as
# "token", "login" or "auth" is not evidence of a credential problem: those
# words regularly occur in ordinary tool errors and used to strand servers in
# an unrecoverable state.
_AUTH_ERROR_PATTERNS = (
    re.compile(r"\b(?:http\s*)?(?:401|403)\b", re.IGNORECASE),
    re.compile(r"\b(?:unauthori[sz]ed|forbidden|authentication\s+(?:required|failed)|authorization\s+(?:required|failed)|login\s+required)\b", re.IGNORECASE),
    re.compile(r"\b(?:invalid|missing|expired)\s+(?:api[ _-]?key|access[ _-]?token|bearer[ _-]?token|credential)s?\b", re.IGNORECASE),
    re.compile(r"\b(?:api[ _-]?key|access[ _-]?token|bearer[ _-]?token|credential)s?\s+(?:is\s+)?(?:invalid|missing|expired|required)\b", re.IGNORECASE),
    re.compile(r"\boauth(?:\s+token)?\s+(?:is\s+)?(?:invalid|missing|expired|required)\b", re.IGNORECASE),
)
_EXPIRED_MARKERS = ("expired", "expire", "renew", "re-auth", "reauth", "re-login")


def _has_auth_fallback_evidence(text: str) -> bool:
    return any(pattern.search(text) for pattern in _AUTH_ERROR_PATTERNS)

_PHASE_DEFAULT_MESSAGE = {
    "connecting": "Connecting…",
    "connected": "Connected",
    "reconnecting": "Reconnecting…",
    "auth_required": "Authentication required",
    "expired": "Credentials expired; re-authentication required",
    "failed": "Connection failed",
    "stopped": "Stopped",
}


def classify_mcp_phase(
    status: "ServerStatus",
    last_error: str,
    error: BaseException | None = None,
) -> tuple[str, bool, bool]:
    """Map (status, last_error) to (phase, recoverable, requires_user_action).

    phase ∈ {connecting, connected, reconnecting, auth_required, expired, failed,
    stopped}. Auth/expiry is inferred conservatively from the error text and is
    the only class flagged requires_user_action=True / recoverable=False — a bare
    retry cannot fix expired or missing credentials. Network/unknown failures stay
    recoverable so the UI/agent can offer a restart.
    """
    if status == ServerStatus.CONNECTED:
        return ("connected", True, False)
    if status == ServerStatus.STARTING:
        return ("connecting", True, False)
    if status == ServerStatus.RECONNECTING:
        return ("reconnecting", True, False)
    if status == ServerStatus.OFFLINE:
        return ("stopped", True, False)
    if error is not None and bool(getattr(error, "mcp_auth_required", False)):
        return (
            "expired" if bool(getattr(error, "mcp_auth_expired", False)) else "auth_required",
            False,
            True,
        )
    # ServerStatus.ERROR — classify by message text.
    text = (last_error or "").lower()
    if _has_auth_fallback_evidence(text):
        if any(marker in text for marker in _EXPIRED_MARKERS):
            return ("expired", False, True)
        return ("auth_required", False, True)
    return ("failed", True, False)


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    headers_helper: str = ""
    oauth_client_id: str = ""
    oauth_callback_port: int | None = None
    cwd: str | None = None
    transport: str = "stdio"
    url: str | None = None
    auto_start: bool = True
    source: str = "local"
    priority: int = 1000
    requires_user_action: bool = False
    setup_hint: str = ""
    docs_url: str = ""
    approval_status: str = "not_applicable"
    config_path: str = ""
    project_workspace: str = ""
    startup_timeout_sec: float | None = None
    tool_timeout_sec: float | None = None
    required: bool = False
    supports_parallel_tool_calls: bool = False
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = field(default_factory=list)
    default_tools_approval_mode: str | None = None
    tool_approval_modes: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    disabled_reason: str = ""


def validate_mcp_server_config(config: MCPServerConfig) -> None:
    """Validate the MCP SDK's structured command/args transport contract.

    Stdio servers are spawned as an executable plus an argv array, not through
    a shell, so ordinary argv characters such as ``$``, ``(``, or ``&`` are
    data rather than shell syntax.
    Security policy belongs at the config trust/policy layer, not in a guessed
    executable-name allowlist.
    """

    transport = normalize_mcp_transport(config.transport)
    # Validation is also the canonical normalization boundary.  Keeping the
    # normalized value on the config prevents aliases accepted at ingress from
    # falling through to the stdio branch when the client is constructed.
    config.transport = transport
    if config.command is None:
        config.command = "python" if transport == "stdio" else ""
    for field_name in ("startup_timeout_sec", "tool_timeout_sec"):
        value = getattr(config, field_name)
        if value is None:
            continue
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError(f"{field_name} must be a positive number")
        setattr(config, field_name, float(value))
    if not isinstance(config.required, bool):
        raise ValueError("required must be a boolean")
    if not isinstance(config.supports_parallel_tool_calls, bool):
        raise ValueError("supports_parallel_tool_calls must be a boolean")
    for field_name, values in (
        ("enabled_tools", config.enabled_tools),
        ("disabled_tools", config.disabled_tools),
    ):
        if values is None and field_name == "enabled_tools":
            continue
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"{field_name} must be an array of non-empty strings")
    approval_modes = {"auto", "prompt", "writes", "approve"}
    if (
        config.default_tools_approval_mode is not None
        and config.default_tools_approval_mode not in approval_modes
    ):
        raise ValueError("default_tools_approval_mode is invalid")
    if not isinstance(config.tool_approval_modes, dict) or any(
        not isinstance(tool_name, str)
        or not tool_name.strip()
        or mode not in approval_modes
        for tool_name, mode in config.tool_approval_modes.items()
    ):
        raise ValueError("tool approval modes must map non-empty tool names to valid modes")
    if transport == "stdio":
        incompatible = [
            field_name
            for field_name, value in (
                ("url", config.url),
                ("headers", config.headers),
                ("headers_helper", config.headers_helper),
                ("oauth", config.oauth_client_id or config.oauth_callback_port),
            )
            if _has_nonempty_config_value(value)
        ]
        if incompatible:
            raise ValueError(
                f"fields {', '.join(incompatible)} are not supported for stdio transport"
            )
        if not isinstance(config.command, str) or not config.command.strip():
            raise ValueError("stdio transport requires a non-empty command")
        if "\x00" in config.command:
            raise ValueError("stdio command cannot contain NUL")
        if not isinstance(config.args, list) or any(not isinstance(arg, str) for arg in config.args):
            raise ValueError("stdio args must be an array of strings")
        if any("\x00" in arg for arg in config.args):
            raise ValueError("stdio args cannot contain NUL")
        if not isinstance(config.env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in config.env.items()
        ):
            raise ValueError("stdio env must be an object of string values")
        if any("\x00" in key or "\x00" in value for key, value in config.env.items()):
            raise ValueError("stdio env cannot contain NUL")
        if config.cwd is not None and not isinstance(config.cwd, str):
            raise ValueError("stdio cwd must be a string")
        if isinstance(config.cwd, str) and "\x00" in config.cwd:
            raise ValueError("stdio cwd cannot contain NUL")
        return
    incompatible = [
        field_name
        for field_name, value in (
            ("command", config.command),
            ("args", config.args),
            ("env", config.env),
            ("cwd", config.cwd),
        )
        if _has_nonempty_config_value(value)
    ]
    if incompatible:
        raise ValueError(
            f"fields {', '.join(incompatible)} are not supported for {transport} transport"
        )
    if transport == "ws" and (
        _has_nonempty_config_value(config.oauth_client_id)
        or _has_nonempty_config_value(config.oauth_callback_port)
    ):
        raise ValueError("oauth is not supported for ws transport")
    if not isinstance(config.headers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in config.headers.items()
    ):
        raise ValueError("remote MCP headers must be an object of string values")
    if any("\x00" in key or "\x00" in value for key, value in config.headers.items()):
        raise ValueError("remote MCP headers cannot contain NUL")
    if config.oauth_callback_port is not None and not (1 <= config.oauth_callback_port <= 65535):
        raise ValueError("MCP OAuth callback_port must be between 1 and 65535")
    url = str(config.url or "").strip()
    if not url:
        raise ValueError(f"MCP {transport} transport requires a URL")
    scheme = urlparse(url).scheme.lower()
    allowed_schemes = {"ws", "wss"} if transport == "ws" else {"http", "https"}
    if scheme not in allowed_schemes:
        raise ValueError(f"invalid {transport} URL scheme '{scheme or '(missing)'}'")


@dataclass
class MCPServerState:
    config: MCPServerConfig
    client: MCPClient | None = None
    status: ServerStatus = ServerStatus.OFFLINE
    tools: list[MCPToolDef] = field(default_factory=list)
    retry_count: int = 0
    last_error: str = ""
    last_exception: BaseException | None = None
    auth_status: MCPAuthStatus = MCPAuthStatus.UNSUPPORTED
    operation_failures: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_status_dict(self) -> dict[str, Any]:
        phase, recoverable, requires_user_action = classify_mcp_phase(
            self.status,
            self.last_error,
            self.last_exception,
        )
        requires_user_action = requires_user_action or (
            self.config.requires_user_action and self.status != ServerStatus.CONNECTED
        )
        requires_user_action = requires_user_action or (
            self.config.source == "project"
            and self.config.approval_status == PROJECT_MCP_PENDING
        )
        capabilities = None
        if self.status == ServerStatus.CONNECTED and self.client:
            capabilities = self.client.server_capabilities
        capability_payload = {
            key: bool(getattr(capabilities, key, False))
            for key in (
                "tools",
                "resources",
                "resources_subscribe",
                "resources_list_changed",
                "prompts",
                "logging",
            )
        }
        cleanup = (
            dict(self.client.cleanup_status)
            if self.client is not None
            else {
                "pending": False,
                "reason": "",
                "requested_at": None,
                "completed_at": None,
            }
        )
        return {
            "name": self.config.name,
            "status": self.status.value,
            "tools_count": len(self.tools),
            # MCP capability flags are negotiated during initialize. Expose
            # the negotiated contract to the connector UI without issuing
            # extra list/read requests from the status path.
            "capabilities": capability_payload,
            "cleanup": cleanup,
            "operation_failures": list(self.operation_failures.values()),
            "error": self.last_error if self.status == ServerStatus.ERROR else "",
            "source": self.config.source,
            "approval_status": self.config.approval_status,
            "config_path": self.config.config_path,
            "project_workspace": self.config.project_workspace,
            "priority": self.config.priority,
            "transport": self.config.transport,
            "required": self.config.required,
            "supports_parallel_tool_calls": self.config.supports_parallel_tool_calls,
            "enabled_tools": self.config.enabled_tools,
            "disabled_tools": list(self.config.disabled_tools),
            "default_tools_approval_mode": self.config.default_tools_approval_mode,
            "enabled": self.config.enabled,
            "disabled_reason": self.config.disabled_reason,
            "auth_status": self.auth_status.value,
            "phase": phase,
            "recoverable": recoverable,
            "requires_user_action": requires_user_action,
            "setup_hint": self.config.setup_hint,
            "docs_url": self.config.docs_url,
            "operation_failures": list(self.operation_failures.values()),
            **(
                {
                    "reconnect_attempt": self.retry_count,
                    "max_reconnect_attempts": MAX_RECONNECT_ATTEMPTS,
                }
                if self.status == ServerStatus.RECONNECTING
                else {}
            ),
        }

    def to_lifecycle_dict(self) -> dict[str, Any]:
        """Single-server lifecycle event payload (mcp.lifecycle)."""
        phase, recoverable, requires_user_action = classify_mcp_phase(
            self.status,
            self.last_error,
            self.last_exception,
        )
        requires_user_action = requires_user_action or (
            self.config.requires_user_action and self.status != ServerStatus.CONNECTED
        )
        requires_user_action = requires_user_action or (
            self.config.source == "project"
            and self.config.approval_status == PROJECT_MCP_PENDING
        )
        return {
            "server_name": self.config.name,
            "status": self.status.value,
            "phase": phase,
            "message": self.last_error or _PHASE_DEFAULT_MESSAGE.get(phase, ""),
            "recoverable": recoverable,
            "requires_user_action": requires_user_action,
            "auth_status": self.auth_status.value,
            "setup_hint": self.config.setup_hint,
            "docs_url": self.config.docs_url,
            **(
                {
                    "reconnect_attempt": self.retry_count,
                    "max_reconnect_attempts": MAX_RECONNECT_ATTEMPTS,
                }
                if self.status == ServerStatus.RECONNECTING
                else {}
            ),
        }

    def to_progress_dict(self) -> dict[str, Any] | None:
        """Coarse progress for the connect/reconnect operation (mcp.progress).

        Returns None when no operation is in flight (stopped/offline). No 0-1
        fraction is reported because the transport exposes none yet.
        """
        if self.status == ServerStatus.STARTING:
            return {"server_name": self.config.name, "operation": "connect", "message": "Connecting…", "status": "running"}
        if self.status == ServerStatus.RECONNECTING:
            return {"server_name": self.config.name, "operation": "reconnect", "message": "Reconnecting…", "status": "running"}
        if self.status == ServerStatus.CONNECTED:
            return {"server_name": self.config.name, "operation": "connect", "message": "Connected", "status": "completed"}
        if self.status == ServerStatus.ERROR:
            return {"server_name": self.config.name, "operation": "connect", "message": self.last_error or "Connection failed", "status": "failed"}
        return None



class MCPServerManager:
    def __init__(
        self,
        config_path: Path | None = None,
        on_status_change: Callable[[str, ServerStatus], Awaitable[None]] | None = None,
        elicitation_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        managed_settings_dir: Path | None = None,
        requirements_path: Path | None = None,
        workspace_root: Path | None | object = _DYNAMIC_WORKSPACE,
    ) -> None:
        self._config_path = config_path or MCP_CONFIG_FILE
        self._servers: dict[str, MCPServerState] = {}
        self._on_status_change = on_status_change
        self._elicitation_handler = elicitation_handler
        self._managed_settings_dir = managed_settings_dir
        self._requirements_path = requirements_path
        self._workspace_root_bound = workspace_root is not _DYNAMIC_WORKSPACE
        self._workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not _DYNAMIC_WORKSPACE and workspace_root is not None
            else None
        )
        self._reconnect_tasks: dict[str, asyncio.Task[Any]] = {}
        self._tool_refresh_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cleanup_reaper_tasks: dict[str, asyncio.Task[Any]] = {}
        self._connection_locks: dict[str, asyncio.Lock] = {}
        self._reload_lock = asyncio.Lock()
        # OAuth tokens for HTTP MCP servers, persisted alongside the MCP config.
        from backend.mcp.oauth import TokenStore

        self._token_store = TokenStore(self._config_path.parent / "mcp_tokens.json")
        # Monotonic counter bumped on every server status change. Consumers
        # (tool registry schema cache) include it in their cache key so a
        # connect/disconnect/reconnect invalidates stale tool schemas.
        self._registry_version = 0
        # Subscription intent belongs to the manager/server owner, not to a
        # transport client that may be replaced during reconnect.
        self._resource_subscriptions: dict[str, set[str]] = {}

    @property
    def registry_version(self) -> int:
        return self._registry_version

    @property
    def workspace_root(self) -> Path | None:
        """Workspace whose project MCP/config layers this manager owns."""

        if self._workspace_root_bound:
            return self._workspace_root
        return get_explicit_active_workspace_root()

    def load_config(self) -> list[MCPServerConfig]:
        workspace_root = self.workspace_root
        from backend.config import load_config_layer_stack

        config_stack = load_config_layer_stack(
            cwd=workspace_root,
            requirements_path=self._requirements_path,
            managed_settings_dir=self._managed_settings_dir,
        )
        policy = load_mcp_policy(
            managed_settings_dir=self._managed_settings_dir,
            config_stack=config_stack,
        )
        enterprise_exists, enterprise_configs = self._load_enterprise_configs()
        if enterprise_exists:
            return self._apply_mcp_policies(
                enterprise_configs,
                policy=policy,
            )

        plugin_configs = self._load_plugin_configs(config_stack=config_stack)
        user_configs = self._load_local_configs()
        project_configs = self._load_project_configs()

        if policy.strict_plugin_only:
            user_configs = []
            project_configs = []

        manual: dict[str, MCPServerConfig] = {}
        # MiniCode's catalog has one precedence order: plugin declarations,
        # user configuration, then approved project configuration.
        for config in user_configs:
            manual[config.name] = config
        for config in project_configs:
            if config.approval_status == PROJECT_MCP_APPROVED:
                manual[config.name] = config

        # Pending/rejected project declarations remain visible for approval,
        # but cannot replace a connectable server from a higher active scope.
        inactive_project_configs: dict[str, MCPServerConfig] = {}
        for config in project_configs:
            if config.approval_status != PROJECT_MCP_APPROVED and config.name not in manual:
                inactive_project_configs[config.name] = config

        # Plugin keys are namespaced, so name collisions cannot suppress a
        # manually configured server. A manual declaration owns a matching
        # command/URL; between plugins, the first declaration owns it.
        manual_signatures = {
            signature
            for config in manual.values()
            if (signature := _mcp_server_signature(config))
        }
        plugin_signatures: set[str] = set()
        merged: dict[str, MCPServerConfig] = {}
        for config in plugin_configs:
            signature = _mcp_server_signature(config)
            if signature and (signature in manual_signatures or signature in plugin_signatures):
                logger.info("Suppressing duplicate plugin MCP server '%s'", config.name)
                continue
            if signature:
                plugin_signatures.add(signature)
            merged[config.name] = config
        merged.update(manual)
        for name, config in inactive_project_configs.items():
            merged.setdefault(name, config)

        return self._apply_mcp_policies(
            sorted(merged.values(), key=_mcp_catalog_sort_key),
            policy=policy,
        )

    def _load_enterprise_configs(self) -> tuple[bool, list[MCPServerConfig]]:
        config_path, servers = load_enterprise_mcp_payload(self._managed_settings_dir)
        if servers is None:
            return False, []
        configs: list[MCPServerConfig] = []
        for index, (name, raw_config) in enumerate(servers.items()):
            config = self._config_from_mapping(
                str(name),
                raw_config,
                source="enterprise",
                priority=index,
                base_dir=config_path.parent,
                config_path=config_path,
            )
            configs.append(config)
        return True, configs

    @staticmethod
    def _apply_mcp_policies(
        configs: list[MCPServerConfig],
        *,
        policy: MCPPolicy,
    ) -> list[MCPServerConfig]:
        allowed: list[MCPServerConfig] = []
        for config in configs:
            if not policy.allows(config):
                config.enabled = False
                config.disabled_reason = "Blocked by MiniCode MCP policy"
                allowed.append(config)
                continue
            disabled_reason = policy.disabled_reason(config)
            config.enabled = disabled_reason is None
            config.disabled_reason = disabled_reason or ""
            allowed.append(config)
        return allowed

    def _load_plugin_configs(self, *, config_stack: Any | None = None) -> list[MCPServerConfig]:
        try:
            from backend.plugins.manager import PluginManager
        except Exception as exc:
            raise RuntimeError("Plugin MCP discovery is unavailable") from exc

        configs: list[MCPServerConfig] = []
        try:
            enabled_plugins = PluginManager(config_stack=config_stack).snapshot().enabled_plugins
        except Exception as exc:
            raise RuntimeError("Unified plugin snapshot unavailable for MCP") from exc
        for plugin_order, plugin in enumerate(enabled_plugins):
            plugin_root = Path(str(plugin.get("path") or ""))
            plugin_name = str(plugin.get("name") or plugin_root.name)
            plugin_id = str(plugin.get("id") or plugin_name)
            manifest_paths = [Path(str(path)) for path in plugin.get("manifest_paths", [])]
            if not manifest_paths:
                manifest_paths = [plugin_manifest_path(plugin_root)]
            for manifest_path in manifest_paths:
                if not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise RuntimeError(
                        f"Failed to read plugin manifest {manifest_path}: {exc}"
                    ) from exc
                if not isinstance(manifest, Mapping):
                    raise ValueError(f"Plugin manifest {manifest_path} must contain an object")

                declaration = manifest.get("mcp_servers")
                server_maps: list[Mapping[str, Any]] = []
                if isinstance(declaration, Mapping):
                    server_maps.append(declaration)
                else:
                    config_path = plugin_root / ".mcp.json"
                    if isinstance(declaration, str) and declaration.strip():
                        candidate = _resolve_plugin_relative_path(plugin_root, declaration)
                        if candidate is None:
                            raise ValueError(
                                f"Plugin '{plugin_name}' declares an unsafe MCP path"
                            )
                        config_path = candidate
                    elif declaration is not None:
                        raise ValueError(
                            f"Plugin '{plugin_name}' field 'mcp_servers' must be an object or relative path"
                        )
                    if config_path.is_file():
                        try:
                            payload = json.loads(config_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise RuntimeError(
                                f"Failed to read plugin MCP config {config_path}: {exc}"
                            ) from exc
                        if not isinstance(payload, Mapping):
                            raise ValueError(f"Plugin MCP config {config_path} must contain an object")
                        unknown_root_fields = sorted(set(payload) - {"servers"})
                        if unknown_root_fields:
                            raise ValueError(
                                f"Plugin MCP config {config_path} contains unsupported fields: "
                                + ", ".join(str(field) for field in unknown_root_fields)
                            )
                        servers = payload.get("servers")
                        if not isinstance(servers, Mapping):
                            raise ValueError(
                                f"Plugin MCP config {config_path} must contain an object field named servers"
                            )
                        server_maps.append(servers)

                for servers in server_maps:
                    for server_index, (name, raw_config) in enumerate(servers.items()):
                        config = self._config_from_mapping(
                            f"plugin:{plugin_id}:{name}",
                            raw_config,
                            source=f"plugin:{plugin_id}",
                            priority=plugin_order * 1000 + server_index,
                            base_dir=plugin_root,
                        )
                        configs.append(config)
        return configs

    def _load_local_configs(self) -> list[MCPServerConfig]:
        if not self._config_path.exists():
            logger.info("No local MCP config found at %s", self._config_path)
            return []

        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Failed to read {self._config_path}: {exc}") from exc

        if not isinstance(data, Mapping):
            raise ValueError(f"{self._config_path} must contain a JSON object")
        unknown_root_fields = sorted(set(data) - {"servers"})
        if unknown_root_fields:
            raise ValueError(
                f"{self._config_path} contains unsupported fields: "
                + ", ".join(str(field) for field in unknown_root_fields)
            )
        servers_data = data.get("servers", {})
        if not isinstance(servers_data, Mapping):
            raise ValueError(
                f"{self._config_path} must contain an object field named servers"
            )
        configs: list[MCPServerConfig] = []
        for index, (name, conf) in enumerate(servers_data.items()):
            config = self._config_from_mapping(
                str(name), conf, source="user", priority=1000 + index,
                base_dir=self._config_path.parent,
                config_path=self._config_path,
            )
            configs.append(config)
        return configs

    def _load_project_configs(self) -> list[MCPServerConfig]:
        workspace_root = self.workspace_root
        config_paths = project_mcp_config_paths(workspace_root)
        if workspace_root is None or not config_paths:
            return []

        merged: dict[str, MCPServerConfig] = {}
        for config_order, config_path in enumerate(config_paths):
            if not config_path.is_file():
                continue
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Failed to read project MCP config {config_path}: {exc}"
                ) from exc
            if not isinstance(data, Mapping):
                raise ValueError(f"{config_path} must contain a JSON object")
            unknown_root_fields = sorted(set(data) - {"servers"})
            if unknown_root_fields:
                raise ValueError(
                    f"{config_path} contains unsupported fields: "
                    + ", ".join(str(field) for field in unknown_root_fields)
                )
            servers_data = data.get("servers", {})
            if not isinstance(servers_data, Mapping):
                raise ValueError(
                    f"{config_path} must contain an object field named servers"
                )
            for server_index, (name, raw_config) in enumerate(servers_data.items()):
                server_name = str(name)
                try:
                    approval_status = project_mcp_server_status(server_name, workspace_root)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Failed to read project MCP approvals: {exc}"
                    ) from exc
                config = self._config_from_mapping(
                    server_name,
                    raw_config,
                    source="project",
                    priority=config_order * 1000 + server_index,
                    base_dir=config_path.parent,
                    approval_status=approval_status,
                    config_path=config_path,
                    project_workspace=workspace_root,
                )
                # Files closer to the active workspace override parents.
                merged[config.name] = config
        return list(merged.values())

    def _config_from_mapping(
        self,
        name: str,
        raw_config: Any,
        *,
        source: str,
        priority: int,
        base_dir: Path,
        approval_status: str = "not_applicable",
        config_path: Path | None = None,
        project_workspace: Path | None = None,
    ) -> MCPServerConfig:
        if not isinstance(raw_config, Mapping):
            raise ValueError(f"MCP server '{name}' from {source} must be an object")
        conf = _resolve_mapping_placeholders(dict(raw_config))
        unknown_fields = sorted(set(conf) - _MCP_SERVER_FIELDS)
        if unknown_fields:
            raise ValueError(
                f"MCP server '{name}' from {source} contains unsupported fields: "
                + ", ".join(str(field) for field in unknown_fields)
            )
        if not name.strip() or "\x00" in name:
            raise ValueError(f"MCP server from {source} has an invalid empty/NUL name")
        raw_url = conf.get("url")
        if raw_url is not None and not isinstance(raw_url, str):
            raise ValueError(f"MCP server '{name}' from {source} URL must be a string")
        url = _optional_str(raw_url)
        try:
            transport = mcp_transport_from_mapping(conf)
        except ValueError as exc:
            raise ValueError(
                f"MCP server '{name}' from {source} has an invalid transport: {exc}"
            ) from exc
        incompatible_fields = (
            ("url", "headers", "headers_helper", "oauth")
            if transport == "stdio"
            else ("command", "args", "env", "env_vars", "cwd")
        )
        incompatible = [
            field_name
            for field_name in incompatible_fields
            if _has_nonempty_config_value(conf.get(field_name))
        ]
        if transport == "ws" and _has_nonempty_config_value(conf.get("oauth")):
            incompatible.append("oauth")
        if incompatible:
            raise ValueError(
                f"MCP server '{name}' from {source} has fields unsupported for "
                f"{transport}: {', '.join(incompatible)}"
            )
        # An HTTP-style MCP server cannot be connected without an endpoint.
        # This also makes an unresolved environment placeholder fail closed,
        # matching the config-file validator instead of registering a dead
        # server that can never connect.
        if transport in {"sse", "http", "ws"} and not url:
            raise ValueError(f"MCP server '{name}' from {source} has no URL")
        if url:
            scheme = urlparse(url).scheme.lower()
            allowed_schemes = {"ws", "wss"} if transport == "ws" else {"http", "https"}
            if transport in {"sse", "http", "ws"} and scheme not in allowed_schemes:
                raise ValueError(
                    f"MCP server '{name}' from {source} has invalid {transport} "
                    f"URL scheme {scheme or '(missing)'}"
                )
        raw_command = conf.get("command")
        if transport == "stdio" and not isinstance(raw_command, str):
            raise ValueError(
                f"MCP stdio server '{name}' from {source} requires a string command"
            )
        command = raw_command if isinstance(raw_command, str) else ""
        raw_args = conf.get("args", [])
        if not isinstance(raw_args, list) or any(not isinstance(arg, str) for arg in raw_args):
            raise ValueError(
                f"MCP server '{name}' from {source} args must be an array of strings"
            )
        args = list(raw_args)
        raw_env = conf.get("env", {})
        if not isinstance(raw_env, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_env.items()
        ):
            raise ValueError(
                f"MCP server '{name}' from {source} env must map strings to strings"
            )
        env = dict(raw_env)
        raw_headers = conf.get("headers", {})
        if not isinstance(raw_headers, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_headers.items()
        ):
            raise ValueError(
                f"MCP server '{name}' from {source} headers must map strings to strings"
            )
        headers = dict(raw_headers)
        raw_headers_helper = conf.get("headers_helper", "")
        if not isinstance(raw_headers_helper, str):
            raise ValueError(
                f"MCP server '{name}' from {source} headers_helper must be a string"
            )
        raw_oauth = conf.get("oauth", {})
        if raw_oauth is None:
            raw_oauth = {}
        if not isinstance(raw_oauth, Mapping):
            raise ValueError(
                f"MCP server '{name}' from {source} oauth must be an object"
            )
        unknown_oauth_fields = sorted(set(raw_oauth) - {"client_id", "callback_port"})
        if unknown_oauth_fields:
            raise ValueError(
                f"MCP server '{name}' from {source} oauth contains unsupported fields: "
                + ", ".join(str(field) for field in unknown_oauth_fields)
            )
        oauth_client_id = raw_oauth.get("client_id", "")
        if not isinstance(oauth_client_id, str):
            raise ValueError(
                f"MCP server '{name}' from {source} oauth.client_id must be a string"
            )
        oauth_callback_port = raw_oauth.get("callback_port")
        if oauth_callback_port is not None and (
            not isinstance(oauth_callback_port, int)
            or isinstance(oauth_callback_port, bool)
        ):
            raise ValueError(
                f"MCP server '{name}' from {source} oauth.callback_port must be an integer"
            )
        env_vars = conf.get("env_vars")
        if env_vars is not None:
            if not isinstance(env_vars, list):
                raise ValueError(
                    f"MCP server '{name}' from {source} env_vars must be an array"
                )
            for item in env_vars:
                if isinstance(item, str):
                    target, source_name = item.strip(), item.strip()
                elif isinstance(item, Mapping):
                    target_value = item.get("name")
                    source_value = item.get("source", target_value)
                    if not isinstance(target_value, str) or not isinstance(source_value, str):
                        raise ValueError(
                            f"MCP server '{name}' from {source} env_vars mappings require string name/source"
                        )
                    unknown_env_fields = sorted(set(item) - {"name", "source"})
                    if unknown_env_fields:
                        raise ValueError(
                            f"MCP server '{name}' from {source} env_vars contains unsupported fields: "
                            + ", ".join(str(field) for field in unknown_env_fields)
                        )
                    target = target_value.strip()
                    source_name = source_value.strip()
                else:
                    raise ValueError(
                        f"MCP server '{name}' from {source} env_vars entries must be strings or objects"
                    )
                if not target or not source_name:
                    raise ValueError(
                        f"MCP server '{name}' from {source} env_vars contains an empty name/source"
                    )
                if source_name not in os.environ:
                    raise ValueError(
                        f"MCP server '{name}' from {source} requires missing environment variable '{source_name}'"
                    )
                env[target] = os.environ[source_name]
        raw_cwd = conf.get("cwd")
        if raw_cwd is not None and not isinstance(raw_cwd, str):
            raise ValueError(f"MCP server '{name}' from {source} cwd must be a string")
        cwd = (
            _resolve_mcp_cwd(
                base_dir,
                raw_cwd,
                workspace_root=self.workspace_root,
            )
            if transport == "stdio"
            else None
        )
        raw_auto_start = conf.get("auto_start", True)
        if not isinstance(raw_auto_start, bool):
            raise ValueError(
                f"MCP server '{name}' from {source} auto_start must be a boolean"
            )
        try:
            startup_timeout_sec = _optional_positive_timeout(
                conf.get("startup_timeout_sec"),
                field="startup_timeout_sec",
            )
            tool_timeout_sec = _optional_positive_timeout(
                conf.get("tool_timeout_sec"),
                field="tool_timeout_sec",
            )
            enabled_tools = _optional_tool_name_list(
                conf.get("enabled_tools"),
                field="enabled_tools",
                preserve_none=True,
            )
            disabled_tools = _optional_tool_name_list(
                conf.get("disabled_tools"),
                field="disabled_tools",
                preserve_none=False,
            ) or []
            default_tools_approval_mode = _optional_approval_mode(
                conf.get("default_tools_approval_mode"),
                field="default_tools_approval_mode",
            )
            tool_approval_modes = _tool_approval_modes(conf.get("tools"))
        except ValueError as exc:
            raise ValueError(
                f"MCP server '{name}' from {source} has invalid policy/config: {exc}"
            ) from exc
        required = conf.get("required", False)
        supports_parallel_tool_calls = conf.get("supports_parallel_tool_calls", False)
        if not isinstance(required, bool) or not isinstance(supports_parallel_tool_calls, bool):
            raise ValueError(
                f"MCP server '{name}' from {source} lifecycle policy must be boolean"
            )
        requires_user_action = conf.get("requires_user_action", False)
        if not isinstance(requires_user_action, bool):
            raise ValueError(
                f"MCP server '{name}' from {source} requires_user_action must be a boolean"
            )
        setup_hint = conf.get("setup_hint", "")
        docs_url = conf.get("docs_url", "")
        if not isinstance(setup_hint, str) or not isinstance(docs_url, str):
            raise ValueError(
                f"MCP server '{name}' from {source} setup_hint/docs_url must be strings"
            )
        config = MCPServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            headers=headers,
            headers_helper=raw_headers_helper.strip(),
            oauth_client_id=oauth_client_id.strip(),
            oauth_callback_port=oauth_callback_port,
            cwd=cwd,
            transport=transport,
            url=url,
            auto_start=raw_auto_start,
            source=source,
            priority=priority,
            requires_user_action=requires_user_action,
            setup_hint=setup_hint,
            docs_url=docs_url,
            approval_status=approval_status,
            config_path=str(config_path) if config_path is not None else "",
            project_workspace=str(project_workspace) if project_workspace is not None else "",
            startup_timeout_sec=startup_timeout_sec,
            tool_timeout_sec=tool_timeout_sec,
            required=required,
            supports_parallel_tool_calls=supports_parallel_tool_calls,
            enabled_tools=enabled_tools,
            disabled_tools=disabled_tools,
            default_tools_approval_mode=default_tools_approval_mode,
            tool_approval_modes=tool_approval_modes,
        )
        try:
            validate_mcp_server_config(config)
        except ValueError as exc:
            raise ValueError(
                f"MCP server '{name}' from {source} is invalid: {exc}"
            ) from exc
        return config

    async def start_all(self) -> None:
        async with self._reload_lock:
            await self._start_all_unlocked()

    async def _start_all_unlocked(self) -> None:
        configs = self.load_config()
        local = [config for config in configs if config.transport == "stdio"]
        remote = [config for config in configs if config.transport != "stdio"]
        await asyncio.gather(
            self._start_config_batch(local, _mcp_connection_batch_size()),
            self._start_config_batch(remote, _mcp_remote_connection_batch_size()),
        )
        required_failures = [
            config.name
            for config in configs
            if config.required
            and config.auto_start
            and _config_can_connect(config)
            and (
                config.name not in self._servers
                or self._servers[config.name].status != ServerStatus.CONNECTED
            )
        ]
        if required_failures:
            raise RuntimeError(
                "Required MCP servers failed to initialize: "
                + ", ".join(required_failures)
            )

    async def _start_config_batch(
        self,
        configs: list[MCPServerConfig],
        batch_size: int,
    ) -> None:
        for start in range(0, len(configs), batch_size):
            batch = configs[start:start + batch_size]
            results = await asyncio.gather(
                *(self._start_or_register_config(config) for config in batch),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, BaseException)]
            if failures:
                raise RuntimeError(
                    "Failed to initialize MCP server batch: "
                    + "; ".join(f"{type(error).__name__}: {error}" for error in failures)
                )

    async def _start_or_register_config(self, config: MCPServerConfig) -> None:
        if config.auto_start and _config_can_connect(config):
            await self.start_server(config)
        else:
            await self.register_config(config)

    async def reload_config(self) -> None:
        """Reconcile running servers with the effective config catalog."""
        async with self._reload_lock:
            await self._reload_config_unlocked()

    async def _reload_config_unlocked(self) -> None:
        desired_configs = self.load_config()
        desired = {config.name: config for config in desired_configs}
        for name in list(self._servers):
            if name not in desired:
                await self.remove_server(name)
        for name, config in desired.items():
            current = self._servers.get(name)
            changed = current is None or not _same_runtime_config(current.config, config)
            if changed:
                if current is not None:
                    await self.stop_server(name)
                if config.auto_start and _config_can_connect(config):
                    await self.start_server(config)
                else:
                    await self.register_config(config)
            elif config.auto_start and _config_can_connect(config) and current.status == ServerStatus.OFFLINE:
                await self.start_server(config)
        # Reconcile changes runtime state as well as configuration. Rebuild the
        # mapping so status/tool discovery cannot retain a stale insertion order
        # after an override, rename, or priority change.
        self._servers = {
            config.name: self._servers[config.name]
            for config in desired_configs
            if config.name in self._servers
        }

    async def start_server(
        self,
        config: MCPServerConfig,
        *,
        interactive_oauth: bool = False,
        force: bool = False,
    ) -> None:
        if not _config_can_connect(config):
            await self.register_config(config)
            return
        # A server owns exactly one connect/OAuth attempt. Config reloads cannot
        # start another browser flow while one is already in progress.
        await self._cancel_reconnect_task(config.name)
        lock = self._connection_locks.get(config.name)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[config.name] = lock
        async with lock:
            state = await self._prepare_state(config)
            if state.status == ServerStatus.CONNECTED and not force:
                return
            phase, _, _ = classify_mcp_phase(
                state.status,
                state.last_error,
                state.last_exception,
            )
            if phase in {"auth_required", "expired"} and not interactive_oauth and not force:
                return
            state.status = ServerStatus.STARTING
            await self._notify_status(config.name, ServerStatus.STARTING)
            await self._attempt_connection(
                config.name,
                state,
                interactive_oauth=interactive_oauth,
            )

    async def register_config(self, config: MCPServerConfig) -> None:
        """Track an installed MCP server without starting it."""
        await self._cancel_reconnect_task(config.name)
        lock = self._connection_locks.get(config.name)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[config.name] = lock
        async with lock:
            state = await self._prepare_state(config)
            state.status = ServerStatus.OFFLINE
            state.tools = []
            state.last_error = ""
            state.last_exception = None
            await self._notify_status(config.name, ServerStatus.OFFLINE)

    async def stop_server(self, name: str) -> bool:
        state = self._servers.get(name)
        if state is None:
            return True

        await self._cancel_reconnect_task(name)
        await self._cancel_tool_refresh_task(name)
        lock = self._connection_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[name] = lock
        async with lock:
            # Fence the disconnect callback before closing the SDK lifecycle.
            # Otherwise an explicit Stop can be observed as an unexpected
            # remote disconnect and schedule a reconnect after the cancellation
            # above has already completed.
            client = state.client
            if client is not None:
                try:
                    closed = await client.close()
                except Exception as exc:
                    state.status = ServerStatus.ERROR
                    state.tools = []
                    state.last_exception = exc
                    state.last_error = f"MCP shutdown failed: {type(exc).__name__}: {exc}"
                    _record_operation_failure(state, "cleanup", exc, retryable=True)
                    await self._notify_status(name, ServerStatus.ERROR)
                    raise RuntimeError(state.last_error) from exc
                if not closed:
                    # A timed-out shutdown remains owned and visible instead of
                    # being declared gone.
                    state.status = ServerStatus.ERROR
                    state.tools = []
                    state.last_error = "MCP shutdown timed out; lifecycle is still owned"
                    state.operation_failures["cleanup"] = {
                        "operation": "cleanup",
                        "failure_kind": "cleanup_pending",
                        "message": state.last_error,
                        "retryable": True,
                    }
                    self._schedule_cleanup_reaper(name, state, client)
                    await self._notify_status(name, ServerStatus.ERROR)
                    return False
            state.client = None
            state.status = ServerStatus.OFFLINE
            state.tools = []
            state.retry_count = 0
            state.last_error = ""
            state.last_exception = None
            state.operation_failures.clear()
            await self._notify_status(name, ServerStatus.OFFLINE)
            return True

    def _schedule_cleanup_reaper(
        self,
        name: str,
        state: MCPServerState,
        client: MCPClient,
    ) -> None:
        active = self._cleanup_reaper_tasks.get(name)
        if active is not None and not active.done():
            return

        async def reap() -> None:
            try:
                closed = await client.finish_pending_cleanup()
                if not closed:
                    return
                lock = self._connection_locks.get(name)
                if lock is None:
                    return
                async with lock:
                    current = self._servers.get(name)
                    if current is not state or current.client is not client:
                        return
                    current.client = None
                    current.status = ServerStatus.OFFLINE
                    current.tools = []
                    current.retry_count = 0
                    current.last_error = ""
                    current.last_exception = None
                    current.operation_failures.pop("cleanup", None)
                    await self._notify_status(name, ServerStatus.OFFLINE)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                current = self._servers.get(name)
                if current is state and current.client is client:
                    current.status = ServerStatus.ERROR
                    current.last_exception = exc
                    current.last_error = f"MCP cleanup failed: {type(exc).__name__}: {exc}"
                    _record_operation_failure(current, "cleanup", exc, retryable=True)
                    await self._notify_status(name, ServerStatus.ERROR)

        task = asyncio.create_task(reap(), name=f"mcp-cleanup-reaper-{name}")
        self._cleanup_reaper_tasks[name] = task

        def settled(completed: asyncio.Task[Any]) -> None:
            if self._cleanup_reaper_tasks.get(name) is completed:
                self._cleanup_reaper_tasks.pop(name, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("MCP cleanup evidence update failed for %s", name)

        task.add_done_callback(settled)

    async def remove_server(self, name: str) -> None:
        if not await self.stop_server(name):
            raise RuntimeError(
                f"MCP server '{name}' is still shutting down and cannot be removed"
            )
        self._servers.pop(name, None)
        self._connection_locks.pop(name, None)
        await self._notify_status(name, ServerStatus.OFFLINE)

    async def stop_all(self) -> None:
        tasks = [self.stop_server(name) for name in list(self._servers.keys())]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [result for result in results if isinstance(result, BaseException)]
            incomplete = sum(result is False for result in results)
            if failures:
                raise RuntimeError(
                    "Failed to stop one or more MCP servers: "
                    + "; ".join(str(error) for error in failures)
                )
            if incomplete:
                raise RuntimeError(
                    f"{incomplete} MCP server lifecycle(s) are still shutting down"
                )

    async def restart_server(self, name: str) -> None:
        state = self._servers.get(name)
        if state is None:
            return
        if not await self.stop_server(name):
            raise RuntimeError(
                f"MCP server '{name}' is still shutting down and cannot restart"
            )
        await self.start_server(state.config, force=True)

    async def oauth_login(self, name: str) -> None:
        """Run the official MCP SDK OAuth flow after an explicit user action."""
        state = self._servers.get(name)
        if state is None:
            raise KeyError(f"MCP server '{name}' is not configured")
        state.retry_count = 0
        await self.start_server(
            state.config,
            interactive_oauth=True,
        )

    async def oauth_logout(self, name: str) -> None:
        state = self._servers.get(name)
        if state is None:
            raise KeyError(f"MCP server '{name}' is not configured")
        if not await self.stop_server(name):
            raise RuntimeError(
                f"MCP server '{name}' is still shutting down and cannot log out"
            )
        self._token_store.clear(name)
        state.auth_status = (
            MCPAuthStatus.NOT_LOGGED_IN
            if state.config.oauth_client_id
            else MCPAuthStatus.UNSUPPORTED
        )
        await self._notify_status(name, ServerStatus.OFFLINE)

    def get_all_tools(self) -> dict[str, list[MCPToolDef]]:
        result: dict[str, list[MCPToolDef]] = {}
        for name, state in self._servers.items():
            if state.status == ServerStatus.CONNECTED and state.tools:
                result[name] = state.tools
        return result

    def get_client(self, server_name: str) -> MCPClient | None:
        state = self._servers.get(server_name)
        if state and state.status == ServerStatus.CONNECTED:
            return state.client
        return None

    async def subscribe_resource(self, server_name: str, uri: str) -> bool:
        client = self.get_client(server_name)
        if client is None:
            return False
        ok = await client.subscribe_resource(uri)
        if ok:
            self._resource_subscriptions.setdefault(server_name, set()).add(str(uri))
        return ok

    async def unsubscribe_resource(self, server_name: str, uri: str) -> bool:
        client = self.get_client(server_name)
        if client is None:
            return False
        ok = await client.unsubscribe_resource(uri)
        if ok:
            self._resource_subscriptions.setdefault(server_name, set()).discard(str(uri))
        return ok

    def get_server_config(self, server_name: str) -> MCPServerConfig | None:
        state = self._servers.get(server_name)
        return state.config if state is not None else None

    def iter_connected_clients(self) -> list[tuple[str, Any]]:
        """Return connected MCP clients using the manager's authoritative state."""
        result: list[tuple[str, Any]] = []
        for name, state in self._servers.items():
            client = state.client
            if state.status == ServerStatus.CONNECTED and client and client.connected:
                result.append((name, client))
        return result

    def get_server_instructions(self) -> dict[str, str]:
        """Return {server_name: instructions} for connected servers that declared any."""
        result: dict[str, str] = {}
        for name, state in self._servers.items():
            if state.status != ServerStatus.CONNECTED or not state.client:
                continue
            text = (state.client.instructions or "").strip()
            if text:
                result[name] = text
        return result

    def get_all_status(self) -> list[dict[str, Any]]:
        return [state.to_status_dict() for state in self._servers.values()]

    def get_server_lifecycle(self, name: str) -> dict[str, Any] | None:
        """Lifecycle payload for one server, or None if unknown."""
        state = self._servers.get(name)
        return state.to_lifecycle_dict() if state else None

    def get_server_progress(self, name: str) -> dict[str, Any] | None:
        """Connect/reconnect progress payload for one server, or None."""
        state = self._servers.get(name)
        return state.to_progress_dict() if state else None


    @property
    def connected_count(self) -> int:
        return sum(
            1
            for state in self._servers.values()
            if state.status == ServerStatus.CONNECTED
        )

    async def _try_automatic_remote_reconnect(self, name: str) -> None:
        state = self._servers.get(name)
        if state is None or state.config.transport not in _REMOTE_RECONNECT_TRANSPORTS:
            return

        lock = self._connection_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[name] = lock
        async with lock:
            for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
                state = self._servers.get(name)
                if state is None or state.config.transport not in _REMOTE_RECONNECT_TRANSPORTS:
                    return

                state.retry_count = attempt
                state.status = ServerStatus.RECONNECTING
                state.tools = []
                await self._notify_status(name, ServerStatus.RECONNECTING)

                stale_client = state.client
                state.client = None
                if stale_client is not None:
                    try:
                        closed = await stale_client.close()
                    except Exception as exc:
                        state.client = stale_client
                        state.status = ServerStatus.ERROR
                        state.last_exception = exc
                        state.last_error = (
                            f"MCP reconnect cleanup failed: {type(exc).__name__}: {exc}"
                        )
                        _record_operation_failure(state, "cleanup", exc, retryable=True)
                        await self._notify_status(name, ServerStatus.ERROR)
                        return
                    if not closed:
                        state.client = stale_client
                        state.status = ServerStatus.ERROR
                        state.last_exception = None
                        state.last_error = "MCP reconnect blocked by pending cleanup"
                        state.operation_failures["cleanup"] = {
                            "operation": "cleanup",
                            "failure_kind": "cleanup_pending",
                            "message": state.last_error,
                            "retryable": True,
                        }
                        await self._notify_status(name, ServerStatus.ERROR)
                        return

                await self._attempt_connection(name, state)
                if state.status == ServerStatus.CONNECTED:
                    return
                if attempt >= MAX_RECONNECT_ATTEMPTS:
                    logger.warning(
                        "MCP server '%s' did not reconnect after %d attempts",
                        name,
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    return

                backoff = min(
                    INITIAL_RECONNECT_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                    MAX_RECONNECT_BACKOFF_SECONDS,
                )
                await asyncio.sleep(backoff)

    async def _attempt_connection(
        self,
        name: str,
        state: MCPServerState,
        *,
        interactive_oauth: bool = False,
    ) -> None:
        client = state.client or self._create_client(
            state.config,
            interactive_oauth=interactive_oauth,
        )
        state.client = client

        try:
            connect_timeout = (
                310.0
                if interactive_oauth
                else _mcp_connection_timeout_seconds(state.config) + 1.0
            )
            await asyncio.wait_for(client.connect(), timeout=connect_timeout)
            # Replay durable subscription intent after every new transport
            # session.  The old client-owned set cannot survive reconnect.
            for uri in sorted(self._resource_subscriptions.get(name, set())):
                await client.subscribe_resource(uri)
            state.tools = _filter_mcp_tools(state.config, await client.list_tools())
            stored_auth_status = self._stored_auth_status(state.config)
            state.auth_status = (
                MCPAuthStatus.OAUTH
                if bool(getattr(client, "has_valid_token", False))
                or stored_auth_status == MCPAuthStatus.OAUTH
                else stored_auth_status
            )
            state.status = ServerStatus.CONNECTED
            state.retry_count = 0
            state.last_error = ""
            state.last_exception = None
            state.operation_failures.pop("connect", None)
            state.operation_failures.pop("resource_restore", None)
            state.operation_failures.pop("tool_catalog", None)
            await self._notify_status(name, ServerStatus.CONNECTED)
        except Exception as exc:
            cleanup_error: BaseException | None = None
            cleanup_pending = False
            try:
                cleanup_pending = not await client.close()
            except Exception as close_exc:
                cleanup_error = close_exc
            state.client = client if cleanup_pending or cleanup_error is not None else None
            state.status = ServerStatus.ERROR
            state.last_exception = exc
            state.tools = []
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            _record_operation_failure(state, "connect", exc, retryable=True)
            if cleanup_pending:
                state.last_error += "; cleanup is still pending"
                state.operation_failures["cleanup"] = {
                    "operation": "cleanup",
                    "failure_kind": "cleanup_pending",
                    "message": "MCP connection cleanup is still pending",
                    "retryable": True,
                }
                self._schedule_cleanup_reaper(name, state, client)
            elif cleanup_error is not None:
                state.last_error += (
                    f"; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
                )
                _record_operation_failure(
                    state,
                    "cleanup",
                    cleanup_error,
                    retryable=True,
                )
            phase, _, _ = classify_mcp_phase(state.status, state.last_error, exc)
            if phase in {"auth_required", "expired"}:
                state.auth_status = MCPAuthStatus.NOT_LOGGED_IN
            else:
                state.auth_status = self._stored_auth_status(state.config)
            logger.error("Failed to start MCP server '%s': %s", name, state.last_error)
            await self._notify_status(name, ServerStatus.ERROR)

    async def _prepare_state(self, config: MCPServerConfig) -> MCPServerState:
        validate_mcp_server_config(config)
        state = self._servers.get(config.name)
        if state is None:
            state = MCPServerState(config=config)
            self._servers[config.name] = state
            state.auth_status = self._stored_auth_status(config)
            return state

        config_changed = not _same_runtime_config(state.config, config)
        if config_changed and state.client is not None:
            stale_client = state.client
            try:
                closed = await stale_client.close()
            except Exception as exc:
                _record_operation_failure(state, "cleanup", exc, retryable=True)
                raise RuntimeError(
                    f"Cannot replace MCP server '{config.name}' while cleanup failed: {exc}"
                ) from exc
            if not closed:
                state.operation_failures["cleanup"] = {
                    "operation": "cleanup",
                    "failure_kind": "cleanup_pending",
                    "message": "MCP config replacement is waiting for cleanup",
                    "retryable": True,
                }
                self._schedule_cleanup_reaper(config.name, state, stale_client)
                raise RuntimeError(
                    f"Cannot replace MCP server '{config.name}' while cleanup is pending"
                )
            state.client = None
            state.status = ServerStatus.OFFLINE
            state.tools = []
            state.operation_failures.pop("cleanup", None)
        state.config = config
        stored_auth_status = self._stored_auth_status(config)
        if (
            config_changed
            or stored_auth_status != MCPAuthStatus.UNSUPPORTED
            or state.auth_status == MCPAuthStatus.UNSUPPORTED
        ):
            state.auth_status = stored_auth_status
        return state

    def _stored_auth_status(self, config: MCPServerConfig) -> MCPAuthStatus:
        if config.transport not in {"sse", "http"}:
            return MCPAuthStatus.UNSUPPORTED
        if self._token_store.has_sdk_tokens(config.name):
            return MCPAuthStatus.OAUTH
        tokens = self._token_store.get(config.name)
        if tokens is None:
            return (
                MCPAuthStatus.NOT_LOGGED_IN
                if config.oauth_client_id
                else MCPAuthStatus.UNSUPPORTED
            )
        return MCPAuthStatus.NOT_LOGGED_IN if tokens.is_expired() else MCPAuthStatus.OAUTH

    def _create_client(
        self,
        config: MCPServerConfig,
        *,
        interactive_oauth: bool = False,
    ) -> MCPClient:
        t = config.transport.lower()
        if t == "sse":
            transport = MCPTransport.SSE
        elif t == "ws":
            transport = MCPTransport.WEBSOCKET
        elif t == "http":
            transport = MCPTransport.HTTP
        elif t == "stdio":
            transport = MCPTransport.STDIO
        else:
            raise ValueError(f"Unsupported MCP transport: {config.transport}")
        client: MCPClient

        async def on_disconnect(_: str) -> None:
            await self._handle_client_disconnect(config.name, client)

        async def on_tools_changed(_: str) -> None:
            self._schedule_tool_refresh(config.name, client)

        client = MCPClient(
            server_name=config.name,
            command=config.command or "python",
            args=config.args,
            env=config.env,
            headers=config.headers,
            headers_helper=config.headers_helper,
            oauth_client_id=config.oauth_client_id,
            oauth_callback_port=config.oauth_callback_port,
            cwd=config.cwd,
            transport=transport,
            url=config.url,
            startup_timeout=(
                300.0
                if interactive_oauth
                else _mcp_connection_timeout_seconds(config)
            ),
            request_timeout=MCP_REQUEST_TIMEOUT_SECONDS,
            tool_timeout=_mcp_tool_timeout_seconds(config),
            token_store=self._token_store if transport in (MCPTransport.SSE, MCPTransport.HTTP) else None,
            interactive_oauth=interactive_oauth,
            elicitation_handler=self._elicitation_handler,
            on_disconnect=on_disconnect,
            on_tools_changed=on_tools_changed,
            workspace_root=self.workspace_root,
        )
        return client

    def _schedule_tool_refresh(self, name: str, client: MCPClient) -> None:
        active = self._tool_refresh_tasks.get(name)
        if active is not None and not active.done():
            return
        task = asyncio.create_task(
            self._refresh_server_tools(name, client),
            name=f"mcp-tools-refresh-{name}",
        )
        self._tool_refresh_tasks[name] = task

    async def _refresh_server_tools(self, name: str, client: MCPClient) -> None:
        current_task = asyncio.current_task()
        try:
            tools = await client.list_tools()
            state = self._servers.get(name)
            if (
                state is None
                or state.client is not client
                or state.status != ServerStatus.CONNECTED
                or not client.connected
            ):
                return
            state.tools = _filter_mcp_tools(state.config, tools)
            await self._notify_status(name, ServerStatus.CONNECTED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = self._servers.get(name)
            if state is not None and state.client is client:
                state.status = ServerStatus.ERROR
                state.tools = []
                state.last_exception = exc
                state.last_error = f"MCP tool catalog refresh failed: {type(exc).__name__}: {exc}"
                _record_operation_failure(state, "tool_catalog", exc, retryable=True)
                await self._notify_status(name, ServerStatus.ERROR)
        finally:
            if self._tool_refresh_tasks.get(name) is current_task:
                self._tool_refresh_tasks.pop(name, None)

    async def _cancel_tool_refresh_task(self, name: str) -> None:
        task = self._tool_refresh_tasks.pop(name, None)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _handle_client_disconnect(
        self,
        name: str,
        disconnected_client: MCPClient | None = None,
    ) -> None:
        state = self._servers.get(name)
        if state is None or state.status != ServerStatus.CONNECTED:
            return
        if disconnected_client is not None and state.client is not disconnected_client:
            # A previous SDK lifecycle may finish after an explicit restart.
            # Its callback must never detach or reconnect the newer client.
            return
        state.tools = []
        # The transport lifecycle that invoked this callback is already ending.
        # Detach it before reconnect so a fresh client/session is constructed.
        state.client = None

        # Only remote transports reconnect automatically. A closed stdio process
        # remains failed for explicit restart because respawning a local command
        # can repeat side effects.
        if state.config.transport not in _REMOTE_RECONNECT_TRANSPORTS:
            state.status = ServerStatus.ERROR
            state.last_error = f"MCP {state.config.transport} transport closed"
            state.last_exception = None
            await self._notify_status(name, ServerStatus.ERROR)
            return

        active = self._reconnect_tasks.get(name)
        if active is not None and not active.done():
            return
        task = asyncio.create_task(
            self._run_scheduled_reconnect(name),
            name=f"mcp-reconnect-{name}",
        )
        self._reconnect_tasks[name] = task

    async def _run_scheduled_reconnect(self, name: str) -> None:
        try:
            await self._try_automatic_remote_reconnect(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = self._servers.get(name)
            if state is not None:
                state.status = ServerStatus.ERROR
                state.tools = []
                state.last_exception = exc
                state.last_error = f"MCP reconnect failed: {type(exc).__name__}: {exc}"
                _record_operation_failure(state, "reconnect", exc, retryable=True)
                await self._notify_status(name, ServerStatus.ERROR)
        finally:
            current = self._reconnect_tasks.get(name)
            if current is asyncio.current_task():
                self._reconnect_tasks.pop(name, None)

    async def _cancel_reconnect_task(self, name: str) -> None:
        task = self._reconnect_tasks.pop(name, None)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _notify_status(self, name: str, status: ServerStatus) -> None:
        self._registry_version += 1
        if self._on_status_change:
            await self._on_status_change(name, status)


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _mcp_connection_timeout_seconds(config: MCPServerConfig | None = None) -> float:
    return (
        config.startup_timeout_sec
        if config is not None and config.startup_timeout_sec is not None
        else MCP_DEFAULT_STARTUP_TIMEOUT_SECONDS
    )


def _mcp_tool_timeout_seconds(config: MCPServerConfig | None = None) -> float:
    return (
        config.tool_timeout_sec
        if config is not None and config.tool_timeout_sec is not None
        else MCP_DEFAULT_TOOL_TIMEOUT_SECONDS
    )


def _mcp_connection_batch_size() -> int:
    return _positive_env_int("MCP_SERVER_CONNECTION_BATCH_SIZE", 3)


def _mcp_remote_connection_batch_size() -> int:
    return _positive_env_int("MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE", 20)


def _config_can_connect(config: MCPServerConfig) -> bool:
    return config.enabled and (
        config.source != "project" or config.approval_status == PROJECT_MCP_APPROVED
    )


def _resolve_mapping_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_mapping_placeholders(item_value)
            for key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_resolve_mapping_placeholders(item) for item in value]
    return _resolve_env_placeholders(value)


def _resolve_env_placeholders(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    # Supports ${VAR} and ${VAR:-default}; variable names follow the POSIX
    # env-var shape (letters, digits, underscore; must start with a letter or
    # underscore) so lowercase variables resolve too. Missing variables without
    # an explicit default are configuration errors.
    pattern = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)(?::-([^}]*))?\}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if default is not None:
            return os.getenv(name, default)
        if name not in os.environ:
            raise ValueError(f"MCP configuration references missing environment variable '{name}'")
        return os.environ[name]

    return pattern.sub(replace, value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_positive_timeout(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{field} must be a positive number")
    return float(value)


def _optional_tool_name_list(
    value: Any,
    *,
    field: str,
    preserve_none: bool,
) -> list[str] | None:
    if value is None:
        return None if preserve_none else []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be an array of non-empty strings")
    return list(dict.fromkeys(item.strip() for item in value))


def _optional_approval_mode(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"auto", "prompt", "writes", "approve"}:
        raise ValueError(f"{field} must be auto, prompt, writes, or approve")
    return value


def _tool_approval_modes(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("tools must be an object")
    result: dict[str, str] = {}
    for tool_name, raw_config in value.items():
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tools contains an invalid tool name")
        if not isinstance(raw_config, Mapping):
            raise ValueError(f"tools.{tool_name} must be an object")
        unknown_fields = sorted(set(raw_config) - {"approval_mode"})
        if unknown_fields:
            raise ValueError(
                f"tools.{tool_name} contains unsupported fields: "
                + ", ".join(str(field) for field in unknown_fields)
            )
        mode = _optional_approval_mode(
            raw_config.get("approval_mode"),
            field=f"tools.{tool_name}.approval_mode",
        )
        if mode is not None:
            result[tool_name] = mode
    return result


def _has_nonempty_config_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return bool(value)
    return True


def _mcp_catalog_sort_key(config: MCPServerConfig) -> tuple[int, int, str, str]:
    source = config.source
    if source.startswith("plugin:"):
        rank = 0
    elif source == "project" and config.approval_status == PROJECT_MCP_APPROVED:
        rank = 1
    elif source in {"user", "local"}:
        rank = 2
    elif source == "enterprise":
        rank = 0
    else:
        rank = 4
    return (rank, config.priority, config.name.casefold(), config.name)


def _mcp_server_signature(config: MCPServerConfig) -> str | None:
    """Return MiniCode's identity for duplicate MCP process declarations."""
    if config.transport == "stdio":
        return "stdio:" + json.dumps(
            [config.command, *config.args],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    url = str(config.url or "").strip()
    return f"url:{url}" if url else None


def _record_operation_failure(
    state: MCPServerState,
    operation: str,
    error: BaseException,
    *,
    retryable: bool,
) -> None:
    state.operation_failures[operation] = {
        "operation": operation,
        "failure_kind": type(error).__name__,
        "message": str(error) or type(error).__name__,
        "retryable": retryable,
    }


def _filter_mcp_tools(
    config: MCPServerConfig,
    tools: list[MCPToolDef],
) -> list[MCPToolDef]:
    enabled = set(config.enabled_tools) if config.enabled_tools is not None else None
    disabled = set(config.disabled_tools)
    return [
        tool
        for tool in tools
        if (enabled is None or tool.name in enabled) and tool.name not in disabled
    ]


def _same_runtime_config(left: MCPServerConfig, right: MCPServerConfig) -> bool:
    return (
        left.command == right.command
        and left.args == right.args
        and left.env == right.env
        and left.headers == right.headers
        and left.headers_helper == right.headers_helper
        and left.oauth_client_id == right.oauth_client_id
        and left.oauth_callback_port == right.oauth_callback_port
        and left.cwd == right.cwd
        and left.transport == right.transport
        and left.url == right.url
        and left.auto_start == right.auto_start
        and left.source == right.source
        and left.requires_user_action == right.requires_user_action
        and left.setup_hint == right.setup_hint
        and left.docs_url == right.docs_url
        and left.approval_status == right.approval_status
        and left.config_path == right.config_path
        and left.project_workspace == right.project_workspace
        and left.startup_timeout_sec == right.startup_timeout_sec
        and left.tool_timeout_sec == right.tool_timeout_sec
        and left.required == right.required
        and left.supports_parallel_tool_calls == right.supports_parallel_tool_calls
        and left.enabled_tools == right.enabled_tools
        and left.disabled_tools == right.disabled_tools
        and left.default_tools_approval_mode == right.default_tools_approval_mode
        and left.tool_approval_modes == right.tool_approval_modes
        and left.enabled == right.enabled
        and left.disabled_reason == right.disabled_reason
    )


def _resolve_plugin_relative_path(plugin_root: Path, value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = (plugin_root / raw).resolve()
    try:
        candidate.relative_to(plugin_root.resolve())
    except ValueError:
        return None
    return candidate


def _resolve_mcp_cwd(
    base_dir: Path,
    value: Any,
    *,
    workspace_root: Path | None = None,
) -> str | None:
    if value is None or not str(value).strip():
        return str((workspace_root or PROJECT_ROOT).resolve())
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())
