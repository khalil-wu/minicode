"""
MCP Server 生命周期管理（DESIGN.md §六）。

职责：
  - 从 .mcp.json 读取所有 Server 配置
  - 启动 / 停止 / 重启 MCP Server
  - 健康检查（定期 ping）
  - 连接状态通知（前端侧边栏联动）
  - 错误恢复（自动重连，最多 3 次）

.mcp.json 格式（与 Claude Code 兼容）：
  {
    "mcpServers": {
      "websearch": {
        "command": "python",
        "args": ["-m", "backend.mcp.servers.websearch"],
        "env": {}
      }
    }
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable

from backend.config import PROJECT_ROOT
from backend.mcp.client import MCPClient, MCPToolDef, MCPTransport

logger = logging.getLogger(__name__)

MCP_CONFIG_FILE = PROJECT_ROOT / ".mcp.json"


class ServerStatus(Enum):
    """Server 连接状态。"""
    OFFLINE = "offline"
    STARTING = "starting"
    CONNECTED = "connected"
    ERROR = "error"
    RECONNECTING = "reconnecting"


@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置。"""
    name: str
    command: str = "python"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    url: str | None = None
    auto_start: bool = True
    max_retries: int = 3


@dataclass
class MCPServerState:
    """MCP Server 的运行时状态。"""
    config: MCPServerConfig
    client: MCPClient | None = None
    status: ServerStatus = ServerStatus.OFFLINE
    tools: list[MCPToolDef] = field(default_factory=list)
    retry_count: int = 0
    last_error: str = ""

    def to_status_dict(self) -> dict[str, Any]:
        """转换为前端可用的状态字典。"""
        return {
            "name": self.config.name,
            "status": self.status.value,
            "tools_count": len(self.tools),
            "error": self.last_error if self.status == ServerStatus.ERROR else "",
        }


class MCPServerManager:
    """
    MCP Server 生命周期管理器。

    使用示例：
        manager = MCPServerManager()
        await manager.start_all()        # 启动所有配置的 Server
        tools = manager.get_all_tools()  # 获取所有 Server 提供的工具
        await manager.stop_all()         # 停止所有 Server
    """

    def __init__(
        self,
        config_path: Path | None = None,
        on_status_change: Callable[[str, ServerStatus], Awaitable[None]] | None = None,
    ) -> None:
        """
        初始化管理器。

        Args:
            config_path: .mcp.json 路径（默认项目根目录）
            on_status_change: 状态变更回调（用于推送前端）
        """
        self._config_path = config_path or MCP_CONFIG_FILE
        self._servers: dict[str, MCPServerState] = {}
        self._on_status_change = on_status_change
        self._health_tasks: dict[str, asyncio.Task] = {}

    def load_config(self) -> list[MCPServerConfig]:
        """
        从 .mcp.json 加载配置。

        格式兼容 Claude Code 的 .mcp.json。
        """
        if not self._config_path.exists():
            logger.info("未找到 %s，跳过 MCP Server 加载", self._config_path)
            return []

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("读取 .mcp.json 失败: %s", exc)
            return []

        servers_data = data.get("mcpServers", {})
        configs: list[MCPServerConfig] = []

        for name, conf in servers_data.items():
            configs.append(MCPServerConfig(
                name=name,
                command=conf.get("command", "python"),
                args=conf.get("args", []),
                env=conf.get("env", {}),
                transport=conf.get("transport", "stdio"),
                url=conf.get("url"),
                auto_start=conf.get("autoStart", True),
                max_retries=conf.get("maxRetries", 3),
            ))

        logger.info("从 .mcp.json 加载了 %d 个 Server 配置", len(configs))
        return configs

    async def start_all(self) -> None:
        """启动所有配置为 auto_start 的 Server。"""
        configs = self.load_config()

        tasks = []
        for config in configs:
            if config.auto_start:
                tasks.append(self.start_server(config))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def start_server(self, config: MCPServerConfig) -> None:
        """
        启动单个 MCP Server。

        流程：
          1. 创建 MCPClient
          2. 连接（启动子进程 + 握手）
          3. 获取工具列表
          4. 更新状态
          5. 启动健康检查
        """
        name = config.name
        logger.info("[MCPManager] 启动 Server: %s", name)

        # 更新状态
        state = MCPServerState(config=config, status=ServerStatus.STARTING)
        self._servers[name] = state
        await self._notify_status(name, ServerStatus.STARTING)

        # 创建客户端
        transport = MCPTransport.HTTP if config.transport == "http" else MCPTransport.STDIO
        client = MCPClient(
            server_name=name,
            command=config.command,
            args=config.args,
            env=config.env,
            transport=transport,
            url=config.url,
            timeout=30.0,
        )
        state.client = client

        try:
            # 连接
            await client.connect()

            # 获取工具列表
            tools = await client.list_tools()
            state.tools = tools
            state.status = ServerStatus.CONNECTED
            state.retry_count = 0
            state.last_error = ""

            await self._notify_status(name, ServerStatus.CONNECTED)
            logger.info(
                "[MCPManager] Server '%s' 已连接, %d 个工具",
                name, len(tools),
            )

            # 启动健康检查
            self._start_health_check(name)

        except Exception as exc:
            error_msg = str(exc)
            logger.error("[MCPManager] Server '%s' 启动失败: %s", name, error_msg)
            state.status = ServerStatus.ERROR
            state.last_error = error_msg
            await self._notify_status(name, ServerStatus.ERROR)

    async def stop_server(self, name: str) -> None:
        """停止单个 Server。"""
        state = self._servers.get(name)
        if not state:
            return

        # 取消健康检查
        health_task = self._health_tasks.pop(name, None)
        if health_task and not health_task.done():
            health_task.cancel()

        # 关闭客户端
        if state.client:
            await state.client.close()

        state.status = ServerStatus.OFFLINE
        state.tools = []
        await self._notify_status(name, ServerStatus.OFFLINE)
        logger.info("[MCPManager] Server '%s' 已停止", name)

    async def stop_all(self) -> None:
        """停止所有 Server。"""
        tasks = [self.stop_server(name) for name in list(self._servers.keys())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def restart_server(self, name: str) -> None:
        """重启单个 Server。"""
        state = self._servers.get(name)
        if not state:
            return

        await self.stop_server(name)
        await asyncio.sleep(1)
        await self.start_server(state.config)

    # ── 工具查询 ──────────────────────────────────────

    def get_all_tools(self) -> dict[str, list[MCPToolDef]]:
        """
        获取所有已连接 Server 的工具列表。

        Returns:
            {server_name: [MCPToolDef, ...]}
        """
        result: dict[str, list[MCPToolDef]] = {}
        for name, state in self._servers.items():
            if state.status == ServerStatus.CONNECTED and state.tools:
                result[name] = state.tools
        return result

    def get_client(self, server_name: str) -> MCPClient | None:
        """获取指定 Server 的客户端实例。"""
        state = self._servers.get(server_name)
        if state and state.status == ServerStatus.CONNECTED:
            return state.client
        return None

    def get_all_status(self) -> list[dict[str, Any]]:
        """获取所有 Server 的状态（用于前端侧边栏）。"""
        return [state.to_status_dict() for state in self._servers.values()]

    @property
    def connected_count(self) -> int:
        return sum(
            1 for s in self._servers.values()
            if s.status == ServerStatus.CONNECTED
        )

    # ── 健康检查 ──────────────────────────────────────

    def _start_health_check(self, name: str) -> None:
        """启动后台健康检查任务。"""
        # 取消旧任务
        old = self._health_tasks.pop(name, None)
        if old and not old.done():
            old.cancel()

        task = asyncio.create_task(
            self._health_check_loop(name),
            name=f"mcp-health-{name}",
        )
        self._health_tasks[name] = task

    async def _health_check_loop(self, name: str) -> None:
        """
        定期检查 Server 健康状态。

        策略：
          - 每 60 秒尝试 list_tools()
          - 失败时标记 ERROR，尝试重连
          - 重连失败 3 次后放弃
        """
        try:
            while True:
                await asyncio.sleep(60)

                state = self._servers.get(name)
                if not state or state.status != ServerStatus.CONNECTED:
                    break

                if not state.client or not state.client.connected:
                    # 连接断开，尝试重连
                    await self._try_reconnect(name)
                    break

        except asyncio.CancelledError:
            pass

    async def _try_reconnect(self, name: str) -> None:
        """尝试重连 Server。"""
        state = self._servers.get(name)
        if not state:
            return

        if state.retry_count >= state.config.max_retries:
            logger.warning(
                "[MCPManager] Server '%s' 重连次数已达上限 (%d)",
                name, state.config.max_retries,
            )
            state.status = ServerStatus.ERROR
            state.last_error = f"重连 {state.config.max_retries} 次失败"
            await self._notify_status(name, ServerStatus.ERROR)
            return

        state.retry_count += 1
        state.status = ServerStatus.RECONNECTING
        await self._notify_status(name, ServerStatus.RECONNECTING)

        logger.info(
            "[MCPManager] 重连 Server '%s' (第 %d/%d 次)",
            name, state.retry_count, state.config.max_retries,
        )

        await asyncio.sleep(2 ** state.retry_count)  # 指数退避
        await self.start_server(state.config)

    async def _notify_status(self, name: str, status: ServerStatus) -> None:
        """通知状态变更。"""
        if self._on_status_change:
            try:
                await self._on_status_change(name, status)
            except Exception as exc:
                logger.error("状态回调异常: %s", exc)
