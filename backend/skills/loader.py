"""MiniCode progressive-disclosure Skill loader.

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

from backend.agent.instruction_discovery import _get_managed_minicode_dir
from backend.agent.markdown_scopes import get_minicode_config_home_dir
from backend.config import PROJECT_ROOT, STATE_ROOT
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
    user_invocable: bool = True
    default_prompt: str = ""
    source_path: Path = field(default_factory=lambda: Path("."))
    source_level: str = "builtin"  # managed / plugin / user / workspace / builtin

    def to_layer1_summary(self) -> str:
        """Render MiniCode's absolute-path catalog line."""
        title = f"{self.name} ({self.display_name})" if self.display_name else self.name
        policy = "" if self.allow_implicit_invocation else " [explicit only]"
        path = str(self.source_path).replace("\\", "/")
        return f"- {title}: {self.description}{policy} (file: {path})"


@dataclass
class SkillFull:
    """完整 Skill 数据（Layer 1 + Layer 2）。"""
    meta: SkillMeta
    content: str  # Layer 2: SKILL.md 正文（去掉 frontmatter）
    raw_content: str = ""  # 完整 SKILL.md；显式调用时原样注入

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
        plugin_only = self._plugin_only_customization()
        workspace_ancestors = self._workspace_ancestors()
        dirs.append(("managed", _get_managed_minicode_dir() / "skills"))
        dirs.extend(self._plugin_search_dirs())
        dirs.append(("user", get_minicode_config_home_dir() / "skills"))
        dirs.extend(("workspace", path / ".minicode" / "skills") for path in workspace_ancestors)
        dirs.append(("builtin", PROJECT_ROOT / "skills"))

        if plugin_only:
            dirs = [
                (level, path)
                for level, path in dirs
                if level.startswith("plugin")
                or level in {"managed", "builtin"}
            ]
        return self._dedupe_search_dirs(dirs)

    @staticmethod
    def _dedupe_search_dirs(dirs: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
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

    def _plugin_only_customization(self) -> bool:
        try:
            from backend.config import load_config_layer_stack

            requirements = load_config_layer_stack(cwd=self._project_root).requirements
            return requirements.restricts_customization_to_plugins("skills")
        except Exception:
            # A managed policy read failure must never silently widen the
            # search surface to user/project Skills.  Fail closed and leave a
            # diagnostic trail for operators.
            logger.warning(
                "Unable to determine managed plugin-only Skills policy; restricting to managed/plugin Skills",
                exc_info=True,
            )
            return True

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
            from backend.plugins.manager import PluginManager
        except Exception:
            return []

        dirs: list[tuple[str, Path]] = []
        try:
            from backend.config import load_config_layer_stack

            stack = load_config_layer_stack(cwd=self._project_root)
            snapshot = PluginManager(config_stack=stack).snapshot()
        except Exception:
            logger.warning("Unified plugin snapshot unavailable for Skills", exc_info=True)
            return []

        owned_roots = [
            (STATE_ROOT / "extensions" / "plugins").resolve(),
        ]
        explicit_roots = str(os.environ.get("MINICODE_PLUGINS_DIR") or "").strip()
        if explicit_roots:
            owned_roots.extend(Path(part).expanduser().resolve() for part in explicit_roots.split(os.pathsep) if part.strip())

        for plugin in snapshot.enabled_plugins:
            plugin_dir = Path(str(plugin.get("path") or ""))
            plugin_name = str(plugin.get("name") or plugin.get("id") or plugin_dir.name).strip()
            if not plugin_name or not plugin_dir.is_dir():
                continue
            resolved_plugin_dir = plugin_dir.resolve()
            if not any(
                resolved_plugin_dir == root or root in resolved_plugin_dir.parents for root in owned_roots
            ):
                continue
            manifest_paths = [Path(str(path)) for path in plugin.get("manifest_paths", [])]
            if not manifest_paths:
                manifest_paths = [plugin_dir / ".minicode-plugin" / "plugin.json"]
            skills_dirs: list[Path] = []
            for manifest_path in manifest_paths:
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
                    continue
            if not skills_dirs:
                skills_dirs.append(plugin_dir / "skills")
            for skills_dir in skills_dirs:
                if skills_dir.is_dir():
                    dirs.append((f"plugin:{plugin.get('id') or plugin_name}", skills_dir))
        return dirs

    @staticmethod
    def _candidate_plugin_dirs(root: Path) -> list[Path]:
        if (root / "skills").is_dir():
            return [root]
        candidates = [path for path in root.iterdir() if path.is_dir()]
        # Versioned marketplace caches may nest plugin roots three levels deep.
        cache_dir = root / "cache"
        if cache_dir.is_dir():
            candidates.extend(path for path in cache_dir.glob("*/*/*") if path.is_dir())
        return candidates

    def discover(self) -> list[SkillMeta]:
        """
        Recursively discover Agent Skills, stopping at each skill root.

        只加载 Layer 1（frontmatter 元数据），不读正文。
        搜索目录保持既定优先级；同名 Skill 通过绝对路径区分。

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

            skill_files = self._discover_skill_files(base_dir)
            for skill_file in skill_files:
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

    @staticmethod
    def _discover_skill_files(base_dir: Path) -> list[Path]:
        """Apply progressive-discovery semantics to one skill root.

        A directory containing ``SKILL.md`` is a complete skill and is not
        traversed further.  Container directories may nest skills (notably
        nested container layouts). Resolve
        directory identities so linked directory trees cannot create cycles.
        """
        pending = [base_dir]
        seen_dirs: set[str] = set()
        found: list[Path] = []
        excluded = {".git", ".hg", ".svn", "node_modules", "__pycache__"}

        while pending:
            current = pending.pop(0)
            try:
                identity = os.path.normcase(str(current.resolve()))
            except OSError:
                identity = os.path.normcase(str(current.absolute()))
            if identity in seen_dirs:
                continue
            seen_dirs.add(identity)

            skill_file = current / "SKILL.md"
            if skill_file.is_file():
                found.append(skill_file)
                continue

            try:
                children = sorted(
                    (
                        child
                        for child in current.iterdir()
                        if child.name not in excluded and child.is_dir()
                    ),
                    key=lambda path: path.name.casefold(),
                )
            except OSError:
                continue
            pending.extend(children)

        return found

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

    def get_invocation_meta(self, skill_name: str) -> SkillMeta | None:
        """Resolve an invocation only when its name has one canonical owner."""
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

        fm_match = re.match(r"^---\s*\n(.*?)\n---", raw, re.DOTALL)
        if not fm_match:
            logger.warning("Ignoring skill without YAML frontmatter: %s", skill_file)
            return None
        else:
            fm_text = fm_match.group(1)
            try:
                fm_payload = yaml.safe_load(fm_text)
            except yaml.YAMLError as exc:
                logger.warning("Ignoring skill with invalid YAML frontmatter %s: %s", skill_file, exc)
                return None
            else:
                if not isinstance(fm_payload, dict):
                    logger.warning("Ignoring skill with non-object YAML frontmatter: %s", skill_file)
                    return None
                else:
                    fm = fm_payload
        raw_name = fm.get("name")
        frontmatter_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else ""
        name = frontmatter_name or skill_file.parent.name
        description = fm.get("description", "")
        description = description.strip() if isinstance(description, str) else ""
        if not description:
            logger.warning("Ignoring skill without description metadata: %s", skill_file)
            return None
        for warning in self._skill_name_warnings(name, skill_file.parent.name):
            logger.warning("Skill metadata warning for %s: %s", skill_file, warning)
        if len(description) > 1024:
            logger.warning(
                "Skill metadata warning for %s: description exceeds 1024 characters (%d)",
                skill_file,
                len(description),
            )

        plugin_namespace = level.split(":", 1)[1] if level.startswith("plugin:") else ""
        qualified_name = f"{plugin_namespace}:{name}" if plugin_namespace else name
        agent_meta = self._load_skill_metadata(skill_file.parent)
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
        disable_model_invocation = self._to_bool(
            fm.get("disable-model-invocation", False),
            default=False,
        )

        return SkillMeta(
            name=qualified_name,
            description=description,
            display_name=(
                self._metadata_text(interface.get("display_name"))
            ),
            short_description=self._metadata_text(interface.get("short_description")),
            icon=self._safe_skill_asset(skill_file.parent, interface.get("icon_small")),
            icon_large=self._safe_skill_asset(skill_file.parent, interface.get("icon_large")),
            brand_color=self._metadata_text(interface.get("brand_color")),
            mcp_dependencies=mcp_dependencies,
            allow_implicit_invocation=(
                self._to_bool(policy.get("allow_implicit_invocation", True), default=True)
                and not disable_model_invocation
            ),
            user_invocable=self._to_bool(fm.get("user-invocable", True), default=True),
            default_prompt=self._metadata_text(interface.get("default_prompt")),
            source_path=skill_file,
            source_level="plugin" if plugin_namespace else level,
        )

    @staticmethod
    def _skill_name_warnings(name: str, parent_dir_name: str) -> list[str]:
        """Validate skill names diagnostically while preserving discoverability."""

        warnings: list[str] = []
        if name != parent_dir_name:
            warnings.append(f'name "{name}" does not match parent directory "{parent_dir_name}"')
        if len(name) > 64:
            warnings.append(f"name exceeds 64 characters ({len(name)})")
        if not re.fullmatch(r"[a-z0-9-]+", name):
            warnings.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
        if name.startswith("-") or name.endswith("-"):
            warnings.append("name must not start or end with a hyphen")
        if "--" in name:
            warnings.append("name must not contain consecutive hyphens")
        return warnings

    @staticmethod
    def _load_skill_metadata(skill_dir: Path) -> dict[str, Any]:
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
