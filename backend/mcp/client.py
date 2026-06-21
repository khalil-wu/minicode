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
import subprocess
import threading
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


def _fix_mcp_subprocess_env(env: dict[str, str]) -> dict[str, str]:
    """Repair PYTHONPATH so the installed MCP SDK is not shadowed.

    MiniCode's ``backend/mcp/`` package has the same top-level name as the
    installed ``mcp`` SDK.  When PYTHONPATH or CWD includes the *backend*
    directory, ``import mcp`` resolves to ``backend/mcp/__init__.py`` instead
    of the SDK — breaking FastMCP-based servers like docparse and memory-rag.

    The fix: (1) strip PYTHONPATH entries that point to the backend directory,
    (2) replace them with the project root so ``backend.*`` imports still work,
    (3) insert the real MCP SDK's site-packages directory at the front of
    PYTHONPATH so it takes absolute precedence.
    """
    import os
    import site
    from backend.config import PROJECT_ROOT

    old_paths = env.get("PYTHONPATH", "")
    backend_dir = str(PROJECT_ROOT / "backend")
    project_root = str(PROJECT_ROOT)

    # Collect all site-packages directories (virtual-env + user + system).
    sdk_paths: list[str] = []
    for sp in site.getsitepackages():
        sdk_paths.append(sp)
    user_site = site.getusersitepackages()
    if user_site:
        sdk_paths.append(user_site)

    new_parts: list[str] = []
    # Site-packages first — ensures mcp SDK is found before any local package.
    for sp in sdk_paths:
        abs_sp = os.path.abspath(sp)
        if abs_sp not in new_parts:
            new_parts.append(abs_sp)

    for entry in (old_paths.split(os.pathsep) if old_paths else []):
        entry = entry.strip()
        if not entry:
            continue
        abs_entry = os.path.abspath(entry)
        # Drop entries that point to the backend dir (they shadow the SDK).
        if abs_entry.lower() == os.path.abspath(backend_dir).lower():
            continue
        if abs_entry not in new_parts:
            new_parts.append(abs_entry)

    # Always include the project root so backend.* imports resolve.
    abs_root = os.path.abspath(project_root)
    if abs_root not in new_parts:
        new_parts.append(abs_root)

    if new_parts:
        env["PYTHONPATH"] = os.pathsep.join(new_parts)
    return env


def _resolve_npm_command_to_node(
    command: str, args: list[str],
) -> tuple[str, list[str]] | None:
    """Resolve an npx/npm invocation to a direct ``node`` call.

    On Windows, ``npx`` and ``npm`` are ``.cmd`` batch scripts that execute
    through ``cmd.exe``.  ``cmd.exe`` buffers stdin/stdout, which breaks the
    MCP JSON-RPC protocol: the initialize request is sent, but the response
    never reaches the parent process.

    By finding the globally-installed package entry point and invoking it
    with ``node`` directly, we bypass the ``cmd.exe`` wrapper and get
    unbuffered, immediate I/O — exactly what MCP stdio transport requires.

    Returns ``(node_executable, new_args)`` on success, or ``None`` if the
    package cannot be resolved (fallback to original command).
    """
    import json
    import os
    import shutil

    # Only applies to npx/npm with -y and a package name.
    if command not in ("npx", "npm"):
        return None

    # Find node executable.
    node_path = shutil.which("node")
    if not node_path:
        logger.debug("Cannot resolve npx → node: node not found in PATH")
        return None

    # Extract the npm package name from args.
    # Common patterns:
    #   npx -y @scope/package         → args = ["-y", "@scope/package"]
    #   npx @scope/package             → args = ["@scope/package"]
    #   npm exec @scope/package        → args = ["exec", "@scope/package"]
    package_name: str | None = None
    filtered_args: list[str] = []
    for arg in args:
        if arg in ("-y", "--yes", "-q", "--quiet"):
            filtered_args.append(arg)  # keep for reference but skip
            continue
        if arg in ("exec", "--"):
            filtered_args.append(arg)
            continue
        # First positional arg that looks like a package name.
        if arg.startswith("@") or (arg and not arg.startswith("-")):
            package_name = arg
            break

    if not package_name:
        return None

    # Locate the globally-installed package.
    # On Windows, npm installs global packages under %APPDATA%\npm\node_modules,
    # not next to the Node.js installation (D:\Nodejs\node_modules).
    # We need to search multiple possible locations.
    candidate_prefixes: list[str] = []
    # 1. %APPDATA%\npm  (default npm global prefix on Windows)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidate_prefixes.append(os.path.join(appdata, "npm", "node_modules"))
    # 2. Next to npm executable (Linux/macOS typical)
    npm_bin = shutil.which("npm")
    if npm_bin:
        candidate_prefixes.append(
            os.path.join(os.path.dirname(npm_bin), "node_modules"),
        )
    # 3. ~/.npm-global/node_modules (custom npm prefix)
    home = os.environ.get("USERPROFILE", "") or os.environ.get("HOME", "")
    if home:
        candidate_prefixes.append(
            os.path.join(home, ".npm-global", "node_modules"),
        )
    # 4. /usr/local/lib/node_modules (Linux system install)
    candidate_prefixes.append("/usr/local/lib/node_modules")
    # 5. /usr/lib/node_modules (Linux system-wide)
    candidate_prefixes.append("/usr/lib/node_modules")

    # Find the first prefix that contains the target package.
    package_dir: str | None = None
    for prefix in candidate_prefixes:
        candidate = os.path.join(prefix, package_name)
        if os.path.isdir(candidate):
            package_dir = candidate
            break

    if not package_dir:
        logger.debug(
            "Cannot resolve npx → node: package '%s' not found in any global prefix",
            package_name,
        )
        return None

    # Read package.json to find the bin entry point.
    pkg_json_path = os.path.join(package_dir, "package.json")
    if not os.path.isfile(pkg_json_path):
        return None

    try:
        with open(pkg_json_path, encoding="utf-8") as f:
            pkg_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # Resolve bin entry.  bin can be a string or a dict.
    bin_field = pkg_data.get("bin")
    entry_path: str | None = None

    if isinstance(bin_field, str):
        entry_path = bin_field
    elif isinstance(bin_field, dict):
        # Pick the first entry (or match the package bin name).
        for _bin_name, bin_path in bin_field.items():
            entry_path = bin_path
            break

    if not entry_path:
        # Try main as fallback.
        entry_path = pkg_data.get("main")

    if not entry_path:
        return None

    entry_abs = os.path.normpath(os.path.join(package_dir, entry_path))
    if not os.path.isfile(entry_abs):
        logger.debug("Entry point not found: %s", entry_abs)
        return None

    return (node_path, [entry_abs])

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
    # MCP tool annotations (readOnlyHint/destructiveHint/openWorldHint/title…)
    # and _meta (e.g. anthropic/alwaysLoad). Used to derive local capability hints.
    annotations: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


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


@dataclass(frozen=True)
class _RpcError:
    message: str
    code: str = "?"

    def to_text(self) -> str:
        return f"MCP RPC error: {self.message} (code={self.code})"


def _rpc_error_from_payload(error: Any) -> _RpcError:
    if isinstance(error, dict):
        return _RpcError(
            message=str(error.get("message", "unknown") or "unknown"),
            code=str(error.get("code", "?") or "?"),
        )
    return _RpcError(message=str(error or "unknown"), code="?")


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
        token_store: Any = None,
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
            token_store: 可选的 OAuth TokenStore（HTTP 模式注入 Bearer token）
        """
        self.server_name = server_name
        self._command = command
        self._args = args or []
        self._env = env or {}
        self._transport = transport
        self._url = url
        self._http_endpoint = (url or "").strip() or None
        self._timeout = timeout
        # OAuth: load any persisted bearer token for this server.
        self._token_store = token_store
        self._tokens = token_store.get(server_name) if token_store is not None else None

        # 运行时状态
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False
        self._server_capabilities = MCPServerCapabilities()
        self._server_info: dict[str, Any] = {}
        self._instructions: str = ""

        # 请求-响应关联
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sync_process: subprocess.Popen[bytes] | None = None
        self._sync_stdout_thread: threading.Thread | None = None
        self._sync_stderr_thread: threading.Thread | None = None

    # ── OAuth token management ──────────────────────────────
    def set_tokens(self, tokens: Any) -> None:
        """Store OAuth tokens after the manager completes authorization."""
        self._tokens = tokens
        if self._token_store is not None and tokens is not None:
            self._token_store.set(self.server_name, tokens)

    def clear_tokens(self) -> None:
        """Drop cached OAuth tokens (e.g. after a final 401)."""
        self._tokens = None
        if self._token_store is not None:
            self._token_store.clear(self.server_name)

    @property
    def has_valid_token(self) -> bool:
        return self._tokens is not None and not self._tokens.is_expired()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_capabilities(self) -> MCPServerCapabilities:
        return self._server_capabilities

    @property
    def instructions(self) -> str:
        """Server-declared usage instructions from the initialize result."""
        return self._instructions

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
        # 构建环境变量，并修复 PYTHONPATH 阴影冲突：
        # MiniCode 的 backend/mcp 包会遮蔽已安装的 MCP SDK，
        # 导致子进程 import mcp 时优先找到 MiniCode 的版本而非 SDK。
        # 解决方案：在子进程环境中将 PYTHONPATH 从 backend 目录改为项目根目录，
        # 这样 backend.mcp.* 仍然可导入，而 import mcp 则找到 SDK。
        env = _fix_mcp_subprocess_env(sanitized_subprocess_env(self._env))

        # 确保在使用正确的 Python 环境（如虚拟环境）中启动
        # 并将 npx 命令解析为直接 node 执行（避免 Windows 上 .cmd 批处理
        # 脚本的 I/O 缓冲问题导致 MCP 握手失败）。
        cmd = self._command
        args = list(self._args)

        if cmd == "python":
            import sys
            cmd = sys.executable
        elif cmd == "npx" or cmd == "npm":
            # Windows: npx.CMD / npm.CMD 是批处理脚本，由 cmd.exe 执行，
            # 这会缓冲 stdin/stdout 导致 MCP JSON-RPC 握手永远无法完成。
            # 解决方案：找到已安装的包入口脚本，直接用 node 运行。
            resolved_cmd = _resolve_npm_command_to_node(cmd, args)
            if resolved_cmd:
                cmd, args = resolved_cmd
                logger.info(
                    "[MCP:%s] 将 npx/npm 命令解析为直接 node 执行: %s %s",
                    self.server_name, cmd, " ".join(args),
                )
            else:
                # 如果无法解析，仍尝试原始 npx/npm 命令
                import shutil
                resolved = shutil.which(cmd)
                if resolved:
                    cmd = resolved
        else:
            import shutil
            resolved = shutil.which(cmd)
            if resolved:
                cmd = resolved

        logger.info(
            "[MCP:%s] 启动子进程: %s %s",
            self.server_name, cmd, " ".join(args),
        )

        self._loop = asyncio.get_running_loop()

        # Set CWD to project root to prevent ``backend/mcp`` shadowing the SDK.
        # When CWD is the backend dir, Python adds it to sys.path and ``import mcp``
        # finds MiniCode's ``backend/mcp/__init__.py`` before the installed SDK.
        from backend.config import PROJECT_ROOT as _PROJECT_ROOT
        subprocess_cwd = str(_PROJECT_ROOT)

        try:
            self._process = await asyncio.create_subprocess_exec(
                cmd, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=subprocess_cwd,
                # Windows 上需要隐藏子进程窗口
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except NotImplementedError:
            logger.warning(
                "[MCP:%s] asyncio subprocess is unavailable on this event loop; "
                "falling back to threaded stdio",
                self.server_name,
            )
            self._connect_stdio_threaded(cmd, env)
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
        if self._process is not None:
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
        if self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.5)
                # 进程已退出 — 握手不可能成功，快速失败
                exit_code = self._process.returncode
                raise ConnectionError(
                    f"MCP Server '{self.server_name}' 启动后立即退出 (exit={exit_code})，"
                    "请检查依赖是否已安装（如 pip install 'mcp[cli]'）"
                )
            except asyncio.TimeoutError:
                # 进程仍在运行，继续握手
                pass
        elif self._sync_process is not None:
            await asyncio.sleep(0.5)
            exit_code = self._sync_process.poll()
            if exit_code is not None:
                raise ConnectionError(
                    f"MCP Server '{self.server_name}' exited immediately after start "
                    f"(exit={exit_code}). Check whether its dependencies are installed."
                )

        # 执行 MCP 握手
        await self._handshake()

    def _connect_stdio_threaded(self, cmd: str, env: dict[str, str]) -> None:
        """Fallback stdio transport for event loops without subprocess support."""
        from backend.config import PROJECT_ROOT as _PROJECT_ROOT
        self._sync_process = subprocess.Popen(
            [cmd, *self._args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(_PROJECT_ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._sync_stdout_thread = threading.Thread(
            target=self._read_sync_stdout_loop,
            name=f"mcp-sync-reader-{self.server_name}",
            daemon=True,
        )
        self._sync_stdout_thread.start()
        self._sync_stderr_thread = threading.Thread(
            target=self._read_sync_stderr_loop,
            name=f"mcp-sync-stderr-{self.server_name}",
            daemon=True,
        )
        self._sync_stderr_thread.start()

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
        self._http_endpoint = self._url.strip()
        self._http_client = httpx.AsyncClient(timeout=self._timeout)

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
            self._instructions = str(init_result.get("instructions", "") or "")
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

        if not init_result:
                raise ConnectionError(
                f"MCP server '{self.server_name}' HTTP initialize returned no result"
            )

        if init_result:
            caps = init_result.get("capabilities", {})
            self._server_capabilities = MCPServerCapabilities(
                tools="tools" in caps,
                resources="resources" in caps,
                prompts="prompts" in caps,
                logging="logging" in caps,
            )
            self._server_info = init_result.get("serverInfo", {})
            self._instructions = str(init_result.get("instructions", "") or "")

        await self._send_http_notification("notifications/initialized")
        self._connected = True
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
                annotations=t.get("annotations", {}) or {},
                meta=t.get("_meta", {}) or {},
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
        """Call one MCP tool and normalize the response."""
        if not self._connected:
            return MCPCallResult(
                content=[{"type": "text", "text": f"MCP server '{self.server_name}' is not connected"}],
                is_error=True,
            )

        params = {
            "name": tool_name,
            "arguments": arguments or {},
        }
        result = await self._send_tool_call_request("tools/call", params)
        if isinstance(result, _RpcError):
            return MCPCallResult(
                content=[{"type": "text", "text": result.to_text()}],
                is_error=True,
            )
        if result is None and tool_name in RETRIABLE_TIMEOUT_TOOLS:
            logger.warning("[MCP:%s] Tool call timed out, retrying once: %s", self.server_name, tool_name)
            result = await self._send_tool_call_request("tools/call", params)
            if isinstance(result, _RpcError):
                return MCPCallResult(
                    content=[{"type": "text", "text": result.to_text()}],
                    is_error=True,
                )

        if result is None:
            return MCPCallResult(
                content=[{"type": "text", "text": "Tool call timed out"}],
                is_error=True,
            )

        return MCPCallResult(
            content=result.get("content", []),
            is_error=result.get("isError", False),
        )

    async def _send_tool_call_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | _RpcError | None:
        raw_helper_overridden = type(self)._send_request_auto_raw is not MCPClient._send_request_auto_raw
        legacy_helper_overridden = type(self)._send_request_auto is not MCPClient._send_request_auto
        if legacy_helper_overridden and not raw_helper_overridden:
            return await self._send_request_auto(method, params)
        return await self._send_request_auto_raw(method, params)

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

        if self._sync_process:
            process = self._sync_process
            try:
                if process.stdin:
                    process.stdin.close()
                process.terminate()
                await asyncio.to_thread(process.wait, 5.0)
            except subprocess.TimeoutExpired:
                process.kill()
            except (ProcessLookupError, OSError, ValueError):
                pass
            finally:
                self._sync_process = None
                self._sync_stdout_thread = None
                self._sync_stderr_thread = None

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
        result = await self._send_request_auto_raw(method, params)
        return result if isinstance(result, dict) else None

    async def _send_request_auto_raw(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | _RpcError | None:
        """Return raw JSON-RPC result/error for callers that need error details."""
        if self._transport == MCPTransport.HTTP:
            return await self._send_http_request_raw(method, params)
        return await self._send_request(method, params)

    async def _send_request(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        发送 JSON-RPC 请求并等待响应（stdio 模式）。

        使用 id 字段关联请求和响应。
        """
        if self._sync_process is not None:
            if not self._sync_process.stdin:
                return None
        elif not self._process or not self._process.stdin:
            return None

        req_id, payload = _JsonRpcHelper.request(method, params)

        # 创建 Future 用于等待响应
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[req_id] = future

        # 发送请求
        try:
            if self._sync_process is not None:
                await asyncio.to_thread(self._write_sync_stdin, payload)
            else:
                self._process.stdin.write(payload)
                await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError) as exc:
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
        if self._sync_process is not None:
            if not self._sync_process.stdin:
                return
        elif not self._process or not self._process.stdin:
            return

        payload = _JsonRpcHelper.notification(method, params)
        try:
            if self._sync_process is not None:
                await asyncio.to_thread(self._write_sync_stdin, payload)
            else:
                self._process.stdin.write(payload)
                await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError) as exc:
            logger.error("[MCP:%s] 通知发送失败: %s", self.server_name, exc)

    def _write_sync_stdin(self, payload: bytes) -> None:
        process = self._sync_process
        if process is None or process.stdin is None:
            raise BrokenPipeError("sync MCP process stdin is not available")
        process.stdin.write(payload)
        process.stdin.flush()

    async def _send_http_request(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Backward-compatible HTTP request helper returning only RPC results."""
        result = await self._send_http_request_raw(method, params)
        return result if isinstance(result, dict) else None

    async def _send_http_request_raw(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | _RpcError | None:
        """HTTP 模式发送请求。"""
        if not hasattr(self, "_http_client"):
            return None

        req_id, payload_bytes = _JsonRpcHelper.request(method, params)
        try:
            payload = json.loads(payload_bytes.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("[MCP:%s] Failed to decode request payload: %s", self.server_name, exc)
            return _RpcError(code=-32700, message="Parse error in request payload")

        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            # OAuth: attach a persisted bearer token if we have one.
            if self._tokens and self._tokens.access_token:
                headers["Authorization"] = self._tokens.authorization_header()
            resp = await self._http_client.post(
                self._http_request_url(),
                json=payload,
                headers=headers,
            )
            # 401 → token missing/expired: surface a needs-auth signal so the
            # manager can refresh or prompt for authorization, rather than
            # silently treating it as a generic failure.
            if resp.status_code == 401:
                logger.warning("[MCP:%s] HTTP 401 — OAuth token missing or expired", self.server_name)
                return _RpcError(code=-32001, message="authentication required")
            resp.raise_for_status()
            return self._parse_http_rpc_result(resp, req_id)
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
        try:
            payload = json.loads(payload_bytes.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("[MCP:%s] Failed to decode notification payload: %s", self.server_name, exc)
            return

        try:
            await self._http_client.post(
                self._http_request_url(),
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        except Exception as exc:
            logger.error("[MCP:%s] HTTP 通知失败: %s", self.server_name, exc)

    # ── stdout 读取循环 ───────────────────────────────

    def _http_request_url(self) -> str:
        endpoint = self._http_endpoint or self._url
        if not endpoint:
            raise ConnectionError("HTTP MCP transport requires a url")
        return endpoint

    def _parse_http_rpc_result(self, resp: Any, req_id: int) -> dict[str, Any] | _RpcError | None:
        data = self._parse_http_response_message(resp, expected_id=req_id)
        if data is None:
            return None
        if data.get("id") not in (None, req_id):
            return None
        if "error" in data:
            error = data.get("error") or {}
            if isinstance(error, dict):
                message = error.get("message", "unknown")
                code = error.get("code", "?")
            else:
                message = str(error)
                code = "?"
            logger.warning(
                "[MCP:%s] HTTP RPC error: %s (code=%s)",
                self.server_name,
                message,
                code,
            )
            return _RpcError(message=str(message), code=str(code))
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def _parse_http_response_message(
        self,
        resp: Any,
        *,
        expected_id: int | None = None,
    ) -> dict[str, Any] | None:
        headers = getattr(resp, "headers", {}) or {}
        content_type = ""
        if hasattr(headers, "get"):
            content_type = str(headers.get("content-type", "") or "").lower()

        if "text/event-stream" in content_type:
            return self._parse_sse_json_rpc_message(
                str(getattr(resp, "text", "") or ""),
                expected_id=expected_id,
            )

        try:
            data = resp.json()
        except Exception:
            return self._parse_sse_json_rpc_message(
                str(getattr(resp, "text", "") or ""),
                expected_id=expected_id,
            )
        return data if isinstance(data, dict) else None

    def _parse_sse_json_rpc_message(
        self,
        text: str,
        *,
        expected_id: int | None = None,
    ) -> dict[str, Any] | None:
        for frame in text.replace("\r\n", "\n").split("\n\n"):
            data_lines: list[str] = []
            for raw_line in frame.splitlines():
                line = raw_line.strip()
                if line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").strip())
            if not data_lines:
                continue
            try:
                message = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                logger.debug("[MCP:%s] Ignoring malformed SSE frame", self.server_name)
                continue
            if not isinstance(message, dict):
                continue
            if expected_id is not None and message.get("id") != expected_id:
                continue
            if "result" in message or "error" in message:
                return message
        return None

    def _read_sync_stdout_loop(self) -> None:
        process = self._sync_process
        if process is None or process.stdout is None:
            return
        reader = process.stdout
        try:
            while True:
                line = reader.readline()
                if not line:
                    self._call_soon_threadsafe(self._mark_disconnected_and_cancel_pending)
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                if line_str.startswith("Content-Length:"):
                    try:
                        content_length = int(line_str.split(":", 1)[1].strip())
                    except ValueError:
                        continue
                    reader.readline()
                    body = reader.read(content_length)
                    line_str = body.decode("utf-8", errors="replace")

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug("[MCP:%s] non-JSON stdout: %s", self.server_name, line_str[:100])
                    continue

                if isinstance(msg, dict):
                    self._call_soon_threadsafe(self._dispatch_message, msg)
        except Exception as exc:
            logger.error("[MCP:%s] threaded stdout read failed: %s", self.server_name, exc)
            self._call_soon_threadsafe(self._mark_disconnected_and_cancel_pending)

    def _read_sync_stderr_loop(self) -> None:
        process = self._sync_process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("[MCP:%s:stderr] %s", self.server_name, text)
        except Exception as exc:
            logger.debug("[MCP:%s] threaded stderr read failed: %s", self.server_name, exc)

    def _call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(callback, *args)

    def _mark_disconnected_and_cancel_pending(self) -> None:
        self._connected = False
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

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
