"""
Skills 生命周期管理器（DESIGN.md §五.3）。

职责：
  - discover(): 扫描并加载所有 Skill 的 Layer 1
  - auto_detect(user_message): 只识别显式 $skill / /skill / @skill 调用
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
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.skills.loader import SkillLoader, SkillFull, SkillMeta

logger = logging.getLogger(__name__)

_USAGE_COUNT_KEYS = ("load_count", "reuse_count", "failure_count", "unload_count")


@dataclass(frozen=True)
class SkillDetection:
    name: str
    trigger_mode: str
    reason: str


class SkillManager:
    """
    Skills 生命周期管理器。

    使用示例：
        manager = SkillManager()
        manager.discover()
        
        # 显式检测
        to_activate = manager.auto_detect("帮我写一个 React 组件")
        for name in to_activate:
            manager.activate(name)
        
        # 获取注入内容
        content = manager.get_active_content()
    """

    def __init__(
        self,
        loader: SkillLoader | None = None,
        *,
        usage_store_path: Path | None = None,
    ) -> None:
        self._loader = loader or SkillLoader()
        self._active: dict[str, SkillFull] = {}  # {name: SkillFull}
        self._invoked: dict[str, SkillFull] = {}  # preserved across one-shot context injection
        self._temporary_hook_owners: set[str] = set()
        self._usage_stats: dict[str, dict[str, Any]] = {}
        self._usage_store_path = Path(usage_store_path) if usage_store_path else None
        self._load_usage_stats()
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

    def detect(self, user_message: str) -> list[SkillDetection]:
        """
        根据用户消息检测显式调用的 Skill。

        匹配逻辑：
          1. 将 user_message 转小写
          2. 只接受 $skill-name、/skill-name 或 @skill-name
          3. 普通提到 skill 名称或 trigger 不再隐式激活，避免 prompt 污染；
             模型可通过 list_skills/load_skill 主动选择匹配的 skill

        Args:
            user_message: 用户消息

        Returns:
            应该激活的 Skill 名称列表
        """
        if not self._discovered:
            self.discover()

        msg_lower = user_message.lower()
        candidates: list[SkillDetection] = []

        all_skills = self._loader.list_skill_names()
        for name in all_skills:
            meta = self._loader.get_meta(name)
            if not meta:
                continue

            if _message_explicitly_invokes_skill_name(msg_lower, name):
                candidates.append(SkillDetection(
                    name=name,
                    trigger_mode="explicit",
                reason=f"用户显式调用 ${name}、/{name} 或 @{name}",
                ))
                continue

        return candidates

    def auto_detect(self, user_message: str) -> list[str]:
        """Compatibility wrapper returning only skill names."""
        return [detection.name for detection in self.detect(user_message)]

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
            self._invoked.setdefault(skill_name, self._active[skill_name])
            self._register_temporary_hooks(self._active[skill_name])
            self._record_usage(skill_name, "reused")
            logger.debug("Skill '%s' 已经激活", skill_name)
            return True

        # 加载 Layer 2
        full = self._loader.load_full(skill_name)
        if not full:
            self._record_usage(skill_name, "failed")
            logger.warning("Skill '%s' 加载失败", skill_name)
            return False

        # 冲突检测
        self._resolve_conflicts(full.meta)

        # 激活
        self._active[skill_name] = full
        self._invoked[skill_name] = full
        self._register_temporary_hooks(full)
        self._record_usage(skill_name, "loaded")
        logger.info(
            "✅ Skill '%s' 已激活（~%d tokens）",
            skill_name, full.token_estimate,
        )
        return True

    def deactivate(self, skill_name: str) -> bool:
        """停用一个 Skill。"""
        removed = self._active.pop(skill_name, None)
        invoked_removed = self._invoked.pop(skill_name, None)
        self._remove_temporary_hooks(skill_name)
        if removed:
            self._record_usage(skill_name, "unloaded")
            logger.info("❌ Skill '%s' 已停用", skill_name)
            return True
        if invoked_removed:
            self._record_usage(skill_name, "unloaded")
            logger.info("❌ Skill '%s' 已从已调用 Skill 记录中移除", skill_name)
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
                metadata_lines: list[str] = []
                if full.meta.when_to_use:
                    metadata_lines.append(f"When to use: {full.meta.when_to_use}")
                if full.meta.tools_required:
                    metadata_lines.append("Allowed tools: " + ", ".join(full.meta.tools_required))
                if full.meta.linked_resources:
                    metadata_lines.append("Linked resources: " + ", ".join(full.meta.linked_resources))
                if metadata_lines:
                    parts.append("\n".join(metadata_lines))
                parts.append(self.render_skill_content(full))
                used += tokens

        return "\n\n".join(parts)

    def consume_active_content(self, budget: int = 4000) -> str:
        """Return current active skill instructions and clear them.

        Codex-style skills are progressive-disclosure snippets selected for the
        current task/turn. They should not stay in the system prompt forever.
        """
        content = self.get_active_content(budget=budget)
        if self._active:
            self._active.clear()
        return content

    def get_invoked_skills(self) -> list[dict[str, Any]]:
        """Return SKILL.md payloads that should survive compaction."""
        return [self._skill_payload(full) for full in self._invoked.values()]

    def get_skill_payload(self, skill_name: str) -> dict[str, Any] | None:
        """Return one skill payload without activating it."""
        full = self._invoked.get(skill_name) or self._active.get(skill_name)
        if full is None:
            if not self._discovered:
                self.discover()
            full = self._loader.load_full(skill_name)
        if full is None:
            return None
        return self._skill_payload(full)

    def get_usage_stats(self, skill_name: str | None = None) -> dict[str, Any]:
        """Return process-local Skill usage counters for UI/telemetry."""
        if skill_name is not None:
            return dict(self._usage_stats.get(skill_name, {}))
        return {name: dict(stats) for name, stats in self._usage_stats.items()}

    def render_skill_content(self, full: SkillFull) -> str:
        """Render SKILL.md content the same way it is injected into context."""
        base_dir = _skill_base_dir(full.meta.source_path)
        base_text = str(base_dir)
        content = str(full.content or "")
        for placeholder in (
            "${CLAUDE_SKILL_DIR}",
            "${CODEX_SKILL_DIR}",
            "${MINICODE_SKILL_DIR}",
        ):
            content = content.replace(placeholder, base_text)
        return f"Base directory for this skill: {base_text}\n\n{content}".strip()

    def _skill_payload(self, full: SkillFull) -> dict[str, Any]:
        meta = full.meta
        return {
            "name": meta.name,
            "path": str(meta.source_path),
            "source_level": meta.source_level,
            "description": meta.description,
            "content": self.render_skill_content(full),
            "token_estimate": full.token_estimate,
        }

    def _register_temporary_hooks(self, full: SkillFull) -> None:
        specs = self._render_temporary_hook_specs(full)
        if not specs:
            return
        try:
            from backend.hooks import get_hook_manager
        except Exception:
            return
        hook_manager = get_hook_manager()
        add_temporary_hooks = getattr(hook_manager, "add_temporary_hooks", None)
        if not callable(add_temporary_hooks):
            return
        owner = self._temporary_hook_owner(full.meta.name)
        try:
            registered = int(add_temporary_hooks(owner, specs) or 0)
        except Exception as exc:
            logger.debug("Failed to register temporary hooks for skill %s: %s", full.meta.name, exc)
            return
        if registered:
            self._temporary_hook_owners.add(owner)
            logger.info("Skill '%s' registered %d temporary hooks", full.meta.name, registered)

    def _remove_temporary_hooks(self, skill_name: str) -> None:
        owner = self._temporary_hook_owner(skill_name)
        if owner not in self._temporary_hook_owners:
            return
        try:
            from backend.hooks import get_hook_manager
        except Exception:
            return
        hook_manager = get_hook_manager()
        remove_temporary_hooks = getattr(hook_manager, "remove_temporary_hooks", None)
        if callable(remove_temporary_hooks):
            try:
                remove_temporary_hooks(owner)
            except Exception as exc:
                logger.debug("Failed to remove temporary hooks for skill %s: %s", skill_name, exc)
        self._temporary_hook_owners.discard(owner)

    def _render_temporary_hook_specs(self, full: SkillFull) -> list[dict[str, Any]]:
        base_dir = str(_skill_base_dir(full.meta.source_path))
        rendered: list[dict[str, Any]] = []
        for raw in getattr(full.meta, "temporary_hooks", []) or []:
            if not isinstance(raw, dict):
                continue
            item: dict[str, Any] = {}
            for key, value in raw.items():
                if isinstance(value, str):
                    item[str(key)] = _replace_skill_placeholders(value, base_dir)
                else:
                    item[str(key)] = value
            rendered.append(item)
        return rendered

    @staticmethod
    def _temporary_hook_owner(skill_name: str) -> str:
        return f"skill:{skill_name}"

    def _record_usage(self, skill_name: str, event: str) -> None:
        name = str(skill_name or "").strip()
        if not name:
            return
        self._load_usage_stats()
        stats = self._usage_stats.setdefault(
            name,
            {
                "load_count": 0,
                "reuse_count": 0,
                "failure_count": 0,
                "unload_count": 0,
                "last_event": "",
                "last_invoked_at": "",
            },
        )
        if event == "loaded":
            stats["load_count"] = int(stats.get("load_count", 0)) + 1
            stats["last_invoked_at"] = _utc_now_iso()
        elif event == "reused":
            stats["reuse_count"] = int(stats.get("reuse_count", 0)) + 1
            stats["last_invoked_at"] = _utc_now_iso()
        elif event == "failed":
            stats["failure_count"] = int(stats.get("failure_count", 0)) + 1
        elif event == "unloaded":
            stats["unload_count"] = int(stats.get("unload_count", 0)) + 1
        stats["last_event"] = event
        self._persist_usage_stats()

    def _load_usage_stats(self) -> None:
        if self._usage_store_path is None or not self._usage_store_path.exists():
            return
        try:
            raw = json.loads(self._usage_store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to load Skill usage stats: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        loaded: dict[str, dict[str, Any]] = {}
        for raw_name, raw_stats in raw.items():
            name = str(raw_name or "").strip()
            if not name or not isinstance(raw_stats, dict):
                continue
            stats: dict[str, Any] = {}
            for key in _USAGE_COUNT_KEYS:
                try:
                    stats[key] = max(0, int(raw_stats.get(key, 0) or 0))
                except (TypeError, ValueError):
                    stats[key] = 0
            stats["last_event"] = str(raw_stats.get("last_event") or "").strip()
            stats["last_invoked_at"] = str(raw_stats.get("last_invoked_at") or "").strip()
            loaded[name] = stats
        self._usage_stats.update(loaded)

    def _persist_usage_stats(self) -> None:
        if self._usage_store_path is None:
            return
        try:
            self._usage_store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._usage_store_path.with_suffix(self._usage_store_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self._usage_stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self._usage_store_path)
        except Exception as exc:
            logger.debug("Failed to persist Skill usage stats: %s", exc)

    def get_layer1_summary(self) -> str:
        """
        获取所有 Skill 的 Layer 1 摘要（始终注入 context）。

        让 LLM 知道有哪些 Skill 可用。
        """
        return self._loader.get_all_layer1()

    def get_active_names(self) -> list[str]:
        """获取已激活的 Skill 名称列表。"""
        return list(self._active.keys())

    def is_active(self, skill_name: str) -> bool:
        """判断 Skill 是否已激活。"""
        return skill_name in self._active

    def get_meta(self, skill_name: str) -> SkillMeta | None:
        """获取 Skill 元数据。"""
        if not self._discovered:
            self.discover()
        return self._loader.get_meta(skill_name)

    def get_active_full(self, skill_name: str) -> SkillFull | None:
        """获取已激活 Skill 的完整内容。"""
        return self._active.get(skill_name)

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
            mcps.update(getattr(full.meta, "mcp_dependencies", []))
        return list(mcps)

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有 Skill 及其状态（供前端展示）。"""
        if not self._discovered:
            self.discover()

        result: list[dict[str, Any]] = []
        for name in self._loader.list_skill_names():
            meta = self._loader.get_meta(name)
            if meta:
                entry = {
                    "name": meta.name,
                    "description": meta.description,
                    "when_to_use": getattr(meta, "when_to_use", ""),
                    "display_name": getattr(meta, "display_name", ""),
                    "icon": getattr(meta, "icon", ""),
                    "active": name in self._active,
                    "triggers": meta.triggers,
                    "version": meta.version,
                    "level": meta.source_level,
                    "source_level": meta.source_level,
                    "tools_required": meta.tools_required,
                    "mcp_required": meta.mcp_required,
                    "mcp_dependencies": getattr(meta, "mcp_dependencies", []),
                    "allow_implicit_invocation": getattr(meta, "allow_implicit_invocation", True),
                    "default_prompt": getattr(meta, "default_prompt", ""),
                }
                hooks_required = getattr(meta, "hooks_required", [])
                shell_commands = getattr(meta, "shell_commands", [])
                if hooks_required:
                    entry["hooks_required"] = hooks_required
                if shell_commands:
                    entry["shell_commands"] = shell_commands
                usage = self.get_usage_stats(meta.name)
                if usage:
                    entry["usage"] = usage
                result.append(entry)
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


def _message_explicitly_invokes_skill_name(message: str, skill_name: str) -> bool:
    """Detect explicit $skill, /skill, or @skill invocation."""
    normalized = skill_name.strip().lower()
    if not normalized:
        return False
    variants = {
        normalized,
        normalized.replace("_", "-"),
        normalized.replace("-", "_"),
    }
    for variant in variants:
        pattern = rf"(?<![\w.-])[$/@]{re.escape(variant)}(?![\w.-])"
        if re.search(pattern, message):
            return True
    return False


def _skill_base_dir(source_path: Path) -> Path:
    try:
        path = Path(source_path)
    except TypeError:
        return Path(".")
    if path.name:
        return path.parent
    return path


def _replace_skill_placeholders(value: str, base_dir: str) -> str:
    result = str(value or "")
    for placeholder in (
        "${CLAUDE_SKILL_DIR}",
        "${CODEX_SKILL_DIR}",
        "${MINICODE_SKILL_DIR}",
    ):
        result = result.replace(placeholder, base_dir)
    return result


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
