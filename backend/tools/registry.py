"""
工具注册中心（DESIGN.md §8.1）。

统一管理内置工具 + MCP 动态注册工具：
  - 注册 BaseTool 实例
  - 按 token 预算裁剪 schema 输出
  - 路由执行 + 统一返回 ToolResult
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import BaseTool, ToolResult, ToolSchema


class ToolRegistry:
    """
    工具注册中心。

    get_schemas(budget) 按重要性排序，控制 schema 总 token 数。
    工具数量超过 15 个时，自动切换到摘要模式。
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册一个工具。"""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """注销一个工具（MCP 动态工具断开时使用）。"""
        return self._tools.pop(name, None) is not None

    def has_tool(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    def get_tool(self, name: str) -> BaseTool | None:
        """获取指定工具实例。"""
        return self._tools.get(name)

    def get_schemas(self, budget: int = 6000) -> list[dict[str, Any]]:
        """
        返回工具 JSON Schema 列表（OpenAI function-calling 格式）。

        按重要性排序，控制总 token 数不超过 budget。
        超预算时，后续工具只输出 name + 一行描述。

        Args:
            budget: token 预算上限

        Returns:
            OpenAI tools 格式的列表
        """
        tools = list(self._tools.values())
        schemas: list[dict[str, Any]] = []
        estimated_tokens = 0

        for tool in tools:
            schema = tool.get_schema()
            full_schema = schema.to_openai_tool()

            # 粗略估算 schema 的 token 数（约 4 字符 = 1 token）
            schema_tokens = len(str(full_schema)) // 4

            if estimated_tokens + schema_tokens <= budget:
                schemas.append(full_schema)
                estimated_tokens += schema_tokens
            else:
                # 超预算：只给 name + 一行描述
                summary_schema = {
                    "type": "function",
                    "function": {
                        "name": schema.name,
                        "description": schema.description.split(".")[0],
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
                schemas.append(summary_schema)
                estimated_tokens += 20  # 摘要 schema 约 20 tokens

        return schemas

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        """
        执行指定工具。

        异常处理：工具自身必须捕获异常返回 ToolResult(is_error=True)。
        这里做最外层兜底。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                content=f"工具 '{name}' 不存在。可用工具: {', '.join(self._tools.keys())}",
                is_error=True,
            )

        try:
            return await tool.execute(args)
        except Exception as exc:
            return ToolResult(
                content=(
                    f"工具 '{name}' 执行异常: {exc}\n"
                    f"建议: 检查参数是否正确，或尝试其他方式完成任务。"
                ),
                is_error=True,
            )

    def list_tools(self) -> list[str]:
        """列出所有已注册工具名。"""
        return list(self._tools.keys())

    @property
    def count(self) -> int:
        return len(self._tools)
