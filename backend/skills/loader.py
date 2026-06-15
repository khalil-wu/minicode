"""
SKILL.md 三层加载器（DESIGN.md §五.1）。

Skills 是 cinstr 层的扩展 — 改变 Agent 的「思维方式」，零代码，纯 Prompt 注入。

三层加载模型：
  Layer 1: name + description（~20 tokens/skill，始终加载到 context）
            用于 LLM 知道有哪些 Skill 可用
  Layer 2: SKILL.md 完整正文（触发时加载，1-4K tokens）
            包含详细指令、工作流、工具使用指导
  Layer 3: linked_resources（Agent 按需读取，不预装）
            外部文档、代码范例等

发现目录优先级（高→低）：
  1. 项目级: ./.mini-code/skills/<skill-name>/SKILL.md
  2. 全局级: ~/.mini-code/skills/<skill-name>/SKILL.md
  3. 内置级: ./skills/<skill-name>/SKILL.md

SKILL.md 格式：
  ---
  name: frontend-dev
  description: React 18 + TypeScript + Tailwind CSS 专家模式
  version: 1.0.0
  triggers: [react, frontend, css, tailwind, component]
  conflicts: [backend-dev]
  tools_required: [write_file, edit_file, run_command]
  mcp_required: []
  linked_resources: [./examples/component-template.tsx]
  ---
  
  （下面是 Layer 2 正文内容）
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


@dataclass
class SkillMeta:
    """Skill 元数据（Layer 1）。"""
    name: str
    description: str
    version: str = "1.0.0"
    triggers: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    mcp_required: list[str] = field(default_factory=list)
    linked_resources: list[str] = field(default_factory=list)
    source_path: Path = field(default_factory=lambda: Path("."))
    source_level: str = "builtin"  # project / global / builtin

    def to_layer1_summary(self) -> str:
        """Layer 1 摘要（~20 tokens）。"""
        return f"- {self.name}: {self.description}"


@dataclass
class SkillFull:
    """完整 Skill 数据（Layer 1 + Layer 2）。"""
    meta: SkillMeta
    content: str  # Layer 2: SKILL.md 正文（去掉 frontmatter）

    @property
    def token_estimate(self) -> int:
        """估算 Layer 2 内容的 token 数。"""
        return len(self.content) // 4


class SkillLoader:
    """
    SKILL.md 发现与解析。

    使用示例：
        loader = SkillLoader()
        all_skills = loader.discover()                # Layer 1
        full = loader.load_full("frontend-dev")       # Layer 2
    """

    # 发现目录（按优先级排序）
    SEARCH_DIRS = [
        ("project", PROJECT_ROOT / ".codex" / "skills"),
        ("project-legacy", PROJECT_ROOT / ".mini-code" / "skills"),
        ("global", Path.home() / ".codex" / "skills"),
        ("global-legacy", Path.home() / ".mini-code" / "skills"),
        ("builtin", PROJECT_ROOT / "skills"),
    ]

    def __init__(self) -> None:
        self._cache: dict[str, SkillMeta] = {}
        self._full_cache: dict[str, SkillFull] = {}

    def _search_dirs(self) -> list[tuple[str, Path]]:
        dirs = list(self.SEARCH_DIRS)
        codex_home = str(os.environ.get("CODEX_HOME") or "").strip()
        if codex_home:
            dirs.insert(2, ("global", Path(codex_home).expanduser() / "skills"))

        deduped: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        for level, path in dirs:
            expanded = path.expanduser()
            key = expanded.resolve() if expanded.exists() else expanded.absolute()
            if key in seen:
                continue
            seen.add(key)
            deduped.append((level, expanded))
        return deduped

    def discover(self) -> list[SkillMeta]:
        """
        扫描三级目录，发现所有 SKILL.md。

        只加载 Layer 1（frontmatter 元数据），不读正文。
        高优先级目录的同名 Skill 覆盖低优先级。

        Returns:
            SkillMeta 列表（已去重，高优先级优先）
        """
        seen: dict[str, SkillMeta] = {}

        # 从最低优先级开始扫描，高优先级覆盖低优先级
        for level, base_dir in reversed(self._search_dirs()):
            if not base_dir.exists():
                continue

            for skill_dir in sorted(base_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue

                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue

                meta = self._parse_frontmatter(skill_file, level)
                if meta:
                    seen[meta.name] = meta

        self._cache = seen
        logger.info("发现 %d 个 Skills: %s", len(seen), ", ".join(seen.keys()))
        return list(seen.values())

    def load_full(self, skill_name: str) -> SkillFull | None:
        """
        加载 Skill 的 Layer 2 内容。

        Args:
            skill_name: Skill 名称

        Returns:
            SkillFull（含完整正文）或 None
        """
        # 检查缓存
        if skill_name in self._full_cache:
            return self._full_cache[skill_name]

        # 需要先 discover
        if not self._cache:
            self.discover()

        meta = self._cache.get(skill_name)
        if not meta:
            logger.warning("Skill '%s' 不存在", skill_name)
            return None

        # 读取完整文件
        skill_file = meta.source_path
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.error("读取 %s 失败: %s", skill_file, exc)
            return None

        # 提取正文（去掉 frontmatter）
        content = self._extract_body(raw)

        full = SkillFull(meta=meta, content=content)
        self._full_cache[skill_name] = full

        logger.info(
            "加载 Skill '%s' Layer 2: ~%d tokens",
            skill_name, full.token_estimate,
        )
        return full

    def get_all_layer1(self) -> str:
        """
        获取所有 Skill 的 Layer 1 摘要。

        用于始终注入 context，让 LLM 知道可用的 Skill。

        Returns:
            格式化的 Skill 列表（每个 ~20 tokens）
        """
        if not self._cache:
            self.discover()

        if not self._cache:
            return ""

        lines = []
        for meta in self._cache.values():
            lines.append(meta.to_layer1_summary())
        return "\n".join(lines)

    def list_skill_names(self) -> list[str]:
        """列出所有 Skill 名称。"""
        if not self._cache:
            self.discover()
        return list(self._cache.keys())

    def get_meta(self, skill_name: str) -> SkillMeta | None:
        """获取 Skill 元数据。"""
        if not self._cache:
            self.discover()
        return self._cache.get(skill_name)

    # ── 解析辅助 ──────────────────────────────────────

    def _parse_frontmatter(self, skill_file: Path, level: str) -> SkillMeta | None:
        """
        解析 SKILL.md 的 YAML frontmatter。

        只读取 frontmatter 部分（--- 之间的内容），不读正文。
        """
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        # 提取 frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---", raw, re.DOTALL)
        if not fm_match:
            # 没有 frontmatter，使用目录名作为 skill name
            return SkillMeta(
                name=skill_file.parent.name,
                description="（无描述）",
                source_path=skill_file,
                source_level=level,
            )

        fm_text = fm_match.group(1)
        fm = self._parse_simple_yaml(fm_text)

        return SkillMeta(
            name=fm.get("name", skill_file.parent.name),
            description=fm.get("description", ""),
            version=fm.get("version", "1.0.0"),
            triggers=self._to_list(fm.get("triggers", [])),
            conflicts=self._to_list(fm.get("conflicts", [])),
            tools_required=self._to_list(fm.get("tools_required", [])),
            mcp_required=self._to_list(fm.get("mcp_required", [])),
            linked_resources=self._to_list(fm.get("linked_resources", [])),
            source_path=skill_file,
            source_level=level,
        )

    @staticmethod
    def _extract_body(raw: str) -> str:
        """从 SKILL.md 提取正文（去掉 frontmatter）。"""
        # 去掉 frontmatter
        body = re.sub(r"^---\s*\n.*?\n---\s*\n?", "", raw, count=1, flags=re.DOTALL)
        return body.strip()

    @staticmethod
    def _parse_simple_yaml(text: str) -> dict[str, Any]:
        """
        简单 YAML 解析器（避免引入 pyyaml 依赖）。

        只支持 key: value 和 key: [item1, item2] 两种格式。
        """
        result: dict[str, Any] = {}
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if ":" not in line:
                continue

            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            # 数组格式：[item1, item2]
            if value.startswith("[") and value.endswith("]"):
                items = value[1:-1].split(",")
                result[key] = [item.strip().strip("'\"") for item in items if item.strip()]
            else:
                # 去掉引号
                value = value.strip("'\"")
                result[key] = value

        return result

    @staticmethod
    def _to_list(val: Any) -> list[str]:
        """确保值为列表。"""
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            return [val] if val else []
        return []
