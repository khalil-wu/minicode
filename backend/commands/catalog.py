from __future__ import annotations

from copy import deepcopy
import re
from pathlib import Path
from typing import Any

import yaml

from backend.agent.claude_md import _find_project_root, _get_managed_claude_dir
from backend.workspace.state import get_explicit_active_workspace_root

def _availability(
    *,
    kind: str = "always",
    scope: str = "session",
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": kind,
        "scope": scope,
    }
    if reason:
        payload["reason"] = reason
    return payload


_REVIEW_PROMPT = (
    "Review the current code changes (staged, unstaged, and untracked files) "
    "and provide prioritized findings."
)

_INIT_PROMPT = """Generate a file named AGENTS.md that serves as a contributor guide for this repository.
Before writing, check whether AGENTS.md already exists in the current working directory. If it does, do not overwrite or modify it.
Create a concise repository-specific guide with actionable sections for project structure, build/test/development commands, coding conventions, testing, and commit or pull-request guidance. Omit sections that do not apply and do not invent project facts."""


_BUILTIN_COMMAND_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "name": "conversation.create",
        "label": "conversation.create",
        "description": "Create a new conversation runtime.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.switch",
        "label": "conversation.switch",
        "description": "Switch the active conversation for the current session.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.list",
        "label": "conversation.list",
        "description": "List conversations and active conversation state.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.delete",
        "label": "conversation.delete",
        "description": "Delete a conversation and remove its transcript storage.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.archive",
        "label": "conversation.archive",
        "description": "Archive the active conversation.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.unarchive",
        "label": "conversation.unarchive",
        "description": "Restore an archived conversation.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.rename",
        "label": "conversation.rename",
        "description": "Rename a conversation.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.memory_mode.set",
        "label": "conversation.memory_mode.set",
        "description": "Update the active conversation memory mode.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.permission_mode.set",
        "label": "conversation.permission_mode.set",
        "description": "Update the active conversation permission mode.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.goal.set",
        "label": "conversation.goal.set",
        "description": "Set, inspect, pause, resume, or clear the active conversation goal.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.permission.rules.list",
        "label": "conversation.permission.rules.list",
        "description": "List permission rules for the active conversation.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.permission.rules.add",
        "label": "conversation.permission.rules.add",
        "description": "Add a permission rule to the active conversation.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "conversation.permission.rules.remove",
        "label": "conversation.permission.rules.remove",
        "description": "Remove a permission rule from the active conversation.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "session.tasks.inspect",
        "label": "session.tasks.inspect",
        "description": "Inspect the current session task runtime summary.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "session.status.inspect",
        "label": "session.status.inspect",
        "description": "Inspect current runtime, model, MCP, and skill status.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "session.usage.inspect",
        "label": "session.usage.inspect",
        "description": "Inspect current context plus process-runtime token and cost totals.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "session.permissions.inspect",
        "label": "session.permissions.inspect",
        "description": "Inspect current permission mode and session rules.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "runtime.capabilities.inspect",
        "label": "runtime.capabilities.inspect",
        "description": "Inspect current runtime tool, command, skill, MCP, and permission capability exposure.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "read_artifact",
        "label": "read_artifact",
        "description": "Read a stored artifact body by artifact id.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "interrupt",
        "label": "interrupt",
        "description": "Interrupt the active turn for the current session.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "llm.model.set",
        "label": "llm.model.set",
        "description": "Change the selected model for the current session.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
)


_COMPOSER_COMMAND_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "cmd-review",
        "name": "review",
        "command": "review",
        "label": "/review",
        "description": "审查当前项目并按严重级别列出问题",
        "template": _REVIEW_PROMPT,
        "search_text": "review audit code quality bug risk",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(),
    },
    {
        "id": "cmd-plan",
        "name": "plan",
        "command": "plan",
        "label": "/plan",
        "description": "进入只读 Plan 模式，先探索并提交实施方案",
        "template": "/plan",
        "search_text": "local legacy plan readonly permissions",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-permissions-local",
        "name": "permissions",
        "command": "permissions",
        "label": "/permissions",
        "description": "查看或切换权限模式，并管理规则",
        "template": "/permissions",
        "search_text": "local permissions mode rules list add remove deny override ask auto full access confirm bypass",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
        "args": [
            {"value": "default", "description": "Ask — 写操作需确认"},
            {"value": "confirm", "description": "Diff review — 改动需审查"},
            {"value": "auto", "description": "Auto — 自动执行常规操作"},
            {"value": "rules list", "description": "查看权限规则"},
        ],
    },
    {
        "id": "cmd-bypass",
        "name": "bypass",
        "command": "bypass",
        "label": "/bypass",
        "description": "Switch to Full access permission mode for advanced trusted sessions.",
        "template": "/bypass",
        "search_text": "local bypass full auto permissions dangerous advanced",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": False,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-effort",
        "name": "effort",
        "command": "effort",
        "label": "/effort",
        "description": "Set reasoning effort: none, minimal, low, medium, high, xhigh, or max.",
        "template": "/effort high",
        "search_text": "local effort reasoning none minimal low medium high xhigh max",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
        "args": [
            {"value": "none", "description": "关闭 reasoning（供应商支持时）"},
            {"value": "minimal", "description": "最少推理"},
            {"value": "low", "description": "最快，适合简单任务"},
            {"value": "medium", "description": "平衡速度与质量"},
            {"value": "high", "description": "更深入的推理"},
            {"value": "xhigh", "description": "OpenAI 官方最高推理档"},
            {"value": "max", "description": "最强推理，最慢"},
        ],
    },
    {
        "id": "cmd-skills-local",
        "name": "skills",
        "command": "skills",
        "label": "/skills",
        "description": "浏览并选择技能",
        "template": "/skills",
        "search_text": "local skills browse select install",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-model-local",
        "name": "model",
        "command": "model",
        "label": "/model",
        "description": "选择或配置当前模型",
        "template": "/model",
        "search_text": "local model provider reasoning",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-mcp-local",
        "name": "mcp",
        "command": "mcp",
        "label": "/mcp",
        "description": "管理 MCP 连接器和工具",
        "template": "/mcp",
        "search_text": "local mcp connectors tools",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-plugins-local",
        "name": "plugins",
        "command": "plugins",
        "label": "/plugins",
        "description": "管理本地 Codex 插件",
        "template": "/plugins",
        "search_text": "local plugins bundles skills mcp apps hooks",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-new-local",
        "name": "new",
        "command": "new",
        "label": "/new",
        "description": "新建空白会话",
        "template": "/new",
        "search_text": "local new conversation",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
        "args": [
            {"value": "none", "description": "新会话：不携带记忆"},
            {"value": "summary", "description": "新会话：携带摘要记忆"},
            {"value": "profile", "description": "新会话：携带画像记忆"},
        ],
    },
    {
        "id": "cmd-clear-local",
        "name": "clear",
        "command": "clear",
        "label": "/clear",
        "description": "清空输入框和上传草稿",
        "template": "/clear",
        "search_text": "local clear composer attachments",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-compact-local",
        "name": "compact",
        "command": "compact",
        "label": "/compact",
        "description": "手动压缩对话上下文以释放 token 预算",
        "template": "/compact",
        "search_text": "local compact context summary tokens",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-goal-local",
        "name": "goal",
        "command": "goal",
        "label": "/goal",
        "description": "Set or manage a persistent conversation goal.",
        "template": "/goal",
        "search_text": "local goal objective target pause resume clear codex",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
        "args": [
            {"value": "set", "description": "设定目标（后跟目标描述）"},
            {"value": "pause", "description": "暂停当前目标"},
            {"value": "resume", "description": "恢复目标"},
            {"value": "clear", "description": "清除目标"},
        ],
    },
    {
        "id": "cmd-memory-local",
        "name": "memory",
        "command": "memory",
        "label": "/memory",
        "description": "切换会话记忆模式",
        "template": "/memory summary",
        "search_text": "local memory mode none summary profile",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
        "args": [
            {"value": "none", "description": "不携带跨会话记忆"},
            {"value": "summary", "description": "携带会话摘要记忆"},
            {"value": "profile", "description": "携带完整用户画像记忆"},
        ],
    },
    {
        "id": "cmd-archive-local",
        "name": "archive",
        "command": "archive",
        "label": "/archive",
        "description": "归档当前会话",
        "template": "/archive",
        "search_text": "local archive conversation",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-unarchive-local",
        "name": "unarchive",
        "command": "unarchive",
        "label": "/unarchive",
        "description": "取消归档当前会话",
        "template": "/unarchive",
        "search_text": "local unarchive restore conversation",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-tasks-local",
        "name": "tasks",
        "command": "tasks",
        "label": "/tasks",
        "description": "查看当前会话任务状态",
        "template": "/tasks",
        "search_text": "local runtime tasks status running pending failed",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-status-local",
        "name": "status",
        "command": "status",
        "label": "/status",
        "description": "查看当前运行指标",
        "template": "/status",
        "search_text": "local runtime status metrics counters",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-usage-local",
        "name": "usage",
        "command": "usage",
        "label": "/usage",
        "description": "查看当前会话 token、上下文和成本用量",
        "template": "/usage",
        "search_text": "local usage cost token context budget",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-context-local",
        "name": "context",
        "command": "context",
        "label": "/context",
        "description": "查看上下文 token 用量和压缩阈值",
        "template": "/context",
        "search_text": "local context token budget compact",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-cost-local",
        "name": "cost",
        "command": "cost",
        "label": "/cost",
        "description": "查看当前上下文及本次应用运行期的成本和模型用量",
        "template": "/cost",
        "search_text": "local cost usage spend price",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-init-template",
        "name": "init",
        "command": "init",
        "label": "/init",
        "description": "分析代码库并生成 AGENTS.md 项目说明",
        "template": _INIT_PROMPT,
        "search_text": "template init agentsmd documentation bootstrap",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-help-local",
        "name": "help",
        "command": "help",
        "label": "/help",
        "description": "查看本地命令帮助",
        "template": "/help",
        "search_text": "local help commands",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-resume-local",
        "name": "resume",
        "command": "resume",
        "label": "/resume",
        "description": "从上次中断的任务恢复继续（断点续跑）",
        "template": "/resume",
        "search_text": "local resume checkpoint recover continue retry",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
)


def get_builtin_command_catalog() -> list[dict[str, Any]]:
    return [deepcopy(entry) for entry in _BUILTIN_COMMAND_CATALOG]


def get_builtin_command_names() -> list[str]:
    return [str(entry["name"]) for entry in _BUILTIN_COMMAND_CATALOG]


_COMMAND_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _project_command_dirs(workspace_root: Path | None) -> list[Path]:
    if workspace_root is None:
        return []
    current = workspace_root.resolve()
    boundary = _find_project_root(current)
    home = Path.home().resolve()
    result: list[Path] = []
    while current != home:
        directory = current / ".claude" / "commands"
        if directory.is_dir():
            result.append(directory)
        if boundary is not None and current == boundary:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return result


def _file_command_dirs(workspace_root: Path | None) -> list[tuple[Path, str]]:
    return [
        (_get_managed_claude_dir() / ".claude" / "commands", "policySettings"),
        *((path, "projectSettings") for path in _project_command_dirs(workspace_root)),
        (Path.home() / ".claude" / "commands", "userSettings"),
    ]


def _parse_file_command(path: Path, source: str) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    frontmatter: dict[str, Any] = {}
    match = _COMMAND_FRONTMATTER_RE.match(raw)
    if match:
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if isinstance(parsed, dict):
            frontmatter = parsed
        raw = raw[match.end():]
    template = raw.strip()
    if not template:
        return None
    name = path.stem.strip().lower()
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        description = next(
            (line.lstrip("# ").strip() for line in template.splitlines() if line.strip()),
            f"Run project command {name}",
        )[:160]
    argument_hint = str(
        frontmatter.get("argument-hint") or frontmatter.get("argument_hint") or ""
    ).strip()
    raw_argument_names = frontmatter.get("arguments")
    if isinstance(raw_argument_names, str):
        argument_names = [
            item for item in raw_argument_names.split()
            if item and not item.isdecimal()
        ]
    elif isinstance(raw_argument_names, list):
        argument_names = [
            str(item).strip() for item in raw_argument_names
            if str(item).strip() and not str(item).strip().isdecimal()
        ]
    else:
        argument_names = []
    user_invocable = frontmatter.get("user-invocable", True)
    if isinstance(user_invocable, str):
        user_invocable = user_invocable.strip().lower() not in {"false", "no", "0"}
    if not bool(user_invocable):
        return None
    return {
        "id": f"file-command:{source}:{path}",
        "name": name,
        "command": name,
        "label": f"/{name}",
        "description": description,
        "template": template,
        "search_text": f"{name} {description} {source}",
        "type": "template",
        "kind": "template",
        "source": source,
        "source_path": str(path),
        "argument_hint": argument_hint,
        "argument_names": argument_names,
        "base_dir": str(path.parent),
        "is_skill_file": path.name.casefold() == "skill.md",
        "enabled": True,
        "availability": _availability(),
    }


def get_file_command_catalog(
    workspace_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    root = (
        Path(workspace_root).resolve()
        if workspace_root is not None
        else get_explicit_active_workspace_root()
    )
    commands: dict[str, dict[str, Any]] = {}
    for directory, source in _file_command_dirs(root):
        if not directory.is_dir():
            continue
        # CC's markdownConfigLoader recursively discovers commands and derives
        # colon-separated namespaces from subdirectories (for example
        # commands/review/security.md -> /review:security).
        all_paths = sorted(directory.rglob("*.md"), key=lambda item: str(item).casefold())
        # Match CC's transformSkillFiles: a directory containing SKILL.md is
        # represented by that file alone; sibling markdown files are not
        # exposed as separate commands.
        skill_dirs = {
            path.parent
            for path in all_paths
            if path.name.casefold() == "skill.md"
        }
        paths = [
            path for path in all_paths
            if path.parent not in skill_dirs or path.name.casefold() == "skill.md"
        ]
        for path in paths:
            entry = _parse_file_command(path, source)
            if entry is not None:
                relative = path.relative_to(directory).with_suffix("")
                if path.name.casefold() == "skill.md":
                    skill_relative = path.parent.relative_to(directory)
                    entry["name"] = ":".join(skill_relative.parts).strip().lower()
                else:
                    entry["name"] = ":".join(relative.parts).strip().lower()
                if not entry["name"]:
                    entry["name"] = path.parent.name.strip().lower()
                entry["command"] = entry["name"]
                entry["label"] = f"/{entry['name']}"
                entry["search_text"] = (
                    f"{entry['name']} {entry['description']} {source}"
                )
                commands.setdefault(str(entry["command"]), entry)
    return list(commands.values())


def get_composer_command_catalog() -> list[dict[str, Any]]:
    builtins = [deepcopy(entry) for entry in _COMPOSER_COMMAND_CATALOG]
    builtin_names = {str(entry.get("command") or "").casefold() for entry in builtins}
    return [
        *builtins,
        *(
            entry
            for entry in get_file_command_catalog()
            if str(entry.get("command") or "").casefold() not in builtin_names
        ),
    ]


def get_enabled_composer_command_catalog() -> list[dict[str, Any]]:
    return [
        entry
        for entry in get_composer_command_catalog()
        if bool(entry.get("enabled", True))
    ]


def get_local_composer_command_catalog() -> list[dict[str, Any]]:
    return [
        entry
        for entry in get_enabled_composer_command_catalog()
        if str(entry.get("type", "")).strip().lower() == "local"
    ]


def get_template_composer_command_catalog() -> list[dict[str, Any]]:
    return [
        entry
        for entry in get_enabled_composer_command_catalog()
        if str(entry.get("type", "")).strip().lower() == "template"
    ]


def get_composer_command_definition(command: str) -> dict[str, Any] | None:
    normalized = str(command or "").strip().lower().lstrip("/")
    if not normalized:
        return None
    for entry in get_composer_command_catalog():
        if str(entry.get("command", "")).strip().lower() == normalized:
            return entry
    return None
