"""Codex-compatible progressive-disclosure Skill loader.

SKILL.md owns the agent-visible name, description, and instructions.
Product metadata and MCP dependencies come from agents/openai.yaml.
"""

from __future__ import annotations

import logging
import os
import re
import json
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT
from backend.feature_flags import feature_enabled

logger = logging.getLogger(__name__)


@dataclass
class SkillMeta:
    """Skill 元数据（Layer 1）。"""
    name: str
    description: str
    display_name: str = ""
    short_description: str = ""
    icon: str = ""
    icon_large: str = ""
    brand_color: str = ""
    mcp_dependencies: list[str] = field(default_factory=list)
    allow_implicit_invocation: bool = True
    default_prompt: str = ""
    source_path: Path = field(default_factory=lambda: Path("."))
    source_level: str = "builtin"  # project / global / builtin

    def to_layer1_summary(self) -> str:
        """Render the Codex absolute-path catalog line."""
        title = f"{self.name} ({self.display_name})" if self.display_name else self.name
        policy = "" if self.allow_implicit_invocation else " [explicit only]"
        path = str(self.source_path).replace("\\", "/")
        return f"- {title}: {self.description}{policy} (file: {path})"


@dataclass
class SkillFull:
    """完整 Skill 数据（Layer 1 + Layer 2）。"""
    meta: SkillMeta
    content: str  # Layer 2: SKILL.md 正文（去掉 frontmatter）
    raw_content: str = ""  # 完整 SKILL.md；显式调用时按 Codex 方式注入

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

    def __init__(self, project_root: Path | str | None = None) -> None:
        self._project_root = self._normalize_project_root(project_root)
        self._cache: dict[str, SkillMeta] = {}
        self._catalog: list[SkillMeta] = []
        self._path_cache: dict[str, SkillMeta] = {}
        self._full_cache: dict[str, SkillFull] = {}

    def set_project_root(self, project_root: Path | str | None) -> None:
        """Rebind discovery to the active session workspace."""
        normalized = self._normalize_project_root(project_root)
        if normalized == self._project_root:
            return
        self._project_root = normalized
        self._cache.clear()
        self._catalog.clear()
        self._path_cache.clear()
        self._full_cache.clear()

    @staticmethod
    def _normalize_project_root(project_root: Path | str | None) -> Path | None:
        if project_root is None or not str(project_root).strip():
            return None
        path = Path(project_root).expanduser()
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def _search_dirs(self) -> list[tuple[str, Path]]:
        dirs: list[tuple[str, Path]] = []
        dirs.extend(("workspace", path / ".agents" / "skills") for path in self._workspace_ancestors())
        if self._project_root is not None:
            dirs.append(("project-legacy", self._project_root / ".codex" / "skills"))
        dirs.extend(self._plugin_search_dirs())
        dirs.append(("user", Path.home() / ".agents" / "skills"))
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
        dirs.append(("user-legacy", codex_home / "skills"))
        dirs.append(("builtin", PROJECT_ROOT / "skills"))

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

    def _workspace_ancestors(self) -> list[Path]:
        current = self._project_root
        if current is None:
            return []
        repository_root = current
        for candidate in (current, *current.parents):
            repository_root = candidate
            if any((candidate / marker).exists() for marker in (".git", ".hg", ".svn")):
                break
        ancestors: list[Path] = []
        for candidate in (current, *current.parents):
            ancestors.append(candidate)
            if candidate == repository_root:
                break
        return ancestors

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
                plugin_name = plugin_name_from_directory(plugin_dir).strip()
                if not plugin_name or plugin_name.casefold() in disabled:
                    continue
                manifest_path = plugin_dir / ".codex-plugin" / "plugin.json"
                if not manifest_path.is_file():
                    continue
                skills_dirs: list[Path] = []
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                    configured = raw.get("skills") if isinstance(raw, dict) else None
                    configured_paths = [configured] if isinstance(configured, str) else configured if isinstance(configured, list) else []
                    for configured_path in configured_paths:
                        if not isinstance(configured_path, str) or not configured_path.strip():
                            continue
                        candidate = (plugin_dir / configured_path).resolve()
                        candidate.relative_to(plugin_dir.resolve())
                        skills_dirs.append(candidate)
                except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
                    pass
                if not skills_dirs:
                    skills_dirs.append(plugin_dir / "skills")
                for skills_dir in skills_dirs:
                    if skills_dir.is_dir():
                        dirs.append((f"plugin:{plugin_name}", skills_dir))
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
        catalog: list[SkillMeta] = []
        primary_by_name: dict[str, SkillMeta] = {}
        seen_paths: set[str] = set()

        # Search roots are already ordered from nearest workspace scope to
        # user/system fallbacks. Keep same-name skills as distinct catalog
        # entries so an exact structured path can select either one.
        for level, base_dir in self._search_dirs():
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
                    path_key = self._path_key(meta.source_path)
                    if path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)
                    catalog.append(meta)
                    primary_by_name.setdefault(meta.name, meta)

        self._catalog = catalog
        self._cache = primary_by_name
        self._path_cache = {self._path_key(meta.source_path): meta for meta in catalog}
        self._full_cache.clear()
        logger.info("发现 %d 个 Skills: %s", len(catalog), ", ".join(meta.name for meta in catalog))
        return list(catalog)

    def load_full(self, skill_name: str, source_path: str | Path | None = None) -> SkillFull | None:
        """
        加载 Skill 的 Layer 2 内容。

        Args:
            skill_name: Skill 名称

        Returns:
            SkillFull（含完整正文）或 None
        """
        # 检查缓存
        # 需要先 discover
        if not self._catalog:
            self.discover()

        meta = self.get_meta_by_path(source_path) if source_path else self.get_unambiguous_meta(skill_name)
        if not meta:
            logger.warning("Skill '%s' 不存在或名称不唯一", skill_name)
            return None
        cache_key = self._path_key(meta.source_path)
        if cache_key in self._full_cache:
            return self._full_cache[cache_key]

        # 读取完整文件
        skill_file = meta.source_path
        try:
            raw = skill_file.read_text(encoding="utf-8")
            content = self._extract_body(raw)
        except (OSError, UnicodeDecodeError) as exc:
            logger.error("读取 %s 失败: %s", skill_file, exc)
            return None

        full = SkillFull(meta=meta, content=content, raw_content=raw)
        self._full_cache[cache_key] = full

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
        if not self._catalog:
            self.discover()

        if not self._catalog:
            return ""

        lines = []
        for meta in self._catalog:
            if not meta.allow_implicit_invocation:
                continue
            lines.append(meta.to_layer1_summary())
        return "\n".join(lines)

    def list_skill_names(self) -> list[str]:
        """列出所有 Skill 名称。"""
        if not self._catalog:
            self.discover()
        return list(dict.fromkeys(meta.name for meta in self._catalog))

    def list_metas(self) -> list[SkillMeta]:
        if not self._catalog:
            self.discover()
        return list(self._catalog)

    def get_metas(self, skill_name: str) -> list[SkillMeta]:
        if not self._catalog:
            self.discover()
        return [meta for meta in self._catalog if meta.name == skill_name]

    def get_unambiguous_meta(self, skill_name: str) -> SkillMeta | None:
        matches = self.get_metas(skill_name)
        return matches[0] if len(matches) == 1 else None

    def get_meta_by_path(self, source_path: str | Path | None) -> SkillMeta | None:
        if source_path is None:
            return None
        if not self._catalog:
            self.discover()
        return self._path_cache.get(self._path_key(Path(source_path)))

    def get_meta(self, skill_name: str) -> SkillMeta | None:
        """获取 Skill 元数据。"""
        if not self._cache:
            self.discover()
        return self._cache.get(skill_name)

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        return os.path.normcase(str(resolved))

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
            logger.warning("Ignoring skill without YAML frontmatter: %s", skill_file)
            return None

        fm_text = fm_match.group(1)
        try:
            fm_payload = yaml.safe_load(fm_text)
        except yaml.YAMLError as exc:
            logger.warning("Ignoring skill with invalid YAML frontmatter %s: %s", skill_file, exc)
            return None
        if not isinstance(fm_payload, dict):
            logger.warning("Ignoring skill with non-object YAML frontmatter: %s", skill_file)
            return None
        fm = fm_payload

        name = self._metadata_text(fm.get("name"), skill_file.parent.name)
        if not name:
            logger.warning("Ignoring skill with invalid name metadata: %s", skill_file)
            return None
        description = self._metadata_text(fm.get("description"))
        if not description:
            logger.warning("Ignoring skill without description metadata: %s", skill_file)
            return None
        if len(name) > 64 or len(description) > 1024:
            logger.warning("Ignoring skill with oversized metadata: %s", skill_file)
            return None

        plugin_namespace = level.split(":", 1)[1] if level.startswith("plugin:") else ""
        qualified_name = f"{plugin_namespace}:{name}" if plugin_namespace else name
        agent_meta = self._load_openai_metadata(skill_file.parent)
        interface = agent_meta.get("interface") if isinstance(agent_meta.get("interface"), dict) else {}
        policy = agent_meta.get("policy") if isinstance(agent_meta.get("policy"), dict) else {}
        dependencies = agent_meta.get("dependencies") if isinstance(agent_meta.get("dependencies"), dict) else {}
        dependency_tools = dependencies.get("tools") if isinstance(dependencies.get("tools"), list) else []
        mcp_dependencies = [
            str(item.get("value") or "").strip()
            for item in dependency_tools
            if isinstance(item, dict)
            and str(item.get("type") or "").strip().lower() == "mcp"
            and str(item.get("value") or "").strip()
        ]

        return SkillMeta(
            name=qualified_name,
            description=description,
            display_name=self._metadata_text(interface.get("display_name")),
            short_description=self._metadata_text(interface.get("short_description")),
            icon=self._safe_skill_asset(skill_file.parent, interface.get("icon_small")),
            icon_large=self._safe_skill_asset(skill_file.parent, interface.get("icon_large")),
            brand_color=self._metadata_text(interface.get("brand_color")),
            mcp_dependencies=mcp_dependencies,
            allow_implicit_invocation=self._to_bool(policy.get("allow_implicit_invocation", True), default=True),
            default_prompt=self._metadata_text(interface.get("default_prompt")),
            source_path=skill_file,
            source_level="plugin" if plugin_namespace else level,
        )

    @staticmethod
    def _load_openai_metadata(skill_dir: Path) -> dict[str, Any]:
        path = skill_dir / "agents" / "openai.yaml"
        if not path.is_file():
            return {}
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            logger.warning("Ignoring invalid skill metadata %s: %s", path, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _safe_skill_asset(skill_dir: Path, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        candidate = (skill_dir / value).resolve()
        try:
            candidate.relative_to(skill_dir.resolve())
        except ValueError:
            return ""
        return str(candidate) if candidate.is_file() else ""

    @staticmethod
    def _metadata_text(value: Any, fallback: str = "") -> str:
        """Return scalar frontmatter text without letting malformed YAML escape."""
        if value is None:
            return fallback
        if isinstance(value, (str, int, float, bool)):
            return str(value).strip()
        return ""

    @staticmethod
    def _extract_body(raw: str) -> str:
        """从 SKILL.md 提取正文（去掉 frontmatter）。"""
        # 去掉 frontmatter
        body = re.sub(r"^---\s*\n.*?\n---\s*\n?", "", raw, count=1, flags=re.DOTALL)
        return body.strip()

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
