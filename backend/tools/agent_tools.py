"""
Agent 辅助工具（DESIGN.md §8.2）。

  - ask_user:       主动向用户提问。权限: AUTO
  - read_artifact:  读取 artifact 全文。权限: AUTO
"""

from __future__ import annotations

from typing import Any

from backend.artifact.store import ArtifactStore
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


class AskUserTool(BaseTool):
    """
    Agent 主动向用户提问。

    当 Agent 缺少关键信息无法继续时使用。
    权限: AUTO
    """

    name = "ask_user"
    description = (
        "向用户提出一个问题以获取缺失的关键信息。"
        "当你缺少完成任务所需的关键决策或信息时使用此工具。"
        "示例: ask_user(question='你想使用 TypeScript 还是 JavaScript？')。"
        "注意: 不要用于不必要的确认，只在真正需要用户决策时使用。"
    )
    permission = PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要向用户提出的问题，应清晰明确",
                    },
                },
                "required": ["question"],
            },
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        question = args.get("question", "")
        if not question:
            return self._error_result("缺少 question 参数")

        # ask_user 的实际实现由 Agent Loop 层拦截处理
        # 这里只返回一个标记，Agent Loop 会将其转发给前端
        return self._success_result(
            f"[等待用户回答] {question}"
        )


class ReadArtifactTool(BaseTool):
    """
    读取 Artifact Store 中存储的完整内容。

    当工具返回了 artifact_id 引用时，Agent 可用此工具获取全文。
    权限: AUTO
    """

    name = "read_artifact"
    description = (
        "读取 artifact 的完整内容。"
        "当其他工具因输出过长而将内容存入 artifact 时，"
        "使用此工具获取完整内容。"
        "示例: read_artifact(artifact_id='art_a1b2c3d4')。"
        "注意: artifact_id 由其他工具返回，在当前会话内有效。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "artifact 的唯一标识符，如 'art_a1b2c3d4'",
                    },
                },
                "required": ["artifact_id"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        artifact_id = args.get("artifact_id", "")
        if not artifact_id:
            return self._error_result("缺少 artifact_id 参数")

        content = self._artifact_store.get(artifact_id)
        if content is None:
            available = self._artifact_store.list_artifacts()
            ids = [a.artifact_id for a in available]
            hint = f"可用的 artifact: {', '.join(ids)}" if ids else "当前没有可用的 artifact"
            return self._error_result(
                f"artifact '{artifact_id}' 不存在。{hint}"
            )

        return self._success_result(content)
