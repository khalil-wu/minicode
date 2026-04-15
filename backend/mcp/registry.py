"""
MCP 动态工具注册（DESIGN.md §六.3）。

将 MCP Server 暴露的工具动态代理注册到主 ToolRegistry，
对 Agent Loop 完全透明 — Agent 不需要知道工具来自 MCP。

命名规范：mcp__{server_name}__{tool_name}
    例如 websearch server 的 search 工具 → mcp__websearch__search

职责：
  - Server 连接后，为每个 MCP tool 创建 MCPToolProxy 代理
  - MCPToolProxy 实现 BaseTool 接口，execute() 转发到 MCPClient.call_tool()
  - 注册到主 ToolRegistry
  - Server 断开时自动注销对应工具

设计原则：
  - Token-efficient: MCP 工具返回也遵从 artifact 模式（长输出存 artifact）
  - 透明代理: Agent 无感知，调用方式与内置工具完全一致
"""

from __future__ import annotations

import logging
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPClient, MCPToolDef, MCPCallResult
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 工具输出超过此长度时存入 artifact
ARTIFACT_THRESHOLD = 2000  # 约 500 tokens


class MCPToolProxy(BaseTool):
    """
    MCP 工具代理。

    将 MCP Server 的工具包装为 BaseTool，使其能被 ToolRegistry 统一管理。
    execute() 调用时转发到 MCPClient.call_tool()。
    """

    def __init__(
        self,
        server_name: str,
        tool_def: MCPToolDef,
        client: MCPClient,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._server_name = server_name
        self._tool_def = tool_def
        self._client = client
        self._artifact_store = artifact_store

        # BaseTool 属性
        self.name = f"mcp__{server_name}__{tool_def.name}"
        self.description = tool_def.description
        self.permission = PermissionLevel.CONFIRM  # MCP 工具默认需要确认

    def get_schema(self) -> ToolSchema:
        """返回工具 JSON Schema。"""
        # 从 MCP inputSchema 构建
        params = self._tool_def.input_schema
        if not params:
            params = {"type": "object", "properties": {}}

        # 在描述中标注来源
        desc = (
            f"[MCP:{self._server_name}] {self._tool_def.description}\n"
            f"原始工具名: {self._tool_def.name}"
        )

        return ToolSchema(
            name=self.name,
            description=desc,
            parameters=params,
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        """
        执行 MCP 工具调用。

        流程：
          1. 通过 MCPClient.call_tool() 转发调用
          2. 解析结果
          3. 大输出存 artifact
          4. 返回 ToolResult
        """
        if not self._client.connected:
            return self._error_result(
                f"MCP Server '{self._server_name}' 未连接，"
                f"请检查 .mcp.json 配置或手动重启"
            )

        try:
            result: MCPCallResult = await self._client.call_tool(
                self._tool_def.name, args,
            )
        except Exception as exc:
            return self._error_result(
                f"MCP 工具 '{self._tool_def.name}' 调用失败: {exc}"
            )

        # 错误处理
        if result.is_error:
            return self._error_result(result.text or "MCP 工具执行失败")

        # 正常结果
        full_text = result.text

        # Token-efficient：大输出存 artifact
        if len(full_text) > ARTIFACT_THRESHOLD and self._artifact_store:
            artifact_id = self._artifact_store.save(
                content=full_text,
                source=self.name,
                type="mcp_result",
            )
            # 生成摘要
            lines = full_text.split("\n")
            preview = "\n".join(lines[:5])
            summary = (
                f"MCP {self._server_name}.{self._tool_def.name} 执行成功\n"
                f"返回 {len(full_text)} 字符（{len(lines)} 行）"
            )
            return self._success_result(
                content=summary,
                artifact_id=artifact_id,
                artifact_preview=preview,
            )

        return self._success_result(content=full_text)


class MCPToolRegistry:
    """
    MCP 动态工具注册管理器。

    使用示例：
        mcp_registry = MCPToolRegistry(tool_registry, artifact_store)
        # Server 连接后注册工具
        mcp_registry.register_server_tools("websearch", tools, client)
        # Server 断开时注销工具
        mcp_registry.unregister_server_tools("websearch")
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._artifact_store = artifact_store
        # 追踪每个 server 注册了哪些工具名
        self._server_tools: dict[str, list[str]] = {}

    def register_server_tools(
        self,
        server_name: str,
        tools: list[MCPToolDef],
        client: MCPClient,
    ) -> int:
        """
        为指定 Server 的所有工具创建代理并注册。

        Args:
            server_name: Server 名称
            tools: 工具定义列表
            client: MCPClient 实例

        Returns:
            注册的工具数量
        """
        # 先注销旧工具
        self.unregister_server_tools(server_name)

        registered_names: list[str] = []

        for tool_def in tools:
            proxy = MCPToolProxy(
                server_name=server_name,
                tool_def=tool_def,
                client=client,
                artifact_store=self._artifact_store,
            )

            self._tool_registry.register(proxy)
            registered_names.append(proxy.name)
            logger.info(
                "[MCPRegistry] 注册工具: %s (来自 %s)",
                proxy.name, server_name,
            )

        self._server_tools[server_name] = registered_names
        return len(registered_names)

    def unregister_server_tools(self, server_name: str) -> None:
        """
        注销指定 Server 的所有工具。

        在 Server 断开连接或重启时调用。
        """
        tool_names = self._server_tools.pop(server_name, [])
        for name in tool_names:
            self._tool_registry.unregister(name)
            logger.info("[MCPRegistry] 注销工具: %s", name)

    def get_server_tool_count(self, server_name: str) -> int:
        """获取指定 Server 的注册工具数量。"""
        return len(self._server_tools.get(server_name, []))

    def get_all_mcp_tools(self) -> dict[str, list[str]]:
        """获取所有 MCP 工具的名称映射。"""
        return dict(self._server_tools)
