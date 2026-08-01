from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from backend.config import PROJECT_ROOT
from backend.mcp.client import MCPClient, MCPToolDef, MCPTransport

logger = logging.getLogger(__name__)

_ALLOWED_MCP_COMMANDS = frozenset({
    "python", "python3", "python3.11", "python3.12", "python3.13",
    "node", "npx", "npm", "uv", "uvx", "pip", "pipx",
    "deno", "bun", "bunx", "tsx", "ts-node",
    "docker", "podman",
})

_DANGEROUS_SHELL_CHARS = re.compile(r"[;&|`$(){}!<>]")


def _is_safe_mcp_command(command: str) -> bool:
    """Validate that an MCP server command is from the allowlist."""
    cmd = command.strip()
    base = Path(cmd).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base in _ALLOWED_MCP_COMMANDS:
        return True
    if cmd.startswith(("/", "\\")) or (len(cmd) > 2 and cmd[1] == ":"):
        base = Path(cmd).stem.lower()
        return base in _ALLOWED_MCP_COMMANDS
    return False


def _has_shell_injection(args: list[str]) -> bool:
    """Check if args contain shell metacharacters that suggest injection."""
    for arg in args:
        if _DANGEROUS_SHELL_CHARS.search(arg):
            return True
    return False


def _unsafe_stdio_config_reason(command: str, args: list[str]) -> str:
    if not _is_safe_mcp_command(command):
        return f"command '{command}' is not in the executable allowlist"
    if _has_shell_injection(args):
        return "arguments contain shell metacharacters"
    return ""


MCP_CONFIG_FILE = PROJECT_ROOT / ".mcp.json"
OPENMCP_CONFIG_FILE = PROJECT_ROOT / ".openmcp" / "connection.json"
_HEALTH_CHECK_INTERVAL_SECONDS = 60.0
_ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep


class ServerStatus(Enum):
    OFFLINE = "offline"
    STARTING = "starting"
    CONNECTED = "connected"
    ERROR = "error"
    RECONNECTING = "reconnecting"


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
    command: str = "python"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    transport: str = "stdio"
    url: str | None = None
    auto_start: bool = True
    max_retries: int = 3
    source: str = "local"
    priority: int = 1000
    requires_user_action: bool = False
    setup_hint: str = ""
    docs_url: str = ""


def validate_mcp_server_config(config: MCPServerConfig) -> None:
    """Apply one fail-closed runtime boundary to every MCP config source."""

    transport = str(config.transport or "stdio").strip().lower()
    if transport in {"streamable-http", "streamablehttp"}:
        transport = "streamable_http"
    if transport not in {"stdio", "http", "streamable_http", "websocket", "in_process"}:
        raise ValueError(f"unsupported MCP transport '{transport}'")
    # Validation is also the canonical normalization boundary.  Keeping the
    # normalized value on the config prevents aliases accepted at ingress from
    # falling through to the stdio branch when the client is constructed.
    config.transport = transport
    if transport == "stdio":
        unsafe_reason = _unsafe_stdio_config_reason(config.command, list(config.args))
        if unsafe_reason:
            raise ValueError(unsafe_reason)
        return
    if transport == "in_process":
        return
    url = str(config.url or "").strip()
    if not url:
        raise ValueError(f"MCP {transport} transport requires a URL")
    scheme = urlparse(url).scheme.lower()
    allowed_schemes = {"ws", "wss"} if transport == "websocket" else {"http", "https"}
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

    def to_status_dict(self) -> dict[str, Any]:
        phase, recoverable, requires_user_action = classify_mcp_phase(
            self.status,
            self.last_error,
            self.last_exception,
        )
        requires_user_action = requires_user_action or (
            self.config.requires_user_action and self.status != ServerStatus.CONNECTED
        )
        return {
            "name": self.config.name,
            "status": self.status.value,
            "tools_count": len(self.tools),
            "error": self.last_error if self.status == ServerStatus.ERROR else "",
            "source": self.config.source,
            "priority": self.config.priority,
            "transport": self.config.transport,
            "phase": phase,
            "recoverable": recoverable,
            "requires_user_action": requires_user_action,
            "setup_hint": self.config.setup_hint,
            "docs_url": self.config.docs_url,
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
        return {
            "server_name": self.config.name,
            "status": self.status.value,
            "phase": phase,
            "message": self.last_error or _PHASE_DEFAULT_MESSAGE.get(phase, ""),
            "recoverable": recoverable,
            "requires_user_action": requires_user_action,
            "setup_hint": self.config.setup_hint,
            "docs_url": self.config.docs_url,
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
        openmcp_config_path: Path | None = None,
        sampling_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        elicitation_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._config_path = config_path or MCP_CONFIG_FILE
        self._openmcp_config_path = openmcp_config_path or OPENMCP_CONFIG_FILE
        self._servers: dict[str, MCPServerState] = {}
        self._on_status_change = on_status_change
        self._sampling_handler = sampling_handler
        self._elicitation_handler = elicitation_handler
        self._health_tasks: dict[str, asyncio.Task[Any]] = {}
        self._connection_locks: dict[str, asyncio.Lock] = {}
        # OAuth tokens for HTTP MCP servers, persisted alongside the MCP config.
        from backend.mcp.oauth import TokenStore

        self._token_store = TokenStore(self._config_path.parent / "mcp_tokens.json")
        # Monotonic counter bumped on every server status change. Consumers
        # (tool registry schema cache) include it in their cache key so a
        # connect/disconnect/reconnect invalidates stale tool schemas.
        self._registry_version = 0

    @property
    def registry_version(self) -> int:
        return self._registry_version

    def load_config(self) -> list[MCPServerConfig]:
        plugin_configs = self._load_plugin_configs()
        external_configs = self._load_openmcp_configs()
        local_configs = self._load_local_configs()

        merged: dict[str, MCPServerConfig] = {}
        # Codex resolves plugin registrations below user config. Preserve the
        # existing MiniCode connector precedence (OpenMCP > local fallback)
        # while ensuring either configured tier replaces plugin-owned entries.
        for config in plugin_configs:
            merged.setdefault(config.name, config)
        for config in external_configs:
            merged[config.name] = config
        external_names = {config.name for config in external_configs}
        for config in local_configs:
            if config.name in external_names:
                continue
            merged[config.name] = config

        return list(merged.values())

    def _load_plugin_configs(self) -> list[MCPServerConfig]:
        try:
            from backend.commands.plugins import _iter_plugin_manifests, default_plugin_roots
            from backend.services.plugin_settings_service import (
                get_disabled_plugin_names,
                plugin_directory_for_manifest,
                plugin_name_from_manifest,
            )
        except Exception:
            logger.warning("Plugin MCP discovery is unavailable", exc_info=True)
            return []

        disabled = get_disabled_plugin_names()
        configs: list[MCPServerConfig] = []
        for plugin_order, manifest_path in enumerate(_iter_plugin_manifests(default_plugin_roots())):
            plugin_root = plugin_directory_for_manifest(manifest_path)
            plugin_name = plugin_name_from_manifest(manifest_path) or plugin_root.name
            if plugin_name.casefold() in disabled:
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("Failed to read plugin manifest %s: %s", manifest_path, exc)
                continue
            if not isinstance(manifest, Mapping):
                continue

            declaration = manifest.get("mcpServers")
            server_maps: list[Mapping[str, Any]] = []
            if isinstance(declaration, Mapping):
                server_maps.append(declaration)
            else:
                config_path = plugin_root / ".mcp.json"
                if isinstance(declaration, str) and declaration.strip():
                    candidate = _resolve_plugin_relative_path(plugin_root, declaration)
                    if candidate is None:
                        logger.warning("Plugin '%s' declares an unsafe MCP path", plugin_name)
                        continue
                    config_path = candidate
                if config_path.is_file():
                    try:
                        payload = json.loads(config_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                        logger.warning("Failed to read plugin MCP config %s: %s", config_path, exc)
                        continue
                    servers = payload.get("mcpServers") if isinstance(payload, Mapping) else None
                    if isinstance(servers, Mapping):
                        server_maps.append(servers)

            for servers in server_maps:
                for server_index, (name, raw_config) in enumerate(servers.items()):
                    config = self._config_from_mapping(
                        str(name),
                        raw_config,
                        source=f"plugin:{plugin_name}",
                        priority=plugin_order * 1000 + server_index,
                        base_dir=plugin_root,
                    )
                    if config is not None:
                        configs.append(config)
        return configs

    def _load_local_configs(self) -> list[MCPServerConfig]:
        if not self._config_path.exists():
            logger.info("No local MCP config found at %s", self._config_path)
            return []

        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read %s: %s", self._config_path, exc)
            return []

        servers_data = data.get("mcpServers", {})
        if not isinstance(servers_data, Mapping):
            logger.error("%s must contain an object field named mcpServers", self._config_path)
            return []
        configs: list[MCPServerConfig] = []
        for index, (name, conf) in enumerate(servers_data.items()):
            config = self._config_from_mapping(
                str(name), conf, source="local", priority=1000 + index,
                base_dir=self._config_path.parent,
            )
            if config is not None:
                configs.append(config)
        return configs

    def _config_from_mapping(
        self,
        name: str,
        raw_config: Any,
        *,
        source: str,
        priority: int,
        base_dir: Path,
    ) -> MCPServerConfig | None:
        if not isinstance(raw_config, Mapping) or raw_config.get("enabled", True) is False:
            return None
        conf = _resolve_mapping_placeholders(dict(raw_config))
        transport = str(conf.get("transport") or conf.get("type") or "").strip().lower()
        if transport in {"streamable-http", "streamablehttp"}:
            transport = "streamable_http"
        url = _optional_str(conf.get("url"))
        if not transport:
            transport = "streamable_http" if url else "stdio"
        # An HTTP-style MCP server cannot be connected without an endpoint.
        # This also makes an unresolved environment placeholder fail closed,
        # matching the config-file validator instead of registering a dead
        # server that can never connect.
        if transport in {"http", "streamable_http", "websocket"} and not url:
            logger.warning("MCP server '%s' from %s has no URL; skipping", name, source)
            return None
        if url:
            scheme = urlparse(url).scheme.lower()
            allowed_schemes = {"ws", "wss"} if transport == "websocket" else {"http", "https"}
            if transport in {"http", "streamable_http", "websocket"} and scheme not in allowed_schemes:
                logger.warning(
                    "MCP server '%s' from %s has invalid %s URL scheme %r; skipping",
                    name,
                    source,
                    transport,
                    scheme or "(missing)",
                )
                return None
        command = str(conf.get("command") or ("python" if transport == "stdio" else ""))
        raw_args = conf.get("args")
        args = [str(arg) for arg in raw_args] if isinstance(raw_args, list) else []
        env = {
            str(key): str(value)
            for key, value in dict(conf.get("env") or {}).items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
        env_vars = conf.get("env_vars")
        if isinstance(env_vars, list):
            for item in env_vars:
                if isinstance(item, str):
                    target, source_name = item, item
                elif isinstance(item, Mapping):
                    target = str(item.get("name") or "").strip()
                    source_name = str(item.get("source") or target).strip()
                else:
                    continue
                if target and source_name and source_name in os.environ:
                    env[target] = os.environ[source_name]
        cwd = _resolve_mcp_cwd(base_dir, conf.get("cwd"))
        config = MCPServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            transport=transport,
            url=url,
            auto_start=bool(conf.get("autoStart", conf.get("auto_start", True))),
            max_retries=_safe_int(conf.get("maxRetries", conf.get("max_retries")), default=3),
            source=source,
            priority=priority,
            requires_user_action=bool(conf.get("requiresUserAction", False)),
            setup_hint=str(conf.get("setupHint") or ""),
            docs_url=str(conf.get("docsUrl") or ""),
        )
        try:
            validate_mcp_server_config(config)
        except ValueError as exc:
            logger.warning("MCP server '%s' from %s blocked: %s", name, source, exc)
            return None
        return config

    def _load_openmcp_configs(self) -> list[MCPServerConfig]:
        if not self._openmcp_config_path.exists():
            logger.info("No external MCP config found at %s", self._openmcp_config_path)
            return []

        try:
            data = json.loads(self._openmcp_config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read %s: %s", self._openmcp_config_path, exc)
            return []

        items = data.get("items", [])
        configs: list[MCPServerConfig] = []
        for index, raw_item in enumerate(items):
            item = dict(raw_item or {})
            nested = item.get("server")
            if isinstance(nested, dict):
                merged = dict(nested)
                merged.update({k: v for k, v in item.items() if k != "server"})
                item = merged

            resolved_item = _resolve_mapping_placeholders(item)
            name = str(
                resolved_item.get("name")
                or resolved_item.get("id")
                or resolved_item.get("server_name")
                or resolved_item.get("provider")
                or ""
            ).strip()
            if not name:
                continue

            if not resolved_item.get("url") and resolved_item.get("endpoint"):
                resolved_item["url"] = resolved_item.get("endpoint")
            priority = _safe_int(resolved_item.get("priority"), default=index)
            config = self._config_from_mapping(
                name,
                resolved_item,
                source="external",
                priority=priority,
                base_dir=self._openmcp_config_path.parent,
            )
            if config is not None:
                configs.append(config)

        configs.sort(key=lambda config: (config.priority, config.name))
        return configs

    async def start_all(self) -> None:
        tasks = [
            self.start_server(config)
            for config in self.load_config()
            if config.auto_start
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reload_config(self) -> None:
        """Reconcile running servers with the effective config catalog."""
        desired = {config.name: config for config in self.load_config()}
        for name in list(self._servers):
            if name not in desired:
                await self.remove_server(name)
        for name, config in desired.items():
            current = self._servers.get(name)
            changed = current is None or not _same_runtime_config(current.config, config)
            if changed:
                if current is not None:
                    await self.stop_server(name)
                if config.auto_start:
                    await self.start_server(config)
                else:
                    await self.register_config(config)
            elif config.auto_start and current.status == ServerStatus.OFFLINE:
                await self.start_server(config)

    async def start_server(
        self,
        config: MCPServerConfig,
        *,
        interactive_oauth: bool = False,
        force: bool = False,
    ) -> None:
        # A server owns exactly one connect/OAuth attempt. Codex exposes OAuth
        # as an explicit login operation; config reloads must not start another
        # browser flow while one is already in progress.
        lock = self._connection_locks.setdefault(config.name, asyncio.Lock())
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
        state = await self._prepare_state(config)
        state.status = ServerStatus.OFFLINE
        state.tools = []
        state.last_error = ""
        state.last_exception = None
        await self._notify_status(config.name, ServerStatus.OFFLINE)

    async def stop_server(self, name: str) -> None:
        state = self._servers.get(name)
        if state is None:
            return

        health_task = self._health_tasks.pop(name, None)
        if health_task and not health_task.done():
            health_task.cancel()

        if state.client:
            await state.client.close()

        state.status = ServerStatus.OFFLINE
        state.tools = []
        state.retry_count = 0
        state.last_error = ""
        state.last_exception = None
        await self._notify_status(name, ServerStatus.OFFLINE)

    async def remove_server(self, name: str) -> None:
        await self.stop_server(name)
        self._servers.pop(name, None)
        self._connection_locks.pop(name, None)
        await self._notify_status(name, ServerStatus.OFFLINE)

    async def stop_all(self) -> None:
        tasks = [self.stop_server(name) for name in list(self._servers.keys())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def restart_server(self, name: str) -> None:
        state = self._servers.get(name)
        if state is None:
            return
        await self.stop_server(name)
        await asyncio.sleep(1)
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

    def _start_health_check(self, name: str) -> None:
        old_task = self._health_tasks.pop(name, None)
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(self._health_check_loop(name), name=f"mcp-health-{name}")
        self._health_tasks[name] = task

    async def _health_check_loop(self, name: str) -> None:
        try:
            while True:
                await _ORIGINAL_ASYNCIO_SLEEP(_HEALTH_CHECK_INTERVAL_SECONDS)
                state = self._servers.get(name)
                if not state or state.status != ServerStatus.CONNECTED:
                    return
                if not state.client or not state.client.connected:
                    await self._try_reconnect(name)
                    return
        except asyncio.CancelledError:
            return

    async def _try_reconnect(self, name: str) -> None:
        lock = self._connection_locks.setdefault(name, asyncio.Lock())
        async with lock:
            state = self._servers.get(name)
            if state is None:
                return

            if state.retry_count >= state.config.max_retries:
                state.status = ServerStatus.ERROR
                state.last_error = f"Reconnect failed after {state.config.max_retries} attempts"
                await self._notify_status(name, ServerStatus.ERROR)
                return

            state.retry_count += 1
            state.status = ServerStatus.RECONNECTING
            await self._notify_status(name, ServerStatus.RECONNECTING)
            await asyncio.sleep(2 ** state.retry_count)
            await self._attempt_connection(name, state)

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
            connect_timeout = 310.0 if interactive_oauth else 15.0
            await asyncio.wait_for(client.connect(), timeout=connect_timeout)
            state.tools = await client.list_tools()
            state.status = ServerStatus.CONNECTED
            state.retry_count = 0
            state.last_error = ""
            state.last_exception = None
            await self._notify_status(name, ServerStatus.CONNECTED)
            self._start_health_check(name)
        except Exception as exc:
            try:
                await client.close()
            except Exception:
                logger.debug("Failed to close MCP client after start failure", exc_info=True)
            state.client = None
            state.status = ServerStatus.ERROR
            state.last_exception = exc
            state.tools = []
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            logger.error("Failed to start MCP server '%s': %s", name, state.last_error)
            await self._notify_status(name, ServerStatus.ERROR)

    async def _prepare_state(self, config: MCPServerConfig) -> MCPServerState:
        validate_mcp_server_config(config)
        state = self._servers.get(config.name)
        if state is None:
            state = MCPServerState(config=config)
            self._servers[config.name] = state
            return state

        config_changed = not _same_runtime_config(state.config, config)
        state.config = config
        if config_changed and state.client is not None:
            try:
                await state.client.close()
            except Exception:
                pass
            state.client = None
        return state

    def _create_client(
        self,
        config: MCPServerConfig,
        *,
        interactive_oauth: bool = False,
    ) -> MCPClient:
        t = config.transport.lower()
        if t == "http":
            transport = MCPTransport.HTTP
        elif t == "websocket":
            transport = MCPTransport.WEBSOCKET
        elif t == "streamable_http":
            transport = MCPTransport.STREAMABLE_HTTP
        elif t == "in_process":
            transport = MCPTransport.IN_PROCESS
        else:
            transport = MCPTransport.STDIO
        return MCPClient(
            server_name=config.name,
            command=config.command,
            args=config.args,
            env=config.env,
            cwd=config.cwd,
            transport=transport,
            url=config.url,
            timeout=20.0,
            token_store=self._token_store if transport in (MCPTransport.HTTP, MCPTransport.STREAMABLE_HTTP) else None,
            interactive_oauth=interactive_oauth,
            sampling_handler=self._sampling_handler,
            elicitation_handler=self._elicitation_handler,
            on_disconnect=self._handle_client_disconnect,
        )

    async def _handle_client_disconnect(self, name: str) -> None:
        state = self._servers.get(name)
        if state is None or state.status != ServerStatus.CONNECTED:
            return
        await self._try_reconnect(name)

    async def _notify_status(self, name: str, status: ServerStatus) -> None:
        self._registry_version += 1
        if self._on_status_change:
            try:
                await self._on_status_change(name, status)
            except Exception as exc:
                logger.error("Status callback failed: %s", exc)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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

    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")

    def replace(match: re.Match[str]) -> str:
        return os.getenv(match.group(1), "")

    return pattern.sub(replace, value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _same_runtime_config(left: MCPServerConfig, right: MCPServerConfig) -> bool:
    return (
        left.command == right.command
        and left.args == right.args
        and left.env == right.env
        and left.cwd == right.cwd
        and left.transport == right.transport
        and left.url == right.url
        and left.auto_start == right.auto_start
        and left.max_retries == right.max_retries
        and left.source == right.source
        and left.requires_user_action == right.requires_user_action
        and left.setup_hint == right.setup_hint
        and left.docs_url == right.docs_url
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


def _resolve_mcp_cwd(base_dir: Path, value: Any) -> str | None:
    if value is None or not str(value).strip():
        return str(base_dir.resolve())
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())
