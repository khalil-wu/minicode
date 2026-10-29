"""MiniCode Skill discovery and turn-scoped selection."""

from __future__ import annotations

import logging
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.skills.loader import SkillLoader, SkillFull, SkillMeta
from backend.skills.errors import RequiredSkillUnavailableError

logger = logging.getLogger(__name__)
_KEEP_PROJECT_ROOT = object()

@dataclass(frozen=True)
class SkillDetection:
    name: str
    trigger_mode: str
    reason: str
    source_path: str = ""
    unavailable_reason: str = ""


class SkillManager:
    """Discover Skills and stage exact SKILL.md files for the current turn."""

    def __init__(
        self,
        loader: SkillLoader | None = None,
        runtime_roots=None,
    ) -> None:
        self._loader = loader or SkillLoader()
        self._discovered = False
        self._is_snapshot = False
        self.executor_catalog = None
        self._implicit_invocations: set[tuple[str, str, str, str]] = set()
        self._implicit_invocation_turn_id = None
        self.runtime_roots = runtime_roots
        self._runtime_roots_revision = -1

    def _ensure_discovered(self) -> None:
        if self.runtime_roots is not None and self._runtime_roots_revision != self.runtime_roots.revision:
            self._discovered = False
        if not self._discovered:
            self.discover()

    def attach_executor_catalog(self, catalog) -> None:
        self.executor_catalog = catalog

    def clone_host_catalog(self) -> SkillManager:
        self._ensure_discovered()
        clone = SkillManager(deepcopy(self._loader))
        clone._discovered = self._discovered
        clone._is_snapshot = True
        clone.runtime_roots = self.runtime_roots
        clone._runtime_roots_revision = self._runtime_roots_revision
        clone.executor_catalog = self.executor_catalog
        clone._implicit_invocations = set(self._implicit_invocations)
        clone._implicit_invocation_turn_id = self._implicit_invocation_turn_id
        return clone

    def record_implicit_invocation(self, turn_id, invocation) -> bool:
        if self._implicit_invocation_turn_id != turn_id:
            self._implicit_invocations.clear()
            self._implicit_invocation_turn_id = turn_id
        key = (invocation.source_kind, invocation.scope or "", invocation.resource,
            invocation.name if invocation.source_kind == "host" else "")
        if key in self._implicit_invocations:
            return False
        self._implicit_invocations.add(key)
        return True

    def validate_required_skills(self, requirements) -> None:
        if self.executor_catalog is not None:
            self.executor_catalog.validate_required_skills(requirements)
            return
        for requirement in requirements:
            for name in requirement.skill_names:
                raise RequiredSkillUnavailableError(f"Required skill {json.dumps(name, ensure_ascii=False)} from environment {json.dumps(requirement.environment_id, ensure_ascii=False)} is unavailable")

    def discover(self) -> list[SkillMeta]:
        """
        扫描并发现所有可用的 Skills。

        Returns:
            所有 Skill 的元数据列表
        """
        if self.runtime_roots is not None:
            self._loader.set_extra_roots(self.runtime_roots.roots)
            self._runtime_roots_revision = self.runtime_roots.revision
        skills = self._loader.discover()
        self._discovered = True
        return skills

    def set_project_root(self, project_root: Path | str | None) -> None:
        """Switch skill discovery with the active workspace session."""
        self._loader.set_project_root(project_root)
        self._discovered = False
        self.discover()

    def adopt_project_snapshot(self, snapshot: SkillManager) -> None:
        """Publish a discovered workspace catalog without changing its owner."""
        self._loader = snapshot._loader
        self._discovered = snapshot._discovered

    def snapshot(self, project_root: Path | str | None | object = _KEEP_PROJECT_ROOT, *, role_config: dict[str, Any] | None = None, role_features: dict[str, bool] | None = None, config_stack: Any = None, force_reload: bool = False) -> SkillManager:
        """Capture discovery for a task without rebinding the window's loader."""
        self._ensure_discovered()
        loader = deepcopy(self._loader)
        snapshot = SkillManager(loader)
        snapshot._discovered = self._discovered
        snapshot._is_snapshot = True
        if self.executor_catalog is not None:
            snapshot._implicit_invocations = set(self._implicit_invocations)
            snapshot._implicit_invocation_turn_id = self._implicit_invocation_turn_id
        if isinstance(loader, SkillLoader) and project_root is not _KEEP_PROJECT_ROOT:
            loader.set_project_root(project_root)
        if isinstance(loader, SkillLoader):
            config_changed = config_stack is not None and (
                loader._config_stack is None or loader._config_stack.fingerprint != config_stack.fingerprint
            )
            if config_stack is not None:
                loader._config_stack = config_stack
            role_changed = loader.restrict_role(role_config or {}, role_features or {})
            if (self._is_snapshot or self.executor_catalog is not None) and loader._discovered and not config_changed and not force_reload:
                # One turn owns one discovered host inventory. Later prompt,
                # role and executor projections narrow that captured scan.
                if role_changed:
                    loader.restrict_discovered_catalog()
                snapshot.attach_executor_catalog(self.executor_catalog)
                return snapshot
        # Discovery belongs to the turn boundary. A new turn sees installed,
        # edited and removed skills; the in-flight snapshot remains stable.
        snapshot.discover()
        snapshot.attach_executor_catalog(self.executor_catalog)
        return snapshot

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
             3. 普通匹配由 MiniCode 的 available-skills 指令引导模型
             读取对应 SKILL.md，不增加私有 skill 工具协议

        Args:
            user_message: 用户消息

        Returns:
            应该激活的 Skill 名称列表
        """
        self._ensure_discovered()

        mentioned_names, mentioned_paths = _extract_skill_mentions(user_message)
        candidates: list[SkillDetection] = []
        selected_paths: set[str] = set()
        selected_names: set[str] = set()

        for selected in selected_skills or []:
            if not isinstance(selected, dict):
                continue
            name = str(selected.get("name") or "").strip()
            source_path = str(selected.get("path") or "").strip()
            selected_names.add(name.casefold())
            meta = self._loader.get_meta_by_path(source_path)
            if meta is None:
                candidates.append(SkillDetection(
                    name=name,
                    trigger_mode="explicit",
                    reason=f"用户显式选择 ${name}",
                    source_path=source_path,
                ))
                continue
            if not meta.user_invocable:
                candidates.append(SkillDetection(
                    name=name or meta.name,
                    trigger_mode="explicit",
                    reason=f"用户显式选择 ${name or meta.name}",
                    source_path=source_path,
                    unavailable_reason="This Skill no longer allows explicit invocation.",
                ))
                continue
            key = self._skill_key(meta.source_path)
            if key in selected_paths:
                continue
            selected_paths.add(key)
            selected_names.add(meta.name.casefold())
            candidates.append(SkillDetection(
                name=meta.name,
                trigger_mode="explicit",
                reason=f"用户显式选择 ${meta.name}",
                source_path=str(meta.source_path),
            ))

        # Linked mentions select the exact discovery path even when names collide.
        # App, MCP and plugin links never become plain Skill-name invocations.
        for meta in self._loader.list_metas():
            key = self._skill_key(meta.source_path)
            if not meta.user_invocable or key in selected_paths or key not in mentioned_paths:
                continue
            selected_paths.add(key)
            selected_names.add(meta.name.casefold())
            candidates.append(SkillDetection(
                name=meta.name,
                trigger_mode="explicit",
                reason=f"用户显式选择 Skill 路径 {meta.source_path}",
                source_path=str(meta.source_path),
            ))

        all_skills = self._loader.list_skill_names()
        for name in all_skills:
            if name.casefold() in selected_names:
                continue
            meta = self._loader.get_invocation_meta(name)
            if meta is None:
                continue
            if not meta.user_invocable:
                continue
            if self._skill_key(meta.source_path) in selected_paths:
                continue

            variants = {name.casefold(), name.casefold().replace("_", "-"), name.casefold().replace("-", "_")}
            if mentioned_names & variants:
                candidates.append(SkillDetection(
                    name=name,
                    trigger_mode="explicit",
                    reason=f"用户显式调用 ${name} 或 /{name}",
                    source_path=str(meta.source_path),
                ))
                continue

        return candidates

    def load_skill_payload(
        self,
        skill_name: str,
        source_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Load one exact SKILL.md as a turn-owned contextual payload."""
        self._ensure_discovered()

        meta = self._loader.get_meta_by_path(source_path) if source_path else self._loader.get_invocation_meta(skill_name)
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
        content = full.raw_content or full.content
        warnings = []
        if meta.agent_plugin and len(content.encode("utf-8")) > 8_000:
            from backend.skills.executor import utf8_prefix

            content = utf8_prefix(content, 8_000)
            warning = f"Skill `{meta.name}` exceeded the main prompt context limit and was truncated."
            logger.warning(warning)
            warnings.append(warning)
        return {
            "name": meta.name,
            "path": str(meta.source_path),
            "source_level": meta.source_level,
            "description": meta.description,
            "mcp_dependencies": list(meta.mcp_dependencies),
            "mcp_dependency_specs": [dict(spec) for spec in meta.mcp_dependency_specs],
            "content": content,
            "token_estimate": (len(content.encode("utf-8")) + 3) // 4 if warnings else full.token_estimate,
            **({"warnings": warnings} if warnings else {}),
        }

    def get_layer1_summary(self) -> str:
        """
        获取所有 Skill 的 Layer 1 摘要（始终注入 context）。

        让 LLM 知道有哪些 Skill 可用。
        """
        self._ensure_discovered()
        return self._loader.get_all_layer1()

    @property
    def include_instructions(self) -> bool:
        self._ensure_discovered()
        return self._loader.skills_configuration.include_instructions

    @property
    def max_context_tokens(self) -> int | None:
        self._ensure_discovered()
        return self._loader.skills_configuration.max_context_tokens

    def list_metas(self) -> list[SkillMeta]:
        """Expose the discovered catalog for budget-aware prompt rendering."""
        self._ensure_discovered()
        return self._loader.list_metas()

    def get_meta_by_path(self, source_path: str | Path) -> SkillMeta | None:
        self._ensure_discovered()
        return self._loader.get_meta_by_path(source_path)

    def readable_roots(self) -> list[Path]:
        """Return the exact discovered Skill directories as read-only roots.

        The catalog exposes absolute Skill locations so the model can read the
        selected ``SKILL.md`` and its referenced files. Keep that access limited
        to directories already admitted by discovery; disabled or malformed
        plugins therefore never become readable roots.
        """
        self._ensure_discovered()
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
        self._ensure_discovered()
        return self._loader.get_meta(skill_name)

    def resolve_asset(self, source_path: str | Path, variant: str) -> Path | None:
        """Resolve an icon only from an exact Skill discovered by this manager."""
        self._ensure_discovered()
        meta = self._loader.get_meta_by_path(source_path)
        if meta is None:
            key = self._loader._path_key(Path(source_path))
            meta = next((meta for meta, _ in self._loader.list_settings_metas()
                if self._loader._path_key(meta.source_path) == key), None)
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
        self._ensure_discovered()

        return [self._public_meta(meta) for meta in self._loader.list_metas()]

    def list_settings(self, *, source_level: str = "user") -> list[dict[str, Any]]:
        self._ensure_discovered()
        return [{**self._public_meta(meta), "enabled": enabled}
            for meta, enabled in self._loader.list_settings_metas() if meta.source_level == source_level]

    def read_skill_detail(self, source_path: str | Path) -> dict[str, Any] | None:
        self._ensure_discovered()
        loaded = self._loader.load_settings_full(source_path)
        if loaded is None:
            return None
        full, enabled = loaded
        return {**self._public_meta(full.meta), "enabled": enabled, "content": full.content,
            "raw_content": full.raw_content}

    @staticmethod
    def _public_meta(meta: SkillMeta) -> dict[str, Any]:
        return {
            "name": meta.name,
            "description": meta.description,
            "display_name": meta.display_name,
            "short_description": meta.short_description,
            "icon": meta.icon,
            "icon_large": meta.icon_large,
            "brand_color": meta.brand_color,
            "path": str(meta.source_path),
            "active": False,
            "level": meta.source_level,
            "source_level": meta.source_level,
            "plugin_enabled": meta.plugin_enabled,
            "mcp_dependencies": meta.mcp_dependencies,
            "allow_implicit_invocation": meta.allow_implicit_invocation,
            "user_invocable": meta.user_invocable,
            "default_prompt": meta.default_prompt,
        }

    @staticmethod
    def _skill_key(source_path: Path | str) -> str:
        path = Path(source_path)
        try:
            path = path.resolve()
        except OSError:
            path = path.absolute()
        return str(path)

_LINKED_MENTION_RE = re.compile(r"\[\$([A-Za-z0-9_:.-]+)\]\s*\(([^)]+)\)")
_PLAIN_MENTION_RE = re.compile(r"(?<![\w.-])[$/]([A-Za-z0-9_:.-]+)(?![\w.-])")
_ENV_VAR_NAMES = {"path", "home", "user", "shell", "pwd", "tmpdir", "temp", "tmp", "lang", "term", "xdg_config_home"}


def _extract_skill_mentions(message: str) -> tuple[set[str], set[str]]:
    paths: set[str] = set()

    def linked(match: re.Match[str]) -> str:
        path = match.group(2).strip()
        if match.group(1).casefold() not in _ENV_VAR_NAMES and not path.startswith(("app://", "mcp://", "plugin://")):
            paths.add(SkillManager._skill_key(path.removeprefix("skill://")))
        return " "

    plain_text = _LINKED_MENTION_RE.sub(linked, message)
    names = {match.group(1).casefold() for match in _PLAIN_MENTION_RE.finditer(plain_text)}
    return names - _ENV_VAR_NAMES, paths
