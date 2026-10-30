from __future__ import annotations

from backend.async_cleanup import _consume_task_result, to_thread_cancel_safe

import asyncio
import base64
import json
import logging
import math
import os
import re
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Any, Awaitable, Callable

from backend.config import DATA_ROOT, PROJECT_ROOT
from backend.mcp.client import MCPClient, MCPToolDef, MCPTransport
from backend.mcp.oauth import MCPTokenStoreError
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
from backend.mcp.transport import mcp_transport_from_mapping, normalize_mcp_remote_url, normalize_mcp_transport
from backend.mcp.value_utils import has_nonempty_value as _has_nonempty_config_value
from backend.mcp.value_utils import resolve_env_placeholders as _resolve_env_placeholders
from backend.plugins.layout import plugin_manifest_path
from backend.feature_flags import FeatureFlags, feature_enabled
from backend.mcp.connections import TOOL_CATALOG_CACHE
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
MCP_DEFAULT_TOOL_TIMEOUT_SECONDS = 300.0
_MCP_SERVER_FIELDS = frozenset({
    "transport", "command", "args", "env", "env_vars", "cwd", "url",
    "headers", "headers_helper", "oauth", "auto_start", "startup_timeout_sec",
    "tool_timeout_sec", "required", "startup_readiness", "supports_parallel_tool_calls",
    "enabled_tools", "disabled_tools", "default_tools_approval_mode", "tools",
    "requires_user_action", "setup_hint", "docs_url",
    "environment_id", "enabled", "bearer_token_env_var", "env_http_headers", "http_headers", "http_headers_helper",
})
_PLUGIN_MCP_DISPLAY_FIELDS = frozenset({"title", "description", "icons", "note"})


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
    startup_readiness: str = "connection"
    supports_parallel_tool_calls: bool = False
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = field(default_factory=list)
    default_tools_approval_mode: str | None = None
    tool_approval_modes: dict[str, str] = field(default_factory=dict)
    tool_output_token_limits: dict[str, int] = field(default_factory=dict)
    enabled: bool = True
    disabled_reason: str = ""
    environment_id: str = "local"
    env_vars: tuple[tuple[str, str], ...] = ()
    credential_policy: str = "HostFallbackAllowed"
    selected_root_id: str | None = None
    plugin_id: str | None = None
    plugin_server_name: str | None = None
    bearer_token_env_var: str | None = None
    env_http_headers: dict[str, str] = field(default_factory=dict)

    def oauth_credential_name(self) -> str:
        if self.environment_id == "local":
            return "local:" + self.name if self.name.startswith(("executor:", "local:", "ema-idp:")) else self.name
        encode = lambda value: base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
        return f"executor:{encode(self.environment_id)}:{encode(self.name)}"


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
    if not isinstance(config.environment_id, str):
        raise ValueError("environment_id must be a string")
    if not isinstance(config.enabled, bool):
        raise ValueError("enabled must be a boolean")
    if config.credential_policy not in {"HostFallbackAllowed", "ExecutorOnly"}:
        raise ValueError("invalid MCP credential policy")
    if config.bearer_token_env_var is not None and not isinstance(config.bearer_token_env_var, str):
        raise ValueError("bearer_token_env_var must be a string")
    for name, source in config.env_vars:
        if not isinstance(name, str) or not name.strip() or source not in {"local", "remote"}:
            raise ValueError("env_vars requires names with local or remote sources")
        if source == "remote" and config.environment_id == "local":
            raise ValueError("remote env_vars requires a remote environment")
    if not isinstance(config.env_http_headers, dict) or any(not isinstance(key, str) or not isinstance(value, str)
        for key, value in config.env_http_headers.items()):
        raise ValueError("env_http_headers must map strings to strings")
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
    if config.startup_readiness not in ("connection", "catalog"):
        raise ValueError("startup_readiness must be connection or catalog")
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
    if any(type(limit) is not int or limit <= 0 for limit in config.tool_output_token_limits.values()):
        raise ValueError("tool output_token_limit must be a positive integer")
    if transport == "stdio":
        incompatible = [
            field_name
            for field_name, value in (
                ("url", config.url),
                ("headers", config.headers),
                ("headers_helper", config.headers_helper),
                ("oauth", config.oauth_client_id or config.oauth_callback_port),
                ("bearer_token_env_var", config.bearer_token_env_var),
                ("env_http_headers", config.env_http_headers),
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
            ("env_vars", config.env_vars),
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
    config.url = normalize_mcp_remote_url(config.url, transport)


@dataclass
class MCPServerState:
    config: MCPServerConfig
    client: MCPClient | None = None
    status: ServerStatus = ServerStatus.OFFLINE
    tools: list[MCPToolDef] = field(default_factory=list)
    raw_tools: list[MCPToolDef] = field(default_factory=list)
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
            "environment_id": self.config.environment_id,
            "status": self.status.value,
            "tools_count": len(self.tools),
            # MCP capability flags are negotiated during initialize. Expose
            # the negotiated contract to the connector UI without issuing
            # extra list/read requests from the status path.
            "capabilities": capability_payload,
            "cleanup": cleanup,
            "error": self.last_error if self.status == ServerStatus.ERROR else "",
            "source": self.config.source,
            "plugin_id": self.config.plugin_id,
            "plugin_server_name": self.config.plugin_server_name,
            "approval_status": self.config.approval_status,
            "config_path": self.config.config_path,
            "project_workspace": self.config.project_workspace,
            "priority": self.config.priority,
            "transport": self.config.transport,
            "required": self.config.required,
            "startup_readiness": self.config.startup_readiness,
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



def _has_tool_catalog_error(state: MCPServerState) -> bool:
    return (
        state.status == ServerStatus.ERROR
        and "tool_catalog" in state.operation_failures
        and not state.operation_failures.keys() & {"connect", "reconnect", "cleanup"}
    )


@dataclass
class _OAuthLoginAttempt:
    state: MCPServerState
    config: MCPServerConfig
    scope: Any
    task: asyncio.Task[None]


class MCPServerManager:
    def __init__(
        self,
        config_path: Path | None = None,
        on_status_change: Callable[[str, ServerStatus], Awaitable[None]] | None = None,
        elicitation_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        managed_settings_dir: Path | None = None,
        requirements_path: Path | None = None,
        workspace_root: Path | None | object = _DYNAMIC_WORKSPACE,
        environment_runtime=None,
        runtime_configs: tuple[MCPServerConfig, ...] | None = None,
        startup_policy: str = "Eager",
        optional_startup_grace_ms: int = 1000,
        feature_flags: FeatureFlags | None = None,
        requires_read_only_tools: bool = False,
    ) -> None:
        self._config_path = config_path or MCP_CONFIG_FILE
        self.environment_runtime = environment_runtime
        self._runtime_configs = runtime_configs
        self._startup_policy = startup_policy
        if type(optional_startup_grace_ms) is not int or not 0 <= optional_startup_grace_ms <= (1 << 64) - 1:
            raise ValueError("optional_startup_grace_ms must be a non-negative integer")
        self._optional_startup_grace_ms = optional_startup_grace_ms
        self._feature_flags = feature_flags if feature_flags is not None else FeatureFlags(flags={
            name: feature_enabled(name) for name in ("mcp_roots", "mcp_elicitation",
                "mcp_streamable_http_transport", "mcp_websocket_transport")})
        self._requires_read_only_tools = requires_read_only_tools
        self._catalog_keys: dict[str, Any] = {}
        self._catalog_contexts: dict[str, Any] = {}
        self._catalog_snapshots: dict[str, Any] = {}
        self._lazy_servers: set[str] = set()
        self._dormant_servers: set[str] = set()
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
        self._pending_tool_refreshes: dict[str, MCPClient] = {}
        self._cleanup_reaper_tasks: dict[str, asyncio.Task[Any]] = {}
        self._connection_locks: dict[str, asyncio.Lock] = {}
        self._oauth_operation_locks: dict[str, asyncio.Lock] = {}
        self._oauth_login_attempts: dict[str, _OAuthLoginAttempt] = {}
        self._node_companion_views: dict[str, Any] = {}
        self._shared_connection_views: dict[str, Any] = {}
        self._startup_tasks: dict[str, asyncio.Task] = {}
        self._local_startup_slots = asyncio.Semaphore(_mcp_connection_batch_size())
        self._remote_startup_slots = asyncio.Semaphore(_mcp_remote_connection_batch_size())
        self._optional_startup_deadline: tuple[int, float] | None = None
        self._startup_admitted_at: dict[str, float] = {}
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

    def _client_capability_identity(self, config: MCPServerConfig):
        from backend.mcp.node_repl import SOURCE_PREFIX
        elicitation = self._elicitation_handler is not None and not config.source.startswith(SOURCE_PREFIX)
        return (MCPClient.PROTOCOL_VERSION, self._feature_flags.enabled("mcp_roots"),
            self._feature_flags.enabled("mcp_elicitation") and elicitation, ())

    def _credential_identity(self, config: MCPServerConfig):
        if config.transport not in {"http", "sse"} or config.credential_policy == "ExecutorOnly":
            return None, False
        return self._token_store.for_server(config.url or "", config.oauth_client_id).startup_identity(config.oauth_credential_name())

    def _catalog_identity(self, config: MCPServerConfig):
        from backend.mcp.catalog_identity import catalog_identity
        _credentials, has_credentials = self._credential_identity(config)
        return catalog_identity(config, self.environment_runtime, self._client_capability_identity(config),
            has_oauth_credentials=has_credentials, requires_read_only_tools=self._requires_read_only_tools)

    def _borrow_connection(self, config: MCPServerConfig, *, interactive_oauth: bool):
        from backend.mcp.catalog_identity import connection_identity
        runtime = self.environment_runtime
        credential_key, _has_credentials = self._credential_identity(config)
        key = connection_identity(config, runtime, self._client_capability_identity(config), credential_key,
            requires_read_only_tools=self._requires_read_only_tools)
        catalog_key = self._catalog_identity(config)
        captured = deepcopy(config)
        def capture():
            connection_runtime = runtime.for_connection()

            async def factory(connection):
                binding = await connection_runtime.bind_server(captured)
                return self._create_client(captured, interactive_oauth=interactive_oauth,
                    execution_binding=binding, connection_events=connection)

            async def close(client):
                if client is not None and not await client.close():
                    if not await client.finish_pending_cleanup():
                        raise ConnectionError("The captured MCP connection still owns pending cleanup")
                await connection_runtime.aclose()
            return factory, close

        async def on_disconnect(name):
            view = self._shared_connection_views.get(name)
            if view is not None:
                await self._handle_client_disconnect(name, view.client)

        async def on_tools_changed(name):
            view = self._shared_connection_views.get(name)
            if view is not None:
                self._schedule_tool_refresh(name, view.client)

        return runtime.connection_pool.acquire(name=config.name, identity=key, catalog_identity=catalog_key,
            capture=capture, on_disconnect=on_disconnect, on_tools_changed=on_tools_changed)

    @property
    def workspace_root(self) -> Path | None:
        """Workspace whose project MCP/config layers this manager owns."""

        if self._workspace_root_bound:
            return self._workspace_root
        return get_explicit_active_workspace_root()

    def load_config(self, *, config_stack: Any | None = None) -> list[MCPServerConfig]:
        if self._runtime_configs is not None:
            if self.environment_runtime is None:
                # A host configuration catalog has no receiving attachment.
                # Static CUA configs are registered offline by start_server;
                # their actual builtin admission still requires the runtime.
                return list(self._runtime_configs)
            return [self.environment_runtime.apply_authority(config) for config in self._runtime_configs]
        workspace_root = self.workspace_root
        from backend.config import load_config_layer_stack

        if config_stack is None:
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

        # Ordinary plugin keys are namespaced. CUA retains its declared
        # canonical identity; manual declarations still win and the first
        # plugin owns an otherwise unclaimed canonical name.
        manual_signatures = {
            signature
            for config in manual.values()
            if (signature := _mcp_server_signature(config))
        }
        plugin_signatures: set[tuple[Any, ...]] = set()
        merged: dict[str, MCPServerConfig] = {}
        for config in plugin_configs:
            if config.name == "cua_repl" and config.name in merged:
                continue
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

    def allows_user_server_install(self, config: MCPServerConfig) -> bool:
        """Check a proposed global server against the current managed MCP policy."""
        from backend.config import load_config_layer_stack

        stack = load_config_layer_stack(
            cwd=self.workspace_root,
            requirements_path=self._requirements_path,
            managed_settings_dir=self._managed_settings_dir,
        )
        policy = load_mcp_policy(
            managed_settings_dir=self._managed_settings_dir,
            config_stack=stack,
        )
        enterprise_exists, _ = self._load_enterprise_configs()
        return (
            not enterprise_exists
            and not policy.strict_plugin_only
            and policy.allows(config)
            and policy.disabled_reason(config) is None
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
            if not config.enabled:
                allowed.append(config)
                continue
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

    def _load_plugin_configs(self, *, config_stack: Any | None = None,
            plugins: Iterable[Mapping[str, Any]] | None = None) -> list[MCPServerConfig]:
        if config_stack is None:
            from backend.config import load_config_layer_stack

            config_stack = load_config_layer_stack(cwd=self.workspace_root)
        try:
            from backend.plugins.manager import PluginManager
        except Exception as exc:
            raise RuntimeError("Plugin MCP discovery is unavailable") from exc

        configs: list[MCPServerConfig] = []
        if plugins is None:
            try:
                plugins = PluginManager(config_stack=config_stack).snapshot().enabled_plugins
            except Exception as exc:
                raise RuntimeError("Unified plugin snapshot unavailable for MCP") from exc
        for plugin_order, plugin in enumerate(plugins):
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
                    from backend.plugins.manifest import _load_plugin_manifest

                    loaded = _load_plugin_manifest(manifest_path)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise RuntimeError(
                        f"Failed to read plugin manifest {manifest_path}: {exc}"
                    ) from exc
                if loaded is None:
                    raise ValueError(f"Plugin manifest {manifest_path} must contain an object")
                manifest = loaded.payload
                agent_plugin = loaded.format == "agent_plugin"
                codex_plugin = not agent_plugin and manifest_path.parent.name in {".codex-plugin", ".claude-plugin", ".cursor-plugin"}

                declaration = manifest.get("mcp_servers")
                server_maps: list[Mapping[str, Any]] = []
                if agent_plugin:
                    from backend.mcp.agent_plugin_config import load_agent_plugin_mcp_config
                    from backend.plugins.layout import agent_plugin_data_root

                    data_root = agent_plugin_data_root(plugin_id)
                    servers = load_agent_plugin_mcp_config(plugin_root, data_root).servers
                    if any(server["transport"] == "stdio" for server in servers.values()):
                        try:
                            data_root.mkdir(parents=True, exist_ok=True)
                        except OSError as exc:
                            logger.warning("Failed to create Agent Plugins data directory %s: %s", data_root, exc)
                            servers = {name: server for name, server in servers.items() if server["transport"] != "stdio"}
                    self._apply_agent_plugin_env_overlay(plugin_root, servers)
                    server_maps.append(servers)
                elif isinstance(declaration, Mapping):
                    server_maps.append(declaration.get("mcpServers", declaration) if codex_plugin else declaration)
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
                        unknown_root_fields = [] if codex_plugin and "mcpServers" not in payload else sorted(set(payload) - {"servers", "mcpServers"})
                        if unknown_root_fields:
                            raise ValueError(
                                f"Plugin MCP config {config_path} contains unsupported fields: "
                                + ", ".join(str(field) for field in unknown_root_fields)
                            )
                        servers = payload.get("servers", payload.get("mcpServers", payload if codex_plugin else None))
                        if not isinstance(servers, Mapping):
                            raise ValueError(
                                f"Plugin MCP config {config_path} must contain an object field named servers"
                            )
                        server_maps.append(servers)

                for servers in server_maps:
                    for server_index, (name, raw_config) in enumerate(servers.items()):
                        plugin_settings = config_stack.effective_config().get("plugins", {}).get(plugin_id, {})
                        policy_override = plugin_settings.get("mcp_servers", {}).get(name, {})
                        raw_config = deepcopy(raw_config)
                        if codex_plugin and isinstance(raw_config, dict):
                            raw_config.pop("type", None)
                            for field in _PLUGIN_MCP_DISPLAY_FIELDS:
                                raw_config.pop(field, None)
                            if "transport" not in raw_config:
                                if "command" in raw_config:
                                    raw_config["transport"] = "stdio"
                                elif "url" in raw_config:
                                    raw_config["transport"] = "http"
                        for field_name in ("enabled", "enabled_tools", "disabled_tools", "default_tools_approval_mode"):
                            if field_name in policy_override:
                                raw_config[field_name] = policy_override[field_name]
                        for tool_name, tool_policy in policy_override.get("tools", {}).items():
                            raw_config.setdefault("tools", {}).setdefault(tool_name, {}).update(tool_policy)
                        config = self._config_from_mapping(
                            f"plugin:{plugin_id}:{name}",
                            raw_config,
                            source=f"plugin:{plugin_id}",
                            priority=plugin_order * 1000 + server_index,
                            base_dir=plugin_root,
                            config_path=plugin_root / "mcp.json" if agent_plugin else None,
                            plugin_id=plugin_id,
                            plugin_server_name=str(name),
                            resolve_placeholders=not agent_plugin,
                        )
                        from backend.mcp.cua_repl import is_computer_cua_declaration

                        if is_computer_cua_declaration(str(name), config.env):
                            config = replace(config, name="cua_repl", plugin_id=plugin_id,
                                plugin_server_name=str(name), config_path=str(manifest_path))
                        configs.append(config)
        return configs

    def _apply_agent_plugin_env_overlay(self, plugin_root: Path, servers: dict[str, dict[str, Any]]) -> None:
        if not servers:
            return
        from backend.mcp.agent_plugin_overlay import load_agent_plugin_env_overlay
        from backend.mcp.agent_plugin_config import ascii_upper

        overlay = load_agent_plugin_env_overlay(plugin_root, lambda name, server: self._config_from_mapping(
            name, server, source="agent-plugin-overlay", priority=0, base_dir=plugin_root,
            resolve_placeholders=False,
        ).env_vars)
        for name, variables in overlay.items():
            server = servers.get(name)
            if server is None or server["transport"] != "stdio":
                continue
            names_match = ascii_upper if os.name == "nt" else (lambda value: value)
            selected = {names_match(variable) for variable in variables}
            server["env"] = {key: value for key, value in server["env"].items()
                if not (value.startswith("${") and value.endswith("}")
                    and names_match(key) == names_match(value[2:-1]) and names_match(key) in selected)}
            server["env_vars"] = variables

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
        credential_policy: str = "HostFallbackAllowed",
        executor_source: bool = False,
        selected_root_id: str | None = None,
        plugin_id: str | None = None,
        plugin_server_name: str | None = None,
        resolve_placeholders: bool = True,
    ) -> MCPServerConfig:
        if not isinstance(raw_config, Mapping):
            raise ValueError(f"MCP server '{name}' from {source} must be an object")
        from backend.runtime_env import vault_subprocess_env

        environment_id = raw_config.get("environment_id", "local")
        if not isinstance(environment_id, str):
            raise ValueError(f"MCP server '{name}' environment_id must be a string")
        environment = {} if executor_source else {**os.environ, **vault_subprocess_env(f"mcp:{name}")}
        configured_env = raw_config.get("env", {})
        if resolve_placeholders and isinstance(configured_env, Mapping):
            environment.update(_resolve_mapping_placeholders(dict(configured_env), environment))
        conf = dict(raw_config) if executor_source or not resolve_placeholders else _resolve_mapping_placeholders(dict(raw_config), environment)
        if "http_headers" in conf:
            conf["headers"] = conf.pop("http_headers")
        if "http_headers_helper" in conf:
            conf["headers_helper"] = conf.pop("http_headers_helper")
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
            ("url", "headers", "headers_helper", "oauth", "bearer_token_env_var", "env_http_headers")
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
        if environment_id != "local" and raw_headers_helper:
            raise ValueError("HTTP headers helpers can only run in the local environment")
        env_http_headers = conf.get("env_http_headers") or {}
        if not isinstance(env_http_headers, Mapping) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env_http_headers.items()):
            raise ValueError("env_http_headers must map strings to strings")
        env_http_headers = dict(env_http_headers)
        if executor_source and env_http_headers:
            raise ValueError("env_http_headers requires executor-side environment resolution for an executor-owned HTTP MCP")
        for header, variable in env_http_headers.items():
            if environment.get(variable, "").strip():
                headers[header] = environment[variable]
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
        named_env_vars = []
        if env_vars is not None:
            if not isinstance(env_vars, list):
                raise ValueError(
                    f"MCP server '{name}' from {source} env_vars must be an array"
                )
            for item in env_vars:
                if isinstance(item, str):
                    target, source_name = item, "remote" if executor_source and environment_id != "local" else "local"
                elif isinstance(item, Mapping):
                    target = item.get("name")
                    source_name = item.get("source") or ("remote" if executor_source and environment_id != "local" else "local")
                    if set(item) - {"name", "source"}:
                        raise ValueError(f"MCP server '{name}' env_vars contains unsupported fields")
                else:
                    raise ValueError(f"MCP server '{name}' env_vars entries must be strings or objects")
                if not isinstance(target, str) or not target.strip() or "\x00" in target:
                    raise ValueError(f"MCP server '{name}' env_vars contains an invalid name")
                if source_name not in {"local", "remote"}:
                    raise ValueError(f"env_vars entry `{target}` source must be local or remote")
                if environment_id == "local" and source_name == "remote":
                    raise ValueError(f"env_vars entry `{target}` cannot use source `remote` in a local environment")
                if executor_source and environment_id != "local" and source_name == "local":
                    raise ValueError(f"env_vars entry `{target}` cannot use source `local` in an executor-owned plugin")
                named_env_vars.append((target, source_name))
                if source_name == "local" and target in environment:
                    env.setdefault(target, environment[target])
        from backend.runtime_env import _NON_INHERITABLE_ENV_NAMES, _scrub_non_inheritable_env

        named_env_vars = [(target, source_name) for target, source_name in named_env_vars
            if target.upper() not in _NON_INHERITABLE_ENV_NAMES]
        _scrub_non_inheritable_env(env)
        raw_cwd = conf.get("cwd")
        if raw_cwd is not None and not isinstance(raw_cwd, str):
            raise ValueError(f"MCP server '{name}' from {source} cwd must be a string")
        cwd = raw_cwd if environment_id != "local" else (
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
            tool_output_token_limits = _tool_output_token_limits(conf.get("tools"))
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
            environment_id=environment_id, env_vars=tuple(named_env_vars), credential_policy=credential_policy,
            selected_root_id=selected_root_id, plugin_id=plugin_id, plugin_server_name=plugin_server_name,
            bearer_token_env_var=conf.get("bearer_token_env_var"), env_http_headers=env_http_headers,
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
            startup_readiness=conf.get("startup_readiness", "connection"),
            supports_parallel_tool_calls=supports_parallel_tool_calls,
            enabled_tools=enabled_tools,
            disabled_tools=disabled_tools,
            enabled=conf.get("enabled", True),
            default_tools_approval_mode=default_tools_approval_mode,
            tool_approval_modes=tool_approval_modes,
            tool_output_token_limits=tool_output_token_limits,
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
        await asyncio.gather(*(self.ensure_server_startup(config) for config in configs))
        await self.capture_startup_readiness()
        self.validate_required_startups()

    def validate_required_startups(self, *, names=None) -> None:
        from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_SOURCE, is_configured_cua
        configs = [state.config for name, state in self._servers.items() if names is None or name in names]
        required_failures = [
            config.name
            for config in configs
            if config.required
            and not (is_configured_cua(config) and not config.source.startswith(CUA_SOURCE))
            and config.auto_start
            and _config_can_connect(config)
            and (
                config.name not in self._servers
                or self.get_client(config.name) is None and not (config.name in self._lazy_servers
                    and self._cached_startup(config, fallback=self._catalog_snapshots.get(config.name)) is not None)
            )
        ]
        if required_failures:
            raise RuntimeError(
                "Required MCP servers failed to initialize: "
                + ", ".join(required_failures)
            )

    def _connection_view(self, name: str):
        return self._shared_connection_views.get(name) or self._node_companion_views.get(name)

    def _startup_pending(self, name: str, task) -> bool:
        view = self._connection_view(name)
        return (view.connection.startup_pending() if view is not None else
            self._servers[name].status == ServerStatus.STARTING and task is not None and not task.done())

    async def _prepare_connection_view(self, state, *, interactive_oauth: bool = False):
        from backend.mcp.node_repl import SOURCE_PREFIX as NODE_SOURCE
        from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_SOURCE
        config = state.config
        view = self._connection_view(config.name)
        if view is None:
            if (self.environment_runtime is not None and self.environment_runtime.connection_pool is not None
                    and not config.source.startswith((NODE_SOURCE, CUA_SOURCE))):
                view = self._borrow_connection(config, interactive_oauth=interactive_oauth)
                self._shared_connection_views[config.name] = view
            elif state.client is None:
                if self.environment_runtime is None and config.environment_id != "local":
                    raise ValueError(f"MCP server `{config.name}` requires its configured execution environment `{config.environment_id}`")
                binding = (await self.environment_runtime.bind_server(config)
                    if self.environment_runtime is not None and not config.source.startswith((NODE_SOURCE, CUA_SOURCE)) else None)
                state.client = self._create_client(config, interactive_oauth=interactive_oauth, execution_binding=binding)
                view = self._connection_view(config.name)
        self._capture_catalog_context(config, view=view)
        return view

    def _capture_catalog_context(self, config, *, view=None) -> None:
        key = self._catalog_identity(config)
        if view is not None and view.connection.catalog_cache is not None:
            context = view.connection.catalog_cache
        elif config.name in self._catalog_contexts and self._catalog_keys[config.name] == key:
            context = self._catalog_contexts[config.name]
        else:
            context = TOOL_CATALOG_CACHE.context(key)
        if view is not None:
            view.connection.attach_catalog(TOOL_CATALOG_CACHE, key, context=context)
        self._catalog_keys[config.name] = key
        self._catalog_contexts[config.name] = context

    def _cached_startup(self, config, *, fallback=None):
        context = self._catalog_contexts.get(config.name)
        cached = context.get(fallback) if context is not None else None
        if cached is None or not cached.tools:
            return None
        if config.startup_readiness == "catalog":
            visible = _filter_mcp_tools(config, cached.tools)
            if config.name in self._dormant_servers:
                return cached if visible else None
            if not any(not isinstance(tool.meta.get("ui"), dict)
                    or not isinstance(tool.meta["ui"].get("visibility"), list)
                    or "model" in tool.meta["ui"]["visibility"] for tool in visible):
                return None
        return cached

    def _project_cached_startup(self, state, cached) -> None:
        raw = cached.copy_tools()
        state.raw_tools = raw
        state.tools = _filter_mcp_tools(state.config, raw)
        self._lazy_servers.add(state.config.name)
        self._catalog_snapshots[state.config.name] = cached

    async def ensure_server_startup(self, config: MCPServerConfig) -> None:
        """Admit a real connection attempt without waiting for its SDK initialize."""
        from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_SOURCE, is_configured_cua
        from backend.mcp.node_repl import SOURCE_PREFIX as NODE_SOURCE
        current = self._servers.get(config.name)
        if current is not None and _same_runtime_config(current.config, config):
            if config.name in self._startup_tasks or config.name in self._dormant_servers or current.status == ServerStatus.CONNECTED:
                return
        if current is not None and not _same_runtime_config(current.config, config):
            await self.stop_server(config.name)
        if (not config.auto_start or not _config_can_connect(config)
                or is_configured_cua(config) and not config.source.startswith(CUA_SOURCE)):
            await self.register_config(config)
            return
        lock = self._connection_locks.setdefault(config.name, asyncio.Lock())
        async with lock:
            # The first caller can publish the startup task while a second
            # caller is waiting for this per-server lock. Re-check ownership
            # after acquiring it so concurrent environment/turn preparation
            # cannot launch a second initialize process for the same config.
            current = self._servers.get(config.name)
            if current is not None and _same_runtime_config(current.config, config) and (
                config.name in self._startup_tasks
                or current.status == ServerStatus.STARTING
                or current.status == ServerStatus.CONNECTED
            ):
                return
            try:
                state = await self._prepare_state(config)
                if state.status == ServerStatus.CONNECTED:
                    return
                if (self.environment_runtime is not None and self.environment_runtime.connection_pool is not None
                        and not config.source.startswith((NODE_SOURCE, CUA_SOURCE))):
                    # Capturing a shared producer/view is synchronous. Its
                    # environment bind and SDK construction run in that owner.
                    await self._prepare_connection_view(state)
                else:
                    # A standalone/builtin connection may fail construction or
                    # wait for an environment. Keep that work inside the actual
                    # attempt so optional grace and per-server errors apply.
                    self._capture_catalog_context(config)
            except MCPTokenStoreError as exc:
                await self._report_credential_store_failure(config.name, exc)
                return
            if self._startup_policy == "LazyWhenCached":
                self._dormant_servers.add(config.name)
            cached = self._cached_startup(config)
            if self._startup_policy == "LazyWhenCached" and cached is not None:
                self._project_cached_startup(state, cached)
                await self._notify_status(config.name, state.status)
                return
            state.status = ServerStatus.STARTING
            await self._notify_status(config.name, ServerStatus.STARTING)
            self._launch_server_startup(state)

    def _launch_server_startup(self, state) -> asyncio.Task:
        name, config = state.config.name, state.config
        self._dormant_servers.discard(name)
        self._startup_admitted_at[name] = asyncio.get_running_loop().time()
        slots = self._local_startup_slots if config.transport == "stdio" else self._remote_startup_slots
        view = self._connection_view(name)
        if view is not None:
            view.connection.start(startup_slot=slots)

        async def project_startup():
            async with self._connection_locks[name]:
                if self._servers.get(name) is not state or not _same_runtime_config(state.config, config):
                    return
                await self._attempt_connection(name, state, startup_slot=slots)
                if state.status == ServerStatus.CONNECTED:
                    self._lazy_servers.discard(name)
                elif state.status == ServerStatus.ERROR:
                    cached = self._cached_startup(config, fallback=self._catalog_snapshots.get(name)) if config.startup_readiness == "catalog" else None
                    if cached is not None:
                        self._project_cached_startup(state, cached)
                        await self._notify_status(name, state.status)
                    else:
                        self._lazy_servers.discard(name)

        async def start():
            try:
                await project_startup()
            except asyncio.CancelledError:
                if self._servers.get(name) is state and state.status == ServerStatus.STARTING:
                    state.status = ServerStatus.OFFLINE
                    state.tools = []
                    self._lazy_servers.discard(name)
                    await self._notify_status(name, ServerStatus.OFFLINE)
                raise

        task = asyncio.create_task(start(), name=f"mcp-startup-view:{name}")
        self._startup_tasks[name] = task
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
        return task

    async def capture_startup_readiness(self, *, names=None, required_servers=()) -> None:
        selected = tuple(names) if names is not None else tuple(self._servers)
        grace_ms = self._optional_startup_grace_ms
        now = asyncio.get_running_loop().time()
        if self._optional_startup_deadline is None or self._optional_startup_deadline[0] != grace_ms:
            self._optional_startup_deadline = (grace_ms, now + grace_ms / 1000)
        default_deadline = self._optional_startup_deadline[1]

        async def wait_for_snapshot(name):
            state = self._servers[name]
            config = state.config
            if not config.auto_start or not _config_can_connect(config) or state.status == ServerStatus.CONNECTED:
                return None
            cached = self._cached_startup(config)
            dormant = name in self._dormant_servers
            must_wait = name in required_servers or config.required and not (cached is not None and (
                config.startup_readiness == "catalog" or dormant))
            if dormant and (must_wait or cached is None):
                task = self._launch_server_startup(state)
            else:
                task = self._startup_tasks.get(name)
            if task is None:
                return name, state, config, cached, task
            from backend.mcp.node_repl import SOURCE_PREFIX as NODE_SOURCE
            from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_SOURCE
            runtime = self.environment_runtime
            companion = (runtime.node_repl_companions if config.source.startswith(NODE_SOURCE)
                else runtime.cua_repl_companions if config.source.startswith(CUA_SOURCE) else None) if runtime is not None else None
            if task.done():
                await asyncio.shield(task)
            elif not must_wait and cached is not None:
                pass
            elif must_wait or grace_ms == 0:
                if grace_ms == 0:
                    view = self._connection_view(name)
                    if view is not None:
                        view.connection.startup_deadline(grace_ms, now)
                    context = self._catalog_contexts.get(name)
                    if context is not None:
                        context.deadline(grace_ms, now)
                    if companion is not None:
                        companion.startup_deadline(config, grace_ms, now)
                await asyncio.shield(task)
            else:
                view = self._connection_view(name)
                initial_deadline = max(default_deadline, self._startup_admitted_at.get(name, now) + grace_ms / 1000)
                deadline = view.connection.startup_deadline(grace_ms, initial_deadline) if view is not None else initial_deadline
                context = self._catalog_contexts.get(name)
                if context is not None:
                    deadline = context.deadline(grace_ms, deadline)
                if companion is not None:
                    deadline = companion.startup_deadline(config, grace_ms, deadline)
                await asyncio.wait({task}, timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
            return name, state, config, cached, task

        async def project(snapshot):
            if snapshot is None:
                return
            name, state, config, cached, task = snapshot
            if self._servers.get(name) is not state or state.status == ServerStatus.CONNECTED:
                return
            if task is None:
                current = self._cached_startup(config, fallback=cached)
                if current is not None:
                    self._project_cached_startup(state, current)
                return
            accepts_cache = self._startup_pending(name, task) or config.startup_readiness == "catalog"
            current = self._cached_startup(config, fallback=cached) if accepts_cache else None
            # A server can opt out of catalog caching after the first pass.
            # Required catalog readiness must then use the actual connection.
            if current is None and config.required and config.startup_readiness == "catalog" and not task.done():
                await asyncio.shield(task)
            if state.status != ServerStatus.CONNECTED:
                accepts_cache = self._startup_pending(name, task) or config.startup_readiness == "catalog"
                current = self._cached_startup(config, fallback=cached) if accepts_cache else None
                if current is not None:
                    self._project_cached_startup(state, current)
                else:
                    self._lazy_servers.discard(name)

        snapshots = await asyncio.gather(*(wait_for_snapshot(name) for name in selected if name in self._servers))
        await asyncio.gather(*(project(snapshot) for snapshot in snapshots))

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
            if changed and current is not None and current.status == ServerStatus.CONNECTED and _same_connection_config(current.config, config):
                async with self._connection_locks[name]:
                    current.config = config
                    if current.status == ServerStatus.CONNECTED:
                        current.tools = _filter_mcp_tools(config, current.raw_tools)
                    await self._notify_status(name, current.status)
                continue
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
        from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_SOURCE, is_configured_cua

        if is_configured_cua(config) and not config.source.startswith(CUA_SOURCE):
            # Computer control belongs to the receiving thread's captured
            # environment. Merely loading host MCP settings must not start it.
            await self.register_config(config)
            return
        if not _config_can_connect(config):
            await self.register_config(config)
            return
        await self._retire_oauth_login_for_config(config)
        # A server owns exactly one connect/OAuth attempt. Config reloads cannot
        # start another browser flow while one is already in progress.
        await self._cancel_reconnect_task(config.name)
        lock = self._connection_locks.get(config.name)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[config.name] = lock
        async with lock:
            try:
                state = await self._prepare_state(config)
            except MCPTokenStoreError as exc:
                await self._report_credential_store_failure(config.name, exc)
                return
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
        await self._retire_oauth_login_for_config(config)
        await self._cancel_reconnect_task(config.name)
        lock = self._connection_locks.get(config.name)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[config.name] = lock
        async with lock:
            try:
                state = await self._prepare_state(config)
            except MCPTokenStoreError as exc:
                await self._report_credential_store_failure(config.name, exc)
                return
            state.status = ServerStatus.OFFLINE
            state.tools = []
            state.raw_tools = []
            state.last_error = ""
            state.last_exception = None
            state.operation_failures.pop("credential_store", None)
            await self._notify_status(config.name, ServerStatus.OFFLINE)

    async def _report_credential_store_failure(self, name: str, exc: MCPTokenStoreError) -> None:
        state = self._servers[name]
        state.status = ServerStatus.ERROR
        state.tools = []
        state.last_exception = exc
        state.last_error = f"MCP credential storage failed: {exc}"
        _record_operation_failure(state, "credential_store", exc, retryable=True)
        await self._notify_status(name, ServerStatus.ERROR)

    async def _close_client_owner(self, name: str, client: MCPClient) -> bool:
        shared = self._shared_connection_views.get(name)
        if shared is not None and shared.client is client:
            closed = await shared.aclose()
            self._shared_connection_views.pop(name)
            return closed
        view = self._node_companion_views.get(name)
        if view is not None and view.client is client:
            closed = await view.aclose()
            self._node_companion_views.pop(name)
            return closed
        return await client.close()

    async def stop_server(self, name: str) -> bool:
        async with self._oauth_operation_locks.setdefault(name, asyncio.Lock()):
            await self._cancel_oauth_login_unlocked(name)
            return await self._stop_server_transport(name)

    async def _stop_server_transport(self, name: str) -> bool:
        state = self._servers.get(name)
        if state is None:
            return True

        await self._cancel_reconnect_task(name)
        startup = self._startup_tasks.pop(name, None)
        if startup is not None and startup is not asyncio.current_task() and not startup.done():
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
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
            if client is None:
                borrowed = self._shared_connection_views.pop(name, None) or self._node_companion_views.pop(name, None)
                if borrowed is not None:
                    await borrowed.aclose()
            if client is not None:
                try:
                    closed = await self._close_client_owner(name, client)
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
            state.raw_tools = []
            state.retry_count = 0
            state.last_error = ""
            state.last_exception = None
            state.operation_failures.clear()
            self._lazy_servers.discard(name)
            self._dormant_servers.discard(name)
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
        self._resource_subscriptions.pop(name, None)
        self._connection_locks.pop(name, None)
        self._oauth_operation_locks.pop(name, None)
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
        # Re-read references before the next launch; vault values may have changed.
        config = {item.name: item for item in self.load_config()}[name]
        if not await self.stop_server(name):
            raise RuntimeError(
                f"MCP server '{name}' is still shutting down and cannot restart"
            )
        await self._restart_connection_owner(name, config)
        await self.start_server(config, force=True)

    async def _restart_connection_owner(self, name: str, config: MCPServerConfig) -> None:
        runtime = self.environment_runtime
        if runtime is not None:
            from backend.mcp.node_repl import SOURCE_PREFIX as NODE_SOURCE
            from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_SOURCE
            if config.source.startswith(NODE_SOURCE):
                await runtime.node_repl_companions.restart(config)
            elif config.source.startswith(CUA_SOURCE):
                await runtime.cua_repl_companions.restart(config)
            elif runtime.connection_pool is not None:
                await runtime.connection_pool.restart(name)

    async def cancel_startup(self) -> None:
        """An idle/owner interrupt cancels producers; view retirement only releases views."""
        failures = []
        for name in tuple(self._oauth_login_attempts):
            async with self._oauth_operation_locks.setdefault(name, asyncio.Lock()):
                try:
                    await self._cancel_oauth_login_unlocked(name)
                except Exception as exc:
                    failures.append(exc)
        views = tuple(self._shared_connection_views.values()) + tuple(self._node_companion_views.values())
        results = await asyncio.gather(*(view.connection.cancel_startup() for view in views), return_exceptions=True)
        pending = [task for task in self._startup_tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            results.extend(await asyncio.gather(*pending, return_exceptions=True))
        failures.extend(result for result in results if isinstance(result, Exception))
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("MCP startup cleanup failed", failures)

    async def oauth_login(self, name: str) -> None:
        """Run the official MCP SDK OAuth flow after an explicit user action."""
        async with self._oauth_operation_locks.setdefault(name, asyncio.Lock()):
            state = self._servers.get(name)
            if state is None:
                raise KeyError(f"MCP server '{name}' is not configured")
            scope = self._oauth_login_scope(state.config)
            attempt = self._oauth_login_attempts.get(name)
            if (attempt is None or attempt.task.done() or attempt.state is not state
                    or attempt.scope != scope or not _same_runtime_config(attempt.config, state.config)):
                await self._cancel_oauth_login_unlocked(name)
                config = deepcopy(state.config)
                task = asyncio.create_task(self._perform_oauth_login(name, config), name=f"mcp-oauth-login-{name}")
                attempt = _OAuthLoginAttempt(state, config, scope, task)
                self._oauth_login_attempts[name] = attempt
                def settled(completed, attempt=attempt):
                    if self._oauth_login_attempts.get(name) is attempt:
                        self._oauth_login_attempts.pop(name)
                    _consume_task_result(completed)
                task.add_done_callback(settled)
        # A window waiting on authentication is an observer. The configured
        # manager owns cancellation and drains the SDK/callback producer.
        await asyncio.shield(attempt.task)

    def _oauth_login_scope(self, config: MCPServerConfig):
        from backend.mcp.catalog_identity import connection_identity
        return self.workspace_root, connection_identity(config, self.environment_runtime,
            self._client_capability_identity(config), None,
            requires_read_only_tools=self._requires_read_only_tools)

    async def _retire_oauth_login_for_config(self, config: MCPServerConfig) -> None:
        attempt = self._oauth_login_attempts.get(config.name)
        if attempt is not None and attempt.task is asyncio.current_task():
            return
        async with self._oauth_operation_locks.setdefault(config.name, asyncio.Lock()):
            attempt = self._oauth_login_attempts.get(config.name)
            if attempt is not None and (attempt.state is not self._servers.get(config.name)
                    or attempt.scope != self._oauth_login_scope(config) or not _same_runtime_config(attempt.config, config)):
                await self._cancel_oauth_login_unlocked(config.name)

    async def _cancel_oauth_login_unlocked(self, name: str) -> None:
        attempt = self._oauth_login_attempts.get(name)
        if attempt is None or attempt.task is asyncio.current_task():
            return
        if not attempt.task.done():
            attempt.task.cancel()
        await asyncio.gather(attempt.task, return_exceptions=True)
        # Cancellation is expected; a failed producer remains the owner's error.
        if not attempt.task.cancelled():
            attempt.task.result()

    async def _perform_oauth_login(self, name: str, config: MCPServerConfig) -> None:
        if not await self._stop_server_transport(name):
            raise RuntimeError(f"MCP server '{name}' is still shutting down and cannot log in")
        await self._restart_connection_owner(name, config)
        await self.start_server(
            config,
            interactive_oauth=True,
            force=True,
        )

    async def oauth_logout(self, name: str) -> None:
        async with self._oauth_operation_locks.setdefault(name, asyncio.Lock()):
            await self._cancel_oauth_login_unlocked(name)
            state = self._servers.get(name)
            if state is None:
                raise KeyError(f"MCP server '{name}' is not configured")
            if not await self._stop_server_transport(name):
                raise RuntimeError(
                    f"MCP server '{name}' is still shutting down and cannot log out"
                )
            await self._restart_connection_owner(name, state.config)
            store = self._token_store.for_server(state.config.url or "", state.config.oauth_client_id)
            await to_thread_cancel_safe(store.clear, state.config.oauth_credential_name())
            self._resource_subscriptions.pop(name, None)
            state.auth_status = (
                MCPAuthStatus.NOT_LOGGED_IN
                if state.config.oauth_client_id
                else MCPAuthStatus.UNSUPPORTED
            )
            await self._notify_status(name, ServerStatus.OFFLINE)

    def get_all_tools(self) -> dict[str, list[MCPToolDef]]:
        result: dict[str, list[MCPToolDef]] = {}
        for name, state in self._servers.items():
            cached = (name in self._lazy_servers and self._cached_startup(state.config,
                fallback=self._catalog_snapshots.get(name)) is not None)
            if (self.get_client(name) is not None or cached) and state.tools:
                result[name] = state.tools
        return result

    def has_enabled_servers(self) -> bool:
        return any(state.config.enabled for state in self._servers.values())

    async def prepare_server_call(self, server_name: str) -> None:
        if server_name in self._dormant_servers:
            self._launch_server_startup(self._servers[server_name])
        await self.wait_for_server_startup(server_name)
        if self.get_client(server_name) is not None:
            self._lazy_servers.discard(server_name)
        else:
            state = self._servers.get(server_name)
            raise ConnectionError(state.last_error if state is not None and state.last_error
                else f"MCP server '{server_name}' is not connected")

    async def wait_for_server_startup(self, server_name: str) -> None:
        """Wait for this server's admitted connection attempt to settle."""
        reconnect = self._reconnect_tasks.get(server_name)
        if reconnect is not None and reconnect is not asyncio.current_task() and not reconnect.done():
            await asyncio.shield(reconnect)
        task = self._startup_tasks.get(server_name)
        if task is not None:
            await asyncio.shield(task)
            return
        lock = self._connection_locks.get(server_name)
        if lock is not None:
            async with lock:
                pass

    def get_client(self, server_name: str) -> MCPClient | None:
        state = self._servers.get(server_name)
        if state and state.status == ServerStatus.CONNECTED and state.client is not None and state.client.connected:
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

    def catalog_is_cached(self, server_name: str) -> bool:
        return server_name in self._lazy_servers

    def get_server_contract(self, server_name: str) -> tuple[Any, ...] | None:
        state = self._servers.get(server_name)
        if state is None:
            return None
        if server_name in self._lazy_servers:
            if self._cached_startup(state.config, fallback=self._catalog_snapshots.get(server_name)) is None:
                return None
            return (_mcp_server_identity(state.config), self._catalog_snapshots[server_name].instructions)
        if state.client is None:
            return None
        return (_mcp_server_identity(state.config), state.client.instructions)

    def iter_connected_clients(self) -> list[tuple[str, Any]]:
        """Return connected MCP clients using the manager's authoritative state."""
        result: list[tuple[str, Any]] = []
        for name, state in self._servers.items():
            client = state.client
            if state.status == ServerStatus.CONNECTED and client and client.connected:
                result.append((name, client))
        return result

    def pending_requests(self) -> tuple[asyncio.Task, ...]:
        return tuple(task for state in self._servers.values() if state.client is not None
            for task in state.client.pending_requests())

    def get_server_instructions(self) -> dict[str, str]:
        """Return {server_name: instructions} for connected servers that declared any."""
        result: dict[str, str] = {}
        for name, state in self._servers.items():
            if name in self._lazy_servers:
                cached = self._cached_startup(state.config, fallback=self._catalog_snapshots.get(name))
                text = cached.instructions.strip() if cached is not None else ""
                if text:
                    result[name] = text
                continue
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
                shared = self._shared_connection_views.get(name)
                if shared is not None:
                    await self.environment_runtime.connection_pool.reconnect(name, shared.connection)
                    await shared.aclose()
                    self._shared_connection_views.pop(name)
                    state.client = None
                elif stale_client is not None:
                    try:
                        closed = await self._close_client_owner(name, stale_client)
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
                        self._schedule_cleanup_reaper(name, state, stale_client)
                        await self._notify_status(name, ServerStatus.ERROR)
                        return
                    state.client = None

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
        startup_slot: asyncio.Semaphore | None = None,
    ) -> None:
        client = state.client
        try:
            view = await self._prepare_connection_view(state, interactive_oauth=interactive_oauth)
            client = state.client
            if view is not None:
                view.connection.start(startup_slot=startup_slot)
                result = await view.wait_for_startup()
                client = result.client
                state.client = client
                raw_tools = list(result.tools)
            else:
                async with (startup_slot if startup_slot is not None else nullcontext()):
                    context = self._catalog_contexts[name]
                    ticket = context.begin_fetch() if context is not None else None
                    connect_timeout = 310.0 if interactive_oauth else _mcp_connection_timeout_seconds(state.config) + 1.0
                    await asyncio.wait_for(client.connect(), timeout=connect_timeout)
                    if context is not None and not client.tool_catalog_cacheable:
                        context.disable()
                    raw_tools = await client.list_tools()
                    if context is not None:
                        context.publish(ticket, raw_tools, client.instructions)
            # Replay durable subscription intent after every new transport
            # session.  The old client-owned set cannot survive reconnect.
            restore_failures: list[tuple[str, BaseException]] = []
            for uri in sorted(self._resource_subscriptions.get(name, set())):
                try:
                    restored = await client.subscribe_resource(uri)
                    if not restored:
                        raise RuntimeError(
                            "server does not support resource subscriptions"
                        )
                except Exception as exc:
                    restore_failures.append((uri, exc))
            state.raw_tools = raw_tools
            state.tools = _filter_mcp_tools(state.config, state.raw_tools)
            stored_auth_status = self._stored_auth_status(state.config)
            state.auth_status = (
                MCPAuthStatus.OAUTH
                if bool(getattr(client, "has_valid_token", False))
                or stored_auth_status == MCPAuthStatus.OAUTH
                else stored_auth_status
            )
            state.status = ServerStatus.CONNECTED
            self._lazy_servers.discard(name)
            self._dormant_servers.discard(name)
            state.retry_count = 0
            state.last_error = ""
            state.last_exception = None
            state.operation_failures.pop("connect", None)
            state.operation_failures.pop("reconnect", None)
            state.operation_failures.pop("credential_store", None)
            if restore_failures:
                failed_uris = [uri for uri, _ in restore_failures]
                state.operation_failures["resource_restore"] = {
                    "operation": "resource_restore",
                    "failure_kind": "partial_failure",
                    "message": (
                        f"Failed to restore {len(failed_uris)} MCP resource "
                        f"subscription(s): {', '.join(failed_uris)}"
                    ),
                    "retryable": True,
                    "uris": failed_uris,
                }
                for uri, exc in restore_failures:
                    logger.warning(
                        "MCP server '%s' could not restore resource subscription %s: %s",
                        name,
                        uri,
                        exc,
                    )
            else:
                state.operation_failures.pop("resource_restore", None)
            state.operation_failures.pop("tool_catalog", None)
            await self._notify_status(name, ServerStatus.CONNECTED)
        except (asyncio.CancelledError, Exception) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            cleanup_error: Exception | None = None
            cleanup_pending = False
            borrowed = self._shared_connection_views.get(name) if cancelled else None
            if not cancelled and self._connection_view(name) is not None:
                # The connection producer closed its failed transport. Retain
                # this view's exact settled attempt until view retirement or an
                # explicit reconnect; its failure must not become "pending".
                client = None
            elif borrowed is not None or client is not None:
                try:
                    if borrowed is not None:
                        await borrowed.aclose()
                        self._shared_connection_views.pop(name)
                        client = None
                    else:
                        cleanup_pending = not await self._close_client_owner(name, client)
                except Exception as close_exc:
                    cleanup_error = close_exc
                    if borrowed is not None:
                        client = borrowed.client
            state.client = client if cleanup_pending or cleanup_error is not None else None
            state.status = (
                ServerStatus.ERROR
                if not cancelled or cleanup_pending or cleanup_error is not None
                else ServerStatus.OFFLINE
            )
            state.last_exception = cleanup_error if cancelled else exc
            state.tools = []
            state.last_error = "MCP initialization cancelled" if cancelled else f"{exc.__class__.__name__}: {exc}"
            if not cancelled:
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
                if client is not None:
                    self._schedule_cleanup_reaper(name, state, client)
            if cancelled:
                if not cleanup_pending and cleanup_error is None:
                    state.last_error = ""
                await self._notify_status(name, state.status)
                if cleanup_error is not None:
                    raise cleanup_error from exc
                raise
            phase, _, _ = classify_mcp_phase(state.status, state.last_error, exc)
            if phase in {"auth_required", "expired"}:
                state.auth_status = MCPAuthStatus.NOT_LOGGED_IN
            elif not isinstance(exc, MCPTokenStoreError):
                try:
                    state.auth_status = self._stored_auth_status(state.config)
                except MCPTokenStoreError as storage_exc:
                    state.last_error += f"; MCP credential storage failed: {storage_exc}"
                    _record_operation_failure(state, "credential_store", storage_exc, retryable=True)
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
        connection_changed = not _same_connection_config(state.config, config)
        if state.client is not None and (connection_changed or not state.client.connected):
            stale_client = state.client
            try:
                closed = await self._close_client_owner(config.name, stale_client)
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
            state.raw_tools = []
            state.operation_failures.pop("cleanup", None)
        if _mcp_server_identity(state.config) != _mcp_server_identity(config):
            self._resource_subscriptions.pop(config.name, None)
        state.config = config
        if config_changed and not connection_changed and state.status == ServerStatus.CONNECTED:
            state.tools = _filter_mcp_tools(config, state.raw_tools)
            await self._notify_status(config.name, state.status)
        stored_auth_status = self._stored_auth_status(config)
        if (
            config_changed
            or stored_auth_status != MCPAuthStatus.UNSUPPORTED
            or state.auth_status == MCPAuthStatus.UNSUPPORTED
        ):
            state.auth_status = stored_auth_status
        return state

    def _stored_auth_status(self, config: MCPServerConfig) -> MCPAuthStatus:
        if config.credential_policy == "ExecutorOnly":
            return MCPAuthStatus.UNSUPPORTED
        if config.transport not in {"sse", "http"}:
            return MCPAuthStatus.UNSUPPORTED
        token_store = self._token_store.for_server(config.url or "", config.oauth_client_id)
        if token_store.has_sdk_tokens(config.oauth_credential_name()):
            return MCPAuthStatus.OAUTH
        tokens = token_store.get(config.oauth_credential_name())
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
        execution_binding=None,
        connection_events=None,
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

        if connection_events is not None:
            on_disconnect = connection_events.notify_disconnect
            on_tools_changed = connection_events.notify_tools_changed
        else:
            async def on_disconnect(_: str) -> None:
                await self._handle_client_disconnect(config.name, client)

            async def on_tools_changed(_: str) -> None:
                context = self._catalog_contexts.get(config.name)
                if context is not None:
                    context.invalidate()
                self._schedule_tool_refresh(config.name, client)

        from backend.mcp.node_repl import SOURCE_PREFIX
        from backend.mcp.cua_repl import SOURCE_PREFIX as CUA_SOURCE

        if config.source.startswith((CUA_SOURCE, SOURCE_PREFIX)) and self.environment_runtime is None:
            raise ValueError("Builtin MCP companion requires a captured receiving environment")
        if config.source.startswith(CUA_SOURCE):
            client, view = self.environment_runtime.cua_repl_companions.client_for(config,
                on_disconnect=on_disconnect, on_tools_changed=on_tools_changed)
            client._elicitation_handler = self._elicitation_handler
            self._node_companion_views[config.name] = view
            return client

        if config.source.startswith(SOURCE_PREFIX):
            client, view = self.environment_runtime.node_repl_companions.client_for(config,
                on_disconnect=on_disconnect, on_tools_changed=on_tools_changed)
            self._node_companion_views[config.name] = view
            return client

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
            token_store=self._token_store if config.credential_policy == "HostFallbackAllowed" and transport in (MCPTransport.SSE, MCPTransport.HTTP) else None,
            interactive_oauth=interactive_oauth,
            elicitation_handler=self._elicitation_handler,
            on_disconnect=on_disconnect,
            on_tools_changed=on_tools_changed,
            workspace_root=self.workspace_root if config.environment_id == "local" else None,
            execution_binding=execution_binding, bearer_token_env_var=config.bearer_token_env_var,
            oauth_credential_name=config.oauth_credential_name(),
            feature_flags=self._feature_flags, requires_read_only_tools=self._requires_read_only_tools,
        )
        return client

    def _schedule_tool_refresh(self, name: str, client: MCPClient) -> None:
        state = self._servers.get(name)
        if state is None or state.client is not client:
            return
        # A notification ends the old schema's authority immediately. Publish
        # the replacement only after the server has returned its complete list.
        state.tools = []
        self._registry_version += 1
        self._pending_tool_refreshes[name] = client
        active = self._tool_refresh_tasks.get(name)
        if active is not None and not active.done():
            return
        task = asyncio.create_task(
            self._refresh_server_tools(name),
            name=f"mcp-tools-refresh-{name}",
        )
        self._tool_refresh_tasks[name] = task

    async def _refresh_server_tools(self, name: str) -> None:
        current_task = asyncio.current_task()
        lock = self._connection_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[name] = lock
        try:
            async with lock:
                while name in self._pending_tool_refreshes:
                    client = self._pending_tool_refreshes.pop(name)
                    state = self._servers.get(name)
                    if (
                        state is None
                        or state.client is not client
                        or not client.connected
                        or not (
                            state.status == ServerStatus.CONNECTED
                            or _has_tool_catalog_error(state)
                        )
                    ):
                        continue
                    try:
                        view = self._shared_connection_views.get(name) or self._node_companion_views.get(name)
                        if view is not None:
                            tools = list((await view.connection.refresh_catalog()).tools)
                        else:
                            context = self._catalog_contexts.get(name)
                            ticket = context.begin_fetch() if context is not None else None
                            tools = await client.list_tools()
                            if context is not None:
                                context.publish(ticket, tools, client.instructions)
                    except Exception as exc:
                        if state.client is client and client.connected:
                            state.status = ServerStatus.ERROR
                            state.tools = []
                            state.last_exception = exc
                            state.last_error = f"MCP tool catalog refresh failed: {type(exc).__name__}: {exc}"
                            _record_operation_failure(state, "tool_catalog", exc, retryable=True)
                            await self._notify_status(name, ServerStatus.ERROR)
                        continue
                    if state.client is not client or not client.connected:
                        continue
                    state.status = ServerStatus.CONNECTED
                    state.raw_tools = tools
                    state.tools = _filter_mcp_tools(state.config, state.raw_tools)
                    state.last_error = ""
                    state.last_exception = None
                    state.operation_failures.pop("tool_catalog", None)
                    await self._notify_status(name, ServerStatus.CONNECTED)
        finally:
            if self._tool_refresh_tasks.get(name) is current_task:
                self._tool_refresh_tasks.pop(name, None)
                self._pending_tool_refreshes.pop(name, None)

    async def _cancel_tool_refresh_task(self, name: str) -> None:
        self._pending_tool_refreshes.pop(name, None)
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
        if state is None or not (
            state.status == ServerStatus.CONNECTED or _has_tool_catalog_error(state)
        ):
            return
        if disconnected_client is not None and state.client is not disconnected_client:
            # A previous SDK lifecycle may finish after an explicit restart.
            # Its callback must never detach or reconnect the newer client.
            return
        state.tools = []
        # Transport end is not client end: OAuth callback peers and the SDK
        # lifecycle may still be closing. Keep this owner until close completes.

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
            try:
                await self._on_status_change(name, status)
            except Exception:
                logger.exception(
                    "MCP status observer failed for server %s (%s)",
                    name,
                    status.value,
                )


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


def _resolve_mapping_placeholders(value: Any, environment: Mapping[str, str] | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_mapping_placeholders(item_value, environment)
            for key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_resolve_mapping_placeholders(item, environment) for item in value]
    return _resolve_env_placeholders(value, environment)


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
        unknown_fields = sorted(set(raw_config) - {"approval_mode", "output_token_limit"})
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


def _mcp_server_signature(config: MCPServerConfig) -> tuple[Any, ...] | None:
    """Compare transport and credentials independently of declaration scope."""
    if config.transport != "stdio" and not config.url:
        return None
    return _mcp_server_identity(config, include_scope=False)


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
        (left.environment_id, left.env_vars, left.credential_policy, left.selected_root_id, left.plugin_id,
            left.plugin_server_name, left.bearer_token_env_var, left.env_http_headers) == (
            right.environment_id, right.env_vars, right.credential_policy, right.selected_root_id, right.plugin_id,
            right.plugin_server_name, right.bearer_token_env_var, right.env_http_headers)
        and left.command == right.command
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
        and left.startup_readiness == right.startup_readiness
        and left.supports_parallel_tool_calls == right.supports_parallel_tool_calls
        and left.enabled_tools == right.enabled_tools
        and left.disabled_tools == right.disabled_tools
        and left.default_tools_approval_mode == right.default_tools_approval_mode
        and left.tool_approval_modes == right.tool_approval_modes
        and left.tool_output_token_limits == right.tool_output_token_limits
        and left.enabled == right.enabled
        and left.disabled_reason == right.disabled_reason
    )


def _tool_output_token_limits(value: Any) -> dict[str, int]:
    limits = {}
    for name, tool_config in (value or {}).items():
        limit = tool_config.get("output_token_limit")
        if limit is not None:
            if type(limit) is not int or limit <= 0:
                raise ValueError(f"tools.{name}.output_token_limit must be a positive integer")
            limits[name] = limit
    return limits


def _same_connection_config(left: MCPServerConfig, right: MCPServerConfig) -> bool:
    return _same_runtime_config(replace(
        left,
        enabled_tools=right.enabled_tools,
        disabled_tools=right.disabled_tools,
        default_tools_approval_mode=right.default_tools_approval_mode,
        tool_approval_modes=right.tool_approval_modes,
        tool_output_token_limits=right.tool_output_token_limits,
        supports_parallel_tool_calls=right.supports_parallel_tool_calls,
        required=right.required,
        startup_readiness=right.startup_readiness,
        startup_timeout_sec=right.startup_timeout_sec,
        tool_timeout_sec=right.tool_timeout_sec,
    ), right)


def _mcp_server_identity(config: MCPServerConfig, *, include_scope: bool = True) -> tuple[Any, ...]:
    identity = (
        config.environment_id, config.env_vars, config.credential_policy, config.selected_root_id,
        config.plugin_id, config.plugin_server_name, config.bearer_token_env_var, tuple(sorted(config.env_http_headers.items())),
        config.transport, config.command, tuple(config.args), config.cwd,
        tuple(sorted(config.env.items())), config.url,
        tuple(sorted(config.headers.items())), config.headers_helper,
        config.oauth_client_id, config.oauth_callback_port,
        # HTTP OAuth credentials are stored by server name; headers helpers
        # also receive that name and can return different account credentials.
        config.name if config.transport != "stdio" else "",
    )
    return (*identity, config.source, config.project_workspace) if include_scope else identity


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
