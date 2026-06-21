"""
Skill 工具（DESIGN.md §5 / §8.2）。

  - load_skill:   激活一个 Skill，注入其指令到 context。权限: AUTO
  - unload_skill: 停用一个 Skill，从 context 中移除。权限: AUTO
  - list_skills:  列出所有可用 Skill 及其状态。权限: AUTO
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.skills.names import normalize_skill_name
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext

logger = logging.getLogger(__name__)


class LoadSkillTool(BaseTool):
    """
    激活一个 Skill。

    将 Skill 的 SKILL.md 指令注入到 Agent 的 active context 中。
    权限: AUTO
    """

    name = "load_skill"
    description = (
        "Activate a Skill and inject its SKILL.md workflow instructions into the current context.\n\n"
        "When to use: the user explicitly names a skill, slash command, or $skill; or an available skill name/"
        "description clearly matches the task. Matching skills are a blocking requirement: call load_skill "
        "before substantive guidance, implementation, or claiming you are using that skill.\n\n"
        "When not to use: no listed/known skill matches, the task is better handled by directly available tools, "
        "or the skill is already active in context.\n\n"
        "Never mention a skill as used or available for the task unless it has been activated or is already active. "
        "Use list_skills only when discovery is needed."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        """懒获取 SkillManager（避免循环导入）。"""
        if self._skill_manager is not None:
            return self._skill_manager
        # 延迟从全局获取
        try:
            from backend.main import _skill_manager
            return _skill_manager
        except ImportError:
            return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["skill_name"],
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": (
                            "Exact skill name to activate, for example 'code_review', 'debugging', or 'refactor'. "
                            "Use when that skill was explicitly requested or clearly matches the task."
                        ),
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        skill_name = normalize_skill_name(args.get("skill_name"))
        if not skill_name:
            return self._error_result("缺少 skill_name 参数")

        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")

        success = manager.activate(skill_name)
        if success:
            active_names = manager.get_active_names()
            return self._success_result(
                content=f"Skill '{skill_name}' 已激活。当前活跃 Skills: {', '.join(active_names)}"
            )
        else:
            return self._error_result(f"Skill '{skill_name}' 激活失败，可能不存在")


class UnloadSkillTool(BaseTool):
    """
    停用一个 Skill。

    从 Agent 的 active context 中移除 Skill 指令。
    权限: AUTO
    """

    name = "unload_skill"
    description = (
        "Deactivate an already active Skill and remove its workflow instructions from the current context. "
        "Use only when the user asks to stop using a skill or when the active skill is no longer relevant."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        if self._skill_manager is not None:
            return self._skill_manager
        try:
            from backend.main import _skill_manager
            return _skill_manager
        except ImportError:
            return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["skill_name"],
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要停用的 Skill 名称",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        skill_name = normalize_skill_name(args.get("skill_name"))
        if not skill_name:
            return self._error_result("缺少 skill_name 参数")

        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")

        success = manager.deactivate(skill_name)
        if success:
            active_names = manager.get_active_names()
            return self._success_result(
                content=f"Skill '{skill_name}' 已停用。当前活跃 Skills: {', '.join(active_names) or '(无)'}"
            )
        else:
            return self._error_result(f"Skill '{skill_name}' 未激活，无需停用")


class ListSkillsTool(BaseTool):
    """
    列出所有可用 Skill 及其状态。

    权限: AUTO
    """

    name = "list_skills"
    description = (
        "List available Skills, their descriptions, and active state for discovery. "
        "Use this when you need to decide whether a matching skill exists or avoid duplicate activation. "
        "If a skill clearly matches after discovery, call load_skill before doing substantive task work."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        if self._skill_manager is not None:
            return self._skill_manager
        try:
            from backend.main import _skill_manager
            return _skill_manager
        except ImportError:
            return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": [],
                "properties": {},
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")

        skills = manager.list_all()
        if not skills:
            return self._success_result(content="暂无可用 Skill")

        lines = ["可用 Skills:"]
        for s in skills:
            status = "✅ 已激活" if s.get("active") else "⬜ 未激活"
            lines.append(f"  {status} {s['name']}: {s.get('description', '无描述')}")

        return self._success_result(content="\n".join(lines))
