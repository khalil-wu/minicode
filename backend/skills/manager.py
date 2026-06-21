"""
Skills 生命周期管理器（DESIGN.md §五.3）。

职责：
  - discover(): 扫描并加载所有 Skill 的 Layer 1
  - auto_detect(user_message): 关键词匹配 triggers，返回应激活的 Skill
  - activate(skill_name): 激活 Skill（加载 Layer 2）
  - deactivate(skill_name): 停用 Skill
  - 冲突检测: conflicts 字段声明的互斥 Skill 自动停用旧的
  - get_active_content(): 所有已激活 Skill 的 Layer 2 内容拼接

设计原则：
  - Skills = cinstr 层扩展，改变 Agent 的「思维方式」
  - MCP = cknow/ctools 层扩展，提供新工具和数据
  - 两者互补不重叠（DESIGN.md §13 Q1）
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.skills.loader import SkillLoader, SkillFull, SkillMeta

logger = logging.getLogger(__name__)


class SkillManager:
    """
    Skills 生命周期管理器。

    使用示例：
        manager = SkillManager()
        manager.discover()
        
        # 自动检测
        to_activate = manager.auto_detect("帮我写一个 React 组件")
        for name in to_activate:
            manager.activate(name)
        
        # 获取注入内容
        content = manager.get_active_content()
    """

    def __init__(self, loader: SkillLoader | None = None) -> None:
        self._loader = loader or SkillLoader()
        self._active: dict[str, SkillFull] = {}  # {name: SkillFull}
        self._discovered = False

    def discover(self) -> list[SkillMeta]:
        """
        扫描并发现所有可用的 Skills。

        Returns:
            所有 Skill 的元数据列表
        """
        skills = self._loader.discover()
        self._discovered = True
        return skills

    def auto_detect(self, user_message: str) -> list[str]:
        """
        根据用户消息自动检测应激活的 Skill（DESIGN.md §3.2 动态触发）。

        匹配逻辑：
          1. 将 user_message 转小写
          2. 遍历所有 Skill 的 triggers 字段
          3. 任一 trigger 关键词出现在消息中 → 加入候选
          4. 排除已激活的

        Args:
            user_message: 用户消息

        Returns:
            应该激活的 Skill 名称列表
        """
        if not self._discovered:
            self.discover()

        msg_lower = user_message.lower()
        candidates: list[str] = []

        all_skills = self._loader.list_skill_names()
        for name in all_skills:
            # 已激活的跳过
            if name in self._active:
                continue

            meta = self._loader.get_meta(name)
            if not meta:
                continue

            if _message_mentions_skill_name(msg_lower, name):
                candidates.append(name)
                continue

            for trigger in meta.triggers:
                if trigger.lower() in msg_lower:
                    candidates.append(name)
                    break

        return candidates

    def activate(self, skill_name: str) -> bool:
        """
        激活一个 Skill。

        流程：
          1. 加载 Layer 2 内容
          2. 冲突检测并自动停用冲突 Skill
          3. 加入 _active 字典

        Args:
            skill_name: Skill 名称

        Returns:
            是否成功激活
        """
        if not self._discovered:
            self.discover()

        # 已激活
        if skill_name in self._active:
            logger.debug("Skill '%s' 已经激活", skill_name)
            return True

        # 加载 Layer 2
        full = self._loader.load_full(skill_name)
        if not full:
            logger.warning("Skill '%s' 加载失败", skill_name)
            return False

        # 冲突检测
        self._resolve_conflicts(full.meta)

        # 激活
        self._active[skill_name] = full
        logger.info(
            "✅ Skill '%s' 已激活（~%d tokens）",
            skill_name, full.token_estimate,
        )
        return True

    def deactivate(self, skill_name: str) -> bool:
        """停用一个 Skill。"""
        removed = self._active.pop(skill_name, None)
        if removed:
            logger.info("❌ Skill '%s' 已停用", skill_name)
            return True
        return False

    def get_active_content(self, budget: int = 4000) -> str:
        """
        获取所有已激活 Skill 的 Layer 2 内容。

        按 token 预算裁剪：优先保留最近激活的 Skill。

        Args:
            budget: token 预算上限

        Returns:
            拼接的 Skill 内容
        """
        if not self._active:
            return ""

        parts: list[str] = []
        used = 0

        for name, full in reversed(list(self._active.items())):
            tokens = full.token_estimate
            if used + tokens > budget:
                # 预算不足，只放 Layer 1 摘要
                parts.append(f"[{name}: 已激活但因预算限制仅显示摘要]")
                parts.append(full.meta.to_layer1_summary())
                used += 30  # 摘要约 30 tokens
            else:
                parts.append(f"## Skill: {name}")
                parts.append(full.content)
                used += tokens

        return "\n\n".join(parts)

    def get_layer1_summary(self) -> str:
        """
        获取所有 Skill 的 Layer 1 摘要（始终注入 context）。

        让 LLM 知道有哪些 Skill 可用。
        """
        return self._loader.get_all_layer1()

    def get_active_names(self) -> list[str]:
        """获取已激活的 Skill 名称列表。"""
        return list(self._active.keys())

    def get_active_tools_required(self) -> list[str]:
        """获取已激活 Skill 需要的工具列表。"""
        tools: set[str] = set()
        for full in self._active.values():
            tools.update(full.meta.tools_required)
        return list(tools)

    def get_active_mcp_required(self) -> list[str]:
        """获取已激活 Skill 需要的 MCP Server 列表。"""
        mcps: set[str] = set()
        for full in self._active.values():
            mcps.update(full.meta.mcp_required)
        return list(mcps)

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有 Skill 及其状态（供前端展示）。"""
        if not self._discovered:
            self.discover()

        result: list[dict[str, Any]] = []
        for name in self._loader.list_skill_names():
            meta = self._loader.get_meta(name)
            if meta:
                result.append({
                    "name": meta.name,
                    "description": meta.description,
                    "active": name in self._active,
                    "triggers": meta.triggers,
                    "version": meta.version,
                    "level": meta.source_level,
                })
        return result

    # ── 冲突处理 ──────────────────────────────────────

    def _resolve_conflicts(self, new_meta: SkillMeta) -> None:
        """
        冲突检测与解决（DESIGN.md §13 Q5）。

        策略：优先保留新激活的 Skill，停用冲突的旧 Skill。
        """
        if not new_meta.conflicts:
            return

        for conflict_name in new_meta.conflicts:
            if conflict_name in self._active:
                logger.info(
                    "Skill 冲突: '%s' 与 '%s' 互斥，自动停用 '%s'",
                    new_meta.name, conflict_name, conflict_name,
                )
                self.deactivate(conflict_name)


def _message_mentions_skill_name(message: str, skill_name: str) -> bool:
    """Detect explicit $skill, /skill, or plain skill-name mentions."""
    normalized = skill_name.strip().lower()
    if not normalized:
        return False
    variants = {
        normalized,
        normalized.replace("_", "-"),
        normalized.replace("-", "_"),
    }
    for variant in variants:
        pattern = rf"(?<![\w.-])[$/]?{re.escape(variant)}(?![\w.-])"
        if re.search(pattern, message):
            return True
    return False
