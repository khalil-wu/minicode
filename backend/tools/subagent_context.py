from __future__ import annotations

from typing import Any, Callable

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.subagent_catalog import BUILTIN_AGENT_TYPES
from backend.tools.toolsets import ToolsetPolicy

_INTERNAL_RUNTIME_METADATA_KEYS = frozenset(
    {
        "_agent_state",
        "_current_tool_call_id",
        "_streamed_tool_output_ids",
    }
)

# Tools a subagent must not call, even when the parent is in bypass/accept-edits
# mode. Subagents report back through their task result; they should not spawn
# more agents, mutate parent-visible plans/todos, ask the user directly, or use
# the shared coordination board as an escape hatch. ``send_message`` and
# ``message_list`` are intentionally allowed: their runtime authorization keeps
# traffic inside the current task tree, and without them the advertised
# subagent-to-parent coordination path was unreachable.
SUBAGENT_DENIED_TOOLS = frozenset(
    {
        "task",
        "task_stop",
        "task_status",
        "team_create",
        "team_list",
        "team_delete",
        "task_create",
        "task_list",
        "task_get",
        "task_update",
        "task_output",
        "ask_user",
        "update_plan",
        "enter_plan_mode",
        "exit_plan_mode",
        "todo_write",
        "todo_read",
        "schedule_cron",
    }
)


def subagent_toolset_policy() -> ToolsetPolicy:
    return ToolsetPolicy.from_iterables(disabled_tools=SUBAGENT_DENIED_TOOLS)


def is_subagent_permission_context(permission: PermissionContext | None) -> bool:
    return str(getattr(permission, "source", "") or "").startswith("subagent:")


def _append_unique_rules(rules: list[str], additions: frozenset[str]) -> list[str]:
    seen = set(rules)
    for rule in sorted(additions):
        if rule not in seen:
            rules.append(rule)
            seen.add(rule)
    return rules


def sanitize_subagent_runtime_metadata(parent_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Copy public parent metadata without leaking internal runtime objects."""
    source = parent_metadata if isinstance(parent_metadata, dict) else {}
    return {
        key: value
        for key, value in source.items()
        if key not in _INTERNAL_RUNTIME_METADATA_KEYS
        and not str(key).startswith("_")
    }


def build_subagent_permission_context(
    agent_type: str,
    parent_context: ToolExecutionContext | None,
    *,
    read_only: bool = False,
    extra_deny_rules: list[str] | None = None,
) -> PermissionContext:
    parent_permission = parent_context.permission if parent_context else PermissionContext()
    if parent_permission.mode == "plan" or read_only or agent_type in {"explore", "plan"}:
        mode = "plan"
    else:
        mode = parent_permission.mode

    deny_rules = _append_unique_rules(list(parent_permission.tool_deny_rules), SUBAGENT_DENIED_TOOLS)

    if extra_deny_rules:
        deny_rules = _append_unique_rules(deny_rules, list(extra_deny_rules))

    return PermissionContext(
        mode=mode,
        session_overrides=dict(parent_permission.session_overrides),
        tool_deny_rules=deny_rules,
        filesystem_constraints=dict(parent_permission.filesystem_constraints),
        source=f"subagent:{agent_type}",
    )


def build_subagent_prompt(
    agent_type: str,
    prompt: str,
    *,
    get_custom_agent: Callable[[str], Any | None] | None = None,
) -> str:
    if agent_type not in BUILTIN_AGENT_TYPES and get_custom_agent is not None:
        custom = get_custom_agent(agent_type)
        if custom and getattr(custom, "prompt", ""):
            return f"{custom.prompt}\n\nTask:\n{prompt}".strip()
    if agent_type == "explore":
        role_note = (
            "You are a read-only exploration agent. Search and analyze the existing codebase or sources, "
            "then report the requested findings clearly. Do not modify files or system state."
        )
    elif agent_type == "plan":
        role_note = (
            "You are a read-only planning agent. Explore the codebase and produce the requested implementation plan. "
            "Do not modify files or system state."
        )
    else:
        role_note = (
            "You are a general-purpose agent. Handle the assigned task with the available tools "
            "and return the result to the parent agent."
        )
    return f"{role_note}\n\nTask:\n{prompt}".strip()
