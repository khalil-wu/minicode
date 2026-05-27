"""
MCP 客户端（DESIGN.md §六）。

与 MCP Server 通信的核心组件，支持两种传输方式：
  - stdio:    本地子进程，通过 stdin/stdout 管道通信（JSON-RPC 2.0）
  - HTTP SSE: 远程服务，Streamable HTTP（POST + Server-Sent Events）

协议核心：JSON-RPC 2.0
  - 请求: {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
  - 响应: {"jsonrpc":"2.0","id":1,"result":{...}}
  - 通知: {"jsonrpc":"2.0","method":"notifications/initialized"}

生命周期：
  1. initialize()  — 能力协商（双方交换 capabilities + protocolVersion）
  2. list_tools()   — 获取 Server 提供的工具列表
  3. call_tool()    — 调用具体工具
  4. close()        — 优雅关闭连接

设计原则（来自 design_principle.md §三-(3)）：
  - Token-efficient: MCP 工具返回也遵从"摘要+artifact"模式
  - Robust: 完善超时、重连、错误隔离
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.runtime_env import sanitized_subprocess_env

logger = logging.getLogger(__name__)
RETRIABLE_TIMEOUT_TOOLS = {
    "search",
    "fetch_page",
    "mcp__websearch__search",
    "mcp__websearch__fetch_page",
}

# ── 数据类型 ────────────────────────────────────────────────


class MCPTransport(Enum):
    """传输方式。"""
    STDIO = "stdio"
    HTTP = "http"


@dataclass
class MCPToolDef:
    """MCP Server 暴露的工具定义。"""
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPResourceDef:
    """MCP Server 暴露的资源定义。"""
    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"


@dataclass
class MCPCallResult:
    """工具调用结果。"""
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    @property
    def text(self) -> str:
        """提取纯文本内容。"""
        parts = []
        for item in self.content:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)


@dataclass
class MCPServerCapabilities:
    """Server 能力声明。"""
    tools: bool = False
    resources: bool = False
    prompts: bool = False
    logging: bool = False


# ── JSON-RPC 辅助 ─────────────────────────────────────────


class _JsonRpcHelper:
    """JSON-RPC 2.0 请求/响应构建。"""

    _id_counter = itertools.count(1)

    @classmethod
    def request(cls, method: str, params: dict[str, Any] | None = None) -> tuple[int, bytes]:
        """构建 JSON-RPC 请求，返回 (id, 编码后的字节)。"""
        req_id = next(cls._id_counter)
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        payload = json.dumps(msg, ensure_ascii=False)
        return req_id, (payload + "\n").encode("utf-8")

    @classmethod
    def notification(cls, method: str, params: dict[str, Any] | None = None) -> bytes:
        """构建 JSON-RPC 通知（无 id，不期望响应）。"""
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        payload = json.dumps(msg, ensure_ascii=False)
        return (payload + "\n").encode("utf-8")


# ── MCP 客户端 ─────────────────────────────────────────────


class MCPClient:
    """
    MCP 客户端 — 与 MCP Server 通信的核心。

    使用示例：
        client = MCPClient(
            server_name="websearch",
            command="python",
            args=["-m", "backend.mcp.servers.websearch"],
        )
        await client.connect()
        tools = await client.list_tools()
        result = await client.call_tool("search", {"query": "Python MCP"})
        await client.close()
    """

    # MCP 协议版本
    PROTOCOL_VERSION = "2025-03-26"

    # 客户端能力声明
    CLIENT_CAPABILITIES: dict[str, Any] = {
        "roots": {"listChanged": True},
    }

    # 客户端信息
    CLIENT_INFO = {
        "name": "MiniCode",
        "version": "0.2.0",
    }

    def __init__(
        self,
        server_name: str,
        command: str = "python",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        transport: MCPTransport = MCPTransport.STDIO,
        url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        初始化 MCP 客户端。

        Args:
            server_name: Server 名称（用于日志和命名空间）
            command: 启动命令（stdio 模式）
            args: 启动参数（stdio 模式）
            env: 环境变量覆盖（stdio 模式）
            transport: 传输方式
            url: HTTP SSE 端点（HTTP 模式）
            timeout: 请求超时（秒）
        """
        self.server_name = server_name
        self._command = command
        self._args = args or []
        self._env = env or {}
        self._transport = transport
        self._url = url
        self._timeout = timeout

        # 运行时状态
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False
        self._server_capabilities = MCPServerCapabilities()
        self._server_info: dict[str, Any] = {}

        # 请求-响应关联
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_capabilities(self) -> MCPServerCapabilities:
        return self._server_capabilities

    # ── 连接生命周期 ──────────────────────────────────

    async def connect(self) -> None:
        """
        连接到 MCP Server。

        完整握手流程：
          1. 启动子进程
          2. 发送 initialize 请求（交换 capabilities）
          3. 发送 initialized 通知（完成握手）
        """
        if self._transport == MCPTransport.STDIO:
            await self._connect_stdio()
        else:
            await self._connect_http()

    async def _connect_stdio(self) -> None:
        """stdio 模式连接。"""
        # 构建环境变量
        env = sanitized_subprocess_env(self._env)

        # 确保在使用正确的 Python 环境（如虚拟环境）中启动
        cmd = self._command
        if cmd == "python":
            import sys
            cmd = sys.executable
        else:
            import shutil
            resolved = shutil.which(cmd)
            if resolved:
                cmd = resolved

        logger.info(
            "[MCP:%s] 启动子进程: %s %s",
            self.server_name, cmd, " ".join(self._args),
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                cmd, *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # Windows 上需要隐藏子进程窗口
                creationflags=getattr(asyncio.subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            raise ConnectionError(
                f"MCP Server '{self.server_name}' 启动失败: "
                f"命令 '{self._command}' 不存在"
            )
        except OSError as exc:
            raise ConnectionError(
                f"MCP Server '{self.server_name}' 启动失败: {exc}"
            )

        # 启动 stdout 读取协程
        self._reader_task = asyncio.create_task(
            self._read_stdout_loop(),
            name=f"mcp-reader-{self.server_name}",
        )

        # 启动 stderr 日志协程
        self._stderr_task = asyncio.create_task(
            self._read_stderr_loop(),
            name=f"mcp-stderr-{self.server_name}",
        )

        # 短暂等待，检测子进程是否在启动阶段立即退出
        try:
            await asyncio.wait_for(
                self._process.wait(),
                timeout=0.5,
            )
            # 进程已退出 — 握手不可能成功，快速失败
            exit_code = self._process.returncode
            raise ConnectionError(
                f"MCP Server '{self.server_name}' 启动后立即退出 (exit={exit_code})，"
                "请检查依赖是否已安装（如 pip install 'mcp[cli]'）"
            )
        except asyncio.TimeoutError:
            # 进程仍在运行，继续握手
            pass

        # 执行 MCP 握手
        await self._handshake()

    async def _connect_http(self) -> None:
        """HTTP SSE 模式连接（Phase 2 扩展）。"""
        # HTTP SSE 传输的完整实现
        if not self._url:
            raise ConnectionError("HTTP 模式需要指定 url")

        try:
            import httpx
        except ImportError:
            raise ConnectionError(
                "HTTP SSE 模式需要 httpx: pip install httpx[http2] httpx-sse"
            )

        # 对 HTTP 模式，不需要子进程，直接通过 HTTP 请求通信
        logger.info("[MCP:%s] HTTP SSE 连接: %s", self.server_name, self._url)
        self._http_client = httpx.AsyncClient(base_url=self._url, timeout=self._timeout)
        self._connected = True

        # HTTP 模式下的 initialize
        await self._handshake_http()

    async def _handshake(self) -> None:
        """
        MCP 协议握手（DESIGN.md §六 连接流程）。

        Step 1: 发送 initialize 请求
        Step 2: 接收 Server capabilities
        Step 3: 发送 initialized 通知
        """
        init_result = await self._send_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": self.CLIENT_CAPABILITIES,
            "clientInfo": self.CLIENT_INFO,
        })

        # 解析 Server 能力
        if init_result:
            caps = init_result.get("capabilities", {})
            self._server_capabilities = MCPServerCapabilities(
                tools="tools" in caps,
                resources="resources" in caps,
                prompts="prompts" in caps,
                logging="logging" in caps,
            )
            self._server_info = init_result.get("serverInfo", {})
            protocol = init_result.get("protocolVersion", "unknown")
            logger.info(
                "[MCP:%s] 握手成功 — 协议: %s, 工具: %s, 资源: %s",
                self.server_name, protocol,
                self._server_capabilities.tools,
                self._server_capabilities.resources,
            )

        # 发送 initialized 通知
        await self._send_notification("notifications/initialized")
        self._connected = True

    async def _handshake_http(self) -> None:
        """HTTP 模式的握手。"""
        init_result = await self._send_http_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": self.CLIENT_CAPABILITIES,
            "clientInfo": self.CLIENT_INFO,
        })

        if init_result:
            caps = init_result.get("capabilities", {})
            self._server_capabilities = MCPServerCapabilities(
                tools="tools" in caps,
                resources="resources" in caps,
                prompts="prompts" in caps,
                logging="logging" in caps,
            )
            self._server_info = init_result.get("serverInfo", {})

        await self._send_http_notification("notifications/initialized")
        logger.info("[MCP:%s] HTTP 握手成功", self.server_name)

    # ── MCP 协议方法 ──────────────────────────────────

    async def list_tools(self) -> list[MCPToolDef]:
        """
        获取 Server 提供的工具列表。

        Returns:
            MCPToolDef 列表，每个包含 name、description、input_schema
        """
        if not self._connected:
            return []

        result = await self._send_request_auto("tools/list")
        tools_data = result.get("tools", []) if result else []

        tools = []
        for t in tools_data:
            tools.append(MCPToolDef(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            ))

        logger.info(
            "[MCP:%s] 发现 %d 个工具: %s",
            self.server_name, len(tools),
            ", ".join(t.name for t in tools),
        )
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPCallResult:
        """
        调用 Server 的一个工具。

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            MCPCallResult 包含 content 列表和 is_error 标记
        """
        if not self._connected:
            return MCPCallResult(
                content=[{"type": "text", "text": f"MCP Server '{self.server_name}' 未连接"}],
                is_error=True,
            )

        params = {
            "name": tool_name,
            "arguments": arguments or {},
        }
        result = await self._send_request_auto("tools/call", params)
        if result is None and tool_name in RETRIABLE_TIMEOUT_TOOLS:
            logger.warning("[MCP:%s] 宸ュ叿璋冪敤瓒呮椂锛岄噸璇曚竴娆? %s", self.server_name, tool_name)
            result = await self._send_request_auto("tools/call", params)

        if result is None:
            return MCPCallResult(
                content=[{"type": "text", "text": "工具调用超时"}],
                is_error=True,
            )

        return MCPCallResult(
            content=result.get("content", []),
            is_error=result.get("isError", False),
        )

    async def list_resources(self) -> list[MCPResourceDef]:
        """获取 Server 提供的资源列表。"""
        if not self._connected or not self._server_capabilities.resources:
            return []

        result = await self._send_request_auto("resources/list")
        resources_data = result.get("resources", []) if result else []

        return [
            MCPResourceDef(
                uri=r.get("uri", ""),
                name=r.get("name", ""),
                description=r.get("description", ""),
                mime_type=r.get("mimeType", "text/plain"),
            )
            for r in resources_data
        ]

    async def read_resource(self, uri: str) -> str:
        """读取一个资源。"""
        if not self._connected:
            return ""

        result = await self._send_request_auto("resources/read", {"uri": uri})
        if result and "contents" in result:
            parts = []
            for c in result["contents"]:
                if "text" in c:
                    parts.append(c["text"])
            return "\n".join(parts)
        return ""

    async def close(self) -> None:
        """优雅关闭连接。"""
        self._connected = False

        # 取消读取任务
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        # 关闭子进程
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except (ProcessLookupError, OSError):
                    pass
            finally:
                self._process = None

        # 关闭 HTTP 客户端
        if hasattr(self, "_http_client"):
            await self._http_client.aclose()

        # 取消所有待处理请求
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

        logger.info("[MCP:%s] 连接已关闭", self.server_name)

    # ── 底层通信 ──────────────────────────────────────

    async def _send_request_auto(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """自动选择传输方式发送请求。"""
        if self._transport == MCPTransport.HTTP:
            return await self._send_http_request(method, params)
        return await self._send_request(method, params)

    async def _send_request(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        发送 JSON-RPC 请求并等待响应（stdio 模式）。

        使用 id 字段关联请求和响应。
        """
        if not self._process or not self._process.stdin:
            return None

        req_id, payload = _JsonRpcHelper.request(method, params)

        # 创建 Future 用于等待响应
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[req_id] = future

        # 发送请求
        try:
            self._process.stdin.write(payload)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(req_id, None)
            logger.error("[MCP:%s] 写入失败: %s", self.server_name, exc)
            self._connected = False
            return None

        # 等待响应
        try:
            result = await asyncio.wait_for(future, timeout=self._timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            logger.warning(
                "[MCP:%s] 请求超时: %s (%.1fs)",
                self.server_name, method, self._timeout,
            )
            return None

    async def _send_notification(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> None:
        """发送 JSON-RPC 通知（不期望响应）。"""
        if not self._process or not self._process.stdin:
            return

        payload = _JsonRpcHelper.notification(method, params)
        try:
            self._process.stdin.write(payload)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.error("[MCP:%s] 通知发送失败: %s", self.server_name, exc)

    async def _send_http_request(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """HTTP 模式发送请求。"""
        if not hasattr(self, "_http_client"):
            return None

        _, payload_bytes = _JsonRpcHelper.request(method, params)
        payload = json.loads(payload_bytes.decode("utf-8").strip())

        try:
            resp = await self._http_client.post(
                "/mcp",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("result")
        except Exception as exc:
            logger.error("[MCP:%s] HTTP 请求失败: %s", self.server_name, exc)
            return None

    async def _send_http_notification(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> None:
        """HTTP 模式发送通知。"""
        if not hasattr(self, "_http_client"):
            return

        payload_bytes = _JsonRpcHelper.notification(method, params)
        payload = json.loads(payload_bytes.decode("utf-8").strip())

        try:
            await self._http_client.post(
                "/mcp",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            logger.error("[MCP:%s] HTTP 通知失败: %s", self.server_name, exc)

    # ── stdout 读取循环 ───────────────────────────────

    async def _read_stdout_loop(self) -> None:
        """
        持续读取子进程 stdout，解析 JSON-RPC 响应。

        支持两种 framing：
          1. newline-delimited JSON（每行一个 JSON 对象）
          2. Content-Length header（标准 LSP 风格）
        """
        if not self._process or not self._process.stdout:
            return

        reader = self._process.stdout

        try:
            while True:
                line = await reader.readline()
                if not line:
                    # EOF — 子进程已退出，取消所有挂起请求让它们快速失败
                    self._connected = False
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.cancel()
                    self._pending.clear()
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                # 尝试解析 Content-Length header
                if line_str.startswith("Content-Length:"):
                    # LSP 风格 framing：读取空行 + body
                    content_length = int(line_str.split(":")[1].strip())
                    # 读取空行
                    await reader.readline()
                    # 读取 body
                    body = await reader.readexactly(content_length)
                    line_str = body.decode("utf-8", errors="replace")

                # 解析 JSON
                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug(
                        "[MCP:%s] 非 JSON 输出: %s",
                        self.server_name, line_str[:100],
                    )
                    continue

                self._dispatch_message(msg)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[MCP:%s] 读取异常: %s", self.server_name, exc)
            self._connected = False

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """
        分发收到的 JSON-RPC 消息。

        - 有 id 的：是对请求的响应，通过 Future 传递
        - 无 id 的：是 Server 主动发的通知
        """
        msg_id = msg.get("id")

        if msg_id is not None and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                if "error" in msg:
                    error = msg["error"]
                    logger.warning(
                        "[MCP:%s] RPC 错误: %s (code=%s)",
                        self.server_name,
                        error.get("message", "unknown"),
                        error.get("code", "?"),
                    )
                    future.set_result(None)
                else:
                    future.set_result(msg.get("result", {}))
        else:
            # 通知处理
            method = msg.get("method", "")
            if method.startswith("notifications/"):
                logger.debug(
                    "[MCP:%s] 收到通知: %s",
                    self.server_name, method,
                )

    async def _read_stderr_loop(self) -> None:
        """读取子进程 stderr 输出（用于调试日志）。"""
        if not self._process or not self._process.stderr:
            return

        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("[MCP:%s:stderr] %s", self.server_name, text)
        except asyncio.CancelledError:
            pass
