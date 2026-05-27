from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

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


MCP_CONFIG_FILE = PROJECT_ROOT / ".mcp.json"
OPENMCP_CONFIG_FILE = PROJECT_ROOT / ".openmcp" / "connection.json"
MCP_CIRCUIT_BREAKER_THRESHOLD = 5
_HEALTH_CHECK_INTERVAL_SECONDS = 60.0
_ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep


class ServerStatus(Enum):
    OFFLINE = "offline"
    STARTING = "starting"
    CONNECTED = "connected"
    ERROR = "error"
    RECONNECTING = "reconnecting"


@dataclass
class MCPServerConfig:
    name: str
    command: str = "python"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    url: str | None = None
    auto_start: bool = True
    max_retries: int = 3
    source: str = "local"
    priority: int = 1000


@dataclass
class MCPServerState:
    config: MCPServerConfig
    client: MCPClient | None = None
    status: ServerStatus = ServerStatus.OFFLINE
    tools: list[MCPToolDef] = field(default_factory=list)
    retry_count: int = 0
    last_error: str = ""
    consecutive_failures: int = 0
    circuit_open: bool = False

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "status": self.status.value,
            "tools_count": len(self.tools),
            "error": self.last_error if self.status == ServerStatus.ERROR else "",
            "source": self.config.source,
            "priority": self.config.priority,
            "transport": self.config.transport,
        }


class MCPServerManager:
    def __init__(
        self,
        config_path: Path | None = None,
        on_status_change: Callable[[str, ServerStatus], Awaitable[None]] | None = None,
        openmcp_config_path: Path | None = None,
    ) -> None:
        self._config_path = config_path or MCP_CONFIG_FILE
        self._openmcp_config_path = openmcp_config_path or OPENMCP_CONFIG_FILE
        self._servers: dict[str, MCPServerState] = {}
        self._on_status_change = on_status_change
        self._health_tasks: dict[str, asyncio.Task[Any]] = {}

    def load_config(self) -> list[MCPServerConfig]:
        external_configs = self._load_openmcp_configs()
        local_configs = self._load_local_configs()

        merged: dict[str, MCPServerConfig] = {}
        for config in external_configs:
            merged[config.name] = config
        for config in local_configs:
            merged.setdefault(config.name, config)

        return list(merged.values())

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
        configs: list[MCPServerConfig] = []
        for index, (name, conf) in enumerate(servers_data.items()):
            command = conf.get("command", "python")
            args = list(conf.get("args", []))
            if not _is_safe_mcp_command(command):
                logger.warning(
                    "MCP server '%s' blocked: command '%s' not in allowlist", name, command
                )
                continue
            if _has_shell_injection(args):
                logger.warning(
                    "MCP server '%s' blocked: args contain shell metacharacters", name
                )
                continue
            resolved_env = {
                str(key): _resolve_env_placeholders(str(value))
                for key, value in dict(conf.get("env", {})).items()
            }
            configs.append(
                MCPServerConfig(
                    name=name,
                    command=command,
                    args=args,
                    env=resolved_env,
                    transport=conf.get("transport", "stdio"),
                    url=_optional_str(_resolve_env_placeholders(conf.get("url"))),
                    auto_start=conf.get("autoStart", True),
                    max_retries=conf.get("maxRetries", 3),
                    source="local",
                    priority=1000 + index,
                )
            )
        return configs

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

            if item.get("enabled", True) is False:
                continue

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

            transport = str(resolved_item.get("transport") or "").strip().lower()
            url = resolved_item.get("url") or resolved_item.get("endpoint")
            if not transport:
                transport = "http" if url else "stdio"
            if transport == "http" and not _optional_str(url):
                continue

            priority = _safe_int(resolved_item.get("priority"), default=index)
            configs.append(
                MCPServerConfig(
                    name=name,
                    command=str(resolved_item.get("command") or "python"),
                    args=[str(arg) for arg in list(resolved_item.get("args", []))],
                    env={
                        str(key): str(value)
                        for key, value in dict(resolved_item.get("env", {})).items()
                    },
                    transport=transport,
                    url=_optional_str(url),
                    auto_start=bool(
                        resolved_item.get(
                            "autoStart", resolved_item.get("auto_start", True)
                        )
                    ),
                    max_retries=_safe_int(resolved_item.get("maxRetries"), default=3),
                    source="external",
                    priority=priority,
                )
            )

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

    async def start_server(self, config: MCPServerConfig) -> None:
        state = await self._prepare_state(config)
        if state.circuit_open:
            state.status = ServerStatus.ERROR
            if not state.last_error:
                state.last_error = self._build_circuit_error_message()
            await self._notify_status(config.name, ServerStatus.ERROR)
            return

        state.status = ServerStatus.STARTING
        await self._notify_status(config.name, ServerStatus.STARTING)
        await self._attempt_connection(config.name, state)

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
        state.consecutive_failures = 0
        state.circuit_open = False
        state.last_error = ""
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
        await self.start_server(state.config)

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

    def get_all_status(self) -> list[dict[str, Any]]:
        return [state.to_status_dict() for state in self._servers.values()]

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
        state = self._servers.get(name)
        if state is None:
            return

        if state.circuit_open:
            state.status = ServerStatus.ERROR
            if not state.last_error:
                state.last_error = self._build_circuit_error_message()
            await self._notify_status(name, ServerStatus.ERROR)
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

    async def _attempt_connection(self, name: str, state: MCPServerState) -> None:
        client = state.client or self._create_client(state.config)
        state.client = client

        try:
            await client.connect()
            state.tools = await client.list_tools()
            state.status = ServerStatus.CONNECTED
            state.retry_count = 0
            state.consecutive_failures = 0
            state.circuit_open = False
            state.last_error = ""
            await self._notify_status(name, ServerStatus.CONNECTED)
            self._start_health_check(name)
        except Exception as exc:
            state.status = ServerStatus.ERROR
            state.tools = []
            state.consecutive_failures += 1
            if state.consecutive_failures >= MCP_CIRCUIT_BREAKER_THRESHOLD:
                state.circuit_open = True
                state.last_error = (
                    f"{self._build_circuit_error_message()} ({exc.__class__.__name__}: {exc})"
                )
            else:
                state.last_error = f"{exc.__class__.__name__}: {exc}"
            logger.error("Failed to start MCP server '%s': %s", name, state.last_error)
            await self._notify_status(name, ServerStatus.ERROR)

    async def _prepare_state(self, config: MCPServerConfig) -> MCPServerState:
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

    def _create_client(self, config: MCPServerConfig) -> MCPClient:
        transport = (
            MCPTransport.HTTP if config.transport.lower() == "http" else MCPTransport.STDIO
        )
        return MCPClient(
            server_name=config.name,
            command=config.command,
            args=config.args,
            env=config.env,
            transport=transport,
            url=config.url,
            timeout=20.0,
        )

    @staticmethod
    def _build_circuit_error_message() -> str:
        return (
            f"Circuit open after {MCP_CIRCUIT_BREAKER_THRESHOLD} consecutive failures; "
            "manual recovery required"
        )

    async def _notify_status(self, name: str, status: ServerStatus) -> None:
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
        and left.transport == right.transport
        and left.url == right.url
    )
