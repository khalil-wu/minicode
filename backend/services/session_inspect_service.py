from __future__ import annotations

from typing import Any

from backend.agent.context_ledger import ContextLedger
from backend.agent.message import AgentEvent
from backend.agent.prompt_cache import (
    prompt_cache_effective_prompt_tokens,
    prompt_cache_hit_rate,
)
from backend.services.runtime_control_service import CommandOutcome


def _prompt_cache_stats(tracker_summary: dict[str, Any]) -> dict[str, Any]:
    cache_read = int(tracker_summary.get("cache_read_tokens") or 0)
    cache_write = int(tracker_summary.get("cache_creation_tokens") or 0)
    input_tokens = int(tracker_summary.get("input_tokens") or 0)
    authoritative_total = int(tracker_summary.get("prompt_cache_total_tokens") or 0)
    denominator = max(
        authoritative_total
        or prompt_cache_effective_prompt_tokens(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
        ),
        1,
    )
    hit_rate = (
        round(min(100.0, cache_read / denominator * 100), 1)
        if authoritative_total > 0 and cache_read > 0
        else prompt_cache_hit_rate(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
        )
    )
    return {
        "read_tokens": cache_read,
        "write_tokens": cache_write,
        "hit_rate": hit_rate,
        "denominator_tokens": denominator,
    }


def build_tasks_inspect_outcome(session_id: str, snapshot: dict[str, Any]) -> CommandOutcome:
    running_tasks = snapshot.get("running_tasks", [])
    summary = snapshot.get("task_summary", {})
    if running_tasks:
        preview = " | ".join(
            f"{task.get('kind', 'task')} ({task.get('status', 'unknown')})"
            for task in running_tasks[:3]
            if isinstance(task, dict)
        )
        message = f"Current session tasks: {preview}" if preview else "Current session tasks: no running tasks"
    else:
        message = "Current session tasks: no running tasks"
    return CommandOutcome(
        "tasks",
        message,
        data={
            "session_id": session_id,
            "task_summary": summary,
            "running_tasks": running_tasks,
        },
    )


def build_status_inspect_outcome(
    *,
    session_id: str,
    selected_model: str,
    permission_mode: str,
    mcp_status: list[dict[str, Any]],
    active_skills: list[str],
    runtime_snapshot: dict[str, Any],
) -> CommandOutcome:
    connected_mcp = [
        server for server in mcp_status if str(server.get("status", "")).strip().lower() == "connected"
    ]
    message = (
        f"Runtime status: model {selected_model or 'unknown'} | "
        f"mode {permission_mode} | "
        f"MCP connected {len(connected_mcp)}/{len(mcp_status)} | "
        f"active skills {len(active_skills)} | "
        f"running tasks {runtime_snapshot.get('task_summary', {}).get('running', 0)}"
    )
    return CommandOutcome(
        "status",
        message,
        data={
            "session_id": session_id,
            "selected_model": selected_model,
            "permission_mode": permission_mode,
            "mcp": mcp_status,
            "active_skills": active_skills,
            "runtime": runtime_snapshot,
        },
    )


def build_permissions_inspect_outcome(
    *,
    session_id: str,
    conversation_id: str,
    rules: dict[str, Any],
) -> CommandOutcome:
    message = (
        f"Permission mode: {rules['mode']} | "
        f"session deny {len(rules['session_deny'])} | "
        f"overrides {len(rules['session_overrides'])} | "
        f"system deny {len(rules['system_deny'])}"
    )
    return CommandOutcome(
        "permissions",
        message,
        data={
            "session_id": session_id,
            "conversation_id": conversation_id,
            "rules": rules,
        },
    )


def build_usage_inspect_result(
    *,
    session_id: str,
    conversation_id: str,
    tracker_summary: dict[str, Any],
    budget_snapshot: dict[str, Any],
    context_ledger: ContextLedger | None = None,
) -> tuple[AgentEvent, AgentEvent, CommandOutcome]:
    used = int(budget_snapshot.get("used") or 0)
    total = int(budget_snapshot.get("total") or 0)
    percent = round((used / total) * 100, 1) if total > 0 else 0.0
    cost = float(tracker_summary.get("total_cost_usd") or 0.0)
    input_tokens = int(tracker_summary.get("input_tokens") or 0)
    output_tokens = int(tracker_summary.get("output_tokens") or 0)
    prompt_cache = _prompt_cache_stats(tracker_summary)
    cache_message = ""
    if prompt_cache["read_tokens"] or prompt_cache["write_tokens"]:
        cache_message = (
            f" | prompt cache read {prompt_cache['read_tokens']} "
            f"write {prompt_cache['write_tokens']} "
            f"hit {prompt_cache['hit_rate']}%"
        )
    message = (
        f"Usage: context {used}/{total} tokens ({percent}%) | "
        f"session API tokens in {input_tokens} out {output_tokens} | "
        f"estimated session cost ${cost:.4f}"
        f"{cache_message}"
    )

    clean_conversation_id = str(conversation_id or "").strip()
    scoped_budget_snapshot = dict(budget_snapshot)
    if clean_conversation_id:
        scoped_budget_snapshot["conversation_id"] = clean_conversation_id
    budget_event = AgentEvent(type="budget_update", data=scoped_budget_snapshot)

    context_usage: dict[str, Any] = {"used": used, "limit": total}
    if clean_conversation_id:
        context_usage["conversation_id"] = clean_conversation_id
    if context_ledger:
        context_usage["ledger"] = context_ledger
    context_event = AgentEvent(type="context_usage", data=context_usage)

    outcome = CommandOutcome(
        "usage",
        message,
        data={
            "session_id": session_id,
            "conversation_id": clean_conversation_id or None,
            "cost": tracker_summary,
            "cost_scope": str(tracker_summary.get("scope") or "session"),
            "prompt_cache": prompt_cache,
            "budget": budget_snapshot,
            "context_ledger": context_ledger or {},
        },
    )
    return budget_event, context_event, outcome
