"""
Skill 注入 Context 执行器（DESIGN.md §3.2 动态注入）。

将激活的 Skill 内容注入 Context 的 cinstr 部分。

职责：
  - 将 SkillManager 的 Layer 2 内容拼入 system prompt
  - 管理 token 预算分配（active_skills 预算默认 4K）
  - 多个 Skill 时按优先级裁剪
  - 注入格式化，确保 LLM 能正确理解 Skill 边界
"""

from __future__ import annotations

import logging
from typing import Any

from backend.skills.manager import SkillManager

logger = logging.getLogger(__name__)

# Skill 注入模板
SKILL_SECTION_HEADER = """
## 激活的 Skills

以下 Skills 定义了你在当前会话中的特殊行为模式。
严格遵守每个 Skill 的指令。如果 Skill 之间有冲突，以最后激活的为准。
"""


class SkillExecutor:
    """
    将 Skills 内容注入 Context。

    使用示例：
        executor = SkillExecutor(skill_manager)
        cinstr_addition = executor.build_skill_context(budget=4000)
        # 将 cinstr_addition 拼接到 system prompt
    """

    def __init__(self, skill_manager: SkillManager) -> None:
        self._manager = skill_manager

    def build_skill_context(self, budget: int = 4000) -> str:
        """
        构建 Skill 注入内容。

        Args:
            budget: Skill 内容的 token 预算上限

        Returns:
            需要拼入 system prompt 的 Skill 文本。
            空字符串表示无激活的 Skill。
        """
        active_names = self._manager.get_active_names()
        if not active_names:
            return ""

        content = self._manager.get_active_content(budget=budget)
        if not content:
            return ""

        return SKILL_SECTION_HEADER + content

    def build_layer1_summary(self) -> str:
        """
        构建 Layer 1 摘要（始终注入）。

        让 LLM 知道有哪些 Skill 可用（即使未激活），
        便于推荐用户激活或 auto_detect 使用。

        Returns:
            Skill 摘要列表文本
        """
        summary = self._manager.get_layer1_summary()
        if not summary:
            return ""

        return (
            "\n\n## Available Skills\n"
            "When the user's request matches a skill description below, "
            "use load_skill to activate it automatically — no need to ask the user first. "
            "The user can also invoke skills explicitly via /skill-name.\n\n"
            + summary
        )

    def get_injection_stats(self) -> dict[str, Any]:
        """获取注入统计（供 Context Budget 面板使用）。"""
        active = self._manager.get_active_names()
        content = self._manager.get_active_content()
        return {
            "active_count": len(active),
            "active_names": active,
            "estimated_tokens": len(content) // 4 if content else 0,
        }
