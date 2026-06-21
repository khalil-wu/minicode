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

# Codex-style progressive disclosure: the always-on skill index must stay small
# so selecting one skill can still load its full SKILL.md later.
LAYER1_SUMMARY_MAX_CHARS = 8000

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

    def build_layer1_summary(self, max_chars: int = LAYER1_SUMMARY_MAX_CHARS) -> str:
        """
        构建 Layer 1 摘要（始终注入）。

        让 LLM 知道有哪些 Skill 可用（即使未激活），
        便于推荐用户激活或 auto_detect 使用。

        Args:
            max_chars: Layer 1 metadata budget. Defaults to the Codex-like
                cap of 8000 chars (roughly 2k tokens).

        Returns:
            Skill 摘要列表文本
        """
        summary = self._manager.get_layer1_summary()
        if not summary:
            return ""

        summary = _cap_layer1_summary(summary, max_chars=max_chars)
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


def _cap_layer1_summary(summary: str, *, max_chars: int) -> str:
    """Cap the always-injected skill index without splitting mid-entry."""
    if max_chars <= 0:
        return ""
    if len(summary) <= max_chars:
        return summary

    notice_template = "\n- ... {omitted} more skill entries omitted by context budget; use list_skills/load_skill if needed."
    lines = [line for line in summary.splitlines() if line.strip()]
    kept: list[str] = []
    used = 0
    omitted = 0

    for index, line in enumerate(lines):
        remaining_after_this = len(lines) - index - 1
        notice = notice_template.format(omitted=max(1, remaining_after_this))
        line_len = len(line) + (1 if kept else 0)
        if used + line_len + len(notice) > max_chars:
            omitted = len(lines) - index
            break
        kept.append(line)
        used += line_len

    if kept:
        notice = notice_template.format(omitted=omitted or max(1, len(lines) - len(kept)))
        capped = "\n".join(kept) + notice
        return capped[:max_chars]

    fallback_notice = notice_template.format(omitted=max(1, len(lines))).lstrip()
    head_budget = max(0, max_chars - len(fallback_notice) - 1)
    return f"{summary[:head_budget].rstrip()}\n{fallback_notice}"[:max_chars]
