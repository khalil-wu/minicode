from __future__ import annotations

from copy import deepcopy
from typing import Any


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
        "description": "Inspect token, context, and cost usage for the current session.",
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
        "name": "load_skill",
        "label": "load_skill",
        "description": "Activate a skill for the current session.",
        "type": "protocol",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="websocket"),
    },
    {
        "name": "unload_skill",
        "label": "unload_skill",
        "description": "Deactivate a skill for the current session.",
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
        "template": (
            "Please review this project and list findings ordered by severity. "
            "Include file references and concrete fixes."
        ),
        "search_text": "review audit code quality bug risk",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(),
    },
    {
        "id": "cmd-debug",
        "name": "debug",
        "command": "debug",
        "label": "/debug",
        "description": "端到端定位并修复当前问题",
        "template": (
            "Help me debug the current issue end to end: reproduce, isolate root cause, "
            "apply minimal fix, and verify."
        ),
        "search_text": "debug reproduce root cause fix verify",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(),
    },
    {
        "id": "cmd-refactor",
        "name": "refactor",
        "command": "refactor",
        "label": "/refactor",
        "description": "提出并执行小步重构方案",
        "template": (
            "Propose a safe refactor plan for this area, then implement it in small steps "
            "with validation after each step."
        ),
        "search_text": "refactor architecture cleanup maintainability",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(),
    },
    {
        "id": "cmd-test",
        "name": "test",
        "command": "test",
        "label": "/test",
        "description": "补测试并保证回归通过",
        "template": "Add or update tests for this change, explain coverage gaps, and run the relevant test suite.",
        "search_text": "test regression coverage vitest pytest",
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
        "description": "切换执行权限模式（默认进入 plan）",
        "template": "/plan",
        "search_text": "local plan mode default confirm bypass",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": False,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-permissions-local",
        "name": "permissions",
        "command": "permissions",
        "label": "/permissions",
        "description": "查看或切换权限模式，并管理规则",
        "template": "/permissions",
        "search_text": "local permissions mode rules list add remove deny override default plan confirm bypass",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-bypass",
        "name": "bypass",
        "command": "bypass",
        "label": "/bypass",
        "description": "Switch to bypass permission mode for advanced trusted sessions.",
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
        "description": "Set reasoning effort: low, medium, high, or max.",
        "template": "/effort high",
        "search_text": "local effort reasoning low medium high max",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-skills-local",
        "name": "skills",
        "command": "skills",
        "label": "/skills",
        "description": "打开 Skills/MCP 市场和管理页",
        "template": "/skills",
        "search_text": "local skills mcp marketplace store settings manage install",
        "type": "local",
        "kind": "local",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(scope="session"),
    },
    {
        "id": "cmd-docs",
        "name": "docs",
        "command": "docs",
        "label": "/docs",
        "description": "阅读代码并产出结构化文档",
        "template": "Read the relevant code and produce concise developer documentation with architecture, data flow, and key decisions.",
        "search_text": "docs documentation architecture data flow",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(),
    },
    {
        "id": "cmd-explain",
        "name": "explain",
        "command": "explain",
        "label": "/explain",
        "description": "解释关键模块和执行路径",
        "template": "Explain this code path clearly with key files, control flow, and practical examples.",
        "search_text": "explain walkthrough files control flow",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(),
    },
    {
        "id": "cmd-commit",
        "name": "commit",
        "command": "commit",
        "label": "/commit",
        "description": "整理改动并生成提交说明",
        "template": "Summarize the changes made, list validation performed, and propose a clean commit message.",
        "search_text": "commit summary changelog validation",
        "type": "template",
        "kind": "template",
        "source": "builtin",
        "enabled": True,
        "availability": _availability(),
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
)


def get_builtin_command_catalog() -> list[dict[str, Any]]:
    return [deepcopy(entry) for entry in _BUILTIN_COMMAND_CATALOG]


def get_builtin_command_names() -> list[str]:
    return [str(entry["name"]) for entry in _BUILTIN_COMMAND_CATALOG]


def get_composer_command_catalog() -> list[dict[str, Any]]:
    return [deepcopy(entry) for entry in _COMPOSER_COMMAND_CATALOG]


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
