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
  2. 插件级: ~/.minicode/plugins/<plugin>/skills/<skill-name>/SKILL.md
  3. 全局级: ~/.mini-code/skills/<skill-name>/SKILL.md
  4. 内置级: ./skills/<skill-name>/SKILL.md

SKILL.md 格式：
  ---
  name: frontend-dev
  description: React 18 + TypeScript + Tailwind CSS 专家模式
  display_name: Frontend Dev
  icon: code
  version: 1.0.0
  when_to_use: Use when building React UI components or frontend flows.
  triggers: [react, frontend, css, tailwind, component]
  conflicts: [backend-dev]
  tools_required: [write_file, edit_file, run_command]
  # aliases also accepted: tools, allowed-tools, allowed_tools
  mcp_required: []
  mcp_dependencies: []
  allow_implicit_invocation: true
  default_prompt: Use this skill for frontend implementation work.
  linked_resources: [./examples/component-template.tsx]
  # aliases also accepted: paths, resources
  ---
  
  （下面是 Layer 2 正文内容）
"""

from __future__ import annotations

import logging
import os
import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT
from backend.feature_flags import feature_enabled

logger = logging.getLogger(__name__)


_BUILTIN_SKILL_FALLBACKS: dict[str, tuple[str, str]] = {
    "api-design": ("Design stable, minimal APIs.", "Design the smallest clear API contract, including validation, errors, compatibility, and tests."),
    "backend-dev": ("Implement reliable backend changes.", "Trace the backend flow end to end, make the smallest root-cause change, and verify it with focused tests."),
    "code-review": ("Review code for release risks.", "Review correctness, security, regressions, performance, and missing tests. Report findings by severity with file references."),
    "commit-message": ("Summarize changes for a commit.", "Inspect the current diff and produce a concise commit title and body that explain the behavior change and verification."),
    "data-analysis": ("Analyze structured data carefully.", "Validate the input, compute only the requested analysis, and state assumptions and uncertainty clearly."),
    "debug-mode": ("Diagnose bugs with a reproducible loop.", "Reproduce the exact symptom, isolate the root cause, apply the smallest fix, and rerun the regression check."),
    "docs-writer": ("Write project documentation.", "Read the relevant code before documenting architecture, behavior, setup, and operational constraints with concrete file references."),
    "frontend-dev": ("Build polished frontend flows.", "Implement accessible, responsive UI using the existing design system and verify keyboard, dark mode, and loading states."),
    "git-workflow": ("Use safe Git workflows.", "Inspect repository state, preserve user changes, avoid destructive commands, and keep commits focused and explainable."),
    "init": ("Initialize project guidance.", "Inspect the repository and create concise project guidance covering architecture, commands, conventions, and verification."),
    "mcp-integration": ("Integrate MCP tools safely.", "Define the smallest MCP contract, validate tool schemas and errors, and verify discovery and execution end to end."),
    "performance": ("Optimize measured bottlenecks.", "Measure first, fix the dominant bottleneck, and verify the improvement without speculative complexity."),
    "refactor": ("Refactor in safe increments.", "Preserve behavior, reduce coupling or duplication with the smallest coherent change, and keep tests green."),
    "security-audit": ("Audit security boundaries.", "Review trust boundaries, authentication, authorization, input handling, secrets, and command execution; prioritize exploitable findings."),
    "simplify": ("Remove unnecessary complexity.", "Delete duplication and speculative abstractions, reuse existing helpers, and keep only behavior the product currently needs."),
    "test-writer": ("Add focused regression coverage.", "Write the smallest deterministic test that reproduces the real behavior at the correct seam, then verify the relevant suite."),
    "verify": ("Verify work before completion.", "Run the narrow regression first, then the relevant broader tests and build; report any remaining warning or unverified surface."),
}


@dataclass
class SkillMeta:
    """Skill 元数据（Layer 1）。"""
    name: str
    description: str
    when_to_use: str = ""
    display_name: str = ""
    icon: str = ""
    version: str = "1.0.0"
    triggers: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    mcp_required: list[str] = field(default_factory=list)
    mcp_dependencies: list[str] = field(default_factory=list)
    hooks_required: list[str] = field(default_factory=list)
    temporary_hooks: list[dict[str, Any]] = field(default_factory=list)
    shell_commands: list[str] = field(default_factory=list)
    allow_implicit_invocation: bool = True
    default_prompt: str = ""
    linked_resources: list[str] = field(default_factory=list)
    source_path: Path = field(default_factory=lambda: Path("."))
    source_level: str = "builtin"  # project / global / builtin

    def to_layer1_summary(self) -> str:
        """Layer 1 摘要（~20 tokens）。"""
        title = f"{self.name} ({self.display_name})" if self.display_name else self.name
        policy = "" if self.allow_implicit_invocation else " [explicit only]"
        when = f" When to use: {self.when_to_use}" if self.when_to_use else ""
        return f"- {title}: {self.description}{when}{policy}"


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
        dirs[2:2] = self._plugin_search_dirs()

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

    def _plugin_search_dirs(self) -> list[tuple[str, Path]]:
        if not feature_enabled("plugin_skills", True):
            return []
        try:
            from backend.commands.plugins import default_plugin_roots
            from backend.services.plugin_settings_service import get_disabled_plugin_names, plugin_name_from_directory
        except Exception:
            return []

        dirs: list[tuple[str, Path]] = []
        disabled = get_disabled_plugin_names()
        for root in default_plugin_roots():
            root = root.expanduser()
            if not root.is_dir():
                continue
            plugin_dirs = self._candidate_plugin_dirs(root)
            for plugin_dir in plugin_dirs:
                if plugin_name_from_directory(plugin_dir).strip().casefold() in disabled:
                    continue
                for skills_dir in (plugin_dir / "skills", plugin_dir / ".codex-plugin" / "skills"):
                    if skills_dir.is_dir():
                        dirs.append(("plugin", skills_dir))
        return dirs

    @staticmethod
    def _candidate_plugin_dirs(root: Path) -> list[Path]:
        if (root / "skills").is_dir():
            return [root]
        candidates = [path for path in root.iterdir() if path.is_dir()]
        # Codex marketplace plugins are cached as:
        #   ~/.codex/plugins/cache/<owner>/<plugin>/<version>/skills
        cache_dir = root / "cache"
        if cache_dir.is_dir():
            candidates.extend(path for path in cache_dir.glob("*/*/*") if path.is_dir())
        return candidates

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

        for name, (description, _content) in _BUILTIN_SKILL_FALLBACKS.items():
            seen.setdefault(
                name,
                SkillMeta(
                    name=name,
                    description=description,
                    source_path=PROJECT_ROOT / "skills" / name / "SKILL.md",
                    source_level="builtin",
                ),
            )

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
            content = self._extract_body(raw)
        except (OSError, UnicodeDecodeError) as exc:
            fallback = _BUILTIN_SKILL_FALLBACKS.get(skill_name)
            if fallback is None:
                logger.error("读取 %s 失败: %s", skill_file, exc)
                return None
            content = fallback[1]

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

        tools_required = self._first_list(
            fm,
            "tools_required",
            "tools",
            "allowed-tools",
            "allowed_tools",
        )
        linked_resources = self._first_list(
            fm,
            "linked_resources",
            "paths",
            "resources",
        )

        return SkillMeta(
            name=fm.get("name", skill_file.parent.name),
            description=fm.get("description", ""),
            when_to_use=fm.get("when_to_use", fm.get("whenToUse", "")),
            display_name=fm.get("display_name", fm.get("displayName", "")),
            icon=fm.get("icon", ""),
            version=fm.get("version", "1.0.0"),
            triggers=self._to_list(fm.get("triggers", [])),
            conflicts=self._to_list(fm.get("conflicts", [])),
            tools_required=tools_required,
            mcp_required=self._to_list(fm.get("mcp_required", [])),
            mcp_dependencies=self._to_list(fm.get("mcp_dependencies", [])),
            hooks_required=self._first_list(fm, "hooks", "hooks_required", "hook_dependencies"),
            temporary_hooks=self._first_hook_specs(fm, "temporary_hooks", "skill_hooks", "hooks"),
            shell_commands=self._first_list(fm, "shell", "shell_commands", "commands", "allowed_commands"),
            allow_implicit_invocation=self._to_bool(fm.get("allow_implicit_invocation", True), default=True),
            default_prompt=fm.get("default_prompt", ""),
            linked_resources=linked_resources,
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

        支持 key: value、key: [item1, item2]，以及用于 Skill hooks
        的简单双层列表：

          temporary_hooks:
            - event: PreToolUse
              matcher: write_file
              command: python scripts/check.py
        """
        result: dict[str, Any] = {}
        lines = text.splitlines()
        index = 0
        while index < len(lines):
            raw_line = lines[index]
            line = raw_line.strip()
            if not line or line.startswith("#"):
                index += 1
                continue

            if ":" not in line:
                index += 1
                continue

            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value in {">", "|", ">-", "|-", ">+", "|+"}:
                base_indent = len(raw_line) - len(raw_line.lstrip())
                block: list[str] = []
                index += 1
                while index < len(lines):
                    child = lines[index]
                    stripped = child.strip()
                    if not stripped:
                        block.append("")
                        index += 1
                        continue
                    if stripped.startswith("#"):
                        index += 1
                        continue
                    indent = len(child) - len(child.lstrip())
                    if indent <= base_indent:
                        break
                    block.append(child)
                    index += 1
                result[key] = SkillLoader._parse_yaml_text_block(block, folded=value.startswith(">"))
                continue
            if value:
                result[key] = SkillLoader._parse_yaml_scalar(value)
                index += 1
                continue

            base_indent = len(raw_line) - len(raw_line.lstrip())
            block: list[str] = []
            index += 1
            while index < len(lines):
                child = lines[index]
                stripped = child.strip()
                if not stripped or stripped.startswith("#"):
                    index += 1
                    continue
                indent = len(child) - len(child.lstrip())
                if indent <= base_indent:
                    break
                block.append(child)
                index += 1
            result[key] = SkillLoader._parse_yaml_block(block)
        return result

    @staticmethod
    def _parse_yaml_scalar(value: str) -> Any:
        value = value.strip()
        if not value:
            return ""
        if value[0] in "[{":
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                pass

        # 数组格式：[item1, item2]
        if value.startswith("[") and value.endswith("]"):
            items = value[1:-1].split(",")
            return [item.strip().strip("'\"") for item in items if item.strip()]

        return value.strip("'\"")

    @staticmethod
    def _parse_yaml_block(block: list[str]) -> Any:
        if not block:
            return ""
        items: list[Any] = []
        current: Any | None = None
        for raw in block:
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("-"):
                if current is not None:
                    items.append(current)
                body = stripped[1:].strip()
                if not body:
                    current = {}
                elif ":" in body:
                    key, _, value = body.partition(":")
                    current = {key.strip(): SkillLoader._parse_yaml_scalar(value.strip())}
                else:
                    current = SkillLoader._parse_yaml_scalar(body)
                continue
            if isinstance(current, dict) and ":" in stripped:
                key, _, value = stripped.partition(":")
                current[key.strip()] = SkillLoader._parse_yaml_scalar(value.strip())
        if current is not None:
            items.append(current)
        if items:
            return items
        return "\n".join(line.strip() for line in block if line.strip())

    @staticmethod
    def _parse_yaml_text_block(block: list[str], *, folded: bool) -> str:
        lines = [line.strip() for line in block]
        if folded:
            return " ".join(line for line in lines if line).strip()
        return "\n".join(lines).strip()

    @staticmethod
    def _to_list(val: Any) -> list[str]:
        """确保值为列表。"""
        if isinstance(val, list):
            return [str(item).strip() for item in val if isinstance(item, str) and str(item).strip()]
        if isinstance(val, str):
            if not val:
                return []
            return [item.strip() for item in val.split(",") if item.strip()]
        return []

    @classmethod
    def _first_list(cls, mapping: dict[str, Any], *keys: str) -> list[str]:
        for key in keys:
            if key in mapping:
                return cls._to_list(mapping.get(key))
        return []

    @classmethod
    def _first_hook_specs(cls, mapping: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, list):
                specs = [item for item in value if isinstance(item, dict)]
                if specs:
                    return specs
            elif isinstance(value, dict):
                return [value]
        return []

    @staticmethod
    def _to_bool(val: Any, *, default: bool) -> bool:
        """Parse YAML-ish booleans from the lightweight frontmatter parser."""
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in {"true", "yes", "1", "on"}:
                return True
            if normalized in {"false", "no", "0", "off"}:
                return False
        return default
