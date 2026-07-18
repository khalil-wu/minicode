"""
Skill 工具（DESIGN.md §5 / §8.2）。

  - load_skill:   激活一个 Skill，注入其指令到 context。权限: AUTO
  - unload_skill: 停用一个 Skill，从 context 中移除。权限: AUTO
  - list_skills:  列出所有可用 Skill 及其状态。权限: AUTO
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from backend.skills.names import normalize_skill_name
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext

logger = logging.getLogger(__name__)

SAFE_SKILL_TOOLS = frozenset({
    "read_file",
    "list_files",
    "grep_files",
    "glob_files",
    "search_files",
    "fuzzy_search",
    "git_status",
    "git_diff",
    "git_log",
    "go_to_definition",
    "find_references",
    "read_artifact",
    "web_search",
    "web_fetch",
    "tool_search",
    "tool_describe",
    "skill_search",
})

RISKY_SKILL_TOOL_PREFIXES = (
    "run_",
    "terminal_",
    "write_",
    "edit_",
    "save_",
    "remember_",
    "git_commit",
    "git_push",
    "git_stage",
    "git_unstage",
    "worktree_",
    "notebook_",
    "schedule_",
)


def _skill_load_permission(manager: Any, skill_name: str) -> PermissionLevel:
    """Classify loading a skill from its declared frontmatter capabilities."""
    if not skill_name or manager is None:
        return PermissionLevel.AUTO
    get_meta = getattr(manager, "get_meta", None)
    if get_meta is None:
        return PermissionLevel.CONFIRM
    meta = get_meta(skill_name)
    if meta is None:
        return PermissionLevel.AUTO

    declared_tools = [str(item).strip().lower() for item in getattr(meta, "tools_required", []) if str(item).strip()]
    declared_hooks = [str(item).strip() for item in getattr(meta, "hooks_required", []) if str(item).strip()]
    declared_temporary_hooks = [
        item
        for item in getattr(meta, "temporary_hooks", [])
        if isinstance(item, dict) and str(item.get("command") or "").strip()
    ]
    declared_shell = [str(item).strip() for item in getattr(meta, "shell_commands", []) if str(item).strip()]
    declared_mcp = [
        str(item).strip()
        for item in [
            *list(getattr(meta, "mcp_required", []) or []),
            *list(getattr(meta, "mcp_dependencies", []) or []),
        ]
        if str(item).strip()
    ]

    if declared_hooks or declared_temporary_hooks or declared_shell or declared_mcp:
        return PermissionLevel.CONFIRM
    if any(not _is_safe_skill_tool(tool_name) for tool_name in declared_tools):
        return PermissionLevel.CONFIRM
    return PermissionLevel.AUTO


def _is_safe_skill_tool(tool_name: str) -> bool:
    if tool_name in SAFE_SKILL_TOOLS:
        return True
    if any(tool_name.startswith(prefix) for prefix in RISKY_SKILL_TOOL_PREFIXES):
        return False
    if "shell" in tool_name or "hook" in tool_name:
        return False
    return tool_name.startswith(("read_", "list_", "grep_", "glob_", "search_", "find_"))


class LoadSkillTool(BaseTool):
    """
    激活一个 Skill。

    将 Skill 的 SKILL.md 指令注入到 Agent 的 active context 中。
    权限: AUTO
    """

    name = "load_skill"
    result_kind = "skill"
    activity_kind = "genericTool"
    display_scope = "silent"
    display_label = "Load skill"
    panel_hint = ""
    description = (
        "Activate a Skill and inject its SKILL.md workflow instructions into the current context.\n\n"
        "When to use: the user explicitly names a skill, slash command, or $skill; or an available skill name/"
        "description clearly matches the task. Matching skills are a blocking requirement: call load_skill "
        "before substantive guidance, implementation, or claiming you are using that skill.\n\n"
        "When not to use: no listed/known skill matches, the task is better handled by directly available tools, "
        "or the skill is already active in context.\n\n"
        "Never mention a skill as used or available for the task unless it has been activated or is already active. "
        "Use list_skills only when discovery is needed."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        """懒获取 SkillManager（避免循环导入）。"""
        if self._skill_manager is not None:
            return self._skill_manager
        # 延迟从全局获取
        try:
            from backend.api import _state
            return _state.bootstrap.skill_manager if _state.bootstrap else None
        except Exception:
            return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["skill_name"],
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": (
                            "Exact skill name to activate, for example 'code_review', 'debugging', or 'refactor'. "
                            "Use when that skill was explicitly requested or clearly matches the task."
                        ),
                    },
                },
            },
        )

    def check_permission(self, args: dict[str, Any] | None = None, context=None) -> PermissionLevel | None:
        skill_name = normalize_skill_name((args or {}).get("skill_name"))
        return _skill_load_permission(self._get_manager(), skill_name)

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        skill_name = normalize_skill_name(args.get("skill_name"))
        if not skill_name:
            return self._error_result("缺少 skill_name 参数")

        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")

        success = manager.activate(skill_name)
        if success:
            active_names = manager.get_active_names()
            return self._success_result(
                content=f"Skill '{skill_name}' 已激活。当前活跃 Skills: {', '.join(active_names)}"
            )
        else:
            return self._error_result(f"Skill '{skill_name}' 激活失败，可能不存在")


class UnloadSkillTool(BaseTool):
    """
    停用一个 Skill。

    从 Agent 的 active context 中移除 Skill 指令。
    权限: AUTO
    """

    name = "unload_skill"
    result_kind = "skill"
    activity_kind = "genericTool"
    display_scope = "silent"
    display_label = "Unload skill"
    panel_hint = ""
    description = (
        "Deactivate an already active Skill and remove its workflow instructions from the current context. "
        "Use only when the user asks to stop using a skill or when the active skill is no longer relevant."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        if self._skill_manager is not None:
            return self._skill_manager
        try:
            from backend.api import _state
            return _state.bootstrap.skill_manager if _state.bootstrap else None
        except Exception:
            return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["skill_name"],
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要停用的 Skill 名称",
                    },
                },
            },
        )

    def check_permission(self, args: dict[str, Any] | None = None, context=None) -> PermissionLevel | None:
        return PermissionLevel.AUTO

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        skill_name = normalize_skill_name(args.get("skill_name"))
        if not skill_name:
            return self._error_result("缺少 skill_name 参数")

        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")

        success = manager.deactivate(skill_name)
        if success:
            active_names = manager.get_active_names()
            return self._success_result(
                content=f"Skill '{skill_name}' 已停用。当前活跃 Skills: {', '.join(active_names) or '(无)'}"
            )
        else:
            return self._error_result(f"Skill '{skill_name}' 未激活，无需停用")


class ListSkillsTool(BaseTool):
    """
    列出所有可用 Skill 及其状态。

    权限: AUTO
    """

    name = "list_skills"
    result_kind = "skill"
    activity_kind = "genericTool"
    display_scope = "silent"
    display_label = "List skills"
    panel_hint = ""
    description = (
        "List available Skills, their descriptions, and active state for discovery. "
        "Use this when you need to decide whether a matching skill exists or avoid duplicate activation. "
        "If a skill clearly matches after discovery, call load_skill before doing substantive task work."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        if self._skill_manager is not None:
            return self._skill_manager
        try:
            from backend.api import _state
            return _state.bootstrap.skill_manager if _state.bootstrap else None
        except Exception:
            return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": [],
                "properties": {},
            },
        )

    def check_permission(self, args: dict[str, Any] | None = None, context=None) -> PermissionLevel | None:
        return PermissionLevel.AUTO

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")

        skills = manager.list_all()
        if not skills:
            return self._success_result(content="暂无可用 Skill")

        lines = ["可用 Skills:"]
        for s in skills:
            status = "✅ 已激活" if s.get("active") else "⬜ 未激活"
            when = f" | When to use: {s.get('when_to_use')}" if s.get("when_to_use") else ""
            lines.append(f"  {status} {s['name']}: {s.get('description', '无描述')}{when}")

        return self._success_result(content="\n".join(lines))


class SkillSearchTool(BaseTool):
    """Search Skills by name, description, triggers, and when-to-use text."""

    name = "skill_search"
    result_kind = "skill"
    activity_kind = "genericTool"
    display_scope = "silent"
    display_label = "Search skills"
    panel_hint = ""
    description = (
        "Search available Skills by task, domain, trigger words, or exact name. "
        "Use this before loading a skill when discovery is needed. "
        "If a result clearly matches the user request, activate it with load_skill using the exact skill_name."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    side_effect_kind = "none"
    idempotent = True

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        if self._skill_manager is not None:
            return self._skill_manager
        try:
            from backend.api import _state
            return _state.bootstrap.skill_manager if _state.bootstrap else None
        except Exception:
            return None

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="skill.search",
            toolset="skill",
            exposure="core",
            required_args=(),
            arg_roles={"query": "search_query", "limit": "control"},
            repair_policy={"query": "resource_resolver"},
            empty_args_policy="repair_or_block",
            blocked_guidance="Provide a concise task/domain query, or omit query to list top available skills.",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Task, domain, trigger, or exact skill name to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum results to return. Defaults to 8.",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            return self._error_result("limit must be an integer")
        limit = max(1, min(20, limit))
        query = str(args.get("query") or "").strip()
        skills = [item for item in (manager.list_all() or []) if isinstance(item, dict)]
        if not skills:
            return self._success_result(content="No available Skills.")

        tokens = _skill_query_tokens(query)
        scored = [(_score_skill(skill, tokens), skill) for skill in skills]
        if tokens:
            scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: (-item[0], str(item[1].get("name") or "")))

        if not scored:
            return self._success_result(
                content=(
                    f"No Skills matched query: {query}\n"
                    "Use list_skills for a full inventory if you need broader discovery."
                )
            )

        header = f"Skill search results for: {query}" if query else "Available Skills"
        lines = [header]
        for index, (score, skill) in enumerate(scored[:limit], 1):
            name = str(skill.get("name") or "").strip()
            display_name = str(skill.get("display_name") or skill.get("title") or "").strip()
            active = "active" if skill.get("active") else "inactive"
            label = f"{name} ({display_name})" if display_name and display_name != name else name
            score_part = f" score={score}" if tokens else ""
            lines.append(f"{index}. {label} [{active}]{score_part}")
            description = str(skill.get("description") or "").strip()
            if description:
                lines.append(f"   description: {description}")
            when = str(skill.get("when_to_use") or "").strip()
            if when:
                lines.append(f"   when_to_use: {when}")
            triggers = _string_list(skill.get("triggers") or skill.get("keywords"))
            if triggers:
                lines.append(f"   triggers: {', '.join(triggers[:8])}")
        lines.append("Use load_skill with the exact skill_name before applying a matching workflow.")
        return self._success_result(content="\n".join(lines))


class SkillTool(BaseTool):
    """Single progressive Skill tool.

    Empty args list skills. Passing skill_name loads that skill by default.
    action="unload" deactivates an active skill. The legacy load_skill,
    unload_skill, and list_skills tools remain registered for compatibility.
    """

    name = "skill"
    result_kind = "skill"
    activity_kind = "genericTool"
    display_scope = "silent"
    display_label = "Skill"
    panel_hint = ""
    description = (
        "List, activate, or deactivate Skills. With no arguments, list available Skills. "
        "With skill_name, activate that Skill unless action='unload'. Use only when a skill is explicitly requested, "
        "clearly matches the task, or discovery is needed before choosing one."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, skill_manager=None) -> None:
        self._skill_manager = skill_manager

    def _get_manager(self):
        if self._skill_manager is not None:
            return self._skill_manager
        try:
            from backend.api import _state
            return _state.bootstrap.skill_manager if _state.bootstrap else None
        except Exception:
            return None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": [],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "load", "unload"],
                        "description": "Optional operation. Defaults to list when skill_name is absent, otherwise load.",
                    },
                    "skill_name": {
                        "type": "string",
                        "description": "Exact skill name to activate or deactivate.",
                    },
                },
            },
        )

    def check_permission(self, args: dict[str, Any] | None = None, context=None) -> PermissionLevel | None:
        action = str((args or {}).get("action") or "").strip().lower()
        skill_name = normalize_skill_name((args or {}).get("skill_name"))
        if not action:
            action = "load" if skill_name else "list"
        if action == "load":
            return _skill_load_permission(self._get_manager(), skill_name)
        return PermissionLevel.AUTO

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        manager = self._get_manager()
        if not manager:
            return self._error_result("SkillManager 未初始化")

        action = str(args.get("action") or "").strip().lower()
        skill_name = normalize_skill_name(args.get("skill_name"))
        if not action:
            action = "load" if skill_name else "list"

        if action == "list":
            skills = manager.list_all()
            if not skills:
                return self._success_result(content="暂无可用 Skill")
            lines = ["可用 Skills:"]
            for s in skills:
                status = "已激活" if s.get("active") else "未激活"
                when = f" | When to use: {s.get('when_to_use')}" if s.get("when_to_use") else ""
                lines.append(f"  [{status}] {s['name']}: {s.get('description', '无描述')}{when}")
            return self._success_result(content="\n".join(lines))

        if action not in {"load", "unload"}:
            return self._error_result("action 必须是 list、load 或 unload")
        if not skill_name:
            return self._error_result("缺少 skill_name 参数")

        if action == "load":
            success = manager.activate(skill_name)
            if success:
                active_names = manager.get_active_names()
                return self._success_result(
                    content=f"Skill '{skill_name}' 已激活。当前活跃 Skills: {', '.join(active_names)}"
                )
            return self._error_result(f"Skill '{skill_name}' 激活失败，可能不存在")

        success = manager.deactivate(skill_name)
        if success:
            active_names = manager.get_active_names()
            return self._success_result(
                content=f"Skill '{skill_name}' 已停用。当前活跃 Skills: {', '.join(active_names) or '(无)'}"
            )
        return self._error_result(f"Skill '{skill_name}' 未激活，无需停用")


def _skill_query_tokens(query: str) -> list[str]:
    return [token for token in re.split(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", query.lower()) if token]


def _string_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in re.split(r"[,;\n]+", raw) if part.strip()]
    return []


def _score_skill(skill: dict[str, Any], tokens: list[str]) -> int:
    if not tokens:
        return 0
    name = str(skill.get("name") or "").lower()
    display_name = str(skill.get("display_name") or skill.get("title") or "").lower()
    description = str(skill.get("description") or "").lower()
    when = str(skill.get("when_to_use") or "").lower()
    triggers = " ".join(_string_list(skill.get("triggers") or skill.get("keywords"))).lower()
    haystack = " ".join([name, display_name, description, when, triggers])
    score = 0
    for token in tokens:
        if token == name:
            score += 20
        elif token in name:
            score += 10
        if display_name and token in display_name:
            score += 7
        if triggers and token in triggers:
            score += 7
        if when and token in when:
            score += 5
        if description and token in description:
            score += 3
        if token in haystack:
            score += 1
    if all(token in haystack for token in tokens):
        score += 6
    return score
