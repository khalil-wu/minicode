from __future__ import annotations

from typing import Any, Callable

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.subagent_catalog import BUILTIN_AGENT_TYPES
from backend.tools.subagent_result import append_subagent_report_contract
from backend.tools.toolsets import ToolsetPolicy

_COORDINATOR_MODE_METADATA_KEYS = frozenset(
    {
        "agent_mode",
        "agentMode",
        "swarm_mode",
        "swarmMode",
        "agent_role",
        "agentRole",
        "mode",
        "coordinator",
        "coordinator_mode",
        "coordinatorMode",
        "coordinator_trigger",
    }
)
_INTERNAL_RUNTIME_METADATA_KEYS = frozenset(
    {
        "_agent_state",
        "_current_tool_call_id",
        "_streamed_tool_output_ids",
    }
)

# Tools a coordinator leader is blocked from calling directly. When the leader
# delegates to a worker, these denies must NOT propagate — the worker is the
# one doing the actual execution.
_COORDINATOR_LEADER_DENIED_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "grep_files",
        "glob_files",
        "fuzzy_search",
        "run_command",
        "web_search",
        "web_fetch",
    }
)

# Tools a subagent must not call, even when the parent is in bypass/accept-edits
# mode. Subagents report back through their task result; they should not spawn
# more agents, mutate parent-visible plans/todos, ask the user directly, or use
# the shared coordination board as an escape hatch.
SUBAGENT_DENIED_TOOLS = frozenset(
    {
        "task",
        "task_stop",
        "task_status",
        "workflow",
        "send_message",
        "message_list",
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


def _is_coordinator_leader(parent_context: ToolExecutionContext | None) -> bool:
    """Detect whether the parent agent is running in coordinator (leader) mode."""
    if parent_context is None:
        return False
    metadata = parent_context.metadata if isinstance(parent_context.metadata, dict) else {}
    for key in ("agent_mode", "agentMode", "swarm_mode", "swarmMode", "agent_role", "agentRole", "mode"):
        value = str(metadata.get(key) or "").strip().lower()
        if value in {"coordinator", "swarm_coordinator", "leader"}:
            return True
    for key in ("coordinator", "coordinator_mode", "coordinatorMode"):
        raw = metadata.get(key)
        if isinstance(raw, bool) and raw:
            return True
        if isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}:
            return True
    source = str(getattr(parent_context.permission, "source", "") or "")
    return source.startswith("coordinator")


def sanitize_subagent_runtime_metadata(parent_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Copy parent metadata for a worker without inheriting leader-only mode flags."""
    source = parent_metadata if isinstance(parent_metadata, dict) else {}
    cleaned = {
        key: value
        for key, value in source.items()
        if key not in _COORDINATOR_MODE_METADATA_KEYS
        and key not in _INTERNAL_RUNTIME_METADATA_KEYS
        and not str(key).startswith("_")
    }
    parent_mode = str(
        source.get("agent_mode")
        or source.get("agentMode")
        or source.get("swarm_mode")
        or source.get("swarmMode")
        or source.get("mode")
        or ""
    ).strip()
    if parent_mode:
        cleaned["parent_agent_mode"] = parent_mode
    return cleaned


def build_subagent_permission_context(
    agent_type: str,
    parent_context: ToolExecutionContext | None,
    *,
    extra_deny_rules: list[str] | None = None,
) -> PermissionContext:
    parent_permission = parent_context.permission if parent_context else PermissionContext()
    if parent_permission.mode == "plan" or agent_type in {"explore", "plan", "verification"}:
        mode = "plan"
    else:
        mode = parent_permission.mode

    deny_rules = _append_unique_rules(list(parent_permission.tool_deny_rules), SUBAGENT_DENIED_TOOLS)

    # Permission sync bridge: when the parent is a coordinator leader, its
    # tool_deny_rules contain the execution tools the leader is blocked from
    # calling directly (read_file, write_file, run_command, etc.). A worker
    # spawned by the leader needs those tools to do its job, so we strip the
    # leader-specific denies while keeping user/policy deny rules and all
    # filesystem constraints (path denylists etc.) intact.
    if _is_coordinator_leader(parent_context):
        deny_rules = [
            rule
            for rule in deny_rules
            if rule not in _COORDINATOR_LEADER_DENIED_TOOLS
        ]

    # Custom-agent restrictions are user policy, not coordinator leader policy.
    # Append them after removing leader-only denies so they cannot be stripped.
    if extra_deny_rules:
        deny_rules = _append_unique_rules(deny_rules, list(extra_deny_rules))

    return PermissionContext(
        mode=mode,
        session_overrides=dict(parent_permission.session_overrides),
        tool_deny_rules=deny_rules,
        filesystem_constraints=dict(parent_permission.filesystem_constraints),
        source=f"subagent:{agent_type}",
    )


_SUBAGENT_NOTES = (
    "Notes:\n"
    "- Your working directory is reset between run_command calls; always use absolute paths, "
    "never relative paths or bare `cd`.\n"
    "- In your final report, refer to shared files by absolute path so the caller can locate them.\n"
    "- Include code snippets only when the exact text is load-bearing (a bug you found, a signature "
    "the caller needs); do not recap code you merely read.\n"
    "- Keep the report concise: the caller relays it to the user, so cover only the essentials."
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
            return append_subagent_report_contract(
                f"{custom.prompt}\n\n{_SUBAGENT_NOTES}\n\nTask:\n{prompt}"
            )
    if agent_type == "explore":
        role_note = (
            "You are a read-only exploration subagent. Inspect the codebase and return concise findings "
            "with file references. Do not edit files or run mutating commands."
        )
    elif agent_type == "plan":
        role_note = (
            "You are a read-only planning research subagent. Gather context for the main agent's plan, "
            "return the relevant findings, and do not edit files or run mutating commands."
        )
    elif agent_type == "implement":
        role_note = (
            "You are an implementation subagent. Complete only the bounded work in this prompt, "
            "then summarize changed files and verification."
        )
    elif agent_type == "verification":
        role_note = (
            "You are a verification specialist. Try to break the result with real read-only checks. "
            "Do not modify project files or install packages. "
            "Return VERDICT: PASS, VERDICT: FAIL, or VERDICT: PARTIAL with concise evidence."
        )
    else:
        role_note = (
            "You are a general-purpose subagent. Complete the bounded task and return a concise summary."
        )
    return append_subagent_report_contract(f"{role_note}\n\n{_SUBAGENT_NOTES}\n\nTask:\n{prompt}")
