"""Codex-compatible Skill discovery and turn-scoped selection."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.skills.loader import SkillLoader, SkillFull, SkillMeta

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class SkillDetection:
    name: str
    trigger_mode: str
    reason: str
    source_path: str = ""


class SkillManager:
    """Discover Skills and stage exact SKILL.md files for the current turn."""

    def __init__(
        self,
        loader: SkillLoader | None = None,
    ) -> None:
        self._loader = loader or SkillLoader()
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

    def set_project_root(self, project_root: Path | str | None) -> None:
        """Switch skill discovery with the active workspace session."""
        self._loader.set_project_root(project_root)
        self._discovered = False
        self.discover()

    def detect(
        self,
        user_message: str,
        selected_skills: list[dict[str, Any]] | None = None,
    ) -> list[SkillDetection]:
        """
        根据用户消息检测显式调用的 Skill。

        匹配逻辑：
          1. 将 user_message 转小写
          2. 只接受 $skill-name 或 /skill-name
          3. 普通匹配由 Codex-style available-skills instructions 引导模型
             读取对应 SKILL.md，不增加私有 skill 工具协议

        Args:
            user_message: 用户消息

        Returns:
            应该激活的 Skill 名称列表
        """
        if not self._discovered:
            self.discover()

        msg_lower = user_message.lower()
        candidates: list[SkillDetection] = []
        selected_paths: set[str] = set()

        for selected in selected_skills or []:
            if not isinstance(selected, dict):
                continue
            name = str(selected.get("name") or "").strip()
            source_path = str(selected.get("path") or "").strip()
            meta = self._loader.get_meta_by_path(source_path)
            if meta is None or (name and meta.name != name):
                continue
            key = self._skill_key(meta.source_path)
            if key in selected_paths:
                continue
            selected_paths.add(key)
            candidates.append(SkillDetection(
                name=meta.name,
                trigger_mode="explicit",
                reason=f"用户显式选择 ${meta.name}",
                source_path=str(meta.source_path),
            ))

        all_skills = self._loader.list_skill_names()
        for name in all_skills:
            metas = self._loader.get_metas(name)
            if len(metas) != 1:
                continue
            meta = metas[0]
            if self._skill_key(meta.source_path) in selected_paths:
                continue

            if _message_explicitly_invokes_skill_name(msg_lower, name):
                candidates.append(SkillDetection(
                    name=name,
                    trigger_mode="explicit",
                    reason=f"用户显式调用 ${name} 或 /{name}",
                    source_path=str(meta.source_path),
                ))
                continue

        return candidates

    def auto_detect(self, user_message: str) -> list[str]:
        """Compatibility wrapper returning only skill names."""
        return [detection.name for detection in self.detect(user_message)]

    def load_skill_payload(
        self,
        skill_name: str,
        source_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Load one exact SKILL.md as a turn-owned contextual payload."""
        if not self._discovered:
            self.discover()

        meta = self._loader.get_meta_by_path(source_path) if source_path else self._loader.get_unambiguous_meta(skill_name)
        if meta is None:
            logger.warning("Skill '%s' 不存在或名称不唯一", skill_name)
            return None

        full = self._loader.load_full(meta.name, meta.source_path)
        if not full:
            logger.warning("Skill '%s' 加载失败", skill_name)
            return None

        logger.info(
            "Loaded Skill '%s' for this turn (~%d tokens)",
            skill_name, full.token_estimate,
        )
        return self._skill_payload(full)

    def _skill_payload(self, full: SkillFull) -> dict[str, Any]:
        meta = full.meta
        return {
            "name": meta.name,
            "path": str(meta.source_path),
            "source_level": meta.source_level,
            "description": meta.description,
            "content": full.raw_content or full.content,
            "token_estimate": full.token_estimate,
        }

    def get_layer1_summary(self) -> str:
        """
        获取所有 Skill 的 Layer 1 摘要（始终注入 context）。

        让 LLM 知道有哪些 Skill 可用。
        """
        return self._loader.get_all_layer1()

    def readable_roots(self) -> list[Path]:
        """Return the exact discovered Skill directories as read-only roots.

        Codex exposes absolute Skill locations in the catalog and lets the
        model read the selected ``SKILL.md`` plus its referenced files.  Keep
        that access limited to directories already admitted by discovery;
        disabled or malformed plugins therefore never become readable roots.
        """
        if not self._discovered:
            self.discover()
        roots: list[Path] = []
        seen: set[Path] = set()
        for meta in self._loader.list_metas():
            try:
                root = meta.source_path.parent.resolve()
            except OSError:
                continue
            if root in seen:
                continue
            seen.add(root)
            roots.append(root)
        return roots

    def get_meta(self, skill_name: str) -> SkillMeta | None:
        """获取 Skill 元数据。"""
        if not self._discovered:
            self.discover()
        return self._loader.get_meta(skill_name)

    def resolve_asset(self, source_path: str | Path, variant: str) -> Path | None:
        """Resolve an icon only from an exact Skill discovered by this manager."""
        if not self._discovered:
            self.discover()
        meta = self._loader.get_meta_by_path(source_path)
        if meta is None:
            return None
        raw_path = meta.icon_large if variant == "large" else meta.icon
        if not raw_path:
            return None
        candidate = Path(raw_path)
        try:
            candidate = candidate.resolve()
            candidate.relative_to(meta.source_path.parent.resolve())
        except (OSError, ValueError):
            return None
        return candidate if candidate.is_file() else None

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有 Skill 及其状态（供前端展示）。"""
        if not self._discovered:
            self.discover()

        result: list[dict[str, Any]] = []
        for meta in self._loader.list_metas():
            if meta:
                entry = {
                    "name": meta.name,
                    "description": meta.description,
                    "display_name": getattr(meta, "display_name", ""),
                    "short_description": getattr(meta, "short_description", ""),
                    "icon": getattr(meta, "icon", ""),
                    "icon_large": getattr(meta, "icon_large", ""),
                    "brand_color": getattr(meta, "brand_color", ""),
                    "path": str(meta.source_path),
                    "active": False,
                    "level": meta.source_level,
                    "source_level": meta.source_level,
                    "mcp_dependencies": getattr(meta, "mcp_dependencies", []),
                    "allow_implicit_invocation": getattr(meta, "allow_implicit_invocation", True),
                    "default_prompt": getattr(meta, "default_prompt", ""),
                }
                result.append(entry)
        return result

    @staticmethod
    def _skill_key(source_path: Path | str) -> str:
        path = Path(source_path)
        try:
            path = path.resolve()
        except OSError:
            path = path.absolute()
        return str(path)

def _message_explicitly_invokes_skill_name(message: str, skill_name: str) -> bool:
    """Detect explicit Codex $skill or CC-compatible /skill invocation."""
    normalized = skill_name.strip().lower()
    if not normalized:
        return False
    variants = {
        normalized,
        normalized.replace("_", "-"),
        normalized.replace("-", "_"),
    }
    for variant in variants:
        pattern = rf"(?<![\w.-])[$/]{re.escape(variant)}(?![\w.-])"
        if re.search(pattern, message):
            return True
    return False
