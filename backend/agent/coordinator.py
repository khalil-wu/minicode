from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from backend.feature_flags import feature_enabled
from backend.tools.toolsets import ToolsetPolicy
from backend.agent.runtime import MAX_CONCURRENT_SUBAGENTS


COORDINATOR_ALLOWED_TOOL_NAMES = frozenset(
    {
        # Delegate / stop / inspect agents.
        "task",
        "task_stop",
        "task_status",
        "workflow",
        # Agent-to-agent communication.
        "send_message",
        "message_list",
        # Shared swarm board.
        "task_create",
        "task_list",
        "task_get",
        "task_update",
        "task_output",
        "team_create",
        "team_list",
        "team_delete",
        # Keep the user-visible plan coherent while coordinating.
        "update_plan",
        "todo_write",
        "todo_read",
    }
)

COORDINATOR_GUIDANCE = (
    "Coordinator mode is active. You are the swarm leader: delegate work with "
    "task/workflow, communicate with send_message, manage shared swarm tasks/teams, "
    "and stop or inspect subagents. Do not read, write, search, or run commands directly; "
    "delegate execution and verification to subagents. When required subagents or workflow "
    "steps finish, synthesize their results directly for the user in the final answer. "
    "For background work, collect a delegation batch with one task_status call using "
    "subagent_ids and wait_seconds (up to 30), instead of one call per worker, repeated "
    "status checks, or sleep-based polling. "
    "Do not end the turn with orchestration narration such as saying you will poll, collect, "
    "read artifacts, or write the final report."
)
COORDINATOR_DELEGATION_TOOL_NAMES = frozenset({"task", "workflow"})
COORDINATOR_ACTIVE_DELEGATION_LIMIT = 5

_COORDINATOR_MODE_KEYS = frozenset(
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
    }
)
_ORCHESTRATION_WORD_RE = re.compile(
    r"(?:\bagents?\b|\bsubagents?\b|\bworkers?\b|\bworkflow\b|\bpipeline\b|"
    r"多\s*agent|multi[-\s]?agent|工作流|智能体|子代理|子任务)",
    re.IGNORECASE,
)
_DELEGATION_WORD_RE = re.compile(
    r"(?:分头|分工|并行|多路|多线|多角度|分别|拆分|"
    r"\bparallel(?:ize)?\b|\bdelegate\b|\bfan[-\s]?out\b|\bsplit\b)",
    re.IGNORECASE,
)
_USE_ORCHESTRATION_RE = re.compile(
    r"(?:用|使用|启动|创建|跑|开|派|分配|交给|调用|"
    r"\buse\b|\busing\b|\bwith\b|\bvia\b|\bstart\b|\bspawn\b|\blaunch\b|\brun\b|\bcreate\b)"
    r".{0,32}"
    r"(?:\bagents?\b|\bsubagents?\b|\bworkers?\b|\bworkflow\b|\bpipeline\b|"
    r"多\s*agent|multi[-\s]?agent|工作流|智能体|子代理|子任务)",
    re.IGNORECASE | re.DOTALL,
)
_ORCHESTRATION_USE_RE = re.compile(
    r"(?:\bagents?\b|\bsubagents?\b|\bworkers?\b|\bworkflow\b|\bpipeline\b|"
    r"多\s*agent|multi[-\s]?agent|工作流|智能体|子代理|子任务)"
    r".{0,32}"
    r"(?:分头|分工|并行|多路|多线|多角度|分别|拆分|"
    r"\bparallel(?:ize)?\b|\bdelegate\b|\bfan[-\s]?out\b|\bsplit\b)",
    re.IGNORECASE | re.DOTALL,
)
_DELEGATION_USE_RE = re.compile(
    r"(?:分头|分工|并行|多路|多线|多角度|分别|拆分|"
    r"\bparallel(?:ize)?\b|\bdelegate\b|\bfan[-\s]?out\b|\bsplit\b)"
    r".{0,32}"
    r"(?:\bagents?\b|\bsubagents?\b|\bworkers?\b|\bworkflow\b|\bpipeline\b|"
    r"多\s*agent|multi[-\s]?agent|工作流|智能体|子代理|子任务)",
    re.IGNORECASE | re.DOTALL,
)
_NO_ORCHESTRATION_RE = re.compile(
    r"(?:不要|别|不用|无需|禁止|别用|不要用|不需要|"
    r"\bno\b|\bwithout\b|\bdon't\b|\bdo not\b|\bnot\s+use\b)"
    r".{0,24}"
    r"(?:\bagents?\b|\bsubagents?\b|\bworkers?\b|\bworkflow\b|\bpipeline\b|"
    r"多\s*agent|multi[-\s]?agent|工作流|智能体|子代理|子任务)",
    re.IGNORECASE | re.DOTALL,
)


def coordinator_mode_enabled(metadata: dict[str, Any] | None) -> bool:
    if not feature_enabled("coordinator_mode", True):
        return False
    data = metadata if isinstance(metadata, dict) else {}
    raw_values = {
        str(data.get("agent_mode") or data.get("agentMode") or "").strip().lower(),
        str(data.get("swarm_mode") or data.get("swarmMode") or "").strip().lower(),
        str(data.get("agent_role") or data.get("agentRole") or "").strip().lower(),
        str(data.get("mode") or "").strip().lower(),
    }
    if any(value in {"coordinator", "swarm_coordinator", "leader"} for value in raw_values):
        return True
    return any(
        _truthy(data.get(key))
        for key in ("coordinator", "coordinator_mode", "coordinatorMode")
    )


def coordinator_intent_detected(user_message: str) -> bool:
    """Return true when the user asks for agent/workflow-style delegation.

    This mirrors cc's coordinator trigger at the intent layer: natural language
    such as "可以用 agents/workflow 分头看" should route the turn through the
    orchestration toolset instead of tempting the model with direct read/search
    tools. Plain mentions like "Show Agents 没看到" deliberately do not match.
    """
    text = str(user_message or "").strip()
    if not text or _NO_ORCHESTRATION_RE.search(text):
        return False
    if _USE_ORCHESTRATION_RE.search(text):
        return True
    if _ORCHESTRATION_USE_RE.search(text) or _DELEGATION_USE_RE.search(text):
        return True
    return bool(_ORCHESTRATION_WORD_RE.search(text) and _DELEGATION_WORD_RE.search(text))


def maybe_enable_coordinator_from_user_message(
    metadata: dict[str, Any] | None,
    user_message: str,
) -> dict[str, Any]:
    """Promote a turn to coordinator mode when the user's wording asks for it."""
    data = dict(metadata or {})
    if coordinator_mode_enabled(data):
        return data
    if not feature_enabled("coordinator_mode", True):
        return data
    if _has_explicit_mode_metadata(data):
        return data
    if not coordinator_intent_detected(user_message):
        return data
    data["coordinator"] = True
    data["agent_mode"] = "coordinator"
    data["coordinator_trigger"] = "user_intent"
    return data


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _has_explicit_mode_metadata(metadata: dict[str, Any]) -> bool:
    return any(
        str(metadata.get(key) or "").strip()
        for key in _COORDINATOR_MODE_KEYS
        if key in metadata
    )


def coordinator_toolset_policy() -> ToolsetPolicy:
    return ToolsetPolicy(
        enabled_toolsets=frozenset(),
        enabled_tools=COORDINATOR_ALLOWED_TOOL_NAMES,
    )


def coordinator_tool_block_reason(tool_name: str, metadata: dict[str, Any] | None) -> str:
    if not coordinator_mode_enabled(metadata):
        return ""
    if tool_name in COORDINATOR_ALLOWED_TOOL_NAMES:
        return ""
    return (
        f"Coordinator mode blocks direct tool '{tool_name}'. "
        "Use task/workflow to delegate execution, send_message for coordination, "
        "or the shared swarm task/team tools to manage work."
    )


def coordinator_delegation_block_reason(
    tool_name: str,
    args: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    *,
    state: Any = None,
) -> str:
    """Block repeated or premature coordinator delegation.

    Coordinator mode should collect and synthesize existing delegated work before
    spawning more workers. This is deliberately scoped to task/workflow so normal
    task_status, task_get, and messaging tools remain available.
    """
    if tool_name not in COORDINATOR_DELEGATION_TOOL_NAMES:
        return ""
    if not coordinator_mode_enabled(metadata):
        return ""

    call_args = args if isinstance(args, dict) else {}
    if tool_name == "workflow" and str(call_args.get("workflow_id") or "").strip() and not call_args.get("steps"):
        return ""

    runtime = metadata.get("agent_runtime") if isinstance(metadata, dict) else None
    parent_run_id = str((metadata or {}).get("run_id") or "").strip() if isinstance(metadata, dict) else ""
    conversation_id = str((metadata or {}).get("conversation_id") or "").strip() if isinstance(metadata, dict) else ""
    if runtime is None or not parent_run_id:
        return ""

    try:
        snapshot = runtime.list_runs(
            conversation_id=conversation_id,
            include_subagents=True,
        )
    except Exception:
        return ""

    subagents = [
        item
        for item in snapshot.get("subagents", [])
        if isinstance(item, dict)
        and str(item.get("parent_run_id") or "") == parent_run_id
        and bool(item.get("background", False))
        and bool(item.get("required_for_final", True))
    ]
    swarm_tasks = [
        item
        for item in snapshot.get("swarm_tasks", [])
        if isinstance(item, dict)
        and bool(item.get("required_for_final", True))
        and (
            str(item.get("created_by") or "") == parent_run_id
            or any(str(agent.get("task_id") or "") == str(item.get("task_id") or "") for agent in subagents)
        )
    ]

    collected_subagents = _collected_subagent_ids(state)
    collected_tasks = _collected_task_ids(state)
    collected_tasks.update(
        str(item.get("task_id") or "")
        for item in subagents
        if str(item.get("subagent_id") or "") in collected_subagents
        and str(item.get("task_id") or "")
    )
    uncollected_subagents = [
        _subagent_label(item)
        for item in subagents
        if str(item.get("status") or "") != "running"
        and bool(item.get("result_available"))
        and str(item.get("subagent_id") or "") not in collected_subagents
    ]
    uncollected_tasks = [
        _task_label(item)
        for item in swarm_tasks
        if str(item.get("status") or "") == "completed"
        and isinstance(item.get("outputs"), list)
        and len(item.get("outputs") or []) > 0
        and str(item.get("task_id") or "") not in collected_tasks
    ]
    if uncollected_subagents or uncollected_tasks:
        parts = []
        if uncollected_subagents:
            parts.append(f"subagent results: {_compact_join(uncollected_subagents)}")
        if uncollected_tasks:
            parts.append(f"workflow outputs: {_compact_join(uncollected_tasks)}")
        return (
            "Coordinator delegation blocked: required delegated results are already available "
            f"but not collected ({'; '.join(parts)}). Use task_status with include_result=true "
            "and task_get for workflow outputs before starting more delegated work."
        )

    requested = _requested_delegation_labels(tool_name, call_args)
    duplicate = _matching_existing_delegation(requested, subagents, swarm_tasks)
    if duplicate:
        return (
            "Coordinator delegation blocked: similar delegated work already exists "
            f"({duplicate}). Use task_status/task_get to inspect it, task_stop if it is wrong, "
            "or synthesize the existing result instead of starting a duplicate worker."
        )

    active_delegations: set[str] = set()
    for item in subagents:
        if (
            str(item.get("status") or "running") == "running"
            or str(item.get("background_task") or "") == "running"
        ):
            active_delegations.add(_active_delegation_key(item, fallback_prefix="subagent"))
    for item in swarm_tasks:
        if str(item.get("status") or "") in {"pending", "in_progress", "blocked"}:
            active_delegations.add(_active_delegation_key(item, fallback_prefix="task"))
    active_count = len(active_delegations)
    if active_count >= COORDINATOR_ACTIVE_DELEGATION_LIMIT:
        return (
            "Coordinator delegation blocked: the active delegated-work budget is full "
            f"({active_count} active item(s)). Collect progress with task_status/task_list/task_get, "
            "stop unnecessary work, or synthesize current results before starting more workers."
        )

    # Concurrency guard: enforce global subagent cap (plan §11.3).
    running_subagents = sum(
        1
        for item in subagents
        if str(item.get("status") or "running") == "running"
    )
    if running_subagents >= MAX_CONCURRENT_SUBAGENTS:
        return (
            f"Coordinator delegation blocked: maximum concurrent subagents reached "
            f"({running_subagents} running, limit={MAX_CONCURRENT_SUBAGENTS}). "
            "Wait for some to finish or stop unnecessary work before starting more."
        )

    # Evidence conflicts between retained subagent results are NOT a delegation
    # block: the feedback tells the model to "delegate targeted verification
    # tasks", which a hard block would forbid — producing a self-contradictory
    # wedge that re-triggers every delegation (results are append-only). The
    # same guidance is already surfaced as NON-blocking advice on task_status, so
    # delegation proceeds here and the coordinator decides how to reconcile.
    return ""


def coordinator_finalization_feedback(
    *,
    runtime: Any,
    parent_run_id: str,
    conversation_id: str,
    state: Any,
    candidate_text: str = "",
) -> str:
    """Return model-facing guidance when a coordinator is finalizing too early.

    Background subagents and workflow nodes report through runtime state, not
    automatically into the coordinator's prompt. This guard keeps the leader
    from ending the turn before required delegated results are available in its
    own model context.
    """
    parent_run_id = str(parent_run_id or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if not parent_run_id or runtime is None:
        return ""

    try:
        snapshot = runtime.list_runs(
            conversation_id=conversation_id,
            include_subagents=True,
        )
    except Exception:
        return ""

    subagents = [
        item
        for item in snapshot.get("subagents", [])
        if isinstance(item, dict)
        and str(item.get("parent_run_id") or "") == parent_run_id
        and bool(item.get("background", False))
        and bool(item.get("required_for_final", True))
    ]
    swarm_tasks = [
        item
        for item in snapshot.get("swarm_tasks", [])
        if isinstance(item, dict)
        and bool(item.get("required_for_final", True))
        and (
            str(item.get("created_by") or "") == parent_run_id
            or any(str(agent.get("task_id") or "") == str(item.get("task_id") or "") for agent in subagents)
        )
    ]

    collected_subagents = _collected_subagent_ids(state)
    collected_tasks = _collected_task_ids(state)
    collected_tasks.update(
        str(item.get("task_id") or "")
        for item in subagents
        if str(item.get("subagent_id") or "") in collected_subagents
        and str(item.get("task_id") or "")
    )

    running_subagents = [
        _subagent_label(item)
        for item in subagents
        if str(item.get("status") or "running") == "running"
        or str(item.get("background_task") or "") == "running"
    ]
    uncollected_subagents = [
        _subagent_label(item)
        for item in subagents
        if str(item.get("status") or "") != "running"
        and bool(item.get("result_available"))
        and str(item.get("subagent_id") or "") not in collected_subagents
    ]

    active_tasks = [
        _task_label(item)
        for item in swarm_tasks
        if str(item.get("status") or "") in {"pending", "in_progress", "blocked"}
    ]
    uncollected_tasks = [
        _task_label(item)
        for item in swarm_tasks
        if str(item.get("status") or "") == "completed"
        and isinstance(item.get("outputs"), list)
        and len(item.get("outputs") or []) > 0
        and str(item.get("task_id") or "") not in collected_tasks
    ]

    if running_subagents or active_tasks:
        parts = []
        if running_subagents:
            parts.append(f"running subagents: {_compact_join(running_subagents)}")
        if active_tasks:
            parts.append(f"workflow tasks not complete: {_compact_join(active_tasks)}")
        return (
            "Required delegated work is not ready for a final answer yet "
            f"({'; '.join(parts)}). Use task_status for running subagents and "
            "task_get/task_list for workflow tasks before synthesizing the user-facing answer. "
            "If you are only acknowledging that delegated work is still running, give a concise "
            "status update without claiming the task is complete."
        )

    if uncollected_subagents or uncollected_tasks:
        parts = []
        if uncollected_subagents:
            parts.append(f"subagent results: {_compact_join(uncollected_subagents)}")
        if uncollected_tasks:
            parts.append(f"workflow outputs: {_compact_join(uncollected_tasks)}")
        return (
            "Required delegated results are available but not yet collected into your context "
            f"({'; '.join(parts)}). Call task_status with include_result=true for each subagent "
            "to collect the summary-first result, use detail_level=\"full\" only when raw detail is needed, "
            "and call task_get for workflow outputs before giving the final answer."
        )

    return ""


def _is_delegated_work_status_update(candidate_text: str) -> bool:
    text = str(candidate_text or "").strip().lower()
    if not text:
        return False
    waiting_signal = re.search(
        r"(?:已启动|已开始|正在|运行中|处理中|等待|稍后|结果回来|完成后|完成时|"
        r"分工|执行项|工作流|子任务|"
        r"\bstarted\b|\blaunched\b|\brunning\b|\bin progress\b|\bwaiting\b|"
        r"\bwhen (?:it|they|the work|the tasks?).{0,24}completes?\b|"
        r"\bonce (?:it|they|the work|the tasks?).{0,24}completes?\b|"
        r"\breport back\b|\bfollow up\b)",
        text,
        re.IGNORECASE,
    )
    if not waiting_signal:
        return False

    completion_claim = re.search(
        r"(?:已完成|完成了|已经完成|修复完成|实现完成|最终答案|最终结论|结论如下|总结如下|"
        r"\ball done\b|\bcompleted\b|\bfixed\b|\bfinal answer\b)",
        text,
        re.IGNORECASE,
    )
    future_completion = re.search(
        r"(?:完成后|完成时|结果回来|when .{0,32}completes?|once .{0,32}completes?)",
        text,
        re.IGNORECASE,
    )
    return not completion_claim or bool(future_completion)


def _requested_delegation_labels(tool_name: str, args: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    if tool_name == "task":
        parallel_tasks = args.get("parallel_tasks")
        if isinstance(parallel_tasks, list):
            for item in parallel_tasks:
                if isinstance(item, dict):
                    labels.append(_request_label(item))
        else:
            labels.append(_request_label(args))
    elif tool_name == "workflow":
        workflow_name = str(args.get("name") or "").strip()
        steps = args.get("steps")
        if isinstance(steps, list):
            for item in steps:
                if isinstance(item, dict):
                    label = _request_label(item)
                    labels.append(f"{workflow_name}: {label}" if workflow_name and label else label)
        elif workflow_name:
            labels.append(workflow_name)
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        text = label.strip()
        key = _normalize_delegation_text(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _request_label(item: dict[str, Any]) -> str:
    return str(
        item.get("objective")
        or item.get("description")
        or item.get("title")
        or item.get("prompt")
        or ""
    ).strip()


def _matching_existing_delegation(
    requested: list[str],
    subagents: list[dict[str, Any]],
    swarm_tasks: list[dict[str, Any]],
) -> str:
    if not requested:
        return ""
    existing: list[str] = []
    for item in subagents:
        existing.append(_subagent_label(item))
    for item in swarm_tasks:
        existing.append(_task_label(item))

    for want in requested:
        for label in existing:
            if _delegation_text_similar(want, label):
                return label
    return ""


def _normalize_delegation_text(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"subagent-[\w-]+|swarm_task_[\w-]+|workflow-[\w-]+", " ", text)
    text = re.sub(r"task[_\s-]?id\s*=\s*[\w-]+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _delegation_text_similar(left: str, right: str) -> bool:
    a = _normalize_delegation_text(left)
    b = _normalize_delegation_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 24 and (a in b or b in a):
        return True
    if re.search(r"[\u4e00-\u9fff]", a + b):
        compact_a = a.replace(" ", "")
        compact_b = b.replace(" ", "")
        if min(len(compact_a), len(compact_b)) >= 4:
            shorter, longer = sorted((compact_a, compact_b), key=len)
            if shorter in longer:
                return True
            if max(len(compact_a), len(compact_b)) <= 40:
                grams_a = {compact_a[index:index + 2] for index in range(len(compact_a) - 1)}
                grams_b = {compact_b[index:index + 2] for index in range(len(compact_b) - 1)}
                shared = grams_a & grams_b
                if (
                    len(shared) >= 4
                    and SequenceMatcher(None, compact_a, compact_b).ratio() >= 0.60
                ):
                    return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if len(a_tokens) < 4 or len(b_tokens) < 4:
        return False
    overlap = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    return union > 0 and overlap / union >= 0.75


def _collected_subagent_ids(state: Any) -> set[str]:
    collected: set[str] = set()
    for record in getattr(state, "tool_calls", []) or []:
        if str(getattr(record, "tool_name", "") or "") != "task_status":
            continue
        tool_input = getattr(record, "tool_input", {}) or {}
        if not isinstance(tool_input, dict):
            continue
        if tool_input.get("include_result") is False:
            continue
        output = str(getattr(record, "tool_output", "") or "")
        subagent_ids = [str(tool_input.get("subagent_id") or "").strip()]
        raw_batch = tool_input.get("subagent_ids")
        if isinstance(raw_batch, list):
            subagent_ids.extend(str(value or "").strip() for value in raw_batch)
        for subagent_id in filter(None, subagent_ids):
            section = output
            if isinstance(raw_batch, list):
                match = re.search(
                    rf"(?ms)^###\s+{re.escape(subagent_id)}\s*$.*?(?=^###\s+|\Z)",
                    output,
                )
                section = match.group(0) if match else ""
            if "Result:" in section or "Error:" in section:
                collected.add(subagent_id)
    return collected


def _collected_task_ids(state: Any) -> set[str]:
    collected: set[str] = set()
    for record in getattr(state, "tool_calls", []) or []:
        if str(getattr(record, "tool_name", "") or "") != "task_get":
            continue
        if str(getattr(record, "status", "") or "") != "success":
            continue
        tool_input = getattr(record, "tool_input", {}) or {}
        if not isinstance(tool_input, dict):
            continue
        output = str(getattr(record, "tool_output", "") or "")
        if "Outputs:" not in output:
            continue
        task_id = str(tool_input.get("task_id") or "").strip()
        if task_id:
            collected.add(task_id)
    return collected


def _subagent_label(item: dict[str, Any]) -> str:
    return str(
        item.get("objective")
        or item.get("prompt_summary")
        or item.get("subagent_id")
        or "subagent"
    ).strip()


def _task_label(item: dict[str, Any]) -> str:
    return str(item.get("objective") or item.get("title") or item.get("task_id") or "task").strip()


def _active_delegation_key(item: dict[str, Any], *, fallback_prefix: str) -> str:
    task_id = str(item.get("task_id") or "").strip()
    if task_id:
        return f"task:{task_id}"
    workflow_id = str(item.get("workflow_id") or "").strip()
    node_id = str(item.get("node_id") or "").strip()
    if workflow_id and node_id:
        return f"workflow:{workflow_id}:{node_id}"
    item_id = str(item.get("subagent_id") or item.get("id") or "").strip()
    if item_id:
        return f"{fallback_prefix}:{item_id}"
    return f"{fallback_prefix}:{_normalize_delegation_text(_subagent_label(item) or _task_label(item))}"


def _compact_join(items: list[str], limit: int = 3) -> str:
    visible = [item for item in items if item][:limit]
    suffix = f", +{len(items) - limit} more" if len(items) > limit else ""
    return ", ".join(visible) + suffix
